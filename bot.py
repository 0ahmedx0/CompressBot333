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
from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

download_executor = ThreadPoolExecutor(max_workers=5) 
compression_executor = ThreadPoolExecutor(max_workers=3) 

# --- [إضافة جديدة] --- قاموس لتخزين إعدادات كل مستخدم ---
# user_settings = { user_id: {'encoder': ..., 'auto_compress': ..., 'auto_quality': ...} }
user_settings = {}
DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',  # الترميز الافتراضي
    'auto_compress': False, # الضغط التلقائي معطل افتراضياً
    'auto_quality': 'crf_23'   # الجودة التلقائية الافتراضية
}

# --- [إضافة جديدة] --- وظيفة مساعدة للحصول على إعدادات المستخدم أو إنشائها
def get_user_settings(user_id):
    """
    تحصل على إعدادات المستخدم من القاموس. إذا لم يكن المستخدم موجودًا،
    تقوم بإنشاء إعدادات افتراضية له.
    """
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]
# -----------------------------------------------------------


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
# user_video_data = { button_message_id: { 'message': ..., 'file': ..., 'quality': ..., 'processing_started': False, 'timer': ..., 'user_id': ... } }
user_video_data = {}

# -------------------------- وظائف المعالجة الأساسية --------------------------

def process_video_for_compression(video_data):
    """
    الدالة المسؤولة عن ضغط الفيديو ورفعه إلى القناة.
    تعمل هذه الدالة داخل `compression_executor`.
    """
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Starting compression for original message ID: {video_data['message'].id} (Button ID: {video_data.get('button_message_id', 'N/A')}).")
    
    file_path = video_data['file']
    message = video_data['message']
    button_message_id = video_data.get('button_message_id')
    quality = video_data['quality']
    user_id = video_data['user_id'] # --- [إضافة جديدة] --- الحصول على هوية المستخدم

    # --- [تعديل] --- استخدام الترميز من إعدادات المستخدم ---
    user_prefs = get_user_settings(user_id)
    encoder = user_prefs['encoder']
    print(f"[{thread_name}] Using encoder '{encoder}' for user {user_id}.")

    # وضع علامة على أن المعالجة لهذا الفيديو قد بدأت.
    # هذا يمنع المستخدم من تغيير الجودة أو إلغاء العملية لهذا الفيديو بعد هذه النقطة.
    if button_message_id and button_message_id in user_video_data: # تأكد أن الفيديو لا يزال موجوداً في البيانات
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
        if not user_prefs['auto_compress']: # لا نطبع هذا التحذير في حالة الضغط التلقائي
          return


    temp_compressed_filename = None

    try:
        if not os.path.exists(file_path):
            print(f"[{thread_name}] Error: Original file not found at '{file_path}'. Cannot proceed with compression.")
            message.reply_text("حدث خطأ: لم يتم العثور على الملف الأصلي للمعالجة. يرجى المحاولة مرة أخرى.")
            return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        # --- [تعديل] --- إعداد أمر FFmpeg باستخدام الترميز المختار ---
        ffmpeg_command = ""
        # بناء الجزء المشترك من الأمر
        common_ffmpeg_part = (
            f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} '
            f'-c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
            f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1'
        )
        
        # إضافة إعدادات الجودة بناءً على الترميز
        if quality == "crf_27":
            quality_settings = "-cq 37 -preset fast" if "nvenc" in encoder else "-crf 27 -preset veryfast"
        elif quality == "crf_23":
            quality_settings = "-cq 23 -preset medium" if "nvenc" in encoder else "-crf 23 -preset medium"
        elif quality == "crf_18":
            quality_settings = "-cq 18 -preset slow" if "nvenc" in encoder else "-crf 18 -preset slow"
        else:
            print(f"[{thread_name}] Internal error: Invalid compression quality '{quality}'.")
            message.reply_text("حدث خطأ داخلي: جودة ضغط غير صالحة.", quote=True)
            return

        ffmpeg_command = f'{common_ffmpeg_part} {quality_settings} "{temp_compressed_filename}"'

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

        # ------------------- رفع الفيديو الأصلي والمضغوط إلى القناة معاً -------------------
        if CHANNEL_ID:
            try:
                sent_to_channel_message = app.send_document(
                    chat_id=CHANNEL_ID,
                    document=temp_compressed_filename,
                    progress=lambda current, total: progress(current, total, f"ChannelUpload-MsgID:{message.id}"),
                    caption=f"📦 الفيديو المضغوط (الجودة: {quality.replace('crf_', 'CRF ')})\nالحجم: {compressed_file_size_mb:.2f} ميجابايت"
                )
                print(f"[{thread_name}] Compressed video uploaded to channel: {CHANNEL_ID} for original message ID {message.id}.")
        
                try:
                    app.copy_message(
                        chat_id=CHANNEL_ID,
                        from_chat_id=message.chat.id,
                        message_id=message.id,
                        caption=" المضغوط اعلا ⬆️🔺🎞️ الفيديو الأصلي"
                    )
                    print(f"[{thread_name}] Original video (ID: {message.id}) copied to channel: {CHANNEL_ID}.")
                except (MessageEmpty, UserNotParticipant) as e:
                    print(f"[{thread_name}] Warning: Could not copy original message {message.id} to channel {CHANNEL_ID} due to: {e}.")
                except Exception as e:
                    print(f"[{thread_name}] Error copying original video to channel: {e}")
        
                message.reply_text(
                    f"✅ تم ضغط الفيديو ورفعه بنجاح إلى القناة!\n"
                    f"الجودة المختارة: **{quality.replace('crf_', 'CRF ')}**\n"
                    f"الحجم الجديد: **{compressed_file_size_mb:.2f} ميجابايت**",
                    quote=True
                )
            except Exception as e:
                print(f"[{thread_name}] Error uploading to channel {CHANNEL_ID} or sending reply to user: {e}")
                message.reply_text(f"حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة: {e}")
        else:
            print(f"[{thread_name}] CHANNEL_ID not configured. Compressed video not sent to channel.")
            message.reply_text(
                f"⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط.\n"
                f"تم ضغط الفيديو بنجاح! (الحجم: **{compressed_file_size_mb:.2f} ميجابايت**) لكن لم يتم رفعه إلى قناة.",
                quote=True
            )

    except subprocess.CalledProcessError as e:
        print(f"[{thread_name}][FFmpeg] error occurred for '{os.path.basename(file_path)}'!")
        print(f"[{thread_name}][FFmpeg] stdout: {e.stdout}")
        print(f"[{thread_name}][FFmpeg] stderr: {e.stderr}")
        user_error_message = f"حدث خطأ أثناء ضغط الفيديو:\n`{e.stderr.strip() if e.stderr else 'غير معروف'}`"
        if len(user_error_message) > 500:
            user_error_message = user_error_message[:497] + "..."
        message.reply_text(user_error_message, quote=True)
    except Exception as e:
        print(f"[{thread_name}] General error during video processing for '{os.path.basename(file_path)}': {e}")
        message.reply_text(f"حدث خطأ غير متوقع أثناء معالجة الفيديو: `{e}`", quote=True)
    finally:
        # ------------------- تنظيف الملفات المؤقتة -------------------
        print(f"[{thread_name}] Preserving original file for further compressions: {file_path}")
        
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            try:
                os.remove(temp_compressed_filename)
                print(f"[{thread_name}] Deleted temporary compressed file: {temp_compressed_filename}")
            except Exception as e:
                print(f"[{thread_name}] Error deleting temporary file {temp_compressed_filename}: {e}")
        
        # --- [تعديل] --- إعادة تعيين بيانات الفيديو أو حذفها نهائيًا ---
        if button_message_id and button_message_id in user_video_data:
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
        else: # في حالة الضغط التلقائي، لا توجد رسالة أزرار لتحديثها، فقط نحذف الملف الأصلي
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"[{thread_name}] Deleted original file after auto-compression: {file_path}")
            if video_data['message'].id in user_video_data:
                 del user_video_data[video_data['message'].id]


