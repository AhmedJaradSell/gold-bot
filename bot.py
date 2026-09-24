import os
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv
from google import genai

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TICKERLAYER_API_KEY = os.getenv("TICKERLAYER_API_KEY")

PORT = int(os.environ.get("PORT", 10000))

GEMINI_MODEL = "gemini-3.5-flash"

gemini = genai.Client(api_key=GEMINI_API_KEY)

# محادثات Gemini لكل مستخدم
user_chats = {}


# =========================
# Render Web Server
# =========================

class HealthCheck(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Gold Bot is running")

    def log_message(self, format, *args):
        pass


def start_web_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthCheck
    )

    server.serve_forever()


# =========================
# القائمة الرئيسية
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
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


async def show_main_menu(update, text="🤖 Gold AI Trader\n\nاختر ماذا تريد:"):

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text,
            reply_markup=main_keyboard()
        )

    else:

        await update.message.reply_text(
            text,
            reply_markup=main_keyboard()
        )


# =========================
# /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["chat_mode"] = False

    await show_main_menu(update)


# =========================
# زر الرجوع
# =========================

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["chat_mode"] = False

    await update.callback_query.edit_message_text(
        "🤖 Gold AI Trader\n\nاختر ماذا تريد:",
        reply_markup=main_keyboard()
    )


# =========================
# جلب سعر الذهب
# =========================

def get_gold_price():

    url = "https://api.tickerlayer.com/commodities/quote/XAUUSD"

    headers = {
        "x-api-key": TICKERLAYER_API_KEY
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=15
    )

    response.raise_for_status()

    return response.json()


# =========================
# أزرار البوت
# =========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    # =====================
    # Gemini
    # =====================

    if query.data == "gemini_chat":

        context.user_data["chat_mode"] = True

        user_id = update.effective_user.id

        if user_id not in user_chats:

            user_chats[user_id] = gemini.chats.create(
                model=GEMINI_MODEL
            )

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 رجوع للقائمة",
                    callback_data="back_menu"
                )
            ]
        ]

        await query.edit_message_text(
            "🤖 دردشة Gemini مفعّلة.\n\n"
            "اكتب رسالتك الآن.\n\n"
            "يمكنك الرجوع للقائمة من الزر بالأسفل 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # =====================
    # تحليل الذهب
    # =====================

    elif query.data == "gold_analysis":

        context.user_data["chat_mode"] = False

        await query.edit_message_text(
            "🥇 جاري جلب سعر الذهب..."
        )

        try:

            data = await asyncio.to_thread(
                get_gold_price
            )

            bid = data.get("bid")
            ask = data.get("ask")
            timestamp = data.get("timestamp")

            if bid is not None and ask is not None:

                mid = (float(bid) + float(ask)) / 2

                text = (
                    "🥇 XAUUSD — الذهب\n\n"
                    f"💵 Bid: {bid}\n"
                    f"💵 Ask: {ask}\n"
                    f"📌 السعر المتوسط: {mid:.2f}\n"
                )

            else:

                text = (
                    "🥇 XAUUSD — الذهب\n\n"
                    f"البيانات:\n{data}"
                )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙 رجوع للقائمة",
                        callback_data="back_menu"
                    )
                ]
            ]

            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        except Exception as e:

            print("TickerLayer error:", e)

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙 رجوع للقائمة",
                        callback_data="back_menu"
                    )
                ]
            ]

            await query.edit_message_text(
                "❌ حصل خطأ أثناء جلب سعر الذهب.\n\n"
                f"الخطأ: {str(e)}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    # =====================
    # رجوع
    # =====================

    elif query.data == "back_menu":

        await back_to_menu(update, context)


# =========================
# رسائل Gemini
# =========================

async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.user_data.get("chat_mode"):
        return

    user_id = update.effective_user.id
    message = update.message.text

    await update.message.reply_text(
        "🤖 Gemini يفكر..."
    )

    try:

        chat = user_chats.get(user_id)

        if chat is None:

            chat = gemini.chats.create(
                model=GEMINI_MODEL
            )

            user_chats[user_id] = chat

        response = await asyncio.to_thread(
            chat.send_message,
            message=message
        )

        await update.message.reply_text(
            response.text
        )

    except Exception as e:

        print("Gemini error:", e)

        context.user_data["chat_mode"] = False

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 رجوع للقائمة",
                    callback_data="back_menu"
                )
            ]
        ]

        await update.message.reply_text(
            "❌ حصل خطأ أثناء الاتصال بـ Gemini.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# =========================
# /models
# =========================

async def list_models(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        models = gemini.models.list()

        text = "📋 الموديلات المتاحة:\n\n"

        for model in models:

            text += f"• {model.name}\n"

        await update.message.reply_text(text)

    except Exception as e:

        await update.message.reply_text(
            f"❌ حصل خطأ:\n{e}"
        )


# =========================
# تشغيل البوت
# =========================

def main():

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("models", list_models)
    )

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat_message
        )
    )

    print("Gold Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
