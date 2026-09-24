import os
import threading
import requests

from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types


# =========================================================
# الإعدادات
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")


# =========================================================
# Gemini
# =========================================================

client = genai.Client(api_key=GEMINI_API_KEY)


# الموديلات بالترتيب
MODEL_PRIORITY = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]


current_model = MODEL_PRIORITY[0]

# محادثات المستخدمين
user_chats = {}


# =========================================================
# Flask - حتى Render يعتبر الخدمة شغالة
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Bot is running!"


def run_web_server():
    port = int(os.environ.get("PORT", 10000))

    web_app.run(
        host="0.0.0.0",
        port=port
    )


# =========================================================
# أزرار Telegram
# =========================================================

def main_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🟢 دردشة مع Gemini",
                callback_data="gemini_chat"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 تحليل الذهب",
                callback_data="gold_analysis"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


def back_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🔙 رجوع للقائمة",
                callback_data="back_menu"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# الحصول على موديلات Gemini المتاحة
# =========================================================

def get_available_models():

    try:

        models = client.models.list()

        available = []

        for model in models:

            name = getattr(model, "name", "")

            if not name:
                continue

            if name.startswith("models/"):
                name = name.replace("models/", "")

            available.append(name)

        return available

    except Exception as e:

        print(f"⚠️ لم أستطع جلب قائمة الموديلات: {e}")

        return []


def build_model_list():

    available = get_available_models()

    # إذا Gemini أعاد القائمة، نستخدم فقط الموديلات الموجودة
    if available:

        result = []

        for model in MODEL_PRIORITY:

            if model in available:
                result.append(model)

        # أضف أي موديلات أخرى ظهرت في حسابك
        for model in available:

            if model not in result:
                if (
                    "flash" in model.lower()
                    and "image" not in model.lower()
                    and "audio" not in model.lower()
                    and "live" not in model.lower()
                ):
                    result.append(model)

        if result:
            return result

    # إذا فشل جلب القائمة نستخدم قائمتنا الاحتياطية
    return MODEL_PRIORITY.copy()


# =========================================================
# إنشاء Chat جديد
# =========================================================

def create_gemini_chat():

    global current_model

    models = build_model_list()

    if not models:
        raise Exception("لا توجد موديلات Gemini متاحة حاليًا.")

    # نبدأ من الموديل الحالي
    ordered_models = []

    if current_model in models:
        ordered_models.append(current_model)

    for model in models:

        if model not in ordered_models:
            ordered_models.append(model)

    last_error = None

    for model in ordered_models:

        try:

            print(f"🔄 محاولة إنشاء Chat باستخدام: {model}")

            chat = client.chats.create(
                model=model
            )

            current_model = model

            print(f"✅ تم اختيار الموديل: {model}")

            return chat, model

        except Exception as e:

            last_error = e

            print(
                f"❌ الموديل {model} فشل أثناء الإنشاء: {e}"
            )

            continue

    raise Exception(
        "لم أستطع إنشاء Chat بأي موديل Gemini.\n"
        f"آخر خطأ: {last_error}"
    )


# =========================================================
# إرسال رسالة Gemini مع Fallback
# =========================================================

def send_gemini_message(user_id, message):

    global current_model

    last_error = None

    # -----------------------------------------------------
    # إذا المستخدم ما عنده Chat
    # -----------------------------------------------------

    if user_id not in user_chats:

        chat, model = create_gemini_chat()

        user_chats[user_id] = {
            "chat": chat,
            "model": model
        }

    # -----------------------------------------------------
    # أخذ الـ Chat الحالي
    # -----------------------------------------------------

    chat_info = user_chats[user_id]

    chat = chat_info["chat"]
    model = chat_info["model"]

    # -----------------------------------------------------
    # نجرب الموديل الحالي أولًا
    # -----------------------------------------------------

    try:

        print(
            f"📤 إرسال الرسالة باستخدام: {model}"
        )

        response = chat.send_message(
            message
        )

        if response and response.text:

            print(
                f"✅ رد {model} بنجاح"
            )

            return response.text

        last_error = Exception(
            "Gemini لم يرجع نصًا."
        )

    except Exception as e:

        last_error = e

        print(
            f"❌ الموديل {model} فشل: {e}"
        )

    # -----------------------------------------------------
    # الموديل الحالي فشل
    # نحذفه وننتقل للبديل
    # -----------------------------------------------------

    user_chats.pop(user_id, None)

    tried_models = {
        model
    }

    models = build_model_list()

    # -----------------------------------------------------
    # تجربة باقي الموديلات
    # -----------------------------------------------------

    for next_model in models:

        if next_model in tried_models:
            continue

        tried_models.add(next_model)

        try:

            print(
                f"🔄 أجرب الموديل البديل: {next_model}"
            )

            new_chat = client.chats.create(
                model=next_model
            )

            response = new_chat.send_message(
                message
            )

            if response and response.text:

                current_model = next_model

                user_chats[user_id] = {
                    "chat": new_chat,
                    "model": next_model
                }

                print(
                    f"✅ انتقلت بنجاح إلى: {next_model}"
                )

                return response.text

            last_error = Exception(
                f"{next_model} لم يرجع نصًا."
            )

        except Exception as e:

            last_error = e

            print(
                f"❌ {next_model} فشل: {e}"
            )

            continue

    # -----------------------------------------------------
    # كل الموديلات فشلت
    # -----------------------------------------------------

    raise Exception(
        "جميع نماذج Gemini فشلت حاليًا.\n"
        f"آخر خطأ: {last_error}"
    )


