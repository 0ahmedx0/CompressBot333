import os
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
    """معالجة الفيديوهات الموجودة في قائمة الانتظار بشكل متسلسل."""
    global is_processing
    while video_queue:
        with processing_lock:
            if not video_queue:
                is_processing = False
                return
            
            # التحقق من حجم قائمة الانتظار
            print(f"Current queue size: {len(video_queue)}")
            if len(video_queue) > MAX_QUEUE_SIZE:
                print("Queue is full. Waiting for processing...")
                time.sleep(5)  # انتظار حتى يتم تفريغ القائمة
                continue

            video_data = video_queue.pop(0)  # الحصول على أول فيديو في القائمة
            is_processing = True

        file = video_data['file']
        message = video_data['message']
        button_message_id = video_data['button_message_id']

        temp_filename = None  # تهيئة المتغير لتجنب UnboundLocalError
        try:
            # التحقق من وجود الملف قبل المعالجة
            if not os.path.exists(file):
                print(f"File not found: {file}")
                message.reply_text("حدث خطأ: لم يتم العثور على الملف الأصلي.")
                continue

            # تحويل الفيديو الأصلي إلى القناة عند بدء المعالجة (بدون إضافة وصف)
            if CHANNEL_ID:
                try:
                    app.forward_messages(
                        chat_id=CHANNEL_ID,
                        from_chat_id=message.chat.id,
                        message_ids=message.id
                    )
                    print(f"Original video forwarded to channel: {CHANNEL_ID}")
                except Exception as e:
                    print(f"Error forwarding original video to channel: {e}")

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

            # إرسال الفيديو المضغوط مباشرة إلى القناة مع إضافة وصف "الفيديو المضغوط"
            if CHANNEL_ID:
                try:
                    sent_to_channel_message = app.send_document(
                        chat_id=CHANNEL_ID,
                        document=temp_filename,
                        progress=channel_progress,
                        caption="الفيديو المضغوط"
                    )
                    print(f"Compressed video uploaded to channel: {CHANNEL_ID}")
                    
                    # إشعار المستخدم بنجاح العملية
                    message.reply_text("تم ضغط الفيديو ورفعه بنجاح إلى القناة.")
                except Exception as e:
                    print(f"Error uploading compressed video to channel: {e}")
                    message.reply_text("حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة.")
            else:
                print("CHANNEL_ID not configured. Video not sent to channel.")
                message.reply_text("لم يتم تهيئة قناة لرفع الفيديو المضغوط.")

        except subprocess.CalledProcessError as e:
            print("FFmpeg error occurred!")
            print(f"FFmpeg stderr: {e.stderr.decode()}")
            message.reply_text("حدث خطأ أثناء ضغط الفيديو.")
        except Exception as e:
            print(f"General error: {e}")
            message.reply_text("حدث خطأ غير متوقع.")
        finally:
            # حذف الملف المؤقت إذا كان موجودًا
            if temp_filename and os.path.exists(temp_filename):
                os.remove(temp_filename)
            # لم نقم بحذف الملف الأصلي، ليظل محفوظاً للضغط لاحقاً.
            time.sleep(5)

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
    # عدم مسح البيانات القديمة للسماح بمعالجة فيديوهات متعددة
    file = client.download_media(
        message.video.file_id if message.video else message.animation.file_id,
        file_name=f"{DOWNLOADS_DIR}/",
        progress=download_progress
    )

    # التحقق من وجود الملف بعد التنزيل
    if not os.path.exists(file):
        message.reply_text("حدث خطأ: لم يتم تنزيل الملف بنجاح.")
        return

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

    # تخزين بيانات الفيديو في القاموس بدون مسح البيانات السابقة للسماح بمعالجة فيديوهات متعددة
    user_video_data[button_message_id] = {
        'file': file,
        'message': message,
        'button_message_id': button_message_id,
        'timer': None,  # مؤقت للاختيار التلقائي
    }

    # إعداد مؤقت لمدة 30 ثانية للاختيار التلقائي
    timer = threading.Timer(30, auto_select_medium_quality, args=[button_message_id])
    user_video_data[button_message_id]['timer'] = timer
    timer.start()

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