def auto_select_medium_quality(button_message_id):
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Auto-select triggered for Button ID: {button_message_id}.")
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        if not video_data.get('processing_started'): 
            print(f"[{thread_name}][Auto-Select] Auto-selecting medium quality for message ID: {button_message_id}")
            video_data['quality'] = "crf_23"
            try:
                app.edit_message_reply_markup(
                    chat_id=video_data['message'].chat.id,
                    message_id=button_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم اختيار جودة متوسطة تلقائيًا", callback_data="none")]])
                )
            except Exception as e:
                print(f"[{thread_name}] Error updating message reply markup after auto-select: {e}")
            print(f"[{thread_name}][Auto-Select] Submitting auto-selected video (ID: {button_message_id}) to compression_executor.")
            compression_executor.submit(process_video_for_compression, video_data)
        else:
            print(f"[{thread_name}][Auto-Select] Processing already started for message ID: {button_message_id}. Skipping auto-selection.")

# -------------------------- معالجات رسائل تيليجرام --------------------------

@app.on_message(filters.command("start"))
def start_command(client, message):
    thread_name = threading.current_thread().name
    print(f"[{thread_name}] /start command received from user {message.from_user.id}")
    # --- [إضافة جديدة] --- إضافة زر الإعدادات
    settings_button = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]])
    message.reply_text(
        "أهلاً بك! أرسل لي فيديو أو رسوم متحركة (GIF) وسأقوم بضغطه لك.\n\n"
        "يمكنك ضبط إعدادات الضغط من خلال زر الإعدادات أدناه.",
        reply_markup=settings_button,
        quote=True
    )

