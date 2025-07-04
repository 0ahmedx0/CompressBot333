import os
import re
import tempfile
import subprocess
import asyncio
import threading
import time
import math
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode
from config import *

MAX_QUEUE_SIZE = 10
DOWNLOADS_DIR = "./downloads"

# تأكد من وجود مجلد التنزيلات
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# لتخزين بيانات الفيديوهات الواردة وانتظار حجم الضغط
user_video_data = {}

# قائمة انتظار لتخزين الفيديوهات التي تحتاج إلى معالجة (قائمة انتظار للفيديوهات التي ينتظرون الضغط)
video_compression_queue = asyncio.Queue()

is_processing = False
processing_lock = asyncio.Lock()

def get_duration_from_ffprobe(filepath):
    """يحصل على مدة الفيديو باستخدام ffprobe."""
    try:
        command = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        return duration
    except Exception as e:
        print(f"Error getting duration with ffprobe: {e}")
        return None

def calculate_video_bitrate(target_size_mb, duration_seconds):
    """يحسب Video Bitrate المطلوب بناءً على الحجم المستهدف والمدة."""
    if duration_seconds is None or duration_seconds <= 0:
        return None

    # حساب الحجم الكلي المستهدف بالكيلوبت ( target_size_mb * 8 * 1024 )
    target_size_kbits = target_size_mb * 8192
    audio_bitrate_kbps = int(VIDEO_AUDIO_BITRATE.replace('k', '')) # تحويل bitrare الصوت إلى Kbps
    audio_size_kbits = audio_bitrate_kbps * duration_seconds

    # حجم الفيديو المستهدف (نطرح حجم الصوت المتوقع)
    target_video_size_kbits = target_size_kbits - audio_size_kbits

    if target_video_size_kbits <= 0:
        print("Target video size is too small after subtracting audio. Increase target size.")
        return None

    # Video Bitrate المطلوب ( بالكيلوبت لكل ثانية )
    video_bitrate_kbps = target_video_size_kbits / duration_seconds

    # تحويل إلى bit/s
    video_bitrate_bps = video_bitrate_kbps * 1000

    return int(video_bitrate_bps)

# تهيئة العميل للبوت
app = Client(
    "video_compressor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN,
    plugins=dict(root="plugins")
)

async def cleanup_downloads():
    """
    تنظيف مجلد التنزيلات.
    """
    print("Starting cleanup...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")
    print("Cleanup finished.")


async def progress_callback(current, total, client: Client, message: Message):
    """عرض تقدم عملية التحميل أو الرفع."""
    if total > 0:
        percent = f"{current / total * 100:.1f}%"
        text = f"📥 جاري المعالجة...\n⬇️ النسبة: {percent}"
    else:
        text = "📥 جاري المعالجة..."

    try:
        await message.edit_text(text)
    except:
        pass  # تجاهل أي خطأ بسبب rate limit

