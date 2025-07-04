import os
import tempfile
import subprocess
import threading
import time
import re
import json
import shutil
import queue
import asyncio # Import asyncio

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

# Queue for sequential compression tasks
compression_queue = queue.Queue()
# No need for is_processing_compression flag if thread runs continuously
processing_thread = None # To hold the reference to the processing thread

# Dictionary to store per-user/per-message data
# Key: user_chat_id, Value: {'state': 'idle'|'downloading'|'waiting_action'|'waiting_size'|'uploading_raw'|'compressing', 'link': str, 'file_path': str, 'duration': int, 'download_msg_id': int, 'action_msg_id': int, 'status_msg_id': int, 'original_filename': str, 'original_message_id': int, 'original_message_chat_id': int}
# Added status_msg_id, original_message_id, original_message_chat_id for use in threads
user_tasks = {}

# --- Helper Functions for Thread-Safe Pyrogram Calls ---
# These helper coroutines are defined to be run from background threads
# using asyncio.run_coroutine_threadsafe or app.loop.call_soon_threadsafe

async def _edit_message(chat_id, message_id, text, reply_markup=None):
    """Helper coroutine to edit a message."""
    try:
        await app.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
    except FloodWait as e:
        print(f"FloodWait editing message {message_id}: {e.value} seconds")
        await asyncio.sleep(e.value)
        await _edit_message(chat_id, message_id, text, reply_markup) # Retry after wait
    except BadRequest as e:
         print(f"Bad Request editing message {message_id}: {e}")
         # Message might have been deleted, absorb error
    except Exception as e:
        print(f"Error editing message {message_id}: {e}")

async def _send_message(chat_id, text, reply_to_message_id=None):
    """Helper coroutine to send a message."""
    try:
        return await app.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
    except FloodWait as e:
        print(f"FloodWait sending message: {e.value} seconds")
        await asyncio.sleep(e.value)
        return await _send_message(chat_id, text, reply_to_message_id) # Retry after wait
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")
        return None # Indicate failure

async def _delete_messages(chat_id, message_ids):
     """Helper coroutine to delete messages."""
     try:
          await app.delete_messages(chat_id, message_ids)
     except Exception as e:
          print(f"Error deleting messages {message_ids}: {e}")

async def _send_document(chat_id, document, caption=None, file_name=None, reply_to_message_id=None):
     """Helper coroutine to send a document."""
     try:
          await app.send_document(
              chat_id=chat_id,
              document=document,
              caption=caption,
              file_name=file_name,
              reply_to_message_id=reply_to_message_id,
               # Add progress=... here if you need upload progress
          )
     except FloodWait as e:
          print(f"FloodWait sending document: {e.value} seconds")
          await asyncio.sleep(e.value)
          await _send_document(chat_id, document, caption=caption, file_name=file_name, reply_to_message_id=reply_to_message_id) # Retry
     except Exception as e:
          print(f"Error sending document to {chat_id}: {e}")
          # Maybe send a text message instead if document fails?


def schedule_async_task(coro):
    """Schedules an async coroutine to run on the main event loop from a sync thread."""
    # Ensure the app.loop is available and running
    if app.loop and app.loop.is_running():
         try:
              asyncio.run_coroutine_threadsafe(coro, app.loop)
         except Exception as e:
              print(f"Error scheduling async task: {e}")
    else:
         print("Error: Event loop not running. Cannot schedule async task.")


# --- Helper Functions (Synchronous unless marked async) ---

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
        return match.groups() # (channel_username, message_id), None for error
    return None

def get_video_metadata(link):
    """Uses yt-dlp to get video metadata (like duration) from a Telegram link."""
    print(f"Getting metadata for: {link}")
    try:
        # Use --force-run-downloader to make yt-dlp work with t.me links
        result = subprocess.run(
            ['yt-dlp', '--force-run-downloader', '--dump-json', link],
            capture_output=True, text=True, check=True, timeout=60
        )
        metadata = json.loads(result.stdout)
        duration = int(metadata.get('duration', 0)) # duration in seconds
        # Get a safe filename - yt-dlp's uploader or title can be long/complex
        original_filename = metadata.get('title') or metadata.get('id') or 'video'
        # Sanitize filename for OS compatibility
        original_filename = re.sub(r'[\\/:*?"<>|]', '_', original_filename)
        return duration, original_filename, None # Return duration, filename, None (for no error)
    except FileNotFoundError:
        return None, None, "[Errno 2] yt-dlp command not found. Please ensure yt-dlp is installed and in PATH."
    except subprocess.CalledProcessError as e:
        error_msg = f"yt-dlp metadata error: {e.stderr.strip()}"
        print(error_msg)
        return None, None, error_msg
    except (json.JSONDecodeError, KeyError, ValueError, Exception) as e:
        error_msg = f"Error processing yt-dlp metadata: {e}"
        print(error_msg)
        return None, None, error_msg

