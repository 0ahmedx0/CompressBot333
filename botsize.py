import os
import re
import json
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import (
    API_ID, API_HASH, API_TOKEN, CHANNEL_ID,
    VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE,
    VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE
)

# مجلد التنزيلات المؤقت
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# تخزين بيانات الفيديوهات قبل الضغط
user_video_data = {}  # key: chat_id, value: {'file_path': str, 'duration': int}

# قائمة انتظار لضغط الفيديوهات بالتسلسل
video_queue = []
is_processing = False

# تهيئة بوت Pyrogram v2.x
app = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN
)

@app.on_message(filters.regex(r'^https?://t\.me/([^/]+)/(\d+)$'))
async def handle_link(client: Client, message):
    """
    عندما يرسل المستخدم رابط قناة + رقم رسالة:
    - نستخرج الرابط المباشر عبر yt-dlp
    - ننزل الفيديو باستخدام aria2c مع تقدم
    - بعد التحميل، نعرض للمستخدم اختيار (ضغط الفيديو أو رفع بدون ضغط)
    """
    match = re.match(r'^https?://t\.me/([^/]+)/(\d+)$', message.text)
    channel_username, msg_id = match.groups()
    page_url = f"https://t.me/{channel_username}/{msg_id}"

    # 1) استخراج رابط الفيديو المباشر
    proc1 = await asyncio.create_subprocess_exec(
        "yt-dlp", "-g", page_url,
        stdout=asyncio.subprocess.PIPE
    )
    url_bytes, _ = await proc1.communicate()
    direct_url = url_bytes.decode().strip()

    # 2) استخراج مدة الفيديو (بالثواني) لاستخدامها لاحقًا
    proc_meta = await asyncio.create_subprocess_exec(
        "yt-dlp", "-j", page_url,
        stdout=asyncio.subprocess.PIPE
    )
    meta_bytes, _ = await proc_meta.communicate()
    try:
        meta = json.loads(meta_bytes)
        duration = meta.get("duration", 0)
    except:
        duration = 0

    # إعداد اسم ومكان الملف
    filename = f"{message.chat.id}_{msg_id}.mp4"
    out_path = os.path.join(DOWNLOADS_DIR, filename)

    # رسالة تقدم التنزيل
    progress_msg = await message.reply("🔄 بدء تحميل الفيديو...", quote=True)

    async def _download():
        proc2 = await asyncio.create_subprocess_exec(
            "aria2c", "-x", "16", "-s", "16",
            "-d", DOWNLOADS_DIR, "-o", filename, direct_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        pattern = re.compile(
            r'(\d+(?:\.\d+)?[KMG]iB)/(\d+(?:\.\d+)?[KMG]iB)\((\d+)%\).*DL:(\d+(?:\.\d+)?[KMG]iB).*ETA:(\d+[smhd])'
        )

        while True:
            line = await proc2.stdout.readline()
            if not line:
                break
            text = line.decode().strip()
            m = pattern.search(text)
            if m:
                loaded, total, percent, speed, eta = m.groups()
                txt = (
                    f"تحميل الفيديو:\n"
                    f"{percent}% | {loaded}/{total}\n"
                    f"السرعة: {speed} | الوقت المتبقي: {eta}"
                )
                try:
                    await client.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=progress_msg.message_id,
                        text=txt
                    )
                except:
                    pass

        await proc2.wait()
        # حذف رسالة التقدم
        try:
            await client.delete_messages(message.chat.id, progress_msg.message_id)
        except:
            pass

        # حفظ مسار الملف والمدة
        user_video_data[message.chat.id] = {
            "file_path": out_path,
            "duration": duration
        }

        # عرض أزرار الاختيار
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("ضغط الفيديو", callback_data="compress")],
            [InlineKeyboardButton("رفع بدون ضغط", callback_data="upload_raw")]
        ])
        await message.reply("✅ تم التحميل بنجاح. اختر الإجراء:", reply_markup=keyboard)

    # بدء التحميل في مهمة غير متزامنة
    asyncio.create_task(_download())


