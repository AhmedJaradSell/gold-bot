import os
import threading
import requests

from flask import Flask

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from google import genai


# =========================================================
# الإعدادات
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")


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

current_model = MODEL_PRIORITY[0]

user_chats = {}


# =========================================================
# Flask - Render
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
# القائمة الرئيسية
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
        ]

    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# زر الرجوع
# =========================================================

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
# موديلات Gemini
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
                    ""
                )

            available.append(name)

        return available

    except Exception as e:

        print(
            f"⚠️ تعذر جلب الموديلات: {e}"
        )

        return []


def build_model_list():

    available = get_available_models()

    if available:

        result = []

        for model in MODEL_PRIORITY:

            if model in available:

                result.append(model)

        for model in available:

            if model in result:
                continue

            lower = model.lower()

            if "flash" not in lower:
                continue

            if "image" in lower:
                continue

            if "audio" in lower:
                continue

            if "live" in lower:
                continue

            result.append(model)

        if result:

            return result

    return MODEL_PRIORITY.copy()


# =========================================================
# إنشاء Gemini Chat
# =========================================================

def create_gemini_chat():

    global current_model

    models = build_model_list()

    if not models:

        raise Exception(
            "لا توجد موديلات Gemini متاحة."
        )

    ordered_models = []

    if current_model in models:

        ordered_models.append(
            current_model
        )

    for model in models:

        if model not in ordered_models:

            ordered_models.append(model)

    last_error = None

    for model in ordered_models:

        try:

            print(
                f"🔄 محاولة استخدام: {model}"
            )

            chat = client.chats.create(
                model=model
            )

            current_model = model

            print(
                f"✅ تم اختيار: {model}"
            )

            return chat, model

        except Exception as e:

            last_error = e

            print(
                f"❌ {model} فشل: {e}"
            )

            continue

    raise Exception(
        "لم أستطع تشغيل أي موديل Gemini.\n"
        f"آخر خطأ: {last_error}"
    )


# =========================================================
# إرسال رسالة Gemini مع Fallback
# =========================================================

def send_gemini_message(
    user_id,
    message
):

    global current_model

    last_error = None

    if user_id not in user_chats:

        chat, model = create_gemini_chat()

        user_chats[user_id] = {
            "chat": chat,
            "model": model
        }

    chat_info = user_chats[user_id]

    chat = chat_info["chat"]

    model = chat_info["model"]

    try:

        print(
            f"📤 إرسال الرسالة إلى: {model}"
        )

        response = chat.send_message(
            message
        )

        if response and response.text:

            print(
                f"✅ {model} رد بنجاح"
            )

            return response.text

        last_error = Exception(
            "Gemini لم يرجع نصًا."
        )

    except Exception as e:

        last_error = e

        print(
            f"❌ {model} فشل: {e}"
        )

    user_chats.pop(
        user_id,
        None
    )

    tried_models = {
        model
    }

    models = build_model_list()

    for next_model in models:

        if next_model in tried_models:
            continue

        tried_models.add(
            next_model
        )

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
                    f"✅ انتقلت إلى: {next_model}"
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

    raise Exception(
        "جميع نماذج Gemini فشلت حاليًا.\n"
        f"آخر خطأ: {last_error}"
    )


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    user_chats.pop(
        user_id,
        None
    )

    await update.message.reply_text(

        "🤖 أهلاً بك\n\n"
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

    models = build_model_list()

    text = "🤖 موديلات Gemini المتاحة:\n\n"

    for i, model in enumerate(
        models,
        1
    ):

        if model == current_model:

            text += (
                f"{i}. 🟢 {model} ← الحالي\n"
            )

        else:

            text += (
                f"{i}. {model}\n"
            )

    await update.message.reply_text(
        text
    )


# =========================================================
# دخول Gemini
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

            model = user_chats[
                user_id
            ]["model"]

        await query.edit_message_text(

            "🟢 دردشة Gemini\n\n"
            f"🤖 الموديل الحالي:\n{model}\n\n"
            "✍️ اكتب رسالتك الآن 👇\n\n"
            "🧠 إذا كان الموديل عليه ضغط، "
            "سأنتقل تلقائيًا لموديل آخر.",

            reply_markup=back_keyboard()

        )

    except Exception as e:

        print(
            f"❌ خطأ Gemini: {e}"
        )

        await query.edit_message_text(

            "❌ حصل خطأ أثناء تشغيل Gemini.\n\n"
            f"{e}",

            reply_markup=main_keyboard()

        )


# =========================================================
# سعر الذهب الحالي
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
# زر السعر الحالي
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
            f"💵 السعر: {price}\n\n"
            "🕐 السعر من Twelve Data.",

            reply_markup=main_keyboard()

        )

    except Exception as e:

        print(
            f"❌ خطأ السعر الحالي: {e}"
        )

        await query.edit_message_text(

            "❌ تعذر جلب السعر الحالي.\n\n"
            f"{e}",

            reply_markup=main_keyboard()

        )


