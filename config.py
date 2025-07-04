# config.py

from os import getenv

# BOT Credentials
# تأكد من تعيين هذه المتغيرات كمتغيرات بيئية في نظام التشغيل أو بيئة التشغيل (مثل Colab).
# مثال في Bash: export API_ID=123456789 export API_HASH="abcdef123456" ...
# مثال في Colab cell:
# %env API_ID=123456789
# %env API_HASH=abcdef123456
# ...etc

API_ID = int(getenv("API_ID", "0"))  # تحويل API_ID إلى integer. استخدام قيمة افتراضية 0 إذا لم يُعثر عليها.
API_HASH = getenv("API_HASH")
API_TOKEN = getenv("API_TOKEN")
CHANNEL_ID_STR = getenv("CHANNEL_ID") # قراءة كـ String أولاً

# التحقق من القيم وتهيئة CHANNEL_ID كـ integer
if not API_ID or not API_HASH or not API_TOKEN or not CHANNEL_ID_STR:
    # يمكنك رفع خطأ هنا أو طباعة رسالة والتعامل مع النقص في مكان آخر
    # رفع خطأ هو أفضل لإيقاف البوت فوراً في حال وجود إعدادات ناقصة
    raise ValueError("Please set API_ID, API_HASH, API_TOKEN, and CHANNEL_ID environment variables.")

try:
    CHANNEL_ID = int(CHANNEL_ID_STR) # تحويل CHANNEL_ID (الذي يبدأ بـ -100) إلى integer
    # يمكن إضافة فحص هنا للتأكد من أنه يبدأ بـ -100 إذا كان إلزاميًا
    # if not str(CHANNEL_ID_STR).startswith('-100'):
    #     raise ValueError("CHANNEL_ID must be a valid Telegram channel ID starting with -100")

except ValueError:
     raise ValueError("CHANNEL_ID environment variable must be a valid integer string (e.g., '-1001234567890').")


# Audio compression settings (these seem unused in the main logic so far,
# except maybe for calculating bitrate in the function)
# VIDEO_AUDIO_CODEC, VIDEO_AUDIO_BITRATE, VIDEO_AUDIO_CHANNELS, VIDEO_AUDIO_SAMPLE_RATE are used
# The below are specific "Audio compression settings" but the video logic uses separate ones
# If you plan to add audio-only processing, these would be used there.
# AUDIO_BITRATE = "32k"
# AUDIO_FORMAT = "mp3"
# AUDIO_CHANNELS = 1
# AUDIO_SAMPLE_RATE = 44100

# Video compression settings (These seem to be configuration examples rather than actual settings used
# directly in generate_ffmpeg_command except for audio parts)
# The generate_ffmpeg_command function constructs the command based on calculated bitrate and
# VIDEO_AUDIO_* settings. Keep these definitions if they are just reference or potentially
# used elsewhere, but be aware they are not currently building the core FFmpeg command
# being used for compression (that one is built in the function using dynamic bitrate).
# VIDEO_SCALE = "iw:ih"
# VIDEO_FPS = 30
# VIDEO_CODEC = "h264_nvenc" # This was replaced by libx264 for debugging
# VIDEO_BITRATE = "1500k"    # This is fixed, but compression uses calculated bitrate
# VIDEO_CRF = 23             # CRF is for variable quality, we are using fixed bitrate (-b:v) now
# VIDEO_PRESET = "medium"
# VIDEO_PIXEL_FORMAT = "yuv420p"
# VIDEO_PROFILE = "high" # Used in libx264 command


# Video and Audio settings ACTUALLY USED in generate_ffmpeg_command (from original code):
VIDEO_AUDIO_CODEC = getenv("VIDEO_AUDIO_CODEC", "aac") # Default if not set
VIDEO_AUDIO_BITRATE = getenv("VIDEO_AUDIO_BITRATE", "128k") # Default if not set
VIDEO_AUDIO_CHANNELS = int(getenv("VIDEO_AUDIO_CHANNELS", "2")) # Default if not set
VIDEO_AUDIO_SAMPLE_RATE = int(getenv("VIDEO_AUDIO_SAMPLE_RATE", "48000")) # Default if not set

# Temporary file settings (These also don't seem directly used in the current version
# as tempfile.NamedTemporaryFile or os.path.join construct paths with dynamic names/extensions)
# TEMP_FILE_SUFFIX_AUDIO = ".mp3"
# TEMP_FILE_SUFFIX_VIDEO = ".mp4"


# --- Optional: Print loaded settings for verification ---
print("\n--- Loaded Config ---")
print("API_ID:", API_ID)
print("API_HASH:", API_HASH)
print("API_TOKEN:", API_TOKEN) # Be careful printing sensitive tokens in production
print("CHANNEL_ID:", CHANNEL_ID)
print("VIDEO_AUDIO_CODEC:", VIDEO_AUDIO_CODEC)
print("VIDEO_AUDIO_BITRATE:", VIDEO_AUDIO_BITRATE)
print("VIDEO_AUDIO_CHANNELS:", VIDEO_AUDIO_CHANNELS)
print("VIDEO_AUDIO_SAMPLE_RATE:", VIDEO_AUDIO_SAMPLE_RATE)
print("--- End Config ---")
