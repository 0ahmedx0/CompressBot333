import os
import tempfile
import subprocess
import threading
import time
import re
import json
import shutil # من أجل دمج وحفظ الألبوم
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
from pyrogram.errors import MessageEmpty, UserNotParticipant, MessageNotModified, FloodWait

from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# ضبط طابور 3 ملفات للتحميل و3 للضغط متزامن
download_executor = ThreadPoolExecutor(max_workers=3)
compression_executor = ThreadPoolExecutor(max_workers=3)

# قواميس التخزين الأساسية
user_states = {}
user_settings = {}
user_video_data = {}
PROGRESS_TRACKER = {} 

# خوادم تتبع وتجميع الفيديوهات لتشكيل الألبوم والتنظيف
user_active_tasks = {}
user_finished_files = {}
user_cleanup_messages = {}
task_lock = threading.Lock()

# الإعدادات الافتراضية
DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_quality_value': 30,
    'auto_percent_value': 50,
    'auto_mode': 'crf',
    'auto_send_album': False
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

def track_message_for_cleanup(user_id, message_id):
    if user_id not in user_cleanup_messages:
        user_cleanup_messages[user_id] = []
    user_cleanup_messages[user_id].append(message_id)

# -------------------------- دالة إرسال الألبوم وتنظيف الرسائل المزعجة --------------------------
def send_user_album(client, chat_id, user_id):
    with task_lock:
        files_to_send = list(user_finished_files.get(user_id, []))
        if not files_to_send: return
        user_finished_files[user_id] = []

    st_msg = client.send_message(chat_id, "📤 جاري تجهيز ورفع النتيجة النهائية...")

    for i in range(0, len(files_to_send), 10):
        chunk = files_to_send[i:i+10]
        media_group = []
        for f_path in chunk:
            thumb, dur, w, h = get_video_info_and_thumb(f_path)
            media_group.append(InputMediaVideo(f_path, thumb=thumb, duration=int(dur), width=w, height=h))
        
        try:
            if len(media_group) == 1:
                client.send_video(
                    chat_id, video=chunk[0], caption="📦 النتيجة النهائية.",
                    duration=int(dur), width=w, height=h, thumb=thumb, supports_streaming=True
                )
            else:
                client.send_media_group(chat_id, media=media_group)
        except Exception as e:
            client.send_message(chat_id, f"❌ خطأ الرفع: {e}")
        finally:
            for f_path in chunk:
                if os.path.exists(f_path): os.remove(f_path)

    try: st_msg.delete()
    except: pass

    # مسح الرسائل المؤقتة بعد انتهاء العملية كلياً
    if user_id in user_cleanup_messages:
        for msg_id in user_cleanup_messages[user_id]:
            try: client.delete_messages(chat_id, msg_id)
            except: pass
        user_cleanup_messages[user_id] = []

def check_and_prompt_album(user_id, client, chat_id):
    with task_lock:
        if user_id in user_active_tasks:
            user_active_tasks[user_id] -= 1
            if user_active_tasks[user_id] <= 0:
                user_active_tasks[user_id] = 0

    tasks_left = user_active_tasks.get(user_id, 0)
    files_ready = user_finished_files.get(user_id, [])
    settings = get_user_settings(user_id)

    if settings['auto_send_album'] and tasks_left == 0 and len(files_ready) > 0:
        threading.Thread(target=send_user_album, args=(client, chat_id, user_id)).start()
        
    elif not settings['auto_send_album'] and tasks_left == 0 and len(files_ready) > 0:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 إرسال النتيجة كألبوم", callback_data="send_batch_album")],
            [InlineKeyboardButton("🗑️ مسح وإلغاء", callback_data="clear_batch_album")]
        ])
        p_msg = client.send_message(chat_id, f"✅ **اكتملت العمليات بالخلفية!**\nلديك ({len(files_ready)}) فيديوهات. إرسالها الآن؟", reply_markup=markup)
        track_message_for_cleanup(user_id, p_msg.id)

# -------------------------- وظائف المساعدة والتقدم الأصلية --------------------------

