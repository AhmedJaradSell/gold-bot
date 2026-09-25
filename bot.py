import os
import re
import asyncio
import threading
import sqlite3
from datetime import datetime, timezone

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
# ENV
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
# GEMINI
# =========================================================

client = genai.Client(api_key=GEMINI_API_KEY)

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


def get_available_models():

    try:

        models = client.models.list()

        available = []

        for model in models:

            name = getattr(model, "name", "")

            if not name:
                continue

            if name.startswith("models/"):
                name = name.replace("models/", "", 1)

            available.append(name)

        return available

    except Exception as e:

        print("Model list error:", e)

        return []


def build_model_list():

    available = get_available_models()

    if not available:
        return MODEL_PRIORITY

    result = [
        model
        for model in MODEL_PRIORITY
        if model in available
    ]

    return result or MODEL_PRIORITY


def send_gemini_message(prompt):

    models = build_model_list()

    last_error = None

    for model_name in models:

        try:

            print(
                f"Trying Gemini model: {model_name}"
            )

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

                print(
                    f"Gemini success: {model_name}"
                )

                return text

            last_error = Exception(
                f"{model_name} لم يرجع نصًا."
            )

        except Exception as e:

            print(
                f"Gemini failed {model_name}: {e}"
            )

            last_error = e

            continue

    if last_error:
        raise last_error

    raise Exception(
        "فشل الاتصال بجميع موديلات Gemini."
    )


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "trades.db"


def init_database():

    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            entry REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            entered_at TEXT,
            closed_at TEXT,
            exit_price REAL,
            result TEXT
        )
    """)

    connection.commit()
    connection.close()


def save_trade(user_id, trade):

    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO trades (
            user_id,
            direction,
            entry,
            stop_loss,
            take_profit,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        trade["direction"],
        trade["entry"],
        trade["sl"],
        trade["tp"],
        "waiting_entry",
        datetime.now(timezone.utc).isoformat(),
    ))

    trade_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return trade_id


def get_active_trades():

    connection = sqlite3.connect(DB_FILE)

    connection.row_factory = sqlite3.Row

    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM trades
        WHERE status IN ('waiting_entry', 'in_trade')
    """)

    rows = cursor.fetchall()

    connection.close()

    return [dict(row) for row in rows]


def update_trade_entered(trade_id):

    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute("""
        UPDATE trades
        SET status = 'in_trade',
            entered_at = ?
        WHERE id = ?
    """, (
        datetime.now(timezone.utc).isoformat(),
        trade_id,
    ))

    connection.commit()
    connection.close()


def close_trade(
    trade_id,
    exit_price,
    result
):

    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute("""
        UPDATE trades
        SET status = 'closed',
            closed_at = ?,
            exit_price = ?,
            result = ?
        WHERE id = ?
    """, (
        datetime.now(timezone.utc).isoformat(),
        exit_price,
        result,
        trade_id,
    ))

    connection.commit()
    connection.close()


def cancel_trade(trade_id):

    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute("""
        UPDATE trades
        SET status = 'cancelled',
            closed_at = ?
        WHERE id = ?
    """, (
        datetime.now(timezone.utc).isoformat(),
        trade_id,
    ))

    connection.commit()
    connection.close()


# =========================================================
# FLASK
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
# GOLD PRICE
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
# GOLD M5
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
# GOLD M1
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
# FORMAT CANDLES
# =========================================================

def format_candles(candles):

    result = []

    for candle in candles:

        result.append({
            "time": candle.get("datetime"),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
        })

    return result


# =========================================================
# TELEGRAM KEYBOARDS
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
        ]

    ]

    return InlineKeyboardMarkup(keyboard)


def back_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔙 رجوع للقائمة",
                callback_data="back_menu"
            )
        ]
    ])


def new_analysis_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔄 تحليل صفقة جديدة",
                callback_data="gemini_analysis"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 القائمة الرئيسية",
                callback_data="back_menu"
            )
        ]
    ])


def active_trade_keyboard(trade_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⛔ إلغاء المتابعة",
                callback_data=f"cancel_trade_{trade_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 القائمة الرئيسية",
                callback_data="back_menu"
            )
        ]
    ])


# =========================================================
# GEMINI CHAT
# =========================================================

user_chats = {}


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🤖 بوت الذهب جاهز.\n\n"
        "اختر من القائمة:",
        reply_markup=main_keyboard()
    )


