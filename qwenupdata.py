import os
import tempfile
import subprocess
import threading
import time
import re
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

def estimate_crf_for_target_size(file_path, target_size_mb, initial_crf=23):
    """
    تقدير قيمة CRF للوصول إلى حجم معين.
    هذه دالة بسيطة، يمكنك تحسينها لاحقًا.
    """
    original_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    
    # تقريب بسيط: كلما قللنا CRF بنسبة معينة، يزيد الحجم
    # العكس صحيح: لخفض الحجم، نحتاج إلى زيادة CRF
    if original_size_mb > target_size_mb:
        ratio = original_size_mb / target_size_mb
        estimated_crf = min(51, max(18, initial_crf + int((ratio - 1) * 5)))
    else:
        ratio = target_size_mb / original_size_mb
        estimated_crf = max(0, min(23, initial_crf - int((ratio - 1) * 5)))
    
    return estimated_crf

def create_progress_bar(percentage):
    """Creates a visual progress bar string."""
    filled_blocks = int(percentage // 5)  # 20 blocks for 100%
    empty_blocks = 20 - filled_blocks
    bar = "█" * filled_blocks + "░" * empty_blocks
    return f"[{bar}] {percentage:.1f}%"

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
            # تحديث الرسالة بناءً على نوع الضغط
            if isinstance(quality, dict) and 'target_size' in quality:
                status_text = f"⏳ جاري الضغط للوصول لحجم ~{quality['target_size']} ميجابايت..."
            else:
                quality_display_value = quality if isinstance(quality, int) else quality.split('_')[1]
                status_text = f"⏳ جاري الضغط... (CRF {quality_display_value})"
            
            app.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=button_message_id,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(status_text, callback_data="none")]])
            )
        except Exception as e:
            print(f"[{thread_name}] Error updating message reply markup to 'processing started': {e}")

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
            f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -map_metadata -1'
        )
        
        # تحديد القيمة بناءً على نوع الضغط
        if isinstance(quality, dict) and 'target_size' in quality:
            target_size_mb = quality['target_size']
            estimated_crf = estimate_crf_for_target_size(file_path, target_size_mb)
            print(f"[{thread_name}] Estimated CRF {estimated_crf} for target size {target_size_mb} MB")
            quality_value = estimated_crf
        else:
            # نفس المنطق الأصلي
            quality_value = 0
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
        
        # إرسال رسالة تتبع التقدم
        progress_msg = message.reply_text("🔄 جاري ضغط الفيديو... [░░░░░░░░░░░░░░░░░░░░] 0.0%", quote=True)
        
        # --- تنفيذ FFmpeg وتحليل التقدم ---
        try:
            process = subprocess.Popen(
                ffmpeg_command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1  # Buffer line by line
            )

            # Get original duration from input
            duration_match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})', subprocess.check_output(f'ffprobe -v quiet -show_format -show_streams "{file_path}"', shell=True, text=True))
            if duration_match:
                h, m, s = map(float, duration_match.groups())
                total_duration_sec = h * 3600 + m * 60 + s
            else:
                total_duration_sec = 0
                print(f"[{thread_name}] Warning: Could not determine original video duration.")

            last_time = 0
            while True:
                output = process.stderr.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    # Look for time= in the output
                    time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2}\.\d{2})', output)
                    if time_match:
                        h, m, s = map(float, time_match.groups())
                        current_time_sec = h * 3600 + m * 60 + s
                        if total_duration_sec > 0:
                            percentage = min(100.0, (current_time_sec / total_duration_sec) * 100)
                        else:
                            # Fallback if duration is unknown
                            percentage = 0.0
                        
                        # Update progress message
                        progress_bar_str = create_progress_bar(percentage)
                        try:
                            app.edit_message_text(
                                chat_id=message.chat.id,
                                message_id=progress_msg.id,
                                text=f"🔄 جاري ضغط الفيديو... {progress_bar_str}"
                            )
                        except:
                            pass # Ignore errors if message is deleted
                        
                        last_time = current_time_sec

            rc = process.poll()
            if rc != 0:
                # Read remaining stderr for error details
                stderr_output = process.stderr.read()
                raise subprocess.CalledProcessError(rc, ffmpeg_command, stderr=stderr_output)

        except subprocess.CalledProcessError as e:
            print(f"[{thread_name}][FFmpeg] error occurred for '{os.path.basename(file_path)}'!")
            print(f"[{thread_name}][FFmpeg] stdout: {e.stdout}")
            print(f"[{thread_name}][FFmpeg] stderr: {e.stderr}")
            user_error_message = f"حدث خطأ أثناء ضغط الفيديو:\n`{e.stderr.strip() if e.stderr else 'غير معروف'}`"
            message.reply_text(user_error_message[:4000], quote=True)
            return # Exit after handling error
        except Exception as e:
            print(f"[{thread_name}] Unexpected error during FFmpeg execution: {e}")
            message.reply_text(f"حدث خطأ أثناء تنفيذ FFmpeg: `{e}`", quote=True)
            return # Exit after handling error

        # حذف رسالة التقدم بعد الانتهاء
        try:
            progress_msg.delete()
        except:
            pass

        compressed_file_size_mb = os.path.getsize(temp_compressed_filename) / (1024 * 1024)
        print(f"[{thread_name}] Compressed file '{os.path.basename(temp_compressed_filename)}' size: {compressed_file_size_mb:.2f} MB")

        # إرسال الملف المضغوط إلى الدردشة نفسها
        try:
            # إرسال رسالة تتبع الرفع مع شريط تقدم
            upload_progress_msg = message.reply_text("📤 جاري رفع الفيديو المضغوط... [░░░░░░░░░░░░░░░░░░░░] 0.0%", quote=True)
            
            # دالة تقدم الرفع
            def upload_progress(current, total):
                if total > 0:
                    percent = (current / total) * 100
                    bar = create_progress_bar(percent)
                    try:
                        app.edit_message_text(
                            chat_id=message.chat.id,
                            message_id=upload_progress_msg.id,
                            text=f"📤 جاري رفع الفيديو المضغوط... {bar}"
                        )
                    except:
                        pass # لا تفعل شيئاً إذا فشل التحديث (مثلاً إذا حذفت الرسالة)
            
            message.reply_document(
                document=temp_compressed_filename,
                progress=upload_progress, # استخدام دالة التقدم
                caption=f"📦 الفيديو المضغوط\nالحجم الأصلي: {os.path.getsize(file_path) / (1024 * 1024):.2f} ميجابايت\nالحجم الجديد: {compressed_file_size_mb:.2f} ميجابايت\nالجودة المستخدمة: CRF {quality_value}"
            )
            
            # حذف رسالة تقدم الرفع بعد الانتهاء
            try:
                upload_progress_msg.delete()
            except:
                pass
            
            message.reply_text(
                f"✅ تم ضغط الفيديو ورفعه بنجاح!\n"
                f"الحجم الجديد: **{compressed_file_size_mb:.2f} ميجابايت**", quote=True
            )
        except Exception as e:
            message.reply_text(f"حدث خطأ أثناء الرفع إلى الدردشة: {e}")

    except Exception as e:
        print(f"[{thread_name}] General error during video processing for '{os.path.basename(file_path)}': {e}")
        # The FFmpeg error is handled above, so this catches other general exceptions
        if "message.reply_text" not in str(e): # Avoid double-sending error messages
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
                    [InlineKeyboardButton("🎯 ضغط لحجم معين", callback_data="target_size_prompt"),
                     InlineKeyboardButton("❌ إنهاء العملية", callback_data="finish_process")]
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