def update_progress_msg(current, total, client, message, action, start_time, known_size=0):
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
    
    # استخدام كلمة "معالجة" بدلاً من "ضغط" بناءً على السجلات التي أعطيتها في آخر طلبك لتقوم بقراءة الثواني للمحرك FFmpeg
    if "معالجة" in action:
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
            
            if "معالجة" not in action:
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
    
    try: client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except FloodWait as e: time.sleep(e.value)
    except MessageNotModified: pass
    except Exception: pass
        
def get_video_info_and_thumb(file_path):
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
        if not os.path.exists(thumb_path): thumb_path = None
    except Exception as e: print(f"Error info: {e}")
    return thumb_path, duration, width, height

def time_to_seconds(time_str):
    try: h, m, s = time_str.split(':'); return int(h) * 3600 + int(m) * 60 + float(s)
    except: return 0

def get_telegram_duration(message):
    if message.video and message.video.duration: return float(message.video.duration)
    elif message.animation and message.animation.duration: return float(message.animation.duration)
    return 0

def get_video_duration(file_path):
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_format "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        return float(data['format']['duration'])
    except: return 0

def calculate_target_bitrate(target_size_mb, duration_seconds, audio_bitrate_kbps=128):
    if duration_seconds <= 0: return 500
    total_bitrate_kbps = (target_size_mb * 8192) / duration_seconds
    video_bitrate_kbps = int(total_bitrate_kbps - audio_bitrate_kbps)
    return max(50, video_bitrate_kbps)

