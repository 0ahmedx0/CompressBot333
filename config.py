# BOT Credentials

from os import getenv
import os 
API_ID = int(os.getenv("API_ID"))  # تحويل API_ID إلى integer
API_HASH = os.getenv("API_HASH")
API_TOKEN = os.getenv("API_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID")) # تحويل CHANNEL_ID إلى integer لأنه رقم

if not API_ID or not API_HASH or not API_TOKEN or not CHANNEL_ID:
    raise ValueError("Please set API_ID, API_HASH, API_TOKEN, and CHANNEL_ID environment variables.")

# Audio compression settings
AUDIO_BITRATE = "32k"  
AUDIO_FORMAT = "mp3" 
AUDIO_CHANNELS = 1     
AUDIO_SAMPLE_RATE = 44100  

# Video compression settings
VIDEO_SCALE = "iw:ih"  # الحفاظ على الأبعاد الأصلية للفيديو
VIDEO_FPS = 30  # الحفاظ على معدل الإطارات كما هو
VIDEO_CODEC = "h264_nvenc"  # استخدام ترميز h264_nvenc لتسريع الضغط باستخدام المعالج الرسومي
VIDEO_BITRATE = "1500k"  # تعيين معدل البت للضغط
VIDEO_CRF = 23 # الحفاظ على قيمة CRF لتوازن جيد بين الحجم والجودة
VIDEO_PRESET = "medium"  # استخدام إعداد "medium" للحصول على توازن بين السرعة والجودة
VIDEO_PIXEL_FORMAT = "yuv420p"  # الحفاظ على تنسيق البيكسل المناسب
VIDEO_PROFILE = "high"  # الحفاظ على إعدادات "high" لجودة الفيديو
VIDEO_AUDIO_CODEC = "aac"  # استخدام ترميز الصوت aac
VIDEO_AUDIO_BITRATE = "128k"  # تعيين معدل البت الصوتي
VIDEO_AUDIO_CHANNELS = 2  # الحفاظ على قنوات الصوت
VIDEO_AUDIO_SAMPLE_RATE = 48000  # الحفاظ على معدل العينة الصوتي
# Temporary file settings
TEMP_FILE_SUFFIX_AUDIO = ".mp3"  
TEMP_FILE_SUFFIX_VIDEO = ".mp4"  