async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    models = build_model_list()

    text = "🤖 موديلات Gemini المتاحة:\n\n"

    for model in models:

        text += f"• {model}\n"

    await update.message.reply_text(text)


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    text = update.message.text

    if user_id not in user_chats:

        user_chats[user_id] = []

    try:

        await update.message.reply_text(
            "🧠 Gemini يفكر..."
        )

        prompt = (
            "أنت مساعد ذكي.\n"
            "أجب على رسالة المستخدم باختصار "
            "وبلغة عربية واضحة.\n\n"
            f"رسالة المستخدم:\n{text}"
        )

        result = send_gemini_message(prompt)

        await update.message.reply_text(
            result,
            reply_markup=back_keyboard()
        )

    except Exception as e:

        print("Chat error:", e)

        await update.message.reply_text(
            "❌ حدث خطأ أثناء الاتصال بـ Gemini."
        )


# =========================================================
# EXTRACT TRADE
# =========================================================

def extract_trade_from_gemini(text):

    upper = text.upper()

    if re.search(
        r"\bWAIT\b",
        upper
    ):

        return None

    direction_match = re.search(
        r"(?:الاتجاه|DIRECTION)\s*[:：\-]?\s*"
        r"(BUY|SELL)",
        text,
        re.IGNORECASE
    )

    if not direction_match:

        if re.search(
            r"\bBUY\b",
            upper
        ):

            direction = "BUY"

        elif re.search(
            r"\bSELL\b",
            upper
        ):

            direction = "SELL"

        else:

            return None

    else:

        direction = (
            direction_match
            .group(1)
            .upper()
        )

    entry_match = re.search(
        r"(?:Entry|الدخول|دخول)\s*"
        r"[:：\-]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE
    )

    sl_match = re.search(
        r"(?:Stop\s*Loss|SL|وقف\s*الخسارة)\s*"
        r"[:：\-]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE
    )

    tp_match = re.search(
        r"(?:Take\s*Profit|TP|جني\s*الربح)\s*"
        r"[:：\-]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE
    )

    if not entry_match:
        return None

    if not sl_match:
        return None

    if not tp_match:
        return None

    entry = float(
        entry_match.group(1)
    )

    sl = float(
        sl_match.group(1)
    )

    tp = float(
        tp_match.group(1)
    )

    # BUY:
    # SL < Entry < TP

    if direction == "BUY":

        if not (
            sl < entry < tp
        ):

            print(
                "Invalid BUY levels:",
                entry,
                sl,
                tp
            )

            return None

    # SELL:
    # TP < Entry < SL

    elif direction == "SELL":

        if not (
            tp < entry < sl
        ):

            print(
                "Invalid SELL levels:",
                entry,
                sl,
                tp
            )

            return None

    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
    }


# =========================================================
# GEMINI GOLD ANALYSIS
# =========================================================

