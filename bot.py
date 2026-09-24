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


# ==================================================
# ENVIRONMENT VARIABLES
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")


# ==================================================
# GEMINI CLIENT
# ==================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ==================================================
# GEMINI MODEL PRIORITY
# ==================================================
# هذه الموديلات موجودة في القائمة التي أرسلتها.
#
# إذا النموذج الحالي عليه ضغط أو فشل:
# ينتقل تلقائيًا إلى النموذج التالي.
# ==================================================

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


# النموذج الناجح حاليًا
current_model = None


# محادثات المستخدمين
user_chats = {}


# ==================================================
# FLASK SERVER FOR RENDER
# ==================================================

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


# ==================================================
# MAIN KEYBOARD
# ==================================================

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

    return InlineKeyboardMarkup(
        keyboard
    )


# ==================================================
# BACK KEYBOARD
# ==================================================

def back_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🔙 رجوع للقائمة",
                callback_data="back_menu"
            )
        ]
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# ==================================================
# CHECK TEMPORARY GEMINI ERROR
# ==================================================

def should_switch_model(error):

    error_text = str(
        error
    ).lower()

    temporary_errors = [

        "429",
        "503",
        "500",

        "resource exhausted",
        "overloaded",
        "high demand",

        "temporarily unavailable",
        "service unavailable",
        "unavailable",

        "rate limit",
        "too many requests",

        "deadline exceeded",
        "timeout",

        "internal server error",

    ]

    for word in temporary_errors:

        if word in error_text:
            return True

    return False


# ==================================================
# GET AVAILABLE MODELS
# ==================================================

def get_available_models():

    available = []

    try:

        models = client.models.list()

        for model in models:

            name = getattr(
                model,
                "name",
                None
            )

            if not name:
                continue

            clean_name = name.replace(
                "models/",
                ""
            )

            # نأخذ فقط موديلات Gemini
            if not clean_name.lower().startswith(
                "gemini"
            ):
                continue

            # نستبعد موديلات الصور والصوت وغيرها
            if "image" in clean_name.lower():
                continue

            if "tts" in clean_name.lower():
                continue

            if "live" in clean_name.lower():
                continue

            if "transcribe" in clean_name.lower():
                continue

            if "embedding" in clean_name.lower():
                continue

            if clean_name not in available:

                available.append(
                    clean_name
                )

    except Exception as e:

        print(
            f"⚠️ فشل جلب قائمة الموديلات: {e}"
        )

    return available


# ==================================================
# BUILD MODEL LIST
# ==================================================

def build_model_list():

    models = []

    # أولًا موديلات الأولوية
    for model in MODEL_PRIORITY:

        if model not in models:

            models.append(
                model
            )

    # ثم الموديلات التي تظهر في API
    try:

        available = get_available_models()

        for model in available:

            if model not in models:

                models.append(
                    model
                )

    except Exception:

        pass

    return models


# ==================================================
# CREATE GEMINI CHAT
# ==================================================
# لا نرسل رسالة اختبار.
#
# نكتفي بإنشاء Chat،
# والاختبار الحقيقي يحصل عند إرسال رسالة المستخدم.
# ==================================================