# --- [إضافة جديدة] --- معالج أمر الإعدادات
@app.on_message(filters.command("settings"))
def settings_command(client, message):
    user_id = message.from_user.id
    send_settings_menu(client, message.chat.id, user_id)

def send_settings_menu(client, chat_id, user_id, message_id=None):
    """
    وظيفة لإنشاء وإرسال قائمة الإعدادات.
    """
    settings = get_user_settings(user_id)
    
    # تحديد النص المعبر عن كل إعداد
    encoder_text = {
        'hevc_nvenc': "H.265 (HEVC)",
        'h264_nvenc': "H.264 (NVENC GPU)",
        'libx264': "H.264 (CPU)"
    }.get(settings['encoder'], "غير محدد")

    auto_compress_text = "✅ مفعل" if settings['auto_compress'] else "❌ معطل"
    auto_quality_text = f"CRF {settings['auto_quality'].split('_')[1]}"

    text = (
        "**⚙️ قائمة الإعدادات**\n\n"
        "هنا يمكنك تخصيص طريقة عمل البوت:\n\n"
        f"🔹 **الترميز (Encoder):** `{encoder_text}`\n"
        f"🔸 **الضغط التلقائي:** `{auto_compress_text}`\n"
        f"📊 **الجودة التلقائية:** `{auto_quality_text}` (تُستخدم فقط عند تفعيل الضغط التلقائي)"
    )

    keyboard = [
        [
            InlineKeyboardButton("🔄 تغيير الترميز", callback_data="settings_encoder"),
        ],
        [
            InlineKeyboardButton(f"الضغط التلقائي: {auto_compress_text}", callback_data="settings_toggle_auto"),
            InlineKeyboardButton("📊 تغيير الجودة التلقائية", callback_data="settings_quality"),
        ],
        [
             InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")
        ]
    ]

    # إذا كان هناك message_id، نقوم بتعديل الرسالة، وإلا نرسل رسالة جديدة
    if message_id:
        try:
            client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception: # قد تكون الرسالة نفسها، نتجاهل الخطأ
            pass
    else:
        client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

# ---------------------------------------------------------------------------------

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
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

    # تخزين البيانات الأولية للفيديو مع هوية المستخدم
    user_video_data[message.id] = {
        'message': message,
        'download_future': download_future,
        'file': None,
        'button_message_id': None,
        'timer': None,
        'quality': None,
        'processing_started': False,
        'user_id': message.from_user.id # --- [إضافة جديدة] --- تخزين هوية المستخدم
    }
    
    threading.Thread(target=post_download_actions, args=[message.id], name=f"PostDownloadThread-{message.id}").start()

