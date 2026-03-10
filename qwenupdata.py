import os
import tempfile
import subprocess
import threading
import time
import re
import json
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty, UserNotParticipant, MessageNotModified, FloodWait

from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

download_executor = ThreadPoolExecutor(max_workers=5)
compression_executor = ThreadPoolExecutor(max_workers=3)

# قواميس التخزين
user_states = {}
user_settings = {}
user_video_data = {}
PROGRESS_TRACKER = {} # لتتبع وقت آخر تحديث لرسائل التقدم (تجنب الـ Flood)

DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_quality_value': 30
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

# -------------------------- وظائف تتبع التقدم التفاعلية --------------------------

def update_progress_msg(current, total, client, message, action, start_time):
    """
    دالة موحدة لتحديث رسائل التقدم بأسلوب شريط بصري (التنزيل والرفع)
    """
    now = time.time()
    msg_id = message.id
    
    # تحديث الرسالة كل 3 ثوانٍ فقط لتجنب FloodWait من تيليجرام
    if msg_id in PROGRESS_TRACKER and (now - PROGRESS_TRACKER[msg_id]) < 3.0 and current < total:
        return

    PROGRESS_TRACKER[msg_id] = now
    
    percent = (current * 100 / total) if total > 0 else 0
    filled = int(percent / 10)
    bar = f"[{'█' * filled}{'░' * (10 - filled)}]"
    
    curr_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)
    
    elapsed = now - start_time
    speed = curr_mb / elapsed if elapsed > 0 else 0
    eta = (total_mb - curr_mb) / speed if speed > 0 else 0
    
    text = (
        f"**{action}**\n"
        f"{bar} `{percent:.1f}%`\n"
        f"📦 **الحجم:** `{curr_mb:.2f} MB / {total_mb:.2f} MB`\n"
        f"🚀 **السرعة:** `{speed:.2f} MB/s`\n"
        f"⏱ **الوقت المتبقي:** `{int(eta)} ثانية`"
    )
    
    try:
        client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except FloodWait as e:
        time.sleep(e.value)
    except MessageNotModified:
        pass
    except Exception as e:
        pass

def time_to_seconds(time_str):
    """تحويل وقت FFmpeg (HH:MM:SS.ms) إلى ثواني"""
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0

def get_video_duration(file_path):
    """جلب المدة الإجمالية للفيديو باستخدام ffprobe"""
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_format "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        return float(data['format']['duration'])
    except:
        return 0