def create_gemini_chat(
    excluded_models=None
):

    global current_model

    if excluded_models is None:

        excluded_models = set()

    models = build_model_list()

    print(
        "\n🔎 قائمة الموديلات:"
    )

    print(
        models
    )

    last_error = None

    for model in models:

        if model in excluded_models:

            continue

        try:

            print(
                f"🔄 إنشاء محادثة باستخدام: {model}"
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

            print(
                "➡️ الانتقال للموديل التالي..."
            )

            continue

    raise Exception(
        "جميع نماذج Gemini فشلت.\n"
        f"آخر خطأ: {last_error}"
    )


# ==================================================
# SEND GEMINI MESSAGE
# ==================================================
# النظام هنا مهم:
#
# 1. يجرب الموديل الحالي.
# 2. إذا فشل بسبب الضغط → يبدله.
# 3. إذا فشل البديل → يبدله مرة أخرى.
# 4. يستمر حتى يجد موديلًا ناجحًا.
# 5. يحفظ الموديل الناجح.
# ==================================================

def send_gemini_message(
    user_id,
    message
):

    global current_model

    # ==============================================
    # إذا المستخدم ليس لديه Chat
    # ==============================================

    if user_id not in user_chats:

        chat, model = create_gemini_chat()

        user_chats[user_id] = {
            "chat": chat,
            "model": model
        }


    chat_info = user_chats[
        user_id
    ]

    chat = chat_info[
        "chat"
    ]

    model = chat_info[
        "model"
    ]


    # ==============================================
    # أول محاولة باستخدام الموديل الحالي
    # ==============================================

    try:

        print(
            f"📤 إرسال الرسالة إلى: {model}"
        )

        response = chat.send_message(
            message
        )

        if response and response.text:

            return response.text

        raise Exception(
            "Gemini لم يرجع نصًا."
        )


    except Exception as first_error:

        print(
            f"❌ فشل الموديل الحالي {model}: "
            f"{first_error}"
        )

        print(
            "🔄 سأبحث عن موديل بديل..."
        )


    # ==============================================
    # الموديل الحالي فشل
    # نحذف المحادثة القديمة
    # ==============================================

    user_chats.pop(
        user_id,
        None
    )


    # ==============================================
    # لا نعيد تجربة الموديل الفاشل
    # ==============================================

    tried_models = {
        model
    }


    models = build_model_list()

    last_error = first_error


    # ==============================================
    # تجربة جميع البدائل
    # ==============================================

    for next_model in models:

        if next_model in tried_models:

            continue

        tried_models.add(
            next_model
        )


        try:

            print(
                f"🔄 أجرب الموديل البديل: "
                f"{next_model}"
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
                    f"✅ انتقلت بنجاح إلى: "
                    f"{next_model}"
                )


                return response.text


            raise Exception(
                "الموديل لم يرجع نصًا."
            )


        except Exception as e:

            last_error = e


            print(
                f"❌ {next_model} فشل: {e}"
            )


            print(
                "➡️ سأنتقل للموديل التالي..."
            )


            continue


    # ==============================================
    # جميع الموديلات فشلت
    # ==============================================

    raise Exception(
        "جميع نماذج Gemini المتاحة "
        "فشلت حاليًا.\n\n"
        f"آخر خطأ: {last_error}"
    )


# ==================================================
# START COMMAND
# ==================================================

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
        "👋 أهلاً بك!\n\n"
        "اختر من القائمة:",
        reply_markup=main_keyboard()
    )


# ==================================================
# MODELS COMMAND
# ==================================================

async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        models = get_available_models()

        if not models:

            await update.message.reply_text(
                "❌ لم أستطع جلب قائمة النماذج."
            )

            return


        text = (
            "📋 الموديلات المتاحة:\n\n"
        )


        for model in models[:50]:

            text += (
                f"• {model}\n"
            )


        if current_model:

            text += (
                "\n🟢 الموديل المستخدم حاليًا:\n"
                f"{current_model}"
            )


        await update.message.reply_text(
            text
        )


    except Exception as e:

        await update.message.reply_text(
            f"❌ حصل خطأ:\n{e}"
        )


# ==================================================
# START GEMINI CHAT
# ==================================================

