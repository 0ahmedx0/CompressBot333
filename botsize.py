import os
import tempfile
import subprocess
import threading
import time
import re  # لاستخدام التعبيرات النمطية لاستخراج الرقم
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty, MessageNotModified  # لتحسين التعامل مع تحديث الرسائل
from config import *  # تأكد من تعريف المتغيرات مثل API_ID, API_HASH, API_TOKEN, CHANNEL_ID, VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE

# تأكد من تعريف هذه المتغيرات في config.py أو هنا
# MAX_QUEUE_SIZE = 10
# CHANNEL_ID = -100xxxxxxxxxx # معرف القناة

# تهيئة مجلد التنزيلات
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# لتخزين بيانات الفيديوهات الواردة وحالة كل مستخدم
# Key: chat_id
# Value: {'file_path': ..., 'original_message': ..., 'download_msg_id': ..., 'duration': ...}
user_states = {}

# لتخزين بيانات تقدم التحميل لتحديث الرسائل
# Key: download_msg_id
# Value: {'chat_id': ..., 'total': ..., 'last_updated_time': time.time(), 'last_current_bytes': 0, 'start_time': time.time()}
download_progress_states = {}

# قائمة انتظار لتخزين الفيديوهات التي تحتاج إلى معالجة (بعد تحديد الحجم)
# Each item: {'file_path': ..., 'original_message': ..., 'target_bitrate_kbps': ...}
video_queue = []
processing_lock = threading.Lock()
is_processing = False

# تهيئة العميل للبوت
app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

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

def format_size(size_in_bytes):
    """تحويل حجم البايتات إلى تنسيق مقروء (KB, MB, GB)."""
    if size_in_bytes is None:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0

def format_time(seconds):
    """تحويل الثواني إلى تنسيق H:M:S."""
    if seconds is None or seconds < 0:
        return "N/A"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def download_progress_callback(current, total, chat_id, message_id):
    """
    تحديث رسالة تقدم التحميل.
    """
    state = download_progress_states.get(message_id)
    if not state:
        return

    now = time.time()
    start_time = state['start_time']
    last_updated_time = state['last_updated_time']
    last_current_bytes = state['last_current_bytes']

    # تحديث الرسالة كل ثانيتين أو كل 1 ميجابايت زيادة لتجنب Flood
    if now - last_updated_time < 2 and (current - last_current_bytes) < 1024 * 1024:
        return

    state['last_updated_time'] = now
    state['last_current_bytes'] = current

    elapsed_time = now - start_time
    speed = (current - last_current_bytes) / (now - last_updated_time) if now > last_updated_time else 0
    speed_formatted = format_size(speed) + "/s"

    percentage = (current / total) * 100 if total > 0 else 0
    downloaded_size = format_size(current)
    total_size = format_size(total)

    eta = (total - current) / speed if speed > 0 else None
    eta_formatted = format_time(eta)

    try:
        app.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"📥 جاري التحميل...\n\n"
                f"📊 النسبة: {percentage:.1f}%\n"
                f"📦 الحجم: {downloaded_size} / {total_size}\n"
                f"⚡ السرعة: {speed_formatted}\n"
                f"⏱ الوقت المتبقي: {eta_formatted}"
            )
        )
    except (MessageEmpty, MessageNotModified):
        pass # تجاهل إذا لم تتغير الرسالة أو كانت فارغة
    except Exception as e:
        print(f"Error updating download message {message_id}: {e}")

def get_video_duration(file_path):
    """
    استخراج مدة الفيديو بالثواني باستخدام ffprobe.
    """
    try:
        command = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        return duration
    except FileNotFoundError:
        print("Error: ffprobe not found. Make sure FFmpeg is installed and in your PATH.")
        return None
    except subprocess.CalledProcessError as e:
        print(f"Error running ffprobe: {e.stderr}")
        return None
    except ValueError:
        print("Error: Could not parse duration from ffprobe output.")
        return None
    except Exception as e:
        print(f"An unexpected error occurred while getting duration: {e}")
        return None

