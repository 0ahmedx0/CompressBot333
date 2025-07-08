import os
import tempfile
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor # لاستخدام التحميل والضغط المتوازيين
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty, UserNotParticipant # لاستثناءات Pyrogram

# استيراد المتغيرات من ملف config.py
# تأكد من تعريف هذه المتغيرات في config.py:
# API_ID, API_HASH, API_TOKEN, CHANNEL_ID
# VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE
from config import *

# -------------------------- الثوابت والإعدادات --------------------------
# تهيئة مجلد التنزيلات
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# تهيئة ThreadPoolExecutor للتحميل (تحميل الفيديوهات من تليجرام)
# يسمح بـ 5 عمليات تحميل متزامنة كحد أقصى.
download_executor = ThreadPoolExecutor(max_workers=5) 

# تهيئة ThreadPoolExecutor للضغط (معالجة الفيديوهات بـ FFmpeg)
# يسمح بـ 3 عمليات ضغط متزامنة كحد أقصى.
compression_executor = ThreadPoolExecutor(max_workers=3) 

# -------------------------- وظائف المساعدة --------------------------

def progress(current, total, message_type="Generic"):
    """
    يعرض تقدم عملية التحميل/الرفع في الطرفية بشكل أوضح.
    يشمل اسم الخيط (Thread), نوع العملية (Download, Channel Upload), ومعرف الرسالة، 
    بالإضافة إلى النسبة المئوية وحجم البيانات.
    """
    # الحصول على اسم الخيط الحالي لتتبع أفضل في السجلات
    thread_name = threading.current_thread().name 
    
    if total > 0:
        percent = current / total * 100
        print(f"[{thread_name}] {message_type}: {percent:.1f}% ({current / (1024 * 1024):.2f}MB / {total / (1024 * 1024):.2f}MB)")
    else:
        # هذه الحالة تحدث عادة في بداية التحميل عندما يكون الحجم الكلي غير معروف بعد،
        # أو لملفات صغيرة جداً حيث Pyrogram لا توفر Total بشكل فوري.
        print(f"[{thread_name}] {message_type}: {current / (1024 * 1024):.2f}MB (Total not yet known)")

