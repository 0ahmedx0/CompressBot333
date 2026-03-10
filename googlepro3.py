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
PROGRESS_TRACKER = {} # لتتبع وقت آخر تحديث لرسائل التقدم (تجنب الحظر FloodWait)

DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_quality_value': 30
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

# -------------------------- وظائف المساعدة وحساب الحجم --------------------------

def update_progress_msg(current, total, client, message, action, start_time):
    """
    دالة موحدة لتحديث رسائل التقدم مع معالجة الأخطاء إذا كان الحجم الإجمالي غير معروف من سيرفر تيليجرام
    """
    now = time.time()
    msg_id = message.id
    
    # تحديد ما إذا كانت العملية انتهت أم لا
    is_finished = (current >= total) if total > 0 else False
    
    # تحديث الرسالة والطباعة كل 5 ثوانٍ فقط (تخطينا مشكلة الصفر هنا)
    if msg_id in PROGRESS_TRACKER and (now - PROGRESS_TRACKER[msg_id]) < 5.0 and not is_finished:
        return

    PROGRESS_TRACKER[msg_id] = now
    
    percent = (current * 100 / total) if total > 0 else 0
    filled = int(percent / 10) if percent > 0 else 0
    if filled > 10: filled = 10
    bar = f"[{'█' * filled}{'░' * (10 - filled)}]"
    
    # تجنب إظهار 0.00 MB إذا لم تكن البيانات متوفرة
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
            # نحسب الوقت المتبقي فقط إذا كنا نعرف الحجم الإجمالي
            if total > 0:
                eta_seconds = max(0, (total - current) / speed) # max(0) لتجنب الأرقام السالبة تماماً
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

    # ------------------ جزء الطباعة في السيرفر ------------------
    clean_action = action.replace('*', '').replace('`', '').split('\n')[0].strip()
    console_log = f"[Task Msg:{msg_id}] {clean_action} | {percent:.1f}% | {curr_val} / {total_val} {console_speed}| المتبقي: {eta_text}"
    print(console_log)
    # -------------------------------------------------------------
    
    try:
        client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except FloodWait as e:
        time.sleep(e.value)
    except MessageNotModified:
        pass
    except Exception:
        pass

def time_to_seconds(time_str):
    """تحويل وقت FFmpeg (HH:MM:SS.ms) إلى ثواني لاستخدامه في شريط التقدم"""
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0

def get_telegram_duration(message):
    """جلب مدة الفيديو بسرعة من بيانات رسالة تيليجرام لضمان الدقة العالية"""
    if message.video and message.video.duration:
        return float(message.video.duration)
    elif message.animation and message.animation.duration:
        return float(message.animation.duration)
    return 0

def get_video_duration(file_path):
    """جلب المدة الإجمالية كخيار بديل إذا فشل جلبها من تيليجرام"""
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_format "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        return float(data['format']['duration'])
    except:
        return 0

