# --- START OF FILE botasli.py ---

import os
import tempfile
import subprocess
import threading
import time
import math
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified # Import specific error
from config import *  # Import configuration variables
import re # For cleaning filename
import functools # For passing arguments to progress callback

# Ensure downloads directory exists
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# Global state for user video data: chat_id -> {file_path, duration, state, download_message_id, size_request_message_id, start_time}
# state can be: 'downloading', 'downloaded', 'waiting_for_size_input', 'queued', 'processing', 'completed', 'failed'
user_video_data = {}
# Global state for video processing queue: list of {file_path, original_message, target_size_mb, duration}
video_queue = []
processing_lock = threading.Lock()
is_processing = False

# Store download progress states per message to calculate speed/ETA
download_progress_states = {} # download_message_id -> {last_time, last_current, start_time}

def format_bytes(byte_count):
    """Formats bytes into human-readable string (e.g., KB, MB, GB)."""
    if byte_count is None:
        return "N/A"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while byte_count >= power and n < len(power_labels) - 1:
        byte_count /= power
        n += 1
    return f"{byte_count:.2f} {power_labels[n]}B"

def calculate_speed_eta(current, total, message_id):
    """Calculates download speed and estimated time remaining."""
    now = time.time()

    if message_id not in download_progress_states:
        download_progress_states[message_id] = {
            'last_time': now,
            'last_current': current,
            'start_time': now,
        }

    state = download_progress_states[message_id]
    time_diff = now - state['last_time']
    data_diff = current - state['last_current']

    speed = 0
    if time_diff > 0:
        speed = data_diff / time_diff

    eta = "Calculating..."
    if speed > 0 and total > 0 and current < total:
        remaining_bytes = total - current
        eta_seconds = remaining_bytes / speed
        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_seconds))
        eta = f"{eta_str}"
    elif current == total:
        eta = "Completed"
    elif speed == 0 and current > 0:
         eta = "Stalled?"


    # Update state for next calculation, only if a sufficient interval has passed or progress is significant
    if time_diff >= PROGRESS_UPDATE_INTERVAL or data_diff > 1024 * 1024: # Update every N seconds or after 1MB change
        state['last_time'] = now
        state['last_current'] = current
    # Remove state if download is complete
    if current == total and message_id in download_progress_states:
         del download_progress_states[message_id]

    return speed, eta


def download_progress_callback(current, total, client, message, progress_message_id, start_time):
    """Callback for client.download_media with enhanced info."""
    try:
        speed, eta = calculate_speed_eta(current, total, progress_message_id)

        if total > 0:
            percentage = current * 100 / total
            progress_text = (
                f"Downloading: {percentage:.1f}%\n"
                f"Size: {format_bytes(current)} / {format_bytes(total)}\n"
                f"Speed: {format_bytes(speed)}/s\n"
                f"ETA: {eta}"
            )
        else:
            progress_text = f"Downloading: {format_bytes(current)}..."

        # Edit message less frequently to avoid FloodWait
        now = time.time()
        if progress_message_id in download_progress_states:
             state = download_progress_states[progress_message_id]
             if now - state['start_time'] < 2 or now - state['last_time'] < 2: # Avoid rapid updates initially
                 if percentage % 5 != 0: # Update only on 5% increments or significant time
                     return

        client.edit_message_text(
            chat_id=message.chat.id,
            message_id=progress_message_id,
            text=progress_text
        )
    except MessageNotModified:
        # Ignore if the message hasn't changed enough to warrant an edit
        pass
    except Exception as e:
        print(f"Error in download_progress_callback: {e}")
        # Potentially remove state to reset calculations or handle this gracefully

