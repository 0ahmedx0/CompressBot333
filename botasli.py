import os
import tempfile
import subprocess
import threading
import time
import json # تم إضافة الاستيراد لـ JSON

from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty, UserNotParticipant

# استيراد المتغيرات من ملف config.py
from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

download_executor = ThreadPoolExecutor(max_workers=5)
compression_executor = ThreadPoolExecutor(max_workers=3)

# -------------------------- وظائف حفظ/تحميل تفضيلات المستخدم --------------------------
# مسار ملف حفظ تفضيلات المستخدم
USER_PREFS_FILE = 'user_preferences.json'
user_preferences = {} # قاموس لتخزين تفضيلات كل مستخدم

def load_preferences():
    """تحميل تفضيلات المستخدمين من ملف JSON."""
    global user_preferences
    if os.path.exists(USER_PREFS_FILE):
        with open(USER_PREFS_FILE, 'r', encoding='utf-8') as f:
            try:
                user_preferences = json.load(f)
                print("User preferences loaded successfully.")
            except json.JSONDecodeError:
                user_preferences = {}
                print("Error decoding user preferences JSON. Starting with empty preferences.")
    else:
        print("User preferences file not found. Starting with empty preferences.")

def save_preferences():
    """حفظ تفضيلات المستخدمين إلى ملف JSON."""
    with open(USER_PREFS_FILE, 'w', encoding='utf-8') as f:
        json.dump(user_preferences, f, indent=4, ensure_ascii=False)
    print("User preferences saved.")

# -------------------------- وظائف المساعدة --------------------------

def progress(current, total, message_type="Generic"):
    thread_name = threading.current_thread().name
    
    if total > 0:
        percent = current / total * 100
        print(f"[{thread_name}] {message_type}: {percent:.1f}% ({current / (1024 * 1024):.2f}MB / {total / (1024 * 1024):.2f}MB)")
    else:
        print(f"[{thread_name}] {message_type}: {current / (1024 * 1024):.2f}MB (Total not yet known)")

def cleanup_downloads():
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

# قاموس لتخزين بيانات كل فيديو
user_video_data = {}

# -------------------------- وظائف المعالجة الأساسية --------------------------

