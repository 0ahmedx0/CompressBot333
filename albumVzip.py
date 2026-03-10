import os
import tempfile
import subprocess
import threading
import time
import re
import json
import shutil # أُضيف لنسخ الملفات للألبوم
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
from pyrogram.errors import MessageEmpty, UserNotParticipant, MessageNotModified, FloodWait

from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# ---- التعديل رقم 1: المزامنة لـ 3 مهام كحد أقصى للتحميل و 3 للضغط ----
download_executor = ThreadPoolExecutor(max_workers=3)
compression_executor = ThreadPoolExecutor(max_workers=3)

# قواميس التخزين الأساسية
user_states = {}
user_settings = {}
user_video_data = {}
PROGRESS_TRACKER = {} # لتتبع وقت آخر تحديث لرسائل التقدم (تجنب الحظر FloodWait)

# ---- الإضافة للتحكم بالألبوم والمهام المتزامنة بالخلفية ----
user_active_tasks = {}       # يحفظ عدد الفيديوهات الجاري معالجتها حالياً للمستخدم
user_finished_files = {}     # يخزن مسارات الملفات التي اكتمل ضغطها لتكوين الألبوم
task_lock = threading.Lock() # لمنع التداخل بين الملفات المتزامنة

# ---- التعديل رقم 2: تحديث الإعدادات لتدعم النسبة المئوية والنمط ----
DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_quality_value': 30,
    'auto_mode': 'crf',         # إضافة: يحدد إذا التلقائي هو CRF أو نسبة (percent)
    'auto_percent_value': 50    # إضافة: النسبة التلقائية الافتراضية 50%
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

# -------------------------- دالة تتبع المهام (للألبوم) --------------------------
def check_and_prompt_album(user_id, client, chat_id):
    """ تتحقق إذا انتهت جميع العمليات بالخلفية للمستخدم لتعرض له الألبوم """
    with task_lock:
        if user_id in user_active_tasks:
            user_active_tasks[user_id] -= 1
            if user_active_tasks[user_id] <= 0:
                user_active_tasks[user_id] = 0
                files_ready = user_finished_files.get(user_id, [])
                if len(files_ready) > 1:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📦 إرسال الجميع كألبوم", callback_data="send_batch_album")],
                        [InlineKeyboardButton("🗑️ مسح الذاكرة وتجاهل الألبوم", callback_data="clear_batch_album")]
                    ])
                    client.send_message(
                        chat_id,
                        f"✅ **اكتملت جميع العمليات بالخلفية!**\nلديك ({len(files_ready)}) فيديوهات مضغوطة وجاهزة، هل ترغب في دمجها في رسالة واحدة (ألبوم)؟",
                        reply_markup=markup
                    )

# -------------------------- وظائف المساعدة وحساب الحجم والتقدم (كما هي بالضبط) --------------------------

def update_progress_msg(current, total, client, message, action, start_time, known_size=0):
    """
    دالة موحدة لتحديث رسائل التقدم مع استخدام حجم احتياطي مؤكد لضمان دقة الحسابات
    """
    now = time.time()
    msg_id = message.id
    
    if total <= 0 and known_size > 0:
        total = known_size

    is_finished = (current >= total) if total > 0 else False
    
    if msg_id in PROGRESS_TRACKER and (now - PROGRESS_TRACKER[msg_id]) < 5.0 and not is_finished:
        return

    PROGRESS_TRACKER[msg_id] = now
    
    percent = (current * 100 / total) if total > 0 else 0
    filled = int(percent / 10) if percent > 0 else 0
    if filled > 10: filled = 10
    bar = f"[{'█' * filled}{'░' * (10 - filled)}]"
    
    if "ضغط" in action:
        curr_val = f"{current:.1f} ثانية"
        total_val = f"{total:.1f} ثانية" if total > 0 else "??"
    else: 
        curr_val = f"{current / (1024 * 1024):.2f} MB"
        total_val = f"{total / (1024 * 1024):.2f} MB" if total > 0 else "??"

    elapsed = now - start_time
    
    speed_text = ""
    console_speed = ""  
    eta_text = "غير معروف" if total <= 0 else "جاري الحساب..."
    
    if elapsed > 0:
        speed = current / elapsed
        if speed > 0:
            if total > 0:
                eta_seconds = max(0, (total - current) / speed)
                eta_text = f"{int(eta_seconds)} ثانية"
            
            if "ضغط" not in action:
                speed_mb = speed / (1024 * 1024)
                speed_text = f"🚀 **السرعة:** `{speed_mb:.2f} MB/s`\n"
                console_speed = f"| السرعة: {speed_mb:.2f} MB/s "

    text = (
        f"{action}\n"
        f"{bar} `{percent:.1f}%`\n"
        f"📊 **التقدم:** `{curr_val} / {total_val}`\n"
        f"{speed_text}"
        f"⏱ **الوقت المتبقي:** `{eta_text}`"
    )

    clean_action = action.replace('*', '').replace('`', '').split('\n')[0].strip()
    console_log = f"[Task Msg:{msg_id}] {clean_action} | {percent:.1f}% | {curr_val} / {total_val} {console_speed}| المتبقي: {eta_text}"
    print(console_log)
    
    try:
        client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except FloodWait as e:
        time.sleep(e.value)
    except MessageNotModified:
        pass
    except Exception:
        pass
        