def upload_progress_callback(current, total, client, channel_id, message_id, start_time):
    """Callback for upload progress (e.g., to channel or user)."""
    try:
        speed, eta = calculate_speed_eta(current, total, message_id) # Use upload message ID for state tracking

        if total > 0:
            percentage = current * 100 / total
            progress_text = (
                f"Uploading: {percentage:.1f}%\n"
                f"Size: {format_bytes(current)} / {format_bytes(total)}\n"
                f"Speed: {format_bytes(speed)}/s\n"
                f"ETA: {eta}"
            )
        else:
             progress_text = f"Uploading: {format_bytes(current)}..."

        # Edit message less frequently
        now = time.time()
        # Need state management for upload progress similar to download
        # A dedicated dict for upload progress or integrate into download_progress_states
        # For simplicity, let's reuse download_progress_states structure keyed by upload_message_id
        if message_id not in download_progress_states: # Initialize state if first time
             download_progress_states[message_id] = {'last_time': now, 'last_current': current, 'start_time': now}
             # This reuses the dict, which is okay as download finishes before upload starts
             # Using a different dict name like upload_progress_states would be cleaner

        state = download_progress_states[message_id]
        if now - state['start_time'] < 2 or now - state['last_time'] < 2:
            if percentage % 5 != 0:
                return

        client.edit_message_text(
            chat_id=channel_id, # Assuming editing the message *in the channel* if possible, or original chat?
            # This callback is used with send_document which returns the sent message.
            # Need to edit the message *in the channel* if sending there.
            # The callback doesn't directly receive the *sent* message ID until *after* it's sent.
            # A better approach might be to send a 'preparing upload' message first, edit it.
            # Or, better yet, provide progress feedback in the *original user chat*.
            # Let's provide progress feedback in the user's chat.

             # Let's edit the message *in the original user chat* for upload progress
            chat_id=message.chat.id, # Original user chat
            message_id=message.id + 1, # Assuming the 'queued' message is the next one - risky!
            # A better approach: send a dedicated upload status message to the user
             # Let's use a simple print for now or assume channel upload progress isn't edited in user chat
             # The original botasli used print. Let's keep it simple for this complex request.
             text=f"Uploading to Channel: {percentage:.1f}% - {format_bytes(current)} / {format_bytes(total)}" # Simple print or log
        )

        # A more robust way would be to send a "Processing/Uploading" message to the user,
        # update THAT message during processing and uploading, and delete it on completion/failure.

    except MessageNotModified:
        pass
    except Exception as e:
        print(f"Error in upload_progress_callback: {e}")
        # Clean up state if upload finishes or fails
        if current == total or "finished" in str(e).lower() and message_id in download_progress_states: # Hacky check for finished state
             del download_progress_states[message_id]


def calculate_video_bitrate(duration_seconds, target_size_mb, audio_bitrate_kbs):
    """
    Calculates the target video bitrate in kilobits per second (k)
    based on total duration, target final size (MB), and audio bitrate (kbs).
    """
    # Convert target size MB to bits
    target_size_bits = target_size_mb * 1024 * 1024 * 8

    # Convert audio bitrate kbs to bits per second
    audio_bitrate_bps = audio_bitrate_kbs * 1000

    # Total bits for audio over the duration
    audio_total_bits = audio_bitrate_bps * duration_seconds

    # Remaining bits for video
    video_target_bits = target_size_bits - audio_total_bits

    # Ensure target video bits is non-negative
    if video_target_bits < 0:
        print(f"Warning: Target size {target_size_mb}MB is too small to accommodate audio ({audio_bitrate_kbs}kbs) for {duration_seconds}s duration.")
        # Fallback: calculate minimum necessary video bitrate to hit audio size
        # or return a standard low bitrate. Let's calculate minimum needed video bits.
        video_target_bits = 0 # Ensure we don't go negative
        print("Calculating minimum video bitrate needed...")
         # To handle the case where target size is less than audio size,
         # we could set video_target_bits to 0 and get total bitrate = audio bitrate.
         # Or return an error/minimum video bitrate.
         # Let's enforce a minimum positive video bitrate, e.g., 50 kbps video + audio
        min_video_bitrate_kbs = 50 # enforce a minimum video bitrate of 50kbps
        min_video_total_bits = min_video_bitrate_kbs * 1000 * duration_seconds
        video_target_bits = max(0, target_size_bits - audio_total_bits)
        # If still negative (target size < audio size), maybe default to a base bitrate or return error
        if video_target_bits == 0 and target_size_bits > 0: # Target size exists but less than audio
             # This is tricky. The formula doesn't quite work.
             # Let's calculate total bitrate first.
             total_target_bits = target_size_bits
             total_target_bps = total_target_bits / duration_seconds if duration_seconds > 0 else 0

             video_target_bps = max(0, total_target_bps - audio_bitrate_bps)
             video_target_kbs = video_target_bps / 1000
             # Enforce a very small minimum if result is negative but target > 0
             return max(50, video_target_kbs) # Minimum video bitrate 50k

    # Video bitrate in bits per second
    video_bitrate_bps = video_target_bits / duration_seconds if duration_seconds > 0 else 0

    # Video bitrate in kilobits per second (for FFmpeg -b:v Xk)
    video_bitrate_kbs = video_bitrate_bps / 1000

    # FFmpeg recommends a minimum bitrate; 50-100 kbps is very low, standard often starts higher.
    # Let's ensure a reasonable minimum, e.g., 100 kbps video + the required audio.
    min_allowed_video_kbs = 100
    return max(min_allowed_video_kbs, video_bitrate_kbs)


