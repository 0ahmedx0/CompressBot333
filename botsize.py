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
# if not isinstance(CHANNEL_ID, int):
#     raise ValueError("CHANNEL_ID in config.py must be an integer.")


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
        # تأكد من أن ffprobe موجود في PATH الخاص بالنظام
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
        audio_bitrate_str = VIDEO_AUDIO_BITRATE.lower().replace('k', '000')
        audio_bitrate_bps = int(audio_bitrate_str)
    except ValueError:
         print(f"Invalid VIDEO_AUDIO_BITRATE format: {VIDEO_AUDIO_BITRATE}. Using default 128000 bps.")
         audio_bitrate_bps = 128000 # قيمة افتراضية في حالة الخطأ

    # حساب حجم الصوت المتوقع بالبت ( bitrate الصوت * مدة الفيديو )
    audio_size_bits = audio_bitrate_bps * duration_seconds

    # حجم الفيديو المستهدف ( نطرح حجم الصوت المتوقع )
    target_video_size_bits = target_size_bits - audio_size_bits

    if target_video_size_bits <= 0:
        print("Target video size is too small after subtracting audio. Increase target size.")
        return None

    # Video Bitrate المطلوب ( بالبت لكل ثانية )
    video_bitrate_bps = target_video_size_bits / duration_seconds

    # للحصول على bitrate أكثر استقراراً، يمكن إضافة حد أدنى أو أقصى
    # if video_bitrate_bps < 500000: # مثال لحد أدنى 500k bit/s
    #     video_bitrate_bps = 500000
    # if video_bitrate_bps > 5000000: # مثال لحد أقصى 5000k bit/s
    #     video_bitrate_bps = 5000000

    return int(video_bitrate_bps)

# تهيئة العميل للبوت
app = Client(
    "video_compressor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN,
    plugins=dict(root="plugins")
)

async def progress_callback(current, total, client: Client, message: Message, caption: str = ""):
    """عرض تقدم عملية التحميل أو الرفع."""
    if total > 0:
        percent = f"{current / total * 100:.1f}%"
        # تحويل بايت إلى ميجابايت لعرض الحجم
        current_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        text = f"{caption}\n📥 النسبة: {percent}\n💾 الحجم: {current_mb:.1f}MB / {total_mb:.1f}MB"
    else:
        text = f"{caption}\n📥 جاري المعالجة..."

    try:
        # تعديل الرسالة الحالية لعرض التقدم
        # لا نعدل رسالة أخرى غير رسالة التقدم لتجنب التداخل
        # تأكد من أن الرسالة هي نفسها التي أنشأتها الدالة التي تستخدم progress_callback
        await message.edit_text(text)
    except:
        # تجاهل أي خطأ يحدث أثناء تعديل الرسالة (مثلاً: rate limit)
        pass