def calculate_bitrate(target_size_mb, duration_seconds, audio_bitrate_kbps=VIDEO_AUDIO_BITRATE):
    """
    حساب bitrate الفيديو المطلوب (بالكيلوبت في الثانية) للوصول إلى حجم مستهدف.
    نعتبر أن حجم الصوت ثابت ونركز على ضغط الفيديو.
    """
    if duration_seconds is None or duration_seconds <= 0:
        return None

    # تحويل الحجم المستهدف (MB) إلى كيلوبت (Kb)
    target_size_kb = target_size_mb * 1024 * 8

    # تقدير حجم الصوت بالكيلوبت (Kb)
    # الصوت: معدل البت (kbps) * المدة (ثانية)
    estimated_audio_size_kb = audio_bitrate_kbps * duration_seconds

    # حجم الفيديو المستهدف بالكيلوبت (Kb)
    target_video_size_kb = target_size_kb - estimated_audio_size_kb

    # يجب أن يكون حجم الفيديو المستهدف موجباً
    if target_video_size_kb <= 0:
         # في حالة كان حجم الصوت أكبر من أو يساوي الحجم المستهدف الإجمالي
         # نضع معدل بت الفيديو منخفض جدا ونعتمد على معدل بت الصوت المحدد
         print(f"Warning: Target size ({target_size_mb}MB) is too small for duration ({duration_seconds}s) with audio bitrate ({audio_bitrate_kbps}kbps). Setting minimum video bitrate.")
         return 100 # معدل بت فيديو منخفض جداً (مثلاً 100 كيلوبت/ثانية)

    # حساب معدل بت الفيديو المطلوب (كيلوبت/ثانية)
    # معدل البت (kbps) = حجم الفيديو (Kb) / المدة (ثانية)
    video_bitrate_kbps = target_video_size_kb / duration_seconds

    # ضمان أن معدل البت ليس منخفضاً جداً
    min_bitrate = 200 # kbps
    if video_bitrate_kbps < min_bitrate:
        print(f"Warning: Calculated bitrate ({video_bitrate_kbps:.2f}kbps) is too low. Using minimum bitrate ({min_bitrate}kbps).")
        return min_bitrate

    return int(video_bitrate_kbps) # نرجع قيمة صحيحة