def get_video_info_and_thumb(file_path):
    """
    تستخرج المدة والأبعاد من الفيديو وتلتقط صورة مصغرة (Thumbnail) لتستخدمها تيليجرام
    """
    duration = 0.0
    width = 0
    height = 0
    thumb_path = None

    try:
        cmd = f'ffprobe -v quiet -print_format json -show_streams "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                width = int(stream.get('width', 0))
                height = int(stream.get('height', 0))
                duration = float(stream.get('duration', 0))
                break
        
        thumb_time = min(1.0, duration * 0.1) if duration > 0 else 1.0
        thumb_path = file_path + "_thumb.jpg"
        
        thumb_cmd = f'ffmpeg -y -ss {thumb_time} -i "{file_path}" -vframes 1 -vf "scale=320:-1" -q:v 5 "{thumb_path}" -loglevel quiet'
        subprocess.run(thumb_cmd, shell=True)
        
        if not os.path.exists(thumb_path):
            thumb_path = None
            
    except Exception as e:
        print(f"Error getting video info & thumb: {e}")
        
    return thumb_path, duration, width, height

def time_to_seconds(time_str):
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0

def get_telegram_duration(message):
    if message.video and message.video.duration:
        return float(message.video.duration)
    elif message.animation and message.animation.duration:
        return float(message.animation.duration)
    return 0

def get_video_duration(file_path):
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_format "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        return float(data['format']['duration'])
    except:
        return 0

def calculate_target_bitrate(target_size_mb, duration_seconds, audio_bitrate_kbps=128):
    if duration_seconds <= 0:
        return 500
    total_bitrate_kbps = (target_size_mb * 8192) / duration_seconds
    video_bitrate_kbps = int(total_bitrate_kbps - audio_bitrate_kbps)
    return max(50, video_bitrate_kbps)