async def process_video_compression():
    """معالجة الفيديوهات الموجودة في قائمة انتظار الضغط."""
    global is_processing
    # هذا القفل يمنع بدء أكثر من نسخة من process_video_compression في نفس الوقت
    async with processing_lock:
        if is_processing:
            return # إذا كانت هناك عملية قيد التنفيذ، لا تبدأ واحدة جديدة
        is_processing = True
        print("Starting video compression queue processing...")

    # الحلقة الرئيسية لمعالجة قائمة الانتظار
    while True:
        try:
            # الحصول على فيديو جديد من قائمة الانتظار، بانتظار قصير لمنع الحظر الدائم
            # إذا كانت القائمة فارغة لثانية واحدة، سنخرج من الحلقة.
            video_data = await asyncio.wait_for(video_compression_queue.get(), timeout=1)

            file_path = video_data['file_path']
            target_size_mb = video_data['target_size_mb']
            message = video_data['message'] # رسالة الفيديو الأصلية من المستخدم
            progress_message_id = video_data['progress_message_id'] # معرف رسالة التقدم التي تم إنشاؤها مسبقاً
            user_id = video_data['user_id']

            # الحصول على كائن الرسالة التي سيتم تحديثها (رسالة التقدم)
            try:
                progress_message = await app.get_messages(chat_id=message.chat.id, message_ids=progress_message_id)
            except Exception as e:
                print(f"Error getting progress message {progress_message_id}: {e}")
                # إذا لم نتمكن من الحصول على رسالة التقدم، ربما لا داعي للمتابعة لهذه المهمة
                video_compression_queue.task_done()
                continue # انتقل إلى المهمة التالية في القائمة


            try:
                # التأكد من وجود الملف قبل البدء بالضغط
                if not os.path.exists(file_path):
                    print(f"Compression failed: File not found: {file_path}")
                    await progress_message.edit_text("❌ حدث خطأ: لم يتم العثور على الملف الأصلي للضغط.")
                    video_compression_queue.task_done()
                    continue

                # الحصول على مدة الفيديو باستخدام ffprobe
                duration = get_duration_from_ffprobe(file_path)
                if duration is None:
                    print(f"Compression failed: Could not get duration for file: {file_path}")
                    await progress_message.edit_text("❌ حدث خطأ في قراءة مدة الفيديو. تأكد من تثبيت FFmpeg و ffprobe بشكل صحيح.")
                    video_compression_queue.task_done()
                    continue

                # حساب Video Bitrate المستهدف
                target_bitrate_bps = calculate_video_bitrate(target_size_mb, duration)
                if target_bitrate_bps is None:
                    print(f"Compression failed: Could not calculate target bitrate for file: {file_path}")
                    await progress_message.edit_text("❌ حدث خطأ في حساب معدل البت المطلوب. ربما الحجم المطلوب صغير جداً بالنسبة لمدة الفيديو.")
                    video_compression_queue.task_done()
                    continue

                # تحديث رسالة التقدم
                await progress_message.edit_text("🔄 بدأ ضغط الفيديو...")

                # إنشاء ملف مؤقت لتخزين الفيديو المضغوط
                with tempfile.NamedTemporaryFile(suffix=TEMP_FILE_SUFFIX_VIDEO, delete=False) as temp_file:
                    compressed_file_path = temp_file.name

                # أمر FFmpeg لضغط الفيديو
                # استخدام المتغيرات من config.py والحجم المستهدف
                ffmpeg_command = [
                    'ffmpeg', '-y', '-i', file_path, # الإدخال
                    # إعدادات الفيديو
                    '-c:v', VIDEO_CODEC, # الترميز من config
                    '-pix_fmt', VIDEO_PIXEL_FORMAT, # تنسيق البكسل من config
                    # '-b:v', str(target_bitrate_bps), # معدل البت المحسوب
                    # استخدام CRF بدلاً من bitrate إذا كنت تريد الحجم التقريبي وجودة أفضل للحجم، أو bitrate إذا كان الحجم هو الأهم
                    # إذا استخدمت CRF، ستحتاج إلى معرفة أي CRF يناسب أي حجم تقريباً للمدة الزمنية هذه
                    # الخيار الأفضل هنا هو استخدام bitrate:
                    '-b:v', f"{target_bitrate_bps} bps", # تحديد معدل البت بالبت لكل ثانية
                    '-preset', VIDEO_PRESET,  # Preset من config
                    '-profile:v', VIDEO_PROFILE, # Profile من config
                    # يمكنك إضافة -vf scale=VIDEO_SCALE إذا كنت تريد تغيير الأبعاد
                    # يمكنك إضافة -r VIDEO_FPS إذا كنت تريد تغيير معدل الإطارات

                    # إعدادات الصوت
                    '-c:a', VIDEO_AUDIO_CODEC, # ترميز الصوت من config
                    '-b:a', VIDEO_AUDIO_BITRATE, # معدل بت الصوت من config
                    '-ac', str(VIDEO_AUDIO_CHANNELS), # عدد القنوات من config
                    '-ar', str(VIDEO_AUDIO_SAMPLE_RATE), # معدل العينة من config

                    '-map_metadata', '-1', # إزالة الميتاداتا
                    compressed_file_path # الإخراج
                ]

                print(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

                # تشغيل أمر FFmpeg ومراقبة تقدمه (FFmpeg يرسل التقدم إلى stderr عادةً)
                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )

                # وظيفة لمراقبة stderr وقراءة التقدم
                async def read_stderr(progress_message):
                    last_update_time = time.time()
                    # نحتاج للحصول على مدة الفيديو هنا مرة أخرى إذا لم تكن متوفرة بسهولة
                    # الأسهل هو تمريرها كمعامل
                    current_duration = get_duration_from_ffprobe(file_path) # قد تكون هذه المكالمة بطيئة، يفضل تمريرها

                    while True:
                        try:
                           line = await asyncio.wait_for(process.stderr.readline(), timeout=0.1) # انتظار قصير للسطر
                           if not line:
                               break
                           line = line.decode('utf-8', errors='ignore').strip()

                           # مثال لسُطر تقدم FFmpeg (قد يختلف التنسيق قليلاً)
                           # frame=  224 fps= 43 q=27.0 size=   425kB time=00:00:09.36 bitrate= 372.7kbits/s speed=1.79x
                           match_time = re.search(r'time=(\d{2}:\d{2}:\d{2}\.\d{2})', line)
                           if match_time:
                               current_time_str = match_time.group(1)
                               h, m, s_ms = current_time_str.split(':')
                               s, ms = s_ms.split('.')
                               current_seconds = int(h) * 3600 + int(m) * 60 + int(s) + float(ms) / 100

                               if current_duration and current_duration > 0:
                                    percentage = (current_seconds / current_duration) * 100
                                    text = f"🔄 جاري ضغط الفيديو...\n💪 النسبة: {percentage:.1f}%"
                                    if time.time() - last_update_time > 3: # تحديث الرسالة كل 3 ثوانٍ على الأقل
                                        try:
                                            await progress_message.edit_text(text)
                                            last_update_time = time.time()
                                        except:
                                            pass # تجاهل الأخطاء

                           # يمكنك أيضاً البحث عن bitrate أو speed إذا أردت عرضها

                        except asyncio.TimeoutError:
                            # لم يتم قراءة أي سطر خلال الوقت المحدد، استمر في الانتظار
                            continue
                        except Exception as e:
                            print(f"Error reading FFmpeg stderr: {e}")
                            break # الخروج من قراءة stderr في حالة وجود خطأ


                # تشغيل مراقبة التقدم في مهمة asyncio منفصلة
                progress_task = asyncio.create_task(read_stderr(progress_message))

                # انتظار انتهاء عملية FFmpeg
                stdout, stderr = await process.communicate()

                # إيقاف مهمة التقدم إذا كانت لا تزال تعمل
                progress_task.cancel()
                try:
                    await progress_task # محاولة الانتظار لإكمال الإلغاء
                except asyncio.CancelledError:
                    pass

                # فحص returncode للتأكد من نجاح عملية FFmpeg
                if process.returncode != 0:
                    print("FFmpeg error occurred!")
                    error_output = stderr.decode(errors='ignore') # استخدام errors='ignore' لتجنب مشاكل الترميز
                    print(f"FFmpeg stderr: {error_output}")
                    await progress_message.edit_text(f"❌ حدث خطأ أثناء ضغط الفيديو:\n`{error_output[:1000]}`") # عرض جزء من الخطأ
                else:
                    print("FFmpeg command executed successfully.")

                    await progress_message.edit_text("⬆️ جاري رفع الفيديو المضغوط...")

                    # رفع الفيديو المضغوط إلى القناة
                    if CHANNEL_ID:
                        try:
                            # التأكد من وجود الملف المضغوط قبل الرفع
                            if not os.path.exists(compressed_file_path):
                                await progress_message.edit_text("❌ حدث خطأ: لم يتم إنشاء ملف الفيديو المضغوط.")
                                video_compression_queue.task_done()
                                continue

                            await app.send_document(
                                chat_id=CHANNEL_ID,
                                document=compressed_file_path,
                                caption=f"فيديو مضغوط بالحجم المطلوب ({target_size_mb}MB) من {message.from_user.mention}",
                                progress=progress_callback, # استخدام دالة التقدم لعملية الرفع
                                progress_args=[app, progress_message, "⬆️ جاري الرفع إلى القناة..."] # تمرير argument للدالة
                            )
                            print(f"Compressed video uploaded to channel: {CHANNEL_ID}")

                            # حذف رسالة التقدم بعد الرفع الناجح (اختياري)
                            # await progress_message.delete()
                            # أو تحديثها بنجاح
                            await progress_message.edit_text("✅ تم ضغط الفيديو ورفعه بنجاح إلى القناة.")

                        except Exception as e:
                            print(f"Error uploading compressed video to channel: {e}")
                            await progress_message.edit_text(f"❌ حدث خطأ أثناء رفع الفيديو المضغوط إلى القناة:\n{e}")
                    else:
                        print("CHANNEL_ID not configured. Video not sent to channel.")
                        await progress_message.edit_text("⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط.")

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
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        print(f"Deleted original file: {file_path}")
                    except Exception as e:
                        print(f"Error deleting original file {file_path}: {e}")

                # إشارة إلى أن مهمة قائمة الانتظار قد اكتملت، السماح بالعنصر التالي في القائمة
                video_compression_queue.task_done()
                print("Video compression task done.")

        except asyncio.TimeoutError:
            # قائمة الانتظار فارغة، نخرج من الحلقة
            print("Compression queue is empty. Processing task will pause.")
            break
        except Exception as e:
            print(f"Error in video compression queue processing loop: {e}")
            # في حالة وجود خطأ، لا تنسخ task_done لعدم تجميد قائمة الانتظار إذا كان الخطأ داخلياً

    # عند الخروج من الحلقة (عندما تصبح قائمة الانتظار فارغة)، نعيد is_processing إلى False
    async with processing_lock:
        is_processing = False
        print("Video compression queue processing finished.")


