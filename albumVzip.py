import os
import tempfile
import subprocess
import threading
import time
import re
import json
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
from pyrogram.errors import MessageEmpty, UserNotParticipant, MessageNotModified, FloodWait

from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# ضبط المزامنة المتعددة: 3 تحميلات متزامنة و 3 عمليات ضغط متزامنة
download_executor = ThreadPoolExecutor(max_workers=3)
compression_executor = ThreadPoolExecutor(max_workers=3)

# قواميس التخزين
user_states = {}
user_settings = {}
user_video_data = {}
PROGRESS_TRACKER = {} 

# تتبع المهام للخلفية وميزة الألبوم
user_active_tasks = {}    # عداد المهام النشطة لكل مستخدم
user_finished_paths = {}  # قائمة المسارات المنتهية لإرسالها كألبوم
album_lock = threading.Lock()

DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_mode': 'crf',           # النمط: crf أو percent
    'auto_quality_value': 30,
    'auto_percent_value': 50      # النسبة الافتراضية 50%
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

# -------------------------- وظائف المساعدة وحساب الحجم والتقدم --------------------------

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
        except Exception:
            pass

# -------------------------- محرك الضغط الرئيسي --------------------------

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

    if button_message_id and button_message_id in user_video_data:
        user_video_data[button_message_id]['processing_started'] = True
        try:
            status_text = f"⏳ جاري الضغط في الخلفية..."
            app.edit_message_reply_markup(message.chat.id, button_message_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(status_text, callback_data="none")]]))
        except: pass

    temp_compressed_filename = None
    thumb_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        if isinstance(quality, dict) and 'target_size' in quality:
            target_size_mb = quality['target_size']
            target_v_bitrate = calculate_target_bitrate(target_size_mb, total_duration)
            quality_settings = f"-b:v {target_v_bitrate}k -maxrate {target_v_bitrate}k -bufsize {target_v_bitrate*2}k -preset fast"
            used_mode_text = f"🎯 طلب حجم: ~{target_size_mb:.2f} MB"
        else:
            quality_value = int(quality.split('_')[1]) if 'crf_' in str(quality) else int(quality)
            quality_param = "cq" if "nvenc" in encoder else "crf"
            quality_settings = f"-{quality_param} {quality_value} -preset fast"
            used_mode_text = f"🎥 جودة CRF: {quality_value}"
        
        ffmpeg_command = f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt yuv420p -c:a aac -b:a 128k {quality_settings} -movflags +faststart "{temp_compressed_filename}"'

        progress_msg = message.reply_text("🔄 **بدأ ضغط الملف في طابور الخلفية...**", quote=True)
        start_time = time.time()

        process = subprocess.Popen(ffmpeg_command, shell=True, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')
        for line in process.stderr:
            time_match = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d+)", line)
            if time_match and total_duration > 0:
                current_time_sec = time_to_seconds(time_match.group(1))
                update_progress_msg(current_time_sec, total_duration, app, progress_msg, "⚙️ **جاري الضغط...**", start_time)
        process.wait()
        try: progress_msg.delete()
        except: pass

        # ميزة الألبوم: حفظ المسار
        with album_lock:
            if user_id not in user_finished_paths: user_finished_paths[user_id] = []
            final_p = os.path.join(DOWNLOADS_DIR, f"final_{int(time.time())}_{os.path.basename(temp_compressed_filename)}")
            os.rename(temp_compressed_filename, final_p)
            user_finished_paths[user_id].append(final_p)

        thumb_path, vid_duration, vid_width, vid_height = get_video_info_and_thumb(final_p)
        up_msg = message.reply_text("📤 جاري الرفع الفردي الآن...", quote=True)
        message.reply_video(
            video=final_p, duration=int(vid_duration), width=vid_width, height=vid_height, thumb=thumb_path,
            progress=update_progress_msg, progress_args=(app, up_msg, "📤 **الرفع الفردي...**", time.time()),
            caption=f"✅ **اكتمل!**\n\n🔻 الحجم الأصلي: {os.path.getsize(file_path)/(1024*1024):.2f} MB\n✅ الحجم الجديد: {os.path.getsize(final_p)/(1024*1024):.2f} MB\n\n{used_mode_text}",
            supports_streaming=True 
        )
        try: up_msg.delete()
        except: pass
        
    except Exception as e: message.reply_text(f"❌ خطأ: {e}")
    finally:
        with album_lock:
            user_active_tasks[user_id] = user_active_tasks.get(user_id, 1) - 1
            if user_active_tasks[user_id] == 0:
                # إذا انتهت جميع المهام، اعرض خيار الألبوم
                if len(user_finished_paths.get(user_id, [])) > 1:
                    app.send_message(message.chat.id, f"📦 **اكتملت جميع المهام في الخلفية!**\nعدد الملفات: {len(user_finished_paths[user_id])}\nهل تريد إرسالها كألبوم؟",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 إرسال كألبوم", callback_data="album_send"), InlineKeyboardButton("🗑️ مسح القائمة", callback_data="album_clear")]]))
        if os.path.exists(file_path): os.remove(file_path)

# -------------------------- التعامل مع المهام المتزامنة --------------------------

def download_task_handler(original_message_id):
    vd = user_video_data[original_message_id]
    msg, client, uid = vd['message'], app, vd['user_id']
    f_id = msg.video.file_id if msg.video else msg.animation.file_id
    f_size = msg.video.file_size if msg.video else msg.animation.file_size
    path = os.path.join(DOWNLOADS_DIR, f"down_{uid}_{msg.id}.mp4")

    download_msg = msg.reply_text("📥 في طابور التحميل المتزامن...", quote=True)
    try:
        final_path = client.download_media(f_id, file_name=path, progress=update_progress_msg, progress_args=(client, download_msg, "📥 **جاري التحميل...**", time.time(), f_size))
        vd['file'] = final_path
        try: download_msg.delete()
        except: pass
        
        s = get_user_settings(uid)
        if s['auto_compress']:
            if s['auto_mode'] == 'percent':
                mb_orig = os.path.getsize(final_path) / (1024 * 1024)
                vd['quality'] = {"target_size": (s['auto_percent_value'] / 100) * mb_orig}
            else: vd['quality'] = s['auto_quality_value']
            compression_executor.submit(process_video_for_compression, vd)
        else:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("ضعيفة (27)", callback_data="crf_27"), InlineKeyboardButton("متوسطة (23)", callback_data="crf_23"), InlineKeyboardButton("عالية (18)", callback_data="crf_18")],
                [InlineKeyboardButton("🎯 حجم (MB)", callback_data="set_mb_man"), InlineKeyboardButton("📉 نسبة (%)", callback_data="set_pct_man")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_c")]
            ])
            rep = msg.reply_text("✅ استُلم الملف. اختر الطريقة:", reply_markup=markup, quote=True)
            vd['button_message_id'] = rep.id
            user_video_data[rep.id] = user_video_data.pop(original_message_id)
    except Exception as e:
        msg.reply_text(f"❌ فشل: {e}")
        with album_lock: user_active_tasks[uid] -= 1

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    uid = message.from_user.id
    with album_lock: user_active_tasks[uid] = user_active_tasks.get(uid, 0) + 1
    user_video_data[message.id] = {'message': message, 'user_id': uid, 'processing_started': False}
    download_executor.submit(download_task_handler, message.id)

