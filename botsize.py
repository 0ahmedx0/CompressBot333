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
from config import * # استيراد المتغيرات من config.py

# تأكد من أن CHANNEL_ID هو integer لأنه ضروري لـ Pyrogram
# بما أننا عدّلنا config.py ليحوله إلى int، يفترض أن يكون صحيحاً الآن.
# يمكنك إضافة فحص إضافي هنا إذا لزم الأمر:
if not isinstance(CHANNEL_ID, int):
     print("Warning: CHANNEL_ID is not an integer. Attempting to convert.")
     try:
         CHANNEL_ID = int(CHANNEL_ID)
         print("Conversion successful.")
     except ValueError:
         raise ValueError("CHANNEL_ID in config.py must be a valid integer representing the channel ID.")


MAX_QUEUE_SIZE = 10 # لم نعد نستخدم هذا لفرض حد على القائمة مباشرة في هذا الكود، القائمة هي asyncio.Queue غير محدودة بحجم
DOWNLOADS_DIR = "./downloads"

# تأكد من وجود مجلد التنزيلات
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# لتخزين بيانات الفيديوهات الواردة وانتظار حجم الضغط
# سنستخدم chat_id (أو user_id) كمفتاح بدلاً من message_id لأنه مرتبط بالمستخدم بشكل مباشر.
user_video_data = {}

# قائمة انتظار لتخزين الفيديوهات التي تحتاج إلى معالجة (قائمة انتظار للفيديوهات التي ينتظرون الضغط)
video_compression_queue = asyncio.Queue()

is_processing = False
# لا نحتاج processing_lock لدالة معالجة قائمة الانتظار إذا كانت تعمل كمهمة asyncio واحدة فقط.
# لكن سنتركه للمثال إذا أردت استخدامها.

# دالة لمراقبة تقدم عمليات التحميل والرفع
async def progress_callback(current, total, client: Client, message: Message, caption: str = ""):
    """عرض تقدم عملية التحميل أو الرفع."""
    if total > 0:
        percent = f"{current / total * 100:.1f}%"
        # تحويل بايت إلى ميجابايت لعرض الحجم
        current_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        text = f"{caption}\n⬇️ النسبة: {percent}\n💾 الحجم: {current_mb:.1f}MB / {total_mb:.1f}MB"
    else:
        text = f"{caption}\n⏳ جاري المعالجة..." # أو رسالة أخرى للتحميل أو الرفع بدون حجم كلي معروف مسبقاً

    try:
        # تعديل الرسالة الحالية لعرض التقدم
        # استخدم chat_id و message_id للتأكد من تعديل الرسالة الصحيحة
        await client.edit_message_text(chat_id=message.chat.id, message_id=message.id, text=text)
    except:
        # تجاهل أي خطأ يحدث أثناء تعديل الرسالة (مثلاً: rate limit أو الرسالة حُذفت)
        pass

def get_duration_from_ffprobe(filepath):
    """يحصل على مدة الفيديو باستخدام ffprobe."""
    try:
        # تأكد من أن ffprobe موجود في PATH الخاص بالنظام
        command = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath
        ]
        # استخدام subprocess.run للدوال التي ليست asyncio
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        return duration
    except FileNotFoundError:
         print("Error: ffprobe not found. Please install FFmpeg.")
         return None
    except Exception as e:
        print(f"Error getting duration with ffprobe: {e}")
        return None

def calculate_video_bitrate(target_size_mb, duration_seconds):
    """يحسب Video Bitrate المطلوب بناءً على الحجم المستهدف والمدة."""
    if duration_seconds is None or duration_seconds <= 0:
        print("Invalid duration for bitrate calculation.")
        return None

    # حساب الحجم الكلي المستهدف بالبت ( target_size_mb * 8 * 1024 * 1024 )
    target_size_bits = target_size_mb * 8 * 1024 * 1024

    # تحويل Audio Bitrate من string مثل "128k" إلى bits/s
    try:
        # يمكن أن يكون Bitrate الصوت أكبر من 128k في ملف config.py
        audio_bitrate_str = VIDEO_AUDIO_BITRATE.lower().replace('k', '000').replace('m', '000000')
        audio_bitrate_bps = int(audio_bitrate_str)
    except ValueError:
         print(f"Invalid VIDEO_AUDIO_BITRATE format: {VIDEO_AUDIO_BITRATE}. Using default 128000 bps.")
         audio_bitrate_bps = 128000 # قيمة افتراضية في حالة الخطأ أو تنسيق غير معروف

    # حساب حجم الصوت المتوقع بالبت ( bitrate الصوت * مدة الفيديو )
    audio_size_bits = audio_bitrate_bps * duration_seconds

    # حجم الفيديو المستهدف ( نطرح حجم الصوت المتوقع )
    target_video_size_bits = target_size_bits - audio_size_bits

    if target_video_size_bits <= 0:
        print(f"Target video size is too small ({target_video_size_bits} bits) after subtracting audio. Increase target size {target_size_mb}MB for duration {duration_seconds}s.")
        return None

    # Video Bitrate المطلوب ( بالبت لكل ثانية )
    video_bitrate_bps = target_video_size_bits / duration_seconds

    # لضمان ترميز جيد، قد نحتاج حد أدنى حتى لو الحجم المطلوب صغير جداً
    # مثلاً لا تقلل bitrate عن 500kbits/s
    min_video_bitrate_bps = 500 * 1024 # 500 kbit/s
    if video_bitrate_bps < min_video_bitrate_bps:
        print(f"Calculated bitrate {video_bitrate_bps} bps is too low. Using minimum bitrate {min_video_bitrate_bps} bps.")
        video_bitrate_bps = min_video_bitrate_bps


    return int(video_bitrate_bps)