def process_queue():
    """معالجة الفيديوهات الموجودة في قائمة الانتظار بشكل متسلسل."""
    global is_processing
    while True:
        with processing_lock:
            if not video_queue:
                is_processing = False
                break # الخروج من الدورة إذا كانت القائمة فارغة
            
            # التحقق من حجم قائمة الانتظار (للتنبيه فقط، التنفيذ متسلسل)
            print(f"Current queue size: {len(video_queue)}")

            video_data = video_queue.pop(0)  # الحصول على أول فيديو في القائمة
            is_processing = True # للتأكيد داخل القفل

        # استخراج البيانات
        file_path = video_data['file_path']
        original_message = video_data['original_message']
        target_bitrate_kbps = video_data['target_bitrate_kbps']

        temp_filename = None # تهيئة المتغير
        try:
            # إشعار المستخدم ببدء الضغط
            processing_message = original_message.reply_text("⚙️ جاري ضغط الفيديو...")

            # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
                temp_filename = temp_file.name

            # أمر FFmpeg باستخدام bitrate محسوب
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v {target_bitrate_kbps}k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'
            )

            print(f"Executing FFmpeg command: {ffmpeg_command}")
            # تنفيذ الأمر مع مراقبة المخرجات (اختياري لتحديث تقدم الضغط)
            # مثال بسيط: تشغيل الأمر والانتظار
            process = subprocess.Popen(ffmpeg_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()

            if process.returncode != 0:
                 print("FFmpeg error occurred!")
                 print(f"FFmpeg stderr: {stderr.decode()}")
                 original_message.reply_text("❌ حدث خطأ أثناء ضغط الفيديو.")
                 app.delete_messages(chat_id=processing_message.chat.id, message_ids=processing_message.id) # حذف رسالة الجاري المعالجة
                 continue # الانتقال إلى الفيديو التالي في القائمة

            print("FFmpeg command executed successfully.")
            app.edit_message_text(chat_id=processing_message.chat.id, message_ids=processing_message.id, text="✅ تم الضغط. جاري الرفع إلى القناة...") # تحديث الرسالة

            # إرسال الفيديو المضغوط إلى القناة
            if CHANNEL_ID:
                try:
                    app.send_document(
                        chat_id=CHANNEL_ID,
                        document=temp_filename,
                        # يمكن إضافة تقدم الرفع للقناة هنا أيضاً إذا لزم الأمر
                        # progress=channel_upload_progress_callback,
                        caption="الفيديو المضغوط"
                    )
                    print(f"Compressed video uploaded to channel: {CHANNEL_ID}")
                    
                    # إشعار المستخدم بنجاح العملية
                    original_message.reply_text("✅ تم ضغط الفيديو ورفعه بنجاح إلى القناة.")
                    app.delete_messages(chat_id=processing_message.chat.id, message_ids=processing_message.id) # حذف رسالة الجاري المعالجة

                except Exception as e:
                    print(f"Error uploading compressed video to channel: {e}")
                    original_message.reply_text("❌ حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة.")
                    app.delete_messages(chat_id=processing_message.chat.id, message_ids=processing_message.id) # حذف رسالة الجاري المعالجة

            else:
                print("CHANNEL_ID not configured. Video not sent to channel.")
                original_message.reply_text("⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط.")
                app.delete_messages(chat_id=processing_message.chat.id, message_ids=processing_message.id) # حذف رسالة الجاري المعالجة


        except Exception as e:
            print(f"General error during processing: {e}")
            original_message.reply_text("❌ حدث خطأ غير متوقع أثناء المعالجة.")
            # محاولة حذف رسالة الجاري المعالجة حتى لو لم يتم تعريفها قبل الـ try
            if 'processing_message' in locals() and processing_message:
                 try:
                     app.delete_messages(chat_id=processing_message.chat.id, message_ids=processing_message.id)
                 except Exception as del_e:
                     print(f"Error deleting processing message: {del_e}")

        finally:
            # حذف الملف المؤقت المضغوط إذا كان موجودًا
            if temp_filename and os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                    print(f"Deleted temporary file: {temp_filename}")
                except Exception as e:
                    print(f"Error deleting temporary file {temp_filename}: {e}")

            # حذف الملف الأصلي الذي تم تنزيله
            if file_path and os.path.exists(file_path):
                 try:
                     os.remove(file_path)
                     print(f"Deleted downloaded file: {file_path}")
                 except Exception as e:
                     print(f"Error deleting downloaded file {file_path}: {e}")

        # انتظار قصير قبل معالجة العنصر التالي
        time.sleep(3)


@app.on_message(filters.command("start") & filters.private)
def start(client, message):
    """الرد على أمر /start."""
    message.reply_text("أرسل لي فيديو أو أنيميشن وسأقوم بضغطه لك حسب الحجم المطلوب.")

@app.on_message((filters.video | filters.animation) & filters.private)
def handle_video(client, message):
    """
    معالجة الفيديو أو الرسوم المتحركة المرسلة.
    يتم تحميل الملف ثم يطلب من المستخدم تحديد الحجم المطلوب.
    """
    chat_id = message.chat.id

    # التحقق مما إذا كان المستخدم لديه عملية قيد التنفيذ بالفعل
    if chat_id in user_states:
        message.reply_text("لا يزال لديك عملية سابقة قيد الانتظار لتحديد الحجم. يرجى إكمالها أولاً.")
        return

    # إرسال رسالة لبدء عرض تقدم التحميل
    download_msg = message.reply_text("📥 جاري التحميل...")
    download_msg_id = download_msg.id

    # تهيئة حالة تقدم التحميل
    download_progress_states[download_msg_id] = {
        'chat_id': chat_id,
        'total': message.video.file_size if message.video else message.animation.file_size,
        'last_updated_time': time.time(),
        'last_current_bytes': 0,
        'start_time': time.time()
    }

    file = None
    try:
        # بدء التحميل باستخدام دالة التقدم المخصصة
        file = client.download_media(
            message.video.file_id if message.video else message.animation.file_id,
            file_name=f"{DOWNLOADS_DIR}/",
            progress=lambda current, total: download_progress_callback(current, total, chat_id, download_msg_id)
        )

        # التحقق من وجود الملف بعد التنزيل
        if not os.path.exists(file):
            message.reply_text("❌ حدث خطأ: لم يتم تنزيل الملف بنجاح.")
            app.delete_messages(chat_id=chat_id, message_ids=download_msg_id)
            del download_progress_states[download_msg_id]
            return

        # حذف رسالة التقدم بعد اكتمال التحميل
        try:
            app.delete_messages(chat_id=chat_id, message_ids=download_msg_id)
        except Exception as e:
            print(f"Error deleting download progress message {download_msg_id}: {e}")
        del download_progress_states[download_msg_id] # حذف حالة التقدم

        # الحصول على مدة الفيديو
        duration = get_video_duration(file)
        if duration is None:
            message.reply_text("❌ تعذر الحصول على مدة الفيديو.")
            # حذف الملف الذي تم تنزيله إذا تعذر الحصول على المدة
            if os.path.exists(file):
                try:
                    os.remove(file)
                    print(f"Deleted file after duration error: {file}")
                except Exception as e:
                    print(f"Error deleting file {file}: {e}")
            return

        # تخزين حالة المستخدم وطلب الحجم
        user_states[chat_id] = {
            'file_path': file,
            'original_message': message, # حفظ الرسالة الأصلية للرد عليها لاحقا
            'duration': duration,
        }

        message.reply_text(
            f"✅ تم تنزيل الفيديو (المدة: {format_time(duration)}).\n\n"
            f"الآن، أرسل لي **رقماً فقط** يمثل الحجم النهائي الذي تريده للفيديو بعد الضغط (بالميجابايت).\n"
            f"مثال: `50` (لضغط الفيديو إلى 50 ميجابايت)."
        )

    except Exception as e:
        print(f"Error during download process: {e}")
        message.reply_text("❌ حدث خطأ غير متوقع أثناء تنزيل الفيديو.")
        # تنظيف حالة التقدم إذا حدث خطأ
        if download_msg_id in download_progress_states:
             try:
                 app.delete_messages(chat_id=chat_id, message_ids=download_msg_id)
             except Exception as del_e:
                 print(f"Error deleting progress message on error: {del_e}")
             del download_progress_states[download_msg_id]
        # حذف الملف إذا تم تنزيله جزئيا
        if file and os.path.exists(file):
            try:
                os.remove(file)
                print(f"Deleted partial file: {file}")
            except Exception as e:
                print(f"Error deleting partial file {file}: {e}")


@app.on_message(filters.text & filters.private)
def handle_size_input(client, message):
    """
    معالجة الرسائل النصية التي تحتوي على رقم (الحجم المطلوب).
    """
    chat_id = message.chat.id

    # التحقق مما إذا كان المستخدم في حالة انتظار إدخال الحجم
    if chat_id not in user_states:
        # إذا لم يكن المستخدم في حالة انتظار، تجاهل الرسالة أو قم بالرد برسالة توجيهية
        # message.reply_text("الرجاء إرسال فيديو أولاً.") # يمكن تفعيل هذا للرسائل النصية العادية
        return

    # محاولة استخراج الرقم من النص
    try:
        target_size_mb = int(message.text.strip())
        if target_size_mb <= 0:
            raise ValueError("الحجم يجب أن يكون رقماً موجباً.")
    except ValueError as e:
        # إذا لم يكن النص رقماً صحيحاً وموجباً
        message.reply_text(f"⚠️ إدخال غير صالح. يرجى إرسال **رقم صحيح وموجب فقط** يمثل الحجم المطلوب بالميجابايت.\n{e}")
        return

    # الحصول على بيانات الفيديو من حالة المستخدم
    video_data = user_states.pop(chat_id) # إزالة المستخدم من الحالة بعد استلام الحجم
    file_path = video_data['file_path']
    original_message = video_data['original_message'] # الرسالة الأصلية للفيديو
    duration = video_data['duration']

    # التحقق مرة أخرى من وجود الملف والمدة
    if not os.path.exists(file_path) or duration is None:
        original_message.reply_text("❌ حدث خطأ: بيانات الفيديو مفقودة أو تالفة.")
        # محاولة حذف الملف إذا كان لا يزال موجوداً
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted file on state error: {file_path}")
            except Exception as e:
                print(f"Error deleting file {file_path}: {e}")
        return

    # حساب Bitrate المطلوب
    target_bitrate_kbps = calculate_bitrate(target_size_mb, duration, VIDEO_AUDIO_BITRATE)

    if target_bitrate_kbps is None:
         original_message.reply_text("❌ تعذر حساب معدل البت المناسب.")
         # حذف الملف الذي تم تنزيله إذا تعذر الحساب
         if os.path.exists(file_path):
             try:
                 os.remove(file_path)
                 print(f"Deleted file after bitrate calculation error: {file_path}")
             except Exception as e:
                 print(f"Error deleting file {file_path}: {e}")
         return

    # إضافة طلب الضغط إلى قائمة الانتظار
    with processing_lock:
        if len(video_queue) >= MAX_QUEUE_SIZE:
            message.reply_text("⚠️ قائمة الانتظار ممتلئة حالياً. يرجى المحاولة لاحقاً.")
            # إذا كانت القائمة ممتلئة، لا نحذف المستخدم من user_states
            # بل نعيده ليبقى في حالة انتظار
            user_states[chat_id] = video_data
            return
        
        video_queue.append({
            'file_path': file_path,
            'original_message': original_message,
            'target_bitrate_kbps': target_bitrate_kbps
        })
        message.reply_text(f"✅ تم إضافة الفيديو إلى قائمة الانتظار للضغط بحجم {target_size_mb} ميجابايت.")


    # بدء معالجة قائمة الانتظار إذا لم تكن هناك عملية قيد التنفيذ
    if not is_processing:
        threading.Thread(target=process_queue).start()

# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت (اختياري لكن مفيد)
def check_channel():
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت والاتصال بـ Telegram
    time.sleep(5)
    if not CHANNEL_ID:
        print("CHANNEL_ID غير محدد في ملف config.py. لن يتم رفع الفيديوهات إلى قناة.")
        return
    try:
        chat = app.get_chat(CHANNEL_ID)
        if not chat.type in ["channel", "supergroup"]:
             print(f"CHANNEL_ID ({CHANNEL_ID}) ليس لقناة أو مجموعة خارقة (Supergroup). يرجى التأكد من المعرف ونوع الدردشة.")
        else:
            print("تم التعرف على القناة:", chat.title)
    except Exception as e:
        print(f"خطأ في التعرف على القناة CHANNEL_ID={CHANNEL_ID}: {e}")
        print("يرجى التأكد من أن CHANNEL_ID صحيح وأن البوت مشرف في القناة ويمكنه إرسال الرسائل والمستندات.")


# تنظيف مجلد التنزيلات عند بدء تشغيل البوت
cleanup_downloads()

# تشغيل فحص القناة في خيط منفصل
threading.Thread(target=check_channel, daemon=True).start()

# تشغيل البوت
print("Bot started. Listening for messages...")
app.run()
