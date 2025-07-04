import os
import re
import math
import asyncio
import time
import tempfile
import subprocess
from pyrogram import Client, filters
from pyrogram.types import Message
from config import *
from functools import partial

# --- الإعدادات ---
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

user_video_data = {}
video_queue = asyncio.Queue()
processing_video = False

# --------- Utils ----------

def sizeof_fmt(num, suffix="B"):
    for unit in ["","K","M","G","T"]:
        if abs(num) < 1024.0:
            return f"{num:.2f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.2f}P{suffix}"

def calc_bitrate(target_size_mb, duration_sec):
    """احسب البت ريت المناسب لتحقيق الحجم النهائي المطلوب."""
    target_bytes = target_size_mb * 1024 * 1024
    # خصم الصوت (تقريباً 128kbps)
    audio_bitrate = 128 * 1024 // 8
    # معدل البت للفيديو = (الحجم المستهدف - الصوت) / مدة الفيديو (بالثواني)
    video_bitrate = ((target_bytes * 8) // duration_sec) - audio_bitrate
    # يجب أن يكون على الأقل 300kbps لتفادي تلف الفيديو
    return max(video_bitrate, 300_000)

async def edit_progress_message(app, chat_id, message_id, template, stop_event, get_progress):
    """حدث رسالة التقدم كل ثانية حتى انتهاء التنزيل أو الضغط."""
    last_text = ""
    while not stop_event.is_set():
        progress = get_progress()
        if progress:
            text = template.format(**progress)
            if text != last_text:
                try:
                    await app.edit_message_text(chat_id, message_id, text)
                    last_text = text
                except: pass
        await asyncio.sleep(1)

async def aria2c_download(url, dest, progress_cb):
    """حمل الملف باستخدام aria2c وأرجع True/False حسب النتيجة."""
    cmd = [
        "aria2c",
        "--max-connection-per-server=16", "--split=16",
        "--dir", os.path.dirname(dest),
        "--out", os.path.basename(dest),
        "--console-log-level=warn",
        "--summary-interval=0",
        url
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

    total = 0
    current = 0
    start_time = time.time()
    speed = 0
    eta = "?"
    last_report = 0

    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        s = line.decode("utf-8").strip()
        # مثال: [#f2e...b0 2.2MiB/123MiB(1%) CN:16 DL:1.2MiB ETA:1m30s]
        m = re.search(r'(\d+\.?\d*)([KMGT]?i?)B/(\d+\.?\d*)([KMGT]?i?)B\((\d+)%\).*DL:([\d.]+)([KMGT]?i?)B\s*ETA:([\w:]+)', s)
        if m:
            c, cu, t, tu, perc, sp, spu, eta = m.groups()
            units = {"":1, "K":1024, "M":1024**2, "G":1024**3}
            cur_bytes = float(c) * units.get(cu[0], 1)
            total_bytes = float(t) * units.get(tu[0], 1)
            speed_bytes = float(sp) * units.get(spu[0], 1)
            percent = int(perc)
            progress_cb({
                "current": cur_bytes,
                "total": total_bytes,
                "speed": speed_bytes,
                "eta": eta,
                "percent": percent
            })
    await proc.wait()
    return proc.returncode == 0

# --------- Pyrogram ---------
app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

# --- مرحلة استقبال الفيديو ---
@app.on_message(filters.video | filters.animation)
async def video_handler(client, message: Message):
    chat_id = message.chat.id

    file = message.video or message.animation
    file_id = file.file_id
    file_path = f"{DOWNLOADS_DIR}/{file_id}.mp4"

    # استخراج رابط التنزيل المباشر باستخدام async generator
    async for file_obj in client.get_file(file_id):
        download_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_obj.file_path}"
        break

    # إرسال رسالة التقدم للمستخدم
    progress = {"current": 0, "total": file.file_size, "speed": 0, "eta": "?", "percent": 0}
    progress_cb = lambda p: progress.update(p)
    msg = await message.reply(f"جاري التحميل...\n0%")
    stop_event = asyncio.Event()
    asyncio.create_task(edit_progress_message(
        client, chat_id, msg.id,
        "🔽 تحميل الفيديو:\n\n{percent}%\n{current}/{total}\nالسرعة: {speed}/ث\nالوقت المتبقي: {eta}",
        stop_event,
        lambda: {
            "percent": progress.get("percent", 0),
            "current": sizeof_fmt(progress.get("current", 0)),
            "total": sizeof_fmt(progress.get("total", 0)),
            "speed": sizeof_fmt(progress.get("speed", 0)),
            "eta": progress.get("eta", "?")
        }
    ))

    # تحميل الملف عبر aria2c
    ok = await aria2c_download(download_url, file_path, progress_cb)
    stop_event.set()
    await asyncio.sleep(1)
    await msg.delete()
    if not ok:
        await message.reply("حدث خطأ أثناء التحميل. جرب لاحقاً.")
        return

    # تخزين بيانات المستخدم مؤقتاً
    user_video_data[chat_id] = {
        "file_path": file_path,
        "duration": file.duration or 0,
        "message": message
    }
    await message.reply(
        "✅ تم تحميل الفيديو بنجاح.\n\nالآن أرسل **رقم فقط** يمثل الحجم النهائي المطلوب للملف المضغوط بالميجابايت (مثال: 50)"
    )

# --- استقبال الحجم (ميجابايت) ---
@app.on_message(filters.text & filters.private)
async def size_handler(client, message: Message):
    chat_id = message.chat.id
    if chat_id not in user_video_data:
        return

    try:
        size_mb = int(message.text.strip())
        assert 5 <= size_mb <= 2048  # السماح بأحجام معقولة فقط
    except:
        await message.reply("رجاء أرسل رقم فقط يمثل الحجم النهائي المطلوب (بين 5 إلى 2048 ميجابايت).")
        return

    # أضف الفيديو وقيمته لقائمة الانتظار
    user_video_data[chat_id]["target_size_mb"] = size_mb
    await video_queue.put(chat_id)
    await message.reply(f"تم إضافة الفيديو إلى قائمة الضغط. سيتم معالجته حسب الدور.")
    global processing_video
    if not processing_video:
        asyncio.create_task(process_queue(client))

# --- معالجة قائمة الانتظار بالتسلسل ---
async def process_queue(client):
    global processing_video
    processing_video = True
    while not video_queue.empty():
        chat_id = await video_queue.get()
        data = user_video_data.get(chat_id)
        if not data: continue
        file_path = data["file_path"]
        duration = data["duration"]
        size_mb = data["target_size_mb"]
        message = data["message"]

        # حساب bitrate
        bitrate = calc_bitrate(size_mb, duration or 1)
        # إعداد اسم مؤقت للفيديو المضغوط
        temp_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        temp_out = temp_file.name
        temp_file.close()
        # أرسل رسالة تقدم الضغط
        msg = await client.send_message(chat_id, "جاري ضغط الفيديو...")

        # أمر ffmpeg للضغط باستخدام GPU إن وجد
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", file_path,
            "-c:v", VIDEO_CODEC,    # h264_nvenc للـ GPU
            "-b:v", f"{bitrate}",
            "-maxrate", f"{bitrate}",
            "-bufsize", str(2*bitrate),
            "-preset", VIDEO_PRESET,
            "-pix_fmt", VIDEO_PIXEL_FORMAT,
            "-c:a", VIDEO_AUDIO_CODEC,
            "-b:a", VIDEO_AUDIO_BITRATE,
            "-ac", str(VIDEO_AUDIO_CHANNELS),
            "-ar", str(VIDEO_AUDIO_SAMPLE_RATE),
            "-movflags", "+faststart",
            temp_out
        ]
        # دالة تحديث رسالة التقدم
        def get_ffmpeg_progress():
            if os.path.exists(temp_out):
                size = os.path.getsize(temp_out)
                percent = min(int((size / (size_mb * 1024 * 1024)) * 100), 100)
                return f"ضغط الفيديو... ({sizeof_fmt(size)}/{size_mb}MB)\n{percent}%"
            else:
                return "جاري ضغط الفيديو..."

        # شغل ffmpeg مع تحديث كل 2 ثانية للرسالة
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd, stderr=asyncio.subprocess.PIPE
        )
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            if b"time=" in line:
                try:
                    await msg.edit_text(get_ffmpeg_progress())
                except: pass
        await process.wait()

        await msg.edit_text("رفع الفيديو المضغوط للقناة...")
        # أرسل للفناة (كـ فيديو)
        try:
            await client.send_video(
                chat_id=CHANNEL_ID,
                video=temp_out,
                caption=f"مضغوط حسب طلب المستخدم @{message.from_user.username if message.from_user else chat_id} إلى {size_mb}MB.",
                progress=partial(send_upload_progress, client, chat_id, message)
            )
        except Exception as e:
            await msg.edit_text("❌ فشل رفع الفيديو للقناة.")
        else:
            await msg.edit_text("✅ تم ضغط الفيديو ورفعه بنجاح للقناة.")
        # حذف الملفات المؤقتة
        try:
            os.remove(file_path)
            os.remove(temp_out)
        except: pass
        user_video_data.pop(chat_id, None)
        await asyncio.sleep(2)
    processing_video = False

async def send_upload_progress(client, chat_id, message, current, total):
    try:
        percent = int(current * 100 / total)
        await client.send_chat_action(chat_id, "upload_video")
    except: pass

# --- ابدأ البوت ---
if __name__ == "__main__":
    print("Bot is running...")
    app.run()
