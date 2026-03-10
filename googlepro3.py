import os
import tempfile
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty, UserNotParticipant

from config import *

# -------------------------- الثوابت والإعدادات --------------------------
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

download_executor = ThreadPoolExecutor(max_workers=5)
compression_executor = ThreadPoolExecutor(max_workers=3)

# قاموس لتخزين "الحالة" الحالية للمستخدم
user_states = {}

# قاموس لتخزين إعدادات كل مستخدم
user_settings = {}
DEFAULT_SETTINGS = {
    'encoder': 'h264_nvenc',
    'auto_compress': False,
    'auto_quality_value': 30
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

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
user_video_data = {}

# -------------------------- وظائف المعالجة الأساسية --------------------------

def process_video_for_compression(video_data):
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Starting compression for original message ID: {video_data['message'].id} (Button ID: {video_data.get('button_message_id', 'N/A')}).")
    
    file_path = video_data['file']
    message = video_data['message']
    button_message_id = video_data.get('button_message_id')
    quality = video_data['quality']
    user_id = video_data['user_id']
    user_prefs = get_user_settings(user_id)
    encoder = user_prefs['encoder']
    print(f"[{thread_name}] Using encoder '{encoder}' for user {user_id} with quality '{quality}'.")

    # تحديد رسالة "جاري الضغط تلقائيًا" لحذفها لاحقًا
    auto_compress_status_message_id = video_data.get('auto_compress_status_message_id')

    if button_message_id and button_message_id in user_video_data:
        user_video_data[button_message_id]['processing_started'] = True
        try:
            quality_display_value = quality if isinstance(quality, int) else quality.split('_')[1]
            app.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=button_message_id,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"⏳ جاري الضغط... (CRF {quality_display_value})", callback_data="none")]])
            )
        except Exception as e:
            print(f"[{thread_name}] Error updating message reply markup to 'processing started': {e}")
    # elif not user_prefs['auto_compress']: # هذا الشرط خاطئ هنا، يمكن أن يؤدي إلى تخطي العملية بالكامل
    #    print(f"[{thread_name}] Video data for {button_message_id} not found when starting compression. Skipping.")
    #    return # هذا الجزء يجب إزالته

    temp_compressed_filename = None

    try:
        if not os.path.exists(file_path):
            message.reply_text("حدث خطأ: لم يتم العثور على الملف الأصلي للمعالجة.")
            return

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
            temp_compressed_filename = temp_file.name

        common_ffmpeg_part = (
            f'ffmpeg -y -i "{file_path}" -c:v {encoder} -pix_fmt {VIDEO_PIXEL_FORMAT} '
            f'-c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
            f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1'
        )
        # ===== هذا هو التعديل المنطقي المطلوب (منطق موحد) =====
        quality_value = 0
        
        # الخطوة 1: الحصول على القيمة الرقمية للجودة
        if isinstance(quality, str) and 'crf_' in quality:
            quality_value = int(quality.split('_')[1])
        elif isinstance(quality, int):
            quality_value = quality
        else:
            message.reply_text("حدث خطأ داخلي: جودة ضغط غير صالحة.", quote=True)
            return
    
        # الخطوة 2: تحديد الإعداد المسبق بناءً على القيمة الرقمية ونوع المرمز
        preset = "fast" # قيمة افتراضية آمنة تعمل على الجميع
        
        if quality_value <= 18:
            preset = "slow"
        elif quality_value <= 23:
            preset = "medium"
        elif quality_value >= 27:
            preset = "veryfast" if encoder == 'libx264' else "fast"
        # القيم بين 24-26 ستستخدم الإعداد الافتراضي "fast"
        # ========================================================
        quality_param = "cq" if "nvenc" in encoder else "crf"
        quality_settings = f'-{quality_param} {quality_value} -preset {preset}'
        ffmpeg_command = f'{common_ffmpeg_part} {quality_settings} "{temp_compressed_filename}"'
        
        print(f"[{thread_name}][FFmpeg] Executing command for '{os.path.basename(file_path)}':\n{ffmpeg_command}")
        process = subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True, text=True, encoding='utf-8')
        if process.stdout: print(f"[{thread_name}][FFmpeg] Stdout for '{os.path.basename(file_path)}':\n{process.stdout.strip()}")
        if process.stderr: print(f"[{thread_name}][FFmpeg] Stderr for '{os.path.basename(file_path)}':\n{process.stderr.strip()}")

        compressed_file_size_mb = os.path.getsize(temp_compressed_filename) / (1024 * 1024)
        print(f"[{thread_name}] Compressed file '{os.path.basename(temp_compressed_filename)}' size: {compressed_file_size_mb:.2f} MB")

        # إرسال الملف المضغوط إلى الدردشة نفسها بدلاً من القناة
        try:
            message.reply_document(
                document=temp_compressed_filename,
                progress=lambda c, t: progress(c, t, f"BotChatUpload-MsgID:{message.id}"),
                caption=f"📦 الفيديو المضغوط (الجودة: CRF {quality_value})\nالحجم: {compressed_file_size_mb:.2f} ميجابايت"
            )
            message.reply_text(
                f"✅ تم ضغط الفيديو ورفعه بنجاح!\n"
                f"الجودة المختارة: **CRF {quality_value}**\n"
                f"الحجم الجديد: **{compressed_file_size_mb:.2f} ميجابايت**", quote=True
            )
        except Exception as e:
            message.reply_text(f"حدث خطأ أثناء الرفع إلى الدردشة: {e}")

    except subprocess.CalledProcessError as e:
        print(f"[{thread_name}][FFmpeg] error occurred for '{os.path.basename(file_path)}'!")
        print(f"[{thread_name}][FFmpeg] stdout: {e.stdout}")
        print(f"[{thread_name}][FFmpeg] stderr: {e.stderr}")
        user_error_message = f"حدث خطأ أثناء ضغط الفيديو:\n`{e.stderr.strip() if e.stderr else 'غير معروف'}`"
        message.reply_text(user_error_message[:4000], quote=True)
    except Exception as e:
        print(f"[{thread_name}] General error during video processing for '{os.path.basename(file_path)}': {e}")
        message.reply_text(f"حدث خطأ غير متوقع أثناء معالجة الفيديو: `{e}`", quote=True)
    finally:
        if temp_compressed_filename and os.path.exists(temp_compressed_filename):
            os.remove(temp_compressed_filename)
        
        # حذف رسالة "تم التنزيل. جاري الضغط تلقائيًا..."
        auto_compress_status_message_id = video_data.get('auto_compress_status_message_id')
        if auto_compress_status_message_id:
            try:
                app.delete_messages(chat_id=message.chat.id, message_ids=auto_compress_status_message_id)
                print(f"[{thread_name}] Deleted auto-compress status message {auto_compress_status_message_id}.")
            except MessageEmpty: # نضيف هذا لتجنب الخطأ إذا حذفت الرسالة من قبل المستخدم
                print(f"[{thread_name}] Auto-compress status message {auto_compress_status_message_id} was already deleted.")
            except Exception as e:
                print(f"[{thread_name}] Error deleting auto-compress status message {auto_compress_status_message_id}: {e}")

        # باقي الكود يجب أن يكون متقدماً بمستوى واحد داخل `finally`
        if button_message_id and button_message_id in user_video_data:
            user_video_data[button_message_id]['processing_started'] = False
            user_video_data[button_message_id]['quality'] = None
            try:
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("ضعيفة (CRF 27)", callback_data="crf_27"),
                     InlineKeyboardButton("متوسطة (CRF 23)", callback_data="crf_23"),
                     InlineKeyboardButton("عالية (CRF 18)", callback_data="crf_18")],
                    [InlineKeyboardButton("❌ إنهاء العملية", callback_data="finish_process")]
                ])
                # يجب أن نتحقق من وجود الرسالة قبل التعديل، فقد تكون حذفت بالخطأ
                app.edit_message_text(
                    chat_id=message.chat.id, message_id=button_message_id,
                    text="🎞️ تم الانتهاء من ضغط الفيديو. يمكنك اختيار جودة أخرى، أو إنهاء العملية:",
                    reply_markup=markup)
            except MessageEmpty: # إذا كانت الرسالة قد حذفت بالفعل
                print(f"[{thread_name}] Message {button_message_id} was already deleted, skipping edit.")
            except Exception as e:
                print(f"[{thread_name}] Error re-displaying quality options: {e}")
        else: # هذه الحالة تحدث عادة للضغط التلقائي
            if os.path.exists(file_path): os.remove(file_path)
            # هذه هي النقطة التي تحتاج إلى ضبط منطق الحذف فيها
            # إذا لم يكن هناك button_message_id (كما في حالة الضغط التلقائي)، نستخدم original_message_id
            # ويجب التأكد أن المفتاح لا يزال موجوداً في القاموس قبل حذفه
            if button_message_id and button_message_id in user_video_data:
                del user_video_data[button_message_id]
            # في حالة الضغط التلقائي، قد لا يكون هناك button_message_id. نعتمد على original_message_id.
            elif video_data['message'].id in user_video_data: # نستخدم المفتاح الأصلي للفيديو هنا
                del user_video_data[video_data['message'].id]
                