def post_download_actions(original_message_id):
    """
    تتم هذه الدالة بعد اكتمال التحميل.
    تُظهر أزرار اختيار الجودة أو تبدأ الضغط التلقائي بناءً على إعدادات المستخدم.
    """
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Starting post-download actions for original message ID: {original_message_id}")
    if original_message_id not in user_video_data:
        print(f"[{thread_name}] Data for original message ID {original_message_id} not found. Possibly canceled.")
        return

    video_data = user_video_data[original_message_id]
    download_future = video_data['download_future']
    message = video_data['message']
    user_id = video_data['user_id']

    try:
        print(f"[{thread_name}] Waiting for download of Message ID: {original_message_id} to complete...")
        file_path = download_future.result() 
        video_data['file'] = file_path 
        print(f"[{thread_name}] Download complete for original message ID {original_message_id}. File path: {file_path}")
        
        # --- [تعديل جذري] --- التحقق من إعدادات المستخدم للضغط التلقائي ---
        user_prefs = get_user_settings(user_id)
        if user_prefs['auto_compress']:
            print(f"[{thread_name}] Auto-compression is ON for user {user_id}. Starting compression automatically.")
            video_data['quality'] = user_prefs['auto_quality']
            
            # إرسال رسالة مؤقتة للمستخدم
            status_message = message.reply_text(
                f"✅ تم التنزيل. جاري الضغط تلقائيًا بالجودة المحددة: **{video_data['quality'].replace('crf_', 'CRF ')}**", 
                quote=True
            )
            # يمكن لاحقاً حذف هذه الرسالة بعد انتهاء الضغط if needed
            
            compression_executor.submit(process_video_for_compression, video_data)
            print(f"[{thread_name}] Auto-compression task submitted for user {user_id}.")
            # لا حاجة لتغيير مفتاح القاموس هنا لأننا لا نستخدم رسالة أزرار
        
        else: # إذا كان الضغط التلقائي معطلاً، نتبع السلوك الأصلي
            print(f"[{thread_name}] Auto-compression is OFF for user {user_id}. Displaying quality options.")
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
            
            video_data['button_message_id'] = reply_message.id
            user_video_data[reply_message.id] = user_video_data.pop(original_message_id)

            timer = threading.Timer(300, auto_select_medium_quality, args=[reply_message.id])
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
            del user_video_data[original_message_id]


