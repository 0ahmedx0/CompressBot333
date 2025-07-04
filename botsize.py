import os
import re
import asyncio
import aiohttp
from pyrogram import Client, filters
from config import (
    API_ID, API_HASH, API_TOKEN, CHANNEL_ID,
    VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE,
    VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE
)

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

user_video_data = {}
video_queue = []
is_processing = False

app = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN
)

@app.on_message(filters.video | filters.animation)
async def handle_media(client: Client, message):
    media = message.video or message.animation
    file_id = media.file_id

    # 1) Call Bot API getFile to get the file_path
    async with aiohttp.ClientSession() as session:
        resp = await session.get(
            f"https://api.telegram.org/bot{API_TOKEN}/getFile",
            params={"file_id": file_id}
        )
        data = await resp.json()

    # Handle error responses from Telegram
    if not data.get("ok") or "result" not in data:
        description = data.get("description", "Unknown error")
        await message.reply(
            f"❌ خطأ في الحصول على رابط الملف من Telegram API:\n{description}"
        )
        return

    file_path = data["result"]["file_path"]
    direct_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_path}"

    filename = f"{message.chat.id}_{message.message_id}.mp4"
    out_path = os.path.join(DOWNLOADS_DIR, filename)

    progress_msg = await message.reply("بدء تحميل الفيديو...", quote=True)

    async def download_and_prompt():
        proc = await asyncio.create_subprocess_exec(
            "aria2c", "-x", "16", "-s", "16",
            "-d", DOWNLOADS_DIR, "-o", filename, direct_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        pattern = re.compile(
            r'(\d+(?:\.\d+)?[KMG]iB)/(\d+(?:\.\d+)?[KMG]iB)\((\d+)%\).*DL:(\d+(?:\.\d+)?[KMG]iB).*ETA:(\d+[smhd])'
        )

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
        try:
            await client.delete_messages(message.chat.id, progress_msg.message_id)
        except:
            pass

        user_video_data[message.chat.id] = {
            "file_path": out_path,
            "duration": media.duration
        }
        await client.send_message(
            message.chat.id,
            "✅ تم تحميل الفيديو بنجاح.\n"
            "أرسل **رقم فقط** يمثل الحجم النهائي المطلوب بالميجابايت (مثال: 50)."
        )

    asyncio.create_task(download_and_prompt())


@app.on_message(filters.text & filters.regex(r'^\d+$'))
async def handle_size(client: Client, message):
    chat_id = message.chat.id
    if chat_id not in user_video_data:
        return

    info = user_video_data.pop(chat_id)
    file_path = info["file_path"]
    duration = info["duration"]
    target_mb = int(message.text)

    bitrate_k = int(target_mb * 1024 * 1024 * 8 / duration / 1000)

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

        if CHANNEL_ID:
            try:
                await client.send_video(
                    chat_id=CHANNEL_ID,
                    video=output_path,
                    caption="✅ الفيديو المضغوط"
                )
                await client.send_message(chat_id, "🎉 تم ضغط الفيديو ورفعه بنجاح إلى القناة.")
            except Exception:
                await client.send_message(chat_id, "❌ حدث خطأ أثناء رفع الفيديو إلى القناة.")
        else:
            await client.send_message(chat_id, "⚠️ لم يتم تهيئة قناة لرفع الفيديو المضغوط.")

        for p in (file_path, output_path):
            try: os.remove(p)
            except: pass

        try:
            await client.delete_messages(chat_id, compress_msg.message_id)
        except: pass

        await asyncio.sleep(1)

    is_processing = False


if __name__ == "__main__":
    app.run()
