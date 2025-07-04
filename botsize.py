# bot.py
import asyncio
import os
import re
import time
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from config import *

# --- الحالة العامة والمتغيرات ---

# مجلد لتخزين التنزيلات والملفات المضغوطة
DOWNLOADS_DIR = "./downloads"

# قاموس لتخزين بيانات فيديو المستخدم قبل الضغط
# المفتاح: chat_id, القيمة: {'file_path': str, 'duration': int, 'original_message': Message}
user_video_data = {}

# قائمة انتظار لمهام ضغط الفيديو
video_queue = asyncio.Queue()

# تهيئة عميل البوت
app = Client("pyro_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)


# --- دوال مساعدة ---

async def run_command(command: str):
    """تنفيذ أمر shell بشكل غير متزامن."""
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode('utf-8', 'ignore'), stderr.decode('utf-8', 'ignore')


# --- منطق البوت الأساسي ---

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    """الرد على أمر /start."""
    await message.reply_text(
        "أهلاً بك! أرسل لي فيديو أو رسوم متحركة (animation) وسأقوم بتهيئته للضغط."
    )

# 1. معالجة الفيديو أو الرسوم المتحركة الواردة
@app.on_message((filters.video | filters.animation) & filters.private)
async def handle_video(client: Client, message: Message):
    """
    يعالج الفيديو أو الرسوم المتحركة الواردة.
    يقوم بتنزيلها باستخدام aria2c ويطلب من المستخدم تحديد الحجم المستهدف.
    """
    if message.chat.id in user_video_data:
        await message.reply_text(
            "لديك بالفعل فيديو قيد المعالجة. يرجى إكمال العملية الحالية أولاً بإرسال الحجم المطلوب."
        )
        return

    media = message.video or message.animation
    if not media:
        await message.reply_text("عذراً، هذه الرسالة لا تحتوي على وسائط صالحة.")
        return

    sent_message = await message.reply_text("⏳ جارٍ التحضير لتنزيل الفيديو...")

    try:
        file = await client.get_file(media.file_id)
        download_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file.file_path}"
    except Exception as e:
        await sent_message.edit(f"❌ حدث خطأ أثناء الحصول على رابط التحميل: `{e}`")
        return

    # إعداد مسار التنزيل
    sanitized_filename = re.sub(r'[\\/*?:"<>|]', "", media.file_name or f"{media.file_unique_id}.mp4")
    download_path = os.path.join(DOWNLOADS_DIR, sanitized_filename)

    # أمر التحميل باستخدام aria2c
    aria2c_cmd = (
        f'aria2c --console-log-level=warn -c -x 16 -s 16 -k 1M '
        f'"{download_url}" '
        f'--dir="{DOWNLOADS_DIR}" '
        f'--out="{sanitized_filename}"'
    )
    
    # بدء التحميل وعرض التقدم
    process = await asyncio.create_subprocess_shell(aria2c_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    
    last_update_time = 0
    while process.returncode is None:
        line = await process.stdout.readline()
        if not line:
            break
        
        # تحليل مخرجات aria2c لاستخلاص التقدم
        progress_match = re.search(
            r'\[#(?:[a-f0-9]+)\s([\d\.]+(?:Ki|Mi|Gi)B)/([\d\.]+(?:Ki|Mi|Gi)B)\((\d+)%\)\s'
            r'.*?DL:\s*([\d\.]+(?:Ki|Mi|Gi)B/s)\sETA:\s*(\w+)',
            line.decode('utf-8', 'ignore').strip()
        )

        if progress_match:
            current_time = time.time()
            if current_time - last_update_time > 3:  # تحديث كل 3 ثوانٍ لتجنب أخطاء FloodWait
                downloaded, total, percent_str, speed, eta = progress_match.groups()
                percent = int(percent_str)
                done_blocks = '▰' * (percent // 10)
                empty_blocks = '▱' * (10 - (percent // 10))

                progress_text = (
                    f"**📥 جاري التحميل...**\n"
                    f"`{done_blocks}{empty_blocks}` ({percent}%)\n\n"
                    f"🗂️ **الحجم:** `{downloaded} / {total}`\n"
                    f"🚀 **السرعة:** `{speed}`\n"
                    f"⏱️ **الوقت المتبقي:** `{eta}`"
                )
                try:
                    await sent_message.edit_text(progress_text)
                    last_update_time = current_time
                except FloodWait as e:
                    await asyncio.sleep(e.x)
                except Exception:
                    pass
        await asyncio.sleep(0.1)

    await process.wait()
    
    if process.returncode != 0:
        stderr_output = (await process.stderr.read()).decode('utf-8', 'ignore')
        await sent_message.edit(f"❌ **فشل التحميل.**\n\n**الخطأ:**\n`{stderr_output[-500:]}`")
        if os.path.exists(download_path): os.remove(download_path)
        return

    await sent_message.delete()
    if not os.path.exists(download_path):
        await message.reply_text("❌ حدث خطأ، لم يتم العثور على الملف بعد اكتمال التحميل.")
        return

    user_video_data[message.chat.id] = {
        'file_path': download_path,
        'duration': media.duration or 0,
        'original_message': message
    }
    
    await message.reply_text(
        "✅ **تم تحميل الفيديو بنجاح!**\n\n"
        "الآن، أرسل الحجم النهائي المطلوب للفيديو **كرقم فقط بالميجابايت (MB)**.\n"
        "مثال: أرسل `50` لضغط الفيديو إلى حجم 50 ميجابايت."
    )


# 2. معالجة رسالة الحجم المستهدف
@app.on_message(filters.regex(r"^\d+$") & filters.private)
async def handle_target_size(client: Client, message: Message):
    """
    يعالج رسالة المستخدم التي تحدد الحجم المستهدف بالميجابايت.
    يحسب معدل البت (Bitrate) ويضيف المهمة إلى قائمة الانتظار.
    """
    chat_id = message.chat.id
    if chat_id not in user_video_data:
        await message.reply_text("🤔 لم أجد أي فيديو مرتبط بك. يرجى إرسال فيديو أولاً.")
        return

    video_data = user_video_data[chat_id]
    duration = video_data['duration']
    
    if duration is None or duration == 0:
        await message.reply_text("❌ لا يمكنني تحديد مدة الفيديو. لا يمكن المتابعة.")
        if os.path.exists(video_data['file_path']): os.remove(video_data['file_path'])
        del user_video_data[chat_id]
        return

    target_size_mb = int(message.text)
    
    # حساب معدل البت (Bitrate)
    audio_bitrate_kbps = int(re.sub(r'\D', '', VIDEO_AUDIO_BITRATE))
    total_bitrate_kbps = (target_size_mb * 1024 * 8) / duration
    video_bitrate_kbps = total_bitrate_kbps - audio_bitrate_kbps

    if video_bitrate_kbps <= 10: # معدل بت منخفض جدًا قد يسبب فشلًا
        await message.reply_text(
            f"❌ الحجم المطلوب ({target_size_mb} MB) صغير جدًا بالنسبة لمدة الفيديو.\n"
            f"هذا يؤدي إلى جودة منخفضة للغاية. يرجى اختيار حجم أكبر."
        )
        return

    # إضافة المهمة إلى قائمة الانتظار
    job = {
        'user_chat_id': chat_id,
        'input_path': video_data['file_path'],
        'video_bitrate': f"{int(video_bitrate_kbps)}k",
    }
    await video_queue.put(job)
    
    del user_video_data[chat_id]
    
    await message.reply_text(
        f"👍 **تمت إضافة طلبك إلى قائمة الانتظار.**\n"
        f"موقعك في الطابور: `{video_queue.qsize()}`\n\n"
        "سيتم إعلامك عند اكتمال الضغط."
    )


# 3. العامل الذي يعالج قائمة الانتظار
async def process_queue_worker():
    """عامل يعمل في الخلفية لمعالجة المهام من قائمة الانتظار."""
    while True:
        job = await video_queue.get()

        user_chat_id = job['user_chat_id']
        input_path = job['input_path']
        video_bitrate = job['video_bitrate']
        
        output_filename = f"compressed_{os.path.basename(input_path)}"
        output_path = os.path.join(DOWNLOADS_DIR, output_filename)
        
        status_message = await app.send_message(user_chat_id, "⚙️ جاري ضغط الفيديو...")

        # أمر FFmpeg
        ffmpeg_cmd = (
            f'ffmpeg -y -i "{input_path}" '
            f'-c:v {VIDEO_CODEC} -b:v {video_bitrate} -pix_fmt {VIDEO_PIXEL_FORMAT} '
            f'-preset {VIDEO_PRESET} -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} '
            f'-ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} "{output_path}"'
        )

        print(f"Executing FFmpeg for chat {user_chat_id}: {ffmpeg_cmd}")
        return_code, _, stderr = await run_command(ffmpeg_cmd)

        if return_code != 0:
            error_text = f"❌ حدث خطأ أثناء ضغط الفيديو.\n\n`{stderr[-1500:]}`"
            await status_message.edit(error_text)
        else:
            await status_message.edit("🚀 جاري رفع الفيديو المضغوط إلى القناة...")
            try:
                await app.send_video(
                    chat_id=CHANNEL_ID,
                    video=output_path,
                    caption=f"فيديو مضغوط للمستخدم `{user_chat_id}`"
                )
                await status_message.edit("🎉 **تم ضغط ورفع الفيديو بنجاح إلى القناة!**")
            except Exception as e:
                error_text = f"❌ تم ضغط الفيديو، ولكن فشل الرفع إلى القناة.\n\n`{e}`"
                await status_message.edit(error_text)
        
        # التنظيف
        if os.path.exists(input_path): os.remove(input_path)
        if os.path.exists(output_path): os.remove(output_path)

        video_queue.task_done()


# --- بدء تشغيل البوت ---
async def main():
    if not os.path.isdir(DOWNLOADS_DIR):
        os.makedirs(DOWNLOADS_DIR)

    await app.start()
    print("Bot started...")
    
    try:
        chat = await app.get_chat(CHANNEL_ID)
        print(f"Successfully connected to channel: {chat.title}")
    except Exception as e:
        print(f"CRITICAL: Could not access CHANNEL_ID ({CHANNEL_ID}). Error: {e}")
        print("Please check the channel ID and ensure the bot is an admin with post permissions.")
        
    asyncio.create_task(process_queue_worker())
    
    await asyncio.Event().wait() # إبقاء البوت يعمل إلى الأبد

if __name__ == "__main__":
    print("Starting bot...")
    print("Make sure 'ffmpeg' and 'aria2c' are installed and in your system's PATH.")
    # In Google Colab, run this in a cell first: !apt-get -y install aria2 ffmpeg
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
