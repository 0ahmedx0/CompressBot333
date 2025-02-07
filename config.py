# BOT Credentials
API_ID = 23151406
API_HASH = "0893a87614fae057c8efe7b85114f45a"
API_TOKEN = "7535942426:AAEq8EiNE4PcMvTFn65k17YjPC5_d-RedDQ"

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
VIDEO_CRF = 23  # الحفاظ على قيمة CRF لتوازن جيد بين الحجم والجودة
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
