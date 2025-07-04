import os
import re
import tempfile
import threading
import time
import subprocess
import ffmpeg
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import (API_ID, API_HASH, API_TOKEN, CHANNEL_ID,
                    VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VIDEO_AUDIO_CODEC,
                    VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE)
app = Client(
    "botsize",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN
)

# --- إعداد المسارات والمتغيرات ---
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

user_video_data = {}  # chat_id -> dict مع بيانات الفيديو بانتظار الحجم
video_queue = []
processing_lock = threading.Lock()
is_processing = False

# --- دالة حساب الـ bitrate المطلوب ---
def calculate_bitrate(target_size_mb, duration_sec):
    # 1MB = 8192 kbits
    return int((target_size_mb * 8192) / duration_sec)

# --- دالة معالجة قائمة الانتظار وضغط الفيديو ---
def process_queue():
    global is_processing
    while video_queue:
        with processing_lock:
            if not video_queue:
                is_processing = False
                return

            video_data = video_queue.pop(0)
            is_processing = True

        file = video_data['file']
        message = video_data['message']
        temp_filename = None

        try:
            if not os.path.exists(file):
                message.reply_text("❌ لم يتم العثور على الملف.")
                continue

            # قراءة مدة الفيديو
            probe = ffmpeg.probe(file)
            duration_sec = float(probe['format']['duration'])

            # حجم الهدف من المستخدم
            target_size_mb = video_data.get('target_size_mb', 20)
            target_bitrate = calculate_bitrate(target_size_mb, duration_sec)

            # إنشاء الملف المؤقت
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
                temp_filename = temp_file.name

            ffmpeg_command = (
                f'ffmpeg -y -i "{file}" -b:v {target_bitrate}k -c:v {VIDEO_CODEC} '
                f'-preset medium -pix_fmt {VIDEO_PIXEL_FORMAT} -c:a {VIDEO_AUDIO_CODEC} '
                f'-b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} '
                f'-map_metadata -1 "{temp_filename}"'
            )

            print(f"🎬 FFmpeg Command: {ffmpeg_command}")
            subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True)
            print("✅ FFmpeg ضغط الفيديو بنجاح.")

            # إرسال الفيديو إلى القناة
            if CHANNEL_ID:
                message.reply_text("⬆️ جاري رفع الفيديو المضغوط إلى القناة...")
                app.send_document(
                    chat_id=CHANNEL_ID,
                    document=temp_filename,
                    caption=f"🎞️ الفيديو المضغوط إلى ~{target_size_mb}MB"
                )
                message.reply_text("✅ تم ضغط ورفع الفيديو بنجاح إلى القناة.")
            else:
                message.reply_text("✅ تم ضغط الفيديو. لكن لم يتم تحديد قناة للرفع.")

        except subprocess.CalledProcessError as e:
            print("❌ خطأ من FFmpeg!")
            print(f"stderr: {e.stderr.decode()}")
            message.reply_text("❌ حدث خطأ أثناء ضغط الفيديو.")
        except Exception as e:
            print(f"❌ General error: {e}")
            message.reply_text("❌ حدث خطأ غير متوقع أثناء المعالجة.")
        finally:
            if temp_filename and os.path.exists(temp_filename):
                os.remove(temp_filename)
            time.sleep(5)

    is_processing = False

# --- استقبال الفيديو وتحميله عبر aria2c مع عرض التقدم ---
@app.on_message(filters.video | filters.animation)
async def handle_video(client, message):
    try:
        file_id = message.video.file_id if message.video else message.animation.file_id

        # الحل المؤكد:
        file_info = await client.get_file(file_id)
        # إذا لا زال الخطأ:
        # file_info = [i async for i in client.get_file(file_id)][0]

        file_path = file_info.file_path
        file_name = os.path.basename(file_path)
        direct_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_path}"
        local_path = f"{DOWNLOADS_DIR}/{file_name}"
        # ...

        print(f"📥 Downloading from: {direct_url}")

        progress_message = await message.reply_text("🔽 بدأ تحميل الفيديو...")

        # أمر aria2c
        aria2_command = [
            "aria2c", "-x", "16", "-s", "16", "--summary-interval=1", "--console-log-level=warn",
            "-o", file_name, "-d", DOWNLOADS_DIR, direct_url
        ]

        process = subprocess.Popen(
            aria2_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        while True:
            line = process.stdout.readline()
            if not line:
                break

            match = re.search(
                r'(\d+(?:\.\d+)?[KMG]iB)/(\d+(?:\.\d+)?[KMG]iB)\((\d+(?:\.\d+)?)%\).*DL:(\d+(?:\.\d+)?[KMG]iB).*ETA:(\d+s)',
                line
            )

            if match:
                downloaded = match.group(1)
                total = match.group(2)
                percent = match.group(3)
                speed = match.group(4)
                eta = match.group(5)
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
                    pass

        process.wait()
        if process.returncode != 0:
            await progress_message.edit_text("❌ فشل تحميل الفيديو.")
            return

        try:
            await progress_message.delete()
        except:
            pass

        await message.reply_text("✅ تم تحميل الفيديو.\nالآن أرسل **رقم الحجم بالميجابايت** الذي تريده للفيديو (مثال: 50)")

        # حفظ بيانات الفيديو للمستخدم حتى يرسل الرقم
        user_video_data[message.chat.id] = {
            'file': local_path,
            'message': message
        }

    except Exception as e:
        print(f"❌ Error in handle_video: {e}")
        await message.reply_text("حدث خطأ أثناء تحميل الفيديو. حاول مرة أخرى.")

# --- التقاط رقم الحجم من المستخدم ووضعه في قائمة الانتظار ---
@app.on_message(filters.text & filters.private)
async def handle_target_size(client, message):
    chat_id = message.chat.id

    if chat_id not in user_video_data:
        return

    # قبول الرقم فقط (مع أو بدون MB)
    txt = message.text.strip().lower().replace('ميجا', '').replace('م', '').replace('mb', '')
    if not txt.isdigit():
        await message.reply_text("❌ أرسل رقمًا فقط يمثل الحجم بالميجابايت (مثال: 50)")
        return

    target_size_mb = int(txt)
    if target_size_mb < 5 or target_size_mb > 200:
        await message.reply_text("❌ الحجم يجب أن يكون بين 5 و200 ميجابايت.")
        return

    video_data = user_video_data.pop(chat_id)
    video_data['target_size_mb'] = target_size_mb
    video_queue.append(video_data)

    await message.reply_text(f"📦 جاري ضغط الفيديو إلى حوالي {target_size_mb}MB...")

    global is_processing
    if not is_processing:
        threading.Thread(target=process_queue).start()

# --- أمر /start ---
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply_text("👋 أرسل لي فيديو وسيتم ضغطه بالحجم الذي تختاره (أرسل الفيديو ثم الحجم المطلوب بالميجابايت).")


if __name__ == "__main__":
    app.run()