def process_video_for_compression(video_data):
    """
    الدالة المسؤولة عن ضغط الفيديو ورفعه إلى القناة أو المحادثة الشخصية.
    تعمل هذه الدالة داخل `compression_executor`.
    """
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Starting compression for original message ID: {video_data['message'].id} (Button ID: {video_data['button_message_id']}).")
    
    file_path = video_data['file']
    message = video_data['message']
    button_message_id = video_data['button_message_id']
    quality = video_data['quality']

    # --- جزء التعديل الجديد: تحديد وجهة الرفع ---
    user_id = str(message.from_user.id)
    # الحصول على وجهة المستخدم المحفوظة، الافتراضي هو المحادثة الخاصة إذا لم يحدد شيء
    destination_type = user_preferences.get(user_id, {}).get('destination', 'private_chat') 

    target_chat_id = None
    destination_name_for_reply = "" # اسم الوجهة في رد البوت للمستخدم

    if destination_type == 'channel':
        if CHANNEL_ID:
            target_chat_id = CHANNEL_ID
            destination_name_for_reply = "القناة المحددة"
        else:
            # إذا اختار المستخدم قناة لكن CHANNEL_ID غير معرف، سنرسلها له في الخاص
            target_chat_id = message.chat.id
            destination_name_for_reply = "محادثتك الخاصة (لأن CHANNEL_ID غير معرف)"
            message.reply_text("⚠️ لقد اخترت إرسال الفيديوهات للقناة، لكن معرف القناة (CHANNEL_ID) لم يتم تهيئته في البوت. سيتم إرسال الفيديو إلى محادثتك الخاصة بدلاً من ذلك.", quote=True)
    else: # private_chat
        target_chat_id = message.chat.id
        destination_name_for_reply = "المحادثة الخاصة معي"

    if not target_chat_id:
        print(f"[{thread_name}] Critical error: No target chat ID determined for user {user_id}. Aborting upload.")
        message.reply_text("حدث خطأ في تحديد وجهة الرفع. لم يتم إرسال الفيديو.", quote=True)
        return
    # --- نهاية جزء التعديل الجديد ---

    # وضع علامة على أن المعالجة لهذا الفيديو قد بدأت.
    # هذا يمنع المستخدم من تغيير الجودة أو إلغاء العملية لهذا الفيديو بعد هذه النقطة.
    if button_message_id in user_video_data: # تأكد أن الفيديو لا يزال موجوداً في البيانات
        user_video_data[button_message_id]['processing_started'] = True
        # نحدّث رسالة الأزرار لتشير إلى بدء المعالجة
        try:
            app.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=button_message_id,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"⏳ جاري الضغط... (الجودة: {quality.replace('crf_', 'CRF ')})", callback_data="none")]])
            )
        except Exception as e:
            print(f"[{thread_name}] Error updating message reply markup to 'processing started': {e}")
            
    # إذا لم يكن الفيديو موجوداً، ربما تم إلغاؤه بالفعل
    else:
        print(f"[{thread_name}] Video data for {button_message_id} not found when starting compression. Skipping.")
        return


    temp_compressed_filename = None

    try:
        if not os.path.exists(file_path):
            print(f"[{thread_name}] Error: Original file not found at '{file_path}'. Cannot proceed with compression.")
            message.reply_text("حدث خطأ: لم يتم العثور على الملف الأصلي للمعالجة. يرجى المحاولة مرة أخرى.")
            return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        ffmpeg_command = ""
        if quality == "crf_27":
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v 1200k -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_compressed_filename}"'
            )
        elif quality == "crf_23":
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v 1700k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_compressed_filename}"'
            )
        elif quality == "crf_18":
            ffmpeg_command = (
                f'ffmpeg -y -i "{file_path}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} '
                f'-b:v 2200k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
                f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_compressed_filename}"'
            )
        else:
            print(f"[{thread_name}] Internal error: Invalid compression quality '{quality}'.")
            message.reply_text("حدث خطأ داخلي: جودة ضغط غير صالحة.")
            return

        print(f"[{thread_name}][FFmpeg] Executing command for '{os.path.basename(file_path)}':\n{ffmpeg_command}")
        process = subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True, text=True, encoding='utf-8')
        print(f"[{thread_name}][FFmpeg] Command executed successfully for '{os.path.basename(file_path)}'.")
        if process.stdout:
            print(f"[{thread_name}][FFmpeg] Stdout for '{os.path.basename(file_path)}':\n{process.stdout.strip()}")
        if process.stderr:
            print(f"[{thread_name}][FFmpeg] Stderr for '{os.path.basename(file_path)}':\n{process.stderr.strip()}")

        compressed_file_size_mb = 0
        if os.path.exists(temp_compressed_filename):
            compressed_file_size_bytes = os.path.getsize(temp_compressed_filename)
            compressed_file_size_mb = compressed_file_size_bytes / (1024 * 1024)
            print(f"[{thread_name}] Compressed file '{os.path.basename(temp_compressed_filename)}' size: {compressed_file_size_mb:.2f} MB")
        else:
            print(f"[{thread_name}] Error: Compressed file {temp_compressed_filename} not found after FFmpeg completion.")
            message.reply_text("حدث خطأ أثناء ضغط الفيديو: لم يتم إنشاء الملف المضغوط بنجاح.")
            return

        # ------------------- رفع الفيديو الأصلي والمضغوط -------------------
        # هنا نستخدم target_chat_id الذي تم تحديده بناءً على تفضيل المستخدم
        try:
            # 1. رفع الفيديو المضغوط أولاً
            sent_document_message = app.send_document(
                chat_id=target_chat_id, # استخدام معرف الشات المستهدف
                document=temp_compressed_filename,
                progress=lambda current, total: progress(current, total, f"Upload-MsgID:{message.id}"),
                caption=f"📦 الفيديو المضغوط (الجودة: {quality.replace('crf_', 'CRF ')})\nالحجم: {compressed_file_size_mb:.2f} ميجابايت"
            )
            print(f"[{thread_name}] Compressed video uploaded to {destination_type} ({target_chat_id}) for original message ID {message.id}.")
    
            # 2. ثم رفع الفيديو الأصلي بعد المضغوط
            try:
                if destination_type == 'private_chat':
                    # إذا كانت الوجهة هي المحادثة الخاصة، يمكن ربط الفيديو الأصلي برسالة الفيديو المضغوط
                    app.copy_message(
                        chat_id=target_chat_id,
                        from_chat_id=message.chat.id,
                        message_id=message.id,
                        caption=" المضغوط أعلاه ⬆️🔺🎞️ الفيديو الأصلي",
                        reply_to_message_id=sent_document_message.id # لربطها بالرسالة المضغوطة
                    )
                else: # للقناة لا يوجد رد مباشر، فقط نسخ
                    app.copy_message(
                        chat_id=target_chat_id,
                        from_chat_id=message.chat.id,
                        message_id=message.id,
                        caption=" المضغوط أعلاه ⬆️🔺🎞️ الفيديو الأصلي"
                    )
                print(f"[{thread_name}] Original video (ID: {message.id}) copied to {destination_type} ({target_chat_id}).")
            except (MessageEmpty, UserNotParticipant) as e:
                print(f"[{thread_name}] Warning: Could not copy original message {message.id} to {destination_type} {target_chat_id} due to: {e}. Check bot permissions or channel type.")
            except Exception as e:
                print(f"[{thread_name}] Error copying original video to {destination_type}: {e}")
    
            # إشعار المستخدم بنجاح العملية (دائماً يرسل إلى محادثة المستخدم الشخصية)
            message.reply_text(
                f"✅ تم ضغط الفيديو ورفعه بنجاح إلى **{destination_name_for_reply}**!\n"
                f"الجودة المختارة: **{quality.replace('crf_', 'CRF ')}**\n"
                f"الحجم الجديد: **{compressed_file_size_mb:.2f} ميجابايت**",
                quote=True
            )
        except Exception as e:
            print(f"[{thread_name}] Error uploading to {destination_type} {target_chat_id} or sending reply to user: {e}")
            message.reply_text(f"حدث خطأ أثناء رفع الفيديو المضغوط إلى وجهتك: {e}")

    except subprocess.CalledProcessError as e:
        print(f"[{thread_name}][FFmpeg] error occurred for '{os.path.basename(file_path)}'!")
        print(f"[{thread_name}][FFmpeg] stdout: {e.stdout}")
        print(f"[{thread_name}][FFmpeg] stderr: {e.stderr}")
        user_error_message = f"حدث خطأ أثناء ضغط الفيديو:\n`{e.stderr.decode('utf-8', errors='ignore').strip() if e.stderr else 'غير معروف'}`"
        if len(user_error_message) > 500:
            user_error_message = user_error_message[:497] + "..."
        message.reply_text(user_error_message, quote=True)
    except Exception as e:
        print(f"[{thread_name}] General error during video processing for '{os.path.basename(file_path)}': {e}")
        message.reply_text(f"حدث خطأ غير متوقع أثناء معالجة الفيديو: `{e}`", quote=True)
    finally:
        # ------------------- تنظيف الملفات المؤقتة -------------------
