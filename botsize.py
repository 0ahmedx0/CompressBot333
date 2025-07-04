import os
import tempfile
import subprocess
import threading
import time
import re
import json
import shutil
import queue
import asyncio

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import MessageTooLong, FloodWait, BadRequest

# Import configuration from config.py
try:
    from config import *
except ImportError:
    print("خطأ: ملف config.py غير موجود أو لا يحتوي على جميع المتغيرات اللازمة.")
    print("يرجى إنشاء ملف config.py يحتوي على:")
    print("API_ID, API_HASH, API_TOKEN, VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE")
    exit()

# --- Configuration and Global State ---

# Download directory setup
DOWNLOADS_DIR = "./downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# Pyrogram Session directory setup
SESSION_DIR = "./pyrogram_sessions"
if not os.path.exists(SESSION_DIR):
    try:
        os.makedirs(SESSION_DIR)
        print(f"Created session directory: {SESSION_DIR}")
    except Exception as e:
        print(f"Error creating session directory {SESSION_DIR}: {e}")

# Optional: Clean old session files in case they are corrupted on startup
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

# Dictionary to store per-user task data
# Key: user_chat_id, Value: {'state': 'idle'|'downloading'|'waiting_action'|'waiting_size'|'uploading_raw'|'compressing'|'cancelled',
#                            'link': str, 'download_url': str, 'file_path': str, 'duration': int,
#                            'download_msg_id': int, 'action_msg_id': int, 'status_msg_id': int,
#                            'original_filename': str, 'original_message_id': int, 'original_message_chat_id': int}
user_tasks = {}

# --- Helper Functions for Thread-Safe Pyrogram Calls ---
# These helper coroutines are defined to be run from background threads

async def _edit_message(chat_id, message_id, text, reply_markup=None):
    """Helper coroutine to edit a message."""
    if not message_id: # Cannot edit if message_id is None
        return
    try:
        await app.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
    except FloodWait as e:
        print(f"FloodWait editing message {message_id}: {e.value} seconds. Retrying...")
        await asyncio.sleep(e.value)
        await _edit_message(chat_id, message_id, text, reply_markup) # Retry after wait
    except BadRequest as e:
         print(f"Bad Request editing message {message_id}: {e}. Message likely deleted or invalid.")
    except Exception as e:
        print(f"Error editing message {message_id}: {e}")

async def _send_message(chat_id, text, reply_to_message_id=None, reply_markup=None):
    """Helper coroutine to send a message."""
    try:
        return await app.send_message(chat_id, text, reply_to_message_id=reply_to_message_id, reply_markup=reply_markup)
    except FloodWait as e:
        print(f"FloodWait sending message to {chat_id}: {e.value} seconds. Retrying...")
        await asyncio.sleep(e.value)
        return await _send_message(chat_id, text, reply_to_message_id=reply_to_message_id, reply_markup=reply_markup) # Retry after wait
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")
        return None # Indicate failure

async def _delete_messages(chat_id, message_ids):
     """Helper coroutine to delete messages."""
     if not message_ids: return
     if not isinstance(message_ids, list): message_ids = [message_ids] # Ensure it's a list
     try:
          await app.delete_messages(chat_id, message_ids)
     except Exception as e:
          print(f"Error deleting messages {message_ids} in chat {chat_id}: {e}")

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
          print(f"FloodWait sending document to {chat_id}: {e.value} seconds. Retrying...")
          await asyncio.sleep(e.value)
          await _send_document(chat_id, document, caption=caption, file_name=file_name, reply_to_message_id=reply_to_message_id) # Retry
     except Exception as e:
          print(f"Error sending document to {chat_id}: {e}")


def schedule_async_task(coro):
    """Schedules an async coroutine to run on the main event loop from a sync thread."""
    # Check if the app.loop exists and is running before scheduling
    try:
        loop = app.loop # Access the running loop instance from Pyrogram client
        if loop and loop.is_running() and not loop.is_closed():
             asyncio.run_coroutine_threadsafe(coro, loop)
        else:
             print("Warning: Event loop not running or closed. Cannot schedule async task.")
    except Exception as e:
         print(f"Error scheduling async task: {e}")


# --- Helper Functions (Synchronous unless marked async) ---

def cleanup_downloads():
    """Cleans up the downloads directory on bot startup."""
    print(f"Cleaning up downloads directory: {DOWNLOADS_DIR}")
    if not os.path.exists(DOWNLOADS_DIR): return # Directory might not exist yet

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
        return match.groups() # (channel_username, message_id_str)
    # Also match the channel_id format t.me/c/channel_id/message_id
    match_id = re.match(r'https://t.me/c/(\d+)/(\d+)', link)
    if match_id:
        # Format as -100 + id string
        return f"-100{match_id.groups()[0]}", match_id.groups()[1]
    return None