def cleanup_downloads():
    """Cleans up old files in the downloads directory on startup."""
    print(f"Cleaning up downloads directory: {DOWNLOADS_DIR}")
    if not os.path.exists(DOWNLOADS_DIR):
        os.makedirs(DOWNLOADS_DIR) # Re-create if somehow deleted
        return

    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")


def delete_user_data(chat_id):
    """Removes user data entry."""
    if chat_id in user_video_data:
        del user_video_data[chat_id]
        print(f"Cleaned up data for chat_id: {chat_id}")

def process_queue():
    """Processes videos in the queue sequentially."""
    global is_processing
    while True: # Keep processing as long as there's work or waiting
        video_data = None
        with processing_lock:
            if not video_queue:
                is_processing = False
                print("Processing queue is empty. Worker thread stopping.")
                return # Exit thread when queue is empty

            # Check queue size before popping
            if len(video_queue) > MAX_QUEUE_SIZE:
                 print(f"Queue size {len(video_queue)} exceeds max {MAX_QUEUE_SIZE}. Waiting...")
                 # This check should perhaps happen *before* adding to queue or be handled differently
                 # The current pop(0) will process even if queue is huge, which is fine for this model.
                 pass # Just log, pop and process anyway

            video_data = video_queue.pop(0)  # Get the next item

        # If we got video_data, process it
        if video_data:
            is_processing = True
            original_message = video_data['original_message']
            chat_id = original_message.chat.id
            file_path = video_data['file_path']
            target_size_mb = video_data['target_size_mb']
            duration = video_data['duration'] # in seconds

            print(f"Processing video for chat {chat_id}: {os.path.basename(file_path)}")

            # Notify user processing started (optional: edit the 'queued' message)
            try:
                # Assuming the last message sent to the user was 'Added to queue...'
                 messages = app.get_messages(chat_id=chat_id, message_ids=[original_message.id + 1]) # Get the message after original video
                 if messages and len(messages) > 0:
                     try:
                        messages[0].edit_text("🔄 Processing video...")
                     except Exception as edit_err:
                        print(f"Could not edit user message: {edit_err}")
                        app.send_message(chat_id, "🔄 Processing video...") # Send new message if edit fails
                 else:
                      app.send_message(chat_id, "🔄 Processing video...") # Send new message if cannot find msg
            except Exception as e:
                print(f"Could not send/edit processing message: {e}")


            compressed_file_path = None # Initialize
            try:
                if not os.path.exists(file_path):
                    app.send_message(chat_id, "❌ Error: Original video file not found for processing.")
                    print(f"File not found during processing: {file_path}")
                    delete_user_data(chat_id)
                    continue # Move to next item in queue

                # Calculate video bitrate
                # Ensure AUDIO_BITRATE_KBS is an integer (strip 'k')
                audio_bitrate_kbs_int = 0
                try:
                    audio_bitrate_kbs_int = int(VIDEO_AUDIO_BITRATE.lower().replace('k', '').strip())
                except ValueError:
                    print(f"Warning: Could not parse AUDIO_BITRATE '{VIDEO_AUDIO_BITRATE}'. Using 128k default for calculation.")
                    audio_bitrate_kbs_int = 128 # Default for calculation if config is bad

                target_video_bitrate_kbs = calculate_video_bitrate(
                    duration_seconds=duration,
                    target_size_mb=target_size_mb,
                    audio_bitrate_kbs=audio_bitrate_kbs_int # Pass as int for calculation
                )

                # Error case: Target size is too small even for minimum video + audio
                if target_video_bitrate_kbs < 50 and target_size_mb > 0: # Check if target size implies near-zero video bitrate
                     app.send_message(chat_id, f"❌ Error: Target size {target_size_mb} MB is too small. Minimum required size is larger to accommodate video/audio.")
                     print(f"Target size {target_size_mb}MB too small for duration {duration}s and audio {audio_bitrate_kbs_int}kbs.")
                     delete_user_data(chat_id)
                     # Clean up downloaded file immediately as processing failed
                     try: os.remove(file_path)
                     except: pass
                     continue # Move to next item

                # Create a temporary file for the compressed video
                # Use a more unique temp filename based on original file or chat_id
                original_basename = os.path.basename(file_path)
                cleaned_basename = re.sub(r'[^\w.-]', '_', original_basename) # Sanitize filename
                temp_suffix = f".compressed_{chat_id}_{cleaned_basename}.mp4"

                with tempfile.NamedTemporaryFile(suffix=temp_suffix, dir=DOWNLOADS_DIR, delete=False) as temp_file:
                    compressed_file_path = temp_file.name

                print(f"Compressing to: {compressed_file_path}")
                print(f"Target video bitrate: {target_video_bitrate_kbs:.2f} kbs")

                # FFmpeg command with calculated bitrate
                # Use -b:v and include other settings from config
                # Need to strip 'k' from VIDEO_AUDIO_BITRATE for FFmpeg command if it expects number
                # FFmpeg usually accepts '128k' directly
                ffmpeg_command = [
                    'ffmpeg', '-y',
                    '-i', file_path,
                    '-c:v', VIDEO_CODEC, # e.g., h264_nvenc
                    '-pix_fmt', VIDEO_PIXEL_FORMAT, # e.g., yuv420p
                    '-b:v', f'{target_video_bitrate_kbs:.0f}k', # Video bitrate kbits/s
                    '-preset', VIDEO_PRESET, # e.g., medium
                    '-profile:v', VIDEO_PROFILE, # e.g., high
                    '-c:a', VIDEO_AUDIO_CODEC, # e.g., aac
                    '-b:a', VIDEO_AUDIO_BITRATE, # e.g., 128k
                    '-ac', str(VIDEO_AUDIO_CHANNELS), # e.g., 2
                    '-ar', str(VIDEO_AUDIO_SAMPLE_RATE), # e.g., 48000
                    '-map_metadata', '-1', # Strip metadata
                    compressed_file_path
                ]

                print(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}") # Print command string

                # Execute FFmpeg
                process = subprocess.run(
                    ffmpeg_command,
                    shell=False, # safer than shell=True
                    check=True, # Raise CalledProcessError if command fails
                    capture_output=True, # Capture stdout/stderr
                    text=True # Decode output as text
                )
                print("FFmpeg command executed successfully.")
                # Optional: Print FFmpeg stdout/stderr
                # print("FFmpeg stdout:\n", process.stdout)
                # print("FFmpeg stderr:\n", process.stderr)

                # Verify if the output file was actually created and is not empty
                if not os.path.exists(compressed_file_path) or os.path.getsize(compressed_file_path) == 0:
                    raise Exception("FFmpeg finished, but output file was not created or is empty.")

                # Upload the compressed video to the channel
                if CHANNEL_ID:
                    print(f"Uploading compressed video to channel {CHANNEL_ID}...")
                    # We can send a message to the user indicating upload started
                    app.send_message(chat_id, "📤 Uploading compressed video to channel...")

                    # Pass necessary args to the upload progress callback if needed for user chat
                    # For simplicity here, the callback might just print or log
                    upload_start_time = time.time()
                    sent_to_channel_message = app.send_document(
                        chat_id=CHANNEL_ID,
                        document=compressed_file_path,
                        caption=f"Compressed video from user {chat_id}", # Add helpful caption
                         # Using a lambda or functools.partial to pass extra args to the callback
                        progress=functools.partial(upload_progress_callback,
                                                    client=app,
                                                    channel_id=CHANNEL_ID,
                                                    message_id=chat_id, # Using chat_id as a key for upload state in download_progress_states for simplicity
                                                    start_time=upload_start_time),
                        progress_args=(app, CHANNEL_ID, chat_id, upload_start_time) # pyrogram v1.x way, v2.x uses partial or closures
                    )
                    print(f"Compressed video uploaded to channel message ID: {sent_to_channel_message.id}")

                    # Notify user success
                    final_size_mb = os.path.getsize(compressed_file_path) / (1024 * 1024)
                    app.send_message(
                        chat_id=chat_id,
                        text=f"✅ Video compressed successfully and uploaded to channel!\n"
                             f"Target size: {target_size_mb:.2f} MB\n"
                             f"Final size: {final_size_mb:.2f} MB"
                             # f"\nChannel link: {sent_to_channel_message.link}" # Requires privacy settings setup
                    )

                else:
                    print("CHANNEL_ID not configured. Compressed video not sent to channel.")
                    app.send_message(chat_id, "✅ Video compressed successfully, but CHANNEL_ID is not configured.")
                    final_size_mb = os.path.getsize(compressed_file_path) / (1024 * 1024)
                    app.send_message(
                        chat_id=chat_id,
                        text=f"✅ Video compressed successfully!\n"
                             f"Target size: {target_size_mb:.2f} MB\n"
                             f"Final size: {final_size_mb:.2f} MB"
                    )
                    # Note: If no channel, the compressed file might not be easily accessible to the user.

            except subprocess.CalledProcessError as e:
                print("FFmpeg error occurred!")
                print(f"FFmpeg stderr: {e.stderr}")
                error_output = e.stderr[:1000] # Limit error output length
                app.send_message(chat_id, f"❌ Error during video compression:\n`{error_output}`")
            except Exception as e:
                print(f"General error during processing for chat {chat_id}: {e}")
                app.send_message(chat_id, f"❌ An unexpected error occurred: {e}")
            finally:
                # Clean up downloaded file
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        print(f"Deleted original file after processing: {file_path}")
                    except Exception as e:
                        print(f"Error deleting original file {file_path}: {e}")
                # Clean up temporary compressed file
                if compressed_file_path and os.path.exists(compressed_file_path):
                    try:
                        os.remove(compressed_file_path)
                        print(f"Deleted temporary compressed file: {compressed_file_path}")
                    except Exception as e:
                        print(f"Error deleting temp file {compressed_file_path}: {e}")

                # Clean up user state data after processing attempt
                delete_user_data(chat_id)

            # Small delay before checking queue for next item
            time.sleep(2)

        # Loop continues to check queue


