import os
import tempfile
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed # لاستخدام التحميل والضغط المتوازيين
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty, UserNotParticipant # لاستثناءات Pyrogram

# تأكد من تعريف المتغيرات في ملف config.py:
# API_ID, API_HASH, API_TOKEN, CHANNEL_ID
# VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE

from config import *

# -------------------------- الثوابت والإعدادات --------------------------
# تهيئة مجلد التنزيلات
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# قائمة انتظار للتحميل، لم تعد تستخدم بنفس الشكل القديم
# لأن التحميل أصبح يتم بشكل فوري في ThreadPool
# MAX_QUEUE_SIZE لم تعد ذات صلة بالتحميل لكن يمكن أن تكون للضغط مستقبلا إذا لزم الأمر

# تهيئة ThreadPoolExecutor للتحميل (تحميل الفيديوهات من تليجرام)
download_executor = ThreadPoolExecutor(max_workers=5) # 5 خيوط للتحميل المتزامن

# تهيئة ThreadPoolExecutor للضغط (معالجة الفيديوهات بـ FFmpeg)
compression_executor = ThreadPoolExecutor(max_workers=3) # 3 خيوط للضغط المتزامن

# -------------------------- وظائف المساعدة --------------------------

def progress(current, total, message_type="User"):
    """عرض تقدم عملية التحميل/الرفع."""
    if total > 0:
        percent = current / total * 100
        print(f"[{message_type}] Progress: {percent:.1f}% ({current / (1024 * 1024):.2f}MB / {total / (1024 * 1024):.2f}MB)")
    else:
        print(f"[{message_type}] Progress: {current / (1024 * 1024):.2f}MB")

def cleanup_downloads():
    """
    تنظيف مجلد التنزيلات عند بدء تشغيل البوت.
    """
    print("Cleaning up downloads directory...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")
    print("Downloads directory cleaned.")

# -------------------------- تهيئة العميل للبوت --------------------------
app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

# لتخزين بيانات الفيديوهات الواردة، المفتاح سيكون `button_message_id`
user_video_data = {}

# -------------------------- وظائف المعالجة --------------------------