def get_video_metadata(link):
    """Uses yt-dlp to get video metadata (like duration) from a Telegram link.
       Tries original link, then with ?single if first attempt fails with JSON error."""
    print(f"Getting metadata for: {link}")
    attempts = [link]
    # Only add ?single if the link format supports it (i.e., username/id, not c/id/id)
    if "/c/" not in link.lower() and "?single" not in link.lower():
         attempts.append(link + "?single")

    for i, url_attempt in enumerate(attempts):
        print(f"Attempt {i+1}: Getting metadata for {url_attempt}")
        result = None # Initialize result outside try
        try:
            result = subprocess.run(
                ['yt-dlp', '-q', '--dump-json', url_attempt],
                capture_output=True, text=True, check=True, timeout=60
            )
            if result.stderr:
                print(f"yt-dlp stderr (metadata attempt {i+1}): {result.stderr.strip()}")

            # If successful, parse JSON and return
            metadata = json.loads(result.stdout.strip())
            duration = int(metadata.get('duration', 0))
            # Prefer original filename if available, otherwise use title or ID
            original_filename = metadata.get('original_url', '').split('/')[-1].split('?')[0] or \
                                metadata.get('title') or metadata.get('id') or 'video'
            original_filename = re.sub(r'[\\/:*?"<>|]', '_', original_filename)

            # Add original file extension if yt-dlp provides it and it's not already in the name
            ext = metadata.get('ext')
            if ext and not original_filename.lower().endswith(f".{ext}"):
                 original_filename = f"{original_filename}.{ext}"

            print(f"Metadata extraction successful from {url_attempt}. Duration: {duration}s, Filename: {original_filename}")
            return duration, original_filename, None # Success!

        except json.JSONDecodeError as e:
            # This is the specific error we want to retry on if it's not the last attempt
            print(f"JSON decode error (attempt {i+1}): {e}")
            if i == len(attempts) - 1: # If this was the last attempt, report the error
                 stdout_output = result.stdout.strip() if result and result.stdout else "N/A"
                 stderr_output = result.stderr.strip() if result and result.stderr else "N/A"
                 error_msg = f"Error decoding yt-dlp JSON metadata after {len(attempts)} attempts: {e}\nyt-dlp stdout:\n{stdout_output[:500]}...\nyt-dlp stderr:\n{stderr_output[:500]}..."
                 print(error_msg)
                 return None, None, error_msg
            else:
                print(f"JSON decode error on attempt {i+1}, trying next URL...")
                continue

        except FileNotFoundError:
            error_msg = "[Errno 2] yt-dlp command not found. Please ensure yt-dlp is installed and in PATH."
            print(error_msg)
            return None, None, error_msg

        except subprocess.CalledProcessError as e:
            error_msg = f"yt-dlp metadata error (code {e.returncode}, attempt {i+1}): {e.stderr.strip()}"
            print(error_msg)
            if i == len(attempts) - 1:
                 return None, None, error_msg
            else:
                 print(f"CalledProcessError on attempt {i+1}, trying next URL...")
                 continue

        except Exception as e:
            error_msg = f"Unexpected error during yt-dlp metadata (attempt {i+1}): {e}"
            print(error_msg)
            if i == len(attempts) - 1:
                 return None, None, error_msg
            else:
                 print(f"Unexpected error on attempt {i+1}, trying next URL...")
                 continue

    # Should not be reached if attempts list is not empty and no error returned
    return None, None, "Unknown error during metadata extraction attempts."


def get_download_url_with_yt_dlp(link):
    """Uses yt-dlp to extract the direct download URL from a Telegram link.
       Tries original link, then with ?single if first attempt fails to get URL."""
    print(f"Getting download URL for: {link}")
    attempts = [link]
    if "/c/" not in link.lower() and "?single" not in link.lower():
         attempts.append(link + "?single")

    for i, url_attempt in enumerate(attempts):
        print(f"Attempt {i+1}: Getting download URL for {url_attempt}")
        result = None # Initialize result outside try
        try:
            result = subprocess.run(
                ['yt-dlp', '-q', '--get-url', url_attempt],
                capture_output=True, text=True, check=True, timeout=60
            )
            if result.stderr:
                 print(f"yt-dlp stderr (get-url attempt {i+1}): {result.stderr.strip()}")

            url = result.stdout.strip()
            if url:
                 print(f"Download URL extracted successfully from {url_attempt}: {url[:100]}...")
                 return url, None # Success!
            else:
                 if i == len(attempts) - 1:
                      stderr_output = result.stderr.strip() if result and result.stderr else "N/A"
                      return None, f"yt-dlp returned empty URL after {len(attempts)} attempts. stderr: {stderr_output}"
                 else:
                      print(f"yt-dlp returned empty URL on attempt {i+1}, trying next URL...")
                      continue

        except FileNotFoundError:
             error_msg = "[Errno 2] yt-dlp command not found. Please ensure yt-dlp is installed and in PATH."
             print(error_msg)
             return None, error_msg

        except subprocess.CalledProcessError as e:
             error_msg = f"yt-dlp get-url error (code {e.returncode}, attempt {i+1}): {e.stderr.strip()}"
             print(error_msg)
             if i == len(attempts) - 1:
                  return None, error_msg
             else:
                  print(f"CalledProcessError on attempt {i+1}, trying next URL...")
                  continue

        except Exception as e:
            error_msg = f"Unexpected error during yt-dlp get-url (attempt {i+1}): {e}"
            print(error_msg)
            if i == len(attempts) - 1:
                 return None, error_msg
            else:
                 print(f"Unexpected error on attempt {i+1}, trying next URL...")
                 continue

    return None, "Unknown error during download URL extraction attempts."