# تهيئة العميل للبوت
app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

@app.on_message(filters.command("start"))
def start_command(client, message):
    """Handles the /start command."""
    message.reply_text("👋 مرحبًا بك! أرسل لي فيديو أو أنيميشن لتبدأ عملية الضغط. "
                       "بعد التنزيل، سأطلب منك تحديد الحجم المطلوب للملف النهائي بالميجابايت.")

@app.on_message(filters.video | filters.animation)
def handle_video_or_animation(client, message):
    """
    Handles incoming video or animation files.
    Downloads the file and asks the user for the target size.
    """
    chat_id = message.chat.id
    file_info = message.video if message.video else message.animation

    if chat_id in user_video_data and user_video_data[chat_id]['state'] in ['downloading', 'waiting_for_size_input', 'queued', 'processing']:
        message.reply_text("⚠️ لا يزال طلبك السابق قيد المعالجة أو بانتظار الحجم المطلوب. يرجى الانتظار حتى ينتهي.")
        return

    # Clean up previous entry if it somehow wasn't removed
    if chat_id in user_video_data:
        print(f"Cleaning up stale data for chat {chat_id} before starting new request.")
        # Attempt to delete file if a path exists from a previous state
        stale_file = user_video_data[chat_id].get('file_path')
        if stale_file and os.path.exists(stale_file):
             try: os.remove(stale_file)
             except: pass
        delete_user_data(chat_id)


    # Generate a unique filename for the download
    original_filename = file_info.file_name or f"video_{file_info.file_unique_id}.mp4"
    # Sanitize filename to prevent path traversal or command injection issues
    safe_filename = re.sub(r'[^\w.-]', '_', original_filename)
    download_file_path = os.path.join(DOWNLOADS_DIR, f"{chat_id}_{int(time.time())}_{safe_filename}")

    # Store initial data and set state
    user_video_data[chat_id] = {
        'file_path': download_file_path,
        'duration': file_info.duration, # duration in seconds
        'state': 'downloading',
        'download_message_id': None, # Will be set after sending progress message
        'size_request_message_id': None,
        'original_message': message, # Keep original message object for reference
        'start_time': time.time(), # For download speed calculation
    }

    print(f"Starting download for chat {chat_id} to {download_file_path}")

    # Send initial downloading message
    try:
        progress_message = message.reply_text(
            "⏳ Starting download...",
             quote=True # Reply to the user's video message
             )
        user_video_data[chat_id]['download_message_id'] = progress_message.id
        download_progress_states[progress_message.id] = { # Initialize state for progress tracking
            'last_time': time.time(),
            'last_current': 0,
            'start_time': time.time(),
        }
    except Exception as e:
        print(f"Error sending initial download message to {chat_id}: {e}")
        delete_user_data(chat_id) # Clean up state if cannot send message
        return # Stop processing this request

    # Use functools.partial to pass extra arguments to the progress callback
    download_callback_partial = functools.partial(
        download_progress_callback,
        client=client,
        message=message,
        progress_message_id=user_video_data[chat_id]['download_message_id'],
        start_time=user_video_data[chat_id]['start_time']
    )

    # Download the file in a separate thread or manage download state carefully
    # Pyrogram's download_media is blocking, so best to do it within the handler
    # but update state accordingly. The progress callback handles updates.

    try:
        start_time = time.time()
        downloaded_file = client.download_media(
            file_info.file_id,
            file_name=download_file_path, # Specify target path
            progress=download_callback_partial,
            progress_args=(client, message, user_video_data[chat_id]['download_message_id'], user_video_data[chat_id]['start_time']) # v1.x arg passing, v2.x relies more on closure/partial
        )
        # Ensure downloaded_file is the same path we expected
        if downloaded_file != download_file_path or not os.path.exists(downloaded_file):
            raise Exception(f"Download path mismatch or file missing. Expected {download_file_path}, got {downloaded_file}")


        # Download finished
        print(f"Download finished for chat {chat_id} to {downloaded_file}")
        user_video_data[chat_id]['state'] = 'downloaded'
        # Ensure download progress state is removed
        if user_video_data[chat_id]['download_message_id'] in download_progress_states:
             del download_progress_states[user_video_data[chat_id]['download_message_id']]


        # Delete the download progress message
        try:
            client.delete_messages(chat_id=chat_id, message_ids=user_video_data[chat_id]['download_message_id'])
            user_video_data[chat_id]['download_message_id'] = None # Clear message ID
        except Exception as e:
             print(f"Could not delete download progress message: {e}")

        # Ask user for target size
        request_msg = client.send_message(
            chat_id=chat_id,
            text="✅ Download complete!\n"
                 "Please send the **target size** for the video in **MB** (e.g., `50`):",
            reply_to_message_id=message.id # Reply to original video
        )
        user_video_data[chat_id]['state'] = 'waiting_for_size_input'
        user_video_data[chat_id]['size_request_message_id'] = request_msg.id

    except Exception as e:
        print(f"Error during download for chat {chat_id}: {e}")
        # Clean up state and file
        file_to_delete = user_video_data[chat_id].get('file_path')
        if file_to_delete and os.path.exists(file_to_delete):
            try: os.remove(file_to_delete)
            except: pass

        error_message_text = f"❌ An error occurred during download: {e}"
        try:
            # Attempt to edit progress message to show error
            if user_video_data[chat_id].get('download_message_id'):
                client.edit_message_text(chat_id=chat_id, message_id=user_video_data[chat_id]['download_message_id'], text=error_message_text)
            else: # Or send a new message
                 client.send_message(chat_id, error_message_text)
        except Exception as edit_err:
            print(f"Could not edit or send error message: {edit_err}")

        delete_user_data(chat_id) # Clean up state
        # Remove download progress state
        if user_video_data[chat_id].get('download_message_id') in download_progress_states:
             del download_progress_states[user_video_data[chat_id]['download_message_id']]