def process_video_for_compression(video_data):
    """
    الدالة التي تقوم بعملية ضغط الفيديو باستخدام FFmpeg ورفعه.
    تعمل داخل compression_executor.
    """
    file_path = video_data['file']
    message = video_data['message']
    button_message_id = video_data['button_message_id']
    quality = video_data['quality']

    temp_compressed_filename = None
    try:
        # التحقق من وجود الملف قبل المعالجة
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            message.reply_text("حدث خطأ: لم يتم العثور على الملف الأصلي.")
            return

        # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        ffmpeg_command = ""
        # يجب أن تأخذ إعدادات FFmpeg من config.py وتستخدمها كما هي
        if quality == "crf_27":  # جودة منخفضة
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v 1200k -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_compressed_filename}"'
            )
        elif quality == "crf_23":  # جودة متوسطة (الافتراضية)
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v 1700k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_compressed_filename}"'
            )
        elif quality == "crf_18":  # جودة عالية
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v 2200k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_compressed_filename}"'
            )
        else:
            message.reply_text("حدث خطأ: جودة ضغط غير صالحة.")
            return

        print(f"Executing FFmpeg command for {file_path}: {ffmpeg_command}")
        # استخدام subprocess.run للحصول على مخرجات الأخطاء بشكل أفضل
        process = subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True, text=True)
        print(f"FFmpeg command executed successfully for {file_path}.")
        if process.stdout:
            print(f"FFmpeg stdout:\n{process.stdout}")
        if process.stderr:
            print(f"FFmpeg stderr:\n{process.stderr}")
        compressed_file_size_mb = 0
        if os.path.exists(temp_compressed_filename):
            compressed_file_size_bytes = os.path.getsize(temp_compressed_filename)
            compressed_file_size_mb = compressed_file_size_bytes / (1024 * 1024)
            print(f"Compressed file size: {compressed_file_size_mb:.2f} MB")
        else:
            print(f"Error: Compressed file {temp_compressed_filename} not found after FFmpeg.")
            message.reply_text("حدث خطأ أثناء ضغط الفيديو: لم يتم إنشاء الملف المضغوط.")
            return # الخروج لأن الملف غير موجود للرفع

        # إرسال الفيديو المضغوط مباشرة إلى القناة
        if CHANNEL_ID:
            try:
                # التحقق مما إذا كان الملف المؤقت قد تم إنشاؤه بحجم معقول
                if not os.path.exists(temp_compressed_filename) or os.path.getsize(temp_compressed_filename) == 0:
                    print(f"Error: Compressed file {temp_compressed_filename} is empty or not created.")
                    message.reply_text("حدث خطأ أثناء ضغط الفيديو: الملف الناتج فارغ.")
                    return

                # إرسال نسخة من الفيديو الأصلي إلى القناة قبل المضغوط (إذا لم يتم إرساله بعد)
                # هذه الخطوة كانت تتم مباشرة بعد التنزيل في الكود الأصلي، نضمن هنا أنها لم تتكرر
                # ولكن لتبسيط الأمر وتجنب التعقيد بوجود مؤشرات للحالة، يمكن فصلها لتصبح رسالتين منفصلتين
                # أو عدم إرسال الأصلي إلا إذا طلب ذلك
                # For now, let's keep it as per the original intention: original and then compressed.
                try:
                    app.forward_messages(
                        chat_id=CHANNEL_ID,
                        from_chat_id=message.chat.id,
                        message_ids=message.id
                    )
                    print(f"Original video from chat {message.chat.id} forwarded to channel {CHANNEL_ID}.")
                except (MessageEmpty, UserNotParticipant) as e:
                    print(f"Could not forward original message {message.id} to channel {CHANNEL_ID}: {e}")
                except Exception as e:
                    print(f"Error forwarding original video to channel: {e}")

                sent_to_channel_message = app.send_document(
                    chat_id=CHANNEL_ID,
                    document=temp_compressed_filename,
                    progress=lambda current, total: progress(current, total, "Channel Upload"), # تقدم الرفع
                    caption="الفيديو المضغوط"
                )
                print(f"Compressed video uploaded to channel: {CHANNEL_ID} for original message ID {message.id}.")
                message.reply_text(f"✅ تم ضغط الفيديو ورفعه بنجاح إلى القناة! (الحجم: {compressed_file_size_mb:.2f} ميجابايت)") # إضافة الحجم هنا
            except Exception as e:
                print(f"Error uploading compressed video to channel {CHANNEL_ID} or sending reply to user: {e}")
                message.reply_text(f"حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة: {e}")
        else:
            print("CHANNEL_ID not configured. Compressed video not sent to channel.")
            message.reply_text(f"⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط. تم ضغط الفيديو بنجاح (الحجم: {compressed_file_size_mb:.2f} ميجابايت) لكن لم يتم رفعه.")

    except subprocess.CalledProcessError as e:
        print(f"FFmpeg error occurred for {file_path}!")
        print(f"FFmpeg stdout: {e.stdout}")
        print(f"FFmpeg stderr: {e.stderr}")
        message.reply_text(f"حدث خطأ أثناء ضغط الفيديو: {e.stderr.decode('utf-8') if e.stderr else 'غير معروف'}")
    except Exception as e:
        print(f"General error during video processing for {file_path}: {e}")
        message.reply_text(f"حدث خطأ غير متوقع أثناء معالجة الفيديو: {e}")
    finally:
        # حذف الملف الأصلي والملف المؤقت المضغوط بعد الانتهاء
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted original file: {file_path}")
            except Exception as e:
                print(f"Error deleting original file {file_path}: {e}")
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            try:
                os.remove(temp_compressed_filename)
                print(f"Deleted temporary compressed file: {temp_compressed_filename}")
            except Exception as e:
                print(f"Error deleting temporary file {temp_compressed_filename}: {e}")
        # حذف إدخال الفيديو من القاموس بعد الانتهاء
        if button_message_id in user_video_data:
            # نتأكد من أن المؤقت قد تم إلغاؤه (أو لم يكن موجودا بالأساس)
            if user_video_data[button_message_id].get('timer') and user_video_data[button_message_id]['timer'].is_alive():
                user_video_data[button_message_id]['timer'].cancel()
            del user_video_data[button_message_id]
            print(f"Cleaned up data for message ID: {button_message_id}")

