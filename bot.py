import os
import tempfile
import subprocess
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
    
    # ضغط الفيديو
  subprocess.run(
    f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
    f'-cq {VIDEO_CRF} -b:v {VIDEO_BITRATE} -preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} '
    f'-b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} '
    f'-profile:v {VIDEO_PROFILE} -map_metadata -1 "{temp_filename}"',
    shell=True, check=True
)

    # إرسال الفيديو المضغوط للمستخدم
    message.reply_video(temp_filename)
    
    # حذف الملفات المؤقتة بعد إرسالها
    os.remove(file)
    os.remove(temp_filename)

app.run()
