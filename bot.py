import os
import tempfile
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import *

def progress(current, total, message_type="User"): # Added message_type for clarity
    if total > 0:
        print(f"Uploading to {message_type}: {current / total * 100:.1f}%")
    else:
        print(f"Uploading to {message_type}...")

def channel_progress(current, total): # Using generic progress now, this is redundant
    progress(current, total, "Channel")

def download_progress(current, total):
    current_mb = current / (1024 * 1024)  # Convert bytes to MB
    print(f"Downloading: {current_mb:.1f} MB") # Show downloaded MB

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

user_video_data = {}

@app.on_message(filters.command("start"))
def start(client, message):
    message.reply_text("Send me a video and I will compress it for you.")

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    file = client.download_media(
        message.video.file_id if message.video else message.animation.file_id,
        progress=download_progress
    )

    if CHANNEL_ID: # Forward original video to channel immediately
        try:
            client.forward_messages(
                chat_id=CHANNEL_ID,
                from_chat_id=message.chat.id,
                message_ids=message.id
            )
            print(f"Original video forwarded to channel: {CHANNEL_ID}")
        except Exception as e:
            print(f"Error forwarding original video to channel: {e}")
        message.delete() # Delete original message from bot's chat

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("جوده ضعيفه", callback_data="crf_27"),
                InlineKeyboardButton("جوده متوسطه", callback_data="crf_23"),
                InlineKeyboardButton("جوده عاليه", callback_data="crf_18"),
            ],
            [
                InlineKeyboardButton("الغاء", callback_data="cancel_compression"),
            ]
        ]
    )
    reply_message = message.reply_text("اختر مستوى الجوده :", reply_markup=markup, quote=True)
    user_video_data[reply_message.id] = {'file': file, 'message': message, 'button_message_id': reply_message.id} # Store button message id


@app.on_callback_query()
def compression_choice(client, callback_query):
    message_id = callback_query.message.id

    if message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب. يرجى إرسال الفيديو مرة أخرى.", show_alert=True)
        return

    if callback_query.data == "cancel_compression":
        video_data = user_video_data.pop(message_id)
        file = video_data['file']
        try:
            os.remove(file)
        except Exception as e:
            print(f"Error deleting file: {e}")
        callback_query.message.delete() # Delete the button message
        callback_query.answer("تم إلغاء الضغط وحذف الفيديو.", show_alert=True)
        return # Stop processing further

    video_data = user_video_data[message_id] # Do not pop, keep data for re-compression
    file = video_data['file']
    message = video_data['message']
    # No button removal or message deletion here, buttons are kept

    callback_query.answer("جاري الضغط...", show_alert=False)

    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_filename = temp_file.name

    try:
        ffmpeg_command = ""
        if callback_query.data == "crf_27": # جوده ضعيفه
            if message.animation:
                ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
            else:
                ffmpeg_command = f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} -b:v 1000k -preset fast -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'
        elif callback_query.data == "crf_23": #  جوده متوسطه
            if message.animation:
                ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
            else:
                ffmpeg_command = f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} -b:v 1700k  -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'

        elif callback_query.data == "crf_18": #  جوده عاليه
            if message.animation:
                ffmpeg_command = f'ffmpeg -y -i "{file}" "{temp_filename}"'
            else:
                ffmpeg_command = f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} -b:v 2500k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'

        print(f"Executing FFmpeg command: {ffmpeg_command}")
        subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True)
        print("FFmpeg command executed successfully.")

        sent_to_user_message = message.reply_document(temp_filename, progress=progress) # Send to user and capture message

        if CHANNEL_ID: # Forward compressed video to channel without forward header
            try:
                client.forward_messages(
                    chat_id=CHANNEL_ID,
                    from_chat_id=message.chat.id, # Forward from user's chat with bot
                    message_ids=sent_to_user_message.id,
                    drop_forward_header=True # Added drop_forward_header=True
                )
                print(f"Compressed video forwarded to channel without forward header: {CHANNEL_ID}")
            except Exception as e:
                print(f"Error forwarding compressed video to channel: {e}")
        else:
            print("CHANNEL_ID not configured. Video not sent to channel.")


    except subprocess.CalledProcessError as e:
        print(f"FFmpeg error occurred!")
        print(f"FFmpeg stderr: {e.stderr.decode()}")
        message.reply_text("حدث خطأ أثناء ضغط الفيديو.")
    except Exception as e:
        print(f"General error: {e}")
        message.reply_text("حدث خطأ غير متوقع.")
    finally:
        # os.remove(file) # Removed this line to prevent deletion after first compression
        os.remove(temp_filename)

app.run()
