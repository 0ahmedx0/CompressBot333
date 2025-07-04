import os
import re
import tempfile
import subprocess
import threading
import time
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import *  # تأكد من تعريف المتغيرات مثل API_ID, API_HASH, API_TOKEN, CHANNEL_ID, VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE
MAX_QUEUE_SIZE = 10
# تهيئة مجلد التنزيلات
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

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

# قائمة انتظار لتخزين الفيديوهات التي تحتاج إلى معالجة
video_queue = []
processing_lock = threading.Lock()
is_processing = False

def cleanup_downloads():
    """
    تنظيف مجلد التنزيلات عند بدء تشغيل البوت.
    """
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")

def process_queue():
    global is_processing
    while video_queue:
        with processing_lock:
            if not video_queue:
                is_processing = False
                return

            video_data = video_queue.pop(0)
            is_processing = True

        file = video_data['file']
        message = video_data['message']
        temp_filename = None

        try:
            if not os.path.exists(file):
                message.reply_text("❌ لم يتم العثور على الملف.")
                continue

            # قراءة مدة الفيديو
            probe = ffmpeg.probe(file)
            duration_sec = float(probe['format']['duration'])

            # حجم الهدف من المستخدم
            target_size_mb = video_data.get('target_size_mb', 20)  # افتراضي 20MB
            target_bitrate = calculate_bitrate(target_size_mb, duration_sec)

            # إنشاء الملف المؤقت
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
                temp_filename = temp_file.name

            ffmpeg_command = (
                f'ffmpeg -y -i "{file}" -b:v {target_bitrate}k -c:v {VIDEO_CODEC} '
                f'-preset medium -pix_fmt {VIDEO_PIXEL_FORMAT} -c:a {VIDEO_AUDIO_CODEC} '
                f'-b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} '
                f'-map_metadata -1 "{temp_filename}"'
            )

            print(f"🎬 FFmpeg Command: {ffmpeg_command}")
            subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True)
            print("✅ FFmpeg ضغط الفيديو بنجاح.")

            # إرسال الفيديو إلى القناة
            if CHANNEL_ID:
                app.send_document(
                    chat_id=CHANNEL_ID,
                    document=temp_filename,
                    progress=channel_progress,
                    caption=f"🎞️ الفيديو المضغوط إلى ~{target_size_mb}MB"
                )
                print("✅ تم رفع الفيديو إلى القناة.")
                message.reply_text("✅ تم ضغط ورفع الفيديو بنجاح إلى القناة.")
            else:
                message.reply_text("✅ تم ضغط الفيديو. لكن لم يتم تحديد قناة للرفع.")

        except subprocess.CalledProcessError as e:
            print("❌ خطأ من FFmpeg!")
            print(f"stderr: {e.stderr.decode()}")
            message.reply_text("❌ حدث خطأ أثناء ضغط الفيديو.")
        except Exception as e:
            print(f"❌ General error: {e}")
            message.reply_text("❌ حدث خطأ غير متوقع أثناء المعالجة.")
        finally:
            if temp_filename and os.path.exists(temp_filename):
                os.remove(temp_filename)
            time.sleep(5)

    is_processing = False
  
@app.on_message(filters.command("start"))
def start(client, message):
    """الرد على أمر /start."""
    message.reply_text("أرسل لي فيديو وسأقوم بضغطه لك.")

def calculate_bitrate(target_size_mb, duration_sec):
    """حساب Bitrate المناسب لحجم الهدف والمدة."""
    return int((target_size_mb * 8192) / duration_sec)

