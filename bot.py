import os
import tempfile
import subprocess
import threading
import time
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import *  # تأكد من تعريف المتغيرات مثل API_ID, API_HASH, API_TOKEN, CHANNEL_ID, VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE

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

# تعريف قفل عالمي لضمان تنفيذ عمليات الضغط بشكل متسلسل
processing_lock = threading.Lock()

def auto_select_medium_quality(button_message_id):
    """
    اختيار الجودة المتوسطة تلقائيًا عند انتهاء المهلة.
    يتم استدعاؤه بواسطة مؤقت (Timer).
    """
    if button_message_id in user_video_data:
        client = app  # استخدام الكائن العام للبوت
        try:
            # استدعاء دالة compression_choice باستخدام استعلام وهمي (dummy callback)
            compression_choice(client, user_video_data[button_message_id]['dummy_callback_query'])
            print(f"Auto-selected medium quality for message ID: {button_message_id}")
        except Exception as e:
            print(f"Error auto-selecting medium quality: {e}")
        finally:
            pass  # لا نقوم بحذف البيانات هنا للسماح بإعادة الضغط لاحقاً

@app.on_message(filters.command("start"))
def start(client, message):
    """الرد على أمر /start."""
    message.reply_text("أرسل لي فيديو وسأقوم بضغطه لك.")

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    """
    معالجة الفيديو أو الرسوم المتحركة المرسلة.
    يتم تحميل الملف ثم إعادة توجيهه للقناة الأصلية في حال تم تهيئة CHANNEL_ID،
    بعدها يتم عرض خيارات الجودة للمستخدم.
    """
    user_video_data.clear()  # مسح البيانات القديمة عند استلام فيديو جديد
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

    # تعريف كائن CallbackQuery وهمي للاستخدام في auto-select
    class DummyCallbackQuery:
        def __init__(self, message, data):
            self.message = message
            self.data = data
        def answer(self, text, show_alert):
            print(f"DummyCallbackQuery Answer: {text}, show_alert={show_alert}")

    dummy_callback_query = DummyCallbackQuery(reply_message, "crf_23")

    # تخزين بيانات الفيديو مع إعداد مؤقت لاختيار الجودة المتوسطة تلقائيًا بعد 30 ثانية
    user_video_data[button_message_id] = {
        'file': file,
        'message': message,
        'button_message_id': button_message_id,
        'timer': threading.Timer(30, auto_select_medium_quality, args=[button_message_id]),
        'dummy_callback_query': dummy_callback_query,
    }
    user_video_data[button_message_id]['timer'].start()

@app.on_callback_query()
def compression_choice(client, callback_query):
    """
    معالجة استعلام اختيار الجودة.
    في حال تم إلغاء الضغط يتم حذف الملف وإزالة الأزرار،
    أما في حال اختيار جودة معينة يتم الضغط باستخدام FFmpeg ثم إرسال الفيديو المضغوط.
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

    # تنفيذ عملية الضغط بشكل متسلسل باستخدام القفل
    with processing_lock:
        video_data = user_video_data[message_id]

        if video_data['timer'].is_alive():
            video_data['timer'].cancel()
            print(f"Timer cancelled for message ID: {message_id}")

        file = video_data['file']
        message = video_data['message']

        callback_query.answer("جاري الضغط...", show_alert=False)

        # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
            temp_filename = temp_file.name

        try:
            ffmpeg_command = ""
            if callback_query.data == "crf_27":  # جودة منخفضة
                if message.animation:
                    ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
                else:
                    ffmpeg_command = (
                        f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                        f'-b:v 1000k -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                        f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'
                    )
            elif callback_query.data == "crf_23":  # جودة متوسطة
                if message.animation:
                    ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
                else:
                    ffmpeg_command = (
                        f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                        f'-b:v 1700k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                        f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'
                    )
            elif callback_query.data == "crf_18":  # جودة عالية
                if message.animation:
                    ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
                else:
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
                    client.forward_messages(
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
            # لا نقوم بحذف الملف الأصلي حتى نسمح بعمليات إعادة الضغط لاحقًا
            os.remove(temp_filename)

# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت
def check_channel():
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت
    time.sleep(3)
    try:
        chat = app.get_chat(CHANNEL_ID)
        print("تم التعرف على القناة:", chat.title)
    except Exception as e:
        print("خطأ في التعرف على القناة:", e)

# تشغيل فحص القناة في خيط منفصل بحيث لا يؤثر على عمل البوت
threading.Thread(target=check_channel, daemon=True).start()

app.run()