# لا نحذف الملف الأصلي الآن حتى نسمح للمستخدم بإعادة اختيار الجودة
        print(f"[{thread_name}] Preserving original file for further compressions: {file_path}")
        
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            try:
                os.remove(temp_compressed_filename)
                print(f"[{thread_name}] Deleted temporary compressed file: {temp_compressed_filename}")
            except Exception as e:
                print(f"[{thread_name}] Error deleting temporary file {temp_compressed_filename}: {e}")
        
        # ------------------- إعادة تعيين بيانات الفيديو لإتاحة اختيار جودة أخرى -------------------
        if button_message_id in user_video_data:
            # إعادة ضبط العلامات للسماح بإعادة الاختيار
            user_video_data[button_message_id]['processing_started'] = False
            user_video_data[button_message_id]['quality'] = None
        
            try:
                markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("جودة ضعيفة (CRF 27)", callback_data="crf_27"),
                            InlineKeyboardButton("جودة متوسطة (CRF 23)", callback_data="crf_23"),
                            InlineKeyboardButton("جودة عالية (CRF 18)", callback_data="crf_18"),
                        ],
                        [
                            InlineKeyboardButton("❌ إنهاء العملية", callback_data="finish_process"),
                        ]
                    ]
                )
                app.edit_message_text(
                    chat_id=video_data['message'].chat.id,
                    message_id=button_message_id,
                    text="🎞️ تم الانتهاء من ضغط الفيديو. يمكنك اختيار جودة أخرى، أو إنهاء العملية:",
                    reply_markup=markup
                )
            except Exception as e:
                print(f"[{thread_name}] Error re-displaying quality options: {e}")

