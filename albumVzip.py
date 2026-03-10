import os
import tempfile
import subprocess
import threading
import time
import re
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
from pyrogram.errors import MessageEmpty, UserNotParticipant, MessageNotModified, FloodWait

from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

download_executor = ThreadPoolExecutor(max_workers=3)
compression_executor = ThreadPoolExecutor(max_workers=3)

user_states = {}
user_settings = {}
user_video_data = {}
PROGRESS_TRACKER = {}

user_active_tasks = {}
user_finished_files = {}
user_cleanup_messages = {}
task_lock = threading.Lock()

DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_quality_value': 30,
    'auto_mode': 'crf',
    'auto_percent_value': 50,
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
    print(f"[CLEANUP LOG] User {user_id}: Added message {message_id} to cleanup list.")

# -------------------------- دالة إرسال الألبوم والتنظيف --------------------------
def send_user_album(client, chat_id, user_id):
    with task_lock:
        files_to_send = list(user_finished_files.get(user_id, []))
        if not files_to_send:
            return
        user_finished_files[user_id] = []
    
    print(f"[ALBUM LOG] User {user_id}: Preparing to send {len(files_to_send)} files as an album/video.")
    st_msg = client.send_message(chat_id, f"📦 جاري تجميع ورفع ({len(files_to_send)}) فيديوهات...")

    try:
        for i in range(0, len(files_to_send), 10):
            chunk = files_to_send[i:i+10]
            media_group = []
            for f_path in chunk:
                thumb, dur, w, h = get_video_info_and_thumb(f_path)
                media_group.append(InputMediaVideo(f_path, thumb=thumb, duration=int(dur), width=w, height=h))
            
            if len(media_group) == 1:
                client.send_video(chat_id, video=chunk[0], caption="📦 النتيجة النهائية.")
            else:
                client.send_media_group(chat_id, media=media_group)
            
            print(f"[ALBUM LOG] User {user_id}: Successfully sent a chunk of {len(chunk)} videos.")
            
    except Exception as e:
        client.send_message(chat_id, f"❌ خطأ أثناء رفع المجموعة: {e}")
        print(f"[ERROR LOG] User {user_id}: Failed to send media group - {e}")
    finally:
        for f_path in files_to_send:
            if os.path.exists(f_path): os.remove(f_path)
        
        # التنظيف التلقائي للرسائل
        if user_id in user_cleanup_messages:
            try:
                client.delete_messages(chat_id, user_cleanup_messages[user_id])
                print(f"[CLEANUP LOG] User {user_id}: Cleaned up {len(user_cleanup_messages[user_id])} temporary messages.")
            except: pass
            user_cleanup_messages[user_id] = []
        try: st_msg.delete()
        except: pass

def check_and_prompt_album(user_id, client, chat_id):
    with task_lock:
        if user_id in user_active_tasks:
            user_active_tasks[user_id] -= 1
            if user_active_tasks[user_id] <= 0:
                user_active_tasks[user_id] = 0
                
    tasks_left = user_active_tasks.get(user_id, 0)
    files_ready_count = len(user_finished_files.get(user_id, []))
    settings = get_user_settings(user_id)
    
    print(f"[TASK LOG] User {user_id}: A task finished. Tasks remaining: {tasks_left}. Files ready: {files_ready_count}.")

    if tasks_left == 0 and files_ready_count > 0:
        if settings['auto_send_album']:
            print(f"[ALBUM LOG] User {user_id}: All tasks finished. Auto-sending album now.")
            threading.Thread(target=send_user_album, args=(client, chat_id, user_id)).start()
        else:
            print(f"[ALBUM LOG] User {user_id}: All tasks finished. Prompting user to send manually.")
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 إرسال النتيجة", callback_data="send_batch_album")],
                [InlineKeyboardButton("🗑️ مسح الذاكرة", callback_data="clear_batch_album")]
            ])
            p_msg = client.send_message(chat_id, f"✅ **اكتملت كل العمليات!**\nجاهز للإرسال: {files_ready_count} فيديوهات.", reply_markup=markup)
            track_message_for_cleanup(user_id, p_msg.id)