def auto_select_medium_quality(button_message_id):
    """
    اختيار الجودة المتوسطة تلقائيًا إذا لم يختار المستخدم خلال 30 ثانية.
    يجب أن يتم هذا داخل الـ Thread الذي يتعامل مع التايمر لتجنب مشكلات.
    """
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        if 'quality_chosen' not in video_data or not video_data['quality_chosen']: # التأكد أنه لم يتم الاختيار بالفعل
            print(f"Auto-selecting medium quality for message ID: {button_message_id}")
            # تحديث الجودة وإضافة علامة على أن الاختيار تم تلقائياً
            video_data['quality'] = "crf_23"
            video_data['quality_chosen'] = True # لنتجنب المعالجة المزدوجة لو اختار المستخدم لاحقًا
            
            # تحديث الرسالة الأصلية لإزالة الأزرار وإعلام المستخدم
            try:
                app.edit_message_reply_markup(
                    chat_id=video_data['message'].chat.id,
                    message_id=button_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم اختيار جودة متوسطة تلقائيًا", callback_data="none")]])
                )
            except Exception as e:
                print(f"Error updating message reply markup after auto-select: {e}")

            # إرسال الفيديو للضغط في الـ ThreadPool
            print(f"Submitting auto-selected video (ID: {button_message_id}) to compression_executor.")
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            print(f"Quality already chosen for message ID: {button_message_id}. Skipping auto-selection.")

def cancel_compression_action(button_message_id):
    """
    إلغاء العملية وحذف الملفات.
    """
    if button_message_id in user_video_data:
        video_data = user_video_data.pop(button_message_id)
        file_path = video_data['file']
        
        # إلغاء المؤقت إن كان نشطًا
        if video_data['timer'] and video_data['timer'].is_alive():
            video_data['timer'].cancel()
            print(f"Timer for message ID {button_message_id} cancelled.")

        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"Deleted file after cancellation: {file_path}")
            else:
                print(f"File {file_path} not found for deletion during cancellation.")
        except Exception as e:
            print(f"Error deleting file {file_path} during cancellation: {e}")
        
        # حذف رسالة اختيار الجودة بعد الإلغاء
        try:
            app.delete_messages(chat_id=video_data['message'].chat.id, message_ids=button_message_id)
            print(f"Deleted quality selection message {button_message_id}.")
            video_data['message'].reply_text("❌ تم إلغاء عملية الضغط وحذف الملف.", quote=True)
        except Exception as e:
            print(f"Error deleting messages after cancellation: {e}")
        
        print(f"Compression canceled for message ID: {button_message_id}")

# -------------------------- معالجات رسائل البوت --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    """الرد على أمر /start."""
    message.reply_text("أهلاً بك! أرسل لي فيديو أو GIF وسأقوم بضغطه لك.")

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    """
    معالجة الفيديو أو الرسوم المتحركة المرسلة.
    يتم تحميل الملف في ThreadPoolExecutor منفصل.
    """
    print(f"Received video/animation from user {message.from_user.id}. Downloading...")
    
    file_id = message.video.file_id if message.video else message.animation.file_id
    file_name_prefix = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{message.id}")
    
    # تحميل الفيديو في download_executor
    download_future = download_executor.submit(
        client.download_media,
        file_id,
        file_name=file_name_prefix, # Pyrogram ستضيف اللاحقة (.mp4) تلقائيا
        progress=lambda current, total: progress(current, total, "Download") # دالة التقدم
    )

    # تخزين Future والبيانات في user_video_data ليتم متابعتها لاحقا
    # هنا نستخدم message.id الأصلي لتتبع حالة التحميل والعلاقة بزر الاختيار
    user_video_data[message.id] = {
        'message': message,
        'download_future': download_future,
        'file': None, # سيتم تعيينه بعد اكتمال التحميل
        'button_message_id': None, # سيتم تعيينه بعد إرسال رسالة الأزرار
        'timer': None, # مؤقت للاختيار التلقائي
        'quality_chosen': False # علم للتأكد من اختيار الجودة مرة واحدة
    }
    
    # انتظار اكتمال التحميل دون حجب (يمكن استخدام as_completed لكن هذا يبسط المثال)
    # الفيديوهات القادمة ستتم معالجتها على الفور بواسطة Pyrogram
    # هذه الخطوة تتطلب "Await" ولكن بما أننا نعمل داخل ThreadPoolExecutor فلا نستطيع استخدامها مباشرة هنا.
    # يمكننا إضافة كولباك لمعالجة ما بعد التحميل، أو جعل معالج Pyrogram ينتظر المستقبل (blocking for current message only).
    # الحل الأفضل: فصل إنشاء الأزرار عن إرسالها بناءً على اكتمال التحميل.
    
    # For simplicity: create a new thread for creating buttons and starting timer after download.
    # A more robust solution might use `done_callbacks` for futures.
    threading.Thread(target=post_download_actions, args=[message.id, message.chat.id]).start()

