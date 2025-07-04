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

# --- Preparation for Pyrogram Session Directory ---
SESSION_DIR = "./pyrogram_sessions"
if not os.path.exists(SESSION_DIR):
    try:
        os.makedirs(SESSION_DIR)
        print(f"Created session directory: {SESSION_DIR}")
    except Exception as e:
        print(f"Error creating session directory {SESSION_DIR}: {e}")
        # Handle the error - maybe exit or try a different location.
        # For now, we print and proceed, letting Pyrogram potentially raise the error again.

# Optional: Clean old session files in case they are corrupted. Useful especially after crashes.
try:
    if os.path.exists(SESSION_DIR):
        for fname in os.listdir(SESSION_DIR):
            if fname.endswith(".session") or fname.endswith(".session-journal") or fname.endswith(".session-wal"):
                fpath = os.path.join(SESSION_DIR, fname)
                try:
                    os.remove(fpath)
                    print(f"Deleted old session file: {fpath}")
                except Exception as e:
                    print(f"Error deleting old session file {fpath}: {e}")
except Exception as e:
    print(f"Error during session directory cleanup: {e}")


# Queue for sequential compression tasks
compression_queue = queue.Queue()
processing_thread = None

# Dictionary to store per-user/per-message data
user_tasks = {}

# --- Helper Functions for Thread-Safe Pyrogram Calls ---
# ... (rest of the helper functions, async helpers, etc. - NO CHANGE HERE) ...
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

async def _send_message(chat_id, text, reply_to_message_id=None, reply_markup=None):
    """Helper coroutine to send a message."""
    try:
        return await app.send_message(chat_id, text, reply_to_message_id=reply_to_message_id, reply_markup=reply_markup)
    except FloodWait as e:
        print(f"FloodWait sending message: {e.value} seconds")
        await asyncio.sleep(e.value)
        return await _send_message(chat_id, text, reply_to_message_id=reply_to_message_id, reply_markup=reply_markup) # Retry after wait
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
    if app.loop and app.loop.is_running():
         try:
              # Check if loop is closed before scheduling
              if not app.loop.is_closed():
                   asyncio.run_coroutine_threadsafe(coro, app.loop)
              else:
                   print("Error: Event loop is closed. Cannot schedule async task.")
         except Exception as e:
              print(f"Error scheduling async task: {e}")
    else:
         print("Warning: Event loop not running or not accessible. Cannot schedule async task yet.")


# --- Helper Functions (Synchronous unless marked async) ---
# ... (rest of synchronous helper functions like cleanup_downloads, parse_telegram_link,
#      get_video_metadata, get_download_url_with_yt_dlp, calculate_bitrate,
#      generate_ffmpeg_command, process_compression_queue) ...
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
        result = subprocess.run(
            ['yt-dlp', '-q', '--dump-json', link],
            capture_output=True, text=True, check=True, timeout=60
            # Removed stderr=subprocess.PIPE
        )
        # Check stderr *after* the run (captured via capture_output)
        if result.stderr:
            print(f"yt-dlp stderr (metadata): {result.stderr.strip()}")

        metadata = json.loads(result.stdout.strip())
        duration = int(metadata.get('duration', 0))
        original_filename = metadata.get('title') or metadata.get('id') or 'video'
        original_filename = re.sub(r'[\\/:*?"<>|]', '_', original_filename)
        return duration, original_filename, None
    except FileNotFoundError:
        return None, None, "[Errno 2] yt-dlp command not found. Please ensure yt-dlp is installed and in PATH."
    except subprocess.CalledProcessError as e:
        error_msg = f"yt-dlp metadata error (code {e.returncode}): {e.stderr.strip()}"
        print(error_msg)
        return None, None, error_msg
    except json.JSONDecodeError as e:
        # Need to access stderr from the result object here
        stderr_output = result.stderr.strip() if 'result' in locals() and result.stderr else "N/A"
        error_msg = f"Error decoding yt-dlp JSON metadata: {e}\nyt-dlp stdout:\n{result.stdout[:500]}...\nyt-dlp stderr:\n{stderr_output[:500]}..." # Include part of stdout/stderr
        print(error_msg)
        return None, None, error_msg
    except Exception as e:
        error_msg = f"Error processing yt-dlp metadata: {e}"
        print(error_msg)
        return None, None, error_msg