# =========================================================
# Telegram /start
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    user_chats.pop(user_id, None)

    text = (
        "🤖 أهلاً بك\n\n"
        "اختر من القائمة:\n\n"
        "🟢 دردشة مع Gemini\n"
        "📊 تحليل الذهب"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard()
    )


# =========================================================
# /models
# =========================================================

async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    models = build_model_list()

    text = "🤖 موديلات Gemini المتاحة:\n\n"

    for i, model in enumerate(models, 1):

        if model == current_model:
            text += f"{i}. 🟢 {model} ← الحالي\n"
        else:
            text += f"{i}. {model}\n"

    await update.message.reply_text(
        text
    )


# =========================================================
# زر Gemini
# =========================================================

async def start_gemini_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    try:

        if user_id not in user_chats:

            chat, model = create_gemini_chat()

            user_chats[user_id] = {
                "chat": chat,
                "model": model
            }

        else:

            model = user_chats[user_id]["model"]

        text = (
            "🟢 دخلنا دردشة Gemini\n\n"
            f"🤖 الموديل الحالي:\n"
            f"{model}\n\n"
            "اكتب رسالتك الآن 👇\n\n"
            "إذا الموديل كان عليه ضغط، "
            "البوت سيحاول تلقائيًا موديلًا آخر."
        )

        await query.edit_message_text(
            text,
            reply_markup=back_keyboard()
        )

    except Exception as e:

        print(
            f"❌ خطأ عند بدء Gemini: {e}"
        )

        await query.edit_message_text(
            "❌ حصل خطأ أثناء تشغيل Gemini.\n\n"
            f"{e}",
            reply_markup=main_keyboard()
        )


# =========================================================
# زر تحليل الذهب
# =========================================================

async def gold_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "📊 جاري جلب سعر الذهب..."
    )

    try:

        price = get_gold_price()

        if price is None:

            await query.edit_message_text(
                "❌ حصل خطأ أثناء جلب سعر الذهب.",
                reply_markup=main_keyboard()
            )

            return

        text = (
            "📊 تحليل الذهب\n\n"
            f"🥇 XAU/USD\n"
            f"💰 السعر الحالي: {price}\n\n"
            "⏳ المرحلة الحالية: جلب السعر فقط.\n"
            "سنضيف M5 و M1 والدعوم والمقاومات "
            "ثم تحليل Gemini."
        )

        await query.edit_message_text(
            text,
            reply_markup=main_keyboard()
        )

    except Exception as e:

        print(
            f"❌ خطأ الذهب: {e}"
        )

        await query.edit_message_text(
            "❌ حصل خطأ أثناء تحليل الذهب.\n\n"
            f"{e}",
            reply_markup=main_keyboard()
        )


# =========================================================
# زر الرجوع
# =========================================================

async def back_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    user_chats.pop(user_id, None)

    await query.edit_message_text(
        "🏠 القائمة الرئيسية",
        reply_markup=main_keyboard()
    )


# =========================================================
# استقبال رسائل المستخدم
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    message = update.message.text

    # إذا المستخدم ليس داخل Gemini
    if user_id not in user_chats:

        await update.message.reply_text(
            "اختر أولًا من القائمة 👇",
            reply_markup=main_keyboard()
        )

        return

    # -----------------------------------------------------
    # إرسال الرسالة إلى Gemini
    # -----------------------------------------------------

    try:

        await update.message.chat.send_action(
            action="typing"
        )

        answer = send_gemini_message(
            user_id,
            message
        )

        current_user_model = user_chats[user_id]["model"]

        await update.message.reply_text(
            answer +
            f"\n\n🤖 الموديل: {current_user_model}",
            reply_markup=back_keyboard()
        )

    except Exception as e:

        print(
            f"❌ Gemini النهائي: {e}"
        )

        await update.message.reply_text(
            "❌ لم أستطع الحصول على رد "
            "من أي موديل Gemini حاليًا.\n\n"
            f"آخر خطأ:\n{e}\n\n"
            "🔄 جرّب إرسال الرسالة مرة أخرى.",
            reply_markup=back_keyboard()
        )


# =========================================================
# الحصول على سعر الذهب من Twelve Data
# =========================================================

def get_gold_price():

    url = "https://api.twelvedata.com/price"

    params = {
        "symbol": "XAU/USD",
        "apikey": TWELVE_DATA_API_KEY
    }

    response = requests.get(
        url,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if "price" not in data:

        raise Exception(
            data.get(
                "message",
                "لم يتم العثور على السعر."
            )
        )

    return data["price"]


# =========================================================
# تشغيل البوت
# =========================================================

def main():

    if not BOT_TOKEN:
        raise Exception(
            "BOT_TOKEN غير موجود في Environment Variables"
        )

    if not GEMINI_API_KEY:
        raise Exception(
            "GEMINI_API_KEY غير موجود في Environment Variables"
        )

    if not TWELVE_DATA_API_KEY:
        raise Exception(
            "TWELVE_DATA_API_KEY غير موجود في Environment Variables"
        )

    # تشغيل Flask
    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    # إنشاء Telegram Application
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "models",
            models_command
        )
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(
            start_gemini_chat,
            pattern="^gemini_chat$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            gold_analysis,
            pattern="^gold_analysis$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            back_menu,
            pattern="^back_menu$"
        )
    )

    # Messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print("===================================")
    print("🤖 Gold Bot Started")
    print("===================================")
    print("🟢 Telegram polling started")
    print("🟢 Flask server started")
    print(f"🤖 Current Gemini model: {current_model}")
    print("===================================")

    # تشغيل Telegram
    app.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