async def process_video_compression():
    """معالجة الفيديوهات الموجودة في قائمة انتظار الضغط."""
    global is_processing
    async with processing_lock:
        if is_processing:
            return
        is_processing = True

    print("Starting video compression queue processing...")

    while True:
        try:
            # انتظار فيديو جديد في قائمة الانتظار
            video_data = await asyncio.wait_for(video_compression_queue.get(), timeout=1) # وقت انتظار قصير

            file_path = video_data['file_path']
            target_size_mb = video_data['target_size_mb']
            message = video_data['message']
            progress_message_id = video_data['progress_message_id']
            user_id = video_data['user_id']

            try:
                # التأكد من وجود الملف قبل البدء بالضغط
                if not os.path.exists(file_path):
                    print(f"Compression failed: File not found: {file_path}")
                    await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="❌ حدث خطأ: لم يتم العثور على الملف الأصلي للضغط.")
                    video_compression_queue.task_done()
                    continue

                # الحصول على مدة الفيديو
                duration = get_duration_from_ffprobe(file_path)
                if duration is None:
                    print(f"Compression failed: Could not get duration for file: {file_path}")
                    await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="❌ حدث خطأ في قراءة مدة الفيديو.")
                    video_compression_queue.task_done()
                    continue

                # حساب Video Bitrate
                target_bitrate_bps = calculate_video_bitrate(target_size_mb, duration)
                if target_bitrate_bps is None:
                    print(f"Compression failed: Could not calculate target bitrate for file: {file_path}")
                    await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="❌ حدث خطأ في حساب معدل البت المطلوب. ربما الحجم المطلوب صغير جدا.")
                    video_compression_queue.task_done()
                    continue

                await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="🔄 بدأ ضغط الفيديو...")

                # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
                with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
                    compressed_file_path = temp_file.name

                ffmpeg_command = [
                    'ffmpeg', '-y', '-i', file_path,
                    '-c:v', VIDEO_CODEC,
                    '-pix_fmt', VIDEO_PIXEL_FORMAT,
                    '-b:v', str(target_bitrate_bps),
                    '-preset', 'medium',  # استخدام preset medium
                    '-profile:v', 'high',
                    '-c:a', VIDEO_AUDIO_CODEC,
                    '-b:a', VIDEO_AUDIO_BITRATE,
                    '-ac', str(VIDEO_AUDIO_CHANNELS),
                    '-ar', str(VIDEO_AUDIO_SAMPLE_RATE),
                    '-map_metadata', '-1',
                    compressed_file_path
                ]

                print(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

                # تشغيل أمر FFmpeg ومراقبة تقدمه (FFmpeg يرسل التقدم إلى stderr عادةً)
                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )

                # وظيفة لمراقبة stderr وقراءة التقدم
                async def read_stderr():
                    last_update_time = time.time()
                    while True:
                        line = await process.stderr.readline()
                        if not line:
                            break
                        line = line.decode('utf-8').strip()

                        # مثال لسُطر تقدم FFmpeg (قد يختلف التنسيق قليلاً)
                        # frame=  224 fps= 43 q=27.0 size=   425kB time=00:00:09.36 bitrate= 372.7kbits/s speed=1.79x
                        match = re.search(r'time=(\d{2}:\d{2}:\d{2}\.\d{2})', line)
                        if match:
                            current_time_str = match.group(1)
                            h, m, s_ms = current_time_str.split(':')
                            s, ms = s_ms.split('.')
                            current_seconds = int(h) * 3600 + int(m) * 60 + int(s) + float(ms) / 100

                            if duration and duration > 0:
                                percentage = (current_seconds / duration) * 100
                                text = f"🔄 جاري ضغط الفيديو...\n💪 النسبة: {percentage:.1f}%"
                                if time.time() - last_update_time > 3: # تحديث الرسالة كل 3 ثوانٍ على الأقل
                                    try:
                                        await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text=text)
                                        last_update_time = time.time()
                                    except:
                                        pass # تجاهل الأخطاء

                # تشغيل مراقبة التقدم في مهمة asyncio منفصلة
                progress_task = asyncio.create_task(read_stderr())

                # انتظار انتهاء عملية FFmpeg
                stdout, stderr = await process.communicate()

                # إيقاف مهمة التقدم إذا كانت لا تزال تعمل
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

                if process.returncode != 0:
                    print("FFmpeg error occurred!")
                    print(f"FFmpeg stderr: {stderr.decode()}")
                    await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text=f"❌ حدث خطأ أثناء ضغط الفيديو:\n`{stderr.decode()}`")
                else:
                    print("FFmpeg command executed successfully.")

                    await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="⬆️ جاري رفع الفيديو المضغوط...")

                    # رفع الفيديو المضغوط إلى القناة
                    if CHANNEL_ID:
                        try:
                            await app.send_document(
                                chat_id=CHANNEL_ID,
                                document=compressed_file_path,
                                caption=f"فيديو مضغوط بالحجم المطلوب ({target_size_mb}MB) من {message.from_user.mention}",
                                # يمكنك إضافة progress هنا لرفع الفيديو المضغوط إلى القناة إذا كنت تريد ذلك
                                # progress=progress_callback, progress_args=[app, ... ]
                            )
                            print(f"Compressed video uploaded to channel: {CHANNEL_ID}")

                            await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="✅ تم ضغط الفيديو ورفعه بنجاح إلى القناة.")
                        except Exception as e:
                            print(f"Error uploading compressed video to channel: {e}")
                            await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="❌ حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة.")
                    else:
                        print("CHANNEL_ID not configured. Video not sent to channel.")
                        await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text="⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط.")

            except Exception as e:
                print(f"General error during compression: {e}")
                await app.edit_message_text(chat_id=message.chat.id, message_id=progress_message_id, text=f"❌ حدث خطأ غير متوقع أثناء المعالجة: {e}")

            finally:
                # حذف الملف المضغوط المؤقت
                if 'compressed_file_path' in locals() and os.path.exists(compressed_file_path):
                    os.remove(compressed_file_path)

                # حذف ملف الفيديو الأصلي بعد انتهاء المعالجة
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"Deleted original file: {file_path}")

                # إشارة إلى أن مهمة قائمة الانتظار قد اكتملت
                video_compression_queue.task_done()
                print("Video compression task done.")

        except asyncio.TimeoutError:
            # قائمة الانتظار فارغة، نخرج من الحلقة
            break
        except Exception as e:
            print(f"Error in video compression queue processing: {e}")
            # في حالة وجود خطأ، لا تنسخ task_done لعدم تجميد قائمة الانتظار إذا كان الخطأ داخلياً

    async with processing_lock:
        is_processing = False
        print("Video compression queue processing finished.")

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    """الرد على أمر /start."""
    await message.reply_text("أرسل لي فيديو أو أنيميشن وسأقوم بضغطه لك إلى الحجم الذي تحدده.")