def run_aria2c_and_report_progress(chat_id):
    """Runs aria2c and edits a Telegram message to show progress.
       This runs in a separate thread."""

    task_data = user_tasks.get(chat_id)
    if not task_data or task_data['state'] != 'downloading':
        print(f"Task data not found or state not downloading for chat {chat_id}")
        # File cleanup should ideally be handled by cancel_task if state is cancelled,
        # or by explicit error handling if process fails immediately before loop.
        # Let's add a final cleanup attempt here if task_data is unexpectedly gone.
        if task_data and task_data.get('file_path') and os.path.exists(task_data['file_path']):
             try: os.remove(task_data['file_path'])
             except Exception as e: print(f"Error deleting file {task_data['file_path']} on thread exit: {e}")
        user_tasks.pop(chat_id, None) # Ensure cleanup of task data
        return


    link = task_data['link']
    download_url = task_data['download_url']
    download_path = task_data['file_path']
    progress_msg_id = task_data['download_msg_id']
    original_filename = task_data['original_filename']
    original_message_id = task_data['original_message_id'] # User's original message


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
        '-c', # continue downloading
        '--no-conf',
    ]

    # Ensure the target directory for the temp file exists
    os.makedirs(os.path.dirname(download_path), exist_ok=True)

    process = None
    try:
        process = subprocess.Popen(aria2c_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        last_edit_time = time.time()
        initial_status_sent = False
        progress_pattern = re.compile(r'\[DL:\s+(\d+\.?\d*[KMGT]?i?B)/(\d+\.?\d*[KMGT]?i?B)\((\d+\.?\d*%)\)\s+CN:\s*\d+\s+ETA:\s*(\S+)\s+Speed:\s*(\S+)]')
        full_output = "" # Accumulate output for error reporting

        while True:
            # Read line from stdout (which includes stderr due to STDOUT)
            line = process.stdout.readline()
            if line:
                 full_output += line # Add to accumulator
                 line = line.strip()
            else:
                 if process.poll() is not None: # Process ended
                     break
                 time.sleep(0.1) # Avoid busy waiting

            if "download completed" in line.lower():
                 # Success indicator line, let the loop finish naturally or break?
                 # Let loop continue to drain pipe, then check return code
                 # Or force break, check if pipe needs draining manually?
                 # Safest is to let loop break when no more output and process is done.
                 pass # continue reading lines until process ends

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
            if chat_id in user_tasks and user_tasks[chat_id].get('state') == 'cancelled':
                 print(f"Task for chat {chat_id} cancelled during download. Terminating aria2c.")
                 if process:
                      try: process.terminate() # Use terminate() or kill()
                      except Exception: pass
                 break # Exit the reading loop


        # Wait for the process to actually finish
        if process and process.poll() is None: # If not already ended, wait
             try: process.wait(timeout=10) # Wait a bit longer for graceful exit
             except subprocess.TimeoutExpired:
                  print(f"aria2c process did not terminate within timeout, killing for chat {chat_id}.")
                  if process:
                       try: process.kill()
                       except Exception: pass

             # Capture any remaining output after waiting
             stdout, stderr = process.communicate()
             full_output += stdout if stdout else ""
             full_output += stderr if stderr else ""
             if stderr: print(f"aria2c stderr (after wait): {stderr.strip()}")
        elif process: # Process ended, but need to drain pipes? Communicate should handle this after wait
             stdout, stderr = process.communicate() # Drains and waits if necessary
             full_output += stdout if stdout else ""
             full_output += stderr if stderr else ""
             if stderr: print(f"aria2c stderr (communicate): {stderr.strip()}")


        # Re-check cancellation status *after* process finishes
        if chat_id in user_tasks and user_tasks[chat_id].get('state') == 'cancelled':
             print(f"Task for chat {chat_id} finished or was cancelled. Cleanup handled by cancel_task.")
             user_tasks.pop(chat_id, None) # Ensure state cleanup
             # File cleanup is scheduled by cancel_task or attempted in the finally block
             return # Exit the thread function


        # If process ended and state is NOT cancelled, evaluate result
        return_code = process.returncode if process else -1 # Assign -1 if process failed to start
        if return_code == 0 and os.path.exists(download_path):
            print(f"aria2c download completed successfully for chat {chat_id}. File exists at {download_path}")
            schedule_async_task(_edit_message(chat_id, progress_msg_id, f"✅ Download complete:\n`{original_filename}`"))

            if chat_id in user_tasks:
                 user_tasks[chat_id]['state'] = 'waiting_action'
                 action_markup = InlineKeyboardMarkup([
                     [InlineKeyboardButton("ضغط الفيديو", callback_data=f"compress_{chat_id}"),
                      InlineKeyboardButton("رفع بدون ضغط", callback_data=f"upload_raw_{chat_id}")]
                 ])
                 # Reply to the original message for context
                 schedule_async_task(_send_message(
                      chat_id,
                      "📥 تم التنزيل. ماذا تود أن تفعل؟",
                      reply_markup=action_markup,
                      reply_to_message_id=original_message_id
                  ))

                 schedule_async_task(_delete_messages(chat_id, progress_msg_id))


        else: # Download failed or file not found after success code
            print(f"aria2c download failed for chat {chat_id}. Return code: {return_code}")
            error_output_snip = full_output[-1000:] if full_output else f"Process exited with code {return_code}" # Last 1000 chars
            error_msg = f"❌ فشل التنزيل:\n`aria2c exited with code {return_code}`\nDetails:\n`{error_output_snip}`"
            schedule_async_task(_send_message(chat_id, error_msg, reply_to_message_id=original_message_id))
            schedule_async_task(_delete_messages(chat_id, progress_msg_id))
            # Clean up data and file via cancel_task logic
            cancel_task(chat_id, user_cancelled=False) # Call cancel_task for cleanup

    except FileNotFoundError:
        print("Error: aria2c not found.")
        schedule_async_task(_send_message(chat_id, "❌ فشل التنزيل: أداة `aria2c` غير موجودة على الخادم.", reply_to_message_id=original_message_id))
        schedule_async_task(_delete_messages(chat_id, progress_msg_id))
        cancel_task(chat_id, user_cancelled=False) # Clean task data and state

    except Exception as e:
        print(f"An error occurred during aria2c execution for chat {chat_id}: {e}")
        error_msg = f"❌ فشل التنزيل بسبب خطأ غير متوقع: {e}"
        schedule_async_task(_send_message(chat_id, error_msg, reply_to_message_id=original_message_id))
        schedule_async_task(_delete_messages(chat_id, progress_msg_id))
        cancel_task(chat_id, user_cancelled=False) # Clean task data and file

    # The finally block for aria2c thread might not be strictly needed
    # because cancel_task or explicit error handling pops the user_tasks and schedules file deletion.
    # Let's keep it simple and rely on the handlers/cancel to trigger file cleanup.
    # The processing_compression_queue's finally block does file cleanup after getting the task from queue.
    pass # No finally block here, cleanup handled by called functions


def calculate_bitrate(target_mb, duration_seconds):
    """Calculates the required video bitrate in kb/s for a target size."""
    if duration_seconds <= 0 or target_mb <= 0:
        return 1000 # Default if duration/target is zero or negative

    # Target total size in bits
    total_target_bits = target_mb * 1024 * 1024 * 8

    try:
        # Convert audio bitrate from string (e.g., '128k') to bits per second
        audio_bitrate_str = str(VIDEO_AUDIO_BITRATE).lower()
        if audio_bitrate_str.endswith('k'):
             audio_bitrate_bps = int(audio_bitrate_str[:-1]) * 1000
        elif audio_bitrate_str.endswith('m'):
             audio_bitrate_bps = int(audio_bitrate_str[:-1]) * 1000000
        else:
             audio_bitrate_bps = int(audio_bitrate_str) # Assume bps if no suffix? Or assume kbps? Let's assume kbps if no suffix for simplicity
             if audio_bitrate_bps < 1000: audio_bitrate_bps *= 1000 # Assume small numbers were meant to be kbps


    except (ValueError, TypeError):
        print(f"Warning: Invalid AUDIO_BITRATE config: {VIDEO_AUDIO_BITRATE}. Using default 128kbps.")
        audio_bitrate_bps = 128000 # Default if config is weird/invalid

    # Estimate total bits for audio
    audio_total_bits = audio_bitrate_bps * duration_seconds

    # Required bits for video
    video_target_bits = total_target_bits - audio_total_bits

    # Ensure video target bits is not negative (meaning target size is too small for audio+min video bitrate)
    if video_target_bits < duration_seconds * 500000: # Ensure minimum 500 kbps for video
        print(f"Warning: Target size {target_mb}MB is too small for duration {duration_seconds}s with audio {VIDEO_AUDIO_BITRATE}. Adjusting video bitrate to minimum 500kbps.")
        video_bitrate_bps = 500000 # Minimum 500 kbps
    else:
         video_bitrate_bps = video_target_bits / duration_seconds

    # Convert video bitrate back to kb/s for ffmpeg -b:v option
    video_bitrate_kbps = int(video_bitrate_bps / 1000)

    return video_bitrate_kbps


def generate_ffmpeg_command(input_path, output_path, bitrate_kbps):
    """Generates the ffmpeg command for compression (using libx264 - CPU based)."""
    # Basic command structure, add audio settings from config
    ffmpeg_command = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-c:v', 'libx264', # Use libx264 for CPU-based H.264 encoding
        '-b:v', f'{bitrate_kbps}k', # Video bitrate in kb/s
        '-preset', 'medium', # x264 preset: balance between speed and quality
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
    # Note: To implement NVENC, you would swap '-c:v libx264' and '-preset medium' with
    # '-hwaccel cuda' and '-c:v h264_nvenc -preset medium' (NVENC has its own presets).
    # Fallback logic would be needed if CUDA/NVENC is not available.
    return ffmpeg_command

def process_compression_queue():
    """Thread worker function to process compression tasks sequentially.
       Sends the compressed video back to the user's chat."""
    print("Compression processing thread started and waiting for tasks.")

    while True:
        task = compression_queue.get() # Blocks until a task is available

        chat_id = task['chat_id'] # This is the user's chat_id
        input_file = task['input_file']
        duration = task['duration']
        target_size_mb = task['target_size_mb']
        status_msg_id = task['status_msg_id'] # User's status message ID
        original_filename = task['original_filename']
        original_message_id = task['original_message_id'] # User's original message ID


        print(f"Processing compression task for chat {chat_id}. Target size: {target_size_mb}MB")

        # Re-check if task was cancelled while waiting in queue or processing
        if chat_id not in user_tasks or user_tasks[chat_id].get('state') == 'cancelled':
             print(f"Task for chat {chat_id} was cancelled, skipping processing.")
             # Input file cleanup will happen in the finally block.
             compression_queue.task_done()
             continue

        # Update state in user_tasks (should be already 'queuing' or similar)
        # State will be updated to 'compressing' in the try block
        if chat_id in user_tasks: user_tasks[chat_id]['state'] = 'compressing'
        else: # Task data missing unexpectedly - clean up
             print(f"Task data missing for chat {chat_id} in compression thread. Cleaning up input file {input_file}...")
             if input_file and os.path.exists(input_file):
                 try: os.remove(input_file)
                 except Exception as e: print(f"Error deleting input file {input_file}: {e}")
             compression_queue.task_done()
             continue # Skip this task


        compressed_file_path = None
        ffmpeg_process = None
        try:
            bitrate_kbps = calculate_bitrate(target_size_mb, duration)
            print(f"Calculated video bitrate: {bitrate_kbps} kb/s")

            # Create output file path in DOWNLOADS_DIR with a unique name
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

            # Run FFmpeg subprocess - blocking call in this thread
            ffmpeg_process = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True, timeout=duration * 10)

            print("FFmpeg command executed successfully.")

            # --- Upload compressed video BACK TO THE USER'S CHAT ---
            # Send status message for upload
            schedule_async_task(_send_message(chat_id, "⬆️ جاري رفع الفيديو المضغوط...", reply_to_message_id=original_message_id))

            try:
                # Send the compressed document using the helper coroutine
                schedule_async_task(
                     _send_document(
                        chat_id=chat_id, # Use the user's chat_id
                        document=compressed_file_path,
                        caption=f"Compressed to ~{target_size_mb}MB",
                        file_name=original_filename if original_filename.lower().endswith('.mp4') else f"{os.path.splitext(original_filename)[0]}.mp4", # Ensure .mp4 extension, use original name base
                        reply_to_message_id=original_message_id
                     )
                )
                print(f"Compressed video upload scheduled to user chat: {chat_id}")
                schedule_async_task(_edit_message(chat_id, status_msg_id, f"✅ تم الضغط وإرسال الفيديو بنجاح! الحجم المستهدف: {target_size_mb}MB"))

            except Exception as e:
                print(f"Error scheduling compressed video upload to user chat {chat_id}: {e}")
                schedule_async_task(_edit_message(chat_id, status_msg_id, f"❌ تم الضغط، ولكن فشل إرسال الفيديو إليك: {e}"))


        except FileNotFoundError:
            print("Error: ffmpeg not found (or libx264 not supported in this build).")
            error_text = "❌ فشل الضغط: أداة `ffmpeg` غير موجودة أو لا تدعم الترميز المطلوب (libx264)."
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
            # Clean up temp files related to this task (input file and output file)
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
                    print(f"Error deleting compressed temp file: {e}")

            # Clean up user task data regardless of outcome (success/failure)
            # Only pop if the task state is not marked cancelled by the main handler (e.g., user pressed cancel after size)
            # We need to be careful here. If cancel_task happens WHILE this thread is running, it sets state to 'cancelled' and pops from user_tasks.
            # If we pop here *after* the thread finishes (and the task data might have been popped by cancel_task already), pop(chat_id, None) is safe.
            # Let's keep the pop here as the primary place to remove task data *after* processing finishes in the thread.
            # Re-checking the state isn't strictly necessary for popping, but the print helps.
            current_task_state = user_tasks.get(chat_id, {}).get('state')
            if current_task_state != 'cancelled': # Only print removed if not cancelled state (cancel prints its own message)
                 print(f"Compression task finished for chat {chat_id}. User data removed.")
            else:
                 print(f"Compression task finished for chat {chat_id} (was cancelled).") # Data already removed by cancel_task

            user_tasks.pop(chat_id, None) # Safely remove task data if still present

            compression_queue.task_done() # Indicate task is done for the queue
            print(f"Queue size remaining: {compression_queue.qsize()}")


