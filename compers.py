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
PROGRESS_TRACKER = {} 

# تحديث الإعدادات الافتراضية لدعم النسبة المئوية التلقائية
DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_mode': 'percentage', # الخيار الجديد: 'percentage' أو 'quality'
    'auto_quality_value': 23,
    'auto_percent_value': 50   # النسبة الافتراضية 50%
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

# -------------------------- وظائف المساعدة والتقدم --------------------------

def update_progress_msg(current, total, client, message, action, start_time, known_size=0):
    now = time.time()
    msg_id = message.id
    if total <= 0 and known_size > 0: total = known_size
    is_finished = (current >= total) if total > 0 else False
    if msg_id in PROGRESS_TRACKER and (now - PROGRESS_TRACKER[msg_id]) < 5.0 and not is_finished: return
    PROGRESS_TRACKER[msg_id] = now
    percent = (current * 100 / total) if total > 0 else 0
    filled = int(percent / 10)
    bar = f"[{'█' * filled}{'░' * (10 - filled)}]"
    if "ضغط" in action:
        curr_val, total_val = f"{current:.1f}s", f"{total:.1f}s"
    else: 
        curr_val, total_val = f"{current/(1024*1024):.2f}MB", f"{total/(1024*1024):.2f}MB"
    
    elapsed = now - start_time
    speed_text = ""
    if elapsed > 0 and "ضغط" not in action:
        speed_mb = (current / elapsed) / (1024 * 1024)
        speed_text = f"🚀 **السرعة:** `{speed_mb:.2f} MB/s`\n"

    text = f"{action}\n{bar} `{percent:.1f}%`\n📊 **التقدم:** `{curr_val} / {total_val}`\n{speed_text}"
    try: client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except: pass
        
def get_video_info_and_thumb(file_path):
    duration, width, height, thumb_path = 0.0, 0, 0, None
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_streams "{file_path}"'
        data = json.loads(subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout)
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                width, height, duration = int(stream.get('width')), int(stream.get('height')), float(stream.get('duration'))
                break
        thumb_path = file_path + "_thumb.jpg"
        subprocess.run(f'ffmpeg -y -ss {min(1.0, duration*0.1)} -i "{file_path}" -vframes 1 -vf "scale=320:-1" "{thumb_path}" -loglevel quiet', shell=True)
    except: pass
    return thumb_path, duration, width, height

def time_to_seconds(time_str):
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except: return 0

def calculate_target_bitrate(target_size_mb, duration_seconds, audio_bitrate_kbps=128):
    if duration_seconds <= 0: return 500
    total_bitrate_kbps = (target_size_mb * 8192) / duration_seconds
    return max(50, int(total_bitrate_kbps - audio_bitrate_kbps))

def cleanup_downloads():
    for f in os.listdir(DOWNLOADS_DIR):
        try: os.remove(os.path.join(DOWNLOADS_DIR, f))
        except: pass

# -------------------------- محرك الضغط الأساسي --------------------------