def auto_select_medium_quality(button_message_id):
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Auto-select triggered for Button ID: {button_message_id}.")
    if button_message_id in user_video_data:
        video_data = user_video_data[button_message_id]
        if not video_data.get('processing_started'):
            video_data['quality'] = "crf_23"
            try:
                app.edit_message_reply_markup(
                    chat_id=video_data['message'].chat.id, message_id=button_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم اختيار جودة متوسطة تلقائيًا", callback_data="none")]]))
            except Exception: pass
            compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- معالجات رسائل تيليجرام --------------------------
@app.on_message(filters.command("start"))
def start_command(client, message):
    settings_button = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]])
    message.reply_text(
        "أهلاً بك! أرسل لي فيديو أو رسوم متحركة (GIF) وسأقوم بضغطه لك.",
        reply_markup=settings_button, quote=True
    )

@app.on_message(filters.command("settings"))
def settings_command(client, message):
    send_settings_menu(client, message.chat.id, message.from_user.id)

# هذا المعالج يجب أن يأتي بعد معالجات الأوامر
@app.on_message(filters.text)
def handle_custom_quality_input(client, message):
    user_id = message.from_user.id
    if user_id in user_states and user_states[user_id].get("state") == "waiting_for_cq_value":
        prompt_message_id = user_states[user_id].get("prompt_message_id")
        try:
            value = int(message.text)
            if 0 <= value <= 51:
                settings = get_user_settings(user_id)
                settings['auto_quality_value'] = value
                del user_states[user_id]
                message.reply_text(f"✅ تم تحديث قيمة الجودة إلى: **{value}**", quote=True)
                send_settings_menu(client, message.chat.id, user_id, prompt_message_id)
            else:
                message.reply_text("❌ قيمة غير صالحة. الرجاء إدخال رقم بين 0 و 51.", quote=True)
        except ValueError:
            message.reply_text("❌ إدخال غير صالح. الرجاء إرسال رقم صحيح فقط.", quote=True)
        finally:
            try: message.delete()
            except Exception: pass