def post_download_actions(original_message_id, chat_id):
    """
    تتم هذه الدالة بعد اكتمال التحميل، في خيط منفصل لتجنب حظر المعالج الرئيسي.
    """
    if original_message_id not in user_video_data:
        print(f"Data for original message ID {original_message_id} not found in post_download_actions.")
        return

    video_data = user_video_data[original_message_id]
    download_future = video_data['download_future']
    message = video_data['message']

    try:
        file_path = download_future.result() # هذا سيحجب الخيط حتى يكتمل التحميل
        video_data['file'] = file_path
        print(f"Download complete for message ID {original_message_id}. File path: {file_path}")

        # إرسال الفيديو الأصلي إلى القناة عند بدء التحميل (تم تغيير موضعها لتكون بعد انتهاء التحميل)
        # هذا يضمن أن يتم فورًا بعد تحميل الملف وقبل أن يبدأ الضغط.
        if CHANNEL_ID:
            try:
                # نستخدم message.copy لعدم إعادة تحميلها ونحافظ على معلومات الملف الأصلي
                app.copy_message(
                    chat_id=CHANNEL_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.id,
                    caption="الفيديو الأصلي"
                )
                print(f"Original video {original_message_id} copied to channel: {CHANNEL_ID}")
            except (MessageEmpty, UserNotParticipant) as e:
                print(f"Could not copy original message {message.id} to channel {CHANNEL_ID}: {e}")
            except Exception as e:
                print(f"Error copying original video to channel: {e}")

        # إعداد قائمة الأزرار لاختيار الجودة
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("جودة ضعيفة (27 CRF)", callback_data="crf_27"),
                    InlineKeyboardButton("جودة متوسطة (23 CRF)", callback_data="crf_23"),
                    InlineKeyboardButton("جودة عالية (18 CRF)", callback_data="crf_18"),
                ],
                [
                    InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_compression"),
                ]
            ]
        )
        # استخدام message.reply_text لربط الأزرار بالرسالة الأصلية
        reply_message = message.reply_text(
            "تم تنزيل الفيديو. يرجى اختيار مستوى الجودة للضغط أو سيتم اختيار جودة متوسطة تلقائيا بعد 30 ثانية:",
            reply_markup=markup,
            quote=True # للاقتباس من الرسالة الأصلية
        )
        # تحديث بيانات الفيديو برمز رسالة الأزرار
        video_data['button_message_id'] = reply_message.id
        # تحديث المفتاح في user_video_data من original_message_id إلى button_message_id
        # لتجنب الارتباك عند التعامل مع callback_query
        user_video_data[reply_message.id] = user_video_data.pop(original_message_id)


        # إعداد مؤقت لمدة 30 ثانية للاختيار التلقائي
        timer = threading.Timer(30, auto_select_medium_quality, args=[reply_message.id])
        user_video_data[reply_message.id]['timer'] = timer
        timer.start()

    except Exception as e:
        print(f"Error during post-download actions for message ID {original_message_id}: {e}")
        message.reply_text(f"حدث خطأ أثناء تنزيل الفيديو: {e}")
        # حذف بيانات الفيديو من القاموس إذا حدث خطأ في التنزيل
        if original_message_id in user_video_data:
            if user_video_data[original_message_id].get('file') and os.path.exists(user_video_data[original_message_id]['file']):
                os.remove(user_video_data[original_message_id]['file'])
                print(f"Cleaned up partial download: {user_video_data[original_message_id]['file']}")
            del user_video_data[original_message_id]