def get_download_url_with_yt_dlp(link):
    """Uses yt-dlp to extract the direct download URL from a Telegram link."""
    print(f"Getting download URL for: {link}")
    try:
         # Use --force-run-downloader to make yt-dlp work with t.me links
        result = subprocess.run(
            ['yt-dlp', '--force-run-downloader', '--get-url', link],
            capture_output=True, text=True, check=True, timeout=60
        )
        url = result.stdout.strip()
        if not url: # yt-dlp might return empty string if no URL found
             return None, "yt-dlp returned empty URL."
        print(f"Extracted URL: {url[:100]}...") # Print only start of URL
        return url, None # Return URL, None (for no error)
    except FileNotFoundError:
         return None, "[Errno 2] yt-dlp command not found. Please ensure yt-dlp is installed and in PATH."
    except subprocess.CalledProcessError as e:
        error_msg = f"yt-dlp get-url error: {e.stderr.strip()}"
        print(error_msg)
        return None, error_msg
    except Exception as e:
        error_msg = f"Error during URL extraction: {e}"
        print(error_msg)
        return None, error_msg

def run_aria2c_and_report_progress(chat_id):
    """Runs aria2c and edits a Telegram message to show progress.
       This runs in a separate thread."""

    # Get task data safely inside the thread
    task_data = user_tasks.get(chat_id)
    if not task_data or task_data['state'] != 'downloading':
        print(f"Task data not found or state not downloading for chat {chat_id}")
        return

    link = task_data['link']
    download_url = task_data['download_url'] # Get from task_data
    download_path = task_data['file_path']
    progress_msg_id = task_data['download_msg_id']
    original_filename = task_data['original_filename']


    print(f"Starting aria2c download for chat {chat_id}...")
    aria2c_cmd = [
        'aria2c',
        download_url,
        '--dir', os.path.dirname(download_path), # Set download directory to where tempfile points
        '--out', os.path.basename(download_path), # Set output filename
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

    # Ensure the target directory for the temp file exists
    os.makedirs(os.path.dirname(download_path), exist_ok=True)

    try:
        process = subprocess.Popen(aria2c_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        last_edit_time = time.time()
        initial_status_sent = False

        # Pattern to extract progress: D: Total / Downloaded (percentage) ETA: Time Speed: Speed
        # Example line: [DL: 10M/50M(20%) CN: 2 ETA: 00h01m Speed: 1.5M]
        progress_pattern = re.compile(r'\[DL:\s+(\d+\.?\d*[KMGT]?i?B)/(\d+\.?\d*[KMGT]?i?B)\((\d+\.?\d*%)\)\s+CN:\s*\d+\s+ETA:\s*(\S+)\s+Speed:\s*(\S+)]')

        while True:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            line = line.strip()
            # print(f"aria2c out: {line}") # Debugging aria2c output

            if "download completed" in line.lower():
                 # Success indicator line
                 break # Exit the reading loop, process will terminate shortly

            match = progress_pattern.search(line)
            if match:
                downloaded, total, percentage, eta, speed = match.groups()
                status_text = f"📥 **Downloading:**\n`{original_filename}`\n\n**Progress:** `{downloaded} / {total}`\n**Percentage:** `{percentage}`\n**Speed:** `{speed}`\n**ETA:** `{eta}`"

                # Edit the message only every few seconds to avoid flood waits
                current_time = time.time()
                if current_time - last_edit_time >= 2 or not initial_status_sent: # Edit at least every 2 seconds
                    schedule_async_task(_edit_message(chat_id, progress_msg_id, status_text))
                    last_edit_time = current_time
                    initial_status_sent = True


        # Wait for the process to actually finish after loop breaks or on error
        # Communicate to get any remaining output in stderr/stdout
        stdout, stderr = process.communicate() # Collect remaining output
        full_output = (line + "\n" + stdout.strip()).strip()
        if stderr: print(f"aria2c stderr: {stderr.strip()}")


        if process.returncode == 0 and os.path.exists(download_path):
            print(f"aria2c download completed successfully for chat {chat_id}. File exists at {download_path}")
            # Update the message one last time to confirm completion
            schedule_async_task(_edit_message(chat_id, progress_msg_id, f"✅ Download complete:\n`{original_filename}`"))

            # Download successful, present action options
            if chat_id in user_tasks: # Re-check in case task was cancelled
                 user_tasks[chat_id]['state'] = 'waiting_action'
                 action_markup = InlineKeyboardMarkup([
                     [InlineKeyboardButton("ضغط الفيديو", callback_data=f"compress_{chat_id}"),
                      InlineKeyboardButton("رفع بدون ضغط", callback_data=f"upload_raw_{chat_id}")]
                 ])
                 schedule_async_task(_send_message(chat_id, "📥 تم التنزيل. ماذا تود أن تفعل؟", reply_markup=action_markup))
                 # The action message ID is needed in the callback handler, but we get it there from the callback_query.message
                 # No need to store action_msg_id in user_tasks unless we need to delete it *before* callback

                 # Delete the progress message if successful
                 schedule_async_task(_delete_messages(chat_id, progress_msg_id))


        else:
            print(f"aria2c download failed for chat {chat_id} with return code {process.returncode}")
            error_output = full_output if full_output else f"aria2c exited with code {process.returncode}"
            # Handle download failure and cleanup via async helper
            error_msg = f"❌ فشل التنزيل:\n`{error_output[-500:]}`" # Last 500 chars of output
            schedule_async_task(_send_message(chat_id, error_msg)) # Notify user
            schedule_async_task(_delete_messages(chat_id, progress_msg_id)) # Delete progress message
            if chat_id in user_tasks: # Remove task data on failure
                 task_data = user_tasks.pop(chat_id, None)
                 if task_data and task_data.get('file_path') and os.path.exists(task_data['file_path']):
                      try: os.remove(task_data['file_path'])
                      except Exception as e: print(f"Error cleaning up file {task_data['file_path']} after download failure: {e}")


    except FileNotFoundError:
        print("Error: aria2c not found.")
        schedule_async_task(_send_message(chat_id, "❌ فشل التنزيل: أداة `aria2c` غير موجودة على الخادم."))
        schedule_async_task(_delete_messages(chat_id, progress_msg_id)) # Delete progress message
        user_tasks.pop(chat_id, None) # Clean task data

    except Exception as e:
        print(f"An error occurred during aria2c execution for chat {chat_id}: {e}")
        error_msg = f"❌ فشل التنزيل بسبب خطأ غير متوقع: {e}"
        schedule_async_task(_send_message(chat_id, error_msg))
        schedule_async_task(_delete_messages(chat_id, progress_msg_id)) # Delete progress message
        if chat_id in user_tasks:
             task_data = user_tasks.pop(chat_id, None)
             if task_data and task_data.get('file_path') and os.path.exists(task_data['file_path']):
                  try: os.remove(task_data['file_path'])
                  except Exception as e: print(f"Error cleaning up file {task_data['file_path']} after unexpected download error: {e}")


def calculate_bitrate(target_mb, duration_seconds):
    """Calculates the required video bitrate in kb/s for a target size."""
    if duration_seconds <= 0 or target_mb <= 0:
        return 1000 # Default if duration/target is zero or negative

    # Target size in bits = target_mb * 1024 * 1024 * 8
    # Required bitrate (bits/second) = Target size in bits / duration in seconds
    # Convert to kb/s = (Target size in bits / duration in seconds) / 1000
    # Subtract some bits for audio overhead (estimate)
    # Let's assume audio bitrate is fixed and adds to total.
    # Audio bits/second = int(VIDEO_AUDIO_BITRATE[:-1]) * 1000 # '128k' -> 128000 bps
    # Total target bits = target_mb * 1024*1024*8
    # Audio total bits = audio_bitrate_bps * duration_seconds
    # Video target bits = Total target bits - Audio total bits (ensure > 0)
    # Video bitrate bps = Video target bits / duration_seconds

    # A simpler approach: Target MB includes everything. Calculate average total bitrate needed.
    total_bitrate_bps = (target_mb * 1024 * 1024 * 8) / duration_seconds
    total_bitrate_kbps = total_bitrate_bps / 1000

    # It's tricky to hit an exact size with ffmpeg bitrate mode due to overhead.
    # A CRF approach is better for quality vs size, but bitrate is requested.
    # We need a video bitrate that, when combined with audio, hits the total.
    try:
        audio_bitrate_kbps = int(VIDEO_AUDIO_BITRATE[:-1]) # Assumes format like '128k'
    except (ValueError, TypeError):
        audio_bitrate_kbps = 128 # Default if config is weird

    # Estimate required video bitrate by subtracting audio bitrate
    video_bitrate_kbps = total_bitrate_kbps - audio_bitrate_kbps

    return max(500, int(video_bitrate_kbps)) # Ensure a minimum video bitrate

def generate_ffmpeg_command(input_path, output_path, bitrate_kbps):
    """Generates the ffmpeg command for compression with NVENC."""
    ffmpeg_command = [
        'ffmpeg', '-y', # Overwrite output without asking
        '-hwaccel', 'cuda', # Enable CUDA hardware acceleration (requires compatible GPU and build)
        '-i', input_path,
        '-c:v', 'h264_nvenc', # H.264 encoding with NVENC
        '-b:v', f'{bitrate_kbps}k', # Video bitrate in kb/s
        '-preset', 'medium', # NVENC preset
        '-profile:v', 'high', # H.264 profile
        '-map_metadata', '-1', # Remove metadata from input

        # Audio settings from config
        '-c:a', VIDEO_AUDIO_CODEC,
        '-b:a', VIDEO_AUDIO_BITRATE,
        '-ac', str(VIDEO_AUDIO_CHANNELS),
        '-ar', str(VIDEO_AUDIO_SAMPLE_RATE),
         '-map', '0:v:0', # Map video stream 0 from input 0
         '-map', '0:a:0?', # Map audio stream 0 from input 0 (if exists)


        output_path
    ]
    return ffmpeg_command

def process_compression_queue():
    """Thread worker function to process compression tasks sequentially."""
    print("Compression processing thread started and waiting for tasks.")

    while True:
        task = compression_queue.get() # Blocks until a task is available

        chat_id = task['chat_id']
        input_file = task['input_file']
        duration = task['duration']
        target_size_mb = task['target_size_mb']
        status_msg_id = task['status_msg_id']
        original_filename = task['original_filename']
        original_message_chat_id = task['original_message_chat_id']
        original_message_id = task['original_message_id']


        print(f"Processing compression task for chat {chat_id}. Target size: {target_size_mb}MB")

        # Re-check if task was cancelled while waiting in queue
        if chat_id not in user_tasks or user_tasks[chat_id].get('state') == 'cancelled':
             print(f"Task for chat {chat_id} was cancelled, skipping processing.")
             compression_queue.task_done()
             continue

        user_tasks[chat_id]['state'] = 'compressing' # Update state in main dict

        compressed_file_path = None
        try:
            # Calculate bitrate
            bitrate_kbps = calculate_bitrate(target_size_mb, duration)
            print(f"Calculated video bitrate: {bitrate_kbps} kb/s")

            # Create temporary output file path
            # Use a unique name that includes part of the original filename
            base_output_name = f"{chat_id}_{original_message_id}_{target_size_mb}MB"
            # Append part of original filename, safely
            safe_original_part = re.sub(r'[^a-zA-Z0-9_.-]', '', original_filename)
            safe_original_part = safe_original_part[:20] # Limit length
            if safe_original_part:
                base_output_name = f"{base_output_name}_{safe_original_part}"

            compressed_file_path = os.path.join(DOWNLOADS_DIR, f"{base_output_name}_compressed.mp4")


            # Generate and execute FFmpeg command
            ffmpeg_cmd = generate_ffmpeg_command(input_file, compressed_file_path, bitrate_kbps)
            print(f"Executing FFmpeg command: {' '.join(ffmpeg_cmd)}")

            # Update status message (using the async helper)
            schedule_async_task(_edit_message(chat_id, status_msg_id, f"⏳ جاري الضغط ({target_size_mb}MB) ..."))

            # Run FFmpeg subprocess
            # Adding stderr=subprocess.PIPE to capture progress for future
            # For now, just basic subprocess.run check
            ffmpeg_process = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True, timeout=duration * 10) # Increased timeout


            print("FFmpeg command executed successfully.")
            # print(f"FFmpeg stdout:\n{ffmpeg_process.stdout}") # Optional debug print
            # print(f"FFmpeg stderr:\n{ffmpeg_process.stderr}")

            # Upload compressed video to channel
            if CHANNEL_ID:
                schedule_async_task(_send_message(chat_id, "⬆️ جاري الرفع إلى القناة...", reply_to_message_id=original_message_id)) # Notify user

                try:
                    # Use the helper coroutine for sending the document
                    schedule_async_task(
                         _send_document(
                            chat_id=CHANNEL_ID,
                            document=compressed_file_path,
                            caption=f"Compressed to ~{target_size_mb}MB | {original_filename}",
                             # Add file_name here if needed
                         )
                    )

                    print(f"Compressed video upload scheduled to channel: {CHANNEL_ID}")
                    schedule_async_task(_edit_message(chat_id, status_msg_id, f"✅ تم الضغط والرفع إلى القناة بنجاح. الحجم المستهدف: {target_size_mb}MB"))

                except Exception as e: # This catch is for issues with _send_document itself, not the subprocess error
                    print(f"Error scheduling upload to channel {CHANNEL_ID}: {e}")
                    schedule_async_task(_edit_message(chat_id, status_msg_id, f"❌ تم الضغط، ولكن فشل الرفع إلى القناة: {e}"))
            else:
                print("CHANNEL_ID not configured. Compressed video not sent to channel.")
                schedule_async_task(_edit_message(chat_id, status_msg_id, f"✅ تم الضغط بنجاح، ولكن لم يتم تهيئة قناة للرفع."))

        except FileNotFoundError:
            print("Error: ffmpeg not found or NVENC not supported.")
            error_text = "❌ فشل الضغط: أداة `ffmpeg` غير موجودة أو لا تدعم تسريع NVENC."
            schedule_async_task(_edit_message(chat_id, status_msg_id, error_text))

        except subprocess.CalledProcessError as e:
            print("FFmpeg error occurred!")
            stderr_output = e.stderr.strip()
            print(f"FFmpeg stderr: {stderr_output}")
            error_text = f"❌ حدث خطأ أثناء ضغط الفيديو:\n`FFmpeg exited with code {e.returncode}`\nDetails:\n`{stderr_output[-500:]}`" # Last 500 chars
            schedule_async_task(_edit_message(chat_id, status_msg_id, error_text))

        except Exception as e:
            print(f"General error during compression: {e}")
            error_text = f"❌ حدث خطأ غير متوقع أثناء الضغط: {e}"
            schedule_async_task(_edit_message(chat_id, status_msg_id, error_text))

        finally:
            # Clean up temp files related to this task
            # Check if files exist before attempting deletion
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

            # Remove task data from user_tasks only after compression attempt completes (success or failure)
            user_tasks.pop(chat_id, None) # Clean up this user's state data

            compression_queue.task_done() # Indicate task is done for the queue
            print(f"Compression task finished for chat {chat_id}. Queue size remaining: {compression_queue.qsize()}")


# --- Pyrogram Handlers (async def) ---

# Initialize the Bot Client
# Auto-detection of loop is default, but can be explicit: work_dir needs to be separate for concurrent tasks
app = Client(
    "video_compressor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN,
    workdir="./pyrogram_sessions" # Separate directory for session files
)

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    """Handles the /start command."""
    # Cancel any ongoing task for this user before starting a new one
    cancel_task(message.chat.id)
    await message.reply_text("👋 مرحباً! أنا بوت لضغط الفيديو. أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`\n\nأو أرسل لي فيديو مباشراً (ميزة تجريبية).\n\nلتثبيت الادوات في Google Colab انسخ السطر التالي في الخليه الأولى:\n`!pip install -U yt-dlp aria2 ffmpeg-python pyrogram && apt-get update && apt-get install -y aria2 ffmpeg`\n**ملاحظة:** قد تحتاج لتثبيت FFmpeg مع دعم CUDA يدوياً على الخادم للاستفادة من التسريع.") # Added installation hint for Colab

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client: Client, message: Message):
    """Handles the /cancel command."""
    chat_id = message.chat.id
    if chat_id in user_tasks:
        cancel_task(chat_id, user_cancelled=True)
        await message.reply_text("❌ تم إلغاء العملية الجارية.", quote=True)
    else:
        await message.reply_text("لا يوجد عملية جارية لإلغائها.", quote=True)