def auto_select_medium_quality(button_message_id):
    """
    وظيفة للاختيار التلقائي للجودة المتوسطة إذا لم يختار المستخدم خلال 30 ثانية.
    """
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Auto-select triggered for Button ID: {button_message_id}.")
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        # الشرط المهم: تأكد أن المعالجة لم تبدأ بعد
        if not video_data.get('processing_started'): 
            print(f"[{thread_name}][Auto-Select] Auto-selecting medium quality for message ID: {button_message_id}")
            
            video_data['quality'] = "crf_23" # اختيار الجودة المتوسطة تلقائيًا

            # تحديث رسالة الأزرار في التيليجرام لإعلام المستخدم بالاختيار التلقائي
            try:
                app.edit_message_reply_markup(
                    chat_id=video_data['message'].chat.id,
                    message_id=button_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم اختيار جودة متوسطة تلقائيًا", callback_data="none")]])
                )
            except Exception as e:
                print(f"[{thread_name}] Error updating message reply markup after auto-select: {e}")

            # تقديم مهمة الضغط لـ compression_executor
            print(f"[{thread_name}][Auto-Select] Submitting auto-selected video (ID: {button_message_id}) to compression_executor.")
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            print(f"[{thread_name}][Auto-Select] Processing already started for message ID: {button_message_id}. Skipping auto-selection.")

def cancel_compression_action(button_message_id):
    """
    إلغاء عملية الضغط بناءً على طلب المستخدم.
    """
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Cancellation requested for Button ID: {button_message_id}.")
    
    # التأكد من أن بيانات الفيديو موجودة وأن الضغط لم يبدأ بعد.
    # إذا بدأ الضغط، لن نسمح بالإلغاء هنا.
    if button_message_id in user_video_data and not user_video_data[button_message_id].get('processing_started'):
        video_data = user_video_data.pop(button_message_id)
        file_path = video_data.get('file') 
        
        # إلغاء المؤقت (auto-selection timer) إن كان لا يزال نشطاً
        if video_data.get('timer') and video_data['timer'].is_alive():
            video_data['timer'].cancel()
            print(f"[{thread_name}] Timer for message ID {button_message_id} cancelled.")

        # محاولة حذف الملف الأصلي الذي تم تنزيله
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                print(f"[{thread_name}] Deleted file after cancellation: {file_path}")
            elif file_path:
                print(f"[{thread_name}] File {file_path} not found for deletion during cancellation (may not have downloaded yet).")
        except Exception as e:
            print(f"[{thread_name}] Error deleting file {file_path} during cancellation: {e}")
        
        # محاولة حذف رسالة الأزرار وإعلام المستخدم
        try:
            app.delete_messages(chat_id=video_data['message'].chat.id, message_ids=button_message_id)
            print(f"[{thread_name}] Deleted quality selection message {button_message_id}.")
            video_data['message'].reply_text("❌ تم إلغاء عملية الضغط وحذف الملفات ذات الصلة.", quote=True)
        except Exception as e:
            print(f"[{thread_name}] Error deleting messages after cancellation: {e}")
        
        print(f"[{thread_name}] Compression canceled for message ID: {button_message_id}")
    elif button_message_id in user_video_data and user_video_data[button_message_id].get('processing_started'):
        print(f"[{thread_name}] Cancellation denied for Button ID: {button_message_id}. Processing has already started.")
    else:
        print(f"[{thread_name}] No video data found or invalid state for cancellation of Button ID: {button_message_id}.")