def send_settings_menu(client, chat_id, user_id, message_id=None):
    settings = get_user_settings(user_id)
    encoder_text = {"hevc_nvenc": "H.265 (HEVC)","h264_nvenc": "H.264 (NVENC)","libx264": "H.264 (CPU)"}.get(settings['encoder'], "-")
    auto_compress_text = "✅ مفعل" if settings['auto_compress'] else "❌ معطل"
    auto_quality_text = settings['auto_quality_value']

    text = (
        "**⚙️ قائمة الإعدادات**\n\n"
        f"🔹 **الترميز (Encoder):** `{encoder_text}`\n"
        f"🔸 **الضغط التلقائي:** `{auto_compress_text}`\n"
        f"📊 **قيمة الجودة التلقائية (CRF/CQ):** `{auto_quality_text}` (تُطبق عند تفعيل الضغط التلقائي)"
    )
    keyboard = [[InlineKeyboardButton("🔄 تغيير الترميز", callback_data="settings_encoder")],
                [InlineKeyboardButton(f"الضغط التلقائي: {auto_compress_text}", callback_data="settings_toggle_auto")],
                [InlineKeyboardButton("✏️ تحديد قيمة الجودة يدويًا", callback_data="settings_custom_quality")],
                [InlineKeyboardButton("✖️ إغلاق", callback_data="close_settings")]]

    if message_id:
        try: client.edit_message_text(chat_id, message_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception: pass
    else:
        client.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.video | filters.animation)
def handle_incoming_video(client, message):
    thread_name = threading.current_thread().name
    print(f"\n--- [{thread_name}] New Incoming Video ---")
    
    file_id = message.video.file_id if message.video else message.animation.file_id
    file_name_prefix = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{message.id}_{int(time.time())}")
    
    download_future = download_executor.submit(
        client.download_media, file_id, file_name=file_name_prefix,
        progress=lambda c, t: progress(c, t, f"Download-MsgID:{message.id}")
    )

    user_video_data[message.id] = {
        'message': message,
        'download_future': download_future,
        'file': None,
        'button_message_id': None,
        'timer': None,
        'quality': None,
        'processing_started': False,
        'user_id': message.from_user.id,
        'auto_compress_status_message_id': None # <--- هذا هو الإضافة الجديدة
    }    
    threading.Thread(target=post_download_actions, args=[message.id], name=f"PostDownloadThread-{message.id}").start()