def cancel_task(chat_id, user_cancelled=True):
    """Cancels the current task for a user, cleans up resources."""
    print(f"Attempting to cancel task for chat {chat_id}, user_cancelled: {user_cancelled}")
    task_data = user_tasks.get(chat_id)

    if task_data:
        current_state = task_data.get('state', 'unknown')
        print(f"Cancelling task with state: {current_state} for chat {chat_id}")
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

        # File cleanup: Mark for deletion, threads should handle in their finally.
        # Adding a delayed cleanup as a fallback is a good idea.
        file_path = task_data.get('file_path')
        compressed_file_path = None # If compression started, the output path might exist
        if current_state == 'compressing':
             # Try to guess the compressed file name pattern
             base_output_name = f"{chat_id}_{task_data.get('original_message_id', 'unknown')}_{task_data.get('target_size_mb', 'unknown')}MB"
             safe_original_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', task_data.get('original_filename', 'unknown'))
             safe_original_part = safe_original_part[:30]
             if safe_original_part: base_output_name = f"{base_output_name}_{safe_original_part}"
             compressed_file_path = os.path.join(DOWNLOADS_DIR, f"{base_output_name}_compressed.mp4")


        def delayed_file_delete(paths, delay=5): # Take a list of paths
                print(f"Scheduled deletion of files {paths} in {delay} seconds due to cancellation.")
                time.sleep(delay)
                for path in paths:
                    if path and os.path.exists(path):
                        try:
                            os.remove(path)
                            print(f"Deleted temp file {path} during cancellation cleanup.")
                        except Exception as e:
                            print(f"Error deleting temp file {path} during cancellation cleanup: {e}")

        files_to_clean_async = [file_path, compressed_file_path]
        # Filter out None or empty paths
        files_to_clean_async = [p for p in files_to_clean_async if p and os.path.exists(p)]

        if files_to_clean_async:
            threading.Thread(target=delayed_file_delete, args=(files_to_clean_async,), daemon=True).start()


        # Remove the task data from the global dictionary *after* scheduling async cleanup
        user_tasks.pop(chat_id, None)
        print(f"Task data for chat {chat_id} removed from user_tasks.")

    else:
        if user_cancelled:
             print(f"Cancel requested for chat {chat_id} but no task found.")
             # schedule_async_task(_send_message(chat_id, "لا يوجد عملية جارية لإلغائها."))


