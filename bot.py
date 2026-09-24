import os
import threading
import requests

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai


# =========================================================
# Environment Variables
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")


if not BOT_TOKEN:
    raise Exception("BOT_TOKEN غير موجود")

if not GEMINI_API_KEY:
    raise Exception("GEMINI_API_KEY غير موجود")

if not TWELVE_DATA_API_KEY:
    raise Exception("TWELVE_DATA_API_KEY غير موجود")


# =========================================================
# Gemini
# =========================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)


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


# =========================================================
# Gemini Chat Memory
# =========================================================

user_chats = {}


# =========================================================
# Flask Server
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Bot is running!"


def run_web_server():

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    web_app.run(
        host="0.0.0.0",
        port=port
    )


# =========================================================
# Main Keyboard
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
                "💰 السعر الحالي",
                callback_data="current_price"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 تحليل الذهب M5",
                callback_data="gold_analysis"
            )
        ],

        [
            InlineKeyboardButton(
                "📉 تحليل الذهب M1",
                callback_data="m1_analysis"
            )
        ],

        [
            InlineKeyboardButton(
                "🤖 تحليل Gemini",
                callback_data="gemini_analysis"
            )
        ],

    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# =========================================================
# Back Button
# =========================================================

def back_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔙 رجوع للقائمة",
                    callback_data="back_menu"
                )
            ]
        ]
    )


# =========================================================
# Available Gemini Models
# =========================================================

def get_available_models():

    try:

        models = client.models.list()

        available = []

        for model in models:

            name = getattr(
                model,
                "name",
                ""
            )

            if not name:
                continue

            if name.startswith("models/"):

                name = name.replace(
                    "models/",
                    "",
                    1
                )

            available.append(name)

        return available

    except Exception:

        return []


def build_model_list():

    available = get_available_models()

    if not available:

        return MODEL_PRIORITY

    result = []

    for model in MODEL_PRIORITY:

        if model in available:

            result.append(model)

    if result:

        return result

    return MODEL_PRIORITY


# =========================================================
# Create Gemini Chat
# =========================================================

def create_gemini_chat():

    models = build_model_list()

    last_error = None

    for model_name in models:

        try:

            chat = client.chats.create(
                model=model_name
            )

            return chat, model_name

        except Exception as e:

            last_error = e

            continue

    if last_error:

        raise last_error

    raise Exception(
        "لم أستطع تشغيل أي موديل Gemini."
    )


# =========================================================
# Send Gemini Message With Fallback
# =========================================================

def send_gemini_message(prompt):

    models = build_model_list()

    last_error = None

    for model_name in models:

        try:

            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )

            text = getattr(
                response,
                "text",
                None
            )

            if text:

                return text

            last_error = Exception(
                f"الموديل {model_name} لم يرجع نصًا."
            )

        except Exception as e:

            last_error = e

            continue

    if last_error:

        raise last_error

    raise Exception(
        "فشل الاتصال بجميع موديلات Gemini."
    )


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "👋 أهلاً بك\n\n"
        "اختر من القائمة:",

        reply_markup=main_keyboard()
    )


# =========================================================
# /models
# =========================================================

async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        models = build_model_list()

        text = "🤖 موديلات Gemini:\n\n"

        for model in models:

            text += f"• {model}\n"

        await update.message.reply_text(
            text
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ حصل خطأ:\n\n{e}"
        )


# =========================================================
# Start Gemini Chat
# =========================================================

async def start_gemini_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = update.effective_user.id

    try:

        chat, model_name = create_gemini_chat()

        user_chats[user_id] = chat

        await query.edit_message_text(

            "🟢 تم تشغيل دردشة Gemini.\n\n"
            "اكتب رسالتك الآن.\n\n"
            f"🤖 الموديل: {model_name}",

            reply_markup=back_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(

            f"❌ حصل خطأ أثناء تشغيل Gemini:\n\n{e}",

            reply_markup=back_keyboard()
        )


# =========================================================
# Normal Gemini Chat
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id not in user_chats:

        return

    text = update.message.text

    thinking_message = await update.message.reply_text(
        "🧠 Gemini يفكر..."
    )

    try:

        chat = user_chats[user_id]

        response = chat.send_message(
            text
        )

        reply = getattr(
            response,
            "text",
            None
        )

        if not reply:

            reply = "❌ Gemini لم يرجع ردًا."

        await thinking_message.delete()

        await update.message.reply_text(
            reply,
            reply_markup=back_keyboard()
        )

    except Exception as e:

        try:

            await thinking_message.delete()

        except Exception:

            pass

        await update.message.reply_text(

            f"❌ حصل خطأ أثناء الاتصال بـ Gemini:\n\n{e}",

            reply_markup=back_keyboard()
        )


# =========================================================
# Current Gold Price
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
                "لم يتم العثور على سعر الذهب."
            )
        )

    return data["price"]