def get_download_url_with_yt_dlp(link):
    """Uses yt-dlp to extract the direct download URL from a Telegram link."""
    print(f"Getting download URL for: {link}")
    try:
        result = subprocess.run(
            ['yt-dlp', '-q', '--get-url', link],
            capture_output=True, text=True, check=True, timeout=60
             # Removed stderr=subprocess.PIPE
        )
        # Check stderr after the run
        if result.stderr:
             print(f"yt-dlp stderr (get-url): {result.stderr.strip()}")

        url = result.stdout.strip()
        if not url:
             # yt-dlp might succeed but find no suitable URL
             stderr_output = result.stderr.strip() if result.stderr else "N/A"
             return None, f"yt-dlp returned empty URL. stderr: {stderr_output}"

        print(f"Extracted URL: {url[:100]}...")
        return url, None
    except FileNotFoundError:
         return None, "[Errno 2] yt-dlp command not found. Please ensure yt-dlp is installed and in PATH."
    except subprocess.CalledProcessError as e:
        error_msg = f"yt-dlp get-url error (code {e.returncode}): {e.stderr.strip()}"
        print(error_msg)
        return None, error_msg
    except Exception as e:
        error_msg = f"Error during URL extraction: {e}"
        print(error_msg)
        return None, error_msg

def run_aria2c_and_report_progress(chat_id):
    """Runs aria2c and edits a Telegram message to show progress.
       This runs in a separate thread."""

    task_data = user_tasks.get(chat_id)
    if not task_data or task_data['state'] != 'downloading':
        print(f"Task data not found or state not downloading for chat {chat_id}")
        return

    link = task_data['link']
    download_url = task_data['download_url']
    download_path = task_data['file_path']
    progress_msg_id = task_data['download_msg_id']
    original_filename = task_data['original_filename']
    original_message_id = task_data['original_message_id']


    print(f"Starting aria2c download for chat {chat_id}...")
    aria2c_cmd = [
        'aria2c',
        download_url,
        '--dir', os.path.dirname(download_path),
        '--out', os.path.basename(download_path),
        '-x', '16',
        '-s', '16',
        '--auto-file-renaming=false',
        '--allow-overwrite=true',
        '--summary-interval=1',
        '-c',
        '--no-conf',
        # '--log-level=info',
        # '--log=/tmp/aria2c.log'
    ]

    os.makedirs(os.path.dirname(download_path), exist_ok=True)

    process = None # Initialize process variable
    try:
        process = subprocess.Popen(aria2c_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        last_edit_time = time.time()
        initial_status_sent = False
        progress_pattern = re.compile(r'\[DL:\s+(\d+\.?\d*[KMGT]?i?B)/(\d+\.?\d*[KMGT]?i?B)\((\d+\.?\d*%)\)\s+CN:\s*\d+\s+ETA:\s*(\S+)\s+Speed:\s*(\S+)]')

        while True:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            line = line.strip()

            if "download completed" in line.lower():
                 break

            match = progress_pattern.search(line)
            if match:
                downloaded, total, percentage, eta, speed = match.groups()
                status_text = f"📥 **Downloading:**\n`{original_filename}`\n\n**Progress:** `{downloaded} / {total}`\n**Percentage:** `{percentage}`\n**Speed:** `{speed}`\n**ETA:** `{eta}`"

                current_time = time.time()
                if current_time - last_edit_time >= 2 or not initial_status_sent:
                    schedule_async_task(_edit_message(chat_id, progress_msg_id, status_text))
                    last_edit_time = current_time
                    initial_status_sent = True

            # Check if task was cancelled
            if chat_id not in user_tasks or user_tasks[chat_id].get('state') == 'cancelled':
                 print(f"Task for chat {chat_id} cancelled during download. Terminating aria2c.")
                 if process:
                      try: process.terminate()
                      except Exception: pass
                 break # Exit read loop

        # Wait for the process to actually finish
        if process: # Check if process was successfully started
             stdout, stderr = process.communicate()
             full_output = (line + "\n" + stdout.strip()).strip() if 'line' in locals() else stdout.strip()
             if stderr: print(f"aria2c stderr: {stderr.strip()}")

        # Re-check cancellation status *after* process finishes (or is terminated)
        if chat_id not in user_tasks or user_tasks[chat_id].get('state') == 'cancelled':
             print(f"Task for chat {chat_id} finished or was cancelled. Cleanup handled by cancel_task.")
             # The cancel_task or error handling for cancelled state will clean up.
             # Do not proceed with success/failure logic here.
             return # Exit the thread function

        # If process ended and state is NOT cancelled, evaluate result
        if process and process.returncode == 0 and os.path.exists(download_path):
            print(f"aria2c download completed successfully for chat {chat_id}. File exists at {download_path}")
            schedule_async_task(_edit_message(chat_id, progress_msg_id, f"✅ Download complete:\n`{original_filename}`"))

            if chat_id in user_tasks:
                 user_tasks[chat_id]['state'] = 'waiting_action'
                 action_markup = InlineKeyboardMarkup([
                     [InlineKeyboardButton("ضغط الفيديو", callback_data=f"compress_{chat_id}"),
                      InlineKeyboardButton("رفع بدون ضغط", callback_data=f"upload_raw_{chat_id}")]
                 ])
                 schedule_async_task(_send_message(chat_id, "📥 تم التنزيل. ماذا تود أن تفعل؟", reply_markup=action_markup, reply_to_message_id=original_message_id))

                 schedule_async_task(_delete_messages(chat_id, progress_msg_id))


        else: # Download failed or file not found after success code (shouldn't happen with allow-overwrite=true and correct paths)
            print(f"aria2c download failed for chat {chat_id}.")
            return_code = process.returncode if process else 'N/A'
            error_output = full_output if full_output else f"aria2c process error. Return code: {return_code}"
            error_msg = f"❌ فشل التنزيل:\n`{error_output[-500:]}`"
            schedule_async_task(_send_message(chat_id, error_msg, reply_to_message_id=original_message_id))
            schedule_async_task(_delete_messages(chat_id, progress_msg_id))
            # Clean up data and file via cancel_task logic which also handles file deletion
            cancel_task(chat_id, user_cancelled=False) # Call cancel_task for cleanup

    except FileNotFoundError:
        print("Error: aria2c not found.")
        schedule_async_task(_send_message(chat_id, "❌ فشل التنزيل: أداة `aria2c` غير موجودة على الخادم.", reply_to_message_id=original_message_id))
        schedule_async_task(_delete_messages(chat_id, progress_msg_id))
        cancel_task(chat_id, user_cancelled=False) # Clean task data

    except Exception as e:
        print(f"An error occurred during aria2c execution for chat {chat_id}: {e}")
        error_msg = f"❌ فشل التنزيل بسبب خطأ غير متوقع: {e}"
        schedule_async_task(_send_message(chat_id, error_msg, reply_to_message_id=original_message_id))
        schedule_async_task(_delete_messages(chat_id, progress_msg_id))
        cancel_task(chat_id, user_cancelled=False) # Clean task data and file


def calculate_bitrate(target_mb, duration_seconds):
    """Calculates the required video bitrate in kb/s for a target size."""
    if duration_seconds <= 0 or target_mb <= 0:
        return 1000 # Default if duration/target is zero or negative

    total_bitrate_bps = (target_mb * 1024 * 1024 * 8) / duration_seconds
    total_bitrate_kbps = total_bitrate_bps / 1000

    try:
        # Convert audio bitrate from string (e.g., '128k') to kbps integer
        # Assumes format like '128k' or just '128'. Handle potential 'M' for Megabits.
        audio_bitrate_str = str(VIDEO_AUDIO_BITRATE).lower()
        if audio_bitrate_str.endswith('k'):
             audio_bitrate_kbps = int(audio_bitrate_str[:-1])
        elif audio_bitrate_str.endswith('m'):
             audio_bitrate_kbps = int(audio_bitrate_str[:-1]) * 1000
        else:
             audio_bitrate_kbps = int(audio_bitrate_str) # Assume kbps if no suffix

    except (ValueError, TypeError):
        audio_bitrate_kbps = 128 # Default if config is weird/invalid

    # Estimate required video bitrate by subtracting estimated audio bitrate
    # Ensure resulting video bitrate is not negative
    video_bitrate_kbps = max(500, int(total_bitrate_kbps - audio_bitrate_kbps)) # Ensure min 500 kbps video

    # It's best practice for 2-pass encoding to hit a target size, but 1-pass with bitrate is simpler.
    # Bitrate VBR might still exceed target size. Adding a buffer might be wise.
    # e.g., target_bitrate = video_bitrate_kbps * 0.95 # Target slightly lower?

    return video_bitrate_kbps

def generate_ffmpeg_command(input_path, output_path, bitrate_kbps):
    """Generates the ffmpeg command for compression (using libx264 - CPU based)."""
    ffmpeg_command = [
        'ffmpeg', '-y',
        # Removed -hwaccel cuda
        '-i', input_path,
        '-c:v', 'libx264', # Changed codec to libx264 (CPU based)
        '-b:v', f'{bitrate_kbps}k',
        '-preset', 'medium', # x264 presets: ultrafast, superfast, fast, medium, slow, slower, veryslow
        '-profile:v', 'high',
        '-map_metadata', '-1',

        # Audio settings from config
        '-c:a', VIDEO_AUDIO_CODEC,
        '-b:a', VIDEO_AUDIO_BITRATE,
        '-ac', str(VIDEO_AUDIO_CHANNELS),
        '-ar', str(VIDEO_AUDIO_SAMPLE_RATE),
        '-map', '0:v:0',
        '-map', '0:a:0?',

        output_path
    ]
    # Note: if you want to use NVENC when available and fallback to libx264 otherwise,
    # the logic to build the command needs to check for NVENC support first (e.g. via ffmpeg -encoders)
    # or handle the subprocess error for CUDA and retry with libx264.
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
        original_message_id = task['original_message_id']


        print(f"Processing compression task for chat {chat_id}. Target size: {target_size_mb}MB")

        # Re-check if task was cancelled while waiting in queue
        if chat_id not in user_tasks or user_tasks[chat_id].get('state') == 'cancelled':
             print(f"Task for chat {chat_id} was cancelled, skipping processing.")
             # Need to clean up file if it still exists? The cancel_task should handle this.
             # Ensure the 'cancel_task' function is robust in removing the file when state is 'cancelled'.
             compression_queue.task_done()
             continue

        # We update state in user_tasks in the main handler when size is received,
        # but this check inside the thread adds robustness.
        if chat_id in user_tasks: user_tasks[chat_id]['state'] = 'compressing'


        compressed_file_path = None
        ffmpeg_process = None
        try:
            # Calculate bitrate
            bitrate_kbps = calculate_bitrate(target_size_mb, duration)
            print(f"Calculated video bitrate: {bitrate_kbps} kb/s")

            base_output_name = f"{chat_id}_{original_message_id}_{target_size_mb}MB"
            safe_original_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', original_filename)
            safe_original_part = safe_original_part[:30]
            if safe_original_part:
                base_output_name = f"{base_output_name}_{safe_original_part}"

            compressed_file_path = os.path.join(DOWNLOADS_DIR, f"{base_output_name}_compressed.mp4")
            os.makedirs(os.path.dirname(compressed_file_path), exist_ok=True) # Ensure dir exists


            ffmpeg_cmd = generate_ffmpeg_command(input_file, compressed_file_path, bitrate_kbps)
            print(f"Executing FFmpeg command: {' '.join(ffmpeg_cmd)}")

            schedule_async_task(_edit_message(chat_id, status_msg_id, f"⏳ جاري الضغط ({target_size_mb}MB) ..."))

            # Run FFmpeg subprocess
            # Can potentially parse stderr for progress here too if needed, but simpler for now
            ffmpeg_process = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True, timeout=duration * 10)

            print("FFmpeg command executed successfully.")

            # Upload compressed video to channel
            if CHANNEL_ID:
                schedule_async_task(_send_message(chat_id, "⬆️ جاري الرفع إلى القناة...", reply_to_message_id=original_message_id))

                try:
                    schedule_async_task(
                         _send_document(
                            chat_id=CHANNEL_ID,
                            document=compressed_file_path,
                            caption=f"Compressed to ~{target_size_mb}MB | {original_filename}",
                            file_name=os.path.basename(compressed_file_path) # Use generated filename for upload
                         )
                    )
                    print(f"Compressed video upload scheduled to channel: {CHANNEL_ID}")
                    schedule_async_task(_edit_message(chat_id, status_msg_id, f"✅ تم الضغط والرفع إلى القناة بنجاح. الحجم المستهدف: {target_size_mb}MB"))

                except Exception as e:
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
            error_text = f"❌ حدث خطأ أثناء ضغط الفيديو:\n`FFmpeg exited with code {e.returncode}`\nDetails:\n`{stderr_output[-500:]}`"
            schedule_async_task(_edit_message(chat_id, status_msg_id, error_text))

        except Exception as e:
            print(f"General error during compression: {e}")
            error_text = f"❌ حدث خطأ غير متوقع أثناء الضغط: {e}"
            schedule_async_task(_edit_message(chat_id, status_msg_id, error_text))

        finally:
            # Clean up temp files
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

            # Clean up user task data regardless of outcome
            user_tasks.pop(chat_id, None)
            print(f"Compression task finished for chat {chat_id}. User data removed. Queue size remaining: {compression_queue.qsize()}")

            compression_queue.task_done() # Indicate task is done for the queue