# --- Pyrogram Handlers (async def) ---

# Initialize the Bot Client
app = Client(
    "video_compressor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=API_TOKEN,
    workdir=SESSION_DIR # Use the defined SESSION_DIR variable
)

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    """Handles the /start command."""
    # Cancel any ongoing task for this user before starting a new one
    cancel_task(message.chat.id)
    await message.reply_text("👋 مرحباً! أنا بوت لضغط الفيديو. أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>` أو `https://t.me/c/<آيدي_القناة>/<رقم_الرسالة>`.\n\nميزة رفع الفيديو المباشر قيد التطوير حالياً. يرجى استخدام روابط تيليجرام حالياً.\n\nلتثبيت الادوات في Google Colab انسخ السطر التالي في الخليه الأولى:\n`!pip install -U yt-dlp aria2 pyrogram ffmpeg-python && apt-get update && apt-get install -y aria2 ffmpeg`\n**ملاحظة:** الضغط حالياً يعتمد على الـ CPU (أداة libx264).") # Updated message


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client: Client, message: Message):
    """Handles the /cancel command."""
    chat_id = message.chat.id
    if chat_id in user_tasks:
        cancel_task(chat_id, user_cancelled=True)
        # Message sending is handled inside cancel_task
    else:
        await message.reply_text("لا يوجد عملية جارية لإلغائها.", quote=True)