# =========================================================
# Current Price Button
# =========================================================

async def current_price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    try:

        price = get_gold_price()

        await query.edit_message_text(

            "💰 السعر الحالي للذهب\n\n"
            "🥇 XAU/USD\n\n"
            f"💵 السعر: {price}",

            reply_markup=main_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(

            f"❌ تعذر جلب السعر الحالي.\n\n{e}",

            reply_markup=main_keyboard()
        )


# =========================================================
# Get M5 - Last 24 Hours
# =========================================================

def get_gold_m5():

    url = "https://api.twelvedata.com/time_series"

    params = {

        "symbol": "XAU/USD",

        "interval": "5min",

        # 24 hours × 12 candles = 288
        "outputsize": 288,

        "order": "asc",

        "timezone": "UTC",

        "apikey": TWELVE_DATA_API_KEY

    }

    response = requests.get(

        url,

        params=params,

        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("status") == "error":

        raise Exception(
            data.get(
                "message",
                "خطأ من Twelve Data"
            )
        )

    values = data.get("values")

    if not values:

        raise Exception(
            "لم تصل بيانات M5."
        )

    return values


# =========================================================
# Get M1 - Last 6 Hours
# =========================================================

def get_gold_m1():

    url = "https://api.twelvedata.com/time_series"

    params = {

        "symbol": "XAU/USD",

        "interval": "1min",

        # 6 hours × 60 candles = 360
        "outputsize": 360,

        "order": "asc",

        "timezone": "UTC",

        "apikey": TWELVE_DATA_API_KEY

    }

    response = requests.get(

        url,

        params=params,

        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("status") == "error":

        raise Exception(
            data.get(
                "message",
                "خطأ من Twelve Data"
            )
        )

    values = data.get("values")

    if not values:

        raise Exception(
            "لم تصل بيانات M1."
        )

    return values


# =========================================================
# Format Raw Candles
# =========================================================

def format_candles(candles):

    result = []

    for candle in candles:

        result.append({

            "time": candle.get(
                "datetime"
            ),

            "open": candle.get(
                "open"
            ),

            "high": candle.get(
                "high"
            ),

            "low": candle.get(
                "low"
            ),

            "close": candle.get(
                "close"
            )

        })

    return result


# =========================================================
# M5 Button
# =========================================================

async def gold_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    try:

        candles = get_gold_m5()

        await query.edit_message_text(

            "📊 بيانات الذهب M5\n\n"
            f"🕯️ عدد الشموع: {len(candles)}\n\n"
            "للحصول على التحليل الكامل استخدم:\n"
            "🤖 تحليل Gemini",

            reply_markup=main_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(

            f"❌ تعذر جلب بيانات M5.\n\n{e}",

            reply_markup=main_keyboard()
        )


# =========================================================
# M1 Button
# =========================================================

async def m1_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    try:

        candles = get_gold_m1()

        await query.edit_message_text(

            "📉 بيانات الذهب M1\n\n"
            f"🕯️ عدد الشموع: {len(candles)}\n\n"
            "للحصول على التحليل الكامل استخدم:\n"
            "🤖 تحليل Gemini",

            reply_markup=main_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(

            f"❌ تعذر جلب بيانات M1.\n\n{e}",

            reply_markup=main_keyboard()
        )


# =========================================================
# Gemini Gold Analysis
# =========================================================

async def gemini_gold_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    try:

        # -------------------------------------------------
        # Step 1
        # -------------------------------------------------

        await query.edit_message_text(

            "🧠 Gemini يحلل الآن...\n\n"
            "⏳ أجمع السعر الحالي..."
        )

        price = get_gold_price()

        # -------------------------------------------------
        # Step 2
        # -------------------------------------------------

        await query.edit_message_text(

            "🧠 Gemini يحلل الآن...\n\n"
            "✅ السعر الحالي\n"
            "⏳ أجمع شموع M5..."
        )

        m5_raw = get_gold_m5()

        m5_candles = format_candles(
            m5_raw
        )

        # -------------------------------------------------
        # Step 3
        # -------------------------------------------------

        await query.edit_message_text(

            "🧠 Gemini يحلل الآن...\n\n"
            "✅ السعر الحالي\n"
            f"✅ {len(m5_candles)} شمعة M5\n"
            "⏳ أجمع شموع M1..."
        )

        m1_raw = get_gold_m1()

        m1_candles = format_candles(
            m1_raw
        )

        # -------------------------------------------------
        # Step 4
        # -------------------------------------------------

        await query.edit_message_text(

            "🧠 Gemini يحلل الآن...\n\n"
            "✅ السعر الحالي\n"
            f"✅ {len(m5_candles)} شمعة M5\n"
            f"✅ {len(m1_candles)} شمعة M1\n"
            "⏳ أرسل البيانات إلى Gemini...\n"
            "⏳ جاري التحليل..."
        )

        # =================================================
        # Prompt
        # =================================================

        prompt = f"""
أنت محلل فني للذهب XAU/USD.

حلل بنفسك بيانات الشموع الخام التي سأرسلها لك.

البيانات:

السعر الحالي:
{price}

M5 - آخر 24 ساعة:
{m5_candles}

M1 - آخر 6 ساعات:
{m1_candles}

مهم جدًا:

لا يوجد تحليل مسبق من البوت.

لا أريد منك الاعتماد على أي اتجاه أو نتيجة محسوبة مسبقًا.

حلل الشموع بنفسك.

ركز على:
- اتجاه M5
- اتجاه M1
- بنية السوق
- Higher High
- Higher Low
- Lower High
- Lower Low
- الزخم
- الدعم
- المقاومة
- الاختراقات
- الرفض
- التوافق أو التعارض بين M5 وM1

بعد التحليل أعطني توصية تداول.

إذا كانت هناك فرصة واضحة:

📊 الاتجاه: BUY أو SELL

🎯 Entry: رقم واحد

🛑 Stop Loss: رقم واحد

💰 Take Profit: رقم واحد

📐 Risk/Reward: النسبة

🔥 السبب:
اذكر أهم سببين أو ثلاثة فقط.

إذا لم تكن هناك فرصة واضحة:

📊 الاتجاه: WAIT

⚠️ السبب:
اذكره باختصار.

شروط مهمة:

- Entry وSL وTP يجب أن تكون مبنية على تحليل الشموع.
- لا تعطِ أكثر من Entry واحد.
- لا تعطِ أكثر من Stop Loss واحد.
- لا تعطِ أكثر من Take Profit واحد.
- لا تعطِ قائمة طويلة من المستويات.
- لا تكرر بيانات الشموع.
- لا تكتب شرحًا طويلًا.
- اجعل الرد مختصرًا ومناسبًا لـTelegram.
- الحد الأقصى للرد حوالي 2000 حرف.
- لا تضمن الربح.
- لا تقل إن الصفقة مؤكدة.

أريد النتيجة بهذا الشكل تقريبًا:

🤖 تحليل Gemini

📊 الاتجاه: BUY/SELL/WAIT

📈 M5: ...
📉 M1: ...
🔄 التوافق: ...

🎯 Entry: ...
🛑 SL: ...
💰 TP: ...
📐 R/R: ...

🔥 السبب:
...

⚠️ التحليل ليس ضمانًا لحركة السعر.
"""

        # =================================================
        # Gemini
        # =================================================

        result = send_gemini_message(
            prompt
        )

        # -------------------------------------------------
        # Final Result
        # -------------------------------------------------

        await query.edit_message_text(

            "🤖 تحليل Gemini للذهب\n\n"
            f"{result}",

            reply_markup=main_keyboard()
        )

    except Exception as e:

        await query.edit_message_text(

            "❌ حصلت مشكلة أثناء تحليل Gemini.\n\n"
            f"{e}\n\n"
            "🔄 جرّب مرة ثانية.",

            reply_markup=main_keyboard()
        )


# =========================================================
# Back To Menu
# =========================================================

async def back_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = update.effective_user.id

    user_chats.pop(
        user_id,
        None
    )

    await query.edit_message_text(

        "🏠 القائمة الرئيسية\n\n"
        "اختر ما تريد:",

        reply_markup=main_keyboard()
    )


# =========================================================
# Main
# =========================================================

def main():

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "models",
            models_command
        )
    )

    # Buttons
    application.add_handler(
        CallbackQueryHandler(
            start_gemini_chat,
            pattern="^gemini_chat$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            current_price,
            pattern="^current_price$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            gold_analysis,
            pattern="^gold_analysis$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            m1_analysis,
            pattern="^m1_analysis$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            gemini_gold_analysis,
            pattern="^gemini_analysis$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            back_menu,
            pattern="^back_menu$"
        )
    )

    # Normal text messages
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print("Bot is running...")

    application.run_polling()


# =========================================================
# Start
# =========================================================

if __name__ == "__main__":

    main()