def cancel_task(chat_id, user_cancelled=True):
    """Cancels the current task for a user, cleans up resources."""
    print(f"Attempting to cancel task for chat {chat_id}, user_cancelled: {user_cancelled}")
    task_data = user_tasks.get(chat_id)

    if task_data:
        # Mark the task as cancelled
        task_data['state'] = 'cancelled'
        print(f"Task for chat {chat_id} marked as cancelled.")

        # Try to terminate subprocesses if running (this is best effort)
        # We don't store process handles in user_tasks yet,
        # so this is hard to do reliably from here.
        # Let's rely on the subprocesses checking the 'cancelled' state in user_tasks.

        # Schedule message and file cleanup asynchronously
        message_ids_to_delete = []
        if task_data.get('download_msg_id'): message_ids_to_delete.append(task_data['download_msg_id'])
        if task_data.get('action_msg_id'): message_ids_to_delete.append(task_data['action_msg_id'])
        if task_data.get('status_msg_id'): message_ids_to_delete.append(task_data['status_msg_id'])

        if message_ids_to_delete:
            schedule_async_task(_delete_messages(chat_id, message_ids_to_delete))

        # File cleanup is handled by the thread's finally block upon completion/termination

        # Notify user
        if user_cancelled:
             schedule_async_task(_send_message(chat_id, "✅ تم إلغاء العملية الجارية.", reply_to_message_id=task_data.get('original_message_id')))

        # Remove the task data
        user_tasks.pop(chat_id, None)
        print(f"Task data for chat {chat_id} removed from user_tasks.")
    else:
        if user_cancelled:
             print(f"Cancel requested for chat {chat_id} but no task found.")
             # schedule_async_task(_send_message(chat_id, "لا يوجد عملية جارية لإلغائها.")) # Optional: notify user if no task