async def gemini_gold_analysis(
    update: Update
):

    query = update.callback_query

    await query.answer()

    user_id = update.effective_user.id

    try:

        await query.edit_message_text(
            "⏳ أجمع بيانات الذهب...\n\n"
            "M5 آخر 24 ساعة\n"
            "M1 آخر 6 ساعات"
        )

        price = get_gold_price()

        m5 = get_gold_m5()

        m1 = get_gold_m1()

        m5_candles = format_candles(m5)

        m1_candles = format_candles(m1)

        prompt = f"""
أنت محلل فني للذهب XAU/USD.

حلل بنفسك بيانات الشموع الخام التي سأرسلها لك.

السعر الحالي:
{price}

M5 - آخر 24 ساعة:
{m5_candles}

M1 - آخر 6 ساعات:
{m1_candles}

مهم جدًا:

لا يوجد تحليل مسبق من البوت.

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

- استخدم بالضبط BUY أو SELL أو WAIT.
- Entry وSL وTP يجب أن تكون مبنية على تحليل الشموع.
- لا تعطِ أكثر من Entry واحد.
- لا تعطِ أكثر من Stop Loss واحد.
- لا تعطِ أكثر من Take Profit واحد.
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

        result = send_gemini_message(
            prompt
        )

        trade = extract_trade_from_gemini(
            result
        )

        # =================================================
        # WAIT
        # =================================================

        if trade is None:

            await query.edit_message_text(
                result,
                reply_markup=new_analysis_keyboard()
            )

            return

        # =================================================
        # Check existing active trade
        # =================================================

        existing = get_active_trades()

        user_existing = [
            t for t in existing
            if t["user_id"] == user_id
        ]

        if user_existing:

            active = user_existing[0]

            extra = (
                "\n\n"
                "⚠️ لديك صفقة قيد المتابعة حاليًا.\n"
                "لن أضيف توصية جديدة حتى تنتهي الصفقة الحالية."
            )

            await query.edit_message_text(
                result + extra,
                reply_markup=active_trade_keyboard(
                    active["id"]
                )
            )

            return

        # =================================================
        # Save trade
        # =================================================

        trade_id = save_trade(
            user_id,
            trade
        )

        result += (
            "\n\n"
            "👀 تمت إضافة التوصية للمتابعة تلقائيًا."
            "\n\n"
            "⏳ الحالة: بانتظار الوصول إلى Entry."
        )

        await query.edit_message_text(
            result,
            reply_markup=active_trade_keyboard(
                trade_id
            )
        )

    except Exception as e:

        print(
            "Gold analysis error:",
            e
        )

        await query.edit_message_text(
            "❌ حدث خطأ أثناء تحليل الذهب.\n\n"
            f"التفاصيل: {str(e)[:500]}",
            reply_markup=back_keyboard()
        )


# =========================================================
# CURRENT PRICE
# =========================================================

async def current_price(
    update: Update
):

    query = update.callback_query

    await query.answer()

    try:

        price = get_gold_price()

        await query.edit_message_text(
            "💰 السعر الحالي للذهب XAU/USD:\n\n"
            f"💵 {price}",
            reply_markup=back_keyboard()
        )

    except Exception as e:

        print(
            "Price error:",
            e
        )

        await query.edit_message_text(
            "❌ لم أستطع الحصول على السعر.",
            reply_markup=back_keyboard()
        )


# =========================================================
# M5 ANALYSIS
# =========================================================

async def gold_m5_analysis(
    update: Update
):

    query = update.callback_query

    await query.answer()

    try:

        await query.edit_message_text(
            "⏳ جاري تجهيز بيانات M5..."
        )

        candles = get_gold_m5()

        formatted = format_candles(
            candles
        )

        prompt = f"""
حلل الذهب XAU/USD على فريم M5.

هذه جميع شموع M5 الخام لآخر 24 ساعة:

{formatted}

حلل بنفسك:
- الاتجاه
- بنية السوق
- الدعم
- المقاومة
- الزخم
- الاختراقات والرفض

أعطني ملخصًا قصيرًا مناسبًا لـTelegram.

لا تكرر بيانات الشموع.
"""

        result = send_gemini_message(
            prompt
        )

        await query.edit_message_text(
            result,
            reply_markup=back_keyboard()
        )

    except Exception as e:

        print(
            "M5 error:",
            e
        )

        await query.edit_message_text(
            "❌ حدث خطأ في تحليل M5.",
            reply_markup=back_keyboard()
        )


# =========================================================
# M1 ANALYSIS
# =========================================================

async def gold_m1_analysis(
    update: Update
):

    query = update.callback_query

    await query.answer()

    try:

        await query.edit_message_text(
            "⏳ جاري تجهيز بيانات M1..."
        )

        candles = get_gold_m1()

        formatted = format_candles(
            candles
        )

        prompt = f"""
حلل الذهب XAU/USD على فريم M1.

هذه جميع شموع M1 الخام لآخر 6 ساعات:

{formatted}

حلل بنفسك:
- الاتجاه
- بنية السوق
- الدعم
- المقاومة
- الزخم
- الاختراقات والرفض

أعطني ملخصًا قصيرًا مناسبًا لـTelegram.