# =========================================================
# بيانات M5 - آخر 24 ساعة
# =========================================================

def get_gold_m5():

    url = "https://api.twelvedata.com/time_series"

    params = {

        "symbol": "XAU/USD",

        "interval": "5min",

        "outputsize": 288,

        "order": "asc",

        "timezone": "UTC",

        "apikey": TWELVE_DATA_API_KEY

    }

    response = requests.get(

        url,

        params=params,

        timeout=20

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

    values = data.get(
        "values"
    )

    if not values:

        raise Exception(
            "لم تصل بيانات M5 من Twelve Data."
        )

    return values


# =========================================================
# تحليل الذهب M5
# =========================================================

async def gold_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(

        "📊 جاري تحليل الذهب M5...\n\n"
        "⏳ أجلب سعر الذهب...\n"
        "⏳ أجلب بيانات M5 لآخر 24 ساعة..."

    )

    try:

        price = get_gold_price()

        m5 = get_gold_m5()

        oldest = m5[0]

        latest = m5[-1]

        highest = max(

            float(candle["high"])

            for candle in m5

        )

        lowest = min(

            float(candle["low"])

            for candle in m5

        )

        first_close = float(
            m5[0]["close"]
        )

        last_close = float(
            m5[-1]["close"]
        )

        if last_close > first_close:

            trend = "📈 صاعد"

        elif last_close < first_close:

            trend = "📉 هابط"

        else:

            trend = "➡️ جانبي"

        text = (

            "📊 تحليل الذهب - M5\n\n"

            "🥇 XAU/USD\n"

            f"💰 السعر الحالي: {price}\n\n"

            f"🕯️ عدد شموع M5: {len(m5)}\n\n"

            f"🕐 البداية:\n"
            f"{oldest['datetime']}\n\n"

            f"🕐 النهاية:\n"
            f"{latest['datetime']}\n\n"

            f"📈 أعلى سعر: {highest:.2f}\n"

            f"📉 أدنى سعر: {lowest:.2f}\n\n"

            f"📊 أول إغلاق: {first_close:.2f}\n"

            f"📊 آخر إغلاق: {last_close:.2f}\n\n"

            f"🔎 الاتجاه الأولي: {trend}\n\n"

            "ℹ️ هذا اتجاه أولي فقط، "
            "وليس إشارة دخول."
        )

        await query.edit_message_text(

            text,

            reply_markup=main_keyboard()

        )

    except Exception as e:

        print(
            f"❌ خطأ M5: {e}"
        )

        await query.edit_message_text(

            "❌ حصل خطأ أثناء جلب بيانات M5.\n\n"
            f"{e}",

            reply_markup=main_keyboard()

        )


# =========================================================
# بيانات M1 - آخر 6 ساعات
# =========================================================

def get_gold_m1():

    url = "https://api.twelvedata.com/time_series"

    params = {

        "symbol": "XAU/USD",

        "interval": "1min",

        "outputsize": 360,

        "order": "asc",

        "timezone": "UTC",

        "apikey": TWELVE_DATA_API_KEY

    }

    response = requests.get(

        url,

        params=params,

        timeout=25

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

    values = data.get(
        "values"
    )

    if not values:

        raise Exception(
            "لم تصل بيانات M1 من Twelve Data."
        )

    return values


# =========================================================
# تحليل الذهب M1 - آخر 6 ساعات
# =========================================================

async def m1_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(

        "📉 جاري تحليل الذهب M1...\n\n"
        "⏳ أجلب بيانات M1 لآخر 6 ساعات..."

    )

    try:

        price = get_gold_price()

        m1 = get_gold_m1()

        oldest = m1[0]

        latest = m1[-1]

        highest = max(

            float(candle["high"])

            for candle in m1

        )

        lowest = min(

            float(candle["low"])

            for candle in m1

        )

        first_close = float(
            m1[0]["close"]
        )

        last_close = float(
            m1[-1]["close"]
        )

        if last_close > first_close:

            trend = "📈 صاعد"

        elif last_close < first_close:

            trend = "📉 هابط"

        else:

            trend = "➡️ جانبي"

        text = (

            "📉 تحليل الذهب - M1\n\n"

            "🥇 XAU/USD\n"

            f"💰 السعر الحالي: {price}\n\n"

            f"🕯️ عدد شموع M1: {len(m1)}\n\n"

            f"🕐 البداية:\n"
            f"{oldest['datetime']}\n\n"

            f"🕐 النهاية:\n"
            f"{latest['datetime']}\n\n"

            f"📈 أعلى سعر: {highest:.2f}\n"

            f"📉 أدنى سعر: {lowest:.2f}\n\n"

            f"📊 أول إغلاق: {first_close:.2f}\n"

            f"📊 آخر إغلاق: {last_close:.2f}\n\n"

            f"🔎 الاتجاه الأولي: {trend}\n\n"

            "ℹ️ هذا تحليل أولي لـ M1 فقط، "
            "وليس إشارة دخول."
        )

        await query.edit_message_text(

            text,

            reply_markup=main_keyboard()

        )

        print(
            f"📉 M1 candles: {len(m1)}"
        )

        print(
            f"📈 M1 highest: {highest}"
        )

        print(
            f"📉 M1 lowest: {lowest}"
        )

        print(
            f"🔎 M1 trend: {trend}"
        )

    except Exception as e:

        print(
            f"❌ خطأ M1: {e}"
        )

        await query.edit_message_text(

            "❌ حصل خطأ أثناء جلب بيانات M1.\n\n"
            f"{e}",

            reply_markup=main_keyboard()

        )


# =========================================================
# الرجوع للقائمة
# =========================================================

async def back_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    user_chats.pop(
        user_id,
        None
    )

    await query.edit_message_text(

        "🏠 القائمة الرئيسية",

        reply_markup=main_keyboard()

    )


# =========================================================
# رسائل Gemini
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    message = update.message.text

    if user_id not in user_chats:

        await update.message.reply_text(

            "اختر أولًا من القائمة 👇",

            reply_markup=main_keyboard()

        )

        return

    thinking_message = await update.message.reply_text(

        "🧠 Gemini يفكر..."

    )

    try:

        await update.message.chat.send_action(
            action="typing"
        )

        answer = send_gemini_message(

            user_id,

            message

        )

        current_user_model = user_chats[
            user_id
        ]["model"]

        try:

            await thinking_message.delete()

        except Exception:

            pass

        await update.message.reply_text(

            answer +

            f"\n\n🤖 الموديل: "
            f"{current_user_model}",

            reply_markup=back_keyboard()

        )

    except Exception as e:

        print(
            f"❌ Gemini النهائي: {e}"
        )

        try:

            await thinking_message.delete()

        except Exception:

            pass

        await update.message.reply_text(

            "❌ لم أستطع الحصول على رد "
            "من أي موديل Gemini حاليًا.\n\n"

            f"آخر خطأ:\n{e}\n\n"

            "🔄 جرّب إرسال الرسالة مرة أخرى.",

            reply_markup=back_keyboard()

        )


# =========================================================
# تشغيل البوت
# =========================================================

def main():

    if not BOT_TOKEN:

        raise Exception(
            "BOT_TOKEN غير موجود."
        )

    if not GEMINI_API_KEY:

        raise Exception(
            "GEMINI_API_KEY غير موجود."
        )

    if not TWELVE_DATA_API_KEY:

        raise Exception(
            "TWELVE_DATA_API_KEY غير موجود."
        )

    threading.Thread(

        target=run_web_server,

        daemon=True

    ).start()

    app = (
        Application
        .builder()
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

    # Gemini
    app.add_handler(
        CallbackQueryHandler(
            start_gemini_chat,
            pattern="^gemini_chat$"
        )
    )

    # السعر الحالي
    app.add_handler(
        CallbackQueryHandler(
            current_price,
            pattern="^current_price$"
        )
    )

    # M5
    app.add_handler(
        CallbackQueryHandler(
            gold_analysis,
            pattern="^gold_analysis$"
        )
    )

    # M1
    app.add_handler(
        CallbackQueryHandler(
            m1_analysis,
            pattern="^m1_analysis$"
        )
    )

    # الرجوع
    app.add_handler(
        CallbackQueryHandler(
            back_menu,
            pattern="^back_menu$"
        )
    )

    # الرسائل
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print(
        "==================================="
    )

    print(
        "🤖 Gold Bot Started"
    )

    print(
        "🟢 Telegram polling started"
    )

    print(
        "🟢 Flask server started"
    )

    print(
        f"🤖 Current model: {current_model}"
    )

    print(
        "==================================="
    )

    app.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()
