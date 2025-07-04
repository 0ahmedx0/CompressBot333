# imports
import os
import tempfile
import subprocess
import threading
import time
import re
import json
import shutil
import queue # A more suitable Queue class for multi-threading
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import MessageTooLong, FloodWait, BadRequest

# Import configuration from config.py
try:
    from config import *
except ImportError:
    print("خطأ: ملف config.py غير موجود أو لا يحتوي على جميع المتغيرات اللازمة.")
    print("يرجى إنشاء ملف config.py يحتوي على:")
    print("API_ID, API_HASH, API_TOKEN, CHANNEL_ID (اختياري), VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE")
    exit()

# --- Configuration and Global State ---

# Download directory setup
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# Maximum concurrent downloads (controlled by aria2c -x and -s, but useful to cap total requests)
# aria2c itself handles parallel connections per file. This cap isn't strictly necessary for *this* aria2c usage,
# but a Semaphore could be added if you wanted to limit how many aria2c processes run simultaneously.
# For this specific request, we rely on aria2c's internal parallelism (-x/-s).

# Queue for sequential compression tasks
compression_queue = queue.Queue()
is_processing_compression = False
processing_thread = None # To hold the reference to the processing thread

# Dictionary to store per-user/per-message data
# Key: user_chat_id, Value: {'state': 'idle'|'downloading'|'waiting_action'|'waiting_size', 'link': str, 'file_path': str, 'duration': int, 'download_msg_id': int, 'action_msg_id': int, 'original_filename': str}
user_tasks = {}

# --- Helper Functions ---

def cleanup_downloads():
    """Cleans up the downloads directory on bot startup."""
    print(f"Cleaning up downloads directory: {DOWNLOADS_DIR}")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
                print(f"Deleted old file: {file_path}")
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")
    print("Download directory cleanup complete.")

def parse_telegram_link(link):
    """Parses a Telegram link to extract channel username and message ID."""
    match = re.match(r'https://t.me/([a-zA-Z0-9_]+)/(\d+)', link)
    if match:
        return match.groups() # (channel_username, message_id)
    return None

def get_video_metadata(link):
    """Uses yt-dlp to get video metadata (like duration) from a Telegram link."""
    print(f"Getting metadata for: {link}")
    try:
        # Use --force-run-downloader to make yt-dlp work with t.me links
        # This option is specific to getting info, not actual download
        result = subprocess.run(['yt-dlp', '--force-run-downloader', '--dump-json', link], capture_output=True, text=True, check=True, timeout=60)
        metadata = json.loads(result.stdout)
        duration = int(metadata.get('duration', 0)) # duration in seconds
        original_filename = metadata.get('uploader') or metadata.get('title') or 'video'
        return duration, original_filename
    except subprocess.CalledProcessError as e:
        print(f"Error getting metadata for {link}: {e.stderr}")
        return None, None
    except (json.JSONDecodeError, KeyError, ValueError, Exception) as e:
        print(f"Error processing metadata for {link}: {e}")
        return None, None

def get_download_url_with_yt_dlp(link):
    """Uses yt-dlp to extract the direct download URL from a Telegram link."""
    print(f"Getting download URL for: {link}")
    try:
         # Use --force-run-downloader to make yt-dlp work with t.me links
        result = subprocess.run(['yt-dlp', '--force-run-downloader', '--get-url', link], capture_output=True, text=True, check=True, timeout=60)
        url = result.stdout.strip()
        print(f"Extracted URL: {url[:100]}...") # Print only start of URL
        return url
    except subprocess.CalledProcessError as e:
        print(f"Error getting download URL for {link}: {e.stderr}")
        return None
    except Exception as e:
        print(f"Error during URL extraction for {link}: {e}")
        return None