def process_video_for_compression(video_data):
    file_path, message = video_data['file'], video_data['message']
    quality, user_id = video_data['quality'], video_data['user_id']
    button_msg_id = video_data.get('button_message_id')
    encoder = get_user_settings(user_id)['encoder']
    
    # تحديد مدة الفيديو بدقة للـ Bitrate
    total_duration = get_telegram_duration(message)
    if total_duration <= 0:
        total_duration = float(subprocess.run(f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{file_path}"', shell=True, capture_output=True, text=True).stdout or 0)

    if button_msg_id:
        user_video_data[button_msg_id]['processing_started'] = True
        try: app.edit_message_reply_markup(message.chat.id, button_msg_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ جاري المعالجة...", callback_data="none")]]))
        except: pass

    temp_out = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as tf: temp_out = tf.name

        # حالة الحجم المستهدف (سواء MB يدوي أو محسوب من نسبة تلقائية)
        if isinstance(quality, dict) and 'target_size' in quality:
            target_mb = quality['target_size']
            target_v_bitrate = calculate_target_bitrate(target_mb, total_duration)
            q_args = f"-b:v {target_v_bitrate}k -maxrate {target_v_bitrate}k -bufsize {target_v_bitrate*2}k -preset fast"
            mode_text = f"🎯 الهدف: {target_mb:.2f} MB"
        else:
            q_val = int(quality.split('_')[1]) if 'crf_' in str(quality) else int(quality)
            q_param = "cq" if "nvenc" in encoder else "crf"
            q_args = f"-{q_param} {q_val} -preset fast"
            mode_text = f"🎥 جودة CRF: {q_val}"

        cmd = f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt yuv420p -c:a aac -b:a 128k {q_args} -movflags +faststart "{temp_out}"'
        
        prog = message.reply_text("🔄 **جاري الضغط الآن...**", quote=True)
        start = time.time()
        process = subprocess.Popen(cmd, shell=True, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')
        
        for line in process.stderr:
            tm = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d+)", line)
            if tm: update_progress_msg(time_to_seconds(tm.group(1)), total_duration, app, prog, "⚙️ **معالجة FFmpeg...**", start)
        process.wait()
        try: prog.delete()
        except: pass

        # الرفع النهائي
        thumb, dur, w, h = get_video_info_and_thumb(temp_out)
        up_prog = message.reply_text("📤 **جاري الرفع...**", quote=True)
        message.reply_video(
            video=temp_out, duration=int(dur), width=w, height=h, thumb=thumb, supports_streaming=True,
            progress=update_progress_msg, progress_args=(app, up_prog, "📤 **رفع الفيديو...**", time.time()),
            caption=f"📦 **اكتمل الضغط**\n🔻 قبل: {os.path.getsize(file_path)/(1024*1024):.2f} MB\n✅ بعد: {os.path.getsize(temp_out)/(1024*1024):.2f} MB\n{mode_text}"
        )
        try: up_prog.delete()
        except: pass
        
    except Exception as e: message.reply_text(f"❌ خطأ: {e}")
    finally:
        if temp_out and os.path.exists(temp_out): os.remove(temp_out)
        if button_msg_id in user_video_data: 
            user_video_data[button_msg_id]['processing_started'] = False
            # زر الإنهاء التلقائي بعد النجاح
            try: app.edit_message_text(message.chat.id, button_msg_id, "✅ اكتملت العملية. هل تريد شيئاً آخر؟", 
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ حذف الأصلي والإنهاء", callback_data="finish_process")]]))
            except: pass

# -------------------------- التعامل مع الرسائل --------------------------

app = Client("vid_compressor", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

@app.on_message(filters.command("start"))
def start(c, m):
    m.reply_text("أهلاً بك! أرسل الفيديو وسأقوم بضغطه تلقائياً أو يدوياً.\nاستخدم /settings لضبط النسبة المئوية التلقائية.",
                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]]))

@app.on_message(filters.command("settings"))
def settings_cmd(c, m):
    send_settings_menu(c, m.chat.id, m.from_user.id)

def send_settings_menu(client, chat_id, user_id, message_id=None):
    s = get_user_settings(user_id)
    mode_display = "نسبة مئوية %" if s['auto_mode'] == 'percentage' else "جودة CRF"
    auto_status = "✅ مفعّل" if s['auto_compress'] else "❌ معطّل"
    
    text = (
        "**⚙️ إعدادات الضغط التلقائي:**\n\n"
        f"🔸 الحالة: **{auto_status}**\n"
        f"🔸 نوع الضغط التلقائي: **{mode_display}**\n"
        f"🔸 النسبة المحفوظة: **{s['auto_percent_value']}%**\n"
        f"🔸 جودة CRF المحفوظة: **{s['auto_quality_value']}**\n"
        f"🔸 المُسرع الحالي: `{s['encoder']}`"
    )
    kb = [
        [InlineKeyboardButton(f"تغيير الوضع التلقائي لـ: {'CRF' if s['auto_mode']=='percentage' else 'نسبة %'}", callback_data="toggle_auto_mode")],
        [InlineKeyboardButton("✏️ ضبط (النسبة %) التلقائية", callback_data="set_auto_pct"),
         InlineKeyboardButton("✏️ ضبط (الـ CRF) التلقائي", callback_data="set_auto_crf")],
        [InlineKeyboardButton(f"الضغط التلقائي: {auto_status}", callback_data="toggle_auto_status")],
        [InlineKeyboardButton("🔄 تغيير المُسرع", callback_data="settings_encoder")],
        [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]
    ]
    if message_id: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(kb))
    else: client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(kb))