@app.on_callback_query()
def compression_choice_callback(client, callback_query):
    """
    معالجة استعلام اختيار الجودة.
    """
    message_id = callback_query.message.id # هو button_message_id هنا
    
    if message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب أو تم إلغاؤه مسبقًا.", show_alert=True)
        # Attempt to delete the inline keyboard message if it exists
        try:
            callback_query.message.delete()
        except Exception as e:
            print(f"Could not delete stale callback message {message_id}: {e}")
        return

    video_data = user_video_data[message_id]

    if video_data.get('quality_chosen'):
        callback_query.answer("تم اختيار الجودة مسبقًا لهذا الفيديو.", show_alert=True)
        return

    if callback_query.data == "cancel_compression":
        callback_query.answer("يتم إلغاء العملية...", show_alert=False)
        cancel_compression_action(message_id)
        return

    # إيقاف المؤقت إذا كان قيد التشغيل
    if video_data['timer'] and video_data['timer'].is_alive():
        video_data['timer'].cancel()
        print(f"Timer for message ID {message_id} cancelled by user choice.")

    # تأكيد أن ملف الفيديو قد تم تنزيله بالفعل
    if not video_data['file'] or not os.path.exists(video_data['file']):
        callback_query.answer("لم يكتمل تنزيل الفيديو بعد، يرجى المحاولة لاحقًا أو إعادة إرساله.", show_alert=True)
        # Cleanup the button message if the file is not found (stale entry)
        try:
            app.delete_messages(chat_id=video_data['message'].chat.id, message_ids=message_id)
        except Exception as e:
            print(f"Could not delete message {message_id}: {e}")
        if message_id in user_video_data: # Remove stale entry
            del user_video_data[message_id]
        return

    # وضع علامة على أن الجودة قد تم اختيارها
    video_data['quality'] = callback_query.data
    video_data['quality_chosen'] = True

    callback_query.answer("تم استلام اختيارك. جاري الضغط...", show_alert=False)

    # تحديث الأزرار لتجنب إعادة الضغط أو الاختيار مرة أخرى
    try:
        app.edit_message_reply_markup(
            chat_id=callback_query.message.chat.id,
            message_id=message_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ تم اختيار الجودة: {callback_query.data.replace('crf_', 'CRF ')}", callback_data="none")]])
        )
    except Exception as e:
        print(f"Error editing message reply markup: {e}")

    # إرسال الفيديو للضغط في الـ ThreadPool
    print(f"Submitting user-selected video (ID: {message_id}) with quality '{video_data['quality']}' to compression_executor.")
    compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- تشغيل البوت --------------------------

# تنظيف مجلد التنزيلات عند بدء تشغيل البوت
cleanup_downloads()

# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت (للتأكد من أنها صالحة)
def check_channel_on_start():
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت
    time.sleep(5)
    if CHANNEL_ID:
        try:
            chat = app.get_chat(CHANNEL_ID)
            print(f"✅ تم التعرف على القناة بنجاح: '{chat.title}' (ID: {CHANNEL_ID})")
            if chat.type not in ["channel", "supergroup"]:
                print("⚠️ ملاحظة: ID القناة المحدد ليس لقناة أو مجموعة خارقة.")
            elif not chat.permissions.can_post_messages: # Example of permission check
                 print(f"⚠️ ملاحظة: البوت ليس لديه صلاحية نشر الرسائل في القناة '{chat.title}'.")
        except Exception as e:
            print(f"❌ خطأ في التعرف على القناة '{CHANNEL_ID}': {e}. يرجى التأكد من أن البوت مشرف في القناة وأن ID صحيح.")
    else:
        print("⚠️ لم يتم تحديد CHANNEL_ID في ملف config.py. لن يتم رفع الفيديوهات إلى قناة.")

# تشغيل فحص القناة في خيط منفصل بحيث لا يؤثر على عمل البوت
threading.Thread(target=check_channel_on_start, daemon=True).start()

# تشغيل البوت
print("🚀 البوت بدأ العمل!")
app.run()