def calculate_target_bitrate(target_size_mb, duration_seconds, audio_bitrate_kbps=128):
    """
    المعادلة الدقيقة لحساب معدل البت (Bitrate) اللازم للوصول إلى حجم محدد (Target Size).
    """
    if duration_seconds <= 0:
        return 500 # قيمة افتراضية آمنة إذا فشل تحديد مدة الفيديو
        
    # الحجم الكلي المستهدف بالكيلوبت
    total_bitrate_kbps = (target_size_mb * 8192) / duration_seconds
    
    # المساحة المتبقية للصورة (بطرح مساحة الصوت المحجوزة)
    video_bitrate_kbps = int(total_bitrate_kbps - audio_bitrate_kbps)
    
    # حد أدنى آمن لكي لا تنهار جودة الفيديو وتفشل العملية تماماً (50kbps)
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

    # الحصول على مدة الفيديو للحساب التفاعلي ولضبط الحجم
    total_duration = get_telegram_duration(message)
    if total_duration <= 0:
        total_duration = get_video_duration(file_path)

    print(f"\n[{thread_name}] Original file: {os.path.basename(file_path)} | Size: {os.path.getsize(file_path)/(1024*1024):.2f}MB | Duration: {total_duration}s")

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

        # بناء أوامر الجودة والحجم لـ FFmpeg
        if isinstance(quality, dict) and 'target_size' in quality:
            target_size_mb = quality['target_size']
            
            # جلب قيمة مساحة الصوت التي سيتم استخدامها، لتنقيصها من إجمالي المساحة المطلوبة
            try: audio_k = int(str(VIDEO_AUDIO_BITRATE).lower().replace('k', '').strip())
            except: audio_k = 128
            
            # حساب المعدل المضبوط
            target_v_bitrate = calculate_target_bitrate(target_size_mb, total_duration, audio_k)
            print(f"[{thread_name}] Mode: EXACT SIZE. Target: {target_size_mb} MB | Target Video Bitrate: {target_v_bitrate}k")
            
            # أوامر إلزام FFmpeg باحترام الحجم المحدد (ABR mode)
            quality_settings = f"-b:v {target_v_bitrate}k -maxrate {target_v_bitrate}k -bufsize {target_v_bitrate*2}k -preset fast"
            used_mode_text = f"🎯 طلب حجم مستهدف: ~{target_size_mb} MB"
            
        else:
            # نمط ضغط הגودة العادي (CRF / CQ)
            quality_value = int(quality.split('_')[1]) if isinstance(quality, str) and 'crf_' in quality else int(quality)
            print(f"[{thread_name}] Mode: QUALITY (CRF/CQ). Level: {quality_value}")
            
            preset = "fast"
            if quality_value <= 18: preset = "slow"
            elif quality_value <= 23: preset = "medium"
            elif quality_value >= 27: preset = "veryfast" if encoder == 'libx264' else "fast"
            
            quality_param = "cq" if "nvenc" in encoder else "crf"
            quality_settings = f"-{quality_param} {quality_value} -preset {preset}"
            used_mode_text = f"🎥 الجودة (CRF/CQ): {quality_value}"
        
        # إنشاء أمر FFmpeg الكامل
        common_ffmpeg_part = (
            f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} '
            f'-c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
            f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -map_metadata -1'
        )
        ffmpeg_command = f'{common_ffmpeg_part} {quality_settings} "{temp_compressed_filename}"'

        # إرسال رسالة التتبع الفعلي للضغط
        progress_msg = message.reply_text("🔄 **بدأ ضغط الفيديو (قد يأخذ وقتاً)...**", quote=True)
        start_time = time.time()

        # تشغيل العملية وتحليل سطر Progress الخاص بها
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

        # رسالة جاري الرفع مع شريط تقدم
        upload_progress_msg = message.reply_text("📤 اكتمل الضغط! بدأ رفع الفيديو النهائي...", quote=True)
        upload_start_time = time.time()

        message.reply_document(
            document=temp_compressed_filename,
            progress=update_progress_msg,
            progress_args=(app, upload_progress_msg, "📤 **الرفع إلى التليجرام...**", upload_start_time),
            caption=f"📦 **النتيجة النهائية**\n"
                    f"🔻 الحجم القديم: {os.path.getsize(file_path) / (1024 * 1024):.2f} MB\n"
                    f"✅ الحجم الجديد: {compressed_file_size_mb:.2f} MB\n\n"
                    f"{used_mode_text}"
        )
        
        try: upload_progress_msg.delete()
        except: pass
        
    except Exception as e:
        print(f"[{thread_name}] Processing error: {e}")
        message.reply_text(f"❌ حدث خطأ أثناء المعالجة أو الرفع:\n`{str(e)[:150]}`", quote=True)
    finally:
        # حذف الملفات المؤقتة فور انتهاء كل المهام المرتبطة بها
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
                    text="✅ تمت المهمة بنجاح! للتحكم، يمكنك طلب تجربة جودة أخرى أم إنهاء العملية من هنا:",
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
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم تفعيل اختيار (متوسط) لانتهاء الوقت", callback_data="none")]]))
            except Exception: pass
            compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- معالجات رسائل تيليجرام --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    settings_button = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]])
    message.reply_text(
        "👋 أهلاً بك! أرسل لي فيديو، أو مقطع متحرك وسأقوم بضغطه بقوة.\n\nتتوفر خيارات متعددة لجودة (CRF) بالإضافة لخيار فريد لضغط الملف نحو (حجم معين بالـ MB).",
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
    
    # حالة التغيير اليدوي للجودة التلقائية (من الإعدادات)
    if state == "waiting_for_cq_value":
        prompt_message_id = user_states[user_id].get("prompt_message_id")
        try:
            value = int(message.text)
            if 0 <= value <= 51:
                settings = get_user_settings(user_id)
                settings['auto_quality_value'] = value
                del user_states[user_id]
                message.reply_text(f"✅ تم تحديث الجودة الافتراضية للضغط التلقائي: **CRF/CQ {value}**", quote=True)
                send_settings_menu(client, message.chat.id, user_id, prompt_message_id)
            else:
                message.reply_text("❌ أرقام مستبعدة، المرجو استخدام بين 0 و 51 فقط.", quote=True)
        except ValueError:
            message.reply_text("❌ إدخال غير صالح. المطلوب رقم.", quote=True)
        finally:
            try: message.delete()
            except: pass

    # حالة الاستجابة لزر إدخال (حجم الهدف) لعملية ضغط معلقة
    elif state == "waiting_for_target_size":
        prompt_message_id = user_states[user_id].get("prompt_message_id")
        button_message_id = user_states[user_id].get("button_message_id")
        try:
            size = float(message.text)
            if size <= 0: raise ValueError
                
            if button_message_id and button_message_id in user_video_data:
                video_data = user_video_data[button_message_id]
                if not video_data.get('processing_started') and video_data.get('file'):
                    # نمرر قيمة החجم (dict) لنقوم بالتبديل إلى نظام (حساب البتريت) لاحقاً 
                    video_data['quality'] = {"target_size": size}
                    try:
                        app.edit_message_reply_markup(
                            chat_id=message.chat.id, message_id=button_message_id,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎯 طلب الوصول لـ ~{size} MB استلم", callback_data="none")]]))
                    except Exception: pass
                    
                    compression_executor.submit(process_video_for_compression, video_data)
                    del user_states[user_id]
                else:
                    message.reply_text("❌ انتهت صلاحية هذا الزر (الفيديو ممسوح أو العملية قيد التنفيذ مسبقاً).", quote=True)
            else:
                message.reply_text("❌ بيانات الجلسة غير متوفرة. أرسل فيديو جديد.", quote=True)
                
        except ValueError:
            message.reply_text("❌ القيمة المُرسلة خاطئة، يرجى كتابة حجم الميغا رقمياً وفقط (مثال: 5.5 أو 12).", quote=True)
        finally:
            try: message.delete()
            except: pass

def send_settings_menu(client, chat_id, user_id, message_id=None):
    settings = get_user_settings(user_id)
    encoder_text = {"hevc_nvenc": "H.265 (HEVC)","h264_nvenc": "H.264 (NVENC)","libx264": "H.264 (CPU)"}.get(settings['encoder'], "-")
    auto_compress_text = "✅ مفعل" if settings['auto_compress'] else "❌ معطل"
    auto_quality_text = settings['auto_quality_value']

    text = (
        "**⚙️ قائمة الإعدادات والمحركات:**\n\n"
        f"🔹 **الترميز والمُسرع (Encoder):** `{encoder_text}`\n"
        f"🔸 **ميزة الضغط التلقائي السريع:** `{auto_compress_text}`\n"
        f"📊 **مستوى الجودة (في التلقائي):** `CRF {auto_quality_text}`"
    )
    keyboard = [[InlineKeyboardButton("🔄 تغيير المُسرع / الترميز", callback_data="settings_encoder")],
                [InlineKeyboardButton(f"وضع الضغط التلقائي: {auto_compress_text}", callback_data="settings_toggle_auto")],
                [InlineKeyboardButton("✏️ ضبط قيمة (الجودة/CRF) للوضع التلقائي", callback_data="settings_custom_quality")],
                [InlineKeyboardButton("✖️ إغلاق اللوحة", callback_data="close_settings")]]

    if message_id:
        try: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
        except: pass
    else:
        client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    file_id = message.video.file_id if message.video else message.animation.file_id
    file_name_prefix = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{message.id}_{int(time.time())}.mp4")
    
    download_msg = message.reply_text("📥 يتم إنشاء الاتصال لتنزيل الفيديو لخادم المعالجة...", quote=True)
    start_time = time.time()
    
    download_future = download_executor.submit(
        client.download_media,
        message=file_id,
        file_name=file_name_prefix,
        progress=update_progress_msg,
        progress_args=(client, download_msg, "📥 **جاري تنزيل الملف الخ...**", start_time)
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
        
        try: download_msg.delete()
        except: pass
        
        user_prefs = get_user_settings(user_id)
        if user_prefs['auto_compress']:
            video_data['quality'] = user_prefs['auto_quality_value']
            status_msg = message.reply_text(f"✅ تم تحميل الملف. يضغط تلقائياً لـ **CRF {video_data['quality']}**...", quote=True)
            video_data['auto_compress_status_message_id'] = status_msg.id
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("أدنى جودة (27)", callback_data="crf_27"),
                 InlineKeyboardButton("متوسط (23)", callback_data="crf_23"),
                 InlineKeyboardButton("عالي جداً (18)", callback_data="crf_18")],
                [InlineKeyboardButton("🎯 استهداف وتحديد حجم الميغا بالضبط", callback_data="target_size_prompt")],
                [InlineKeyboardButton("❌ إلغاء العملية بأكملها", callback_data="cancel_compression")]
            ])
            reply_message = message.reply_text("✅ استُلم الملف.\nتفضل بتحديد الجودة المطلوبة (أو اطلب تقليصه لحجم محدد):", reply_markup=markup, quote=True)
            video_data['button_message_id'] = reply_message.id
            user_video_data[reply_message.id] = user_video_data.pop(original_message_id)
            # اختيار ذاتي إن مر 5 دقائق
            timer = threading.Timer(300, auto_select_medium_quality, args=[reply_message.id])
            user_video_data[reply_message.id]['timer'] = timer
            timer.start()
            
    except Exception as e:
        message.reply_text(f"❌ وقع خطأ مقاطع أثناء التحميل أو بعده:\n`{e}`")
        if original_message_id in user_video_data: del user_video_data[original_message_id]