# تهيئة العميل للبوت
# لاحظ استخدام اسم الكلاينت لملف جلسة مختلف عن البوت الأصلي
app = Client(
    "video_compressor_size_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN,
    plugins=dict(root="plugins") # يمكنك إضافة مجلد plugins إذا كان لديك
)


async def process_video_compression():
    """معالجة الفيديوهات الموجودة في قائمة انتظار الضغط بشكل متسلسل."""
    global is_processing
    # تأكد من أن نسخة واحدة فقط من هذه الدالة تعمل في أي وقت
    # Lock هنا لم يعد ضروريا مع asyncio.Queue و مهمة واحدة
    # async with processing_lock:
    #    if is_processing: return
    is_processing = True # مجرد مؤشر لحالة المعالجة
    print("Starting video compression queue processing task...")

    # الحلقة الرئيسية لمعالجة قائمة الانتظار
    while True:
        try:
            # انتظار فيديو جديد في قائمة الانتظار
            # إذا كانت القائمة فارغة لثانية واحدة، سنخرج من هذا الانتظار وننهي المهمة (لتُعاد التشغيل لاحقاً عند إضافة عنصر جديد).
            # أو يمكننا الانتظار بلا نهاية إذا أردنا أن تبقى المهمة نشطة دائماً.
            # لنستخدم انتظار قصير هنا ثم ننهي المهمة ونعتمد على handle_target_size لإعادة تشغيلها.
            video_data = await asyncio.wait_for(video_compression_queue.get(), timeout=1) # انتظار لمدة ثانية

            file_path = video_data['file_path']
            target_size_mb = video_data['target_size_mb']
            original_message = video_data['original_message'] # رسالة الفيديو الأصلية
            progress_message = video_data['progress_message'] # كائن رسالة التقدم نفسها
            user_id = video_data['user_id']


            try:
                # التأكد من وجود الملف قبل البدء بالضغط
                if not os.path.exists(file_path):
                    print(f"Compression failed: File not found: {file_path}")
                    await progress_message.edit_text("❌ حدث خطأ: لم يتم العثور على الملف الأصلي للضغط.")
                    video_compression_queue.task_done()
                    continue # انتقل إلى المهمة التالية

                # الحصول على مدة الفيديو باستخدام ffprobe
                # يتم استدعاؤها هنا مرة أخرى لأن الدالة ليست async ولا يمكن استدعاؤها بـ await داخل هذه async def مباشرة
                # والأهم، يجب أن يتم تشغيل subprocess بشكل async هنا أيضاً
                # نستخدم asyncio.to_thread لتشغيل دالة blocking (ffprobe) في Thread pool منفصل لعدم حظر الحلقة الرئيسية
                duration = await asyncio.to_thread(get_duration_from_ffprobe, file_path)

                if duration is None:
                    print(f"Compression failed: Could not get duration for file: {file_path}")
                    await progress_message.edit_text("❌ حدث خطأ في قراءة مدة الفيديو. تأكد من تثبيت FFmpeg و ffprobe بشكل صحيح.")
                    video_compression_queue.task_done()
                    continue

                # حساب Video Bitrate المستهدف
                target_bitrate_bps = calculate_video_bitrate(target_size_mb, duration)
                if target_bitrate_bps is None:
                    print(f"Compression failed: Could not calculate target bitrate for file: {file_path}")
                    await progress_message.edit_text(f"❌ حدث خطأ في حساب معدل البت المطلوب للحجم {target_size_mb}MB والمدة {duration:.1f} ثانية. ربما الحجم المطلوب صغير جداً بالنسبة لمدة الفيديو.")
                    video_compression_queue.task_done()
                    continue

                # تحديث رسالة التقدم
                await progress_message.edit_text("🔄 بدأ ضغط الفيديو...")

                # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
                with tempfile.NamedTemporaryFile(suffix=TEMP_FILE_SUFFIX_VIDEO, delete=False) as temp_file:
                    compressed_file_path = temp_file.name

                # أمر FFmpeg لضغط الفيديو
                # استخدام المتغيرات من config.py والbitrate المحسوب
                ffmpeg_command = [
                    'ffmpeg', '-y', '-i', file_path, # الإدخال
                    # إعدادات الفيديو
                    '-c:v', VIDEO_CODEC, # الترميز من config
                    '-pix_fmt', VIDEO_PIXEL_FORMAT, # تنسيق البكسل من config
                    '-b:v', f"{target_bitrate_bps} bps", # تحديد معدل البت بالبت لكل ثانية (الأهم هنا لتحديد الحجم)
                    '-preset', VIDEO_PRESET,  # Preset من config
                    '-profile:v', VIDEO_PROFILE, # Profile من config
                    '-vf', f"scale={VIDEO_SCALE}", # الحفاظ على الأبعاد أو تغييرها من config
                    '-r', str(VIDEO_FPS), # معدل الإطارات من config

                    # إعدادات الصوت
                    '-c:a', VIDEO_AUDIO_CODEC, # ترميز الصوت من config
                    '-b:a', VIDEO_AUDIO_BITRATE, # معدل بت الصوت من config
                    '-ac', str(VIDEO_AUDIO_CHANNELS), # عدد القنوات من config
                    '-ar', str(VIDEO_AUDIO_SAMPLE_RATE), # معدل العينة من config

                    '-map_metadata', '-1', # إزالة الميتاداتا
                    compressed_file_path # الإخراج
                ]

                print(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

                # تشغيل أمر FFmpeg ومراقبة تقدمه
                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_command,
                    stdout=subprocess.PIPE, # احتفظ بـ stdout
                    stderr=subprocess.PIPE # احتفظ بـ stderr (عادة FFmpeg يرسل التقدم هنا)
                )

                # وظيفة لمراقبة stderr وقراءة التقدم بشكل async
                async def monitor_ffmpeg_progress(proc, progress_msg, duration):
                    last_update_time = time.time()
                    while True:
                        try:
                           # قراءة سطر سطر من stderr
                           line = await asyncio.wait_for(proc.stderr.readline(), timeout=1.0) # انتظار قصير لقراءة السطر
                           if not line:
                               # إذا كانت العملية قد انتهت، نخرج. وإلا نستمر في الانتظار.
                               if await proc.wait() is not None:
                                    break
                               continue # العملية ما زالت تعمل، استمر في الانتظار للقراءة

                           line = line.decode('utf-8', errors='ignore').strip()

                           # البحث عن تقدم الوقت في خرج FFmpeg
                           match_time = re.search(r'time=(\d{2}:\d{2}:\d{2}\.\d{2})', line)
                           if match_time:
                               current_time_str = match_time.group(1)
                               h, m, s_ms = current_time_str.split(':')
                               s, ms = s_ms.split('.')
                               current_seconds = int(h) * 3600 + int(m) * 60 + int(s) + float(ms) / 100

                               if duration and duration > 0:
                                    percentage = (current_seconds / duration) * 100
                                    text = f"🔄 جاري ضغط الفيديو...\n💪 النسبة: {percentage:.1f}%"
                                    # تحديث رسالة التقدم كل بضع ثوانٍ لتجنب الـ rate limit
                                    if time.time() - last_update_time > 3:
                                        try:
                                            await progress_msg.edit_text(text)
                                            last_update_time = time.time()
                                        except:
                                            pass # تجاهل الأخطاء


                        except asyncio.TimeoutError:
                            # لم يتم قراءة أي سطر خلال الوقت المحدد، استمر في الانتظار
                            continue
                        except Exception as e:
                            print(f"Error reading FFmpeg stderr: {e}")
                            break # الخروج من قراءة stderr في حالة وجود خطأ

                # تشغيل مراقبة التقدم في مهمة asyncio منفصلة
                monitor_task = asyncio.create_task(monitor_ffmpeg_progress(process, progress_message, duration))


                # انتظار انتهاء عملية FFmpeg والحصول على الخرج
                stdout, stderr = await process.communicate()

                # إيقاف مهمة المراقبة إذا كانت لا تزال تعمل
                monitor_task.cancel()
                try:
                    await monitor_task # انتظر المهمة لإكمال الإلغاء
                except asyncio.CancelledError:
                    pass


                # فحص returncode للتأكد من نجاح عملية FFmpeg
                if process.returncode != 0:
                    print("FFmpeg error occurred!")
                    error_output = stderr.decode(errors='ignore') # استخدام errors='ignore' لتجنب مشاكل الترميز
                    print(f"FFmpeg stderr: {error_output}")
                    # عرض جزء من الخطأ في رسالة Telegram
                    await progress_message.edit_text(f"❌ حدث خطأ أثناء ضغط الفيديو:\n`{error_output[:1000]}`")
                else:
                    print("FFmpeg command executed successfully.")

                    await progress_message.edit_text("⬆️ جاري رفع الفيديو المضغوط...")

                    # رفع الفيديو المضغوط إلى القناة
                    # تأكد من أن CHANNEL_ID معرف ونوعه integer
                    if CHANNEL_ID and isinstance(CHANNEL_ID, int):
                        try:
                            # تأكد من وجود الملف المضغوط قبل الرفع
                            if not os.path.exists(compressed_file_path):
                                await progress_message.edit_text("❌ حدث خطأ: لم يتم إنشاء ملف الفيديو المضغوط.")
                                video_compression_queue.task_done()
                                continue # الانتقال للمهمة التالية

                            await app.send_document(
                                chat_id=CHANNEL_ID, # يستخدم CHANNEL_ID من config
                                document=compressed_file_path,
                                caption=f"فيديو مضغوط بحجم {target_size_mb}MB من {original_message.from_user.mention if original_message.from_user else 'مستخدم مجهول'}",
                                # استخدام دالة التقدم لعملية الرفع
                                progress=progress_callback,
                                progress_args=[app, progress_message, "⬆️ جاري الرفع إلى القناة..."]
                            )
                            print(f"Compressed video uploaded to channel: {CHANNEL_ID}")

                            # حذف رسالة التقدم بعد الرفع الناجح
                            # await progress_message.delete()
                            # أو تحديثها بنجاح
                            await progress_message.edit_text("✅ تم ضغط الفيديو ورفعه بنجاح إلى القناة.")

                        except Exception as e:
                            print(f"Error uploading compressed video to channel: {e}")
                            await progress_message.edit_text(f"❌ حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة:\n{e}")
                    else:
                        print("CHANNEL_ID not configured or is not an integer. Video not sent to channel.")
                        await progress_message.edit_text("⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط أو المعرّف غير صحيح.")

            except Exception as e:
                print(f"General error during compression: {e}")
                # في حالة وجود خطأ عام، قم بتحديث رسالة التقدم
                await progress_message.edit_text(f"❌ حدث خطأ غير متوقع أثناء المعالجة: {e}")

            finally:
                # حذف الملف المضغوط المؤقت إذا كان موجودًا
                if 'compressed_file_path' in locals() and os.path.exists(compressed_file_path):
                    try:
                        os.remove(compressed_file_path)
                        print(f"Deleted temporary compressed file: {compressed_file_path}")
                    except Exception as e:
                        print(f"Error deleting temporary file {compressed_file_path}: {e}")


                # حذف ملف الفيديو الأصلي بعد انتهاء المعالجة بنجاح أو خطأ
                # لا نحتاج لحذفه إلا بعد المعالجة لمرة واحدة
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        print(f"Deleted original file: {file_path}")
                    except Exception as e:
                        print(f"Error deleting original file {file_path}: {e}")

                # إشارة إلى أن مهمة قائمة الانتظار قد اكتملت
                video_compression_queue.task_done()
                print("Video compression task done.")

        except asyncio.TimeoutError:
            # قائمة الانتظار فارغة لثانية واحدة، نخرج من هذه الدورة لإنهاء المهمة
            # سيتم إعادة تشغيل المهمة تلقائياً عند إضافة عنصر جديد في handle_target_size
            # print("Compression queue is empty. Processing task will pause.")
            break # الخروج من حلقة while True لإنهاء المهمة
        except Exception as e:
            print(f"Error in video compression queue processing loop: {e}")
            # إذا حدث خطأ أثناء معالجة عنصر ما، يمكننا تسجيل الخطأ والاستمرار في الحلقة

    # عند الخروج من الحلقة (لأن القائمة فارغة)، نعيد is_processing إلى False
    # Lock غير ضروري هنا ولكن تركناه للمثال
    # async with processing_lock:
    is_processing = False
    print("Video compression queue processing task finished.")