async def start_gemini(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id


    try:

        await query.edit_message_text(
            "⏳ جاري اختيار موديل Gemini..."
        )


        chat, model = create_gemini_chat()


        user_chats[user_id] = {

            "chat": chat,

            "model": model

        }


        await query.edit_message_text(

            "🟢 أنت الآن في دردشة مع Gemini.\n\n"

            f"🤖 الموديل الحالي:\n"
            f"{model}\n\n"

            "اكتب أي سؤال.\n\n"

            "إذا أصبح الموديل عليه ضغط أو "
            "تعطل، سأنتقل تلقائيًا إلى موديل آخر.\n\n"

            "وعندما تريد الخروج اضغط الزر بالأسفل.",

            reply_markup=back_keyboard()

        )


    except Exception as e:

        await query.edit_message_text(

            "❌ جميع نماذج Gemini غير متاحة حاليًا.\n\n"

            f"{e}",

            reply_markup=main_keyboard()

        )


# ==================================================
# HANDLE TEXT MESSAGE
# ==================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id


    # ==============================================
    # المستخدم داخل Gemini
    # ==============================================

    if user_id in user_chats:

        user_text = update.message.text


        try:

            answer = send_gemini_message(
                user_id,
                user_text
            )


            current_user_model = user_chats[
                user_id
            ][
                "model"
            ]


            await update.message.reply_text(

                answer,

                reply_markup=back_keyboard()

            )


        except Exception as e:

            await update.message.reply_text(

                "❌ لم أستطع الحصول على رد من "
                "أي موديل Gemini حاليًا.\n\n"

                f"{e}",

                reply_markup=back_keyboard()

            )


    # ==============================================
    # المستخدم ليس داخل Gemini
    # ==============================================

    else:

        await update.message.reply_text(

            "اختر أحد الخيارات من القائمة 👇",

            reply_markup=main_keyboard()

        )


# ==================================================
# GOLD PRICE - TWELVE DATA
# ==================================================

def get_gold_price():

    url = (
        "https://api.twelvedata.com/price"
    )


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


# ==================================================
# GOLD ANALYSIS
# ==================================================

async def gold_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


    try:

        data = get_gold_price()


        # ==========================================
        # Twelve Data Error
        # ==========================================

        if (
            "code" in data
            and "message" in data
        ):

            await query.edit_message_text(

                "❌ خطأ من Twelve Data:\n\n"

                f"{data.get('message')}",

                reply_markup=main_keyboard()

            )

            return


        price = data.get(
            "price"
        )


        if price is None:

            await query.edit_message_text(

                "❌ لم أستطع الحصول على سعر الذهب.\n\n"

                f"البيانات:\n{data}",

                reply_markup=main_keyboard()

            )

            return


        await query.edit_message_text(

            "📊 تحليل الذهب\n\n"

            "🥇 XAU/USD\n"

            f"💰 السعر الحالي: {price}\n\n"

            "⏳ السعر الحالي يعمل بنجاح.\n\n"

            "سنضيف لاحقًا:\n"

            "• M5\n"
            "• M1\n"
            "• الاتجاه\n"
            "• الدعم والمقاومة\n"
            "• Entry\n"
            "• Stop Loss\n"
            "• Take Profit",

            reply_markup=main_keyboard()

        )


    except Exception as e:

        await query.edit_message_text(

            "❌ حصل خطأ أثناء جلب سعر الذهب:\n\n"

            f"{e}",

            reply_markup=main_keyboard()

        )


# ==================================================
# BACK TO MENU
# ==================================================

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

        "🏠 القائمة الرئيسية\n\n"

        "اختر ماذا تريد:",

        reply_markup=main_keyboard()

    )


# ==================================================
# BUTTON HANDLER
# ==================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query


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


# ==================================================
# MAIN
# ==================================================

def main():

    # ==============================================
    # التحقق من Environment Variables
    # ==============================================

    if not BOT_TOKEN:

        print(
            "❌ BOT_TOKEN غير موجود"
        )

        return


    if not GEMINI_API_KEY:

        print(
            "❌ GEMINI_API_KEY غير موجود"
        )

        return


    if not TWELVE_DATA_API_KEY:

        print(
            "❌ TWELVE_DATA_API_KEY غير موجود"
        )

        return


    # ==============================================
    # إنشاء Telegram Application
    # ==============================================

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )


    # ==============================================
    # Commands
    # ==============================================

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


    # ==============================================
    # Buttons
    # ==============================================

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )


    # ==============================================
    # Text Messages
    # ==============================================

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )


    # ==============================================
    # Flask Server
    # ==============================================

    threading.Thread(

        target=run_web_server,

        daemon=True

    ).start()


    print(
        "🌐 Flask server started"
    )


    print(
        "🤖 Telegram bot is running..."
    )


    # ==============================================
    # Telegram Polling
    # ==============================================

    app.run_polling()


# ==================================================
# RUN
# ==================================================

if __name__ == "__main__":

    main()