def run_aria2c_and_report_progress(chat_id, link, download_url, download_path, progress_msg_id):
    """Runs aria2c and edits a Telegram message to show progress."""
    print(f"Starting aria2c download for chat {chat_id}...")
    aria2c_cmd = [
        'aria2c',
        download_url,
        '--dir', DOWNLOADS_DIR, # Set download directory
        '--out', os.path.basename(download_path), # Set output filename relative to dir
        '-x', '16', # max-concurrent-downloads per file
        '-s', '16', # max-connection-per-server
        '--auto-file-renaming=false', # Prevent aria2c from renaming if file exists
        '--allow-overwrite=true', # Overwrite if file exists
        '--summary-interval=1', # Report progress every 1 second
        '-c', # continue downloading
        '--no-conf', # don't read aria2.conf
        # '--log-level=info', # Optionally log aria2c's output to a file
        # '--log=/tmp/aria2c.log'
    ]

    try:
        user_tasks[chat_id]['state'] = 'downloading'
        process = subprocess.Popen(aria2c_cmd, cwd=DOWNLOADS_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        last_edit_time = time.time()
        initial_status_sent = False

        # Pattern to extract progress: D: Total / Downloaded (percentage) ETA: Time Speed: Speed
        # Example line: [DL: 10M/50M(20%) CN: 2 ETA: 00h01m Speed: 1.5M]
        # We need to parse stderr/stdout as combined because of STDOUT redirect
        progress_pattern = re.compile(r'\[DL:\s+(\d+\.?\d*[KMGT]?i?B)/(\d+\.?\d*[KMGT]?i?B)\((\d+\.?\d*%)\)\s+CN:\s*\d+\s+ETA:\s*(\S+)\s+Speed:\s*(\S+)]')

        while True:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None: # Check if process has terminated
                    break
                time.sleep(0.1) # Don't busy-wait
                continue

            line = line.strip()
            # print(f"aria2c out: {line}") # Debugging aria2c output

            if "download completed" in line.lower():
                 # Success indicator
                 break # Exit the reading loop, process will terminate shortly

            match = progress_pattern.search(line)
            if match:
                downloaded, total, percentage, eta, speed = match.groups()
                status_text = f"📥 **Downloading:**\n`{os.path.basename(download_path)}`\n\n**Progress:** `{downloaded} / {total}`\n**Percentage:** `{percentage}`\n**Speed:** `{speed}`\n**ETA:** `{eta}`"

                # Edit the message only every few seconds to avoid flood waits
                current_time = time.time()
                if current_time - last_edit_time >= 2 or not initial_status_sent: # Edit at least every 2 seconds
                    try:
                        app.edit_message_text(chat_id, progress_msg_id, status_text)
                        last_edit_time = current_time
                        initial_status_sent = True
                    except FloodWait as e:
                        print(f"FloodWait editing message: {e.value} seconds")
                        time.sleep(e.value) # Wait before retrying edit
                        last_edit_time = current_time # Update time to avoid immediate re-edit
                    except BadRequest as e:
                         print(f"Bad Request editing message {progress_msg_id}: {e}")
                         # This can happen if message was deleted manually.
                         # We might want to stop the download if message is gone, but tricky to handle here.
                         # Let's continue for now and handle download failure if message is gone.
                    except Exception as e:
                         print(f"Error editing message {progress_msg_id}: {e}")

        # Wait for the process to actually finish after loop breaks
        process.wait()

        if process.returncode == 0:
            print(f"aria2c download completed successfully for chat {chat_id}")
            # Update the message one last time to confirm completion
            try:
                 app.edit_message_text(chat_id, progress_msg_id, f"✅ Download complete:\n`{os.path.basename(download_path)}`")
            except Exception as e:
                 print(f"Error editing final download complete message: {e}")

            # Move the file from DOWNLOADS_DIR to the expected temporary path if aria2c saved it under a different name/structure
            # By setting --out and --dir, aria2c should ideally save it directly.
            # Check if the file exists at the target path
            if not os.path.exists(download_path):
                 print(f"Warning: Expected file {download_path} not found after aria2c. Checking DOWNLOADS_DIR...")
                 # aria2c might save it with a slightly different name or in a subdir if options were tricky.
                 # Simple check: find the first file in the dir matching expected prefix
                 downloaded_files = [f for f in os.listdir(DOWNLOADS_DIR) if f.startswith(os.path.basename(download_path).split('.')[0]) and f != os.path.basename(download_path)]
                 if downloaded_files:
                      actual_downloaded_file = os.path.join(DOWNLOADS_DIR, downloaded_files[0])
                      print(f"Found potentially matching file: {actual_downloaded_file}. Renaming/Moving...")
                      try:
                           # Move the file to the *exact* location expected by user_tasks[chat_id]['file_path']
                           # Ensure target directory exists first (the tempfile one)
                           target_dir = os.path.dirname(download_path)
                           os.makedirs(target_dir, exist_ok=True)
                           shutil.move(actual_downloaded_file, download_path)
                           print(f"Moved '{actual_downloaded_file}' to '{download_path}'")
                      except Exception as e:
                           print(f"Error moving downloaded file: {e}")
                           # File not at expected location, mark as failed
                           if chat_id in user_tasks:
                               user_tasks[chat_id]['state'] = 'failed'
                               handle_error_cleanup(chat_id, f"حدث خطأ في تحديد مسار الملف بعد التنزيل.")
                           return # Stop processing this task
                 else:
                      print(f"Error: Downloaded file not found at expected path {download_path} and no similar files found in {DOWNLOADS_DIR}")
                      if chat_id in user_tasks:
                           user_tasks[chat_id]['state'] = 'failed'
                           handle_error_cleanup(chat_id, f"حدث خطأ في تحديد مسار الملف بعد التنزيل.")
                      return # Stop processing this task

            # Download successful, present action options
            if chat_id in user_tasks:
                 user_tasks[chat_id]['state'] = 'waiting_action'
                 action_markup = InlineKeyboardMarkup([
                     [InlineKeyboardButton("ضغط الفيديو", callback_data=f"compress_{chat_id}"),
                      InlineKeyboardButton("رفع بدون ضغط", callback_data=f"upload_raw_{chat_id}")]
                 ])
                 try:
                     action_msg = app.send_message(chat_id, "📥 تم التنزيل. ماذا تود أن تفعل؟", reply_markup=action_markup)
                     user_tasks[chat_id]['action_msg_id'] = action_msg.id
                     # Delete the progress message if successful
                     try:
                         app.delete_messages(chat_id, progress_msg_id)
                     except Exception as e:
                         print(f"Error deleting progress message {progress_msg_id}: {e}")

                 except Exception as e:
                      print(f"Error sending action message: {e}")
                      # Clean up on send message error
                      handle_error_cleanup(chat_id, "حدث خطأ في إرسال خيارات الإجراء.")
        else:
            print(f"aria2c download failed for chat {chat_id} with return code {process.returncode}")
            stderr_output = process.communicate()[1].decode().strip() # Capture remaining stderr if any
            print(f"aria2c stderr/stdout: {stderr_output}")
            # Handle download failure
            if chat_id in user_tasks:
                 user_tasks[chat_id]['state'] = 'failed'
                 error_msg = f"❌ فشل التنزيل:\n`aria2c exited with code {process.returncode}`\nDetails:\n`{stderr_output[-500:]}`" # Last 500 chars of output
                 handle_error_cleanup(chat_id, error_msg, True) # Clean file if partially downloaded

    except FileNotFoundError:
        print("Error: aria2c not found. Please ensure it's installed and in your system's PATH.")
        if chat_id in user_tasks:
             user_tasks[chat_id]['state'] = 'failed'
             handle_error_cleanup(chat_id, "❌ فشل التنزيل: أداة `aria2c` غير موجودة على الخادم.", True)
    except Exception as e:
        print(f"An error occurred during aria2c execution for chat {chat_id}: {e}")
        if chat_id in user_tasks:
             user_tasks[chat_id]['state'] = 'failed'
             handle_error_cleanup(chat_id, f"❌ فشل التنزيل بسبب خطأ غير متوقع: {e}", True)

def calculate_bitrate(target_mb, duration_seconds):
    """Calculates the required video bitrate in kb/s for a target size."""
    if duration_seconds <= 0:
        return 1000 # Default if duration is zero or negative

    # Target size in bits = target_mb * 1024 * 1024 * 8
    # Required bitrate (bits/second) = Target size in bits / duration in seconds
    # Convert to kb/s = (Target size in bits / duration in seconds) / 1000
    bitrate_bps = (target_mb * 1024 * 1024 * 8) / duration_seconds
    bitrate_kbps = bitrate_bps / 1000

    # FFmpeg expects bitrate usually as integers, rounded up
    return max(500, int(bitrate_kbps)) # Ensure a minimum bitrate like 500 kbps

def generate_ffmpeg_command(input_path, output_path, bitrate_kbps):
    """Generates the ffmpeg command for compression with NVENC."""
    # Basic command structure, add audio settings from config
    ffmpeg_command = [
        'ffmpeg', '-y', # Overwrite output without asking
        '-hwaccel', 'cuda', # Enable CUDA hardware acceleration
        '-i', input_path,
        '-c:v', 'h264_nvenc', # H.264 encoding with NVENC
        '-b:v', f'{bitrate_kbps}k', # Video bitrate in kb/s
        '-preset', 'medium', # NVENC preset: slowest, slow, medium, fast, high, hp, lossless, ll, llhp, llhq
        '-profile:v', 'high', # H.264 profile
        '-map_metadata', '-1', # Remove metadata from input

        # Audio settings from config
        '-c:a', VIDEO_AUDIO_CODEC,
        '-b:a', VIDEO_AUDIO_BITRATE,
        '-ac', str(VIDEO_AUDIO_CHANNELS),
        '-ar', str(VIDEO_AUDIO_SAMPLE_RATE),

        output_path
    ]
    return ffmpeg_command

def process_compression_queue():
    """Thread worker function to process compression tasks sequentially."""
    global is_processing_compression
    is_processing_compression = True
    print("Compression processing thread started.")

    while True:
        try:
            task = compression_queue.get(timeout=1) # Get task, wait max 1 second
        except queue.Empty:
            # Queue is empty, exit the loop if no more tasks for a while (or just keep running)
            # Let's keep the thread alive and waiting
            time.sleep(5) # Wait before checking again
            continue

        chat_id = task['chat_id']
        input_file = task['input_file']
        duration = task['duration']
        target_size_mb = task['target_size_mb']
        original_message = task['original_message'] # Reference to the original message object
        compression_status_msg_id = task['status_msg_id'] # ID of the message to update status

        print(f"Processing compression task for chat {chat_id}. Target size: {target_size_mb}MB")

        compressed_file_path = None
        try:
            # Calculate bitrate
            bitrate_kbps = calculate_bitrate(target_size_mb, duration)
            print(f"Calculated bitrate: {bitrate_kbps} kb/s")

            # Create temporary output file path
            with tempfile.NamedTemporaryFile(suffix=f'_compressed.mp4', delete=False, dir=DOWNLOADS_DIR) as temp_file:
                compressed_file_path = temp_file.name

            # Generate and execute FFmpeg command
            ffmpeg_cmd = generate_ffmpeg_command(input_file, compressed_file_path, bitrate_kbps)
            print(f"Executing FFmpeg command: {' '.join(ffmpeg_cmd)}")

            # Update status message
            try:
                app.edit_message_text(chat_id, compression_status_msg_id, f"⏳ جاري الضغط ({target_size_mb}MB) ...")
            except Exception as e:
                print(f"Error editing compression status message {compression_status_msg_id}: {e}")

            # Run FFmpeg subprocess
            ffmpeg_process = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True, timeout=duration * 5) # Basic timeout

            print("FFmpeg command executed successfully.")
            # print(f"FFmpeg stdout:\n{ffmpeg_process.stdout}")
            # print(f"FFmpeg stderr:\n{ffmpeg_process.stderr}")

            # Check output file size (optional sanity check)
            compressed_size_mb = os.path.getsize(compressed_file_path) / (1024 * 1024)
            print(f"Compressed file size: {compressed_size_mb:.2f} MB")
            if compressed_size_mb > target_size_mb * 1.2: # Allow some tolerance (e.g., 20%)
                 print(f"Warning: Compressed file size ({compressed_size_mb:.2f}MB) is significantly larger than target ({target_size_mb}MB).")
                 # Decide if this should be treated as a failure. For now, proceed but log.

            # Upload compressed video to channel
            if CHANNEL_ID:
                try:
                    upload_status_msg = app.send_message(chat_id, "⬆️ جاري الرفع إلى القناة...", reply_to_message_id=original_message.id) # Notify user
                    # Upload to channel
                    app.send_document(
                        chat_id=CHANNEL_ID,
                        document=compressed_file_path,
                        caption=f"Compressed to ~{target_size_mb}MB | {os.path.basename(input_file)}" # Optional caption
                        # No progress bar here for simplicity, could add one by subclassing and reading file handle
                    )
                    try:
                         upload_status_msg.edit_text("✅ تم الرفع إلى القناة بنجاح!")
                    except Exception as e:
                         print(f"Error editing final upload message: {e}")

                    print(f"Compressed video uploaded to channel: {CHANNEL_ID}")
                    app.edit_message_text(chat_id, compression_status_msg_id, "✅ تم الضغط والرفع إلى القناة بنجاح.")

                except Exception as e:
                    print(f"Error uploading compressed video to channel {CHANNEL_ID}: {e}")
                    app.edit_message_text(chat_id, compression_status_msg_id, "❌ تم الضغط، ولكن فشل الرفع إلى القناة.")
            else:
                print("CHANNEL_ID not configured. Compressed video not sent to channel.")
                app.edit_message_text(chat_id, compression_status_msg_id, "✅ تم الضغط بنجاح، ولكن لم يتم تهيئة قناة للرفع.")

        except subprocess.CalledProcessError as e:
            print("FFmpeg error occurred!")
            print(f"FFmpeg stdout: {e.stdout}")
            print(f"FFmpeg stderr: {e.stderr}")
            error_text = f"❌ حدث خطأ أثناء ضغط الفيديو:\n`FFmpeg exited with code {e.returncode}`\nDetails:\n`{e.stderr[-500:]}`" # Last 500 chars
            try:
                app.edit_message_text(chat_id, compression_status_msg_id, error_text)
            except Exception as edit_e:
                print(f"Error editing error message {compression_status_msg_id}: {edit_e}")
                try:
                    app.send_message(chat_id, error_text, reply_to_message_id=original_message.id)
                except Exception as send_e:
                    print(f"Error sending error message: {send_e}")

        except FileNotFoundError:
            print("Error: ffmpeg not found. Please ensure it's installed and in your system's PATH with NVENC support.")
            error_text = "❌ فشل الضغط: أداة `ffmpeg` غير موجودة على الخادم أو لا تدعم تسريع NVENC."
            try:
                 app.edit_message_text(chat_id, compression_status_msg_id, error_text)
            except Exception as edit_e:
                 print(f"Error editing error message: {edit_e}")

        except Exception as e:
            print(f"General error during compression or upload: {e}")
            error_text = f"❌ حدث خطأ غير متوقع أثناء الضغط: {e}"
            try:
                 app.edit_message_text(chat_id, compression_status_msg_id, error_text)
            except Exception as edit_e:
                 print(f"Error editing error message: {edit_e}")

        finally:
            # Clean up temp files related to this task
            if input_file and os.path.exists(input_file):
                try:
                    os.remove(input_file)
                    print(f"Deleted original temp file: {input_file}")
                except Exception as e:
                    print(f"Error deleting original temp file {input_file}: {e}")
            if compressed_file_path and os.path.exists(compressed_file_path):
                try:
                    os.remove(compressed_file_path)
                    print(f"Deleted compressed temp file: {compressed_file_path}")
                except Exception as e:
                    print(f"Error deleting compressed temp file {compressed_file_path}: {e}")

            # Mark user as idle after task completion (success or failure)
            if chat_id in user_tasks and 'state' in user_tasks[chat_id] and user_tasks[chat_id]['state'] != 'cancelled':
                user_tasks[chat_id]['state'] = 'idle'
                # Clean up remaining task data from user_tasks if it was the last step
                # Need to be careful if other states might still use it.
                # Let's keep it until the user starts a new task or timer expires if we add one.
                # For now, mark idle. Data cleanup happens on cancel or start of new task.


        compression_queue.task_done() # Indicate task is done for the queue

    # is_processing_compression remains True, the thread is designed to keep running and waiting for tasks

