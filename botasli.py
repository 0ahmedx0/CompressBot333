# ... (باقي الكود كما هو) ...

cleanup_downloads()
load_preferences() # *** أضف هذا السطر لضمان تحميل التفضيلات عند بدء البوت ***

def check_channel_on_start():
    # الانتظار لبضع ثوانٍ للتأكد من بدء تشغيل البوت
    time.sleep(5)
    if CHANNEL_ID:
        try:
            chat = app.get_chat(CHANNEL_ID)
            print(f"✅ تم التعرف على القناة بنجاح: '{chat.title}' (ID: {CHANNEL_ID})")
            if chat.type not in ["channel", "supergroup"]:
                print("⚠️ ملاحظة: ID القناة المحدد ليس لقناة أو مجموعة خارقة.")
            # Check bot's admin status and permissions if needed
            # You might need to check if the bot is an admin with `can_post_messages`
            # app.get_chat_member(CHANNEL_ID, app.get_me().id)
        except Exception as e:
            print(f"❌ خطأ في التعرف على القناة '{CHANNEL_ID}': {e}. يرجى التأكد من أن البوت مشرف في القناة وأن ID صحيح.")
    else:
        print("⚠️ لم يتم تحديد CHANNEL_ID في ملف config.py. لن يتم رفع الفيديوهات إلى قناة إلا إذا تم اختيار المحادثة الخاصة.")


threading.Thread(target=check_channel_on_start, daemon=True, name="ChannelCheckThread").start()

print("🚀 البوت بدأ العمل! بانتظار الفيديوهات...")
app.run()