def cleanup_downloads():
    print("Cleaning up downloads directory...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
        except Exception as e:
            pass

def estimate_crf_for_target_size(file_path, target_size_mb, initial_crf=23):
    original_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if original_size_mb > target_size_mb:
        ratio = original_size_mb / target_size_mb
        estimated_crf = min(51, max(18, initial_crf + int((ratio - 1) * 5)))
    else:
        ratio = target_size_mb / original_size_mb
        estimated_crf = max(0, min(23, initial_crf - int((ratio - 1) * 5)))
    return estimated_crf

# -------------------------- تهيئة العميل --------------------------
app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

# -------------------------- وظائف المعالجة الأساسية --------------------------

def process_video_for_compression(video_data):
    thread_name = threading.current_thread().name
    file_path = video_data['file']
    message = video_data['message']
    button_message_id = video_data.get('button_message_id')
    quality = video_data['quality']
    user_id = video_data['user_id']
    user_prefs = get_user_settings(user_id)
    encoder = user_prefs['encoder']

    # الحصول على مدة الفيديو للحساب التفاعلي
    total_duration = get_video_duration(file_path)

    # تحديث رسالة الأزرار إذا وجدت
    if button_message_id and button_message_id in user_video_data:
        user_video_data[button_message_id]['processing_started'] = True
        try:
            status_text = f"⏳ تم وضع الفيديو في طابور المعالجة..."
            app.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=button_message_id,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(status_text, callback_data="none")]])
            )
        except: pass

    temp_compressed_filename = None

    try:
        if not os.path.exists(file_path):
            message.reply_text("❌ حدث خطأ: لم يتم العثور على الملف الأصلي للمعالجة.")
            return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        # ضبط القيمة
        if isinstance(quality, dict) and 'target_size' in quality:
            target_size_mb = quality['target_size']
            estimated_crf = estimate_crf_for_target_size(file_path, target_size_mb)
            quality_value = estimated_crf
        else:
            quality_value = int(quality.split('_')[1]) if isinstance(quality, str) and 'crf_' in quality else int(quality)
    
        # ضبط البريسيت (Preset)
        preset = "fast"
        if quality_value <= 18: preset = "slow"
        elif quality_value <= 23: preset = "medium"
        elif quality_value >= 27: preset = "veryfast" if encoder == 'libx264' else "fast"
        
        quality_param = "cq" if "nvenc" in encoder else "crf"
        
        # إنشاء أوامر FFmpeg
        common_ffmpeg_part = (
            f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} '
            f'-c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
            f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -map_metadata -1'
        )
        quality_settings = f'-{quality_param} {quality_value} -preset {preset}'
        ffmpeg_command = f'{common_ffmpeg_part} {quality_settings} "{temp_compressed_filename}"'

        # إرسال رسالة التتبع الفعلي للضغط
        progress_msg = message.reply_text("🔄 **بدأ ضغط الفيديو الآن...**", quote=True)
        start_time = time.time()

        # تشغيل Popen لتحليل السطور الحية
        process = subprocess.Popen(ffmpeg_command, shell=True, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')

        for line in process.stderr:
            if total_duration > 0:
                # استخراج الوقت المنقضي من السطر (مثل: time=00:01:23.45)
                time_match = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d+)", line)
                if time_match:
                    current_time_str = time_match.group(1)
                    current_time_sec = time_to_seconds(current_time_str)
                    
                    # استخدمنا دالة تحديث التقدم، نمرر الثواني بدل الـ bytes
                    update_progress_msg(
                        current=current_time_sec,
                        total=total_duration,
                        client=app,
                        message=progress_msg,
                        action="⚙️ **جاري ضغط الفيديو...**",
                        start_time=start_time
                    )

        process.wait()
        
        if process.returncode != 0:
            raise Exception("FFmpeg failed to compress video.")
            
        try: progress_msg.delete()
        except: pass

        compressed_file_size_mb = os.path.getsize(temp_compressed_filename) / (1024 * 1024)

        # مرحلة الرفع
        upload_progress_msg = message.reply_text("📤 بدأ رفع الفيديو المكتمل...", quote=True)
        upload_start_time = time.time()

        message.reply_document(
            document=temp_compressed_filename,
            progress=update_progress_msg,
            progress_args=(app, upload_progress_msg, "📤 **الرفع إلى التليجرام...**", upload_start_time),
            caption=f"📦 الفيديو المضغوط\n"
                    f"🔻 الحجم الأصلي: {os.path.getsize(file_path) / (1024 * 1024):.2f} MB\n"
                    f"✅ الحجم الجديد: {compressed_file_size_mb:.2f} MB\n"
                    f"🎥 الجودة/الترقيع المستخدم: CRF {quality_value}"
        )
        
        try: upload_progress_msg.delete()
        except: pass
        
    except Exception as e:
        print(f"Error: {e}")
        message.reply_text(f"❌ حدث خطأ أثناء المعالجة أو الرفع:\n`{str(e)[:100]}`", quote=True)
    finally:
        # الحذف المؤقت والتنظيف
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            os.remove(temp_compressed_filename)
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

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
                     InlineKeyboardButton("❌ إنهاء العملية", callback_data="finish_process")]
                ])
                app.edit_message_text(
                    chat_id=message.chat.id, message_id=button_message_id,
                    text="✅ تم إنجاز الطلب! هل تود المحاولة بجودة أخرى، أم إنهاء العملية؟",
                    reply_markup=markup)
            except: pass
        elif video_data['message'].id in user_video_data:
            del user_video_data[video_data['message'].id]

def auto_select_medium_quality(button_message_id):
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        if not video_data.get('processing_started'):
            video_data['quality'] = "crf_23"
            try:
                app.edit_message_reply_markup(
                    chat_id=video_data['message'].chat.id, message_id=button_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم اختيار جودة متوسطة تلقائيًا", callback_data="none")]]))
            except Exception: pass
            compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- معالجات رسائل تيليجرام --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    settings_button = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]])
    message.reply_text(
        "👋 أهلاً بك! أرسل لي فيديو وسأقوم بضغطه.\nتتوفر خيارات لجودة محددة أو طلب حجم مستهدف كـ 10 MB.",
        reply_markup=settings_button, quote=True
    )

@app.on_message(filters.command("settings"))
def settings_command(client, message):
    send_settings_menu(client, message.chat.id, message.from_user.id)