def cleanup_downloads():
    print("Cleaning up downloads directory...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
        except Exception:
            pass

# -------------------------- تهيئة العميل --------------------------
app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

# -------------------------- وظائف المعالجة الأساسية (الضغط) --------------------------

def process_video_for_compression(video_data):
    thread_name = threading.current_thread().name
    file_path = video_data['file']
    message = video_data['message']
    button_message_id = video_data.get('button_message_id')
    quality = video_data['quality']
    user_id = video_data['user_id']
    user_prefs = get_user_settings(user_id)
    encoder = user_prefs['encoder']

    total_duration = get_telegram_duration(message)
    if total_duration <= 0:
        total_duration = get_video_duration(file_path)

    print(f"\n[{thread_name}] Original file: {os.path.basename(file_path)} | Size: {os.path.getsize(file_path)/(1024*1024):.2f}MB | Duration: {total_duration}s")

    if button_message_id and button_message_id in user_video_data:
        user_video_data[button_message_id]['processing_started'] = True
        try:
            status_text = f"⏳ تم وضع الفيديو في طابور المعالجة المتزامن..."
            app.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=button_message_id,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(status_text, callback_data="none")]])
            )
        except: pass

    temp_compressed_filename = None
    thumb_path = None

    try:
        if not os.path.exists(file_path):
            message.reply_text("❌ حدث خطأ: لم يتم العثور على الملف الأصلي للمعالجة.")
            return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        if isinstance(quality, dict) and 'target_size' in quality:
            target_size_mb = quality['target_size']
            try: audio_k = int(str(VIDEO_AUDIO_BITRATE).lower().replace('k', '').strip())
            except: audio_k = 128
            target_v_bitrate = calculate_target_bitrate(target_size_mb, total_duration, audio_k)
            print(f"[{thread_name}] Mode: EXACT SIZE. Target: {target_size_mb} MB | Target Video Bitrate: {target_v_bitrate}k")
            quality_settings = f"-b:v {target_v_bitrate}k -maxrate {target_v_bitrate}k -bufsize {target_v_bitrate*2}k -preset fast"
            used_mode_text = f"🎯 طلب حجم مستهدف (أو نسبة مئوية): ~{target_size_mb:.2f} MB"
        else:
            quality_value = int(quality.split('_')[1]) if isinstance(quality, str) and 'crf_' in quality else int(quality)
            print(f"[{thread_name}] Mode: QUALITY (CRF/CQ). Level: {quality_value}")
            preset = "fast"
            if quality_value <= 18: preset = "slow"
            elif quality_value <= 23: preset = "medium"
            elif quality_value >= 27: preset = "veryfast" if encoder == 'libx264' else "fast"
            quality_param = "cq" if "nvenc" in encoder else "crf"
            quality_settings = f"-{quality_param} {quality_value} -preset {preset}"
            used_mode_text = f"🎥 الجودة (CRF/CQ): {quality_value}"
        
        common_ffmpeg_part = (
            f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} '
            f'-c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
            f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -map_metadata -1'
        )
        ffmpeg_command = f'{common_ffmpeg_part} {quality_settings} -movflags +faststart "{temp_compressed_filename}"'

        progress_msg = message.reply_text("🔄 **بدأ ضغط الفيديو (متزامن)...**", quote=True)
        start_time = time.time()

        process = subprocess.Popen(ffmpeg_command, shell=True, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')

        for line in process.stderr:
            if total_duration > 0:
                time_match = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d+)", line)
                if time_match:
                    current_time_str = time_match.group(1)
                    current_time_sec = time_to_seconds(current_time_str)
                    update_progress_msg(
                        current=current_time_sec,
                        total=total_duration,
                        client=app,
                        message=progress_msg,
                        action="⚙️ **جاري المعالجة والضغط...**",
                        start_time=start_time
                    )

        process.wait()
        
        if process.returncode != 0:
            raise Exception("FFmpeg process crashed or failed.")
            
        try: progress_msg.delete()
        except: pass

        compressed_file_size_mb = os.path.getsize(temp_compressed_filename) / (1024 * 1024)
        print(f"[{thread_name}] Compression Done! New Size: {compressed_file_size_mb:.2f} MB")

        # ---- إضافة: حفظ الملف لاستخدامه لاحقاً في الألبوم ----
        try:
            album_copy_path = os.path.join(DOWNLOADS_DIR, f"album_file_{user_id}_{int(time.time()*100)}.mp4")
            shutil.copy2(temp_compressed_filename, album_copy_path)
            with task_lock:
                if user_id not in user_finished_files:
                    user_finished_files[user_id] = []
                user_finished_files[user_id].append(album_copy_path)
        except Exception as ex:
            print(f"Failed to copy file for album: {ex}")

        upload_progress_msg = message.reply_text("📤 اكتمل الضغط! بدأ الرفع النهائي كفيديو منفصل...", quote=True)
        upload_start_time = time.time()

        # استخراج الصورة المصغرة والمعلومات لجعل الفيديو Streamable
        thumb_path, vid_duration, vid_width, vid_height = get_video_info_and_thumb(temp_compressed_filename)

        # الرفع الفردي باستخدام reply_video كما طلبت عدم تغييره
        message.reply_video(
            video=temp_compressed_filename,
            progress=update_progress_msg,
            progress_args=(app, upload_progress_msg, "📤 **الرفع إلى التليجرام...**", upload_start_time),
            caption=f"📦 **النتيجة النهائية**\n"
                    f"🔻 الحجم القديم: {os.path.getsize(file_path) / (1024 * 1024):.2f} MB\n"
                    f"✅ الحجم الجديد: {compressed_file_size_mb:.2f} MB\n\n"
                    f"{used_mode_text}",
            duration=int(vid_duration),
            width=vid_width,
            height=vid_height,
            thumb=thumb_path, 
            supports_streaming=True 
        )
        
        try: upload_progress_msg.delete()
        except: pass
        
    except Exception as e:
        print(f"[{thread_name}] Processing error: {e}")
        message.reply_text(f"❌ حدث خطأ أثناء المعالجة أو الرفع:\n`{str(e)[:150]}`", quote=True)
    finally:
        # حذف الملفات المؤقتة الخاصة بـ ffmpeg
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            os.remove(temp_compressed_filename)
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)

        auto_compress_status_message_id = video_data.get('auto_compress_status_message_id')
        if auto_compress_status_message_id:
            try: app.delete_messages(chat_id=message.chat.id, message_ids=auto_compress_status_message_id)
            except: pass

        if button_message_id and button_message_id in user_video_data:
            user_video_data[button_message_id]['processing_started'] = False
            user_video_data[button_message_id]['quality'] = None
            try:
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("ضعيفة (CRF 27)", callback_data="crf_27"),
                     InlineKeyboardButton("متوسطة (CRF 23)", callback_data="crf_23"),
                     InlineKeyboardButton("عالية (CRF 18)", callback_data="crf_18")],
                    [InlineKeyboardButton("🎯 ضغط لحجم معين", callback_data="target_size_prompt"),
                     InlineKeyboardButton("📉 نسبة מئوية (%)", callback_data="target_percent_prompt")],
                    [InlineKeyboardButton("❌ إنهاء العملية (وحذف الأصلي)", callback_data="finish_process")]
                ])
                app.edit_message_text(
                    chat_id=message.chat.id, message_id=button_message_id,
                    text="✅ تمت المهمة بنجاح! للتحكم، يمكنك طلب تجربة جودة أو نسبة مئوية أخرى أم إنهاء العملية من هنا:",
                    reply_markup=markup)
            except: pass

        # عند الانتهاء (سواء نجاح أو فشل) استدعِ فحص الألبوم
        check_and_prompt_album(user_id, app, message.chat.id)