@app.on_message(filters.text)
def handle_text(c, m):
    uid = m.from_user.id
    if uid not in user_states: return
    state_data = user_states[uid]
    state = state_data.get("state")

    # 1. إدخال النسبة المئوية للإعدادات (التلقائية)
    if state == "waiting_for_auto_pct_value":
        try:
            val = float(m.text)
            if 1 <= val <= 100:
                get_user_settings(uid)['auto_percent_value'] = val
                m.reply_text(f"✅ تم حفظ النسبة التلقائية: {val}%")
                user_states.pop(uid)
                send_settings_menu(c, m.chat.id, uid)
            else: m.reply_text("أرسل رقم من 1 لـ 100.")
        except: m.reply_text("أرسل رقماً صحيحاً.")

    # 2. إدخال نسبة مئوية لفيديو محدد (يدوي)
    elif state == "waiting_for_manual_pct":
        try:
            pct = float(m.text)
            bid = state_data['button_message_id']
            if bid in user_video_data:
                vd = user_video_data[bid]
                orig_mb = os.path.getsize(vd['file']) / (1024*1024)
                target = (pct/100) * orig_mb
                vd['quality'] = {"target_size": target}
                compression_executor.submit(process_video_for_compression, vd)
                user_states.pop(uid)
        except: m.reply_text("خطأ في الرقم.")

    # 3. إدخال حجم MB محدد (يدوي)
    elif state == "waiting_for_target_mb":
        try:
            mb = float(m.text)
            bid = state_data['button_message_id']
            if bid in user_video_data:
                user_video_data[bid]['quality'] = {"target_size": mb}
                compression_executor.submit(process_video_for_compression, user_video_data[bid])
                user_states.pop(uid)
        except: m.reply_text("أرسل رقم الميجا بايت.")

@app.on_message(filters.video | filters.animation)
def handle_video(c, m):
    d_msg = m.reply_text("📥 جاري التحميل...", quote=True)
    f_path = c.download_media(m, progress=update_progress_msg, progress_args=(c, d_msg, "📥 تحميل...", time.time()))
    try: d_msg.delete()
    except: pass

    uid = m.from_user.id
    s = get_user_settings(uid)
    
    # تحضير بيانات الفيديو
    video_entry = {'message': m, 'file': f_path, 'user_id': uid, 'processing_started': False}
    
    # فحص الضغط التلقائي
    if s['auto_compress']:
        if s['auto_mode'] == 'percentage':
            orig_size = os.path.getsize(f_path) / (1024*1024)
            target = (s['auto_percent_value'] / 100) * orig_size
            video_entry['quality'] = {"target_size": target}
        else:
            video_entry['quality'] = s['auto_quality_value']
        
        m.reply_text(f"🚀 تم اكتشاف وضع الضغط التلقائي ({s['auto_mode']}). جاري التنفيذ...")
        compression_executor.submit(process_video_for_compression, video_entry)
    else:
        # الوضع اليدوي
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("CRF 27 (ضعيفة)", callback_data="crf_27"),
             InlineKeyboardButton("CRF 23 (متوسط)", callback_data="crf_23")],
            [InlineKeyboardButton("🎯 حجم (MB) محدد", callback_data="manual_mb"),
             InlineKeyboardButton("📉 نسبة (%) من الأصل", callback_data="manual_pct")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_process")]
        ])
        rep = m.reply_text("✅ تم التحميل. اختر طريقة الضغط:", reply_markup=kb, quote=True)
        video_entry['button_message_id'] = rep.id
        user_video_data[rep.id] = video_entry

@app.on_callback_query()
def cb_handler(c, q):
    data, uid, msg = q.data, q.from_user.id, q.message
    s = get_user_settings(uid)

    if data == "settings": send_settings_menu(c, msg.chat.id, uid, msg.id)
    elif data == "toggle_auto_status":
        s['auto_compress'] = not s['auto_compress']
        send_settings_menu(c, msg.chat.id, uid, msg.id)
    elif data == "toggle_auto_mode":
        s['auto_mode'] = 'quality' if s['auto_mode'] == 'percentage' else 'percentage'
        send_settings_menu(c, msg.chat.id, uid, msg.id)
    elif data == "set_auto_pct":
        user_states[uid] = {"state": "waiting_for_auto_pct_value"}
        msg.reply_text("🔢 أرسل النسبة المئوية الافتراضية (مثلاً 50):")
    elif data == "close_settings": msg.delete()
    
    # الأزرار اليدوية على الفيديو
    elif data == "manual_pct":
        user_states[uid] = {"state": "waiting_for_manual_pct", "button_message_id": msg.id}
        q.answer("أرسل النسبة المئوية %")
    elif data == "manual_mb":
        user_states[uid] = {"state": "waiting_for_target_mb", "button_message_id": msg.id}
        q.answer("أرسل الحجم بالـ MB")
    elif data.startswith("crf_"):
        if msg.id in user_video_data:
            user_video_data[msg.id]['quality'] = data
            compression_executor.submit(process_video_for_compression, user_video_data[msg.id])
    elif data == "finish_process":
        if msg.id in user_video_data:
            if os.path.exists(user_video_data[msg.id]['file']): os.remove(user_video_data[msg.id]['file'])
            del user_video_data[msg.id]
        msg.delete()
    q.answer()

def get_telegram_duration(m):
    if m.video: return m.video.duration
    if m.animation: return m.animation.duration
    return 0

if __name__ == "__main__":
    cleanup_downloads()
    app.run()