# --- Pyrogram Handlers (async def) ---
# Initialize the Bot Client
app = Client(
    "video_compressor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN,
    workdir=SESSION_DIR # Use the defined SESSION_DIR variable
)

@app.on_message(filters.command("check_channel") & filters.private)
async def check_channel_command(client: Client, message: Message):
    """Checks if a given channel ID/username is valid and accessible."""
    chat_info_str = message.text.split(maxsplit=1)
    if len(chat_info_str) != 2:
        await message.reply_text("الاستخدام:\n`/check_channel <اسم_القناة_أو_آيدي>`\n\nمثال: `/check_channel my_channel` أو `/check_channel -1001234567890`", quote=True)
        return

    chat_id_or_username = chat_info_str[1].strip()
    await message.reply_text(f"🔍 جارٍ التحقق من القناة: `{chat_id_or_username}` ...", quote=True)

    try:
        # Try to get chat information
        chat = await client.get_chat(chat_id_or_username)

        # Extract relevant info
        chat_type = chat.type
        chat_title = chat.title
        chat_id_numeric = chat.id # This is the numeric ID as an integer

        response_text = f"✅ تم العثور على القناة:\n"
        response_text += f"**النوع:** `{chat_type.value}`\n" # Use .value for string representation
        response_text += f"**العنوان:** `{chat_title}`\n"
        response_text += f"**آيدي (للاستخدام في Pyrogram):** `{chat_id_numeric}`\n"
        response_text += f"**اسم المستخدم (@):** `{chat.username or 'N/A'}`\n"

        await message.reply_text(response_text, quote=True)

    except Exception as e:
        await message.reply_text(f"❌ فشل التحقق من القناة:\n`{e}`\n\nيرجى التأكد من صحة آيدي أو اسم المستخدم وأن البوت عضو أو مشرف في القناة إذا كانت خاصة.", quote=True)

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    """Handles the /start command."""
    cancel_task(message.chat.id) # Cancel any ongoing task for this user
    await message.reply_text("👋 مرحباً! أنا بوت لضغط الفيديو. أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`\n\nلتثبيت الادوات في Google Colab انسخ السطر التالي في الخليه الأولى:\n`!pip install -U yt-dlp aria2 pyrogram && apt-get update && apt-get install -y aria2 ffmpeg`\n**ملاحظة:** قد تحتاج لتثبيت FFmpeg مع دعم CUDA يدوياً على الخادم للاستفادة من التسريع.")

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client: Client, message: Message):
    """Handles the /cancel command."""
    chat_id = message.chat.id
    if chat_id in user_tasks:
        cancel_task(chat_id, user_cancelled=True)
        # Message sending is handled inside cancel_task now
    else:
        await message.reply_text("لا يوجد عملية جارية لإلغائها.", quote=True)