@app.on_message((filters.text | filters.video | filters.animation) & filters.private)
async def handle_message(client: Client, message: Message):
    """Handles incoming text messages or direct videos."""
    chat_id = message.chat.id
    text = message.text.strip() if message.text else None

    # --- Handle target size input (if state is waiting_size) ---
    if chat_id in user_tasks and user_tasks[chat_id]['state'] == 'waiting_size' and text:
        try:
            target_size_mb = int(text)
            if target_size_mb <= 0:
                 await message.reply_text("حجم الفيديو يجب أن يكون رقماً موجباً. يرجى إرسال الرقم مرة أخرى.", quote=True)
                 return

            task_data = user_tasks[chat_id]
            # Use stored original message info for reply_to
            original_message_id = task_data['original_message_id']
            original_message_chat_id = task_data['original_message_chat_id']

            # Reset state immediately to prevent user sending size again
            user_tasks[chat_id]['state'] = 'queuing' # New temporary state

            # Queue the compression task
            status_msg = await message.reply_text("⌛️ جارٍ إضافة المهمة إلى قائمة الانتظار...", reply_to_message_id=original_message_id)

            # Store the status message ID and other relevant data for the compression thread
            task_data['target_size_mb'] = target_size_mb
            task_data['status_msg_id'] = status_msg.id
            # The rest of task_data (file_path, duration, etc.) should already be there
            # We copy relevant data for the queue item
            compression_task = {
                'chat_id': chat_id,
                'input_file': task_data['file_path'],
                'duration': task_data['duration'],
                'target_size_mb': target_size_mb,
                'status_msg_id': status_msg.id,
                'original_filename': task_data['original_filename'],
                'original_message_chat_id': original_message_chat_id,
                'original_message_id': original_message_id,
            }
            compression_queue.put(compression_task)

            queue_size = compression_queue.qsize()
            await status_msg.edit_text(f"✅ تم إضافة مهمة الضغط إلى قائمة الانتظار. ترتيبك في قائمة الانتظار: **{queue_size}**")

            # The compression processing thread is started at bot startup and runs continuously

        except ValueError:
            await message.reply_text("هذا ليس رقماً صحيحاً. يرجى إرسال حجم الفيديو المطلوب بالميجابايت كـ **رقم فقط** (مثال: `50`).", quote=True)
        except Exception as e:
            print(f"Error processing target size input for chat {chat_id}: {e}")
            # Clean up task data on error
            cancel_task(chat_id, user_cancelled=False)
            await message.reply_text(f"حدث خطأ أثناء معالجة طلبك للحجم: {e}", quote=True)

    # --- Handle new link or direct video ---
    elif chat_id in user_tasks and user_tasks[chat_id]['state'] != 'idle':
        await message.reply_text("أنت قيد المعالجة حالياً. يرجى الانتظار أو استخدام /cancel لإلغاء العملية الجارية.", quote=True)
        return

    elif text and text.startswith("https://t.me/"):
        # Cancel any stale state for this user just in case
        cancel_task(chat_id)

        parse_result = parse_telegram_link(text)
        if not parse_result:
            await message.reply_text("صيغة الرابط غير صحيحة. يرجى إرسال رابط بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`", quote=True)
            return

        channel_username, message_id_str = parse_result
        print(f"Received Telegram link: Channel={channel_username}, Message ID={message_id_str}")
        # Store original message details for future replies
        original_message_id = message.id
        original_message_chat_id = chat_id


        try:
            # Send an initial processing message
            process_msg = await message.reply_text("🔍 جارٍ استخلاص معلومات الفيديو...", quote=True)

            # --- Step 2a: Get Metadata (Duration, Original Filename) ---
            # This is synchronous, runs in the main async event loop, which is fine for short subprocess calls
            duration, original_filename, metadata_error = get_video_metadata(text)
            if metadata_error:
                 await process_msg.edit_text(f"❌ فشل استخلاص معلومات الفيديو:\n`{metadata_error}`\n\nيرجى التأكد من صحة الرابط وأن القناة عامة وليست خاصة.")
                 # No task data created yet, just return
                 return

            if duration <= 0:
                 await process_msg.edit_text(f"⚠️ فشل استخلاص مدة الفيديو أو المدة صفر. لا يمكن المتابعة.\n\nيرجى التأكد من أن الرابط يحتوي على فيديو صالح.")
                 return

            await process_msg.edit_text("✅ تم استخلاص المعلومات بنجاح. جارٍ الحصول على رابط التنزيل...")


            # --- Step 2b: Get Direct Download URL ---
            # Synchronous call
            download_url, url_error = get_download_url_with_yt_dlp(text)
            if url_error:
                await process_msg.edit_text(f"❌ فشل الحصول على رابط التنزيل المباشر:\n`{url_error}`\n\nيرجى التأكد من أن الفيديو متاح للتنزيل عبر yt-dlp.")
                # No task data created yet, just return
                return


            await process_msg.edit_text("✅ تم استخلاص الرابط. جارٍ التحضير للتنزيل...")

            # --- Step 2c: Setup for Download using aria2c ---
            # Generate a unique temporary file path
            # We need a name based on the original filename but safe
            base_download_name = f"{chat_id}_{original_message_id}"
            safe_original_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', original_filename)
            safe_original_part = safe_original_part[:30] # Limit length for safety
            temp_output_file = os.path.join(DOWNLOADS_DIR, f"{base_download_name}_{safe_original_part}_temp_download.mp4") # Assuming mp4 for most cases


            # Store task data *before* starting the thread
            user_tasks[chat_id] = {
                'state': 'downloading',
                'link': text,
                'download_url': download_url, # Store the extracted URL
                'file_path': temp_output_file, # Store the expected final path
                'duration': duration,
                'original_filename': original_filename,
                'download_msg_id': process_msg.id,
                'action_msg_id': None, # Will be set later
                'status_msg_id': None, # Will be set later for compression
                'original_message_chat_id': original_message_chat_id,
                'original_message_id': original_message_id,
            }

            # Edit message to indicate downloading state
            await process_msg.edit_text("⬇️ جارٍ بدء التنزيل...")


            # Run aria2c in a separate thread to not block the main handler loop
            # The thread function will update progress and handle next steps (actions)
            download_thread = threading.Thread(
                target=run_aria2c_and_report_progress,
                args=(chat_id,), # Pass chat_id only, function will fetch task_data
                daemon=True
            )
            download_thread.start()


        except Exception as e:
            print(f"Error handling link {text} for chat {chat_id}: {e}")
            # General unexpected error during initial phase
            if chat_id in user_tasks: # Clean up task data if it was partially created
                 cancel_task(chat_id, user_cancelled=False) # Clean up data and files
            await message.reply_text(f"حدث خطأ غير متوقع أثناء المعالجة: {e}")

    # --- Handle direct video or animation upload ---
    elif message.video or message.animation:
         # This part handles direct file uploads as in your original script
         # It needs to be integrated carefully with the state machine and queue.
         # For simplicity in this update focused on links and threading,
         # we'll add a placeholder or basic logic. A full implementation
         # would store file_id, download using client.download_media (potentially in a thread)
         # and then follow the same state logic (waiting_action, etc.)

         await message.reply_text("ميزة رفع الفيديو المباشر قيد التطوير حالياً. يرجى استخدام روابط تيليجرام حالياً.", quote=True)
         # To implement this fully:
         # 1. Download the media using client.download_media (might block, consider thread).
         # 2. Get duration/filename from message.video/animation object.
         # 3. Store file_path, duration, filename in user_tasks.
         # 4. Change state to 'waiting_action' and send inline keyboard.
         # 5. Follow existing callback logic for 'upload_raw' or 'compress'.

    # --- Handle any other text input when not waiting for size ---
    elif text: # Only if it was a text message that didn't match other conditions
        await message.reply_text("أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`\n\nأو أرسل لي فيديو مباشراً (قيد التطوير).")

