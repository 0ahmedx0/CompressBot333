import os
import tempfile
import subprocess
import time
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery
from config import *

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

@app.on_message(filters.command("start"))
def start(client, message):
    message.reply_text("Send me a video and I will compress it for you.")

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    # تحميل الفيديو من المرسل
    file = client.download_media(message.video.file_id if message.video else message.animation.file_id)
    
    # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_filename = temp_file.name
    
    # إذا كان الفيديو من نوع Animation نحتاج لتحويله إلى فيديو
    if message.animation:
        subprocess.run(f'ffmpeg -y -i "{file}" "{temp_filename}"', shell=True, check=True)
    
    # حساب سرعة التحميل والوقت المتبقي أثناء التحميل
    total_size = os.path.getsize(file)
    start_time = time.time()
    last_time = start_time
    last_received = 0
    
    # تحديث حالة التنزيل للمستخدم كل 3 ثوانٍ
    def update_download_progress():
        nonlocal last_time, last_received
        
        current_time = time.time()
        elapsed_time = current_time - last_time
        if elapsed_time >= 3:  # التحديث كل 3 ثوانٍ
            current_received = os.path.getsize(file)
            download_speed = (current_received - last_received) / elapsed_time  # سرعة التنزيل بالبايت
            remaining_time = (total_size - current_received) / download_speed if download_speed else 0
            progress_text = (
                f"Downloading... {current_received / total_size * 100:.2f}%\n"
                f"Speed: {download_speed / 1024:.2f} KB/s\n"
                f"Time remaining: {remaining_time:.2f} seconds"
            )
            message.reply_text(progress_text, quote=True, disable_web_page_preview=True)
            last_time = current_time
            last_received = current_received
    
    # تنفيذ عملية التحميل واستخدام الدالة للتحديث
    while os.path.getsize(file) < total_size:
        update_download_progress()
    
    # ضغط الفيديو
    subprocess.run(
        f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v h264_nvenc -pix_fmt {VIDEO_PIXEL_FORMAT} '
        f'-b:v 500k -crf 28 -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
        f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v {VIDEO_PROFILE} '
        f'-map_metadata -1 "{temp_filename}"',
        shell=True, check=True
    )
    
    # حساب سرعة الرفع والوقت المتبقي أثناء الرفع
    total_size = os.path.getsize(temp_filename)
    start_time = time.time()
    last_time = start_time
    last_sent = 0
    
    # تحديث حالة الرفع للمستخدم
    def update_upload_progress():
        nonlocal last_time, last_sent
        
        current_time = time.time()
        elapsed_time = current_time - last_time
        if elapsed_time >= 3:  # التحديث كل 3 ثوانٍ
            current_sent = os.path.getsize(temp_filename)
            upload_speed = (current_sent - last_sent) / elapsed_time  # سرعة الرفع بالبايت
            remaining_time = (total_size - current_sent) / upload_speed if upload_speed else 0
            progress_text = (
                f"Uploading... {current_sent / total_size * 100:.2f}%\n"
                f"Speed: {upload_speed / 1024:.2f} KB/s\n"
                f"Time remaining: {remaining_time:.2f} seconds"
            )
            message.reply_text(progress_text, quote=True, disable_web_page_preview=True)
            last_time = current_time
            last_sent = current_sent
    
    # إرسال الفيديو المضغوط للمستخدم
    message.reply_video(temp_filename, progress=update_upload_progress)
    
    # حذف الملفات المؤقتة بعد إرسالها
    os.remove(file)
    os.remove(temp_filename)

app.run()
