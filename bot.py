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
    
    # تحميل الفيديو من المرسل
    file = client.download_media(message.video.file_id if message.video else message.animation.file_id, progress=download_progress, progress_args=(progress_msg))
    
    # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_filename = temp_file.name
    
    # إذا كان الفيديو من نوع Animation نحتاج لتحويله إلى فيديو
    if message.animation:
        subprocess.run(f'ffmpeg -y -i "{file}" "{temp_filename}"', shell=True, check=True)
    
    # إخبار المستخدم بأننا بدأنا ضغط الفيديو
    progress_msg.edit_text("Compressing video... Please wait.")
    
    # تنفيذ عملية ضغط الفيديو باستخدام ffmpeg مع طباعة حالة التقدم
    compress_video_with_progress(file, temp_filename, progress_msg)
    
    # إرسال الفيديو المضغوط للمستخدم
    upload_file(client, message, temp_filename, progress_msg)
    
    # حذف الملفات المؤقتة بعد إرسالها
    os.remove(file)
    os.remove(temp_filename)

    # إخبار المستخدم أن العملية انتهت
    progress_msg.edit_text("Video processed and sent successfully!")

def compress_video_with_progress(input_file, output_file, progress_msg):
    """ضغط الفيديو باستخدام ffmpeg مع عرض حالة التقدم"""
    command = [
        'ffmpeg', '-y', '-i', input_file, '-r', VIDEO_FPS, '-c:v', 'h264_nvenc', '-pix_fmt', VIDEO_PIXEL_FORMAT,
        '-b:v', '500k', '-crf', '28', '-preset', 'fast', '-c:a', VIDEO_AUDIO_CODEC, '-b:a', VIDEO_AUDIO_BITRATE,
        '-ac', VIDEO_AUDIO_CHANNELS, '-ar', VIDEO_AUDIO_SAMPLE_RATE, '-profile:v', VIDEO_PROFILE,
        '-map_metadata', '-1', '-progress', 'pipe:1', output_file
    ]
    
    # تشغيل ffmpeg وحجز الإخراج لقراءة حالة التقدم
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    # معالجة الإخراج من ffmpeg لالتقاط التقدم
    while True:
        output = process.stdout.readline()
        if output == '' and process.poll() is not None:
            break
        if output:
            if 'out_time_ms=' in output:
                # استخراج الوقت المنقضي من التقدم
                time_info = get_ffmpeg_time(output)
                if time_info:
                    elapsed_time, remaining_time = time_info
                    progress_msg.edit_text(
                        f"Compressing video... Elapsed time: {elapsed_time}\nTime remaining: {remaining_time} seconds"
                    )
            time.sleep(1)

def get_ffmpeg_time(output):
    """استخراج الوقت المنقضي والوقت المتبقي من إخراج ffmpeg"""
    time_info = {}
    
    # تحديد الوقت المنقضي باستخدام `out_time_ms=` من إخراج ffmpeg
    if 'out_time_ms=' in output:
        time_ms = output.split('out_time_ms=')[1].split(' ')[0]
        elapsed_time = float(time_ms) / 1000000  # تحويل الوقت بالميلي ثانية إلى ثواني
        
        # استرجاع المدة الكلية للفيديو من إخراج ffmpeg
        total_duration = get_video_duration(output)
        remaining_time = total_duration - elapsed_time
        
        return elapsed_time, remaining_time

    return None

def get_video_duration(output):
    """الحصول على مدة الفيديو من إخراج ffmpeg"""
    duration_line = [line for line in output.split('\n') if 'duration=' in line]
    if duration_line:
        duration_str = duration_line[0].split('=')[1].strip()
        hours, minutes, seconds = map(float, duration_str.split(':'))
        total_duration = hours * 3600 + minutes * 60 + seconds
        return total_duration
    return 0

def download_progress(current, total, message):
    """تحديث حالة التنزيل للمستخدم"""
    progress_text = f"Downloading... {current / total * 100:.2f}%"
    message.edit_text(progress_text)

def upload_file(client, message, file_path, progress_msg):
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