@app.on_message(filters.text)
def handle_text_inputs(client, message):
    user_id = message.from_user.id
    if user_id not in user_states: return

    state = user_states[user_id].get("state")
    
    # 1. حالة الجودة الافتراضية للضغط التلقائي
    if state == "waiting_for_cq_value":
        prompt_message_id = user_states[user_id].get("prompt_message_id")
        try:
            value = int(message.text)
            if 0 <= value <= 51:
                settings = get_user_settings(user_id)
                settings['auto_quality_value'] = value
                del user_states[user_id]
                message.reply_text(f"✅ تم تحديث الجودة التلقائية إلى: **CRF/CQ {value}**", quote=True)
                send_settings_menu(client, message.chat.id, user_id, prompt_message_id)
            else:
                message.reply_text("❌ قيمة غير صالحة. الرجاء إدخال رقم بين 0 و 51.", quote=True)
        except ValueError:
            message.reply_text("❌ أرسل رقماً صحيحاً.", quote=True)
        finally:
            try: message.delete()
            except: pass

    # 2. حالة تحديد الحجم الهدف بالـ MB
    elif state == "waiting_for_target_size":
        prompt_message_id = user_states[user_id].get("prompt_message_id")
        button_message_id = user_states[user_id].get("button_message_id")
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
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎯 الحجم المطلوب ~{size} MB", callback_data="none")]]))
                    except Exception: pass
                    
                    compression_executor.submit(process_video_for_compression, video_data)
                    del user_states[user_id]
                    # احذف رسالة الطلب إن أردت: 
                    # try: app.delete_messages(message.chat.id, prompt_message_id) except: pass
                else:
                    message.reply_text("❌ العملية انتهت سلفاً أو التنزيل لم يكتمل.", quote=True)
            else:
                message.reply_text("❌ الجلسة مفقودة أو منتهية. يرجى إعادة إرسال الفيديو.", quote=True)
                
        except ValueError:
            message.reply_text("❌ الرجاء إدخال رقم موجب (مثل 15 أو 10.5).", quote=True)
        finally:
            try: message.delete()
            except: pass

def send_settings_menu(client, chat_id, user_id, message_id=None):
    settings = get_user_settings(user_id)
    encoder_text = {"hevc_nvenc": "H.265 (HEVC)","h264_nvenc": "H.264 (NVENC)","libx264": "H.264 (CPU)"}.get(settings['encoder'], "-")
    auto_compress_text = "✅ مفعل" if settings['auto_compress'] else "❌ معطل"
    auto_quality_text = settings['auto_quality_value']

    text = (
        "**⚙️ قائمة الإعدادات**\n\n"
        f"🔹 **الترميز (Encoder):** `{encoder_text}`\n"
        f"🔸 **الضغط التلقائي:** `{auto_compress_text}`\n"
        f"📊 **جودة الضغط التلقائية (CRF):** `{auto_quality_text}`"
    )
    keyboard = [[InlineKeyboardButton("🔄 تغيير الترميز", callback_data="settings_encoder")],
                [InlineKeyboardButton(f"الضغط التلقائي: {auto_compress_text}", callback_data="settings_toggle_auto")],
                [InlineKeyboardButton("✏️ ضبط قيمة CRF/CQ التلقائية", callback_data="settings_custom_quality")],
                [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]]

    if message_id:
        try: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
        except: pass
    else:
        client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    file_id = message.video.file_id if message.video else message.animation.file_id
    file_name_prefix = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{message.id}_{int(time.time())}.mp4")
    
    # رسالة التقدم للتنزيل
    download_msg = message.reply_text("📥 بدأ التنزيل للفي بي إس...", quote=True)
    start_time = time.time()
    
    download_future = download_executor.submit(
        client.download_media,
        message=file_id,
        file_name=file_name_prefix,
        progress=update_progress_msg,
        progress_args=(client, download_msg, "📥 **التنزيل من التليجرام...**", start_time)
    )

    user_video_data[message.id] = {
        'message': message,
        'download_msg': download_msg,
        'download_future': download_future,
        'file': None,
        'button_message_id': None,
        'timer': None,
        'quality': None,
        'processing_started': False,
        'user_id': message.from_user.id,
        'auto_compress_status_message_id': None 
    }    
    threading.Thread(target=post_download_actions, args=[message.id]).start()

