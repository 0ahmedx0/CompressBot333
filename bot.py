import os
import tempfile
import subprocess
import threading
import time
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import *  # تأكد من تعريف المتغيرات مثل API_ID, API_HASH, API_TOKEN, CHANNEL_ID, VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE

# قائمة انتظار لتخزين الفيديوهات التي تحتاج إلى معالجة
video_queue = []
processing_lock = threading.Lock()
is_processing = False

def progress(current, total, message_type="User"):
    """عرض تقدم عملية التحميل."""
    if total > 0:
        print(f"Uploading to {message_type}: {current / total * 100:.1f}%")
    else:
        print(f"Uploading to {message_type}...")

def channel_progress(current, total):
    """عرض تقدم عملية تحميل الرسالة إلى القناة."""
    progress(current, total, "Channel")

def download_progress(current, total):
    """عرض تقدم عملية تحميل الفيديو (بالميجابايت)."""
    current_mb = current / (1024 * 1024)
    print(f"Downloading: {current_mb:.1f} MB")

# تهيئة العميل للبوت
app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

# لتخزين بيانات الفيديوهات الواردة
user_video_data = {}

def process_queue():
    """معالجة الفيديوهات الموجودة في قائمة الانتظار بشكل متسلسل."""
    global is_processing
    while video_queue:
        with processing_lock:
            if not video_queue:
                is_processing = False
                return
            video_data = video_queue.pop(0)  # الحصول على أول فيديو في القائمة
            is_processing = True

        file = video_data['file']
        message = video_data['message']
        button_message_id = video_data['button_message_id']

        try:
            # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
                temp_filename = temp_file.name

            ffmpeg_command = ""
            if video_data['quality'] == "crf_27":  # جودة منخفضة
                ffmpeg_command = (
                    f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                    f'-b:v 1200k -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                    f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'
                )
            elif video_data['quality'] == "crf_23":  # جودة متوسطة
                ffmpeg_command = (
                    f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                    f'-b:v 1700k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                    f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'
                )
            elif video_data['quality'] == "crf_18":  # جودة عالية
                ffmpeg_command = (
                    f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                    f'-b:v 2200k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                    f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'
                )

            print(f"Executing FFmpeg command: {ffmpeg_command}")
            subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True)
            print("FFmpeg command executed successfully.")

            # إرسال الفيديو المضغوط إلى المستخدم
            sent_to_user_message = message.reply_document(temp_filename, progress=progress)

            # إضافة تأخير بسيط للسماح لـ Telegram بمعالجة الرسالة قبل إعادة التوجيه
            time.sleep(3)
            if CHANNEL_ID:
                try:
                    app.forward_messages(
                        chat_id=CHANNEL_ID,
                        from_chat_id=message.chat.id,
                        message_ids=sent_to_user_message.id
                    )
                    print(f"Compressed video forwarded to channel: {CHANNEL_ID}")
                except Exception as e:
                    print(f"Error forwarding compressed video to channel: {e}")
            else:
                print("CHANNEL_ID not configured. Video not sent to channel.")
        except subprocess.CalledProcessError as e:
            print("FFmpeg error occurred!")
            print(f"FFmpeg stderr: {e.stderr.decode()}")
            message.reply_text("حدث خطأ أثناء ضغط الفيديو.")
        except Exception as e:
            print(f"General error: {e}")
            message.reply_text("حدث خطأ غير متوقع.")
        finally:
            # حذف الملف المؤقت
            os.remove(temp_filename)
            os.remove(file)

    is_processing = False

@app.on_message(filters.command("start"))
def start(client, message):
    """الرد على أمر /start."""
    message.reply_text("أرسل لي فيديو وسأقوم بضغطه لك.")

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    """
    معالجة الفيديو أو الرسوم المتحركة المرسلة.
    يتم تحميل الملف ثم إضافته إلى قائمة الانتظار.
    """
    file = client.download_media(
        message.video.file_id if message.video else message.animation.file_id,
        progress=download_progress
    )

    if CHANNEL_ID:
        try:
            client.forward_messages(
                chat_id=CHANNEL_ID,
                from_chat_id=message.chat.id,
                message_ids=message.id
            )
            print(f"Original video forwarded to channel: {CHANNEL_ID}")
        except Exception as e:
            print(f"Error forwarding original video to channel: {e}")

    # إعداد قائمة الأزرار لاختيار الجودة
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("جوده ضعيفه", callback_data="crf_27"),
                InlineKeyboardButton("جوده متوسطه", callback_data="crf_23"),
                InlineKeyboardButton("جوده عاليه", callback_data="crf_18"),
            ],
            [
                InlineKeyboardButton("الغاء", callback_data="cancel_compression"),
            ]
        ]
    )
    reply_message = message.reply_text("اختر مستوى الجوده :", reply_markup=markup, quote=True)
    button_message_id = reply_message.id

    # تخزين بيانات الفيديو في قائمة الانتظار
    user_video_data[button_message_id] = {
        'file': file,
        'message': message,
        'button_message_id': button_message_id,
    }

@app.on_callback_query()
def compression_choice(client, callback_query):
    """
    معالجة استعلام اختيار الجودة.
    في حال تم إلغاء الضغط يتم حذف الملف وإزالة الأزرار،
    أما في حال اختيار جودة معينة يتم إضافة الفيديو إلى قائمة الانتظار.
    """
    message_id = callback_query.message.id
    if message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب. يرجى إرسال الفيديو مرة أخرى.", show_alert=True)
        return

    if callback_query.data == "cancel_compression":
        video_data = user_video_data.pop(message_id)
        file = video_data['file']
        try:
            os.remove(file)
        except Exception as e:
            print(f"Error deleting file: {e}")
        callback_query.message.delete()
        callback_query.answer("تم إلغاء الضغط وحذف الفيديو.", show_alert=False)
        return

    # إضافة الفيديو إلى قائمة الانتظار مع الجودة المختارة
    video_data = user_video_data.pop(message_id)
    video_data['quality'] = callback_query.data
    video_queue.append(video_data)

    callback_query.answer("جاري الضغط...", show_alert=False)

    # بدء معالجة قائمة الانتظار إذا لم تكن هناك عملية قيد التنفيذ
    if not is_processing:
        threading.Thread(target=process_queue).start()

# تشغيل البوت
app.run()