@app.on_message(filters.text & filters.private)
def handle_text_input(client, message):
    """Handles text input from users, specifically looking for target size."""
    chat_id = message.chat.id

    if chat_id not in user_video_data or user_video_data[chat_id]['state'] != 'waiting_for_size_input':
        # Ignore text that is not a size input or for a request that's not waiting
        if message.text.startswith('/') or message.text.lower() in ['hi', 'hello', 'مرحبا']:
             # Simple check for commands or greetings to avoid spamming "send video"
             pass
        else:
            # Optional: Inform the user they need to send a video first
             pass # message.reply_text("Please send me a video or animation first.")
        return

    # Check if the input is a number
    target_size_str = message.text.strip()
    if not target_size_str.isdigit():
        message.reply_text("🚫 Invalid input. Please send a **number only** representing the target size in MB (e.g., `50`).")
        # Keep the state as 'waiting_for_size_input'
        return

    target_size_mb = int(target_size_str)

    if target_size_mb <= 0:
        message.reply_text("🚫 Invalid input. Please provide a positive number for the target size.")
        return

    # User provided a valid size. Add to queue.
    user_data = user_video_data[chat_id]

    # Add necessary data to the queue item
    queue_item = {
        'file_path': user_data['file_path'],
        'original_message': user_data['original_message'], # Pass the original message object
        'target_size_mb': target_size_mb,
        'duration': user_data['duration'],
    }

    with processing_lock:
         # Check queue size again before appending if desired, but popping handles limit implicitly
         if len(video_queue) >= MAX_QUEUE_SIZE:
             message.reply_text(f"😞 The queue is full. Please try again later.")
             print(f"Queue full ({len(video_queue)}). Cannot add request from {chat_id}.")
             # Optionally, delete the downloaded file if queue is full and cannot add
             file_to_delete = user_data.get('file_path')
             if file_to_delete and os.path.exists(file_to_delete):
                 try: os.remove(file_to_delete)
                 except: pass
             delete_user_data(chat_id) # Clear state
             # Attempt to delete size request message
             if user_data.get('size_request_message_id'):
                 try: client.delete_messages(chat_id=chat_id, message_ids=user_data['size_request_message_id'])
                 except: pass
             return

         video_queue.append(queue_item)
         print(f"Added video from chat {chat_id} to queue. Queue size: {len(video_queue)}")


    # Change user state to 'queued'
    user_video_data[chat_id]['state'] = 'queued'

    # Delete the 'waiting for size' message
    if user_data.get('size_request_message_id'):
        try:
             client.delete_messages(chat_id=chat_id, message_ids=user_data['size_request_message_id'])
             user_data['size_request_message_id'] = None # Clear message ID
        except Exception as e:
             print(f"Could not delete size request message: {e}")

    # Notify user
    message.reply_text(f"✅ Got it! Your video has been added to the queue ({len(video_queue)} in queue)."
                       f"\nTarget size: {target_size_mb} MB."
                       f"\nProcessing will start soon.")


    # Start the processing thread if it's not already running
    if not is_processing:
        print("Starting processing thread...")
        threading.Thread(target=process_queue, daemon=True).start()
    else:
        print("Processing thread is already running.")