@app.on_message((filters.text | filters.video | filters.animation) & filters.private)
async def handle_message(client: Client, message: Message):
    """Handles incoming text messages or direct videos."""
    chat_id = message.chat.id
    text = message.text.strip() if message.text else None
    original_message_id = message.id
    original_message_chat_id = chat_id # Redundant, but useful for clarity


    # --- Handle target size input (if state is waiting_size) ---
    # Check if the message is a reply to the bot's 'waiting_size' message
    if chat_id in user_tasks and user_tasks[chat_id]['state'] == 'waiting_size' and text:
        try:
            target_size_mb = int(text)
            if target_size_mb <= 0:
                 await message.reply_text("حجم الفيديو يجب أن يكون رقماً موجباً. يرجى إرسال الرقم مرة أخرى.", quote=True)
                 return

            task_data = user_tasks.get(chat_id) # Use get for safety
            # Ensure essential data exists from previous step
            if not task_data or not task_data.get('file_path') or not task_data.get('duration'):
                print(f"Error: Missing task data for chat {chat_id} in waiting_size state.")
                await message.reply_text("حدث خطأ في بيانات المهمة. يرجى إرسال الرابط مرة أخرى.", quote=True)
                cancel_task(chat_id, user_cancelled=False) # Clean up state
                return

            # Reset state immediately
            user_tasks[chat_id]['state'] = 'queuing' # New temporary state


            # Queue the compression task - Create status message first
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
                'original_message_chat_id': task_data['original_message_chat_id'],
                'original_message_id': task_data['original_message_id'],
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
            cancel_task(chat_id, user_cancelled=False)


    # --- Handle new link ---
    elif text and text.startswith("https://t.me/"):
        # Cancel any stale state for this user just in case
        cancel_task(chat_id)

        parse_result = parse_telegram_link(text)
        if not parse_result:
            await message.reply_text("صيغة الرابط غير صحيحة. يرجى إرسال رابط بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>` أو `https://t.me/c/<آيدي_القناة>/<رقم_الرسالة>`", quote=True)
            return

        channel_identifier, message_id_str = parse_result
        print(f"Received Telegram link: Channel={channel_identifier}, Message ID={message_id_str}")

        try:
            # Send an initial processing message
            process_msg = await message.reply_text("🔍 جارٍ استخلاص معلومات الفيديو...", quote=True)

            # --- Step 2a: Get Metadata (Duration, Original Filename) ---
            # Synchronous call
            duration, original_filename, metadata_error = get_video_metadata(text) # Pass original text link
            if metadata_error:
                 await process_msg.edit_text(f"❌ فشل استخلاص معلومات الفيديو:\n`{metadata_error}`\n\nيرجى التأكد من صحة الرابط وأن القناة عامة وليست خاصة.")
                 return

            if duration is None or duration <= 0:
                 await process_msg.edit_text(f"⚠️ فشل استخلاص مدة الفيديو أو المدة صفر. لا يمكن المتابعة.\n\nيرجى التأكد من أن الرابط يحتوي على فيديو صالح.")
                 return

            await process_msg.edit_text("✅ تم استخلاص المعلومات بنجاح. جارٍ الحصول على رابط التنزيل...")

            # --- Step 2b: Get Direct Download URL ---
            # Synchronous call
            download_url, url_error = get_download_url_with_yt_dlp(text) # Pass original text link
            if url_error:
                await process_msg.edit_text(f"❌ فشل الحصول على رابط التنزيل المباشر:\n`{url_error}`\n\nيرجى التأكد من أن الفيديو متاح للتنزيل عبر yt-dlp.")
                return

            await process_msg.edit_text("✅ تم استخلاص الرابط. جارٍ التحضير للتنزيل...")

            # --- Step 2c: Setup for Download using aria2c ---
            # Generate a unique temporary file path based on chat and message ID
            # Try to get extension from original_filename returned by yt-dlp metadata
            file_extension = os.path.splitext(original_filename)[1] if os.path.splitext(original_filename)[1] else ".bin" # Default to .bin

            base_download_name = f"{chat_id}_{original_message_id}"
            safe_original_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', os.path.splitext(original_filename)[0]) # Use base name part
            safe_original_part = safe_original_part[:30] # Limit length
            if safe_original_part:
                base_download_name = f"{base_download_name}_{safe_original_part}"

            temp_output_file = os.path.join(DOWNLOADS_DIR, f"{base_download_name}_temp_download{file_extension}")


            # Store task data *before* starting the thread
            user_tasks[chat_id] = {
                'state': 'downloading',
                'link': text, # Original link
                'download_url': download_url,
                'file_path': temp_output_file,
                'duration': duration,
                'original_filename': original_filename,
                'download_msg_id': process_msg.id,
                'action_msg_id': None,
                'status_msg_id': None, # For compression status later
                'original_message_chat_id': chat_id, # Store original chat/message IDs
                'original_message_id': original_message_id,
            }

            # Edit message to indicate downloading state
            await process_msg.edit_text(f"⬇️ جارٍ بدء التنزيل (`{original_filename}`)...")


            # Run aria2c in a separate thread
            download_thread = threading.Thread(
                target=run_aria2c_and_report_progress,
                args=(chat_id,), # Pass chat_id only, thread reads task_data
                daemon=True
            )
            download_thread.start()


        except Exception as e:
            print(f"Error handling link {text} for chat {chat_id}: {e}")
            # General unexpected error during initial phase
            if chat_id in user_tasks:
                 # If task data was created, use cancel_task for cleanup
                 cancel_task(chat_id, user_cancelled=False)
            else:
                # If task data wasn't even created, just report error
                await message.reply_text(f"حدث خطأ غير متوقع أثناء المعالجة: {e}", quote=True)
            # Attempt to delete initial message if it exists
            if 'process_msg' in locals() and process_msg.id:
                 try: await process_msg.delete()
                 except Exception: pass


    # --- Handle direct video or animation upload ---
    elif message.video or message.animation:
         # This section is still a placeholder based on the requirement
         # Implementing this requires downloading the file via Pyrogram
         # and then proceeding with the waiting_action state.
         await message.reply_text("ميزة رفع الفيديو المباشر قيد التطوير حالياً. يرجى استخدام روابط تيليجرام حالياً.", quote=True)
         # To implement:
         # 1. client.download_media() - may need a thread for large files
         # 2. Get duration, filename from message.video/animation object
         # 3. Create and store task data in user_tasks with state 'downloading'
         # 4. Upon download completion (in the download thread or after await):
         #    - Get file_path of downloaded file
         #    - Update state to 'waiting_action'
         #    - Send action inline keyboard
         #    - Store action message ID
         # 5. Callbacks will then process as usual.


    # --- Handle any other text input when not waiting for size ---
    elif text: # Only if it was a text message that didn't match other conditions
        await message.reply_text("أرسل لي رابط فيديو من قناة تيليجرام عامة بالصيغة التالية:\n`https://t.me/<اسم_القناة>/<رقم_الرسالة>` أو `https://t.me/c/<آيدي_القناة>/<رقم_الرسالة>`.\n\nميزة رفع الفيديو المباشر قيد التطوير حالياً.", quote=True)