# -------------------------- معالجات رسائل تيليجرام --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    thread_name = threading.current_thread().name
    user_id = str(message.from_user.id) # نحولها لـ string لأن مفاتيح JSON يجب أن تكون string

    print(f"[{thread_name}] /start command received from user {user_id}")

    # زر تغيير الوجهة سيكون متاحاً دائماً
    change_destination_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ تغيير وجهة الرفع", callback_data="change_destination_menu")
        ]
    ])

    # التحقق مما إذا كان المستخدم قد اختار وجهة من قبل
    if user_id not in user_preferences or 'destination' not in user_preferences[user_id]:
        # المستخدم لأول مرة أو لم يختر وجهة بعد
        message.reply_text(
            "أهلاً بك! قبل أن نبدأ، أين تفضل أن أرسل لك الفيديوهات المضغوطة؟",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("إلى القناة المحددة 📤", callback_data="set_destination_channel")
                ],
                [
                    InlineKeyboardButton("إلى هذه المحادثة الخاصة 💬", callback_data="set_destination_private")
                ]
            ]),
            quote=True
        )
        # التأكد من وجود مفتاح المستخدم في القاموس
        user_preferences[user_id] = user_preferences.get(user_id, {})
    else:
        # المستخدم قام بالاختيار مسبقاً
        current_dest_name = "القناة المحددة" if user_preferences[user_id]['destination'] == 'channel' else "المحادثة الخاصة معي"
        message.reply_text(
            f"أهلاً بك! وجهة إرسال الفيديوهات الحالية هي: **{current_dest_name}**.\n\n"
            "أرسل لي فيديو أو رسوم متحركة (GIF) وسأقوم بضغطه لك.",
            reply_markup=change_destination_keyboard,
            quote=True
        )

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    """
    معالجة الفيديوهات والرسوم المتحركة الجديدة المرسلة.
    تبدأ عملية التحميل بشكل متوازي وتعد البيانات للمراحل التالية.
    """
    thread_name = threading.current_thread().name
    print(f"\n--- [{thread_name}] New Incoming Video ---")
    print(f"[{thread_name}] Received video/animation from user {message.from_user.id} (Message ID: {message.id}). Initiating download...")
    
    file_id = message.video.file_id if message.video else message.animation.file_id
    file_name_prefix = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{message.id}_{int(time.time())}")
    
    print(f"[{thread_name}] Submitting download for Message ID: {message.id} to download_executor.")
    download_future = download_executor.submit(
        client.download_media,
        file_id,
        file_name=file_name_prefix, 
        progress=lambda current, total: progress(current, total, f"Download-MsgID:{message.id}") 
    )
    print(f"[{thread_name}] Download submission for Message ID: {message.id} completed. Bot is ready for next incoming message.")

    # تخزين البيانات الأولية للفيديو
    user_video_data[message.id] = {
        'message': message,
        'download_future': download_future,
        'file': None,
        'button_message_id': None,
        'timer': None,
        'quality': None, # تم إضافتها لسهولة التتبع
        'processing_started': False # علامة جديدة لتتبع بدء الضغط الفعلي
    }
    
    threading.Thread(target=post_download_actions, args=[message.id], name=f"PostDownloadThread-{message.id}").start()

def post_download_actions(original_message_id):
    """
    تتم هذه الدالة بعد اكتمال التحميل.
    تُظهر أزرار اختيار الجودة وتُضبط المؤقت التلقائي.
    """
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Starting post-download actions for original message ID: {original_message_id}")
    if original_message_id not in user_video_data:
        print(f"[{thread_name}] Data for original message ID {original_message_id} not found in user_video_data. Possibly canceled.")
        return

    video_data = user_video_data[original_message_id]
    download_future = video_data['download_future']
    message = video_data['message']

    try:
        print(f"[{thread_name}] Waiting for download of Message ID: {original_message_id} to complete...")
        file_path = download_future.result() 
        video_data['file'] = file_path 
        print(f"[{thread_name}] Download complete for original message ID {original_message_id}. File path: {file_path}")

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
            quote=True
        )
        
        # تحديث المفتاح في القاموس user_video_data من original_message_id إلى button_message_id
        video_data['button_message_id'] = reply_message.id
        user_video_data[reply_message.id] = user_video_data.pop(original_message_id)

        timer = threading.Timer(500, auto_select_medium_quality, args=[reply_message.id])
        user_video_data[reply_message.id]['timer'] = timer
        timer.name = f"AutoSelectTimer-{reply_message.id}"
        timer.start()

        print(f"[{thread_name}] Post-download actions completed for Message ID: {original_message_id}.")

    except Exception as e:
        print(f"[{thread_name}] Error during post-download actions for original message ID {original_message_id}: {e}")
        message.reply_text(f"حدث خطأ أثناء تنزيل الفيديو الخاص بك: `{e}`")
        if original_message_id in user_video_data:
            temp_file_path = user_video_data[original_message_id].get('file')
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                print(f"[{thread_name}] Cleaned up partial download: {temp_file_path}")
            del user_video_data[original_message_id]