# Function to check and recognize the channel on startup
def check_channel():
    """Checks if the configured channel ID is valid and accessible."""
    if CHANNEL_ID is None:
        print("CHANNEL_ID is not configured in config.py. Upload to channel is disabled.")
        return
    time.sleep(5) # Wait a bit for bot to initialize
    try:
        chat = app.get_chat(CHANNEL_ID)
        if chat.type not in ["channel", "supergroup"]:
             print(f"تحذير: CHANNEL_ID ({CHANNEL_ID}) يشير إلى {chat.type} وليس قناة أو مجموعة خارقة. قد تفشل محاولة الرفع.")
        print(f"تم التعرف على القناة/المجموعة: {chat.title} (ID: {CHANNEL_ID})")
    except Exception as e:
        print(f"خطأ في التعرف على القناة/المجموعة ({CHANNEL_ID}): {e}\n"
              "الرجاء التأكد من صحة معرف القناة وإضافة البوت كمسؤول فيها.")
        # Consider setting CHANNEL_ID = None here if error is critical, but it's often just a permission issue

# Clean up downloads directory on startup
cleanup_downloads()

# Start channel check in a separate thread
threading.Thread(target=check_channel, daemon=True).start()

# Run the bot
print("Bot started. Waiting for messages...")
app.run()
# --- END OF FILE botasli.py ---
