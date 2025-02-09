import os
import tempfile
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import *

def progress(current, total):
    print(f"Uploading: {current / total * 100:.1f}%")

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

user_video_data = {}

@app.on_message(filters.command("start"))
def start(client, message):
    message.reply_text("Send me a video and I will compress it for you.")

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    file = client.download_media(message.video.file_id if message.video else message.animation.file_id)

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ضغط عالي", callback_data="crf_27"),
                InlineKeyboardButton("ضغط متوسط", callback_data="crf_23"),
                InlineKeyboardButton("ضغط منخفض", callback_data="crf_18"),
            ]
        ]
    )
    reply_message = message.reply_text("اختر مستوى الضغط:", reply_markup=markup, quote=True) # احصل على كائن الرسالة المرسلة التي تحتوي على الأزرار
    user_video_data[reply_message.id] = {'file': file, 'message': message} # خزّن البيانات باستخدام معرّف رسالة الأزرار


@app.on_callback_query()
def compression_choice(client, callback_query):
    message_id = callback_query.message.id # استخدم message.id الخاص بـ callback_query.message مباشرة

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
        if callback_query.data == "crf_27": # ضغط عالي
            if message.animation:
                subprocess.run(f'ffmpeg -y -i "{file}" "{temp_filename}"', shell=True, check=True, capture_output=True)
            else:
                subprocess.run(
                    f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                    f'-b:v {VIDEO_BITRATE} -crf {27} -preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} '
                    f'-b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} '
                    f'-profile:v {VIDEO_PROFILE} -map_metadata -1 "{temp_filename}"',
                    shell=True, check=True, capture_output=True
                )
        elif callback_query.data == "crf_23": # ضغط متوسط
            if message.animation:
                subprocess.run(f'ffmpeg -y -i "{file}" "{temp_filename}"', shell=True, check=True, capture_output=True) # في حالة الانيميشن لا يتم تغيير قيمة crf
            else:
                subprocess.run(
                    f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                    f'-b:v {VIDEO_BITRATE} -crf {23} -preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} '
                    f'-b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} '
                    f'-profile:v {VIDEO_PROFILE} -map_metadata -1 "{temp_filename}"',
                    shell=True, check=True, capture_output=True
                )
        elif callback_query.data == "crf_18": # ضغط منخفض
            if message.animation:
                subprocess.run(f'ffmpeg -y -i "{file}" "{temp_filename}"', shell=True, check=True, capture_output=True) # في حالة الانيميشن لا يتم تغيير قيمة crf
            else:
                subprocess.run(
                    f'ffmpeg -y -i "{file}" -r {VIDEO_FPS} -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                    f'-b:v {VIDEO_BITRATE} -crf {18} -preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} '
                    f'-b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} '
                    f'-profile:v {VIDEO_PROFILE} -map_metadata -1 "{temp_filename}"',
                    shell=True, check=True, capture_output=True
                )

        message.reply_document(temp_filename, progress=progress)

    except subprocess.CalledProcessError as e:
        print(f"FFmpeg error: {e.stderr.decode()}")
        message.reply_text("حدث خطأ أثناء ضغط الفيديو.")
    except Exception as e:
        print(f"Error: {e}")
        message.reply_text("حدث خطأ غير متوقع.")
    finally:
        os.remove(file)
        os.remove(temp_filename)

app.run()