def auto_select_medium_quality(button_message_id):
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        if not video_data.get('processing_started'):
            video_data['quality'] = "crf_23"
            try:
                app.edit_message_reply_markup(
                    chat_id=video_data['message'].chat.id, message_id=button_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم تفعيل اختيار (متوسط) لانتهاء الوقت", callback_data="none")]]))
            except Exception: pass
            compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- معالجات رسائل تيليجرام (استقبال وأزرار) --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    settings_button = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]])
    message.reply_text(
        "👋 أهلاً بك! أرسل لي فيديوهات (ويمكنك إرسال مجموعة معاً للضغط المتزامن 3/3).\nيدعم البوت اختيار الجودة، الحجم، والنسبة المئوية، ويرسلها كألبوم بالنهاية.",
        reply_markup=settings_button, quote=True
    )

@app.on_message(filters.command("settings"))
def settings_command(client, message):
    send_settings_menu(client, message.chat.id, message.from_user.id)

@app.on_message(filters.text)
def handle_text_inputs(client, message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return
        
    state_data = user_states[user_id]
    state = state_data.get("state")
    
    # 1. إدخال الحجم (الميجا بايت) يدوي
    if state == "waiting_for_target_size":
        prompt_message_id = state_data.get("prompt_message_id")
        button_message_id = state_data.get("button_message_id")
        try:
            size = float(message.text)
            if size <= 0: raise ValueError
            
            if button_message_id and button_message_id in user_video_data:
                video_data = user_video_data[button_message_id]
                if not video_data.get('processing_started') and video_data.get('file'):
                    video_data['quality'] = {"target_size": size}
                    try:
                        app.edit_message_reply_markup(
                            chat_id=message.chat.id, message_id=button_message_id,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎯 طلب الوصول لـ ~{size} MB استلم", callback_data="none")]]))
                    except Exception: pass
                    
                    compression_executor.submit(process_video_for_compression, video_data)
                    del user_states[user_id]
                else:
                    message.reply_text("❌ انتهت صلاحية هذا الزر.", quote=True)
            else: message.reply_text("❌ الجلسة غير متوفرة.", quote=True)
        except ValueError:
            message.reply_text("❌ خطأ، يرجى كتابة حجم الميغا رقمياً (مثال 5.5).", quote=True)
        finally:
            try: message.delete()
            except: pass

    # 2. إدخال النسبة المئوية (%) يدوي للفيديو المختار
    elif state == "waiting_for_percentage":
        button_message_id = state_data.get("button_message_id")
        try:
            pct = float(message.text)
            if not (1 <= pct <= 100): raise ValueError
            if button_message_id and button_message_id in user_video_data:
                video_data = user_video_data[button_message_id]
                if not video_data.get('processing_started') and video_data.get('file'):
                    # الحساب: (النسبة/100) * حجم الملف الأصلي بالميجا
                    orig_size_mb = os.path.getsize(video_data['file']) / (1024 * 1024)
                    target_mb = (pct / 100) * orig_size_mb
                    
                    video_data['quality'] = {"target_size": target_mb}
                    try:
                        app.edit_message_reply_markup(chat_id=message.chat.id, message_id=button_message_id,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"📉 معالجة نسبة {pct}%", callback_data="none")]]))
                    except Exception: pass
                    
                    compression_executor.submit(process_video_for_compression, video_data)
                    del user_states[user_id]
            else: message.reply_text("❌ انتهت صلاحية الزر.", quote=True)
        except ValueError: message.reply_text("❌ أرسل رقماً بين 1 و 100 فقط.")
        finally:
            try: message.delete()
            except: pass

    # 3. تغيير قيمة الـ CRF التلقائي (من الإعدادات)
    elif state == "waiting_for_cq_value":
        try:
            val = int(message.text)
            if 0 <= val <= 51:
                get_user_settings(user_id)['auto_quality_value'] = val
                message.reply_text(f"✅ تم حفظ قيمة الجودة التلقائية: CRF {val}")
                user_states.pop(user_id, None)
                send_settings_menu(client, message.chat.id, user_id)
            else:
                message.reply_text("❌ يرجى إدخال رقم بين 0 و 51.")
        except:
            message.reply_text("❌ يرجى إدخال رقم صحيح.")

    # 4. تغيير النسبة المئوية للتلقائي (من الإعدادات)
    elif state == "waiting_for_auto_percentage":
        try:
            val = float(message.text)
            if 1 <= val <= 100:
                get_user_settings(user_id)['auto_percent_value'] = val
                message.reply_text(f"✅ تم حفظ نسبة التلقائي بنجاح: {val}%")
                user_states.pop(user_id, None)
                send_settings_menu(client, message.chat.id, user_id)
            else: message.reply_text("❌ يرجى إدخال نسبة بين 1 و 100.")
        except: message.reply_text("❌ إدخال خاطئ.")