# معالجة إدخال حجم الهدف
@app.on_message(filters.text)
def handle_target_size_input(client, message):
    user_id = message.from_user.id
    if user_id in user_states and user_states[user_id].get("state") == "waiting_for_target_size":
        prompt_message_id = user_states[user_id].get("prompt_message_id")
        button_message_id = user_states[user_id].get("button_message_id")
        try:
            size = float(message.text)
            if size <= 0:
                raise ValueError("Size must be positive")
                
            if button_message_id and button_message_id in user_video_data:
                video_data = user_video_data[button_message_id]
                if not video_data.get('processing_started') and video_data.get('file'):
                    video_data['quality'] = {"target_size": size}
                    
                    # تحديث الزر
                    try:
                        app.edit_message_reply_markup(
                            chat_id=message.chat.id, message_id=button_message_id,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎯 تم تحديد الحجم ~{size} ميجابايت", callback_data="none")]]))
                    except Exception: pass
                    
                    compression_executor.submit(process_video_for_compression, video_data)
                    del user_states[user_id]
                    message.reply_text(f"✅ بدأ الضغط للوصول لحجم ~{size} ميجابايت", quote=True)
                else:
                    message.reply_text("❌ انتهت مهلة العملية أو لم يكتمل تنزيل الفيديو.", quote=True)
            else:
                message.reply_text("❌ انتهت مهلة العملية. يرجى بدء عملية جديدة.", quote=True)
                
        except ValueError:
            message.reply_text("❌ إدخال غير صالح. الرجاء إرسال رقم موجب يمثل الحجم بالميغابايت.", quote=True)
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
    
    # --- تعديل دالة التقدم ---
    def download_progress(current, total):
        if total > 0:
            percent = (current / total) * 100
            bar = create_progress_bar(percent)
            # لا يمكننا تحرير رسالة قيد الإنشاء، لذا نستخدم رسالة مؤقتة
            if hasattr(handle_incoming_video, '_dl_progress_msg'):
                try:
                    app.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=handle_incoming_video._dl_progress_msg.id,
                        text=f"📥 جاري تنزيل الفيديو... {bar}"
                    )
                except:
                    pass # Ignore if message is deleted

    # إرسال رسالة تقدم التنزيل
    dl_progress_msg = message.reply_text("📥 جاري تنزيل الفيديو... [░░░░░░░░░░░░░░░░░░░░] 0.0%", quote=True)
    handle_incoming_video._dl_progress_msg = dl_progress_msg

    download_future = download_executor.submit(
        client.download_media, file_id, file_name=file_name_prefix,
        progress=download_progress # استخدام دالة التقدم
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
        'auto_compress_status_message_id': None
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
        
        # حذف رسالة تقدم التنزيل
        try:
            handle_incoming_video._dl_progress_msg.delete()
        except:
            pass # إذا لم توجد الرسالة أو تم حذفها مسبقًا

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
                [InlineKeyboardButton("🎯 ضغط لحجم معين", callback_data="target_size_prompt"),
                 InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_compression")]
            ])
            reply_message = message.reply_text("✅ تم تنزيل الفيديو.\nاختر جودة الضغط، أو سيتم اختيار جودة متوسطة بعد **300 ثانية**:", reply_markup=markup, quote=True)
            video_data['button_message_id'] = reply_message.id
            user_video_data[reply_message.id] = user_video_data.pop(original_message_id)
            timer = threading.Timer(300, auto_select_medium_quality, args=[reply_message.id])
            user_video_data[reply_message.id]['timer'] = timer
            timer.start()
    except Exception as e:
        print(f"[{thread_name}] Error during post-download actions for original_message_id {original_message_id}: {e}")
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

    if data == "target_size_prompt":
        if video_data.get('timer') and video_data['timer'].is_alive(): video_data['timer'].cancel()
        if not video_data.get('file') or not os.path.exists(video_data['file']):
            callback_query.answer("لم يكتمل تنزيل الفيديو بعد.", show_alert=True)
            return
        
        # طلب إدخال الحجم
        prompt_msg = message.reply_text("🔢 أرسل الحجم المطلوب للملف المضغوط (بالميغابايت):", quote=True)
        user_states[user_id] = {
            "state": "waiting_for_target_size", 
            "prompt_message_id": prompt_msg.id,
            "button_message_id": button_message_id
        }
        callback_query.answer("يرجى إدخال الحجم المطلوب...")
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
