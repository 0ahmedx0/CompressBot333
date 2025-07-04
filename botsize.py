import os
import re
import asyncio
from pyrogram import Client, filters
from config import (
    API_ID, API_HASH, API_TOKEN, CHANNEL_ID,
    VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE,
    VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE
)

# مجلد التنزيلات المؤقت
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# تخزين بيانات الفيديوهات بعد التحميل قبل الضغط
user_video_data = {}  # key: chat_id, value: {'file_path': str, 'duration': int}

# قائمة انتظار لضغط الفيديوهات بالتسلسل
video_queue = []
is_processing = False  # علم لمعرفة ما إذا كانت عملية الضغط جارية

# تهيئة بوت Pyrogram v2.x
app = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN
)

@app.on_message(filters.video | filters.animation)
async def handle_media(client: Client, message):
    """
    عندما يرسل المستخدم فيديو أو أنيميشن:
    - استخرج رابط التحميل المباشر
    - حمّل الملف باستخدام aria2c مع تقدم
    - بعد الاكتمال، اطلب حجم النهاية بالميجابايت
    """
    # احصل على بيانات الملف من Telegram
    media = message.video or message.animation
    tg_file = await client.get_file(media.file_id)
    direct_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{tg_file.file_path}"

    # اسم الملف المؤقت
    filename = f"{message.chat.id}_{message.message_id}.mp4"
    out_path = os.path.join(DOWNLOADS_DIR, filename)

    # رسالة تقدم التنزيل
    progress_msg = await message.reply("بدء تحميل الفيديو...", quote=True)

    async def download_and_prompt():
        # تشغيل aria2c مع 16 اتصال متوازي
        proc = await asyncio.create_subprocess_exec(
            "aria2c", "-x", "16", "-s", "16",
            "-d", DOWNLOADS_DIR, "-o", filename, direct_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        pattern = re.compile(
            r'(\d+(?:\.\d+)?[KMG]iB)/(\d+(?:\.\d+)?[KMG]iB)\((\d+)%\).*DL:(\d+(?:\.\d+)?[KMG]iB).*ETA:(\d+[smhd])'
        )

        # قراءة سطور الإخراج وتحديث التقدم
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode().strip()
            m = pattern.search(text)
            if m:
                loaded, total, percent, speed, eta = m.groups()
                txt = (
                    f"تحميل الفيديو:\n"
                    f"{percent}%  |  {loaded}/{total}\n"
                    f"السرعة: {speed}  |  الوقت المتبقي: {eta}"
                )
                try:
                    await client.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=progress_msg.message_id,
                        text=txt
                    )
                except:
                    pass

        await proc.wait()

        # حذف رسالة التقدم
        try:
            await client.delete_messages(message.chat.id, progress_msg.message_id)
        except:
            pass

        # تخزين مسار الملف والمدة لمرحلة الضغط
        user_video_data[message.chat.id] = {
            "file_path": out_path,
            "duration": media.duration  # بالثواني
        }

        # طلب حجم النهاية من المستخدم
        await client.send_message(
            message.chat.id,
            "تم تحميل الفيديو بنجاح.\n"
            "أرسل **رقم فقط** يمثل الحجم النهائي المطلوب بالميجابايت (مثال: 50)."
        )

    # ابدأ عملية التحميل في مهمة غير متزامنة
    asyncio.create_task(download_and_prompt())


@app.on_message(filters.text & filters.regex(r'^\d+$'))
async def handle_size(client: Client, message):
    """
    عندما يرسل المستخدم رقمًا:
    - احسب bitrate المناسب بناءً على المدة
    - أضف المهمة إلى قائمة الانتظار للضغط
    """
    chat_id = message.chat.id
    if chat_id not in user_video_data:
        # لا يوجد ملف جاهز للضغط
        return

    info = user_video_data.pop(chat_id)
    file_path = info["file_path"]
    duration = info["duration"]
    target_mb = int(message.text)

    # حساب bitrate بالكيلو بت/ث
    # الحجم بالبايت = target_mb * 1024*1024
    # bitrate (bits/s) = size_bytes * 8 / duration
    # ثم نقسم على 1000 لتحويل إلى kb/s
    bitrate_k = int(target_mb * 1024 * 1024 * 8 / duration / 1000)

    # أضف إلى قائمة الانتظار
    video_queue.append({
        "chat_id": chat_id,
        "file_path": file_path,
        "bitrate_k": bitrate_k,
        "reply_to": message
    })

    await message.reply(
        "تمت إضافة الفيديو إلى قائمة الانتظار للضغط.\n"
        "سيتم تنفيذ الضغط بالتسلسل."
    )

    global is_processing
    if not is_processing:
        asyncio.create_task(process_queue(client))


async def process_queue(client: Client):
    """
    تنفيذ عمليات الضغط الموجودة في قائمة الانتظار بالتسلسل.
    """
    global is_processing
    is_processing = True

    while video_queue:
        item = video_queue.pop(0)
        chat_id = item["chat_id"]
        file_path = item["file_path"]
        bitrate_k = item["bitrate_k"]
        reply_to = item["reply_to"]

        # رسالة بداية الضغط
        compress_msg = await client.send_message(chat_id, "جاري ضغط الفيديو...")

        # مسار الفيديو المضغوط
        base = os.path.basename(file_path)
        name, _ = os.path.splitext(base)
        output_name = f"{name}_compressed.mp4"
        output_path = os.path.join(DOWNLOADS_DIR, output_name)

        # تنفيذ ffmpeg مع NVENC
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-hwaccel", "cuda",
            "-i", file_path,
            "-c:v", "h264_nvenc",
            "-b:v", f"{bitrate_k}k",
            "-preset", "fast",
            "-c:a", VIDEO_AUDIO_CODEC,
            "-b:a", VIDEO_AUDIO_BITRATE,
            "-ac", str(VIDEO_AUDIO_CHANNELS),
            "-ar", str(VIDEO_AUDIO_SAMPLE_RATE),
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        # قراءة أي إخراج (يمكن تجاهله أو إضافته لتقدم بسيط)
        await proc.wait()

        if proc.returncode != 0:
            await client.send_message(chat_id, "حدث خطأ أثناء ضغط الفيديو.")
            continue

        # رفع الملف المضغوط إلى القناة
        if CHANNEL_ID:
            try:
                await client.send_video(
                    chat_id=CHANNEL_ID,
                    video=output_path,
                    caption="الفيديو المضغوط"
                )
                await client.send_message(chat_id, "تم ضغط الفيديو ورفعه بنجاح إلى القناة.")
            except Exception as e:
                await client.send_message(chat_id, "حدث خطأ أثناء رفع الفيديو إلى القناة.")
        else:
            await client.send_message(chat_id, "لم يتم تهيئة قناة لرفع الفيديو المضغوط.")

        # تنظيف الملفات المؤقتة
        for path in (file_path, output_path):
            try:
                os.remove(path)
            except:
                pass

        # حذف رسالة الضغط
        try:
            await client.delete_messages(chat_id, compress_msg.message_id)
        except:
            pass

        # فاصل صغير قبل المهمة التالية
        await asyncio.sleep(1)

    is_processing = False


if __name__ == "__main__":
    app.run()
