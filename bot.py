import os
import threading
import asyncio
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

GEMINI_MODEL = "gemini-2.5-flash"


# =========================
# GEMINI
# =========================

client = genai.Client(
    api_key=GEMINI_API_KEY
)

user_chats = {}


# =========================
# FLASK FOR RENDER
# =========================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Bot is running!"


def run_web_server():

    port = int(
        os.environ.get("PORT", 10000)
    )

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

        models = await asyncio.to_thread(
            lambda: list(client.models.list())
        )

        text = "🤖 النماذج المتاحة:\n\n"

        count = 0

        for model in models:

            name = getattr(
                model,
                "name",
                ""
            )

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
# START GEMINI
# =========================

async def start_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    user_id = query.from_user.id

    try:

        chat = await asyncio.to_thread(
            lambda: client.chats.create(
                model=GEMINI_MODEL
            )
        )

        user_chats[user_id] = chat

        await query.edit_message_text(
            "🟢 أنت الآن في دردشة مع Gemini.\n\n"
            "اكتب أي سؤال وسأرسله إلى Gemini.\n\n"
            "⏳ إذا تأخر Gemini، سيبقى الطلب قيد المعالجة "
            "حتى يصل الرد.",
            reply_markup=back_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(
            f"❌ حصل خطأ أثناء تشغيل Gemini:\n{e}",
            reply_markup=main_keyboard()
        )


# =========================
# SEND TO GEMINI
# =========================

async def send_to_gemini(chat, user_text):

    # تشغيل طلب Gemini في Thread منفصل
    # حتى لا يجمّد بوت Telegram
    response = await asyncio.to_thread(
        lambda: chat.send_message(user_text)
    )

    return response


# =========================
# GEMINI CHAT MESSAGE
# =========================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    # إذا المستخدم ليس داخل Gemini
    if user_id not in user_chats:

        await update.message.reply_text(
            "اختر أحد الخيارات من القائمة 👇",
            reply_markup=main_keyboard()
        )

        return

    user_text = update.message.text

    chat = user_chats[user_id]

    # رسالة انتظار
    waiting_message = await update.message.reply_text(
        "🤖 Gemini يفكر...\n\n"
        "⏳ قد يستغرق الرد بعض الوقت، "
        "والبوت سيبقى شغالًا بشكل طبيعي."
    )

    try:

        # لا يوجد Timeout هنا
        # الطلب سيستمر حتى يرد Gemini
        response = await send_to_gemini(
            chat,
            user_text
        )

        answer = response.text

        if not answer:

            answer = "❌ Gemini لم يرجع نصًا."

        # حذف رسالة الانتظار
        try:

            await waiting_message.delete()

        except Exception:

            pass

        # إرسال الرد
        await update.message.reply_text(
            answer,
            reply_markup=back_keyboard()
        )

    except Exception as e:

        try:

            await waiting_message.edit_text(
                f"❌ حصل خطأ أثناء الاتصال بـ Gemini:\n\n{e}"
            )

        except Exception:

            await update.message.reply_text(
                f"❌ حصل خطأ أثناء الاتصال بـ Gemini:\n\n{e}"
            )


# =========================
# GOLD PRICE
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
# GOLD ANALYSIS
# =========================

async def gold_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    try:

        data = await asyncio.to_thread(
            get_gold_price
        )

        if (
            "code" in data
            and "message" in data
        ):

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
            "🥇 XAU/USD\n"
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

    user_id = query.from_user.id

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

    # الرد فورًا على Telegram
    try:

        await query.answer()

    except Exception:

        pass

    if query.data == "gemini_chat":

        await start_gemini(
            update,
            context
        )

    elif query.data == "gold_analysis":

        await gold_analysis(
            update,
            context
        )

    elif query.data == "back_menu":

        await back_menu(
            update,
            context
        )


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

    app = Application.builder().token(
        BOT_TOKEN
    ).build()

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
            button_handler
        )
    )

    # Messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    # Flask / Render
    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    print("Bot is running...")

    # Telegram
    app.run_polling()


# =========================
# RUN
# =========================

if __name__ == "__main__":

    main()