@app.on_message((filters.text | filters.video | filters.animation) & filters.private)
async def handle_message(client: Client, message: Message):
    """Handles incoming text messages or direct videos."""
    chat_id = message.chat.id
    text = message.text.strip() if message.text else None
    original_message_id = message.id
    original_message_chat_id = chat_id # Redundant here but good practice


    # --- Handle target size input (if state is waiting_size) ---
    if chat_id in user_tasks and user_tasks[chat_id]['state'] == 'waiting_size' and text:
        try:
            target_size_mb = int(text)
            if target_size_mb <= 0:
                 await message.reply_text("حجم الفيديو يجب أن يكون رقماً موجباً. يرجى إرسال الرقم مرة أخرى.", quote=True)
                 return

            task_data = user_tasks[chat_id]
            # Ensure essential data exists from previous step
            if not task_data.get('file_path') or not task_data.get('duration'):
                print(f"Error: Missing file_path or duration for chat {chat_id} in waiting_size state.")
                await message.reply_text("حدث خطأ في بيانات المهمة. يرجى إرسال الرابط مرة أخرى.", quote=True)
                cancel_task(chat_id, user_cancelled=False) # Clean up state
                return


            # Use stored original message info for reply_to in queue thread messages
            # These are already stored in task_data when link was processed
            # original_message_id = task_data.get('original_message_id')
            # original_message_chat_id = task_data.get('original_message_chat_id')


            # Reset state immediately
            user_tasks[chat_id]['state'] = 'queuing' # New temporary state

            # Queue the compression task
            status_msg = await message.reply_text("⌛️ جارٍ إضافة المهمة إلى قائمة الانتظار...", reply_to_message_id=original_message_id)

            # Store the status message ID and target size in task_data (will be passed to thread via queue)
            task_data['target_size_mb'] = target_size_mb
            task_data['status_msg_id'] = status_msg.id

            # Package relevant data for the queue item
            compression_task = {
                'chat_id': chat_id,
                'input_file': task_data['file_path'],
                'duration': task_data['duration'],
                'target_size_mb': target_size_mb,
                'status_msg_id': status_msg.id,
                'original_filename': task_data['original_filename'],
                'original_message_chat_id': task_data['original_message_chat_id'], # Pass these to thread
                'original_message_id': task_data['original_message_id'],         # Pass these to thread
            }
            compression_queue.put(compression_task)

            queue_size = compression_queue.qsize()
            await status_msg.edit_text(f"✅ تم إضافة مهمة الضغط إلى قائمة الانتظار. ترتيبك في قائمة الانتظار: **{queue_size}**")

            # The compression processing thread is started at bot startup and runs continuously


        except ValueError:
            await message.reply_text("هذا ليس رقماً صحيحاً. يرجى إرسال حجم الفيديو المطلوب بالميجابايت كـ **رقم فقط** (مثال: `50`).", quote=True)
        except Exception as e:
            print(f"Error processing target size input for chat {chat_id}: {e}")
            await message.reply_text(f"حدث خطأ أثناء معالجة طلبك للحجم: {e}", quote=True)
            cancel_task(chat_id, user_cancelled=False) # Clean up task data on error

    # --- Handle new link ---
    elif text and text.startswith("https://t.me/"):
        # Cancel any stale state for this user just in case
        cancel_task(chat_id)

        parse_result = parse_telegram_link(text)
        if not parse_result:
            await message.reply_text("صيغة الرابط غير صحيحة. يرجى إرسال رابط بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`", quote=True)
            return

        channel_username, message_id_str = parse_result
        print(f"Received Telegram link: Channel={channel_username}, Message ID={message_id_str}")

        try:
            # Send an initial processing message
            process_msg = await message.reply_text("🔍 جارٍ استخلاص معلومات الفيديو...", quote=True)

            # --- Step 2a: Get Metadata (Duration, Original Filename) ---
            # This is synchronous, runs in the main async event loop
            duration, original_filename, metadata_error = get_video_metadata(text)
            if metadata_error:
                 await process_msg.edit_text(f"❌ فشل استخلاص معلومات الفيديو:\n`{metadata_error}`\n\nيرجى التأكد من صحة الرابط وأن القناة عامة وليست خاصة.")
                 # No task data created yet, just return
                 return

            if duration is None or duration <= 0:
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
            # Generate a unique temporary file path based on chat and message ID
            base_download_name = f"{chat_id}_{original_message_id}"
            # Append a part of original filename for better identification, sanitized
            safe_original_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', original_filename)
            safe_original_part = safe_original_part[:30] # Limit length
            temp_output_file = os.path.join(DOWNLOADS_DIR, f"{base_download_name}_{safe_original_part}_temp_download.bin") # Use .bin initially, rename later if needed

            # Store task data *before* starting the thread
            user_tasks[chat_id] = {
                'state': 'downloading',
                'link': text,
                'download_url': download_url, # Store the extracted URL
                'file_path': temp_output_file, # Store the expected final path
                'duration': duration,
                'original_filename': original_filename,
                'download_msg_id': process_msg.id,
                'action_msg_id': None,
                'status_msg_id': None,
                'original_message_chat_id': chat_id, # Store original chat/message IDs
                'original_message_id': original_message_id,
            }

            # Edit message to indicate downloading state
            await process_msg.edit_text("⬇️ جارٍ بدء التنزيل...")


            # Run aria2c in a separate thread
            download_thread = threading.Thread(
                target=run_aria2c_and_report_progress,
                args=(chat_id,), # Pass chat_id only
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
    # This section is still a placeholder
    elif message.video or message.animation:
         await message.reply_text("ميزة رفع الفيديو المباشر قيد التطوير حالياً. يرجى استخدام روابط تيليجرام حالياً.", quote=True)
         # To implement this fully:
         # 1. Download the media using client.download_media (might block, consider thread).
         # 2. Get duration/filename from message.video/animation object.
         # 3. Store file_path, duration, filename in user_tasks.
         # 4. Change state to 'waiting_action' and send inline keyboard.
         # 5. Follow existing callback logic for 'upload_raw' or 'compress'.


    # --- Handle any other text input when not waiting for size ---
    elif text:
        await message.reply_text("أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>`\n\nأو أرسل لي فيديو مباشراً (قيد التطوير).")


@app.on_callback_query()
async def handle_callback(client: Client, callback_query):
    """Handles inline keyboard button presses."""
    data = callback_query.data
    chat_id = callback_query.message.chat.id
    message_id = callback_query.message.id # ID of the action message

    # Ensure the callback is for a known, active task and action message
    if chat_id not in user_tasks or user_tasks[chat_id]['state'] != 'waiting_action':
        print(f"Callback received for unknown/stale task: {data} from chat {chat_id}, message {message_id}")
        await callback_query.answer("انتهت صلاحية هذا الطلب أو تم معالجته مسبقاً.", show_alert=True)
        try:
             await callback_query.message.delete()
        except Exception:
             pass
        return

    await callback_query.answer()

    task_data = user_tasks[chat_id]
    file_path = task_data['file_path']
    duration = task_data['duration']
    original_filename = task_data['original_filename']
    original_message_id = task_data['original_message_id']
    # original_message_chat_id = task_data['original_message_chat_id']


    # Delete the action message after processing the choice
    try:
        await callback_query.message.delete()
    except Exception as e:
        print(f"Error deleting action message {message_id}: {e}")

    if data.startswith("upload_raw_"):
        print(f"Uploading raw video for chat {chat_id}")
        user_tasks[chat_id]['state'] = 'uploading_raw'

        if not os.path.exists(file_path):
            await _send_message(chat_id, "❌ خطأ: لم يتم العثور على الملف الأصلي للرفع.", reply_to_message_id=original_message_id)
            cancel_task(chat_id, user_cancelled=False) # Clean task data
            return

        try:
            upload_status_msg = await client.send_message(chat_id, "⬆️ جارٍ رفع الفيديو الأصلي...", reply_to_message_id=original_message_id)

            await client.send_document(
                chat_id=chat_id,
                document=file_path,
                file_name=original_filename + os.path.splitext(file_path)[1], # Use original filename + extension from downloaded file
                reply_to_message_id=original_message_id
            )
            await upload_status_msg.edit_text("✅ تم رفع الفيديو الأصلي بنجاح!")
            print(f"Raw video uploaded successfully for chat {chat_id}")

        except Exception as e:
            print(f"Error uploading raw video for chat {chat_id}: {e}")
            await client.send_message(chat_id, f"❌ فشل رفع الفيديو الأصلي: {e}", reply_to_message_id=original_message_id)

        finally:
            # Clean up temp file
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

        await client.send_message(
            chat_id,
            "كم ميجابايت تود أن يكون حجم الفيديو المضغوط؟ أرسل **الرقم فقط** (مثال: `50`)",
            reply_to_message_id=original_message_id
        )


def cancel_task(chat_id, user_cancelled=True):
    """Cancels the current task for a user, cleans up resources."""
    print(f"Attempting to cancel task for chat {chat_id}, user_cancelled: {user_cancelled}")
    task_data = user_tasks.get(chat_id)

    if task_data:
        print(f"Cancelling task with state: {task_data.get('state')} for chat {chat_id}")
        # Mark the task as cancelled
        task_data['state'] = 'cancelled'

        # Schedule message deletion
        message_ids_to_delete = []
        if task_data.get('download_msg_id'): message_ids_to_delete.append(task_data['download_msg_id'])
        if task_data.get('action_msg_id'): message_ids_to_delete.append(task_data['action_msg_id'])
        if task_data.get('status_msg_id'): message_ids_to_delete.append(task_data['status_msg_id'])

        if message_ids_to_delete:
            schedule_async_task(_delete_messages(chat_id, message_ids_to_delete))

        # Notify user (if initiated by user)
        if user_cancelled:
             schedule_async_task(_send_message(chat_id, "✅ تم إلغاء العملية الجارية.", reply_to_message_id=task_data.get('original_message_id')))

        # File cleanup: The threads (aria2c/ffmpeg) are designed to check the 'cancelled' state
        # and the finally block should attempt to remove the file they were working on.
        # However, explicitly trying to remove it here as well adds robustness
        # in case the thread didn't start or died early.
        file_path = task_data.get('file_path')
        if file_path and os.path.exists(file_path):
            # Schedule deletion after a small delay might help if a subprocess is just finishing
            def delayed_file_delete(path, delay=2):
                print(f"Scheduled deletion of {path} in {delay} seconds.")
                time.sleep(delay)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        print(f"Deleted temp file {path} during cancellation cleanup.")
                except Exception as e:
                    print(f"Error deleting temp file {path} during cancellation cleanup: {e}")
            # Run deletion in a separate short-lived thread
            threading.Thread(target=delayed_file_delete, args=(file_path,), daemon=True).start()


        # Remove the task data from the global dictionary
        user_tasks.pop(chat_id, None)
        print(f"Task data for chat {chat_id} removed from user_tasks.")

    else:
        if user_cancelled:
             print(f"Cancel requested for chat {chat_id} but no task found.")


# --- Main Execution ---

if __name__ == "__main__":
    print("Bot starting...")    
    cleanup_downloads() # Clean up temp files on startup

    # SESSION_DIR handling is done above, before client initialization

    print("Starting Pyrogram client...")

    # Start the compression processing thread on startup, make it daemon
    processing_thread = threading.Thread(target=process_compression_queue, daemon=True)
    processing_thread.start()

    # Pyrogram client needs to run the main event loop
    app.run() # This is a blocking call that starts the async event loop

    print("Bot stopped.")