@app.on_message(filters.command("start") & filters.private)
async def start(client, message: Message):
    """الرد على أمر /start."""
    await message.reply_text("👋 أهلاً بك! أرسل لي فيديو أو أنيميشن وسأقوم بضغطه لك إلى الحجم الذي تحدده.")

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_operation(client, message: Message):
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

        # يمكنك أيضاً محاولة حذف رسالة التقدم إذا كانت موجودة
        progress_message_id = video_data.get('progress_message_id')
        if progress_message_id:
            try:
                await app.delete_messages(chat_id=message.chat.id, message_ids=[progress_message_id])
            except Exception as e:
                 print(f"Error deleting progress message on cancel: {e}")

        await message.reply_text("✅ تم إلغاء العملية الحالية.", quote=True)

    else:
        # فحص إذا كان لدى المستخدم مهام قيد الانتظار في قائمة الانتظار الرئيسية (أكثر تعقيدا ولا يتم التعامل معه هنا ببساطة)
        # يمكنك تتبع المهام في قائمة الانتظار أيضاً وإلغائها إذا لزم الأمر
        # For simplicity, we only cancel the waiting_size state.
        await message.reply_text("❌ ليس لديك أي عملية تحميل أو انتظار حجم قيد التنفيذ حالياً للإلغاء.\nإذا كان الفيديو قيد الضغط بالفعل، لا يمكن إلغاؤه.", quote=True)