def send_settings_menu(client, chat_id, user_id, message_id=None):
    settings = get_user_settings(user_id)
    encoder_text = {"hevc_nvenc": "H.265 (HEVC)","h264_nvenc": "H.264 (NVENC)","libx264": "H.264 (CPU)"}.get(settings['encoder'], "-")
    auto_compress_text = "✅ مفعل" if settings['auto_compress'] else "❌ معطل"
    mode_text = "تلقائي عبر (النسبة %)" if settings.get('auto_mode') == 'percent' else "تلقائي عبر (CRF)"

    text = (
        "**⚙️ قائمة الإعدادات والمحركات:**\n\n"
        f"🔹 **المُسرع (Encoder):** `{encoder_text}`\n"
        f"🔸 **الضغط التلقائي:** `{auto_compress_text}`\n"
        f"📊 **النمط الافتراضي:** `{mode_text}`\n"
        f"📈 **جودة CRF:** `{settings['auto_quality_value']}`  |  **نسبة %:** `{settings['auto_percent_value']}%`"
    )
    keyboard = [
        [InlineKeyboardButton("🔄 تغيير المُسرع / الترميز", callback_data="settings_encoder")],
        [InlineKeyboardButton(f"تبديل الوضع إلى: {'CRF' if settings['auto_mode'] == 'percent' else 'نسبة مئوية'}", callback_data="settings_toggle_mode")],
        [InlineKeyboardButton("✏️ ضبط قيمة (CRF) للتلقائي", callback_data="settings_custom_quality"),
         InlineKeyboardButton("📉 ضبط النسبة (%) للتلقائي", callback_data="settings_custom_percent")],
        [InlineKeyboardButton(f"وضع الضغط التلقائي: {auto_compress_text}", callback_data="settings_toggle_auto")],
        [InlineKeyboardButton("✖️ إغلاق اللوحة", callback_data="close_settings")]
    ]

    if message_id:
        try: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
        except: pass
    else:
        client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))


