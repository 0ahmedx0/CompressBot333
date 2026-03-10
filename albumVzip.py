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

# طابور 3/3 للتحميل والضغط المتزامن
download_executor = ThreadPoolExecutor(max_workers=3)
compression_executor = ThreadPoolExecutor(max_workers=3)

# قواميس التخزين
user_states = {}
user_settings = {}
user_video_data = {}
PROGRESS_TRACKER = {} 

# خوادم تتبع الألبوم والمهام والتنظيف
user_active_tasks = {}
user_finished_files = {}
user_cleanup_messages = {}
task_lock = threading.Lock()

DEFAULT_SETTINGS = {
    'encoder': 'hevc_nvenc',
    'auto_compress': True,
    'auto_quality_value': 30,
    'auto_mode': 'percent',         
    'auto_percent_value': 50,
    'auto_send_album': True     # إضافة الإعداد الجديد للرفع التلقائي
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

def track_message_for_cleanup(user_id, message_id):
    if user_id not in user_cleanup_messages:
        user_cleanup_messages[user_id] = []
    user_cleanup_messages[user_id].append(message_id)

# -------------------------- دالة إرسال الألبوم والتنظيف --------------------------
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
                    chat_id, video=chunk[0], caption="📦 النتيجة النهائية للملف.",
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
        p_msg = client.send_message(chat_id, f"✅ **اكتملت العمليات!**\nلديك ({len(files_ready)}) فيديوهات جاهزة.", reply_markup=markup)
        track_message_for_cleanup(user_id, p_msg.id)

# -------------------------- وظائف المساعدة والتقدم الأصلية --------------------------

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
    
    if "معالجة" in action:
        curr_val, total_val = f"{current:.1f} ثانية", f"{total:.1f} ثانية" if total > 0 else "??"
    else: 
        curr_val, total_val = f"{current / (1024*1024):.2f} MB", f"{total / (1024*1024):.2f} MB" if total > 0 else "??"

    elapsed = now - start_time
    speed_text, console_speed, eta_text = "", "", "..."
    if elapsed > 0:
        speed = current / elapsed
        if speed > 0:
            if total > 0: eta_text = f"{int(max(0, (total - current) / speed))} ثانية"
            if "معالجة" not in action:
                speed_mb = speed / (1024 * 1024)
                speed_text = f"🚀 **السرعة:** `{speed_mb:.2f} MB/s`\n"
                console_speed = f"| السرعة: {speed_mb:.2f} MB/s "

    text = f"{action}\n{bar} `{percent:.1f}%`\n📊 **التقدم:** `{curr_val} / {total_val}`\n{speed_text}⏱ **المتبقي:** `{eta_text}`"
    clean_action = action.replace('*', '').replace('`', '').split('\n')[0].strip()
    console_log = f"[Task Msg:{msg_id}] {clean_action} | {percent:.1f}% | {curr_val} / {total_val} {console_speed}| المتبقي: {eta_text}"
    print(console_log)
    
    try: client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except: pass
        
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
        thumb_path = file_path + "_thumb.jpg"
        subprocess.run(f'ffmpeg -y -ss {min(1.0, duration * 0.1)} -i "{file_path}" -vframes 1 -vf "scale=320:-1" -q:v 5 "{thumb_path}" -loglevel quiet', shell=True)
        if not os.path.exists(thumb_path): thumb_path = None
    except: pass
    return thumb_path, duration, width, height

def time_to_seconds(time_str):
    try: h, m, s = time_str.split(':'); return int(h) * 3600 + int(m) * 60 + float(s)
    except: return 0

def get_telegram_duration(message):
    if message.video: return float(message.video.duration)
    elif message.animation: return float(message.animation.duration)
    return 0

def get_video_duration(file_path):
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_format "{file_path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return float(json.loads(result.stdout)['format']['duration'])
    except: return 0

def calculate_target_bitrate(target_size_mb, duration_seconds, audio_bitrate_kbps=128):
    if duration_seconds <= 0: return 500
    total_kbps = (target_size_mb * 8192) / duration_seconds
    return max(50, int(total_kbps - audio_bitrate_kbps))

def cleanup_downloads():
    for f in os.listdir(DOWNLOADS_DIR):
        try: os.remove(os.path.join(DOWNLOADS_DIR, f))
        except: pass

# -------------------------- وظائف المعالجة الأساسية --------------------------

def process_video_for_compression(video_data):
    thread_name = threading.current_thread().name
    file_path, message = video_data['file'], video_data['message']
    button_msg_id = video_data.get('button_message_id')
    quality, user_id = video_data['quality'], video_data['user_id']
    user_prefs = get_user_settings(user_id)
    encoder = user_prefs['encoder']

    total_duration = get_telegram_duration(message)
    if total_duration <= 0: total_duration = get_video_duration(file_path)

    print(f"\n[{thread_name}] Original file: {os.path.basename(file_path)} | Size: {os.path.getsize(file_path)/(1024*1024):.2f}MB")

    if button_msg_id and button_msg_id in user_video_data:
        user_video_data[button_msg_id]['processing_started'] = True
        try:
            app.edit_message_reply_markup(chat_id=message.chat.id, message_id=button_msg_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ في طابور المعالجة...", callback_data="none")]]))
            track_message_for_cleanup(user_id, button_msg_id)
        except: pass

    temp_compressed_filename = None

    try:
        if not os.path.exists(file_path): return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        if isinstance(quality, dict) and 'target_size' in quality:
            target_mb = quality['target_size']
            v_bitrate = calculate_target_bitrate(target_mb, total_duration, 128)
            q_settings = f"-b:v {v_bitrate}k -maxrate {v_bitrate}k -bufsize {v_bitrate*2}k -preset fast"
            used_mode_text = f"🎯 الهدف (حجم/نسبة): ~{target_mb:.2f} MB"
        else:
            q_val = int(quality.split('_')[1]) if isinstance(quality, str) and 'crf_' in quality else int(quality)
            preset = "fast"
            if q_val <= 18: preset = "slow"
            elif q_val <= 23: preset = "medium"
            elif q_val >= 27: preset = "veryfast" if encoder == 'libx264' else "fast"
            param = "cq" if "nvenc" in encoder else "crf"
            q_settings = f"-{param} {q_val} -preset {preset}"
            used_mode_text = f"🎥 الجودة (CRF/CQ): {q_val}"
        
        cmd = f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -map_metadata -1 {q_settings} -movflags +faststart "{temp_compressed_filename}"'

        prog_msg = message.reply_text("⚙️ **جاري المعالجة...**", quote=True)
        start_time = time.time()

        process = subprocess.Popen(cmd, shell=True, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')
        for line in process.stderr:
            if total_duration > 0:
                tm = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d+)", line)
                if tm: update_progress_msg(time_to_seconds(tm.group(1)), total_duration, app, prog_msg, "⚙️ جاري المعالجة...", start_time)

        process.wait()
        if process.returncode != 0: raise Exception("FFmpeg failed.")
        try: prog_msg.delete()
        except: pass

        compressed_size_mb = os.path.getsize(temp_compressed_filename) / (1024 * 1024)
        
        # حفظ الألبوم 
        try:
            album_copy_path = os.path.join(DOWNLOADS_DIR, f"album_{user_id}_{int(time.time()*100)}.mp4")
            shutil.copy2(temp_compressed_filename, album_copy_path)
            with task_lock:
                if user_id not in user_finished_files: user_finished_files[user_id] = []
                user_finished_files[user_id].append(album_copy_path)
                
                if user_prefs['auto_send_album'] and len(user_finished_files[user_id]) >= 10:
                    threading.Thread(target=send_user_album, args=(app, message.chat.id, user_id)).start()
        except: pass

        # الرسالة التي تظهر بعد كل فيديو لإعلامك بالنتيجة (سيتم مسحها لاحقاً)
        fin_msg = message.reply_text(
            f"✅ **اكتمل ضغط الملف وجاري حفظه للرفع النهائي...**\n"
            f"🔻 الحجم القديم: {os.path.getsize(file_path)/(1024*1024):.2f} MB\n"
            f"✅ الحجم الجديد: {compressed_size_mb:.2f} MB\n\n"
            f"{used_mode_text}",
            quote=True
        )
        track_message_for_cleanup(user_id, fin_msg.id)
        
    except Exception as e:
        message.reply_text(f"❌ خطأ: `{str(e)[:150]}`", quote=True)
    finally:
        if temp_compressed_filename and os.path.exists(temp_compressed_filename): os.remove(temp_compressed_filename)
        if video_data.get('auto_compress_status_message_id'):
            try: app.delete_messages(message.chat.id, video_data['auto_compress_status_message_id'])
            except: pass
        if button_message_id and button_message_id in user_video_data:
            user_video_data[button_message_id]['processing_started'] = False

        check_and_prompt_album(user_id, app, message.chat.id)

# -------------------------- أوامر ورسائل التيليجرام --------------------------

app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

@app.on_message(filters.command("start"))
def start_command(client, message):
    message.reply_text("👋 أهلاً! أرسل الفيديوهات وسأقوم بتجميعها لك في النهاية.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]]), quote=True)

@app.on_message(filters.command("settings"))
def settings_command(client, message):
    send_settings_menu(client, message.chat.id, message.from_user.id)

@app.on_message(filters.text)
def handle_text_inputs(client, message):
    user_id = message.from_user.id
    if user_id not in user_states: return
    st = user_states[user_id]
    state, bid = st.get("state"), st.get("button_message_id")
    track_message_for_cleanup(user_id, message.id)

    if state == "waiting_for_target_size" and bid in user_video_data:
        try:
            video_data = user_video_data[bid]
            video_data['quality'] = {"target_size": float(message.text)}
            compression_executor.submit(process_video_for_compression, video_data)
            del user_states[user_id]
        except: message.reply_text("❌ أرسل رقماً.")

    elif state == "waiting_for_percentage" and bid in user_video_data:
        try:
            video_data = user_video_data[bid]
            video_data['quality'] = {"target_size": (float(message.text)/100) * (os.path.getsize(video_data['file']) / (1024*1024))}
            compression_executor.submit(process_video_for_compression, video_data)
            del user_states[user_id]
        except: message.reply_text("❌ أرسل بين 1-100.")

    elif state == "waiting_for_cq_value":
        try: get_user_settings(user_id)['auto_quality_value'] = int(message.text); send_settings_menu(client, message.chat.id, user_id)
        finally: user_states.pop(user_id, None)

    elif state == "waiting_for_auto_percentage":
        try: get_user_settings(user_id)['auto_percent_value'] = float(message.text); send_settings_menu(client, message.chat.id, user_id)
        finally: user_states.pop(user_id, None)

def send_settings_menu(client, chat_id, user_id, message_id=None):
    s = get_user_settings(user_id)
    auto_send = "تلقائي (عند الانتهاء/10)" if s['auto_send_album'] else "زر يدوي"
    text = (f"**⚙️ الإعدادات:**\n🔹 الترميز: `{s['encoder']}`\n🔸 التلقائي: {'✅' if s['auto_compress'] else '❌'}\n"
            f"📊 وضع التلقائي: {'(نسبة %)' if s.get('auto_mode')=='percent' else '(CRF)'}\n"
            f"📦 إرسال الألبوم: `{auto_send}`")
    kb = [
        [InlineKeyboardButton("🔄 المحرك", callback_data="settings_encoder")],
        [InlineKeyboardButton("تغيير نمط التلقائي", callback_data="settings_toggle_mode"), InlineKeyboardButton("تفعيل התلقائي", callback_data="settings_toggle_auto")],
        [InlineKeyboardButton("✏️ CRF للتلقائي", callback_data="settings_custom_quality"), InlineKeyboardButton("📉 النسبة(%) للتلقائي", callback_data="settings_custom_percent")],
        [InlineKeyboardButton("📦 نظام الألبوم", callback_data="settings_toggle_send")],
        [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]
    ]
    if message_id:
        try: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(kb))
        except: pass
    else: client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(kb))

def post_download_actions(original_message_id):
    if original_message_id not in user_video_data: return
    vd = user_video_data[original_message_id]
    message, user_id, download_msg = vd['message'], vd['user_id'], vd['download_msg']

    try:
        vd['file'] = vd['download_future'].result()
        try: download_msg.delete()
        except: pass
        
        s = get_user_settings(user_id)
        if s['auto_compress']:
            if s.get('auto_mode') == 'percent':
                pct = s.get('auto_percent_value', 50)
                vd['quality'] = {"target_size": (pct/100) * (os.path.getsize(vd['file']) / (1024*1024))}
            else: vd['quality'] = s['auto_quality_value']
            
            st_msg = message.reply_text("🚀 تلقائي: جاري الضغط...")
            vd['auto_compress_status_message_id'] = st_msg.id
            compression_executor.submit(process_video_for_compression, vd)
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("ضعيفة (27)", callback_data="crf_27"), InlineKeyboardButton("متوسط (23)", callback_data="crf_23")],
                [InlineKeyboardButton("🎯 حجم (MB)", callback_data="target_size_prompt"), InlineKeyboardButton("📉 نسبة (%)", callback_data="target_percent_prompt")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_compression")]
            ])
            rep = message.reply_text("✅ استُلم. اختر نمط الضغط:", reply_markup=kb, quote=True)
            track_message_for_cleanup(user_id, rep.id)
            vd['button_message_id'] = rep.id
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
    with task_lock: user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
    
    f_id, f_sz, f_name = (message.video.file_id, message.video.file_size, f"{user_id}_{message.id}.mp4") if message.video else (message.animation.file_id, message.animation.file_size, f"{user_id}_{message.id}.mp4")
    
    down_msg = message.reply_text("📥 في الطابور...", quote=True)
    track_message_for_cleanup(user_id, down_msg.id)
    
    down_future = download_executor.submit(client.download_media, message=f_id, file_name=os.path.join(DOWNLOADS_DIR, f_name), progress=update_progress_msg, progress_args=(client, down_msg, "📥 جاري التنزيل...", time.time(), f_sz))

    user_video_data[message.id] = {'message': message, 'download_msg': down_msg, 'download_future': down_future, 'file': None, 'user_id': user_id}    
    threading.Thread(target=post_download_actions, args=[message.id]).start()

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
        callback_query.answer("🗑 تم تنظيف.")
        try: message.delete()
        except: pass
        return

    if data.startswith("settings"):
        if data == "settings": send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_encoder":
            keyboard = [[InlineKeyboardButton("H.265", callback_data="set_encoder:hevc_nvenc"), InlineKeyboardButton("H.264(NV)", callback_data="set_encoder:h264_nvenc")],
                        [InlineKeyboardButton("H.264(CPU)", callback_data="set_encoder:libx264"), InlineKeyboardButton("رجوع", callback_data="settings")]]
            message.edit_text("إختر المحرك:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "settings_custom_quality": user_states[user_id] = {"state": "waiting_for_cq_value"}; message.edit_text("أرسل قيمة CRF.")
        elif data == "settings_custom_percent": user_states[user_id] = {"state": "waiting_for_auto_percentage"}; message.edit_text("أرسل نسبة (1-100).")
        elif data == "settings_toggle_mode": get_user_settings(user_id)['auto_mode'] = 'percent' if get_user_settings(user_id).get('auto_mode') == 'crf' else 'crf'; send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_toggle_auto": get_user_settings(user_id)['auto_compress'] = not get_user_settings(user_id)['auto_compress']; send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_toggle_send": get_user_settings(user_id)['auto_send_album'] = not get_user_settings(user_id)['auto_send_album']; send_settings_menu(client, message.chat.id, user_id, message.id)
        callback_query.answer()
        return

    elif data.startswith("set_encoder:"): get_user_settings(user_id)['encoder'] = data.split(":")[1]; send_settings_menu(client, message.chat.id, user_id, message.id); return
    elif data == "close_settings":
        try: message.delete()
        except: pass; return

    bid = message.id
    if bid not in user_video_data: callback_query.answer("تم التعامل مع هذا الملف.", show_alert=True); return
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
        p = message.reply_text("🔢 أرسل النسبة (100).", quote=True)
        user_states[user_id] = {"state": "waiting_for_percentage", "button_message_id": bid}
        track_message_for_cleanup(user_id, p.id)
        return

    if video_data.get('timer'): video_data['timer'].cancel()
    video_data['quality'] = data
    compression_executor.submit(process_video_for_compression, video_data)

if __name__ == "__main__":
    app.run()