@app.on_message(filters.video | filters.animation)
async def handle_video(client, message):
    """معالجة الفيديوهات والأنيميشن الواردة."""
    user_id = message.from_user.id

    # التحقق من وجود فيديو آخر للمستخدم قيد الانتظار
    if user_id in user_video_data and user_video_data[user_id].get('status') == 'waiting_size':
        await message.reply_text(" لديك فيديو آخر ينتظر تحديد الحجم. يرجى إرسال الحجم المطلوب للفيديو السابق أولاً، أو أرسل `/cancel` لإلغاء العملية السابقة.", quote=True)
        return

    # حذف البيانات السابقة للمستخدم في user_video_data إن وجدت
    if user_id in user_video_data:
        old_file_path = user_video_data[user_id].get('file_path')
        if old_file_path and os.path.exists(old_file_path):
            try:
                os.remove(old_file_path)
                print(f"Deleted old file for user {user_id}: {old_file_path}")
            except Exception as e:
                print(f"Error deleting old file for user {user_id}: {e}")
        del user_video_data[user_id]


    file_id = message.video.file_id if message.video else message.animation.file_id
    file_size = message.video.file_size if message.video else message.animation.file_size
    file_name = message.video.file_name if message.video and message.video.file_name else message.animation.file_name

    # استخدم file_id كاسم فريد للملف المؤقت
    temp_filename = f"{file_id}_{file_name or 'video'}"
    local_path = os.path.join(DOWNLOADS_DIR, temp_filename)

    print(f"📥 Starting download for file_id: {file_id} to {local_path}")

    # إرسال رسالة مؤقتة لعرض التقدم
    progress_message = await message.reply_text("🔽 بدأ تحميل الفيديو...")

    try:
        # تحميل الفيديو باستخدام aria2c
        file_info = await client.get_file(file_id)
        direct_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"

        aria2_command = [
            "aria2c", "-x", "16", "-s", "16", "--summary-interval=1", "--console-log-level=warn",
            "-o", temp_filename, "-d", DOWNLOADS_DIR, direct_url
        ]

        # تشغيل aria2c ومراقبة الخرج
        process = await asyncio.create_subprocess_exec(
            *aria2_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line = line.decode('utf-8', errors='ignore').strip()

            # مثال للسطر: [#a1b2c3 12MiB/35MiB(35%) CN:16 DL:2.3MiB ETA:19s]
            match = re.search(r'(\d+(?:\.\d+)?[KMG]iB)\/(\d+(?:\.\d+)?[KMG]iB)\((\d+(?:\.\d+)?)%\).*DL:(\d+(?:\.\d+)?[KMG]iB).*ETA:(\d+s)', line)

            if match:
                downloaded = match.group(1)
                total = match.group(2)
                percent = match.group(3)
                speed = match.group(4)
                eta = match.group(5)

                # نص الرسالة المحدث
                text = (
                    f"📥 جاري تحميل الفيديو...\n"
                    f"⬇️ النسبة: {percent}%\n"
                    f"💾 الحجم: {downloaded} / {total}\n"
                    f"⚡ السرعة: {speed}\n"
                    f"⏳ متبقي: {eta}"
                )

                try:
                    await progress_message.edit_text(text)
                except:
                    pass  # تجاهل أي خطأ بسبب rate limit

        returncode = await process.wait()
        if returncode != 0:
            await progress_message.edit_text("❌ فشل تحميل الفيديو.")
            if os.path.exists(local_path):
                os.remove(local_path)
            return

        # حذف رسالة التقدم بعد انتهاء التحميل
        try:
            await progress_message.delete()
        except Exception as e:
            print(f"Error deleting progress message: {e}")


        # إعلام المستخدم بالتحميل وإعداد البيانات للانتظار
        await message.reply_text(f"📥 تم تحميل الفيديو بنجاح!\nالآن، **أرسل رقماً فقط** يمثل الحجم النهائي الذي تريده بالفيديو بالميجابايت (مثال: `50`) لتحديد حجم الضغط.")

        # تخزين بيانات الفيديو بانتظار حجم الضغط
        user_video_data[user_id] = {
            'file_path': local_path,
            'message': message, # رسالة الفيديو الأصلية
            'status': 'waiting_size',
            'progress_message_id': None # سنضيف لاحقاً رسالة التقدم لعملية الضغط
        }


    except Exception as e:
        print(f"❌ Error in handle_video: {e}")
        await message.reply_text("حدث خطأ أثناء تحميل الفيديو. حاول مرة أخرى.")
        # تنظيف الملف المحلي في حالة وجود خطأ
        if 'local_path' in locals() and os.path.exists(local_path):
             try:
                 os.remove(local_path)
             except Exception as e:
                 print(f"Error deleting local file after error: {e}")
        # إزالة بيانات المستخدم من الانتظار
        if user_id in user_video_data:
            del user_video_data[user_id]

@app.on_message(filters.text & filters.private & filters.user(list(user_video_data.keys())))
async def handle_target_size(client, message):
    """معالجة إدخال المستخدم لحجم الضغط المستهدف."""
    user_id = message.from_user.id

    if user_id not in user_video_data or user_video_data[user_id].get('status') != 'waiting_size':
        return # تجاهل الرسائل التي ليست بحجم مستهدف متوقع

    try:
        target_size_mb = float(message.text.strip())
        if target_size_mb <= 0:
            await message.reply_text("🔢 يرجى إدخال رقم موجب يمثل الحجم بالميجابايت.")
            return

        video_data = user_video_data.pop(user_id) # استخراج البيانات من الانتظار
        file_path = video_data['file_path']
        original_message = video_data['message']

        # التحقق من وجود الملف قبل البدء بالضغط
        if not os.path.exists(file_path):
            await message.reply_text("❌ حدث خطأ: لم يتم العثور على الملف الأصلي المطلوب ضغطه.")
            return

        # إرسال رسالة التقدم لعملية الضغط
        progress_message = await original_message.reply_text(" queuing...⏳", quote=True)
        progress_message_id = progress_message.id

        # إضافة الفيديو إلى قائمة انتظار الضغط
        video_data['target_size_mb'] = target_size_mb
        video_data['progress_message_id'] = progress_message_id
        await video_compression_queue.put(video_data)

        await progress_message.edit_text(f"🎬 تم إضافة الفيديو إلى قائمة الانتظار بحجم مستهدف {target_size_mb}MB. سيتم المعالجة قريباً.")
        print(f"Video added to compression queue for user {user_id}. Target size: {target_size_mb}MB")

        # بدء معالجة قائمة الانتظار إذا لم تكن قيد التنفيذ
        async with processing_lock:
            if not is_processing:
                 # تشغيل معالجة قائمة الانتظار في مهمة asyncio منفصلة
                 asyncio.create_task(process_video_compression())


    except ValueError:
        await message.reply_text("🔢 يرجى إدخال رقم صحيح أو عشري فقط يمثل الحجم بالميجابايت.", quote=True)
    except Exception as e:
        print(f"❌ Error in handle_target_size: {e}")
        await message.reply_text("حدث خطأ غير متوقع أثناء معالجة الحجم المطلوب.")
        # تنظيف البيانات إذا كان هناك خطأ بعد استخراجه من user_video_data
        if 'video_data' in locals() and 'file_path' in video_data and os.path.exists(video_data['file_path']):
            try:
                os.remove(video_data['file_path'])
                print(f"Deleted file after error in handle_target_size: {video_data['file_path']}")
            except Exception as e:
                print(f"Error deleting file after error: {e}")


@app.on_message(filters.command("cancel") & filters.private & filters.user(list(user_video_data.keys())))
async def cancel_operation(client, message):
    """يلغي عملية التحميل أو تحديد الحجم الحالية للمستخدم."""
    user_id = message.from_user.id

    if user_id in user_video_data:
        video_data = user_video_data.pop(user_id)
        file_path = video_data.get('file_path')

        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted file after cancellation for user {user_id}: {file_path}")
            except Exception as e:
                print(f"Error deleting file after cancellation for user {user_id}: {e}")

        await message.reply_text("✅ تم إلغاء العملية الحالية.")

    # ليس هناك داعي لحذف الرسائل هنا لأنها تتم تلقائيًا عند انتهاء العمليات بنجاح أو خطأ
    # إذا كان البوت في مرحلة التحميل (handled by handle_video with aria2c cancellation - not implemented in this basic version but possible)
    # إذا كان البوت في مرحلة انتظار الحجم (handled by removing from user_video_data)
    # إذا كان البوت في قائمة انتظار الضغط (would require modifying the queue, which is more complex and not necessary for this basic version)
    # إذا كان البوت يضغط فعليا (would require killing the ffmpeg process, complex and not implemented in this basic version)

# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت
async def check_channel(client: Client):
    """فحص والتعرف على القناة عند بدء تشغيل البوت."""
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت بالكامل (قد لا تكون ضرورية جدا هنا)
    await asyncio.sleep(1) # تم تقليل وقت الانتظار
    if not CHANNEL_ID:
        print("⚠️ CHANNEL_ID not configured. Uploading compressed videos to channel is disabled.")
        return
    try:
        # نستخدم CHANNEL_ID مباشرة من config.py بعد أن تأكدنا أنه int
        chat = await client.get_chat(CHANNEL_ID)
        print("تم التعرف على القناة:", chat.title)
    except Exception as e:
        print("خطأ في التعرف على القناة:", e)
        print("يرجى التأكد من أن CHANNEL_ID صحيح وأن البوت مسؤول في القناة ويمكنه إرسال المستندات.")

# تنظيف مجلد التنزيلات عند بدء تشغيل البوت
@app.on_connect()
async def on_connect(client):
    print("Bot connected. Starting cleanup...")
    await cleanup_downloads()
    print("Cleanup finished. Starting channel check...")
    # بدء فحص القناة في مهمة asyncio منفصلة
    asyncio.create_task(check_channel(client))

# تشغيل البوت (في Pyrogram v2.x، app.run() هو دالة awaitable تقوم بتشغيل البوت)
if __name__ == "__main__":
    async def main():
        # تنظيف مجلد التنزيلات قبل بدء تشغيل البوت
        await cleanup_downloads()

        print("Starting bot...")
        await app.start()
        print("Bot started.")

        # تشغيل فحص القناة في مهمة asyncio منفصلة بعد بدء البوت
        asyncio.create_task(check_channel(app))

        # انتظر حتى يتوقف البوت (إذا تم إيقافه بواسطة إشارة خارجية مثلا)
        # هذه الحلقة يمكن استخدامها للحفاظ على البوت يعمل
        await asyncio.Future() # ببساطة انتظر مهمة Future لا تنتهي

    try:
        # تشغيل الحلقة الرئيسية
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped manually.")
    finally:
        # إيقاف الكلاينت عند الخروج
        if app.is_connected:
            app.stop()
        print("Bot stopped.")