# -------------------------- معالجات النصوص والإعدادات --------------------------

@app.on_message(filters.command("start"))
def start_cmd(c, m):
    m.reply_text("أهلاً بك! أرسل فيديوهات (3 متزامنة) وسيتم معالجتها.\nاستخدم /settings للضبط.", 
                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]]))

@app.on_message(filters.text)
def text_input(c, m):
    uid = m.from_user.id
    if uid not in user_states: return
    state_d = user_states[uid]
    state, bid = state_d.get("state"), state_d.get("button_message_id")

    if state == "in_pct" and bid in user_video_data:
        try:
            val = float(m.text)
            vd = user_video_data[bid]
            vd['quality'] = {"target_size": (val/100)*(os.path.getsize(vd['file'])/(1024*1024))}
            compression_executor.submit(process_video_for_compression, vd)
            del user_states[uid]
        except: m.reply_text("أرسل رقماً صحيحاً.")
    elif state == "in_mb" and bid in user_video_data:
        try:
            user_video_data[bid]['quality'] = {"target_size": float(m.text)}
            compression_executor.submit(process_video_for_compression, user_video_data[bid])
            del user_states[uid]
        except: m.reply_text("أرسل رقم الميجا.")
    elif state == "set_auto_pct":
        try:
            get_user_settings(uid)['auto_percent_value'] = float(m.text)
            m.reply_text("✅ تم حفظ نسبة التلقائي.")
            del user_states[uid]
            send_settings_menu(c, m.chat.id, uid)
        except: pass

