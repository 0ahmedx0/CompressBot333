import os
import tempfile
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import *

def progress(current, total):
    if total > 0:
        print(f"Uploading: {current / total * 100:.1f}%")
    else:
        print("Uploading...")

def download_progress(current, total):
    print(f"Download Progress - Current: {current}, Total: {total}") # Always print current and total for monitoring

    if total > 0:
        print(f"Downloading: {current / total * 100:.1f}%")
    else:
        print("Downloading...")

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

user_video_data = {}

@app.on_message(filters.command("start"))
def start(client, message):
    message.reply_text("Send me a video and I will compress it for you.")

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    file = client.download_media(
        message.video.file_id if message.video else message.animation.file_id,
        progress=download_progress
    )

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ضغط عالي", callback_data="crf_27"),
                InlineKeyboardButton("ضغط متوسط", callback_data="crf_23"),
                InlineKeyboardButton("ضغط منخفض", callback_data="crf_18"),
            ]
        ]
    )
    reply_message = message.reply_text("اختر مستوى الضغط:", reply_markup=markup, quote=True)
    user_video_data[reply_message.id] = {'file': file, 'message': message}


@app.on_callback_query()
def compression_choice(client, callback_query):
    message_id = callback_query.message.id

    if message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب. يرجى إرسال الفيديو مرة أخرى.", show_alert=True)
        return

    video_data = user_video_data.pop(message_id)
    file = video_data['file']
    message = video_data['message']

    callback_query.answer("جاري الضغط...", show_alert=False)

    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_filename = temp_file.name

    try:
        ffmpeg_command = ""
        if callback_query.data == "crf_27": # ضغط عالي
            if message.animation:
                ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
            else:
                ffmpeg_command = f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} -b:v {VIDEO_BITRATE} -crf 27 -preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v {VIDEO_PROFILE} -map_metadata -1 "{temp_filename}"'
        elif callback_query.data == "crf_23": # ضغط متوسط
            if message.animation:
                ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
            else:
                ffmpeg_command = f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} -b:v {VIDEO_BITRATE} -crf 23 -preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v {VIDEO_PROFILE} -map_metadata -1 "{temp_filename}"'
        elif callback_query.data == "crf_18": # ضغط منخفض
            if message.animation:
                ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
            else:
                ffmpeg_command = f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} -b:v {VIDEO_BITRATE} -crf 18 -preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v {VIDEO_PROFILE} -map_metadata -1 "{temp_filename}"'

        print(f"Executing FFmpeg command: {ffmpeg_command}")
        subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True)
        print("FFmpeg command executed successfully.")

        message.reply_document(temp_filename, progress=progress)

    except subprocess.CalledProcessError as e:
        print(f"FFmpeg error occurred!")
        print(f"FFmpeg stderr: {e.stderr.decode()}")
        message.reply_text("حدث خطأ أثناء ضغط الفيديو.")
    except Exception as e:
        print(f"General error: {e}")
        message.reply_text("حدث خطأ غير متوقع.")
    finally:
        os.remove(file)
        os.remove(temp_filename)

app.run()