def handle_error_cleanup(chat_id, error_message, delete_file=False):
     """Handles cleanup and notifies user in case of an error during download or initial steps."""
     print(f"Handling error for chat {chat_id}: {error_message}")
     task_data = user_tasks.pop(chat_id, None)
     if task_data:
          file_path = task_data.get('file_path')
          progress_msg_id = task_data.get('download_msg_id')
          action_msg_id = task_data.get('action_msg_id')

          # Delete progress message if it exists
          if progress_msg_id:
               try:
                    app.delete_messages(chat_id, progress_msg_id)
               except Exception as e:
                    print(f"Error deleting progress message {progress_msg_id} on error: {e}")
          # Delete action message if it exists
          if action_msg_id:
              try:
                  app.delete_messages(chat_id, action_msg_id)
              except Exception as e:
                  print(f"Error deleting action message {action_msg_id} on error: {e}")

          # Delete temp file if exists and required
          if delete_file and file_path and os.path.exists(file_path):
               try:
                    os.remove(file_path)
                    print(f"Deleted temp file {file_path} during error cleanup.")
               except Exception as e:
                    print(f"Error deleting temp file {file_path} on error: {e}")

          # Notify user of the error
          try:
               app.send_message(chat_id, error_message)
          except Exception as e:
               print(f"Error sending error message to user {chat_id}: {e}")

