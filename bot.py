import os
import threading
import requests

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from google import genai


# =========================
# ENVIRONMENT VARIABLES
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

GEMINI_MODEL = "gemini-3.5-flash"


# =========================
# GEMINI
# =========================

client = genai.Client(api_key=GEMINI_API_KEY)

user_chats = {}


# =========================
# FLASK SERVER FOR RENDER
# =========================

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


# =========================
# MAIN MENU
# =========================

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


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    # إيقاف وضع Gemini لهذا المستخدم
    user_chats.pop(user_id, None)

    await update.message.reply_text(
        "👋 أهلاً بك!\n\n"
        "اختر من القائمة:",
        reply_markup=main_keyboard()
    )


# =========================
# MODELS
# =========================

async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        models = client.models.list()

        text = "🤖 النماذج المتاحة:\n\n"

        count = 0

        for model in models:

            name = getattr(model, "name", "")

            if name:
                text += f"• {name}\n"
                count += 1

            if count >= 20:
                break

        await update.message.reply_text(text)

    except Exception as e:

        await update.message.reply_text(
            f"❌ حصل خطأ أثناء جلب النماذج:\n{e}"
        )


# =========================
# GEMINI CHAT
# =========================

async def start_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    try:

        chat = client.chats.create(
            model=GEMINI_MODEL
        )

        user_chats[user_id] = chat

        await query.edit_message_text(
            "🟢 أنت الآن في دردشة مع Gemini.\n\n"
            "اكتب أي سؤال وسأرسله إلى Gemini.\n\n"
            "وعندما تريد الخروج اضغط الزر بالأسفل.",
            reply_markup=back_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(
            f"❌ حصل خطأ أثناء تشغيل Gemini:\n{e}",
            reply_markup=main_keyboard()
        )


# =========================
# GEMINI MESSAGES
# =========================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    # إذا المستخدم داخل دردشة Gemini
    if user_id in user_chats:

        user_text = update.message.text

        try:

            chat = user_chats[user_id]

            response = chat.send_message(user_text)

            answer = response.text

            if not answer:
                answer = "❌ Gemini لم يرجع ردًا."

            await update.message.reply_text(
                answer,
                reply_markup=back_keyboard()
            )

        except Exception as e:

            await update.message.reply_text(
                f"❌ حصل خطأ أثناء الاتصال بـ Gemini:\n\n{e}",
                reply_markup=back_keyboard()
            )

    else:

        await update.message.reply_text(
            "اختر أحد الخيارات من القائمة 👇",
            reply_markup=main_keyboard()
        )


# =========================
# GOLD PRICE - TWELVE DATA
# =========================

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

    return response.json()


# =========================
# GOLD ANALYSIS BUTTON
# =========================

async def gold_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    try:

        data = get_gold_price()

        # لو API رجع خطأ
        if "code" in data and "message" in data:

            await query.edit_message_text(
                f"❌ خطأ من Twelve Data:\n\n"
                f"{data.get('message')}",
                reply_markup=main_keyboard()
            )

            return

        price = data.get("price")

        if price is None:

            await query.edit_message_text(
                f"❌ لم أستطع الحصول على سعر الذهب.\n\n"
                f"البيانات:\n{data}",
                reply_markup=main_keyboard()
            )

            return

        await query.edit_message_text(
            "📊 تحليل الذهب\n\n"
            f"🥇 XAU/USD\n"
            f"💰 السعر الحالي: {price}\n\n"
            "⏳ المرحلة الحالية هي جلب البيانات.\n"
            "سنضيف لاحقًا تحليل M5 و M1 والدعم والمقاومة.",
            reply_markup=main_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(
            f"❌ حصل خطأ أثناء جلب سعر الذهب:\n\n{e}",
            reply_markup=main_keyboard()
        )


# =========================
# BACK TO MENU
# =========================

async def back_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    # الخروج من Gemini
    user_chats.pop(user_id, None)

    await query.edit_message_text(
        "🏠 القائمة الرئيسية\n\n"
        "اختر ماذا تريد:",
        reply_markup=main_keyboard()
    )


# =========================
# BUTTON HANDLER
# =========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query.data == "gemini_chat":

        await start_gemini(update, context)

    elif query.data == "gold_analysis":

        await gold_analysis(update, context)

    elif query.data == "back_menu":

        await back_menu(update, context)


# =========================
# MAIN
# =========================

def main():

    if not BOT_TOKEN:

        print("❌ BOT_TOKEN غير موجود")
        return

    if not GEMINI_API_KEY:

        print("❌ GEMINI_API_KEY غير موجود")
        return

    if not TWELVE_DATA_API_KEY:

        print("❌ TWELVE_DATA_API_KEY غير موجود")
        return

    # تشغيل بوت Telegram
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("models", models_command)
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # Messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    # تشغيل Flask في Thread منفصل
    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    print("Bot is running...")

    # تشغيل Telegram polling
    app.run_polling()


# =========================
# RUN
# =========================

if __name__ == "__main__":
    main()