# -------------------------- وظائف المساعدة وحساب الحجم والتقدم --------------------------

def update_progress_msg(current, total, client, message, action, start_time, known_size=0):
    now = time.time()
    msg_id = message.id
    if total <= 0 and known_size > 0: total = known_size
    is_finished = (current >= total) if total > 0 else False
    if msg_id in PROGRESS_TRACKER and (now - PROGRESS_TRACKER[msg_id]) < 5.0 and not is_finished: return
    PROGRESS_TRACKER[msg_id] = now
    
    percent = (current * 100 / total) if total > 0 else 0
    filled = int(percent / 10) if percent > 0 else 0
    if filled > 10: filled = 10
    bar = f"[{'█' * filled}{'░' * (10 - filled)}]"
    
    if "ضغط" in action or "المعالجة" in action: # ---- إصلاح الخطأ: الشرط الآن صحيح ليظهر الثواني
        curr_val, total_val = f"{current:.1f} ثانية", f"{total:.1f} ثانية" if total > 0 else "??"
    else: 
        curr_val, total_val = f"{current / (1024 * 1024):.2f} MB", f"{total / (1024 * 1024):.2f} MB" if total > 0 else "??"

    elapsed = now - start_time
    speed_text, console_speed, eta_text = "", "", "جاري الحساب..."
    if elapsed > 0:
        speed = current / elapsed
        if speed > 0:
            if total > 0:
                eta_seconds = max(0, (total - current) / speed)
                eta_text = f"{int(eta_seconds)} ثانية"
            if "ضغط" not in action and "المعالجة" not in action:
                speed_mb = speed / (1024 * 1024)
                speed_text = f"🚀 **السرعة:** `{speed_mb:.2f} MB/s`\n"
                console_speed = f"| السرعة: {speed_mb:.2f} MB/s "

    text = f"{action}\n{bar} `{percent:.1f}%`\n📊 **التقدم:** `{curr_val} / {total_val}`\n{speed_text}⏱ **الوقت المتبقي:** `{eta_text}`"
    clean_action = action.replace('*', '').replace('`', '').split('\n')[0].strip()
    
    # تحسين تسجيل الكونسول
    if message.chat:
      console_log = f"[PROGRESS] User {message.chat.id} | Task Msg:{msg_id} | {clean_action} | {percent:.1f}% | {curr_val} / {total_val} {console_speed}| المتبقي: {eta_text}"
      print(console_log)
    
    try: client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except Exception: pass
        
def get_video_info_and_thumb(file_path):
    duration, width, height, thumb_path = 0.0, 0, 0, None
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_streams "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                width, height, duration = int(stream.get('width', 0)), int(stream.get('height', 0)), float(stream.get('duration', 0))
                break
        
        thumb_time = min(1.0, duration * 0.1) if duration > 0 else 1.0
        thumb_path = file_path + "_thumb.jpg"
        thumb_cmd = f'ffmpeg -y -ss {thumb_time} -i "{file_path}" -vframes 1 -vf "scale=320:-1" -q:v 5 "{thumb_path}" -loglevel quiet'
        subprocess.run(thumb_cmd, shell=True)
        if not os.path.exists(thumb_path): thumb_path = None
    except Exception as e: print(f"Error getting video info: {e}")
    return thumb_path, duration, width, height

def time_to_seconds(time_str):
    try: h, m, s = time_str.split(':'); return int(h) * 3600 + int(m) * 60 + float(s)
    except: return 0

def get_telegram_duration(message):
    if message.video and message.video.duration: return float(message.video.duration)
    if message.animation and message.animation.duration: return float(message.animation.duration)
    return 0

def get_video_duration(file_path):
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_format "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return float(json.loads(result.stdout)['format']['duration'])
    except: return 0