def post_download_actions(original_message_id):
    if original_message_id not in user_video_data: return
    video_data = user_video_data[original_message_id]
    message = video_data['message']
    user_id = video_data['user_id']
    download_msg = video_data['download_msg']

    try:
        file_path = video_data['download_future'].result()
        video_data['file'] = file_path
        
        try: download_msg.delete()
        except: pass
        
        user_prefs = get_user_settings(user_id)
        if user_prefs['auto_compress']:
            # قراءة الإعداد: هل المستخدم يريد تلقائي CRF أو نسبة مئوية؟
            if user_prefs.get('auto_mode') == 'percent':
                # إذا تلقائي كنسبة:
                orig_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                pct_value = user_prefs.get('auto_percent_value', 50)
                target_mb = (pct_value / 100) * orig_size_mb
                video_data['quality'] = {"target_size": target_mb}
                status_msg = message.reply_text(f"✅ تم التحميل، يضغط تلقائياً بقوة **{pct_value}%** من حجمه الأصلي...", quote=True)
            else:
                # إذا تلقائي كجودة:
                video_data['quality'] = user_prefs['auto_quality_value']
                status_msg = message.reply_text(f"✅ تم تحميل الملف. يضغط تلقائياً لـ **CRF {video_data['quality']}**...", quote=True)
            
            video_data['auto_compress_status_message_id'] = status_msg.id
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("أدنى جودة (27)", callback_data="crf_27"),
                 InlineKeyboardButton("متوسط (23)", callback_data="crf_23"),
                 InlineKeyboardButton("عالي جداً (18)", callback_data="crf_18")],
                [InlineKeyboardButton("🎯 تحديد الحجم (MB)", callback_data="target_size_prompt"),
                 InlineKeyboardButton("📉 تحديد نسبة مئوية (%)", callback_data="target_percent_prompt")],
                [InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_compression")]
            ])
            reply_message = message.reply_text("✅ استُلم الملف. الرجاء التحديد:", reply_markup=markup, quote=True)
            video_data['button_message_id'] = reply_message.id
            user_video_data[reply_message.id] = user_video_data.pop(original_message_id)
            timer = threading.Timer(300, auto_select_medium_quality, args=[reply_message.id])
            user_video_data[reply_message.id]['timer'] = timer
            timer.start()
            
    except Exception as e:
        message.reply_text(f"❌ وقع خطأ مقاطع أثناء التحميل:\n`{e}`")
        if original_message_id in user_video_data: del user_video_data[original_message_id]
        check_and_prompt_album(user_id, app, message.chat.id) # تقليل العداد لأن المهمة فشلت


