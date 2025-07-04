# الخلية الثانية في دفتر Colab
# يجب أن تكون هذه الخلية بعد الخلية التي تعيّن المتغيرات البيئية

# محتوى ملف config.py
from os import getenv
import os

# BOT Credentials
API_ID = int(getenv("API_ID", "0"))
API_HASH = getenv("API_HASH")
API_TOKEN = getenv("API_TOKEN")
CHANNEL_ID_STR = getenv("CHANNEL_ID") # اقرأ CHANNEL_ID كنص أولاً

# التحقق من القيم الأساسية وتهيئة CHANNEL_ID كـ integer
if not API_ID or not API_HASH or not API_TOKEN or not CHANNEL_ID_STR:
    # هذا الفحص يجب أن ينجح الآن لأن المتغيرات تم تعيينها في الخلية السابقة
    raise ValueError("Please set API_ID, API_HASH, API_TOKEN, and CHANNEL_ID environment variables correctly.")

try:
    # الآن حوّل CHANNEL_ID إلى عدد صحيح. يجب أن تكون القيمة -100... كنص لتنجح هنا.
    CHANNEL_ID = int(CHANNEL_ID_STR)
    # يمكنك إضافة فحص إضافي للتحقق من تنسيق -100... إذا أردت المزيد من الصرامة
    if not CHANNEL_ID_STR.startswith('-100') and CHANNEL_ID != 0: # CHANNEL_ID can be 0 if not set, depends on usage
         print(f"Warning: CHANNEL_ID '{CHANNEL_ID_STR}' does not start with '-100'. Ensure this is correct.")


except ValueError:
     # هذا الخطأ سيحدث إذا كانت قيمة CHANNEL_ID التي عينتها في الخلية الأولى ليست رقمًا صالحًا
     raise ValueError(f"CHANNEL_ID environment variable must be a valid integer string (e.g., '-1001234567890'), received '{CHANNEL_ID_STR}'.")


# Video and Audio settings USED in generate_ffmpeg_command
# يمكنك أيضًا قراءتها من البيئة أو تعريفها مباشرة هنا
VIDEO_AUDIO_CODEC = getenv("VIDEO_AUDIO_CODEC", "aac") # Default if not set
VIDEO_AUDIO_BITRATE = getenv("VIDEO_AUDIO_BITRATE", "128k") # Default if not set
VIDEO_AUDIO_CHANNELS = int(getenv("VIDEO_AUDIO_CHANNELS", "2")) # Default if not set
VIDEO_AUDIO_SAMPLE_RATE = int(getenv("VIDEO_AUDIO_SAMPLE_RATE", "48000")) # Default if not set


# --- Optional: Print loaded settings for verification ---
print("\n--- Loaded Config ---")
print("API_ID:", API_ID)
print("API_HASH:", API_HASH)
# print("API_TOKEN:", API_TOKEN) # كن حذرًا عند طباعة التوكن
print("CHANNEL_ID:", CHANNEL_ID)
print("VIDEO_AUDIO_CODEC:", VIDEO_AUDIO_CODEC)
print("VIDEO_AUDIO_BITRATE:", VIDEO_AUDIO_BITRATE)
print("VIDEO_AUDIO_CHANNELS:", VIDEO_AUDIO_CHANNELS)
print("VIDEO_AUDIO_SAMPLE_RATE:", VIDEO_AUDIO_SAMPLE_RATE)
print("--- End Config ---")

# ملاحظة: الآن المتغيرات API_ID, API_HASH, ..., CHANNEL_ID متاحة في هذا النطاق (scope).
# في كود البوت الرئيسي، إذا كان في ملف منفصل، ستحتاج إلى `from config import *`.
# إذا كان الكود كاملاً في خلية واحدة، ستكون المتغيرات معرفة وجاهزة للاستخدام.