@app.on_callback_query()
def universal_callback_handler(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    message = callback_query.message
    
    # قائمة الإعدادات الفرعية (لا تستدعي عملية الفيديو)
    if data.startswith("settings"):
        if data == "settings": send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_encoder":
            keyboard = [[InlineKeyboardButton("H.265 (HEVC)", callback_data="set_encoder:hevc_nvenc")],
                        [InlineKeyboardButton("H.264 (NVENC GPU)", callback_data="set_encoder:h264_nvenc")],
                        [InlineKeyboardButton("H.264 (CPU العادي)", callback_data="set_encoder:libx264")],
                        [InlineKeyboardButton("« رجوع للقائمة السابقة", callback_data="settings")]]
            message.edit_text("إختر التقنية ومحرك المعالجة المعتمد لديك:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "settings_custom_quality":
            user_states[user_id] = {"state": "waiting_for_cq_value", "prompt_message_id": message.id}
            message.edit_text("أرسل رسالة برقم الجودة من 0 إلى 51 (للضغط التلقائي).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء الأمر", callback_data="cancel_input")]]))
        elif data == "settings_toggle_auto":
            settings = get_user_settings(user_id)
            settings['auto_compress'] = not settings['auto_compress']
            callback_query.answer(f"صار الضغط التلقائي {'مفعلاً الآن' if settings['auto_compress'] else 'معطلاً'}")
            send_settings_menu(client, message.chat.id, user_id, message.id)
        callback_query.answer()
        return

    elif data.startswith("set_encoder:"):
        _, value = data.split(":", 1)
        get_user_settings(user_id)['encoder'] = value
        callback_query.answer(f"سُجل. التفضيل صار لـ: {value}")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "cancel_input":
        if user_id in user_states: del user_states[user_id]
        callback_query.answer("تم إلغاء حالة الاستقبال.")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "close_settings":
        try: message.delete()
        except: pass
        return
        
    button_message_id = message.id
    if button_message_id not in user_video_data:
        callback_query.answer("زر قديم جداً منتهي الصلاحية.", show_alert=True)
        try: message.delete()
        except: pass
        return
    
    video_data = user_video_data[button_message_id]
    if video_data.get('processing_started'):
        callback_query.answer("طابور التنفيذ يعمل بالفعل للفيديو...", show_alert=True)
        return

    # أوامر إنهاء أو مقاطعة
    if data in ["cancel_compression", "finish_process"]:
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        file_path = video_data.get('file')
        if file_path and os.path.exists(file_path): os.remove(file_path)
        try:
            message.delete()
            video_data['message'].reply_text("🗑️ دُمر الطلب وأُزيل من الذاكرة بأمرك.", quote=True)
        except Exception: pass
        if button_message_id in user_video_data: del user_video_data[button_message_id]
        return

    # حالة الزر للضغط الخاص بحجم معين
    if data == "target_size_prompt":
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        
        prompt_msg = message.reply_text("🔢 رجاءً أرسل الحجم (المستهدف) رقماً بوحدة الميجا بايت في دردشة البوت الآن.\n"
                                        "*(مثلاً، للحصول على 5MB، قم بإرسال الرقم: 5)*", quote=True)
        
        # حفظ مسار المحادثة لهذا الـ ID للاستماع للحجم المُرسل من قبل المستخدم
        user_states[user_id] = {
            "state": "waiting_for_target_size", 
            "prompt_message_id": prompt_msg.id,
            "button_message_id": button_message_id
        }
        callback_query.answer("في الانتظار لكتابة حجمك المفضل...")
        return

    # باقي الأزرار الخاصة بالاختيار اليدوي للجودة الثابتة
    if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()

    video_data['quality'] = data
    callback_query.answer("في المعالجة... يرجى التمهل")
    compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- التشغيل --------------------------
if __name__ == "__main__":
    cleanup_downloads()
    print("\n✅ البوت تم تجهيزه. المزامنة مستمرة بنجاح وخاصية تحديد الحجم المستهدف شغالة...")
    app.run()
