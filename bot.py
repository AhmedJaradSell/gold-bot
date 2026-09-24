import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from google import genai

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))

gemini = genai.Client(api_key=GEMINI_API_KEY)


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
    keyboard = [
        [InlineKeyboardButton("🟢 START", callback_data="start_analysis")]
    ]

    await update.message.reply_text(
        "🤖 Gold AI Trader\n\nجاهز لتحليل الذهب.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def start_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🤖 جاري الاتصال بـ Gemini...\n\n⏳ لحظة..."
    )

    try:
        response = gemini.models.generate_content(
            model="gemini-3.8-flash",
            contents="قل: تم الاتصال بـ Gemini بنجاح، واكتب جملة قصيرة بالعربية."
        )

        await query.message.reply_text(
            "✅ Gemini متصل!\n\n" + response.text
        )

    except Exception as e:
        await query.message.reply_text(
            "❌ حصل خطأ أثناء الاتصال بـ Gemini.\n\n"
            "راجع Logs في Render."
        )
        print("Gemini error:", e)


def main():
    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(start_analysis, pattern="^start_analysis$")
    )

    print("Gold Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