@app.on_callback_query()
async def handle_callback(client: Client, callback_query):
    """Handles inline keyboard button presses."""
    data = callback_query.data
    chat_id = callback_query.message.chat.id
    message_id = callback_query.message.id # ID of the action message

    # Ensure the callback is for a known, active task and action message
    # Note: user_tasks['action_msg_id'] is NOT set for now. We rely on the state.
    if chat_id not in user_tasks or user_tasks[chat_id]['state'] != 'waiting_action':
        print(f"Callback received for unknown/stale task: {data} from chat {chat_id}, message {message_id}")
        await callback_query.answer("انتهت صلاحية هذا الطلب أو تم معالجته مسبقاً.", show_alert=True)
        try:
             await callback_query.message.delete()
        except Exception:
             pass # Ignore errors if message is already gone
        return

    await callback_query.answer() # Answer the callback immediately

    task_data = user_tasks[chat_id]
    file_path = task_data['file_path']
    duration = task_data['duration']
    original_filename = task_data['original_filename']
    # Use stored original message info for reply_to
    original_message_id = task_data['original_message_id']
    original_message_chat_id = task_data['original_message_chat_id']


    # Delete the action message after processing the choice
    try:
        await callback_query.message.delete()
    except Exception as e:
        print(f"Error deleting action message {message_id}: {e}")

    if data.startswith("upload_raw_"):
        print(f"Uploading raw video for chat {chat_id}")
        user_tasks[chat_id]['state'] = 'uploading_raw'

        if not os.path.exists(file_path):
            await _send_message(chat_id, "❌ خطأ: لم يتم العثور على الملف الأصلي للرفع.")
            cancel_task(chat_id, user_cancelled=False) # Clean task data
            return

        try:
            upload_status_msg = await client.send_message(chat_id, "⬆️ جارٍ رفع الفيديو الأصلي...", reply_to_message_id=original_message_id)

            await client.send_document(
                chat_id=chat_id,
                document=file_path,
                file_name=original_filename + os.path.splitext(file_path)[1], # Use original filename with correct extension
                reply_to_message_id=original_message_id
            )
            await upload_status_msg.edit_text("✅ تم رفع الفيديو الأصلي بنجاح!")
            print(f"Raw video uploaded successfully for chat {chat_id}")

        except Exception as e:
            print(f"Error uploading raw video for chat {chat_id}: {e}")
            await client.send_message(chat_id, f"❌ فشل رفع الفيديو الأصلي: {e}", reply_to_message_id=original_message_id)

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
            reply_to_message_id=original_message_id # Reply to the original user message
        )

