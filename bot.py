import os
import tempfile
import subprocess
import threading
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import *
import time

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

def auto_select_medium_quality(button_message_id):
    if button_message_id in user_video_data:
        client = app  # Access the client from the outer scope
        try:
            #client.answer_callback_query( # Removed client.answer_callback_query as it's causing errors
            #    callback_query_id=None, # Pass None instead of dummy string ID
            #    text="تم اختيار الجودة المتوسطة تلقائيًا.",
            #    show_alert=False
            #) # Removed this line causing error
            compression_choice(client, user_video_data[button_message_id]['dummy_callback_query']) # Call compression_choice with dummy callback
            print(f"Auto-selected medium quality for message ID: {button_message_id}")
        except Exception as e:
            print(f"Error auto-selecting medium quality: {e}")
        finally:
            pass # Removed data cleanup from here - Keep data for re-compression
            #if button_message_id in user_video_data:
            #    del user_video_data[button_message_id] # Clean up data after auto-selection


@app.on_message(filters.command("start"))
def start(client, message):
    message.reply_text("Send me a video and I will compress it for you.")

@app.on_message(filters.video | filters.animation)
def handle_video(client, message):
    user_video_data.clear() # Clear old data when new video is received
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
    button_message_id = reply_message.id

    # Create a dummy CallbackQuery object for auto-selection
    class DummyCallbackQuery:
        def __init__(self, message, data):
            self.message = message
            self.data = data
        def answer(self, text, show_alert):
            print(f"DummyCallbackQuery Answer: {text}, show_alert={show_alert}") # Optional logging

    dummy_callback_query = DummyCallbackQuery(reply_message, "crf_23")


    user_video_data[button_message_id] = {
        'file': file,
        'message': message,
        'button_message_id': button_message_id,
        'timer': threading.Timer(30, auto_select_medium_quality, args=[button_message_id]),
        'dummy_callback_query': dummy_callback_query,
        #'callback_query_id': "dummy_callback_id" # Dummy ID - Not needed anymore, removed this line
    } # Store button message id and timer

    user_video_data[button_message_id]['timer'].start() # Start the timer


@app.on_callback_query()
def compression_choice(client, callback_query):
    message_id = callback_query.message.id

    if message_id not in user_video_data:
        callback_query.answer("انتهت صلاحية هذا الطلب. يرجى إرسال الفيديو مرة أخرى.", show_alert=True)
        return

    if callback_query.data == "cancel_compression":
        video_data = user_video_data.pop(message_id) # pop for cancel - keep this
        file = video_data['file']
        try:
            os.remove(file)
        except Exception as e:
            print(f"Error deleting file: {e}")
        callback_query.message.delete() # Delete the button message
        callback_query.answer("تم إلغاء الضغط وحذف الفيديو.",show_alert=False)
        return # Stop processing further


    video_data = user_video_data[message_id] # Do not pop for quality selection - keep this

    if video_data['timer'].is_alive():
        video_data['timer'].cancel() # Cancel the timer if user chose quality in time
        print(f"Timer cancelled for message ID: {message_id}")

    # user_video_data.pop(message_id) # Removed pop from here - Keep data for re-compression

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
                ffmpeg_command = f'ffmpeg -y -i "{file}" -c:v {VIDEO_CODEC} -pix_fmt {VIDEO_PIXEL_FORMAT} -b:v 2200k -preset medium -c:a {VIDEO_AUDIO_CODEC} -b:a {VIDEO_AUDIO_BITRATE} -ac {VIDEO_AUDIO_CHANNELS} -ar {VIDEO_AUDIO_SAMPLE_RATE} -profile:v high -map_metadata -1 "{temp_filename}"'

        print(f"Executing FFmpeg command: {ffmpeg_command}")
        subprocess.run(ffmpeg_command, shell=True, check=True, capture_output=True)
        print("FFmpeg command executed successfully.")

        sent_to_user_message = message.reply_document(temp_filename, progress=progress) # Send to user and capture message

        if CHANNEL_ID: # Check if CHANNEL_ID is configured
            try:
                client.forward_messages(
                    chat_id=CHANNEL_ID,
                    from_chat_id=message.chat.id, # Forward from user's chat with bot
                    message_ids=sent_to_user_message.id # Forward the message sent to user
                )
                print(f"Compressed video forwarded to channel: {CHANNEL_ID}")
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
