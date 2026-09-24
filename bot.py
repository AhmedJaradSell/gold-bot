import os
import requests

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
# Environment Variables
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

GEMINI_MODEL = "gemini-3.5-flash"

client = genai.Client(api_key=GEMINI_API_KEY)

# حفظ حالة دردشة Gemini لكل مستخدم
user_chats = {}

# =========================
# Main Keyboard
# =========================

def main_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(
                "🟢 دردشة مع Gemini",
                callback_data="gemini"
            ),
            InlineKeyboardButton(
                "📊 تحليل الذهب",
                callback_data="gold"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================
# Back Button
# =========================

def back_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(
                "🔙 رجوع للقائمة",
                callback_data="back"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================
# Start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "أهلًا 👋\nاختار من القائمة:",
        reply_markup=main_keyboard()
    )


# =========================
# Gemini Chat
# =========================

async def start_gemini_chat(update: Update):

    user_id = update.effective_user.id

    user_chats[user_id] = []

    await update.callback_query.edit_message_text(
        "🟢 أنت الآن في دردشة Gemini.\n\n"
        "اكتب رسالتك وأنا أرسلها إلى Gemini.\n\n"
        "للخروج اضغط رجوع:",
        reply_markup=back_keyboard()
    )


async def gemini_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    # إذا المستخدم ليس داخل دردشة Gemini
    if user_id not in user_chats:
        return

    text = update.message.text

    try:

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=text
        )

        answer = response.text

        await update.message.reply_text(
            answer,
            reply_markup=back_keyboard()
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ حصل خطأ أثناء الاتصال بـ Gemini:\n\n{e}",
            reply_markup=back_keyboard()
        )


# =========================
# Twelve Data - Gold Price
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
# Button Handler
# =========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    # -------------------------
    # Gemini
    # -------------------------

    if query.data == "gemini":

        await start_gemini_chat(update)

        return

    # -------------------------
    # Back
    # -------------------------

    if query.data == "back":

        user_id = update.effective_user.id

        user_chats.pop(user_id, None)

        await query.edit_message_text(
            "القائمة الرئيسية 👇",
            reply_markup=main_keyboard()
        )

        return

    # -------------------------
    # Gold
    # -------------------------

    if query.data == "gold":

        await query.edit_message_text(
            "⏳ بجلب سعر الذهب من Twelve Data..."
        )

        try:

            data = get_gold_price()

            if "price" not in data:

                await query.edit_message_text(
                    "❌ Twelve Data لم يرجع السعر.\n\n"
                    f"{data}",
                    reply_markup=main_keyboard()
                )

                return

            price = data["price"]

            await query.edit_message_text(
                f"🟡 XAU/USD\n\n"
                f"💰 السعر الحالي:\n"
                f"{price}",
                reply_markup=main_keyboard()
            )

        except Exception as e:

            await query.edit_message_text(
                f"❌ حصل خطأ أثناء جلب سعر الذهب:\n\n"
                f"{e}",
                reply_markup=main_keyboard()
            )

        return


# =========================
# Main
# =========================

def main():

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN غير موجود")

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY غير موجود")

    if not TWELVE_DATA_API_KEY:
        raise ValueError("TWELVE_DATA_API_KEY غير موجود")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            gemini_message
        )
    )

    print("Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