@app.on_callback_query(filters.regex(r"^(set_destination_|change_destination_menu)"))
def destination_choice_callback(client, callback_query):
    """
    معالجة استعلام اختيار الوجهة من قبل المستخدم.
    """
    thread_name = threading.current_thread().name
    user_id = str(callback_query.from_user.id)
    data = callback_query.data
    print(f"[{thread_name}] Destination Callback received from User ID: {user_id}, Data: {data}")

    # لوحة مفاتيح خيارات الوجهة
    destination_options_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("إلى القناة المحددة 📤", callback_data="set_destination_channel")
        ],
        [
            InlineKeyboardButton("إلى هذه المحادثة الخاصة 💬", callback_data="set_destination_private")
        ]
    ])

    # زر الرجوع/التغيير الذي يظهر دائماً
    change_destination_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ تغيير وجهة الرفع", callback_data="change_destination_menu")
        ]
    ])

    # عند الضغط على "تغيير وجهة الرفع"
    if data == "change_destination_menu":
        callback_query.answer("اختر الوجهة الجديدة:", show_alert=False)
        callback_query.edit_message_text(
            "يرجى اختيار وجهة إرسال الفيديوهات المضغوطة:",
            reply_markup=destination_options_keyboard
        )
        return

    # عند اختيار وجهة جديدة
    destination_type = ""
    destination_name = ""
    if data == "set_destination_channel":
        if not CHANNEL_ID:
            callback_query.answer("⚠️ لم يتم تحديد معرف القناة في إعدادات البوت! لا يمكن تعيين القناة كوجهة.", show_alert=True)
            # يمكن هنا إظهار خيار للمحادثة الخاصة بدلاً من القناة إذا لم يتم تعيين CHANNEL_ID
            callback_query.edit_message_text(
                "⚠️ لم يتم تحديد معرف القناة (CHANNEL_ID) في إعدادات البوت. يرجى التواصل مع المطور أو اختيار المحادثة الخاصة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلى هذه المحادثة الخاصة 💬", callback_data="set_destination_private")]])
            )
            return
        destination_type = "channel"
        destination_name = "القناة المحددة"
    elif data == "set_destination_private":
        destination_type = "private_chat"
        destination_name = "المحادثة الخاصة معي"
    else:
        callback_query.answer("خطأ: اختيار وجهة غير صالح.", show_alert=True)
        return

    # حفظ التفضيلات وتحديث رسالة المستخدم
    user_preferences.setdefault(user_id, {})['destination'] = destination_type
    save_preferences()

    callback_query.answer(f"✅ تم تعيين وجهة الرفع إلى: {destination_name}", show_alert=False)

    # تحديث رسالة الكولباك
    callback_query.edit_message_text(
        f"✅ تم تعيين وجهة إرسال الفيديوهات إلى: **{destination_name}**.\n\n"
        "الآن يمكنك إرسال فيديوهات لضغطها.",
        reply_markup=change_destination_keyboard
    )

