import os
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer

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
PORT = int(os.environ.get("PORT", 10000))

gemini = genai.Client(api_key=GEMINI_API_KEY)

# محادثات Gemini لكل مستخدم
user_chats = {}

# الموديل
GEMINI_MODEL = "gemini-3.8-flash"


class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Gold Bot is running")

    def log_message(self, format, *args):
        pass


def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheck)
    server.serve_forever()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["chat_mode"] = False

    keyboard = [
        [InlineKeyboardButton("🟢 دردشة مع Gemini", callback_data="gemini_chat")],
        [InlineKeyboardButton("📊 تحليل الذهب", callback_data="gold_analysis")],
    ]

    await update.message.reply_text(
        "🤖 Gold AI Trader\n\nاختر ماذا تريد:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "gemini_chat":

        context.user_data["chat_mode"] = True

        user_id = update.effective_user.id

        if user_id not in user_chats:
            user_chats[user_id] = gemini.chats.create(
                model=GEMINI_MODEL
            )

        await query.edit_message_text(
            "🤖 دردشة Gemini مفعّلة.\n\n"
            "اكتب رسالتك الآن، وسأرسلها إلى Gemini.\n\n"
            "مثال:\n"
            "ما هو الذكاء الاصطناعي؟"
        )

    elif query.data == "gold_analysis":

        context.user_data["chat_mode"] = False

        await query.edit_message_text(
            "📊 تحليل الذهب\n\n"
            "هذه الوظيفة سنبنيها في الخطوة القادمة."
        )


async def send_to_gemini(chat, message):
    """
    إرسال الرسالة إلى Gemini مع إعادة المحاولة
    إذا كان الخطأ 503.
    """

    max_attempts = 4

    for attempt in range(max_attempts):
        try:
            response = await asyncio.to_thread(
                chat.send_message,
                message=message
            )

            return response

        except Exception as e:

            error_text = str(e)

            # إذا كان الخطأ 503 نعيد المحاولة
            if "503" in error_text or "UNAVAILABLE" in error_text:

                if attempt < max_attempts - 1:

                    wait_time = 2 ** attempt

                    print(
                        f"Gemini 503 - retrying in {wait_time} seconds..."
                    )

                    await asyncio.sleep(wait_time)

                    continue

            raise e


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.user_data.get("chat_mode"):
        return

    user_id = update.effective_user.id
    message = update.message.text

    await update.message.reply_text("🤖 Gemini يفكر...")

    try:

        chat = user_chats.get(user_id)

        if chat is None:
            chat = gemini.chats.create(
                model=GEMINI_MODEL
            )

            user_chats[user_id] = chat

        response = await send_to_gemini(
            chat,
            message
        )

        if response.text:
            await update.message.reply_text(
                response.text
            )
        else:
            await update.message.reply_text(
                "⚠️ Gemini لم يرجع نصًا."
            )

    except Exception as e:

        print("Gemini error:", e)

        await update.message.reply_text(
            "❌ Gemini غير متاح حاليًا.\n"
            "حاول مرة أخرى بعد قليل."
        )


def main():

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
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