لا تكرر بيانات الشموع.
"""

        result = send_gemini_message(
            prompt
        )

        await query.edit_message_text(
            result,
            reply_markup=back_keyboard()
        )

    except Exception as e:

        print(
            "M1 error:",
            e
        )

        await query.edit_message_text(
            "❌ حدث خطأ في تحليل M1.",
            reply_markup=back_keyboard()
        )


# =========================================================
# CALLBACKS
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    data = query.data

    if data == "back_menu":

        await query.answer()

        await query.edit_message_text(
            "🤖 القائمة الرئيسية:",
            reply_markup=main_keyboard()
        )

        return

    if data == "current_price":

        await current_price(update)

        return

    if data == "gold_analysis":

        await gold_m5_analysis(update)

        return

    if data == "m1_analysis":

        await gold_m1_analysis(update)

        return

    if data == "gemini_analysis":

        await gemini_gold_analysis(update)

        return

    if data == "gemini_chat":

        await query.answer()

        await query.edit_message_text(
            "🟢 دردشة Gemini مفعلة.\n\n"
            "اكتب رسالتك الآن.",
            reply_markup=back_keyboard()
        )

        return

    if data.startswith("cancel_trade_"):

        await query.answer()

        try:

            trade_id = int(
                data.replace(
                    "cancel_trade_",
                    ""
                )
            )

            cancel_trade(
                trade_id
            )

            await query.edit_message_text(
                "⛔ تم إلغاء متابعة الصفقة.",
                reply_markup=new_analysis_keyboard()
            )

        except Exception as e:

            print(
                "Cancel trade error:",
                e
            )

            await query.edit_message_text(
                "❌ حدث خطأ أثناء إلغاء المتابعة.",
                reply_markup=back_keyboard()
            )

        return


# =========================================================
# TRADE MONITOR
# =========================================================

async def monitor_trades(
    application
):

    print(
        "Trade monitor started."
    )

    while True:

        try:

            trades = get_active_trades()

            if not trades:

                await asyncio.sleep(15)

                continue

            price = float(
                get_gold_price()
            )

            for trade in trades:

                trade_id = trade["id"]

                user_id = trade["user_id"]

                direction = trade["direction"]

                entry = float(
                    trade["entry"]
                )

                sl = float(
                    trade["stop_loss"]
                )

                tp = float(
                    trade["take_profit"]
                )

                status = trade["status"]

                # =========================================
                # WAITING ENTRY
                # =========================================

                if status == "waiting_entry":

                    entered = False

                    if direction == "BUY":

                        if price >= entry:

                            entered = True

                    elif direction == "SELL":

                        if price <= entry:

                            entered = True

                    if entered:

                        update_trade_entered(
                            trade_id
                        )

                        await application.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "✅ تم الدخول\n\n"
                                f"📊 الاتجاه: {direction}\n"
                                f"💵 سعر الدخول: {price}\n"
                                f"🛑 SL: {sl}\n"
                                f"🎯 TP: {tp}\n\n"
                                "👀 بدأت متابعة الصفقة."
                            ),
                            reply_markup=active_trade_keyboard(
                                trade_id
                            )
                        )

                        continue

                # =========================================
                # IN TRADE
                # =========================================

                if status == "in_trade":

                    hit_sl = False

                    hit_tp = False

                    if direction == "BUY":

                        if price <= sl:

                            hit_sl = True

                        elif price >= tp:

                            hit_tp = True

                    elif direction == "SELL":

                        if price >= sl:

                            hit_sl = True

                        elif price <= tp:

                            hit_tp = True

                    # =====================================
                    # STOP LOSS
                    # =====================================

                    if hit_sl:

                        close_trade(
                            trade_id,
                            price,
                            "SL"
                        )

                        await application.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "🛑 تم ضرب وقف الخسارة\n\n"
                                f"📊 الاتجاه: {direction}\n"
                                f"🎯 Entry: {entry}\n"
                                f"💵 الإغلاق: {price}\n"
                                f"🛑 SL: {sl}\n"
                                f"🎯 TP: {tp}\n\n"
                                "❌ انتهت الصفقة."
                            ),
                            reply_markup=new_analysis_keyboard()
                        )

                    # =====================================
                    # TAKE PROFIT
                    # =====================================

                    elif hit_tp:

                        close_trade(
                            trade_id,
                            price,
                            "TP"
                        )

                        await application.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "🎯 تحقق الهدف\n\n"
                                f"📊 الاتجاه: {direction}\n"
                                f"🎯 Entry: {entry}\n"
                                f"💵 الإغلاق: {price}\n"
                                f"🛑 SL: {sl}\n"
                                f"🎯 TP: {tp}\n\n"
                                "✅ انتهت الصفقة."
                            ),
                            reply_markup=new_analysis_keyboard()
                        )

        except Exception as e:

            print(
                "Trade monitor error:",
                e
            )

        await asyncio.sleep(15)


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "Starting Gold Gemini Bot..."
    )

    init_database()

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "models",
            models_command
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text
        )
    )

    async def start_monitor(
        app
    ):

        asyncio.create_task(
            monitor_trades(app)
        )

    application.post_init = start_monitor

    print(
        "Bot is running..."
    )

    application.run_polling()


if __name__ == "__main__":

    main()