def cancel_task(chat_id, user_cancelled=True):
    """Cancels the current task for a user, cleans up resources."""
    print(f"Cancelling task for chat {chat_id}, user_cancelled: {user_cancelled}")
    task_data = user_tasks.pop(chat_id, None)
    if task_data:
        file_path = task_data.get('file_path')
        download_msg_id = task_data.get('download_msg_id')
        action_msg_id = task_data.get('action_msg_id')

        # Note: We cannot easily stop aria2c or ffmpeg process if it's already running.
        # We just mark the state as cancelled, delete temp files, and notify the user.
        # The external process might continue running for a bit before finishing.

        # Delete relevant messages
        if download_msg_id:
            try:
                app.delete_messages(chat_id, download_msg_id)
            except Exception as e:
                print(f"Error deleting download message {download_msg_id} on cancel: {e}")
        if action_msg_id:
            try:
                 app.delete_messages(chat_id, action_msg_id)
            except Exception as e:
                 print(f"Error deleting action message {action_msg_id} on cancel: {e}")


        # Clean up temp file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted temp file {file_path} on cancel.")
            except Exception as e:
                print(f"Error deleting temp file {file_path} on cancel: {e}")

        # Notify user
        if user_cancelled:
             try:
                  app.send_message(chat_id, "✅ تم إلغاء العملية.")
             except Exception as e:
                  print(f"Error sending cancel message to user {chat_id}: {e}")

        # If the task was in the compression queue, it will process and see the file is gone, handling it there.
        # Or we could try to remove it from the queue, but that's harder with a queue.Queue.