@app.on_callback_query()
def compression_choice_callback(client, callback_query):
    """
    معالجة استعلام اختيار الجودة من قبل المستخدم.
    """
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Callback received for Button ID: {callback_query.message.id}, Data: {callback_query.data}")
    message_id = callback_query.message.id # هذا هو button_message_id
    
    if message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب أو تم إلغاؤه مسبقًا.", show_alert=True)
        try:
            callback_query.message.delete()
        except Exception as e:
            print(f"[{thread_name}] Could not delete stale callback message {message_id}: {e}")
        return

    video_data = user_video_data[message_id]

    # الشرط هنا تغير: نمنع إعادة التفاعل فقط إذا كانت عملية الضغط قد بدأت بالفعل.
    # إذا لم تبدأ بعد، يُسمح للمستخدم بتغيير الجودة أو الإلغاء.
    if video_data.get('processing_started'):
        callback_query.answer("العملية جارية بالفعل، لا يمكن تغيير الجودة الآن.", show_alert=True)
        return

    # معالجة الضغط على زر الإلغاء أو الإنهاء
    if callback_query.data in ["cancel_compression", "finish_process"]:
        callback_query.answer("🚫 يتم إنهاء العملية...", show_alert=False)
    
        # حذف الملف (إن وُجد)
        file_path = video_data.get('file')
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"[{thread_name}] Deleted file during finish/cancel: {file_path}")
            except Exception as e:
                print(f"[{thread_name}] Error deleting file during finish/cancel: {e}")
    
        # حذف الرسالة من تيليجرام
        try:
            app.delete_messages(chat_id=video_data['message'].chat.id, message_ids=message_id)
            video_data['message'].reply_text("✅ تم إنهاء العملية وحذف الملف المؤقت.", quote=True)
        except Exception as e:
            print(f"[{thread_name}] Error deleting finish/cancel message: {e}")
    
        # حذف البيانات من الذاكرة
        if message_id in user_video_data:
            if video_data.get('timer') and video_data['timer'].is_alive():
                video_data['timer'].cancel()
            del user_video_data[message_id]
    
        return

    # إيقاف المؤقت التلقائي لأن المستخدم اختار يدوياً
    if video_data.get('timer') and video_data['timer'].is_alive():
        video_data['timer'].cancel()
        print(f"[{thread_name}] Timer for message ID {message_id} cancelled by user choice.")

    # تأكيد أن ملف الفيديو موجود وجاهز للضغط
    if not video_data.get('file') or not os.path.exists(video_data['file']):
        callback_query.answer("لم يكتمل تنزيل الفيديو بعد، يرجى المحاولة لاحقًا أو إعادة إرساله.", show_alert=True)
        try:
            app.delete_messages(chat_id=video_data['message'].chat.id, message_ids=message_id)
        except Exception as e:
            print(f"[{thread_name}] Could not delete message {message_id}: {e}")
        if message_id in user_video_data: 
            del user_video_data[message_id]
        return

    # تعيين الجودة المختارة
    video_data['quality'] = callback_query.data
    
    callback_query.answer("تم استلام اختيارك. جاري الضغط...", show_alert=False)

    # تحديث الأزرار لتجنب التفاعل المستقبلي أو لتأكيد الاختيار
    try:
        app.edit_message_reply_markup(
            chat_id=callback_query.message.chat.id,
            message_id=message_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"⏳ جاري الضغط... (الجودة: {callback_query.data.replace('crf_', 'CRF ')})", callback_data="none")]])
        )
    except Exception as e:
        print(f"[{thread_name}] Error editing message reply markup for message ID {message_id}: {e}")

    # تقديم مهمة الضغط لـ compression_executor
    print(f"[{thread_name}] Submitting compression for Message ID: {video_data['message'].id} (Button ID: {message_id}) to compression_executor.")
    compression_executor.submit(process_video_for_compression, video_data)
    print(f"[{thread_name}] Compression submission completed for Button ID: {message_id}.")

# -------------------------- وظائف التشغيل والإدارة --------------------------

cleanup_downloads()
load_preferences() # *** مهم: تحميل التفضيلات عند بدء البوت ***

def check_channel_on_start():
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت
    time.sleep(5)
    if CHANNEL_ID:
        try:
            chat = app.get_chat(CHANNEL_ID)
            print(f"✅ تم التعرف على القناة بنجاح: '{chat.title}' (ID: {CHANNEL_ID})")
            if chat.type not in ["channel", "supergroup"]:
                print("⚠️ ملاحظة: ID القناة المحدد ليس لقناة أو مجموعة خارقة.")
            # يمكنك إضافة تحقق من صلاحيات البوت كإشرافي هنا
            # member = app.get_chat_member(CHANNEL_ID, app.get_me().id)
            # if not member.can_post_messages:
            #     print(f"⚠️ ملاحظة: البوت ليس لديه صلاحية نشر الرسائل في القناة '{chat.title}'.")
        except Exception as e:
            print(f"❌ خطأ في التعرف على القناة '{CHANNEL_ID}': {e}. يرجى التأكد من أن البوت مشرف في القناة وأن ID صحيح.")
    else:
        print("⚠️ لم يتم تحديد CHANNEL_ID في ملف config.py. لن يتم رفع الفيديوهات إلى قناة إلا إذا تم اختيار المحادثة الخاصة.")

threading.Thread(target=check_channel_on_start, daemon=True, name="ChannelCheckThread").start()

print("🚀 البوت بدأ العمل! بانتظار الفيديوهات...")
app.run()
