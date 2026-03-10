import os
import tempfile
import subprocess
import threading
import time
import re
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty, UserNotParticipant
import math

# تأكد من وجود ملف config.py يحتوي على الإعدادات المطلوبة
from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

download_executor = ThreadPoolExecutor(max_workers=5)
compression_executor = ThreadPoolExecutor(max_workers=3)

# قاموس لتخزين "الحالة" الحالية للمستخدم
user_states = {}

# قاموس لتخزين إعدادات كل مستخدم
user_settings = {}
DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_quality_value': 30
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

# -------------------------- وظائف المساعدة --------------------------

def cleanup_downloads():
    print("Cleaning up downloads directory...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")
    print("Downloads directory cleaned.")

def estimate_crf_for_target_size(file_path, target_size_mb, initial_crf=23):
    original_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if original_size_mb > target_size_mb:
        ratio = original_size_mb / target_size_mb
        estimated_crf = min(51, max(18, initial_crf + int((ratio - 1) * 5)))
    else:
        ratio = target_size_mb / original_size_mb
        estimated_crf = max(0, min(23, initial_crf - int((ratio - 1) * 5)))
    return estimated_crf

def create_progress_bar(percentage):
    filled_blocks = int(percentage // 5)
    empty_blocks = 20 - filled_blocks
    bar = "█" * filled_blocks + "░" * empty_blocks
    return f"[{bar}] {percentage:.1f}%"

# -------------------------- تهيئة العميل --------------------------
app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)
user_video_data = {}

# -------------------------- وظائف المعالجة --------------------------

def process_video_for_compression(video_data):
    thread_name = threading.current_thread().name
    file_path = video_data['file']
    message = video_data['message']
    button_message_id = video_data.get('button_message_id')
    quality = video_data['quality']
    user_id = video_data['user_id']
    user_prefs = get_user_settings(user_id)
    encoder = user_prefs['encoder']

    if button_message_id and button_message_id in user_video_data:
        user_video_data[button_message_id]['processing_started'] = True
        try:
            status_text = "⏳ جاري الضغط..."
            app.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=button_message_id,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(status_text, callback_data="none")]])
            )
        except: pass

    temp_compressed_filename = None
    try:
        if not os.path.exists(file_path):
            message.reply_text("حدث خطأ: لم يتم العثور على الملف.")
            return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        quality_value = 23
        if isinstance(quality, dict) and 'target_size' in quality:
            quality_value = estimate_crf_for_target_size(file_path, quality['target_size'])
        elif isinstance(quality, str) and 'crf_' in quality:
            quality_value = int(quality.split('_')[1])
        elif isinstance(quality, int):
            quality_value = quality

        preset = "fast"
        quality_param = "cq" if "nvenc" in encoder else "crf"
        
        ffmpeg_command = (
            f'ffmpeg -y -i "{file_path}" -c:v {encoder} -{quality_param} {quality_value} '
            f'-preset {preset} -pix_fmt yuv420p -c:a aac -b:a 128k "{temp_compressed_filename}"'
        )

        progress_msg = message.reply_text("🔄 جاري الضغط... 0.0%", quote=True)
        
        process = subprocess.Popen(ffmpeg_command, shell=True, stderr=subprocess.PIPE, universal_newlines=True)
        
        # محاكاة بسيطة للتقدم (لأن تحليل FFmpeg معقد عبر الشل)
        for i in range(1, 11):
            time.sleep(1)
            try: app.edit_message_text(message.chat.id, progress_msg.id, f"🔄 جاري الضغط... {create_progress_bar(i*10)}")
            except: pass
            if process.poll() is not None: break

        process.wait()
        try: progress_msg.delete()
        except: pass

        if os.path.exists(temp_compressed_filename):
            new_size = os.path.getsize(temp_compressed_filename) / (1024 * 1024)
            message.reply_document(
                document=temp_compressed_filename,
                caption=f"✅ تم الضغط بنجاح\nالحجم: {new_size:.2f} MB\nالجودة: {quality_value}"
            )
    except Exception as e:
        message.reply_text(f"❌ خطأ: {e}")
    finally:
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            os.remove(temp_compressed_filename)
        if button_message_id in user_video_data:
            user_video_data[button_message_id]['processing_started'] = False