@app.on_message(filters.video | filters.animation)
async def handle_video(client, message):
    try:
        file_id = message.video.file_id if message.video else message.animation.file_id

        # استخراج معلومات الملف من Telegram
        file_info = await client.get_file(file_id)
        file_path = file_info.file_path
        file_name = os.path.basename(file_path)
        direct_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_path}"
        local_path = f"{DOWNLOADS_DIR}/{file_name}"

        print(f"📥 Downloading from: {direct_url}")

        # إرسال رسالة مؤقتة لعرض التقدم
        progress_message = message.reply_text("🔽 بدأ تحميل الفيديو...")

        # أمر aria2c
        aria2_command = [
            "aria2c", "-x", "16", "-s", "16", "--summary-interval=1", "--console-log-level=warn",
            "-o", file_name, "-d", DOWNLOADS_DIR, direct_url
        ]

        process = subprocess.Popen(
            aria2_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        while True:
            line = process.stdout.readline()
            if not line:
                break

            match = re.search(
                r'(\d+(?:\.\d+)?[KMG]iB)/(\d+(?:\.\d+)?[KMG]iB)\((\d+(?:\.\d+)?)%\).*DL:(\d+(?:\.\d+)?[KMG]iB).*ETA:(\d+s)',
                line
            )

            if match:
                downloaded = match.group(1)
                total = match.group(2)
                percent = match.group(3)
                speed = match.group(4)
                eta = match.group(5)

                # تحديث النص
                text = (
                    f"📥 جاري تحميل الفيديو...\n"
                    f"⬇️ النسبة: {percent}%\n"
                    f"💾 الحجم: {downloaded} / {total}\n"
                    f"⚡ السرعة: {speed}\n"
                    f"⏳ متبقي: {eta}"
                )

                try:
                    progress_message.edit_text(text)
                except:
                    pass

        process.wait()
        if process.returncode != 0:
            progress_message.edit_text("❌ فشل تحميل الفيديو.")
            return

        # حذف رسالة التقدم
        try:
            progress_message.delete()
        except:
            pass

        # إرسال تعليمات للمستخدم
        message.reply_text("✅ تم تحميل الفيديو.\nالآن أرسل **رقم الحجم بالميجابايت** الذي تريده للفيديو (مثال: `50`)")

        # حفظ بيانات المستخدم مؤقتًا لحين استلام الرقم
        user_video_data[message.chat.id] = {
            'file': local_path,
            'message': message
        }

    except Exception as e:
        print(f"❌ Error in handle_video: {e}")
        message.reply_text("حدث خطأ أثناء تحميل الفيديو. حاول مرة أخرى.")

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

    video_data = user_video_data[message_id]

    if callback_query.data == "cancel_compression":
        cancel_compression(message_id)
        return

    # إيقاف المؤقت إذا كان قيد التشغيل
    if video_data['timer'] and video_data['timer'].is_alive():
        video_data['timer'].cancel()

    # إضافة الفيديو إلى قائمة الانتظار مع الجودة المختارة
    video_data['quality'] = callback_query.data
    video_queue.append(video_data)

    callback_query.answer("جاري الضغط...", show_alert=False)

    # بدء معالجة قائمة الانتظار إذا لم تكن هناك عملية قيد التنفيذ
    if not is_processing:
        threading.Thread(target=process_queue).start()

def auto_select_medium_quality(button_message_id):
    """
    اختيار الجودة المتوسطة تلقائيًا إذا لم يختار المستخدم خلال 30 ثانية.
    """
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        video_data['quality'] = "crf_23"  # اختيار الجودة المتوسطة تلقائيًا
        video_queue.append(video_data)

        # بدء معالجة قائمة الانتظار إذا لم تكن هناك عملية قيد التنفيذ
        if not is_processing:
            threading.Thread(target=process_queue).start()

        print(f"Auto-selected medium quality for message ID: {button_message_id}")

def cancel_compression(button_message_id):
    """
    إلغاء العملية وحذف الملف فقط عند الضغط على زر الإلغاء.
    """
    if button_message_id in user_video_data:
        video_data = user_video_data.pop(button_message_id)
        file = video_data['file']
        try:
            if os.path.exists(file):
                os.remove(file)
                print(f"Deleted file after cancellation: {file}")
        except Exception as e:
            print(f"Error deleting file: {e}")
        # حذف رسالة اختيار الجودة بعد الإلغاء
        app.get_messages(chat_id=video_data['message'].chat.id, message_ids=button_message_id).delete()
        print(f"Compression canceled for message ID: {button_message_id}")

        # بدء معالجة الفيديو التالي إذا كان هناك أي فيديوهات في قائمة الانتظار
        if not is_processing:
            threading.Thread(target=process_queue).start()
          
@app.on_message(filters.text & filters.private)
def handle_target_size(client, message):
    chat_id = message.chat.id

    # تحقق: هل عنده فيديو محفوظ ينتظر الحجم؟
    if chat_id not in user_video_data:
        return  # تجاهل الرسالة، ليست ذات صلة

    # تحقق أن الرسالة عبارة عن رقم فقط
    if not message.text.strip().isdigit():
        message.reply_text("❌ أرسل رقمًا فقط يمثل الحجم بالميجابايت (مثل: 50)")
        return

    target_size_mb = int(message.text.strip())

    # تحقق من الحجم المسموح
    if target_size_mb < 5 or target_size_mb > 200:
        message.reply_text("❌ الحجم يجب أن يكون بين 5 و200 ميجابايت.")
        return

    # استخراج بيانات الفيديو
    video_data = user_video_data.pop(chat_id)
    video_data['target_size_mb'] = target_size_mb

    # إضافة إلى قائمة الانتظار
    video_queue.append(video_data)

    # إعلام المستخدم
    message.reply_text(f"📦 جاري ضغط الفيديو إلى حوالي {target_size_mb}MB...")

    # بدء المعالجة إذا ما كانت شغالة
    if not is_processing:
        threading.Thread(target=process_queue).start()

# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت
def check_channel():
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت
    time.sleep(3)
    try:
        chat = app.get_chat(CHANNEL_ID)
        print("تم التعرف على القناة:", chat.title)
    except Exception as e:
        print("خطأ في التعرف على القناة:", e)

# تنظيف مجلد التنزيلات عند بدء تشغيل البوت
cleanup_downloads()

# تشغيل فحص القناة في خيط منفصل بحيث لا يؤثر على عمل البوت
threading.Thread(target=check_channel, daemon=True).start()

# تشغيل البوت
app.run()