def cancel_task(chat_id, user_cancelled=True):
    """Cancels the current task for a user, cleans up resources."""
    print(f"Attempting to cancel task for chat {chat_id}, user_cancelled: {user_cancelled}")
    task_data = user_tasks.get(chat_id)

    if task_data:
        # Mark the task as cancelled to signal threads (they should check this state)
        task_data['state'] = 'cancelled'
        print(f"Task for chat {chat_id} marked as cancelled.")

        # Try to delete associated messages asynchronously
        message_ids_to_delete = []
        if task_data.get('download_msg_id'): message_ids_to_delete.append(task_data['download_msg_id'])
        if task_data.get('action_msg_id'): message_ids_to_delete.append(task_data['action_msg_id'])
        if task_data.get('status_msg_id'): message_ids_to_delete.append(task_data['status_msg_id'])

        if message_ids_to_delete:
            # Schedule deletion using the event loop
            schedule_async_task(_delete_messages(chat_id, message_ids_to_delete))


        # Clean up temp file asynchronously
        file_path = task_data.get('file_path')
        if file_path and os.path.exists(file_path):
            # Schedule file deletion using the event loop or run directly if it's safe/needed immediately
            # Deleting from a thread is generally safe, but might interfere if subprocess is still using it.
            # Let the threaded functions (aria2c, ffmpeg) handle their own file cleanup in their 'finally' blocks
            # based on the 'cancelled' state or process exit.
            # Removing the manual deletion from here. Thread cleanup is more reliable.
             pass
             # try:
             #      os.remove(file_path)
             #      print(f"Deleted temp file {file_path} on cancel.")
             # except Exception as e:
             #      print(f"Error deleting temp file {file_path} on cancel: {e}")


        # Notify user
        if user_cancelled:
             # Schedule sending message using the event loop
             schedule_async_task(_send_message(chat_id, "✅ تم إلغاء العملية.", reply_to_message_id=task_data.get('original_message_id')))

        # Remove the task data from the global dictionary
        user_tasks.pop(chat_id, None)
        print(f"Task data for chat {chat_id} removed from user_tasks.")
    else:
        if user_cancelled: # Only message if user explicitly cancelled and there was nothing
             print(f"Cancel requested for chat {chat_id} but no task found.")
             # Optionally send a message saying no task is active

# --- Main Execution ---

if __name__ == "__main__":
    print("Bot starting...")
    cleanup_downloads() # Clean up temp files on startup
    print("Starting Pyrogram client...")

    # Start the compression processing thread on startup, make it daemon
    # It will wait for tasks in the queue forever (until main program exits)
    processing_thread = threading.Thread(target=process_compression_queue, daemon=True)
    processing_thread.start()

    # Pyrogram client needs to run the main event loop
    # It will block until interrupted (e.g., Ctrl+C)
    app.run() # This is a blocking call that starts the async event loop

    print("Bot stopped.")