@app.on_callback_query()
def cb_handler(c, q):
    d, uid, msg = q.data, q.from_user.id, q.message
    s = get_user_settings(uid)

    if d == "album_send":
        with album_lock:
            files = user_finished_paths.get(uid, [])
            if not files: return
            media = [InputMediaVideo(f) for f in files[:10]]
            app.send_media_group(msg.chat.id, media)
            for f in files: (os.remove(f) if os.path.exists(f) else None)
            user_finished_paths[uid] = []
            msg.delete()
    elif d == "album_clear":
        with album_lock:
            for f in user_finished_paths.get(uid, []): (os.remove(f) if os.path.exists(f) else None)
            user_finished_paths[uid] = []
            msg.delete()
    elif d == "settings": send_settings_menu(c, msg.chat.id, uid, msg.id)
    elif d == "toggle_auto_status": s['auto_compress'] = not s['auto_compress']; send_settings_menu(c, msg.chat.id, uid, msg.id)
    elif d == "toggle_mode": s['auto_mode'] = 'percent' if s['auto_mode'] == 'crf' else 'crf'; send_settings_menu(c, msg.chat.id, uid, msg.id)
    elif d == "set_auto_pct_cb": user_states[uid] = {"state": "set_auto_pct"}; msg.reply_text("أرسل النسبة (1-100)% للتلقائي:")
    elif d == "settings_encoder":
        kb = [[InlineKeyboardButton("H.265 NVENC", callback_data="enc:hevc_nvenc")], [InlineKeyboardButton("H.264 NVENC", callback_data="enc:h264_nvenc")], [InlineKeyboardButton("CPU", callback_data="enc:libx264")], [InlineKeyboardButton("رجوع", callback_data="settings")]]
        msg.edit_text("المحرك:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("enc:"): s['encoder'] = d.split(":")[1]; send_settings_menu(c, msg.chat.id, uid, msg.id)
    elif msg.id in user_video_data:
        vd = user_video_data[msg.id]
        if d == "set_pct_man": user_states[uid] = {"state": "in_pct", "button_message_id": msg.id}; q.answer("أرسل النسبة %")
        elif d == "set_mb_man": user_states[uid] = {"state": "in_mb", "button_message_id": msg.id}; q.answer("أرسل الحجم MB")
        elif d.startswith("crf_"): vd['quality'] = d; compression_executor.submit(process_video_for_compression, vd)
        elif d == "cancel_c": (os.remove(vd['file']) if vd['file'] else None); msg.delete()
    q.answer()

def send_settings_menu(client, chat_id, user_id, message_id=None):
    s = get_user_settings(user_id)
    auto_s = "✅ مفعل" if s['auto_compress'] else "❌ معطل"
    mode_s = "نسبة مئوية %" if s['auto_mode'] == 'percent' else "جودة CRF"
    text = f"**⚙️ الإعدادات:**\n🔹 الترميز: `{s['encoder']}`\n🔸 التلقائي: {auto_s}\n📊 النمط: {mode_s}\n📈 القيمة التلقائية: `{s['auto_percent_value']}%`"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تغيير الترميز", callback_data="settings_encoder")], [InlineKeyboardButton(f"وضع التلقائي: {mode_s}", callback_data="toggle_mode")], [InlineKeyboardButton("📉 ضبط % للتلقائي", callback_data="set_auto_pct_cb")], [InlineKeyboardButton(f"الضغط التلقائي: {auto_s}", callback_data="toggle_auto_status")], [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]])
    if message_id: client.edit_message_text(chat_id, message_id, text, reply_markup=kb)
    else: client.send_message(chat_id, text, reply_markup=kb)

app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)
if __name__ == "__main__":
    cleanup_downloads()
    app.run()