# --- Pyrogram Handlers ---

# Initialize the Bot Client
app = Client("video_compressor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=API_TOKEN)

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    """Handles the /start command."""
    await message.reply_text("أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`")

@app.on_message(filters.text & filters.private)
async def handle_message(client: Client, message: Message):
    """Handles incoming text messages."""
    chat_id = message.chat.id
    text = message.text.strip()

    # Check if user is expected to send the target size
    if chat_id in user_tasks and user_tasks[chat_id]['state'] == 'waiting_size':
        try:
            target_size_mb = int(text)
            if target_size_mb <= 0:
                 await message.reply_text("حجم الفيديو يجب أن يكون رقماً موجباً. يرجى إرسال الرقم مرة أخرى.", quote=True)
                 return

            task_data = user_tasks[chat_id]
            input_file = task_data['file_path']
            duration = task_data['duration']
            original_message = task_data['original_message']

            # Reset state immediately
            user_tasks[chat_id]['state'] = 'idle'

            # Queue the compression task
            # Create a temporary status message to be updated during compression
            status_msg = await message.reply_text("⌛️ جارٍ إضافة المهمة إلى قائمة الانتظار...")
            compression_task = {
                'chat_id': chat_id,
                'input_file': input_file,
                'duration': duration,
                'target_size_mb': target_size_mb,
                'original_message': original_message,
                'status_msg_id': status_msg.id
            }
            compression_queue.put(compression_task)
            await status_msg.edit_text("✅ تم إضافة مهمة الضغط إلى قائمة الانتظار. سيتم معالجتها قريباً.")

            # Start the compression thread if it's not running
            global processing_thread
            if not is_processing_compression or (processing_thread and not processing_thread.is_alive()):
                processing_thread = threading.Thread(target=process_compression_queue, daemon=True)
                processing_thread.start()

        except ValueError:
            await message.reply_text("هذا ليس رقماً صحيحاً. يرجى إرسال حجم الفيديو المطلوب بالميجابايت كـ **رقم فقط** (مثال: `50`).", quote=True)
        except Exception as e:
            print(f"Error processing target size input for chat {chat_id}: {e}")
            await message.reply_text("حدث خطأ أثناء معالجة طلبك.", quote=True)
            handle_error_cleanup(chat_id, f"حدث خطأ: {e}", delete_file=False) # Keep file for now, user might retry


    # Check if the message is a Telegram link
    elif text.startswith("https://t.me/"):
        if chat_id in user_tasks and user_tasks[chat_id]['state'] != 'idle':
            await message.reply_text("أنت قيد المعالجة حالياً. يرجى الانتظار أو استخدام أمر الإلغاء إذا كان متاحاً.", quote=True)
            return

        parse_result = parse_telegram_link(text)
        if not parse_result:
            await message.reply_text("صيغة الرابط غير صحيحة. يرجى إرسال رابط بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`", quote=True)
            return

        channel_username, message_id = parse_result
        print(f"Received Telegram link: Channel={channel_username}, Message ID={message_id}")

        # Start processing the link
        try:
            # Send an initial processing message
            process_msg = await message.reply_text("🔍 جارٍ استخلاص معلومات الفيديو...", quote=True)
            user_tasks[chat_id] = {
                'state': 'fetching_info',
                'link': text,
                'download_msg_id': process_msg.id,
                'action_msg_id': None,
                'file_path': None, # Will be set after getting URL
                'duration': None,  # Will be set after getting info
                'original_filename': None, # Will be set after getting info
                'original_message': message # Store original message object
            }

            # --- Step 2a: Get Metadata (Duration, Original Filename) ---
            duration, original_filename = get_video_metadata(text)
            if duration is None:
                 handle_error_cleanup(chat_id, "❌ فشل استخلاص معلومات الفيديو. قد يكون الرابط غير صحيح أو القناة خاصة.")
                 return

            user_tasks[chat_id]['duration'] = duration
            user_tasks[chat_id]['original_filename'] = original_filename

            # --- Step 2b: Get Direct Download URL ---
            download_url = get_download_url_with_yt_dlp(text)
            if not download_url:
                handle_error_cleanup(chat_id, "❌ فشل الحصول على رابط التنزيل المباشر للفيديو.")
                return

            # --- Step 2c: Download using aria2c ---
            # Generate a unique temporary file path
            temp_output_file = os.path.join(DOWNLOADS_DIR, f"{chat_id}_{message.id}_temp_download_{os.path.basename(download_url).split('?')[0]}")
            # Ensure unique name if a simple name like "video" is returned by yt-dlp
            if os.path.basename(temp_output_file) in ["video", "file", "stream"]: # Avoid generic names that clash easily
                temp_output_file += f"_{int(time.time())}"

            user_tasks[chat_id]['file_path'] = temp_output_file # Store the expected final path

            await process_msg.edit_text("✅ تم استخلاص الرابط. جارٍ بدء التنزيل...")

            # Run aria2c in a separate thread to not block the main handler
            download_thread = threading.Thread(
                target=run_aria2c_and_report_progress,
                args=(chat_id, text, download_url, user_tasks[chat_id]['file_path'], process_msg.id),
                daemon=True # Thread exits if main program exits
            )
            download_thread.start()

        except Exception as e:
            print(f"Error handling link {text} for chat {chat_id}: {e}")
            handle_error_cleanup(chat_id, f"حدث خطأ غير متوقع: {e}", delete_file=False) # File likely not created yet

    # If text is not a link and not a number when waiting for size
    elif chat_id in user_tasks and user_tasks[chat_id]['state'] == 'waiting_size':
         # User sent text, but not a number. The 'waiting_size' block handled this already.
         pass # Do nothing here
    else:
        # Any other random text
        await message.reply_text("أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`\n\nأو أرسل لي فيديو مباشراً.", quote=True)


@app.on_callback_query()
async def handle_callback(client: Client, callback_query):
    """Handles inline keyboard button presses."""
    data = callback_query.data
    chat_id = callback_query.message.chat.id
    message_id = callback_query.message.id # ID of the action message (not download progress)

    # Ensure the callback is for a known, active task and message
    if chat_id not in user_tasks or user_tasks[chat_id]['action_msg_id'] != message_id:
        print(f"Callback received for unknown/stale task: {data} from chat {chat_id}, message {message_id}")
        await callback_query.answer("انتهت صلاحية هذا الطلب أو تم معالجته مسبقاً.", show_alert=True)
        try:
             # Try to delete the outdated action message
             await callback_query.message.delete()
        except Exception:
             pass # Ignore errors if message is already gone
        return

    await callback_query.answer() # Answer the callback immediately

    task_data = user_tasks[chat_id]
    file_path = task_data['file_path']
    duration = task_data['duration']
    original_filename = task_data['original_filename']
    original_message = task_data['original_message'] # Original user message (for reply_to)

    # Delete the action message after processing the choice
    try:
        await callback_query.message.delete()
    except Exception as e:
        print(f"Error deleting action message {message_id}: {e}")

    if data.startswith("upload_raw_"):
        print(f"Uploading raw video for chat {chat_id}")
        user_tasks[chat_id]['state'] = 'uploading_raw'

        if not os.path.exists(file_path):
            handle_error_cleanup(chat_id, "❌ خطأ: لم يتم العثور على الملف الأصلي للرفع.", delete_file=False) # File might already be gone or download failed earlier
            return

        try:
            # Send status message
            upload_status_msg = await client.send_message(chat_id, "⬆️ جارٍ رفع الفيديو الأصلي...", reply_to_message_id=original_message.id)

            await client.send_document(
                chat_id=chat_id,
                document=file_path,
                file_name=original_filename + os.path.splitext(file_path)[1], # Use original filename with correct extension
                # caption="الفيديو الأصلي", # Optional caption
                 # No progress bar for simplicity here
            )
            await upload_status_msg.edit_text("✅ تم رفع الفيديو الأصلي بنجاح!")
            print(f"Raw video uploaded successfully for chat {chat_id}")

        except Exception as e:
            print(f"Error uploading raw video for chat {chat_id}: {e}")
            await client.send_message(chat_id, f"❌ فشل رفع الفيديو الأصلي: {e}")

        finally:
            # Clean up temp file after upload (success or failure)
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    print(f"Deleted raw temp file after upload: {file_path}")
                except Exception as e:
                    print(f"Error deleting raw temp file {file_path}: {e}")

            # Mark task as complete and remove from user_tasks
            user_tasks.pop(chat_id, None) # Remove the task data completely


    elif data.startswith("compress_"):
        print(f"Compressing video requested for chat {chat_id}")
        user_tasks[chat_id]['state'] = 'waiting_size'

        # Ask the user to send the target size
        await client.send_message(
            chat_id,
            "كم ميجابايت تود أن يكون حجم الفيديو المضغوط؟ أرسل **الرقم فقط** (مثال: `50`)",
            reply_to_message_id=original_message.id # Reply to the original user message
        )

    # We don't need a separate cancel button here, as it wasn't specified in the requirements
    # If we had one, it would call cancel_task(chat_id)

# --- Main Execution ---

if __name__ == "__main__":
    print("Bot starting...")
    cleanup_downloads() # Clean up temp files on startup
    print("Starting Pyrogram client...")

    # Start the compression processing thread on startup, make it daemon
    # It will wait for tasks in the queue
    processing_thread = threading.Thread(target=process_compression_queue, daemon=True)
    processing_thread.start()
    is_processing_compression = True # Flag to indicate the thread is running

    app.run()
    print("Bot stopped.")