@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    user_id = message.from_user.id

    # تسجيل أن هناك عملية نشطة قادمة للمستخدم (مهمة للألبوم)
    with task_lock:
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1

    file_id = message.video.file_id if message.video else message.animation.file_id
    file_size = message.video.file_size if message.video else message.animation.file_size
    file_name_prefix = os.path.join(DOWNLOADS_DIR, f"{user_id}_{message.id}_{int(time.time())}.mp4")
    
    download_msg = message.reply_text("📥 في طابور التنزيل...", quote=True)
    start_time = time.time()
    
    # يعمل التحميل كطابور متزامن (3/3)
    download_future = download_executor.submit(
        client.download_media,
        message=file_id,
        file_name=file_name_prefix,
        progress=update_progress_msg,
        progress_args=(client, download_msg, "📥 **جاري تنزيل الملف الخ...**", start_time, file_size)
    )

    user_video_data[message.id] = {
        'message': message, 'download_msg': download_msg, 'download_future': download_future,
        'file': None, 'button_message_id': None, 'timer': None, 'quality': None,
        'processing_started': False, 'user_id': user_id, 'auto_compress_status_message_id': None 
    }    
    threading.Thread(target=post_download_actions, args=[message.id]).start()

@app.on_callback_query()
def universal_callback_handler(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    message = callback_query.message

    # ----- التحكم في إرسال الألبوم الجديد -----
    if data == "send_batch_album":
        with task_lock:
            files_to_send = user_finished_files.get(user_id, [])
            if not files_to_send:
                callback_query.answer("⚠️ لا توجد ملفات في الذاكرة لتجميعها كألبوم.", show_alert=True)
                return
            callback_query.answer("📤 يتم الآن معالجة ورفع الألبوم، انتظر...")
            
            media_group = []
            for f_path in files_to_send[:10]: # أقصى عدد في تيليجرام 10 لكل رسالة ألبوم
                thumb, dur, w, h = get_video_info_and_thumb(f_path)
                media_group.append(InputMediaVideo(f_path, thumb=thumb, duration=int(dur), width=w, height=h))
            
            try:
                client.send_media_group(chat_id=message.chat.id, media=media_group)
            except Exception as e:
                client.send_message(message.chat.id, f"❌ خطأ أثناء إرسال الألبوم: {e}")
            finally:
                # حذف الملفات من القرص بعد الإرسال للحفاظ على المساحة
                for f_path in files_to_send:
                    if os.path.exists(f_path): os.remove(f_path)
                user_finished_files[user_id] = []
        try: message.delete()
        except: pass
        return

    elif data == "clear_batch_album":
        with task_lock:
            files_to_send = user_finished_files.get(user_id, [])
            for f_path in files_to_send:
                if os.path.exists(f_path): os.remove(f_path)
            user_finished_files[user_id] = []
        callback_query.answer("🗑 تم تنظيف ذاكرة الألبوم للملفات بنجاح.")
        try: message.delete()
        except: pass
        return

    # ---------------- إعدادات البوت الأصلية ----------------
    if data.startswith("settings"):
        if data == "settings":
            send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_encoder":
            keyboard = [[InlineKeyboardButton("H.265 (HEVC)", callback_data="set_encoder:hevc_nvenc")],
                        [InlineKeyboardButton("H.264 (NVENC GPU)", callback_data="set_encoder:h264_nvenc")],
                        [InlineKeyboardButton("H.264 (CPU العادي)", callback_data="set_encoder:libx264")],
                        [InlineKeyboardButton("« رجوع للقائمة السابقة", callback_data="settings")]]
            message.edit_text("إختر التقنية ومحرك المعالجة المعتمد لديك:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "settings_custom_quality":
            user_states[user_id] = {"state": "waiting_for_cq_value", "prompt_message_id": message.id}
            message.edit_text("أرسل رسالة برقم الجودة من 0 إلى 51 (للضغط التلقائي).",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء الأمر", callback_data="cancel_input")]]))
        elif data == "settings_custom_percent":
            user_states[user_id] = {"state": "waiting_for_auto_percentage", "prompt_message_id": message.id}
            message.edit_text("أرسل رقم النسبة (مثال: 40) التي سيعتمدها البوت تلقائياً:",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء الأمر", callback_data="cancel_input")]]))
        elif data == "settings_toggle_mode":
            settings = get_user_settings(user_id)
            settings['auto_mode'] = 'percent' if settings.get('auto_mode') == 'crf' else 'crf'
            callback_query.answer(f"صار وضع التلقائي يعمل عن طريق: {settings['auto_mode']}")
            send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_toggle_auto":
            settings = get_user_settings(user_id)
            settings['auto_compress'] = not settings['auto_compress']
            callback_query.answer(f"صار الضغط التلقائي {'مفعلاً الآن' if settings['auto_compress'] else 'معطلاً'}")
            send_settings_menu(client, message.chat.id, user_id, message.id)
        callback_query.answer()
        return

    # ---------------- ضبط الـ encoder ----------------
    elif data.startswith("set_encoder:"):
        _, value = data.split(":", 1)
        get_user_settings(user_id)['encoder'] = value
        callback_query.answer(f"سُجل. التفضيل صار لـ: {value}")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return

    # ---------------- إلغاء استقبال الرسائل ----------------
    elif data == "cancel_input":
        if user_id in user_states: del user_states[user_id]
        callback_query.answer("تم إلغاء حالة الاستقبال.")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "close_settings":
        try: message.delete()
        except: pass
        return

    # ---------------- التحقق من الفيديو ----------------
    button_message_id = message.id
    if button_message_id not in user_video_data:
        callback_query.answer("زر قديم جداً منتهي الصلاحية.", show_alert=True)
        try: message.delete()
        except: pass
        return
    
    video_data = user_video_data[button_message_id]
    if video_data.get('processing_started') and data not in ["cancel_compression", "finish_process"]:
        callback_query.answer("طابور التنفيذ يعمل بالفعل للفيديو...", show_alert=True)
        return

    # ---------------- إلغاء العملية والإنهاء ----------------
    if data in ["cancel_compression", "finish_process"]:
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        file_path = video_data.get('file')
        if file_path and os.path.exists(file_path): 
            os.remove(file_path)
            print(f"File Deleted on exit: {file_path}")
        try:
            message.delete()
            if data == "cancel_compression":
                video_data['message'].reply_text("🗑️ دُمر الطلب وأُزيل الملف الأصلي من الذاكرة بنجاح.", quote=True)
        except Exception: pass
        
        # لأن المستخدم ألغى، نلغي مهمته من عداد الألبوم
        check_and_prompt_album(user_id, client, message.chat.id)
        
        if button_message_id in user_video_data: del user_video_data[button_message_id]
        return

    # ---------------- طلب تحديد الحجم المستهدف أو النسبة اليدوية ----------------
    if data == "target_size_prompt":
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        prompt_msg = message.reply_text("🔢 رجاءً أرسل الحجم (المستهدف) رقماً بالميغا بايت في الدردشة الآن.\n*(مثال: 5.5 أو 12)*", quote=True)
        user_states[user_id] = {"state": "waiting_for_target_size", "prompt_message_id": prompt_msg.id, "button_message_id": button_message_id}
        callback_query.answer("في الانتظار لكتابة الحجم...")
        return
        
    if data == "target_percent_prompt":
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        prompt_msg = message.reply_text("🔢 رجاءً أرسل النسبة المئوية (1-100) ليقوم البوت بضغط الملف بناءً على حجمه.\n*(مثال: أرسل 30 للحصول على 30% من الحجم الحالي)*", quote=True)
        user_states[user_id] = {"state": "waiting_for_percentage", "prompt_message_id": prompt_msg.id, "button_message_id": button_message_id}
        callback_query.answer("في الانتظار للنسبة المئوية...")
        return

    if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()

    # ---------------- اختيار الجودة CRF مباشرة ----------------
    video_data['quality'] = data
    callback_query.answer("في المعالجة... يرجى التمهل")
    compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- التشغيل --------------------------
if __name__ == "__main__":
    cleanup_downloads()
    print("\n✅ البوت تم تجهيزه بالمهام المتزامنة. المزامنة مستمرة بنجاح...")
    app.run()