# -------------------------- معالجات الرسائل --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    message.reply_text("أهلاً بك! أرسل لي فيديو وسأقوم بضغطه لك.", 
                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]]))

@app.on_message(filters.command("settings"))
def settings_cmd(client, message):
    send_settings_menu(client, message.chat.id, message.from_user.id)

def send_settings_menu(client, chat_id, user_id, message_id=None):
    settings = get_user_settings(user_id)
    text = f"**⚙️ الإعدادات الحالية:**\nالترميز: {settings['encoder']}\nالضغط التلقائي: {settings['auto_compress']}"
    keyboard = [[InlineKeyboardButton("🔄 تغيير الترميز", callback_data="settings_encoder")],
                [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]]
    
    if message_id:
        client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.text & filters.private)
def handle_all_text_inputs(client, message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return

    state_data = user_states[user_id]
    state = state_data.get("state")

    # معالجة إدخال جودة مخصصة للإعدادات
    if state == "waiting_for_cq_value":
        try:
            val = int(message.text)
            if 0 <= val <= 51:
                get_user_settings(user_id)['auto_quality_value'] = val
                message.reply_text(f"✅ تم الحفظ: {val}")
                del user_states[user_id]
            else:
                message.reply_text("أدخل رقم بين 0 و 51")
        except:
            message.reply_text("أرسل رقماً صحيحاً")

    # معالجة إدخال حجم معين لضغط الفيديو
    elif state == "waiting_for_target_size":
        try:
            size = float(message.text)
            btn_id = state_data.get("button_message_id")
            if btn_id in user_video_data:
                video_data = user_video_data[btn_id]
                video_data['quality'] = {"target_size": size}
                compression_executor.submit(process_video_for_compression, video_data)
                message.reply_text(f"⏳ جاري العمل للوصول لحجم {size}MB")
                del user_states[user_id]
        except:
            message.reply_text("أرسل حجماً صحيحاً (رقم)")

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    msg = message.reply_text("📥 جاري التحميل...", quote=True)
    file_path = client.download_media(message)
    
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("متوسطة (CRF 23)", callback_data="crf_23"),
         InlineKeyboardButton("🎯 حجم محدد", callback_data="target_size_prompt")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_compression")]
    ])
    
    res = message.reply_text("✅ تم التحميل، اختر النوع:", reply_markup=markup)
    user_video_data[res.id] = {
        'message': message,
        'file': file_path,
        'user_id': message.from_user.id,
        'button_message_id': res.id,
        'processing_started': False
    }
    msg.delete()

@app.on_callback_query()
def cb_handler(client, query):
    data = query.data
    user_id = query.from_user.id
    message = query.message

    if data == "settings":
        send_settings_menu(client, message.chat.id, user_id, message.id)
    
    elif data == "target_size_prompt":
        user_states[user_id] = {
            "state": "waiting_for_target_size",
            "button_message_id": message.id
        }
        query.message.reply_text("🔢 أرسل الحجم المطلوب بالميغابايت (مثال: 20)")
        query.answer()

    elif data.startswith("crf_"):
        if message.id in user_video_data:
            video_data = user_video_data[message.id]
            video_data['quality'] = data
            compression_executor.submit(process_video_for_compression, video_data)
            query.answer("بدأ الضغط...")
        else:
            query.answer("خطأ: الملف غير موجود", show_alert=True)
            
    elif data == "close_settings":
        message.delete()

# -------------------------- التشغيل --------------------------
if __name__ == "__main__":
    cleanup_downloads()
    print("🚀 Bot Started")
    app.run()
