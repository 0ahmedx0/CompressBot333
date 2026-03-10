import os
import tempfile
import subprocess
import threading
import time
import re
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageEmpty
import math

from config import *

DOWNLOADS_DIR = "./downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

download_executor = ThreadPoolExecutor(max_workers=5)
compression_executor = ThreadPoolExecutor(max_workers=3)

user_states = {}
user_settings = {}

DEFAULT_SETTINGS = {
    "encoder": "h264_nvenc",
    "auto_compress": False,
    "auto_quality_value": 30
}

def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]

# ----------------------------------------------------

def cleanup_downloads():
    for f in os.listdir(DOWNLOADS_DIR):
        p = os.path.join(DOWNLOADS_DIR, f)
        try:
            os.remove(p)
        except:
            pass

def estimate_crf_for_target_size(file_path, target_size_mb, initial_crf=23):
    original_size_mb = os.path.getsize(file_path) / (1024*1024)

    if original_size_mb > target_size_mb:
        ratio = original_size_mb / target_size_mb
        return min(51, max(18, initial_crf + int((ratio-1)*5)))
    else:
        ratio = target_size_mb / original_size_mb
        return max(0, min(23, initial_crf - int((ratio-1)*5)))

def create_progress_bar(p):
    filled = int(p // 5)
    empty = 20 - filled
    return "[" + "█"*filled + "░"*empty + f"] {p:.1f}%"

# ----------------------------------------------------

app = Client(
    "video_compressor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN
)

user_video_data = {}

# ----------------------------------------------------

def process_video_for_compression(video_data):

    file_path = video_data["file"]
    message = video_data["message"]
    quality = video_data["quality"]
    user_id = video_data["user_id"]

    prefs = get_user_settings(user_id)
    encoder = prefs["encoder"]

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, dir=DOWNLOADS_DIR) as temp:
        out_file = temp.name

    if isinstance(quality, dict):
        target = quality["target_size"]
        quality_value = estimate_crf_for_target_size(file_path, target)
    else:
        if isinstance(quality, str):
            quality_value = int(quality.split("_")[1])
        else:
            quality_value = quality

    preset = "fast"
    if quality_value <= 18:
        preset = "slow"
    elif quality_value <= 23:
        preset = "medium"
    elif quality_value >= 27:
        preset = "veryfast"

    quality_param = "cq" if "nvenc" in encoder else "crf"

    cmd = f'ffmpeg -y -i "{file_path}" -c:v {encoder} -{quality_param} {quality_value} -preset {preset} -c:a aac "{out_file}"'

    progress_msg = message.reply_text(
        "🔄 جاري ضغط الفيديو... [░░░░░░░░░░░░░░░░░░░░] 0%"
    )

    try:
        process = subprocess.Popen(
            cmd,
            shell=True,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        duration = 0

        while True:
            line = process.stderr.readline()

            if not line:
                break

            m = re.search(r"time=(\d+):(\d+):(\d+.\d+)", line)
            if m and duration > 0:

                h,mn,s = map(float,m.groups())
                cur = h*3600+mn*60+s

                percent = min(100,(cur/duration)*100)

                try:
                    progress_msg.edit_text(
                        f"🔄 جاري ضغط الفيديو... {create_progress_bar(percent)}"
                    )
                except:
                    pass

        process.wait()

    except Exception as e:
        message.reply_text(f"خطأ أثناء الضغط\n`{e}`")
        return

    try:
        progress_msg.delete()
    except:
        pass

    size_mb = os.path.getsize(out_file)/(1024*1024)

    up_msg = message.reply_text(
        "📤 جاري رفع الفيديو..."
    )

    def up_progress(c,t):
        if t:
            p=(c/t)*100
            try:
                up_msg.edit_text(f"📤 رفع الفيديو... {create_progress_bar(p)}")
            except:
                pass

    message.reply_document(
        out_file,
        progress=up_progress,
        caption=f"الحجم بعد الضغط {size_mb:.2f} MB"
    )

    try:
        up_msg.delete()
    except:
        pass

    os.remove(out_file)
    os.remove(file_path)

# ----------------------------------------------------

@app.on_message(filters.command("start"))
def start(client,message):

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⚙️ الإعدادات",callback_data="settings")]]
    )

    message.reply_text(
        "أرسل فيديو وسيتم ضغطه",
        reply_markup=kb
    )

# ----------------------------------------------------

@app.on_message(filters.video | filters.animation)
def handle_video(client,message):

    msg = message.reply_text("📥 جاري تنزيل الفيديو...")

    file = client.download_media(message)

    msg.delete()

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ضعيفة CRF27",callback_data="crf_27"),
            InlineKeyboardButton("متوسطة CRF23",callback_data="crf_23"),
            InlineKeyboardButton("عالية CRF18",callback_data="crf_18")
        ],
        [
            InlineKeyboardButton("🎯 حجم محدد",callback_data="target_size_prompt"),
            InlineKeyboardButton("❌ إلغاء",callback_data="cancel")
        ]
    ])

    m = message.reply_text(
        "اختر جودة الضغط",
        reply_markup=markup
    )

    user_video_data[m.id] = {
        "message":message,
        "file":file,
        "quality":None,
        "user_id":message.from_user.id
    }

# ----------------------------------------------------

@app.on_message(filters.text)
def handle_text(client,message):

    uid = message.from_user.id

    if uid not in user_states:
        return

    state = user_states[uid]["state"]

    if state == "waiting_for_target_size":

        try:
            size=float(message.text)

            bid=user_states[uid]["button_message_id"]

            video_data=user_video_data.get(bid)

            if video_data:
                video_data["quality"]={"target_size":size}
                compression_executor.submit(process_video_for_compression,video_data)

                message.reply_text("بدأ الضغط للحجم المحدد")

        except:
            message.reply_text("أدخل رقم صحيح")

        del user_states[uid]

# ----------------------------------------------------

@app.on_callback_query()
def callbacks(client,query):

    data=query.data
    message=query.message
    uid=query.from_user.id

    if data=="cancel":
        try:
            message.delete()
        except:
            pass
        return

    if data=="target_size_prompt":

        prompt=message.reply_text(
            "🔢 أرسل الحجم المطلوب للملف المضغوط بالميغابايت:"
        )

        user_states[uid]={
            "state":"waiting_for_target_size",
            "prompt_message_id":prompt.id,
            "button_message_id":message.id
        }

        query.answer("أرسل الحجم الآن")
        return

    if data.startswith("crf_"):

        video_data=user_video_data.get(message.id)

        if not video_data:
            query.answer("انتهت الجلسة")
            return

        video_data["quality"]=data

        compression_executor.submit(
            process_video_for_compression,
            video_data
        )

        query.answer("بدأ الضغط")

# ----------------------------------------------------

cleanup_downloads()

print("🚀 البوت يعمل")

app.run()
