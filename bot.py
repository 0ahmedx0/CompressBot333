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
    # إخبار المستخدم أننا بدأنا تنزيل الفيديو
    progress_msg = message.reply_text("Downloading video... Please wait.")
    
    # تحميل الفيديو من المرسل وحساب السرعة المتوقعة
    start_time = time.time()
    file = client.download_media(message.video.file_id if message.video else message.animation.file_id, progress=download_progress, progress_args=(progress_msg, start_time))
    
    # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_filename = temp_file.name
    
    # إذا كان الفيديو من نوع Animation نحتاج لتحويله إلى فيديو
    if message.animation:
        subprocess.run(f'ffmpeg -y -i "{file}" "{temp_filename}"', shell=True, check=True)
    
    # إخبار المستخدم بأننا بدأنا ضغط الفيديو
    progress_msg.edit_text("Compressing video... Please wait.")

    
    # تنفيذ عملية ضغط الفيديو باستخدام ffmpeg
    subprocess.run(
        f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v h264_nvenc -pix_fmt {VIDEO_PIXEL_FORMAT} '
        f'-b:v 500k -crf 28 -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
        f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v {VIDEO_PROFILE} '
        f'-map_metadata -1 "{temp_filename}"',
        shell=True, check=True
    )
    
    # إرسال الفيديو المضغوط للمستخدم وحساب الوقت المتبقي للرفع
    upload_start_time = time.time()
    upload_file(client, message, temp_filename, progress_msg, upload_start_time)
    
    # حذف الملفات المؤقتة بعد إرسالها
    os.remove(file)
    os.remove(temp_filename)

    # إخبار المستخدم أن العملية انتهت
    progress_msg.edit_text("Video processed and sent successfully!")

def download_progress(current, total, message, start_time):
    """حساب سرعة التنزيل والوقت المتبقي"""
    elapsed_time = time.time() - start_time
    download_speed = current / elapsed_time if elapsed_time > 0 else 0
    remaining_time = (total - current) / download_speed if download_speed > 0 else 0
    
    # تحديث حالة التنزيل للمستخدم كل 3 ثوانٍ
    progress_text = (
        f"Downloading... {current / total * 100:.2f}%\n"
        f"Speed: {download_speed / 1024:.2f} KB/s\n"
        f"Time remaining: {remaining_time:.2f} seconds"
    )
    message.edit_text(progress_text)

def upload_file(client, message, file_path, progress_msg, start_time):
    """رفع الفيديو وإظهار حالة الرفع"""
    with open(file_path, 'rb') as f:
        total_size = os.path.getsize(file_path)
        start_time = time.time()
        
        def upload_progress(current, total):
            elapsed_time = time.time() - start_time
            upload_speed = current / elapsed_time if elapsed_time > 0 else 0
            remaining_time = (total - current) / upload_speed if upload_speed > 0 else 0
            
            # تحديث حالة الرفع للمستخدم
            progress_text = (
                f"Uploading... {current / total * 100:.2f}%\n"
                f"Speed: {upload_speed / 1024:.2f} KB/s\n"
                f"Time remaining: {remaining_time:.2f} seconds"
            )
            progress_msg.edit_text(progress_text)
        
        # رفع الفيديو
        client.send_video(message.chat.id, file_path, progress=upload_progress)

app.run()