def cleanup_downloads():
    """
    تنظيف مجلد التنزيلات عند بدء تشغيل البوت تلقائيًا.
    يحذف جميع الملفات القديمة لضمان بيئة نظيفة وتقليل استهلاك المساحة.
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
app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

# قاموس لتخزين بيانات كل فيديو وارد، 
# المفتاح: button_message_id (معرف رسالة الأزرار التي يرسلها البوت للمستخدم).
# يتم استخدام original_message_id كمفتاح مؤقت في البداية.
user_video_data = {}

# -------------------------- وظائف المعالجة الأساسية --------------------------

def process_video_for_compression(video_data):
    """
    الدالة المسؤولة عن ضغط الفيديو باستخدام FFmpeg ورفعه إلى القناة المحددة.
    هذه الدالة يتم تنفيذها داخل `compression_executor` (في خيط منفصل).
    """
    # لتعقب العملية في السجلات
    print(f"\n[{threading.current_thread().name}] Starting compression for original message ID: {video_data['message'].id} (Button ID: {video_data['button_message_id']}).")
    
    file_path = video_data['file'] # مسار الملف المحمل على الخادم
    message = video_data['message'] # رسالة المستخدم الأصلية (كائن Message)
    button_message_id = video_data['button_message_id'] # معرف رسالة الأزرار
    quality = video_data['quality'] # الجودة المختارة (مثال: 'crf_23')

    temp_compressed_filename = None # متغير لتخزين مسار الملف المضغوط المؤقت

    try:
        # التأكد من أن الملف الأصلي موجود قبل البدء في الضغط
        if not os.path.exists(file_path):
            print(f"[{threading.current_thread().name}] Error: Original file not found at '{file_path}'. Cannot proceed with compression.")
            message.reply_text("حدث خطأ: لم يتم العثور على الملف الأصلي للمعالجة. يرجى المحاولة مرة أخرى.")
            return

        # إنشاء ملف مؤقت لتخزين ناتج الضغط
        # `dir=DOWNLOADS_DIR` يضمن أن يتم إنشاء الملف المؤقت داخل مجلد التنزيلات
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        # بناء أمر FFmpeg بناءً على الجودة المختارة.
        # الإعدادات مأخوذة من `config.py` للحفاظ على المرونة.
        ffmpeg_command = ""
        if quality == "crf_27":  # جودة منخفضة
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v 1200k -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_compressed_filename}"'
            )
        elif quality == "crf_23":  # جودة متوسطة (الافتراضية للاختيار التلقائي)
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
            # رسالة خطأ إذا كانت الجودة غير معروفة (لا ينبغي أن تحدث في الظروف العادية)
            print(f"[{threading.current_thread().name}] Internal error: Invalid compression quality '{quality}'.")
            message.reply_text("حدث خطأ داخلي: جودة ضغط غير صالحة.")
            return

        print(f"[{threading.current_thread().name}][FFmpeg] Executing command for '{os.path.basename(file_path)}':\n{ffmpeg_command}")
        # تنفيذ أمر FFmpeg. `subprocess.run` هو خيار أفضل من `os.system`
        # لأنه يسمح بالتقاط `stdout` و `stderr` للأخطاء.
        process = subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True, text=True, encoding='utf-8')
        print(f"[{threading.current_thread().name}][FFmpeg] Command executed successfully for '{os.path.basename(file_path)}'.")
        # طباعة مخرجات FFmpeg لمزيد من التفاصيل في الطرفية (خاصة الأخطاء التحذيرية)
        if process.stdout:
            print(f"[{threading.current_thread().name}][FFmpeg] Stdout for '{os.path.basename(file_path)}':\n{process.stdout.strip()}")
        if process.stderr:
            print(f"[{threading.current_thread().name}][FFmpeg] Stderr for '{os.path.basename(file_path)}':\n{process.stderr.strip()}")

        # ------------------- حساب حجم الفيديو المضغوط لإظهاره للمستخدم -------------------
        compressed_file_size_mb = 0
        if os.path.exists(temp_compressed_filename):
            compressed_file_size_bytes = os.path.getsize(temp_compressed_filename)
            compressed_file_size_mb = compressed_file_size_bytes / (1024 * 1024) # تحويل من بايت إلى ميجابايت
            print(f"[{threading.current_thread().name}] Compressed file '{os.path.basename(temp_compressed_filename)}' size: {compressed_file_size_mb:.2f} MB")
        else:
            # إذا لم يتم إنشاء الملف المضغوط، يتم إعلام المستخدم والإنهاء
            print(f"[{threading.current_thread().name}] Error: Compressed file {temp_compressed_filename} not found after FFmpeg completion.")
            message.reply_text("حدث خطأ أثناء ضغط الفيديو: لم يتم إنشاء الملف المضغوط بنجاح.")
            return # إنهاء الدالة لأن الملف غير موجود للرفع

        # ------------------- رفع الفيديو المضغوط إلى القناة وإرسال إشعار للمستخدم -------------------
        if CHANNEL_ID:
            try:
                # محاولة إرسال نسخة من الفيديو الأصلي إلى القناة أولاً
                try:
                    app.copy_message(
                        chat_id=CHANNEL_ID,
                        from_chat_id=message.chat.id,
                        message_ids=message.id,
                        caption="الفيديو الأصلي (النسخة الأصلية)"
                    )
                    print(f"[{threading.current_thread().name}] Original video (ID: {message.id}) copied to channel: {CHANNEL_ID}.")
                except (MessageEmpty, UserNotParticipant) as e:
                    print(f"[{threading.current_thread().name}] Warning: Could not copy original message {message.id} to channel {CHANNEL_ID} due to: {e}. Check bot permissions or channel type.")
                except Exception as e:
                    print(f"[{threading.current_thread().name}] Error copying original video to channel: {e}")

                # إرسال الفيديو المضغوط إلى القناة مع وصف يتضمن الحجم الجديد
                sent_to_channel_message = app.send_document(
                    chat_id=CHANNEL_ID,
                    document=temp_compressed_filename,
                    # دالة التقدم الخاصة برفع القناة، تتضمن معرف الرسالة للتتبع
                    progress=lambda current, total: progress(current, total, f"ChannelUpload-MsgID:{message.id}"), 
                    caption=f"الفيديو المضغوط (الجودة: {quality.replace('crf_', 'CRF ')}) \nالحجم: {compressed_file_size_mb:.2f} ميجابايت"
                )
                print(f"[{threading.current_thread().name}] Compressed video uploaded to channel: {CHANNEL_ID} for original message ID {message.id}.")
                
                # إشعار المستخدم بنجاح العملية وإظهار الحجم المضغوط
                message.reply_text(
                    f"✅ تم ضغط الفيديو ورفعه بنجاح إلى القناة!\n"
                    f"الجودة المختارة: **{quality.replace('crf_', 'CRF ')}**\n"
                    f"الحجم الجديد: **{compressed_file_size_mb:.2f} ميجابايت**",
                    quote=True # للرد على رسالة المستخدم الأصلية لربط السياق
                )
            except Exception as e:
                print(f"[{threading.current_thread().name}] Error uploading compressed video to channel {CHANNEL_ID} or sending reply to user: {e}")
                message.reply_text(f"حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة: {e}")
        else:
            print(f"[{threading.current_thread().name}] CHANNEL_ID not configured. Compressed video not sent to channel.")
            # إعلام المستخدم بنجاح الضغط حتى لو لم يكن هناك قناة مخرجة
            message.reply_text(
                f"⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط.\n"
                f"تم ضغط الفيديو بنجاح! (الحجم: **{compressed_file_size_mb:.2f} ميجابايت**) لكن لم يتم رفعه إلى قناة.",
                quote=True
            )

    # ------------------- معالجة الأخطاء -------------------
    except subprocess.CalledProcessError as e:
        print(f"[{threading.current_thread().name}][FFmpeg] Error occurred for '{os.path.basename(file_path)}'!")
        print(f"[{threading.current_thread().name}][FFmpeg] stdout: {e.stdout}")
        print(f"[{threading.current_thread().name}][FFmpeg] stderr: {e.stderr}")
        user_error_message = f"حدث خطأ أثناء ضغط الفيديو:\n`{e.stderr.decode('utf-8', errors='ignore').strip() if e.stderr else 'غير معروف'}`"
        # تقصير رسالة الخطأ إذا كانت طويلة جداً لمنع مشاكل العرض في تيليجرام
        if len(user_error_message) > 500:
            user_error_message = user_error_message[:497] + "..."
        message.reply_text(user_error_message, quote=True)
    except Exception as e:
        print(f"[{threading.current_thread().name}] General error during video processing for '{os.path.basename(file_path)}': {e}")
        message.reply_text(f"حدث خطأ غير متوقع أثناء معالجة الفيديو: `{e}`", quote=True)
    finally:
        # ------------------- تنظيف الملفات المؤقتة -------------------
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"[{threading.current_thread().name}] Deleted original file: {file_path}")
            except Exception as e:
                print(f"[{threading.current_thread().name}] Error deleting original file {file_path}: {e}")
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            try:
                os.remove(temp_compressed_filename)
                print(f"[{threading.current_thread().name}] Deleted temporary compressed file: {temp_compressed_filename}")
            except Exception as e:
                print(f"[{threading.current_thread().name}] Error deleting temporary file {temp_compressed_filename}: {e}")
        
        # ------------------- تنظيف بيانات الفيديو من القاموس -------------------
        # يتم حذف بيانات الفيديو بمجرد الانتهاء من معالجتها بالكامل
        if button_message_id in user_video_data:
            # إلغاء المؤقت (auto-selection timer) إن كان لا يزال نشطاً
            if user_video_data[button_message_id].get('timer') and user_video_data[button_message_id]['timer'].is_alive():
                user_video_data[button_message_id]['timer'].cancel()
            del user_video_data[button_message_id]
            print(f"[{threading.current_thread().name}] Cleaned up data for message ID: {button_message_id}")

def auto_select_medium_quality(button_message_id):
    """
    تُستدعى هذه الدالة بواسطة `threading.Timer` إذا لم يختار المستخدم جودة خلال 30 ثانية.
    تقوم باختيار الجودة المتوسطة تلقائيًا وتُقدم الفيديو للضغط.
    """
    print(f"\n[{threading.current_thread().name}] Auto-select triggered for Button ID: {button_message_id}.")
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        # التأكد أنه لم يتم اختيار الجودة يدوياً بالفعل قبل أن يعمل الاختيار التلقائي
        if not video_data.get('quality_chosen'): 
            print(f"[{threading.current_thread().name}][Auto-Select] Auto-selecting medium quality for message ID: {button_message_id}")
            
            # تعيين الجودة المتوسطة وتحديد أنها قد تم اختيارها
            video_data['quality'] = "crf_23"  # CRF 23 هي الجودة المتوسطة
            video_data['quality_chosen'] = True # لضمان عدم معالجته مرة أخرى يدوياً

            # محاولة تحديث رسالة الأزرار في التيليجرام لإعلام المستخدم بالاختيار التلقائي
            try:
                app.edit_message_reply_markup(
                    chat_id=video_data['message'].chat.id,
                    message_id=button_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم اختيار جودة متوسطة تلقائيًا", callback_data="none")]])
                )
            except Exception as e:
                print(f"[{threading.current_thread().name}] Error updating message reply markup after auto-select: {e}")

            # تقديم مهمة الضغط لـ compression_executor
            print(f"[{threading.current_thread().name}][Auto-Select] Submitting auto-selected video (ID: {button_message_id}) to compression_executor.")
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            print(f"[{threading.current_thread().name}][Auto-Select] Quality already chosen for message ID: {button_message_id}. Skipping auto-selection.")

def cancel_compression_action(button_message_id):
    """
    إلغاء عملية الضغط بناءً على طلب المستخدم.
    تقوم بحذف الملفات ذات الصلة وتنظيف البيانات.
    """
    print(f"\n[{threading.current_thread().name}] Cancellation requested for Button ID: {button_message_id}.")
    if button_message_id in user_video_data:
        video_data = user_video_data.pop(button_message_id) # إزالة البيانات من القاموس
        file_path = video_data.get('file') # مسار الملف الذي تم تنزيله (قد لا يكون موجوداً إذا تم الإلغاء مبكراً)
        
        # إلغاء المؤقت (auto-selection timer) إن كان لا يزال نشطاً
        if video_data.get('timer') and video_data['timer'].is_alive():
            video_data['timer'].cancel()
            print(f"[{threading.current_thread().name}] Timer for message ID {button_message_id} cancelled.")

        # محاولة حذف الملف الأصلي الذي تم تنزيله
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                print(f"[{threading.current_thread().name}] Deleted file after cancellation: {file_path}")
            elif file_path:
                print(f"[{threading.current_thread().name}] File {file_path} not found for deletion during cancellation (it might not have completed downloading).")
        except Exception as e:
            print(f"[{threading.current_thread().name}] Error deleting file {file_path} during cancellation: {e}")
        
        # محاولة حذف رسالة الأزرار التي ظهرت للمستخدم وإعلامه بالإلغاء
        try:
            # استخدام delete_messages بدلاً من get_messages.delete() لسهولة الاستخدام
            app.delete_messages(chat_id=video_data['message'].chat.id, message_ids=button_message_id)
            print(f"[{threading.current_thread().name}] Deleted quality selection message {button_message_id}.")
            video_data['message'].reply_text("❌ تم إلغاء عملية الضغط وحذف الملفات ذات الصلة.", quote=True)
        except Exception as e:
            print(f"[{threading.current_thread().name}] Error deleting messages after cancellation: {e}")
        
        print(f"[{threading.current_thread().name}] Compression canceled for message ID: {button_message_id}")

# -------------------------- معالجات رسائل تيليجرام --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    """الرد على أمر /start للمستخدمين الجدد أو لبدء تفاعل."""
    print(f"[{threading.current_thread().name}] /start command received from user {message.from_user.id}")
    message.reply_text("أهلاً بك! أرسل لي فيديو أو رسوم متحركة (GIF) وسأقوم بضغطه لك.", quote=True)

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    """
    معالجة الفيديوهات والرسوم المتحركة الجديدة المرسلة إلى البوت.
    تقوم بتقديم مهمة تحميل الفيديو إلى `download_executor` لمعالجتها بالتوازي.
    """
    # لتعقب العملية في السجلات
    print(f"\n--- [{threading.current_thread().name}] New Incoming Video ---")
    print(f"[{threading.current_thread().name}] Received video/animation from user {message.from_user.id} (Message ID: {message.id}). Initiating download...")
    
    file_id = message.video.file_id if message.video else message.animation.file_id
    # إنشاء اسم ملف فريد للتنزيل لتجنب التداخل بين التحميلات المتوازية
    file_name_prefix = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{message.id}_{int(time.time())}")
    
    # تقديم مهمة التحميل إلى `download_executor`. هذه العملية لا تمنع معالج الرسائل.
    print(f"[{threading.current_thread().name}] Submitting download for Message ID: {message.id} to download_executor.")
    download_future = download_executor.submit(
        client.download_media,
        file_id,
        file_name=file_name_prefix, 
        # دالة التقدم المخصصة، التي تتضمن معرف الرسالة لتحديد التقدم لكل تحميل
        progress=lambda current, total: progress(current, total, f"Download-MsgID:{message.id}") 
    )
    print(f"[{threading.current_thread().name}] Download submission for Message ID: {message.id} completed. Bot is ready for next incoming message.")

    # تخزين بيانات الفيديو في `user_video_data` باستخدام `message.id` الأصلي كمفتاح مؤقت.
    # سيتم تحديث المفتاح إلى `button_message_id` بعد إرسال رسالة الأزرار.
    user_video_data[message.id] = {
        'message': message, # كائن رسالة المستخدم الأصلية
        'download_future': download_future, # كائن Future الخاص بمهمة التحميل
        'file': None, # مسار الملف الذي سيتم تنزيله (يُعين لاحقاً)
        'button_message_id': None, # معرف رسالة الأزرار (يُعين لاحقاً)
        'timer': None, # مؤقت للاختيار التلقائي
        'quality_chosen': False # علامة لتتبع ما إذا تم اختيار الجودة بالفعل (لمنع المعالجة المزدوجة)
    }
    
    # بدء خيط منفصل (Thread) لمتابعة اكتمال التحميل والقيام بالإجراءات التالية (مثل إرسال الأزرار).
    # `name` يساعد في تتبع الخيوط في السجلات.
    threading.Thread(target=post_download_actions, args=[message.id], name=f"PostDownloadThread-{message.id}").start()

def post_download_actions(original_message_id):
    """
    تتم هذه الدالة بعد اكتمال التحميل لكل فيديو.
    تقوم بانتظار اكتمال التحميل، ثم تُقدم أزرار اختيار الجودة للمستخدم.
    هذه الدالة تعمل في خيط منفصل لعدم حظر المعالج الرئيسي للبوت.
    """
    print(f"\n[{threading.current_thread().name}] Starting post-download actions for original message ID: {original_message_id}")
    # التحقق من أن بيانات الفيديو لا تزال موجودة في `user_video_data` (قد يكون قد تم إلغاؤها بالفعل)
    if original_message_id not in user_video_data:
        print(f"[{threading.current_thread().name}] Data for original message ID {original_message_id} not found in user_video_data. Possibly canceled or already processed.")
        return

    video_data = user_video_data[original_message_id]
    download_future = video_data['download_future']
    message = video_data['message']

    try:
        print(f"[{threading.current_thread().name}] Waiting for download of Message ID: {original_message_id} to complete...")
        # الانتظار حتى اكتمال مهمة التحميل (هذا السطر مانع، لكن فقط لهذا الخيط المحدد)
        file_path = download_future.result() 
        video_data['file'] = file_path # حفظ المسار النهائي للملف المحمل
        print(f"[{threading.current_thread().name}] Download complete for original message ID {original_message_id}. File path: {file_path}")

        # ------------------- إرسال نسخة من الفيديو الأصلي إلى القناة (بعد انتهاء تحميله) -------------------
        if CHANNEL_ID:
            try:
                app.copy_message(
                    chat_id=CHANNEL_ID,
                    from_chat_id=message.chat.id,
                    message_ids=message.id,
                    caption="الفيديو الأصلي (قبل الضغط)" # وصف يوضح أنه النسخة الأصلية
                )
                print(f"[{threading.current_thread().name}] Original video (ID: {message.id}) copied to channel: {CHANNEL_ID}.")
            except (MessageEmpty, UserNotParticipant) as e:
                print(f"[{threading.current_thread().name}] Warning: Could not copy original message {message.id} to channel {CHANNEL_ID} due to: {e}. Check bot permissions or channel type.")
            except Exception as e:
                print(f"[{threading.current_thread().name}] Error copying original video to channel: {e}")

        # ------------------- إعداد أزرار اختيار الجودة للمستخدم -------------------
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("جودة ضعيفة (CRF 27)", callback_data="crf_27"),
                    InlineKeyboardButton("جودة متوسطة (CRF 23)", callback_data="crf_23"),
                    InlineKeyboardButton("جودة عالية (CRF 18)", callback_data="crf_18"),
                ],
                [
                    InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_compression"),
                ]
            ]
        )
        reply_message = message.reply_text(
            "✅ تم تنزيل الفيديو بنجاح.\nيرجى اختيار مستوى الجودة للضغط، أو سيتم اختيار جودة متوسطة تلقائيا بعد **30 ثانية**:",
            reply_markup=markup,
            quote=True # للرد على رسالة المستخدم الأصلية
        )
        
        # ------------------- تحديث مفتاح الفيديو في `user_video_data` -------------------
        # نغير المفتاح من `original_message_id` (الخاص برسالة المستخدم) إلى `button_message_id`
        # (الخاص برسالة الأزرار). هذا ضروري لربط `callback_query` الصحيح بالبيانات الصحيحة.
        video_data['button_message_id'] = reply_message.id
        user_video_data[reply_message.id] = user_video_data.pop(original_message_id) # نقل البيانات وإزالة المدخل القديم

        # ------------------- إعداد مؤقت للاختيار التلقائي (30 ثانية) -------------------
        timer = threading.Timer(30, auto_select_medium_quality, args=[reply_message.id])
        user_video_data[reply_message.id]['timer'] = timer # تخزين المؤقت لتمكين إلغائه لاحقاً
        timer.name = f"AutoSelectTimer-{reply_message.id}" # تسمية خيط المؤقت لتتبع أفضل في السجلات
        timer.start()

        print(f"[{threading.current_thread().name}] Post-download actions completed for Message ID: {original_message_id}.")

    except Exception as e:
        print(f"[{threading.current_thread().name}] Error during post-download actions for original message ID {original_message_id}: {e}")
        # إعلام المستخدم بحدوث خطأ أثناء التنزيل
        message.reply_text(f"حدث خطأ أثناء تنزيل الفيديو الخاص بك: `{e}`")
        # تنظيف أي بيانات وملفات جزئية في حالة حدوث خطأ أثناء هذه المرحلة
        if original_message_id in user_video_data:
            temp_file_path = user_video_data[original_message_id].get('file')
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                print(f"[{threading.current_thread().name}] Cleaned up partial download: {temp_file_path}")
            del user_video_data[original_message_id]


@app.on_callback_query()
def compression_choice_callback(client, callback_query):
    """
    معالجة اختيار الجودة من قبل المستخدم عبر أزرار InlineKeyboard.
    """
    print(f"\n[{threading.current_thread().name}] Callback received for Button ID: {callback_query.message.id}, Data: {callback_query.data}")
    message_id = callback_query.message.id # معرف رسالة الأزرار التي تم الضغط عليها
    
    # التحقق مما إذا كانت بيانات الفيديو لا تزال موجودة في قاموس التتبع
    # (قد تكون قد حذفت إذا انتهت صلاحية الطلب أو تم إلغاؤه مسبقاً)
    if message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب أو تم إلغاؤه مسبقًا.", show_alert=True)
        # محاولة حذف رسالة الأزرار القديمة لعدم إظهارها مرة أخرى
        try:
            callback_query.message.delete()
        except Exception as e:
            print(f"[{threading.current_thread().name}] Could not delete stale callback message {message_id}: {e}")
        return

    video_data = user_video_data[message_id]

    # منع المستخدم من اختيار الجودة أكثر من مرة لنفس الفيديو
    if video_data.get('quality_chosen'):
        callback_query.answer("تم اختيار الجودة مسبقًا لهذا الفيديو. لا يمكن تغييرها الآن.", show_alert=True)
        return

    # معالجة الضغط على زر الإلغاء
    if callback_query.data == "cancel_compression":
        callback_query.answer("يتم إلغاء العملية...", show_alert=False)
        cancel_compression_action(message_id) # استدعاء دالة الإلغاء
        return

    # إيقاف المؤقت التلقائي (auto-selection timer) إذا كان لا يزال نشطاً،
    # وذلك لأن المستخدم قام بالاختيار يدوياً.
    if video_data.get('timer') and video_data['timer'].is_alive():
        video_data['timer'].cancel()
        print(f"[{threading.current_thread().name}] Timer for message ID {message_id} cancelled by user choice.")

    # التحقق مرة أخرى من وجود ملف الفيديو المحمل قبل البدء في الضغط
    # (لتجنب مشاكل إذا كان التحميل لم يكتمل بالرغم من كل شيء)
    if not video_data.get('file') or not os.path.exists(video_data['file']):
        callback_query.answer("لم يكتمل تنزيل الفيديو بعد، يرجى المحاولة لاحقًا أو إعادة إرساله.", show_alert=True)
        # تنظيف رسالة الأزرار إذا كان الملف غير موجود (قد يكون بسبب خطأ تنزيل)
        try:
            app.delete_messages(chat_id=video_data['message'].chat.id, message_ids=message_id)
        except Exception as e:
            print(f"[{threading.current_thread().name}] Could not delete message {message_id}: {e}")
        if message_id in user_video_data: 
            del user_video_data[message_id] # إزالة المدخل من القاموس
        return

    # تعيين الجودة المختارة من قبل المستخدم وتحديث علامة `quality_chosen`
    video_data['quality'] = callback_query.data
    video_data['quality_chosen'] = True

    # إرسال إشعار للمستخدم بأن الاختيار قد تم
    callback_query.answer("تم استلام اختيارك. جاري الضغط...", show_alert=False)

    # تحديث أزرار الرسالة في تيليجرام لتعكس الاختيار وتمنع التفاعل المستقبلي
    try:
        app.edit_message_reply_markup(
            chat_id=callback_query.message.chat.id,
            message_id=message_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ تم اختيار الجودة: {callback_query.data.replace('crf_', 'CRF ')}", callback_data="none")]])
        )
    except Exception as e:
        print(f"[{threading.current_thread().name}] Error editing message reply markup for message ID {message_id}: {e}")

    # تقديم مهمة الضغط لـ `compression_executor` لمعالجتها بشكل متوازي
    print(f"[{threading.current_thread().name}] Submitting compression for Message ID: {video_data['message'].id} (Button ID: {message_id}) to compression_executor.")
    compression_executor.submit(process_video_for_compression, video_data)
    print(f"[{threading.current_thread().name}] Compression submission completed for Button ID: {message_id}.")

# -------------------------- وظائف التشغيل والإدارة --------------------------

# تنفيذ دالة تنظيف مجلد التنزيلات عند بدء تشغيل البوت مباشرة
cleanup_downloads()

def check_channel_on_start():
    """
    تُنفذ هذه الدالة عند بدء تشغيل البوت في خيط منفصل.
    تهدف إلى التحقق من صحة `CHANNEL_ID` في `config.py` وصلاحيات البوت فيه.
    """
    time.sleep(5) # الانتظار بضع ثواني للتأكد من أن البوت يعمل
    if CHANNEL_ID:
        try:
            chat = app.get_chat(CHANNEL_ID) # محاولة الحصول على معلومات القناة
            print(f"[{threading.current_thread().name}] ✅ تم التعرف على القناة بنجاح: '{chat.title}' (ID: {CHANNEL_ID})")
            
            # تحقق إضافي لنوع الدردشة
            if chat.type not in ["channel", "supergroup"]:
                print(f"[{threading.current_thread().name}] ⚠️ ملاحظة: معرف القناة '{CHANNEL_ID}' المحدد في config.py ليس لقناة أو مجموعة خارقة. قد تواجه مشاكل في رفع الملفات إذا لم يكن نوع الدردشة متوقعاً (قد يكون group عادي أو private chat).")
            
            # تحقق من صلاحيات البوت في القناة
            # هذا الفحص ليس مثالياً وقد يختلف بناءً على نسخة Pyrogram أو طبيعة البوت كمسؤول
            if not chat.permissions or not chat.permissions.can_post_messages: 
                 print(f"[{threading.current_thread().name}] ⚠️ ملاحظة: البوت ليس لديه صلاحية نشر الرسائل في القناة '{chat.title}' (ID: {CHANNEL_ID}). يرجى منحه صلاحيات المشرف المطلوبة للنشر.")
        except Exception as e:
            # رسالة خطأ إذا لم يتمكن البوت من الوصول إلى القناة
            print(f"[{threading.current_thread().name}] ❌ خطأ في التعرف على القناة '{CHANNEL_ID}': {e}. يرجى التأكد من أن البوت مشرف في القناة وأن معرف القناة (ID) صحيح ومتاح.")
    else:
        print(f"[{threading.current_thread().name}] ⚠️ لم يتم تحديد CHANNEL_ID في ملف config.py. لن يتم رفع الفيديوهات المضغوطة إلى أي قناة.")

# تشغيل دالة فحص القناة في خيط منفصل، بحيث لا تمنع بدء تشغيل البوت.
# `daemon=True` يسمح بإنهاء هذا الخيط تلقائياً عند إنهاء البرنامج الرئيسي.
threading.Thread(target=check_channel_on_start, daemon=True, name="ChannelCheckThread").start()

# رسالة بدء تشغيل البوت
print("🚀 البوت بدأ العمل! بانتظار الفيديوهات...")
# تشغيل البوت لبدء معالجة الرسائل
app.run()