def post_download_actions(original_message_id):
    if original_message_id not in user_video_data: return
    video_data = user_video_data[original_message_id]
    message = video_data['message']
    user_id = video_data['user_id']
    download_msg = video_data['download_msg']

    try:
        file_path = video_data['download_future'].result()
        video_data['file'] = file_path
        
        # حذف رسالة التقدم الخاص بالتنزيل
        try: download_msg.delete()
        except: pass
        
        user_prefs = get_user_settings(user_id)
        if user_prefs['auto_compress']:
            video_data['quality'] = user_prefs['auto_quality_value']
            status_msg = message.reply_text(f"✅ تم التنزيل. جاري الضغط التلقائي بالجودة **CRF {video_data['quality']}**", quote=True)
            video_data['auto_compress_status_message_id'] = status_msg.id
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("ضعيفة (CRF 27)", callback_data="crf_27"),
                 InlineKeyboardButton("متوسطة (CRF 23)", callback_data="crf_23"),
                 InlineKeyboardButton("عالية (CRF 18)", callback_data="crf_18")],
                [InlineKeyboardButton("🎯 ضغط لحجم معين", callback_data="target_size_prompt"),
                 InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_compression")]
            ])
            reply_message = message.reply_text("✅ تم تنزيل الفيديو. اختر إما الجودة (CRF)، أو تحديد الحجم المطلوب:", reply_markup=markup, quote=True)
            video_data['button_message_id'] = reply_message.id
            user_video_data[reply_message.id] = user_video_data.pop(original_message_id)
            timer = threading.Timer(300, auto_select_medium_quality, args=[reply_message.id])
            user_video_data[reply_message.id]['timer'] = timer
            timer.start()
    except Exception as e:
        message.reply_text(f"❌ حدث خطأ أثناء التنزيل: `{e}`")
        if original_message_id in user_video_data: del user_video_data[original_message_id]

@app.on_callback_query()
def universal_callback_handler(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    message = callback_query.message
    
    if data.startswith("settings"):
        if data == "settings": send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_encoder":
            keyboard = [[InlineKeyboardButton("H.265 (HEVC)", callback_data="set_encoder:hevc_nvenc")],
                        [InlineKeyboardButton("H.264 (NVENC GPU)", callback_data="set_encoder:h264_nvenc")],
                        [InlineKeyboardButton("H.264 (CPU)", callback_data="set_encoder:libx264")],
                        [InlineKeyboardButton("« رجوع", callback_data="settings")]]
            message.edit_text("اختر ترميز الفيديو المفضل:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "settings_custom_quality":
            user_states[user_id] = {"state": "waiting_for_cq_value", "prompt_message_id": message.id}
            message.edit_text("أرسل الآن قيمة الجودة الافتراضية للضغط التلقائي (بين 0 و 51).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data="cancel_input")]]))
        elif data == "settings_toggle_auto":
            settings = get_user_settings(user_id)
            settings['auto_compress'] = not settings['auto_compress']
            callback_query.answer(f"الضغط التلقائي الآن {'مفعل' if settings['auto_compress'] else 'معطل'}")
            send_settings_menu(client, message.chat.id, user_id, message.id)
        callback_query.answer()
        return

    elif data.startswith("set_encoder:"):
        _, value = data.split(":", 1)
        get_user_settings(user_id)['encoder'] = value
        callback_query.answer(f"تم التغيير إلى {value}")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "cancel_input":
        if user_id in user_states: del user_states[user_id]
        callback_query.answer("تم الإلغاء.")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "close_settings":
        try: message.delete()
        except: pass
        return
        
    button_message_id = message.id
    if button_message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب.", show_alert=True)
        try: message.delete()
        except: pass
        return
    
    video_data = user_video_data[button_message_id]
    if video_data.get('processing_started'):
        callback_query.answer("العملية قيد التنفيذ يرجى الانتظار...", show_alert=True)
        return

    if data in ["cancel_compression", "finish_process"]:
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        file_path = video_data.get('file')
        if file_path and os.path.exists(file_path): os.remove(file_path)
        try:
            message.delete()
            video_data['message'].reply_text("🗑️ تم إنهاء العملية وحذف الملفات المؤقتة.", quote=True)
        except Exception: pass
        if button_message_id in user_video_data: del user_video_data[button_message_id]
        return

    if data == "target_size_prompt":
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        
        prompt_msg = message.reply_text("🔢 أرسل الحجم المطلوب للنسخة النهائية بالميغابايت\n(مثال: إرسال 10 يعني محاولة ضغطه ليكون بحجم 10MB):", quote=True)
        
        user_states[user_id] = {
            "state": "waiting_for_target_size", 
            "prompt_message_id": prompt_msg.id,
            "button_message_id": button_message_id
        }
        callback_query.answer("يرجى إرسال الحجم المطلوب في الدردشة...")
        return

    # باقي الأزرار الخاصة بالجودة الافتراضية
    if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()

    video_data['quality'] = data
    callback_query.answer("تم بدء عملية الضغط...")
    compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- التشغيل --------------------------
if __name__ == "__main__":
    cleanup_downloads()
    print("🚀 Bot with advanced progress bar is running!")
    app.run()