@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    """الرد على أمر /start."""
    await message.reply_text("أرسل لي فيديو أو أنيميشن وسأقوم بضغطه لك إلى الحجم الذي تحدده.")

@app.on_message(filters.command("cancel") & filters.private)
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

        # يمكنك أيضاً محاولة حذف رسالة التقدم إذا كانت موجودة
        progress_message_id = video_data.get('progress_message_id')
        if progress_message_id:
            try:
                await app.delete_messages(chat_id=message.chat.id, message_ids=progress_message_id)
            except Exception as e:
                 print(f"Error deleting progress message on cancel: {e}")

        await message.reply_text("✅ تم إلغاء العملية الحالية.", quote=True)

    else:
        await message.reply_text("❌ ليس لديك أي عملية قيد التنفيذ حالياً للإلغاء.", quote=True)


@app.on_message(filters.video | filters.animation)
async def handle_video(client, message: Message):
    """معالجة الفيديوهات والأنيميشن الواردة."""
    user_id = message.from_user.id

    # التحقق من وجود فيديو آخر للمستخدم قيد الانتظار
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
    # احصل على اسم الملف من الرسالة، أو استخدم اسم افتراضي
    file_name = message.video.file_name if message.video and message.video.file_name else (message.animation.file_name if message.animation and message.animation.file_name else f"{file_id}.{'mp4' if message.video else 'gif'}")


    # استخدم file_id كجزء من اسم الملف المؤقت لضمان التفرد
    temp_filename = f"{file_id}_{file_name}"
    local_path = os.path.join(DOWNLOADS_DIR, temp_filename)

    print(f"📥 Starting download for file_id: {file_id} to {local_path}")

    # إرسال رسالة مؤقتة لعرض التقدم
    # استخدم reply_text مع quote=True لربط رسالة التقدم بالرسالة الأصلية
    progress_message = await message.reply_text("🔽 بدأ تحميل الفيديو...", quote=True)


    try:
        # تحميل الفيديو باستخدام aria2c
        # تأكد أن get_file تعمل وأن file_path متاح للاستخدام مع aria2c
        file_info = await client.get_file(file_id)
        # Pyrogram v2.x get_file قد لا يعيد دائماً file_path مناسب للاستخدام المباشر أو رابط مباشر
        # قد يكون التحميل المباشر عبر Telegram Bot API باستخدام getFile والتنزيل اليدوي أكثر موثوقية في بعض الحالات
        # لكن بما أنك تستخدم aria2c برابط مباشر، سنحاول بناء الرابط
        # بناء الرابط المباشر قد يتغير أو لا يكون متاحاً دائماً بنفس الطريقة.
        # Alternative: استخدام client.download_media() المباشرة (أبطأ ولكن أبسط)
        # local_path = await client.download_media(message, file_name=local_path, progress=progress_callback, progress_args=[client, progress_message, "🔽 جاري التحميل..."])
        # إذا كنت تريد الاستمرار مع aria2c:
        direct_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        print(f"Attempting to download with aria2c from: {direct_url}")


        # أمر aria2c
        # تأكد أن المسارات صحيحة
        aria2_command = [
            "aria2c", "-x", "16", "-s", "16", "--summary-interval=1", "--console-log-level=warn",
            "-o", temp_filename, "-d", DOWNLOADS_DIR, direct_url
        ]

        # تشغيل aria2c ومراقبة الخرج
        process = await asyncio.create_subprocess_exec(
            *aria2_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT # دمج stderr و stdout لتبسيط قراءة التقدم
        )

        last_update_time = time.time()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0) # انتظر قليلا لقراءة السطر
            if not line:
                # هذا if/else يجب أن تكون المسافة البادئة متساوية لهما
                if await asyncio.wait_for(process.wait(), timeout=5.0) is not None:
                    break # إذا انتهى aria2c بالفعل، نخرج
                else: # <--- لاحظ المسافة البادئة هنا، تتماشى مع الـ 'if' أعلاها
                    continue # لم نحصل على سطر لكن aria2c ما زال يعمل، استمر في الانتظار
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

                if time.time() - last_update_time > 2: # تحديث الرسالة كل 2 ثانية على الأقل
                    try:
                        await progress_message.edit_text(text)
                        last_update_time = time.time()
                    except:
                        pass  # تجاهل أي خطأ بسبب rate limit
        # تأكد من انتظار انتهاء العملية إذا لم تخرج من الحلقة عن طريق break
        returncode = await process.wait()
        print(f"aria2c process finished with return code: {returncode}")

        if returncode != 0:
            await progress_message.edit_text("❌ فشل تحميل الفيديو باستخدام aria2c.")
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


        # حذف رسالة التقدم بعد انتهاء التحميل
        try:
            await progress_message.delete()
        except Exception as e:
            print(f"Error deleting progress message after download: {e}")


        # إعلام المستخدم بالتحميل وإعداد البيانات للانتظار للحجم
        await message.reply_text(f"📥 تم تحميل الفيديو بنجاح!\nالحجم الأصلي: {file_size / (1024 * 1024):.2f} MB\n\nالآن، **أرسل رقماً صحيحاً أو عشرياً فقط** يمثل الحجم النهائي الذي تريده بالفيديو بالميجابايت (مثال: `50`) لتحديد حجم الضغط.", quote=True)

        # تخزين بيانات الفيديو بانتظار حجم الضغط
        user_video_data[user_id] = {
            'file_path': local_path,
            'message': message, # رسالة الفيديو الأصلية
            'status': 'waiting_size',
            'progress_message_id': None # رسالة التقدم لعملية الضغط ستُنشأ لاحقاً
        }


    except asyncio.TimeoutError:
        print("aria2c read timeout")
        await progress_message.edit_text("❌ فشل تحميل الفيديو: انتهى وقت انتظار بيانات التحميل.")
        if 'process' in locals() and process.returncode is None:
             process.terminate() # حاول إنهاء عملية aria2c
             await process.wait()
        if os.path.exists(local_path):
             try:
                 os.remove(local_path)
             except Exception as e:
                 print(f"Error cleaning up partial download {local_path}: {e}")

        # إزالة بيانات المستخدم من الانتظار
        if user_id in user_video_data:
             del user_video_data[user_id]

    except Exception as e:
        print(f"❌ Error in handle_video: {e}")
        await progress_message.edit_text(f"حدث خطأ أثناء تحميل الفيديو: {e}\nحاول مرة أخرى.")
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

    # تأكد من أن المستخدم ينتظر بالفعل تحديد الحجم
    if user_id not in user_video_data or user_video_data[user_id].get('status') != 'waiting_size':
        # إذا أرسل رقما وهو ليس في حالة الانتظار، لا تفعل شيئا
        # يمكنك إرسال رسالة تطلب منه إرسال فيديو أولا إذا أردت
        return

    try:
        target_size_mb = float(message.text.strip())
        if target_size_mb <= 0:
            await message.reply_text("🔢 يرجى إدخال رقم موجب يمثل الحجم بالميجابايت.", quote=True)
            return

        # استخلاص البيانات من الانتظار (إزالة المستخدم من حالة الانتظار)
        video_data = user_video_data.pop(user_id)
        file_path = video_data['file_path']
        original_message = video_data['message']

        # التحقق من وجود الملف قبل البدء بالضغط
        if not os.path.exists(file_path):
            await message.reply_text("❌ حدث خطأ: لم يتم العثور على الملف الأصلي المطلوب ضغطه. ربما تم حذفه.", quote=True)
            # لا تضع في قائمة الانتظار، العملية انتهت بالخطأ هنا
            return

        # إرسال رسالة التقدم لعملية الضغط قبل الإضافة إلى قائمة الانتظار
        # نربط رسالة التقدم هذه برسالة المستخدم التي تحتوي على الرقم (الحجم)
        progress_message = await message.reply_text("🎬 إضافة الفيديو إلى قائمة الانتظار...", quote=True)
        progress_message_id = progress_message.id


        # إضافة الفيديو إلى قائمة انتظار الضغط مع البيانات اللازمة
        video_data['target_size_mb'] = target_size_mb
        video_data['progress_message_id'] = progress_message_id # حفظ معرف رسالة التقدم الجديدة
        video_data['user_id'] = user_id # تأكد من حفظ معرف المستخدم للرجوع إليه
        await video_compression_queue.put(video_data) # وضع البيانات في قائمة الانتظار


        # لا تبدأ المعالجة هنا، عملية المعالجة تعمل بشكل مستمر في مهمة الخلفية process_video_compression
        # إذا لم تكن تعمل بالفعل، سيتم تشغيلها تلقائياً عند بدء تشغيل البوت


    except ValueError:
        await message.reply_text("🔢 يرجى إدخال رقم صحيح أو عشري فقط يمثل الحجم بالميجابايت.", quote=True)
        # البيانات لا تزال في user_video_data، لذا يمكن للمستخدم المحاولة مرة أخرى
    except Exception as e:
        print(f"❌ Error in handle_target_size: {e}")
        await message.reply_text(f"حدث خطأ غير متوقع أثناء معالجة الحجم المطلوب: {e}", quote=True)
        # في حالة وجود خطأ، قم بإزالة بيانات المستخدم لمنع تعليقه
        if user_id in user_video_data:
             video_data = user_video_data.pop(user_id)
             if 'file_path' in video_data and os.path.exists(video_data['file_path']):
                try:
                    os.remove(video_data['file_path'])
                    print(f"Deleted file after error in handle_target_size: {video_data['file_path']}")
                except Exception as e:
                    print(f"Error deleting file after error: {e}")