@app.on_callback_query(filters.regex(r'^compress$'))
async def on_compress(client: Client, callback_query):
    """
    عند الضغط على زر 'ضغط الفيديو':
    نطلب من المستخدم إرسال الحجم النهائي المطلوب بالميجابايت.
    """
    await callback_query.answer()
    await client.send_message(
        callback_query.message.chat.id,
        "📏 أرسل **رقم فقط** يمثل الحجم النهائي المطلوب بالميجابايت (مثال: 50)."
    )


@app.on_callback_query(filters.regex(r'^upload_raw$'))
async def on_upload_raw(client: Client, callback_query):
    """
    عند الضغط على زر 'رفع بدون ضغط':
    نرفع الفيديو الأصلي مرة أخرى للمستخدم ثم ننظف الملفات.
    """
    await callback_query.answer()
    chat_id = callback_query.message.chat.id
    info = user_video_data.pop(chat_id, None)
    if not info:
        return await client.send_message(chat_id, "⚠️ لا يوجد فيديو جاهز للرفع.")
    file_path = info["file_path"]
    await client.send_video(chat_id, video=file_path, caption="📤 الفيديو الأصلي")
    try:
        os.remove(file_path)
    except:
        pass


@app.on_message(filters.text & filters.regex(r'^\d+$'))
async def handle_size(client: Client, message):
    """
    عندما يرسل المستخدم رقم الحجم:
    - نحسب الـ bitrate
    - نضيف المهمة إلى قائمة الانتظار للضغط
    """
    chat_id = message.chat.id
    if chat_id not in user_video_data:
        return

    info = user_video_data.pop(chat_id)
    file_path = info["file_path"]
    duration = info["duration"]
    target_mb = int(message.text)

    # حساب bitrate (kb/s)
    bitrate_k = int(target_mb * 1024 * 1024 * 8 / max(duration, 1) / 1000)

    video_queue.append({
        "chat_id": chat_id,
        "file_path": file_path,
        "bitrate_k": bitrate_k,
    })

    await message.reply(
        "🕒 تمت إضافة الفيديو إلى قائمة الانتظار للضغط.\n"
        "سيتم تنفيذ الضغط بالتسلسل."
    )

    global is_processing
    if not is_processing:
        asyncio.create_task(process_queue(client))


async def process_queue(client: Client):
    """
    تنفيذ مهام الضغط واحدة تلو الأخرى.
    """
    global is_processing
    is_processing = True

    while video_queue:
        item = video_queue.pop(0)
        chat_id = item["chat_id"]
        file_path = item["file_path"]
        bitrate_k = item["bitrate_k"]

        compress_msg = await client.send_message(chat_id, "⚙️ جاري ضغط الفيديو...")

        base = os.path.basename(file_path)
        name, _ = os.path.splitext(base)
        output_name = f"{name}_compressed.mp4"
        output_path = os.path.join(DOWNLOADS_DIR, output_name)

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
        await proc.wait()

        if proc.returncode != 0:
            await client.send_message(chat_id, "❌ حدث خطأ أثناء ضغط الفيديو.")
            continue

        # رفع الفيديو المضغوط
        if CHANNEL_ID:
            try:
                await client.send_video(
                    chat_id=CHANNEL_ID,
                    video=output_path,
                    caption="✅ الفيديو المضغوط"
                )
                await client.send_message(chat_id, "🎉 تم ضغط الفيديو ورفعه بنجاح.")
            except:
                await client.send_message(chat_id, "❌ حدث خطأ أثناء رفع الفيديو المضغوط.")
        else:
            await client.send_message(chat_id, "⚠️ لم يتم تهيئة قناة للرفع.")

        # تنظيف الملفات المؤقتة
        for p in (file_path, output_path):
            try:
                os.remove(p)
            except:
                pass

        # حذف رسالة الضغط
        try:
            await client.delete_messages(chat_id, compress_msg.message_id)
        except:
            pass

        await asyncio.sleep(1)

    is_processing = False


if __name__ == "__main__":
    app.run()