@app.on_callback_query()
def universal_callback_handler(client, callback_query):
    """
    معالج واحد لجميع استعلامات الأزرار (الضغط والإعدادات).
    """
    thread_name = threading.current_thread().name
    data = callback_query.data
    user_id = callback_query.from_user.id
    message = callback_query.message
    
    # --- [إضافة جديدة] --- قسم معالجة أزرار الإعدادات ---
    if data.startswith("settings"):
        if data == "settings":
            send_settings_menu(client, message.chat.id, user_id, message.id)
        
        elif data == "settings_encoder":
            keyboard = [
                [InlineKeyboardButton("H.265 (HEVC)", callback_data="set_encoder:hevc_nvenc")],
                [InlineKeyboardButton("H.264 (NVENC GPU)", callback_data="set_encoder:h264_nvenc")],
                [InlineKeyboardButton("H.264 (CPU)", callback_data="set_encoder:libx264")],
                [InlineKeyboardButton("« رجوع", callback_data="settings")]
            ]
            message.edit_text("اختر ترميز الفيديو المفضل:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "settings_quality":
            keyboard = [
                [InlineKeyboardButton("ضعيفة (CRF 27)", callback_data="set_quality:crf_27")],
                [InlineKeyboardButton("متوسطة (CRF 23)", callback_data="set_quality:crf_23")],
                [InlineKeyboardButton("عالية (CRF 18)", callback_data="set_quality:crf_18")],
                [InlineKeyboardButton("« رجوع", callback_data="settings")]
            ]
            message.edit_text("اختر الجودة الافتراضية للضغط التلقائي:", reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data == "settings_toggle_auto":
            settings = get_user_settings(user_id)
            settings['auto_compress'] = not settings['auto_compress']
            callback_query.answer(f"الضغط التلقائي الآن {'مفعل' if settings['auto_compress'] else 'معطل'}")
            send_settings_menu(client, message.chat.id, user_id, message.id)
            
        callback_query.answer()
        return

    elif data.startswith("set_"):
        action, value = data.split(":", 1)
        settings = get_user_settings(user_id)
        
        if action == "set_encoder":
            settings['encoder'] = value
            callback_query.answer(f"تم تغيير الترميز إلى {value}")
        elif action == "set_quality":
            settings['auto_quality'] = value
            callback_query.answer(f"تم تغيير الجودة التلقائية إلى {value}")
            
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return

    elif data == "close_settings":
        try:
            message.delete()
        except Exception:
            pass
        return
        
    # --- قسم معالجة أزرار الضغط (الكود الأصلي مع تعديلات طفيفة) ---
    print(f"\n[{thread_name}] Callback received for Button ID: {message.id}, Data: {data}")
    button_message_id = message.id
    
    if button_message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب أو تم إلغاؤه مسبقًا.", show_alert=True)
        try:
            message.delete()
        except Exception as e:
            print(f"[{thread_name}] Could not delete stale callback message {button_message_id}: {e}")
        return

    video_data = user_video_data[button_message_id]

    if video_data.get('processing_started'):
        callback_query.answer("العملية جارية بالفعل، لا يمكن تغيير الجودة الآن.", show_alert=True)
        return

    if data in ["cancel_compression", "finish_process"]:
        callback_query.answer("🚫 يتم إنهاء العملية...", show_alert=False)
        file_path = video_data.get('file')
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"[{thread_name}] Deleted file during finish/cancel: {file_path}")
            except Exception as e:
                print(f"[{thread_name}] Error deleting file during finish/cancel: {e}")
        try:
            message.delete()
            video_data['message'].reply_text("✅ تم إنهاء العملية وحذف الملف المؤقت.", quote=True)
        except Exception as e:
            print(f"[{thread_name}] Error deleting finish/cancel message: {e}")
        if button_message_id in user_video_data:
            if video_data.get('timer') and video_data['timer'].is_alive():
                video_data['timer'].cancel()
            del user_video_data[button_message_id]
        return

    if video_data.get('timer') and video_data['timer'].is_alive():
        video_data['timer'].cancel()
        print(f"[{thread_name}] Timer for message ID {button_message_id} cancelled by user choice.")

    if not video_data.get('file') or not os.path.exists(video_data['file']):
        callback_query.answer("لم يكتمل تنزيل الفيديو بعد، يرجى المحاولة لاحقًا.", show_alert=True)
        try:
            message.delete()
        except Exception as e:
            print(f"[{thread_name}] Could not delete message {button_message_id}: {e}")
        if button_message_id in user_video_data: 
            del user_video_data[button_message_id]
        return

    video_data['quality'] = data
    callback_query.answer("تم استلام اختيارك. جاري الضغط...", show_alert=False)

    try:
        app.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=button_message_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"⏳ جاري الضغط... (الجودة: {data.replace('crf_', 'CRF ')})", callback_data="none")]])
        )
    except Exception as e:
        print(f"[{thread_name}] Error editing message reply markup for message ID {button_message_id}: {e}")

    print(f"[{thread_name}] Submitting compression for Message ID: {video_data['message'].id} (Button ID: {button_message_id}) to compression_executor.")
    compression_executor.submit(process_video_for_compression, video_data)
    print(f"[{thread_name}] Compression submission completed for Button ID: {button_message_id}.")

# -------------------------- وظائف التشغيل والإدارة --------------------------

cleanup_downloads()

def check_channel_on_start():
    time.sleep(5)
    if CHANNEL_ID:
        try:
            chat = app.get_chat(CHANNEL_ID)
            print(f"✅ تم التعرف على القناة بنجاح: '{chat.title}' (ID: {CHANNEL_ID})")
        except Exception as e:
            print(f"❌ خطأ في التعرف على القناة '{CHANNEL_ID}': {e}.")
    else:
        print("⚠️ لم يتم تحديد CHANNEL_ID في ملف config.py.")

threading.Thread(target=check_channel_on_start, daemon=True, name="ChannelCheckThread").start()

print("🚀 البوت بدأ العمل! بانتظار الفيديوهات...")
app.run()