def cleanup_downloads():
    print("Cleaning up downloads directory...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path): os.remove(file_path); print(f"Deleted old file: {file_path}")
        except Exception: pass

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

    total_duration = get_telegram_duration(message)
    if total_duration <= 0: total_duration = get_video_duration(file_path)

    print(f"\n[{thread_name}] Original file: {os.path.basename(file_path)} | Size: {os.path.getsize(file_path)/(1024*1024):.2f}MB")

    if button_message_id and button_message_id in user_video_data:
        user_video_data[button_message_id]['processing_started'] = True
        try:
            app.edit_message_reply_markup(chat_id=message.chat.id, message_id=button_message_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ جاري العمل...", callback_data="none")]]))
            track_message_for_cleanup(user_id, button_message_id)
        except: pass

    temp_compressed_filename = None

    try:
        if not os.path.exists(file_path):
            message.reply_text("❌ لم يتم العثور على الملف الأصلي.")
            return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        if isinstance(quality, dict) and 'target_size' in quality:
            target_size_mb = quality['target_size']
            try: audio_k = int(str(VIDEO_AUDIO_BITRATE).lower().replace('k', '').strip())
            except: audio_k = 128
            target_v_bitrate = calculate_target_bitrate(target_size_mb, total_duration, audio_k)
            quality_settings = f"-b:v {target_v_bitrate}k -maxrate {target_v_bitrate}k -bufsize {target_v_bitrate*2}k -preset fast"
            used_mode_text = f"🎯 الهدف (حجم/نسبة): ~{target_size_mb:.2f} MB"
        else:
            quality_value = int(quality.split('_')[1]) if isinstance(quality, str) and 'crf_' in quality else int(quality)
            preset = "fast"
            if quality_value <= 18: preset = "slow"
            elif quality_value <= 23: preset = "medium"
            elif quality_value >= 27: preset = "veryfast" if encoder == 'libx264' else "fast"
            quality_param = "cq" if "nvenc" in encoder else "crf"
            quality_settings = f"-{quality_param} {quality_value} -preset {preset}"
            used_mode_text = f"🎥 الجودة (CRF/CQ): {quality_value}"
        
        ffmpeg_command = f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -map_metadata -1 {quality_settings} -movflags +faststart "{temp_compressed_filename}"'

        # تم تحديث الكلمة لتتوافق مع الكونصول "⚙️ جاري المعالجة..."
        progress_msg = message.reply_text("⚙️ **جاري المعالجة...**", quote=True)
        start_time = time.time()

        process = subprocess.Popen(ffmpeg_command, shell=True, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')
        for line in process.stderr:
            if total_duration > 0:
                time_match = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d+)", line)
                if time_match:
                    current_time_sec = time_to_seconds(time_match.group(1))
                    update_progress_msg(current_time_sec, total_duration, app, progress_msg, "⚙️ جاري المعالجة...", start_time)

        process.wait()
        if process.returncode != 0: raise Exception("FFmpeg failed.")
            
        try: progress_msg.delete()
        except: pass

        compressed_file_size_mb = os.path.getsize(temp_compressed_filename) / (1024 * 1024)
        
        # حفظ الألبوم 
        try:
            album_copy_path = os.path.join(DOWNLOADS_DIR, f"album_file_{user_id}_{int(time.time()*100)}.mp4")
            shutil.copy2(temp_compressed_filename, album_copy_path)
            with task_lock:
                if user_id not in user_finished_files: user_finished_files[user_id] = []
                user_finished_files[user_id].append(album_copy_path)
                
                # فحص الـ 10 إذا مفعل الإرسال التلقائي
                if user_prefs['auto_send_album'] and len(user_finished_files[user_id]) >= 10:
                    threading.Thread(target=send_user_album, args=(app, message.chat.id, user_id)).start()
        except Exception as ex: print(f"Copy fail: {ex}")

        # رسالة مؤقتة لتنظيفها
        fin_msg = message.reply_text(f"✅ اكتمل. تم النقل للألبوم.\n{used_mode_text}", quote=True)
        track_message_for_cleanup(user_id, fin_msg.id)
        
    except Exception as e:
        message.reply_text(f"❌ خطأ: `{str(e)[:150]}`", quote=True)
    finally:
        if temp_compressed_filename and os.path.exists(temp_compressed_filename): os.remove(temp_compressed_filename)

        auto_compress_status_message_id = video_data.get('auto_compress_status_message_id')
        if auto_compress_status_message_id:
            try: app.delete_messages(chat_id=message.chat.id, message_ids=auto_compress_status_message_id)
            except: pass

        if button_message_id and button_message_id in user_video_data:
            user_video_data[button_message_id]['processing_started'] = False
            user_video_data[button_message_id]['quality'] = None

        check_and_prompt_album(user_id, app, message.chat.id)


def auto_select_medium_quality(button_message_id):
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        if not video_data.get('processing_started'):
            video_data['quality'] = "crf_23"
            try:
                app.edit_message_reply_markup(chat_id=video_data['message'].chat.id, message_id=button_message_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ فعل لانقضاء الوقت", callback_data="none")]]))
                track_message_for_cleanup(video_data['user_id'], button_message_id)
            except: pass
            compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- أوامر ورسائل التيليجرام --------------------------

app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

@app.on_message(filters.command("start"))
def start_command(client, message):
    message.reply_text("👋 أهلاً! البوت يعمل كألبومات. كل الإعدادات متوفرة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]]), quote=True)

@app.on_message(filters.command("settings"))
def settings_command(client, message):
    send_settings_menu(client, message.chat.id, message.from_user.id)

@app.on_message(filters.text)
def handle_text_inputs(client, message):
    user_id = message.from_user.id
    if user_id not in user_states: return
    st = user_states[user_id]
    state = st.get("state")

    if state == "waiting_for_target_size":
        bid = st.get("button_message_id")
        try:
            size = float(message.text)
            if bid in user_video_data:
                video_data = user_video_data[bid]
                video_data['quality'] = {"target_size": size}
                compression_executor.submit(process_video_for_compression, video_data)
                del user_states[user_id]
        except: message.reply_text("❌ أرسل رقماً.")
        try: message.delete()
        except: pass

    elif state == "waiting_for_percentage":
        bid = st.get("button_message_id")
        try:
            pct = float(message.text)
            if bid in user_video_data:
                video_data = user_video_data[bid]
                video_data['quality'] = {"target_size": (pct/100) * (os.path.getsize(video_data['file']) / (1024*1024))}
                compression_executor.submit(process_video_for_compression, video_data)
                del user_states[user_id]
        except: message.reply_text("❌ أرسل بين 1-100.")
        try: message.delete()
        except: pass

    elif state == "waiting_for_cq_value":
        try:
            get_user_settings(user_id)['auto_quality_value'] = int(message.text)
            user_states.pop(user_id, None)
            send_settings_menu(client, message.chat.id, user_id)
        except: pass

    elif state == "waiting_for_auto_percentage":
        try:
            get_user_settings(user_id)['auto_percent_value'] = float(message.text)
            user_states.pop(user_id, None)
            send_settings_menu(client, message.chat.id, user_id)
        except: pass

def send_settings_menu(client, chat_id, user_id, message_id=None):
    s = get_user_settings(user_id)
    text = (f"**⚙️ الإعدادات:**\n🔹 الترميز: `{s['encoder']}`\n🔸 التلقائي: {'✅' if s['auto_compress'] else '❌'}\n"
            f"📊 وضع التلقائي: {'(نسبة %)' if s.get('auto_mode')=='percent' else '(CRF)'}\n"
            f"📦 إرسال الألبوم: {'مفعل(تلقائي/10)' if s['auto_send_album'] else 'عن طريق زر ידوي'}")
    kb = [
        [InlineKeyboardButton("🔄 المحرك", callback_data="settings_encoder")],
        [InlineKeyboardButton("تبديل نمط التلقائي", callback_data="settings_toggle_mode"), InlineKeyboardButton("تفعيل התلقائي", callback_data="settings_toggle_auto")],
        [InlineKeyboardButton("✏️ CRF للتلقائي", callback_data="settings_custom_quality"), InlineKeyboardButton("📉 النسبة(%) للتلقائي", callback_data="settings_custom_percent")],
        [InlineKeyboardButton("📦 نظام الألبوم (تلقائي/يدوي)", callback_data="settings_toggle_send")],
        [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]
    ]
    if message_id:
        try: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(kb))
        except: pass
    else: client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(kb))

def start_download_task(original_message_id):
    video_data = user_video_data[original_message_id]
    message, user_id = video_data['message'], video_data['user_id']
    f_id = message.video.file_id if message.video else message.animation.file_id
    f_sz = message.video.file_size if message.video else message.animation.file_size
    f_name = os.path.join(DOWNLOADS_DIR, f"{user_id}_{message.id}.mp4")

    down_msg = message.reply_text("📥 في الطابور...", quote=True)
    video_data['download_msg'] = down_msg

    try:
        path = app.download_media(f_id, file_name=f_name, progress=update_progress_msg, progress_args=(app, down_msg, "📥 جاري التنزيل...", time.time(), f_sz))
        video_data['file'] = path
        try: down_msg.delete()
        except: pass
        
        s = get_user_settings(user_id)
        if s['auto_compress']:
            if s.get('auto_mode') == 'percent':
                pct = s.get('auto_percent_value', 50)
                video_data['quality'] = {"target_size": (pct/100) * (os.path.getsize(path)/(1024*1024))}
            else: video_data['quality'] = s['auto_quality_value']
            
            comp_st = message.reply_text("🚀 تلقائي: جاري الضغط...")
            video_data['auto_compress_status_message_id'] = comp_st.id
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("ضعيفة (27)", callback_data="crf_27"), InlineKeyboardButton("متوسط (23)", callback_data="crf_23")],
                [InlineKeyboardButton("🎯 حجم (MB)", callback_data="target_size_prompt"), InlineKeyboardButton("📉 نسبة (%)", callback_data="target_percent_prompt")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_compression")]
            ])
            rep = message.reply_text("✅ استُلم.", reply_markup=kb, quote=True)
            track_message_for_cleanup(user_id, rep.id)
            video_data['button_message_id'] = rep.id
            user_video_data[rep.id] = user_video_data.pop(original_message_id)
            t = threading.Timer(300, auto_select_medium_quality, args=[rep.id])
            user_video_data[rep.id]['timer'] = t
            t.start()
    except Exception as e:
        message.reply_text(f"❌ خطأ: {e}")
        with task_lock: user_active_tasks[user_id] -= 1

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    user_id = message.from_user.id
    with task_lock:
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
    
    user_video_data[message.id] = {'message': message, 'user_id': user_id, 'processing_started': False, 'file': None}
    download_executor.submit(start_download_task, message.id)