@app.on_message(filters.video | filters.animation)
async def handle_video(client, message: Message):
    """
    معالجة الفيديو أو الرسوم المتحركة المرسلة.
    يتم تحميل الملف باستخدام aria2c ثم يطلب من المستخدم تحديد حجم الضغط.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id

    # التحقق من وجود فيديو آخر للمستخدم قيد الانتظار لتحديد الحجم
    if user_id in user_video_data and user_video_data[user_id].get('status') == 'waiting_size':
        await message.reply_text("⚠️ لديك فيديو آخر ينتظر تحديد الحجم. يرجى إرسال الحجم المطلوب للفيديو السابق أولاً، أو أرسل `/cancel` لإلغاء العملية السابقة.", quote=True)
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
    # احصل على اسم الملف من الرسالة، أو استخدم اسم افتراضي مع file_id لضمان التفرد
    file_name = message.video.file_name if message.video and message.video.file_name else (message.animation.file_name if message.animation and message.animation.file_name else f"{file_id}_file.{'mp4' if message.video else 'gif'}")


    # استخدم file_id كجزء من اسم الملف المؤقت لضمان التفرد التام
    temp_filename = f"{file_id}_{file_name}"
    local_path = os.path.join(DOWNLOADS_DIR, temp_filename)

    print(f"📥 Starting download for file_id: {file_id} to {local_path}")

    # إرسال رسالة مؤقتة لعرض التقدم في نفس محادثة المستخدم
    # نربطها برسالة الفيديو الأصلية
    progress_message = await message.reply_text("🔽 بدأ تحميل الفيديو...", quote=True)

    try:
        # بناء الرابط المباشر لتحميل aria2c
        # نحتاج لـ file_path من get_file() لبناء الرابط
        file_info = await client.get_file(file_id)
        # تأكد من أن file_info.file_path موجود وصحيح
        if not file_info or not file_info.file_path:
            await progress_message.edit_text("❌ خطأ في جلب معلومات الملف من Telegram.")
            return

        direct_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        print(f"Attempting to download with aria2c from: {direct_url}")

        # أمر aria2c
        aria2_command = [
            "aria2c", "-x", "16", "-s", "16", "--summary-interval=1", "--console-log-level=warn",
            "--no-conf", # عدم قراءة ملف aria2c.conf
            "-o", temp_filename, # اسم الملف الناتج
            "-d", DOWNLOADS_DIR, # مجلد التنزيل
            direct_url # الرابط
        ]

        # تشغيل aria2c ومراقبة الخرج باستخدام asyncio.create_subprocess_exec
        process = await asyncio.create_subprocess_exec(
            *aria2_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT # دمج stderr و stdout لتبسيط قراءة التقدم
        )

        last_update_time = time.time()
        print("Monitoring aria2c download process...")
        while True:
            try:
                # قراءة سطر واحد من خرج العملية
                line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
                if not line:
                    # إذا لم يتم قراءة سطر خلال المهلة، تحقق مما إذا كانت العملية قد انتهت
                    try:
                         returncode = await asyncio.wait_for(process.wait(), timeout=1.0)
                         if returncode is not None:
                              print(f"aria2c process finished reading stdout, return code: {returncode}")
                              break # العملية انتهت
                    except asyncio.TimeoutError:
                         continue # العملية ما زالت تعمل، استمر في القراءة


                line = line.decode('utf-8', errors='ignore').strip()

                # البحث عن خط معلومات التقدم من aria2c
                # مثال: [#a1b2c3 12MiB/35MiB(35%) CN:16 DL:2.3MiB ETA:19s]
                match = re.search(r'(\d+(?:\.\d+)?[KMG]iB)\/(\d+(?:\.\d+)?[KMG]iB)\((\d+(?:\.\d+)?)%\).*DL:(\d+(?:\.\d+)?[KMG]iB).*ETA:(\d+s)', line)

                if match:
                    downloaded = match.group(1)
                    total = match.group(2)
                    percent = match.group(3)
                    speed = match.group(4)
                    eta = match.group(5)

                    # نص رسالة التقدم
                    text = (
                        f"📥 جاري تحميل الفيديو...\n"
                        f"⬇️ النسبة: {percent}%\n"
                        f"💾 الحجم: {downloaded} / {total}\n"
                        f"⚡ السرعة: {speed}\n"
                        f"⏳ متبقي: {eta}"
                    )

                    # تحديث رسالة التقدم كل بضع ثوانٍ لتجنب الـ rate limit
                    if time.time() - last_update_time > 2:
                        try:
                            await progress_message.edit_text(text)
                            last_update_time = time.time()
                        except Exception as e:
                            # تجاهل الأخطاء الشائعة مثل MessageNotModified
                            if "MessageNotModified" not in str(e):
                                print(f"Error editing progress message: {e}")
                            pass

            except asyncio.TimeoutError:
                 # لم يتم قراءة أي سطر خلال الوقت المحدد، قد يكون التحميل عالقاً
                 print("Timeout waiting for aria2c output line. Process might be stuck or finished.")
                 # لا تكسر الحلقة هنا، العملية قد تطبع المزيد لاحقاً أو قد تكون معلقة فعلاً.
                 # يمكن إضافة منطق هنا للتحقق من نشاط العملية لفترة طويلة

            except Exception as e:
                print(f"Error monitoring aria2c output: {e}")
                break # الخروج من حلقة المراقبة في حالة وجود خطأ


        # انتظر انتهاء عملية aria2c بشكل نهائي
        returncode = await process.wait()
        print(f"aria2c process finished with return code: {returncode}")


        if returncode != 0:
            # التحقق من stderr/stdout في حالة الخطأ لفهم المشكلة
            stdout, stderr = await process.communicate() # اجمع الخرج المتبقي
            error_output = (stdout + stderr).decode('utf-8', errors='ignore')
            print(f"aria2c error output:\n{error_output}")

            await progress_message.edit_text(f"❌ فشل تحميل الفيديو باستخدام aria2c.\nخطأ: {error_output[:500]}") # عرض جزء من الخطأ

            # حذف أي ملف تم تحميله جزئيا
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception as e:
                     print(f"Error cleaning up partial download {local_path}: {e}")

            # لا تتابع إذا فشل التحميل
            return

        # التحقق النهائي من وجود الملف بعد التحميل
        if not os.path.exists(local_path):
             await progress_message.edit_text("❌ فشل تحميل الفيديو: الملف المحلي غير موجود بعد انتهاء aria2c.")
             return


        # حذف رسالة التقدم بعد انتهاء التحميل بنجاح
        try:
            await progress_message.delete()
        except Exception as e:
            print(f"Error deleting progress message after download: {e}")


        # إعلام المستخدم بالتحميل وإعداد البيانات للانتظار للحجم
        # احصل على حجم الملف المحمل فعلياً
        actual_downloaded_size_bytes = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        actual_downloaded_size_mb = actual_downloaded_size_bytes / (1024 * 1024)


        await message.reply_text(
            f"📥 تم تحميل الفيديو بنجاح!\n"
            f"الحجم الأصلي: {actual_downloaded_size_mb:.2f} MB\n\n"
            f"الآن، **أرسل رقماً صحيحاً أو عشرياً فقط** يمثل الحجم النهائي الذي تريده للفيديو بالميجابايت (مثال: `50`) لتحديد حجم الضغط.\n"
            f"أو أرسل `/cancel` للإلغاء.",
            quote=True
        )

        # تخزين بيانات الفيديو بانتظار حجم الضغط
        # استخدم chat_id كمفتاح للسماح للمستخدمين بإرسال فيديوهات متعددة، لكن يمكن لكل مستخدم معالجة فيديو واحد في كل مرة لتحديد الحجم
        user_video_data[chat_id] = { # استخدام chat_id
            'file_path': local_path,
            'original_message': message, # رسالة الفيديو الأصلية
            'status': 'waiting_size',
            'progress_message': None # رسالة التقدم لعملية الضغط ستُنشأ لاحقاً
        }


    except Exception as e:
        print(f"❌ Error in handle_video process: {e}")
        # تأكد من حذف رسالة التقدم الأصلية إذا حدث خطأ عام قبل تحديد الحجم
        try:
            await progress_message.delete()
        except Exception as del_e:
            print(f"Error deleting progress message on handle_video error: {del_e}")

        await message.reply_text(f"حدث خطأ أثناء تحميل الفيديو: {e}\nحاول مرة أخرى.")
        # تنظيف الملف المحلي في حالة وجود خطأ
        if 'local_path' in locals() and os.path.exists(local_path):
             try:
                 os.remove(local_path)
             except Exception as e:
                 print(f"Error deleting local file after error: {e}")
        # إزالة بيانات المستخدم من الانتظار (باستخدام chat_id)
        if chat_id in user_video_data:
            del user_video_data[chat_id]


@app.on_message(filters.text & filters.private & filters.create(lambda _, __, m: m.chat.id in user_video_data and user_video_data[m.chat.id].get('status') == 'waiting_size'))
async def handle_target_size(client, message: Message):
    """معالجة إدخال المستخدم لحجم الضغط المستهدف."""
    chat_id = message.chat.id

    # البيانات يجب أن تكون موجودة في user_video_data لأن الفلتر يضمن ذلك
    video_data = user_video_data.get(chat_id)
    # تأكيد إضافي
    if not video_data or video_data.get('status') != 'waiting_size':
        # هذا لا ينبغي أن يحدث مع الفلتر، لكن لضمان الصلابة
        return

    try:
        target_size_mb = float(message.text.strip())
        if target_size_mb <= 0:
            await message.reply_text("🔢 يرجى إدخال رقم موجب يمثل الحجم بالميجابايت.", quote=True)
            return

        # استخلاص البيانات من الانتظار (إزالة المستخدم من حالة الانتظار)
        # استخدم pop لإزالة العنصر بعد معالجته
        video_data = user_video_data.pop(chat_id)
        file_path = video_data['file_path']
        original_message = video_data['original_message'] # رسالة الفيديو الأصلية التي سيتم الرد عليها

        # التحقق من وجود الملف قبل البدء بالضغط
        if not os.path.exists(file_path):
            await message.reply_text("❌ حدث خطأ: لم يتم العثور على الملف الأصلي المطلوب ضغطه. ربما تم حذفه أو خطأ سابق.", quote=True)
            # لا تضع في قائمة الانتظالر، العملية انتهت بالخطأ هنا
            return

        # إرسال رسالة التقدم لعملية الضغط قبل الإضافة إلى قائمة الانتظار
        # نربط رسالة التقدم هذه برسالة المستخدم التي تحتوي على الرقم (الحجم)
        progress_message = await message.reply_text("🎬 إضافة الفيديو إلى قائمة الانتظار...", quote=True)

        # تحديث كائن رسالة التقدم في video_data قبل وضعها في القائمة
        video_data['progress_message'] = progress_message
        video_data['target_size_mb'] = target_size_mb
        video_data['user_id'] = message.from_user.id # حفظ user_id أيضاً


        # إضافة الفيديو إلى قائمة انتظار الضغط
        # قائمة الانتظار هي asyncio.Queue آمنة للمعالجة المتزامنة
        await video_compression_queue.put(video_data)
        print(f"Video for chat {chat_id} added to compression queue. Target size: {target_size_mb}MB")


        # بدء مهمة معالجة قائمة الانتظار إذا لم تكن قيد التشغيل بالفعل
        # استخدم processing_lock لتجنب بدء مهمات متعددة
        global is_processing
        if not is_processing:
             print("Compression processing task is not running. Starting it.")
             # تشغيل معالجة قائمة الانتظار في مهمة asyncio منفصلة
             # مهمة المعالجة ستخرج من loop إذا كانت القائمة فارغة وتعود للبحث عن عناصر جديدة.
             # نعتمد على process_video_compression loop أنها تنهي المهمة إذا لم تجد شيء لفترة
             # ولكن قد يكون من الأفضل إعادة تشغيل المهمة هنا فقط إذا تأكدنا أنها لا تعمل
             # طريقة أبسط للتأكد: إنشاء المهمة مرة واحدة عند بدء البوت في main().
             # بما أنني عدلت main() لبدء المهمة بالفعل، لا نحتاج لإعادة تشغيلها هنا.
             # ولكن ترك هذا المنطق في مكانه يمكن أن يكون مفيداً كنظام احتياطي أو إذا كان timeout في process_video_compression طويلاً.

             # إعادة منطق بدء المهمة هنا كنظام احتياطي فقط
             # async with processing_lock: # لا نحتاج للقفل فقط لفحص متغير
             #    if not is_processing:
             #         is_processing = True # نضبط الحالة هنا لمنع السباق الشرطي
             #         asyncio.create_task(process_video_compression())


    except ValueError:
        # إذا لم يكن الإدخال رقمًا، تبقى البيانات في user_video_data والسماح بالمحاولة مرة أخرى
        await message.reply_text("🔢 يرجى إدخال رقم صحيح أو عشري فقط يمثل الحجم بالميجابايت.", quote=True)
    except Exception as e:
        print(f"❌ Error in handle_target_size process: {e}")
        await message.reply_text(f"حدث خطأ غير متوقع أثناء معالجة الحجم المطلوب: {e}", quote=True)
        # في حالة وجود خطأ، قم بإزالة بيانات المستخدم لمنع تعليقه
        if chat_id in user_video_data:
             video_data = user_video_data.pop(chat_id)
             if 'file_path' in video_data and os.path.exists(video_data['file_path']):
                try:
                    os.remove(video_data['file_path'])
                    print(f"Deleted file after error in handle_target_size: {video_data['file_path']}")
                except Exception as e:
                    print(f"Error deleting file after error: {e}")

# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت (لاحظ أنها async)
async def check_channel(client: Client):
    """فحص والتعرف على القناة عند بدء تشغيل البوت."""
    # الانتظار قليلا للتأكد من أن البوت متصل
    await asyncio.sleep(2)
    # نستخدم CHANNEL_ID مباشرة من config.py بعد أن تأكدنا أنه int
    if not CHANNEL_ID or not isinstance(CHANNEL_ID, int):
        print("⚠️ CHANNEL_ID not configured correctly or is not an integer. Uploading compressed videos to channel is disabled.")
        return
    try:
        # تأكد أن CHANNEL_ID هو integer كما هو مطلوب من Pyrogram
        chat = await client.get_chat(CHANNEL_ID)
        print(f"تم التعرف على القناة: {chat.title} (ID: {CHANNEL_ID})")
    except Exception as e:
        print(f"خطأ في التعرف على القناة (ID: {CHANNEL_ID}): {e}")
        print("يرجى التأكد من أن CHANNEL_ID صحيح وأن البوت مسؤول في القناة ويمكنه إرسال المستندات.")

# دالة لتنظيف مجلد التنزيلات (لاحظ أنها async)
async def cleanup_downloads():
    """
    تنظيف مجلد التنزيلات عند بدء تشغيل البوت.
    """
    print("Starting cleanup of download directory...")
    if not os.path.exists(DOWNLOADS_DIR):
         print("Download directory does not exist.")
         return

    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            # تأكد من أنك تتعامل مع ملفات فقط وليس مجلدات فرعية بالخطأ
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
            # يمكن إضافة شرط هنا لحذف المجلدات الفرعية إن وجدت وفارغة
            # elif os.path.isdir(file_path) and not os.listdir(file_path):
            #     os.rmdir(file_path)
            #     print(f"Deleted empty directory: {file_path}")

        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")
    print("Cleanup finished.")


# تشغيل البوت باستخدام asyncio
if __name__ == "__main__":
    async def main():
        # تنظيف مجلد التنزيلات قبل بدء تشغيل البوت
        await cleanup_downloads()

        print("Starting bot...")
        # بدء اتصال الكلاينت
        await app.start()
        print("Bot started.")

        # تشغيل فحص القناة في مهمة asyncio منفصلة بعد بدء البوت
        asyncio.create_task(check_channel(app))

        # ابدأ مهمة معالجة قائمة الانتظار في الخلفية عند بدء التشغيل
        # هذه المهمة ستظل تعمل وتبحث في القائمة عن فيديوهات للمعالجة
        # إذا كانت القائمة فارغة لفترة، ستتوقف مؤقتاً. سيتم إعادة تشغيلها عند الحاجة.
        # طريقة أفضل لضمان عمل المهمة دائماً: لا تجعل process_video_compression تتوقف بال timeout
        # قم بتعديل process_video_compression لتنتظر دائماً video_compression_queue.get() بدون timeout.
        # وإذا كانت is_processing تستخدم للإشارة إلى حالة العملية، قم بإعادة فحصها وبدء المهمة فقط إذا لم تكن تعمل.
        # Let's ensure the processing task is started and kept alive.

        # الحل: اجعل process_video_compression تنتظر بلا نهاية، وابدأها مرة واحدة هنا.
        # remove timeout=1.0 from video_compression_queue.get() inside process_video_compression

        # بدء المهمة الأولى لمعالجة قائمة الانتظار
        asyncio.create_task(process_video_compression())
        print("Compression queue processing task started.")


        # انتظر حتى يتم إيقاف البوت (مثل تلقي إشارة إيقاف Ctrl+C)
        try:
            # Wait indefinitely for signals or events
            await asyncio.Future()
        except asyncio.CancelledError:
             # يحدث هذا إذا تم إلغاء المهمة الرئيسية
             print("Main task was cancelled.")
        except Exception as e:
             print(f"Unexpected error in main loop: {e}")

        # سيتم الوصول إلى هنا عند إيقاف حلقة asyncio الرئيسية


    try:
        # تشغيل الحلقة الرئيسية لـ asyncio
        # هذا سيقوم بتشغيل main() وينتظر حتى تنتهي (عادة عند إيقاف البوت)
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot received stop signal (KeyboardInterrupt/SystemExit). Stopping...")
        # عند تلقي إشارة إيقاف، يتم رفع استثناء KeyboardInterrupt أو SystemExit.
        # asyncio.run يتعامل مع هذا ويقوم بإيقاف حلقات asyncio بشكل منظم.

        # هنا يمكنك إضافة منطق انتظار لإنهاء المهام الجارية بشكل نظيف إذا لزم الأمر،
        # لكن في معظم الحالات، سيتم التعامل مع إيقاف المهام Async بشكل آلي.
        # لا نستخدم app.stop() هنا لأن asyncio.run يتولى ذلك غالباً.
        # لكن قد يكون من الضروري في سيناريوهات معينة.
        # إذا كنت بحاجة لإيقاف منظم للبوت والكلاينت، يمكن استخدام signals ومعالجة أكثر تعقيداً.


    # الكود هنا لا يتم الوصول إليه إلا إذا لم يتم استخدام asyncio.run(main())
    # app.run() كان يوضع هنا في Pyrogram v1.x.
    # في v2.x نستخدم asyncio.run لإدارة الحلقة الرئيسية وتشغيل الدوال async.

# إزالة استدعاءات التنظيف والبدء القديمة

# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت (النسخة القديمة بخيوط)
# def check_channel(): pass # removed

# تنظيف مجلد التنزيلات عند بدء تشغيل البوت (النسخة القديمة بخيوط)
# cleanup_downloads() # removed

# تشغيل فحص القناة في خيط منفصل (النسخة القديمة)
# threading.Thread(target=check_channel, daemon=True).start() # removed

# تشغيل البوت (النسخة القديمة blocking)
# app.run() # removed