@app.on_callback_query()
async def handle_callback(client: Client, callback_query):
    """Handles inline keyboard button presses."""
    data = callback_query.data
    chat_id = callback_query.message.chat.id # User's chat_id
    message_id = callback_query.message.id # ID of the action message

    # Ensure the callback is for a known, active task and action message
    if chat_id not in user_tasks or user_tasks[chat_id].get('state') != 'waiting_action':
        print(f"Callback received for unknown/stale task: {data} from chat {chat_id}, message {message_id}")
        await callback_query.answer("انتهت صلاحية هذا الطلب أو تم معالجته مسبقاً.", show_alert=True)
        try: await callback_query.message.delete()
        except Exception: pass
        return

    await callback_query.answer() # Answer the callback immediately

    task_data = user_tasks[chat_id]
    file_path = task_data['file_path']
    duration = task_data['duration'] # Needed for compress option
    original_filename = task_data['original_filename']
    original_message_id = task_data['original_message_id']


    # Delete the action message after processing the choice
    try:
        await callback_query.message.delete()
    except Exception as e:
        print(f"Error deleting action message {message_id}: {e}")


    # --- Handle "Upload Raw" ---
    if data.startswith("upload_raw_"):
        print(f"Uploading raw video back to user chat {chat_id}")
        user_tasks[chat_id]['state'] = 'uploading_raw'

        if not file_path or not os.path.exists(file_path):
            await client.send_message(chat_id, "❌ خطأ: لم يتم العثور على الملف الأصلي للرفع.", reply_to_message_id=original_message_id)
            cancel_task(chat_id, user_cancelled=False) # Clean task data
            return

        try:
            upload_status_msg = await client.send_message(chat_id, "⬆️ جارٍ رفع الفيديو الأصلي...", reply_to_message_id=original_message_id)

            # Use user's chat_id for sending document
            await client.send_document(
                chat_id=chat_id, # Send back to the user's chat
                document=file_path,
                file_name=original_filename, # Use the filename from yt-dlp metadata
                reply_to_message_id=original_message_id
            )
            await upload_status_msg.edit_text("✅ تم رفع الفيديو الأصلي بنجاح!")
            print(f"Raw video uploaded successfully to user chat: {chat_id}")

        except Exception as e:
            print(f"Error uploading raw video to user chat {chat_id}: {e}")
            await client.send_message(chat_id, f"❌ فشل رفع الفيديو الأصلي إليك: {e}", reply_to_message_id=original_message_id)

        finally:
            # Clean up temp file
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    print(f"Deleted raw temp file after upload: {file_path}")
                except Exception as e:
                    print(f"Error deleting raw temp file {file_path}: {e}")

            # Remove task data
            user_tasks.pop(chat_id, None)


    # --- Handle "Compress" ---
    elif data.startswith("compress_"):
        print(f"Compressing video requested for chat {chat_id}")
        # Ensure file exists before proceeding to compression steps
        if not file_path or not os.path.exists(file_path):
            await client.send_message(chat_id, "❌ خطأ: لم يتم العثور على الملف الأصلي للضغط.", reply_to_message_id=original_message_id)
            cancel_task(chat_id, user_cancelled=False) # Clean task data
            return

        user_tasks[chat_id]['state'] = 'waiting_size'
        user_tasks[chat_id]['duration'] = duration # Ensure duration is stored for compress
        user_tasks[chat_id]['original_filename'] = original_filename # Ensure filename is stored

        await client.send_message(
            chat_id,
            "كم ميجابايت تود أن يكون حجم الفيديو المضغوط؟ أرسل **الرقم فقط** (مثال: `50`)",
            reply_to_message_id=original_message_id
        )

# --- Main Execution ---

if __name__ == "__main__":
    print("Bot starting...")
    cleanup_downloads() # Clean up temp files on startup

    # Session directory handled before client initialization


    print("Starting Pyrogram client...")

    # Start the compression processing thread on startup, make it daemon
    # It will wait for tasks in the queue forever
    processing_thread = threading.Thread(target=process_compression_queue, daemon=True)
    processing_thread.start()

    # Pyrogram client needs to run the main event loop
    app.run() # This is a blocking call that starts the async event loop

    print("Bot stopped.")