@app.on_callback_query()
def universal_callback_handler(client, callback_query):
    data, user_id, message = callback_query.data, callback_query.from_user.id, callback_query.message

    if data == "send_batch_album":
        callback_query.answer()
        try: message.delete()
        except: pass
        threading.Thread(target=send_user_album, args=(client, message.chat.id, user_id)).start()
        return

    elif data == "clear_batch_album":
        with task_lock:
            for f in user_finished_files.get(user_id, []):
                if os.path.exists(f): os.remove(f)
            user_finished_files[user_id] = []
        callback_query.answer("🗑 تنظيف...")
        try: message.delete()
        except: pass
        return

    if data.startswith("settings"):
        if data == "settings": send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_encoder":
            keyboard = [[InlineKeyboardButton("H.265", callback_data="set_encoder:hevc_nvenc"), InlineKeyboardButton("H.264", callback_data="set_encoder:h264_nvenc")],
                        [InlineKeyboardButton("CPU", callback_data="set_encoder:libx264"), InlineKeyboardButton("رجوع", callback_data="settings")]]
            message.edit_text("إختر محرك:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "settings_custom_quality":
            user_states[user_id] = {"state": "waiting_for_cq_value", "prompt_message_id": message.id}
            message.edit_text("أرسل قيمة لـ CRF.")
        elif data == "settings_custom_percent":
            user_states[user_id] = {"state": "waiting_for_auto_percentage", "prompt_message_id": message.id}
            message.edit_text("أرسل نسبة (1-100).")
        elif data == "settings_toggle_mode":
            get_user_settings(user_id)['auto_mode'] = 'percent' if get_user_settings(user_id).get('auto_mode') == 'crf' else 'crf'
            send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_toggle_auto":
            get_user_settings(user_id)['auto_compress'] = not get_user_settings(user_id)['auto_compress']
            send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_toggle_send":
            get_user_settings(user_id)['auto_send_album'] = not get_user_settings(user_id)['auto_send_album']
            send_settings_menu(client, message.chat.id, user_id, message.id)
        callback_query.answer()
        return

    elif data.startswith("set_encoder:"):
        get_user_settings(user_id)['encoder'] = data.split(":")[1]
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "close_settings":
        try: message.delete()
        except: pass
        return

    bid = message.id
    if bid not in user_video_data:
        callback_query.answer("تم التعامل مسبقاً", show_alert=True)
        return
    
    video_data = user_video_data[bid]
    if data == "cancel_compression":
        if video_data.get('timer'): video_data['timer'].cancel()
        if video_data.get('file') and os.path.exists(video_data['file']): os.remove(video_data['file'])
        try: message.delete()
        except: pass
        check_and_prompt_album(user_id, client, message.chat.id)
        if bid in user_video_data: del user_video_data[bid]
        return

    if data == "target_size_prompt":
        if video_data.get('timer'): video_data['timer'].cancel()
        p = message.reply_text("🔢 أرسل الحجم بـ MB", quote=True)
        user_states[user_id] = {"state": "waiting_for_target_size", "button_message_id": bid}
        track_message_for_cleanup(user_id, p.id)
        return
        
    if data == "target_percent_prompt":
        if video_data.get('timer'): video_data['timer'].cancel()
        p = message.reply_text("🔢 أرسل النسبة (100) ليضغط بناء عليه.", quote=True)
        user_states[user_id] = {"state": "waiting_for_percentage", "button_message_id": bid}
        track_message_for_cleanup(user_id, p.id)
        return

    if video_data.get('timer'): video_data['timer'].cancel()
    video_data['quality'] = data
    compression_executor.submit(process_video_for_compression, video_data)

if __name__ == "__main__":
    cleanup_downloads()
    print("\n✅ البوت تم تحديثه، والمزامنة والرسائل واللوج شغالة بشكل مثالي!")
    app.run()