def calculate_target_bitrate(target_size_mb, duration_seconds, audio_bitrate_kbps=128):
    if duration_seconds <= 0: return 500
    total_bitrate_kbps = (target_size_mb * 8192) / duration_seconds
    return max(50, int(total_bitrate_kbps - audio_bitrate_kbps))

def cleanup_downloads():
    print("Cleaning up downloads directory...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path): os.remove(file_path)
        except Exception: pass

app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

def process_video_for_compression(video_data):
    thread_name = threading.current_thread().name
    file_path, message = video_data['file'], video_data['message']
    button_message_id = video_data.get('button_message_id')
    quality, user_id = video_data['quality'], video_data['user_id']
    user_prefs = get_user_settings(user_id)
    encoder = user_prefs['encoder']

    total_duration = get_telegram_duration(message)
    if total_duration <= 0: total_duration = get_video_duration(file_path)

    print(f"\n[COMPRESSION START] User {user_id} | Thread {thread_name}: Compressing '{os.path.basename(file_path)}' ({os.path.getsize(file_path)/(1024*1024):.2f}MB).")

    if button_message_id in user_video_data:
        user_video_data[button_message_id]['processing_started'] = True
        track_message_for_cleanup(user_id, button_message_id)

    temp_compressed_filename = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        if isinstance(quality, dict) and 'target_size' in quality:
            target_size_mb = quality['target_size']
            target_v_bitrate = calculate_target_bitrate(target_size_mb, total_duration, 128)
            quality_settings = f"-b:v {target_v_bitrate}k -maxrate {target_v_bitrate}k -bufsize {target_v_bitrate*2}k -preset fast"
            used_mode_text = f"🎯 طلب حجم مستهدف/نسبة: ~{target_size_mb:.2f} MB"
        else:
            quality_value = int(quality.split('_')[1]) if 'crf_' in str(quality) else int(quality)
            preset = "fast"
            if quality_value <= 18: preset = "slow"
            quality_param = "cq" if "nvenc" in encoder else "crf"
            quality_settings = f"-{quality_param} {quality_value} -preset {preset}"
            used_mode_text = f"🎥 الجودة (CRF): {quality_value}"
        
        ffmpeg_command = f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -map_metadata -1 {quality_settings} -movflags +faststart "{temp_compressed_filename}"'

        progress_msg = message.reply_text("🔄 **بدأ ضغط الفيديو (متزامن)...**", quote=True)
        track_message_for_cleanup(user_id, progress_msg.id)
        start_time = time.time()
        process = subprocess.Popen(ffmpeg_command, shell=True, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')

        for line in process.stderr:
            if total_duration > 0:
                time_match = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d+)", line)
                if time_match:
                    update_progress_msg(time_to_seconds(time_match.group(1)), total_duration, app, progress_msg, "⚙️ **جاري ضغط ومعالجة...**", start_time) # اسم صحيح
        process.wait()
        if process.returncode != 0: raise Exception("FFmpeg failed.")
        
        compressed_size_mb = os.path.getsize(temp_compressed_filename)/(1024*1024)
        print(f"[COMPRESSION END] User {user_id}: Finished '{os.path.basename(file_path)}'. New size: {compressed_size_mb:.2f} MB. Storing for album.")

        try:
            album_copy_path = os.path.join(DOWNLOADS_DIR, f"album_file_{user_id}_{int(time.time()*100)}.mp4")
            shutil.copy2(temp_compressed_filename, album_copy_path)
            with task_lock:
                if user_id not in user_finished_files: user_finished_files[user_id] = []
                user_finished_files[user_id].append(album_copy_path)
                
                if user_prefs['auto_send_album'] and len(user_finished_files[user_id]) >= 10:
                    print(f"[ALBUM LOG] User {user_id}: Reached 10 files. Triggering auto-send.")
                    threading.Thread(target=send_user_album, args=(app, message.chat.id, user_id)).start()
        except Exception as ex: print(f"Failed copy for album: {ex}")
        
        fin_msg = message.reply_text(f"✅ **اكتمل ضغط الملف وحفظه**\n🔻 الحجم القديم: {os.path.getsize(file_path)/(1024*1024):.2f} MB\n✅ الحجم الجديد: {compressed_size_mb:.2f} MB", quote=True)
        track_message_for_cleanup(user_id, fin_msg.id)
        
    except Exception as e:
        message.reply_text(f"❌ حدث خطأ: `{str(e)[:150]}`", quote=True)
        print(f"[ERROR LOG] FFMPEG Error for user {user_id}: {e}")
    finally:
        if temp_compressed_filename and os.path.exists(temp_compressed_filename): os.remove(temp_compressed_filename)
        if 'auto_compress_status_message_id' in video_data: track_message_for_cleanup(user_id, video_data['auto_compress_status_message_id'])
        if button_message_id in user_video_data: user_video_data[button_message_id]['processing_started'] = False
        check_and_prompt_album(user_id, app, message.chat.id)


def auto_select_medium_quality(button_message_id):
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        if not video_data.get('processing_started'):
            video_data['quality'] = "crf_23"
            print(f"[AUTOSELECT LOG] User {video_data['user_id']}: Timeout reached, auto-selecting CRF 23.")
            track_message_for_cleanup(video_data['user_id'], button_message_id)
            compression_executor.submit(process_video_for_compression, video_data)

@app.on_message(filters.command("start"))
def start_command(client, message):
    message.reply_text("👋 أهلاً بك! أرسل فيديوهات للضغط المتزامن.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]]), quote=True)

@app.on_message(filters.command("settings"))
def settings_command(client, message): send_settings_menu(client, message.chat.id, message.from_user.id)

@app.on_message(filters.text)
def handle_text_inputs(client, message):
    user_id = message.from_user.id
    if user_id not in user_states: return
    state, button_msg_id = user_states[user_id].get("state"), user_states[user_id].get("button_message_id")
    
    if state == "waiting_for_target_size":
        try:
            size = float(message.text)
            if size > 0 and button_msg_id in user_video_data:
                user_video_data[button_msg_id]['quality'] = {"target_size": size}
                compression_executor.submit(process_video_for_compression, user_video_data[button_msg_id])
                del user_states[user_id]
            else: raise ValueError()
        except: message.reply_text("❌ أرسل رقماً صحيحاً للميغا.")
        try: message.delete()
        except: pass

    elif state == "waiting_for_percentage":
        try:
            pct = float(message.text)
            if 1 <= pct <= 100 and button_msg_id in user_video_data:
                orig_size = os.path.getsize(user_video_data[button_msg_id]['file']) / (1024*1024)
                user_video_data[button_msg_id]['quality'] = {"target_size": (pct/100) * orig_size}
                compression_executor.submit(process_video_for_compression, user_video_data[button_msg_id])
                del user_states[user_id]
            else: raise ValueError()
        except: message.reply_text("❌ أرسل من 1 لـ 100 فقط.")
        try: message.delete()
        except: pass

    elif state == "waiting_for_cq_value":
        try:
            val = int(message.text)
            if 0 <= val <= 51:
                get_user_settings(user_id)['auto_quality_value'] = val
                user_states.pop(user_id, None); send_settings_menu(client, message.chat.id, user_id)
        except: pass

    elif state == "waiting_for_auto_percentage":
        try:
            val = float(message.text)
            if 1 <= val <= 100:
                get_user_settings(user_id)['auto_percent_value'] = val
                user_states.pop(user_id, None); send_settings_menu(client, message.chat.id, user_id)
        except: pass

def send_settings_menu(client, chat_id, user_id, message_id=None):
    s = get_user_settings(user_id)
    auto_send_text = "تلقائي (عند الانتهاء أو كل 10)" if s['auto_send_album'] else "يدوي (عبر زر نهائي)"
    mode_text = "(نسبة %)" if s['auto_mode'] == 'percent' else "(جودة CRF)"

    text = f"**⚙️ الإعدادات:**\n🔹 المحرك: `{s['encoder']}`\n🔸 التلقائي: `{'✅' if s['auto_compress'] else '❌'}`\n📊 وضع التلقائي: `{mode_text}`\n📈 القيم: CRF `{s['auto_quality_value']}` | النسبة `{s['auto_percent_value']}%`\n📦 رفع الألبوم: `{auto_send_text}`"
    
    keyboard = [
        [InlineKeyboardButton("🔄 تغيير المحرك", callback_data="settings_encoder")],
        [InlineKeyboardButton("تبديل وضع التلقائي", callback_data="settings_toggle_mode"), InlineKeyboardButton("تفعيل/إيقاف التلقائي", callback_data="settings_toggle_auto")],
        [InlineKeyboardButton("✏️ ضبط CRF", callback_data="settings_custom_quality"), InlineKeyboardButton("📉 ضبط النسبة %", callback_data="settings_custom_percent")],
        [InlineKeyboardButton("📦 نظام الألبوم (تلقائي/يدوي)", callback_data="settings_toggle_send")],
        [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]
    ]
    if message_id:
        try: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
        except: pass
    else: client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

def post_download_actions(original_message_id):
    if original_message_id not in user_video_data: return
    vd, user_id = user_video_data[original_message_id], user_video_data[original_message_id]['user_id']
    try:
        vd['file'] = vd['download_future'].result()
        print(f"[DOWNLOAD END] User {user_id}: Downloaded '{os.path.basename(vd['file'])}'. Size: {os.path.getsize(vd['file'])/(1024*1024):.2f}MB.")
        try: vd['download_msg'].delete()
        except: pass
        
        user_prefs = get_user_settings(user_id)
        if user_prefs['auto_compress']:
            if user_prefs.get('auto_mode') == 'percent':
                pct = user_prefs.get('auto_percent_value', 50)
                vd['quality'] = {"target_size": (pct/100)*(os.path.getsize(vd['file'])/(1024*1024))}
            else: vd['quality'] = user_prefs['auto_quality_value']
            
            st_msg = vd['message'].reply_text("🚀 جاري إضافة الملف لمعالج الضغط التلقائي...", quote=True)
            vd['auto_compress_status_message_id'] = st_msg.id
            compression_executor.submit(process_video_for_compression, vd)
        else:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("ضعيفة (27)", callback_data="crf_27"), InlineKeyboardButton("متوسط (23)", callback_data="crf_23"), InlineKeyboardButton("عالي (18)", callback_data="crf_18")],
                [InlineKeyboardButton("🎯 تحديد حجم (MB)", callback_data="target_size_prompt"), InlineKeyboardButton("📉 تحديد نسبة %", callback_data="target_percent_prompt")],
                [InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_compression")]
            ])
            rep = vd['message'].reply_text("✅ استُلم الملف. اختر:", reply_markup=markup, quote=True)
            vd['button_message_id'] = rep.id
            user_video_data[rep.id] = user_video_data.pop(original_message_id)
            user_video_data[rep.id]['timer'] = threading.Timer(300, auto_select_medium_quality, args=[rep.id])
            user_video_data[rep.id]['timer'].start()
    except Exception as e:
        vd['message'].reply_text(f"❌ خطأ أثناء التحميل: {e}")
        check_and_prompt_album(user_id, app, vd['message'].chat.id)

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    user_id = message.from_user.id
    with task_lock:
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
    print(f"[VIDEO RECEIVED] User {user_id}: Added msg {message.id} to queue. Total active tasks: {user_active_tasks[user_id]}.")
    
    file_id = message.video.file_id if message.video else message.animation.file_id
    file_size = message.video.file_size if message.video else message.animation.file_size
    file_name = os.path.join(DOWNLOADS_DIR, f"{user_id}_{message.id}.mp4")
    
    download_msg = message.reply_text("📥 في طابور التنزيل...", quote=True)
    track_message_for_cleanup(user_id, download_msg.id)
    
    download_future = download_executor.submit(
        client.download_media, message=file_id, file_name=file_name,
        progress=update_progress_msg, progress_args=(client, download_msg, "📥 **جاري تنزيل الملف...**", time.time(), file_size)
    )

    user_video_data[message.id] = {'message': message, 'download_msg': download_msg, 'download_future': download_future, 'user_id': user_id}
    threading.Thread(target=post_download_actions, args=[message.id]).start()