def post_download_actions(original_message_id):
    thread_name = threading.current_thread().name
    print(f"\n[{thread_name}] Starting post-download actions for original message ID: {original_message_id}")
    if original_message_id not in user_video_data: return

    video_data = user_video_data[original_message_id]
    message = video_data['message']
    user_id = video_data['user_id']

    try:
        file_path = video_data['download_future'].result()
        video_data['file'] = file_path
        print(f"[{thread_name}] Download complete for original message ID {original_message_id}.")
        
        user_prefs = get_user_settings(user_id)
        if user_prefs['auto_compress']:
            video_data['quality'] = user_prefs['auto_quality_value']
            # هنا نقوم بتخزين ID الرسالة التي سنرسلها <--- التعديل هنا
            status_msg = message.reply_text(f"✅ تم التنزيل. جاري الضغط تلقائيًا بالجودة المحددة: **CRF {video_data['quality']}**", quote=True)
            video_data['auto_compress_status_message_id'] = status_msg.id # <--- هذا هو التعديل
            compression_executor.submit(process_video_for_compression, video_data)
            
        else:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("ضعيفة (CRF 27)", callback_data="crf_27"),
                 InlineKeyboardButton("متوسطة (CRF 23)", callback_data="crf_23"),
                 InlineKeyboardButton("عالية (CRF 18)", callback_data="crf_18")],
                [InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_compression")]
            ])
            reply_message = message.reply_text("✅ تم تنزيل الفيديو.\nاختر جودة الضغط، أو سيتم اختيار جودة متوسطة بعد **300 ثانية**:", reply_markup=markup, quote=True)
            video_data['button_message_id'] = reply_message.id
            user_video_data[reply_message.id] = user_video_data.pop(original_message_id)
            timer = threading.Timer(300, auto_select_medium_quality, args=[reply_message.id])
            user_video_data[reply_message.id]['timer'] = timer
            timer.start()
    except Exception as e:
        print(f"[{thread_name}] Error during post-download actions for original message ID {original_message_id}: {e}")
        message.reply_text(f"حدث خطأ أثناء تنزيل الفيديو: `{e}`")
        if original_message_id in user_video_data: del user_video_data[original_message_id]

@app.on_callback_query()
def universal_callback_handler(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    message = callback_query.message
    
    if data.startswith("settings"):
        if data == "settings": send_settings_menu(client, message.chat.id, user_id, message.id)
        elif data == "settings_encoder":
            keyboard = [[InlineKeyboardButton("H.265 (HEVC)", callback_data="set_encoder:hevc_nvenc")],
                        [InlineKeyboardButton("H.264 (NVENC GPU)", callback_data="set_encoder:h264_nvenc")],
                        [InlineKeyboardButton("H.264 (CPU)", callback_data="set_encoder:libx264")],
                        [InlineKeyboardButton("« رجوع", callback_data="settings")]]
            message.edit_text("اختر ترميز الفيديو المفضل:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "settings_custom_quality":
            user_states[user_id] = {"state": "waiting_for_cq_value", "prompt_message_id": message.id}
            cancel_button = InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data="cancel_input")]])
            message.edit_text("أرسل الآن قيمة الجودة التي تريدها (رقم بين 0 و 51).", reply_markup=cancel_button)
        elif data == "settings_toggle_auto":
            settings = get_user_settings(user_id)
            settings['auto_compress'] = not settings['auto_compress']
            callback_query.answer(f"الضغط التلقائي الآن {'مفعل' if settings['auto_compress'] else 'معطل'}")
            send_settings_menu(client, message.chat.id, user_id, message.id)
        callback_query.answer()
        return

    elif data.startswith("set_encoder:"):
        _, value = data.split(":", 1)
        get_user_settings(user_id)['encoder'] = value
        callback_query.answer(f"تم تغيير الترميز إلى {value}")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "cancel_input":
        if user_id in user_states: del user_states[user_id]
        callback_query.answer("تم الإلغاء.")
        send_settings_menu(client, message.chat.id, user_id, message.id)
        return
    elif data == "close_settings":
        try: message.delete()
        except: pass
        return
        
    button_message_id = message.id
    if button_message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب.", show_alert=True)
        try: message.delete()
        except: pass
        return
    video_data = user_video_data[button_message_id]
    if video_data.get('processing_started'):
        callback_query.answer("العملية جارية بالفعل.", show_alert=True)
        return

    if data in ["cancel_compression", "finish_process"]:
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        file_path = video_data.get('file')
        if file_path and os.path.exists(file_path): os.remove(file_path)
        try:
            message.delete()
            video_data['message'].reply_text("✅ تم إنهاء العملية وحذف الملف المؤقت.", quote=True)
        except Exception: pass
        if button_message_id in user_video_data: del user_video_data[button_message_id]
        return

    if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
    if not video_data.get('file') or not os.path.exists(video_data['file']):
        callback_query.answer("لم يكتمل تنزيل الفيديو بعد.", show_alert=True)
        return

    video_data['quality'] = data
    callback_query.answer("تم استلام اختيارك...", show_alert=False)
    
    quality_display_value = data.split('_')[1]
    try:
        app.edit_message_reply_markup(chat_id=message.chat.id, message_id=button_message_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"⏳ جاري الضغط... (CRF {quality_display_value})", callback_data="none")]]))
    except Exception: pass
    compression_executor.submit(process_video_for_compression, video_data)

# -------------------------- وظائف التشغيل والإدارة --------------------------
cleanup_downloads()
print("🚀 البوت بدأ العمل! بانتظار الفيديوهات...")
app.run()