# دالة لفحص والتعرف على القناة عند بدء تشغيل البوت (لاحظ أنها async)
async def check_channel(client: Client):
    """فحص والتعرف على القناة عند بدء تشغيل البوت."""
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت بالكامل (قد لا تكون ضرورية جدا هنا)
    await asyncio.sleep(1) # تم تقليل وقت الانتظار
    # نستخدم CHANNEL_ID مباشرة من config.py بعد أن تأكدنا أنه int
    if not CHANNEL_ID:
        print("⚠️ CHANNEL_ID not configured. Uploading compressed videos to channel is disabled.")
        return
    try:
        # تأكد أن CHANNEL_ID هو integer كما هو مطلوب من Pyrogram
        chat = await client.get_chat(CHANNEL_ID)
        print("تم التعرف على القناة:", chat.title)
    except Exception as e:
        print("خطأ في التعرف على القناة:", e)
        print("يرجى التأكد من أن CHANNEL_ID صحيح وأن البوت مسؤول في القناة ويمكنه إرسال المستندات.")

# دالة لتنظيف مجلد التنزيلات (لاحظ أنها async)
async def cleanup_downloads():
    """
    تنظيف مجلد التنزيلات.
    """
    print("Starting cleanup...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            # تأكد من أنك لا تحاول حذف مجلدات فرعية إذا كان هناك
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
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
        asyncio.create_task(process_video_compression())
        print("Compression queue processing task started.")


        # انتظر حتى يتوقف البوت (إذا تم إيقافه بواسطة إشارة خارجية مثلا)
        # هذه الحلقة يمكن استخدامها للحفاظ على البوت يعمل
        await asyncio.Future() # ببساطة انتظر مهمة Future لا تنتهي بشكل طبيعي

    try:
        # تشغيل الحلقة الرئيسية لـ asyncio
        # سيقوم هذا باستدعاء main() وتشغيل الحلقة غير المتزامنة حتى يتم تلقي إشارة إيقاف
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped manually by KeyboardInterrupt.")
    except SystemExit:
         print("Bot stopped by SystemExit.")
    finally:
        # إيقاف الكلاينت بشكل نظيف عند الخروج
        # لا تكرر app.stop() إذا كان asyncio.run يدير الخروج
        # في هذا الهيكل، app.start() داخل main()، لذا main() ستنتظر حتى يتم إلغاؤها أو تنهي
        # عند إيقاف الحلقة الرئيسية (مثل KeyboardInterrupt)، يجب أن يتم التعامل مع إيقاف البوت بشكل آلي إلى حد كبير بواسطة asyncio
        # لكن من الجيد التأكد
        print("Attempting to stop the bot client...")
        try:
            # لا نستخدم await هنا لأننا خارج حلقة asyncio الرئيسية
            # أو يمكن إضافة إدارة خروج أكثر تعقيداً باستخدام signals و Task cancellation
             if app.is_connected:
                 app.stop() # استدعاء stop() هنا في سياق sync ليس الأفضل ولكنه محاولة عند الخروج المفاجئ
        except Exception as e:
            print(f"Error during bot stop: {e}")

        print("Bot shutdown sequence finished.")