@app.on_callback_query()
def universal_callback_handler(client, callback_query):
    data, user_id, message = callback_query.data, callback_query.from_user.id, callback_query.message

    if data == "send_batch_album":
        threading.Thread(target=send_user_album, args=(client, message.chat.id, user_id)).start()
        return

    elif data == "clear_batch_album":
        with task_lock:
            for f in user_finished_files.get(user_id, []):
                if os.path.exists(f): os.remove(f)
            user_finished_files[user_id] = []
        track_message_for_cleanup(user_id, message.id)
        callback_query.answer("🗑 تم تنظيف الذاكرة.")
        return

    if data.startswith("settings"):
        settings = get_user_settings(user_id)
        if data == "settings": send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_encoder": message.edit_text("إختر المحرك:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("H.265 (HEVC)", callback_data="set_encoder:hevc_nvenc"), InlineKeyboardButton("H.264 (NV)", callback_data="set_encoder:h264_nvenc")], [InlineKeyboardButton("H.264 (CPU)", callback_data="set_encoder:libx264")], [InlineKeyboardButton("« رجوع", callback_data="settings")]]))
        elif data == "settings_custom_quality": user_states[user_id] = {"state": "waiting_for_cq_value"}
        elif data == "settings_custom_percent": user_states[user_id] = {"state": "waiting_for_auto_percentage"}
        elif data == "settings_toggle_mode": settings['auto_mode'] = 'percent' if settings.get('auto_mode') == 'crf' else 'crf'; send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_toggle_auto": settings['auto_compress'] = not settings['auto_compress']; send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_toggle_send": settings['auto_send_album'] = not settings['auto_send_album']; send_settings_menu(client, message.chat.id, user_id, message.id)
        return

    elif data.startswith("set_encoder:"): get_user_settings(user_id)['encoder'] = data.split(":")[1]; send_settings_menu(client, message.chat.id, user_id, message.id); return
    elif data == "close_settings": message.delete(); return

    button_message_id = message.id
    if button_message_id not in user_video_data: return

    video_data = user_video_data[button_message_id]
    if data == "cancel_compression":
        if video_data.get('timer'): video_data['timer'].cancel()
        if video_data.get('file') and os.path.exists(video_data['file']): os.remove(video_data['file'])
        message.delete()
        check_and_prompt_album(user_id, client, message.chat.id)
        if button_message_id in user_video_data: del user_video_data[button_message_id]
        return

    if data == "target_size_prompt":
        if video_data.get('timer'): video_data['timer'].cancel()
        p = message.reply_text("🔢 أرسل الحجم المطلوب بالميغا بايت (MB).", quote=True); user_states[user_id] = {"state": "waiting_for_target_size", "button_message_id": button_message_id}
        track_message_for_cleanup(user_id, p.id); return
        
    if data == "target_percent_prompt":
        if video_data.get('timer'): video_data['timer'].cancel()
        p = message.reply_text("🔢 أرسل النسبة (1-100) لضغط الفيديو.", quote=True); user_states[user_id] = {"state": "waiting_for_percentage", "button_message_id": button_message_id}
        track_message_for_cleanup(user_id, p.id); return

    if video_data.get('timer'): video_data['timer'].cancel()
    video_data['quality'] = data
    compression_executor.submit(process_video_for_compression, video_data)

if __name__ == "__main__":
    cleanup_downloads()
    print("\n✅ البوت يعمل بطوابير المزامنة والألبومات والتنظيف التلقائي...")
    app.run()
