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
# ENVIRONMENT
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
# SETTINGS
# =========================================================

SYMBOL = "XAU/USD"

# فحص السعر كل 15 ثانية أثناء وجود متابعة
MONITOR_SECONDS = 15

# إعادة التحليل بعد WAIT
WAIT_REANALYZE_SECONDS = 300

# كاش السعر
PRICE_CACHE_SECONDS = 30

# منع أكثر من تحليل في نفس الوقت
analysis_lock = asyncio.Lock()


# =========================================================
# GEMINI
# =========================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)

MODEL_PRIORITY = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]


def get_available_models():

    try:

        models = client.models.list()

        result = []

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

            result.append(name)

        return result

    except Exception as e:

        print(
            "Gemini model list error:",
            e
        )

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
                "Trying Gemini:",
                model_name
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
                return text.strip()

            last_error = Exception(
                f"{model_name} لم يرجع نصًا."
            )

        except Exception as e:

            print(
                f"Gemini error {model_name}:",
                e
            )

            last_error = e

    if last_error:
        raise last_error

    raise Exception(
        "لم يعمل أي موديل Gemini."
    )


# =========================================================
# SQLITE
# =========================================================

DB_FILE = "trades.db"


def db_connect():

    connection = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():

    connection = db_connect()

    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            direction TEXT NOT NULL,

            entry REAL NOT NULL,

            stop_loss REAL NOT NULL,

            take_profit REAL NOT NULL,

            armed_price REAL NOT NULL,

            status TEXT NOT NULL,

            created_at TEXT NOT NULL,

            entered_at TEXT,

            closed_at TEXT,

            exit_price REAL,

            result TEXT,

            pnl_points REAL,

            pnl_percent REAL
        )
    """)

    connection.commit()

    connection.close()


def get_active_trade(user_id):

    connection = db_connect()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM trades
        WHERE user_id = ?
        AND status IN ('waiting_entry', 'in_trade')
        ORDER BY id DESC
        LIMIT 1
    """, (
        user_id,
    ))

    row = cursor.fetchone()

    connection.close()

    if row:
        return dict(row)

    return None


def get_all_active_trades():

    connection = db_connect()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM trades
        WHERE status IN ('waiting_entry', 'in_trade')
        ORDER BY id ASC
    """)

    rows = cursor.fetchall()

    connection.close()

    return [
        dict(row)
        for row in rows
    ]


def create_trade(
    user_id,
    direction,
    entry,
    sl,
    tp,
    armed_price
):

    connection = db_connect()

    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO trades (
            user_id,
            direction,
            entry,
            stop_loss,
            take_profit,
            armed_price,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (

        user_id,
        direction,
        entry,
        sl,
        tp,
        armed_price,
        "waiting_entry",

        datetime.now(
            timezone.utc
        ).isoformat(),

    ))

    trade_id = cursor.lastrowid

    connection.commit()

    connection.close()

    return trade_id


def mark_entered(
    trade_id,
    actual_price
):

    connection = db_connect()

    cursor = connection.cursor()

    cursor.execute("""
        UPDATE trades

        SET status = 'in_trade',
            armed_price = ?,
            entered_at = ?

        WHERE id = ?
    """, (

        actual_price,

        datetime.now(
            timezone.utc
        ).isoformat(),

        trade_id,

    ))

    connection.commit()

    connection.close()


def close_trade(
    trade_id,
    exit_price,
    result,
    pnl_points,
    pnl_percent
):

    connection = db_connect()

    cursor = connection.cursor()

    cursor.execute("""
        UPDATE trades

        SET status = 'closed',
            closed_at = ?,
            exit_price = ?,
            result = ?,
            pnl_points = ?,
            pnl_percent = ?

        WHERE id = ?
    """, (

        datetime.now(
            timezone.utc
        ).isoformat(),

        exit_price,
        result,
        pnl_points,
        pnl_percent,

        trade_id,

    ))

    connection.commit()

    connection.close()


def cancel_user_active_trade(user_id):

    connection = db_connect()

    cursor = connection.cursor()

    cursor.execute("""
        UPDATE trades

        SET status = 'cancelled',
            closed_at = ?

        WHERE user_id = ?

        AND status IN (
            'waiting_entry',
            'in_trade'
        )
    """, (

        datetime.now(
            timezone.utc
        ).isoformat(),

        user_id,

    ))

    connection.commit()

    connection.close()


# =========================================================
# AUTO PILOT STATE
# =========================================================

autopilot_users = set()

user_wait_tasks = {}


# =========================================================
# FLASK
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():

    return "Gold Gemini Bot is running!"


@web_app.route("/health")
def health():

    return {
        "status": "ok"
    }


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
# MARKET STATE
# =========================================================

market_state = {

    "price": None,

    "previous_price": None,

    "updated_at": None,

    "last_5min_report": None,

    "rate_limited_until": 0,

}


def get_cached_price(
    max_age=60
):

    price = market_state["price"]

    updated_at = market_state["updated_at"]

    if price is None:
        return None

    if updated_at is None:
        return None

    age = (
        datetime.now(timezone.utc)
        - updated_at
    ).total_seconds()

    if age <= max_age:
        return price

    return None


def get_gold_price(
    force=False
):

    now = datetime.now(
        timezone.utc
    )

    now_timestamp = now.timestamp()

    # -----------------------------------------------------
    # إذا Twelve Data أعطانا 429
    # لا نكرر الطلبات خلال فترة الحظر
    # -----------------------------------------------------

    if (
        now_timestamp
        <
        market_state["rate_limited_until"]
    ):

        cached = get_cached_price(
            300
        )

        if cached is not None:
            return cached

        raise Exception(
            "Twelve Data rate limit (429). "
            "Retry after 60 seconds."
        )

    # -----------------------------------------------------
    # استخدام الكاش إذا السعر حديث
    # -----------------------------------------------------

    if not force:

        cached = get_cached_price(
            PRICE_CACHE_SECONDS
        )

        if cached is not None:
            return cached

    url = (
        "https://api.twelvedata.com/price"
    )

    params = {

        "symbol": SYMBOL,

        "apikey":
            TWELVE_DATA_API_KEY,

    }

    response = None

    try:

        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        if response.status_code == 429:

            market_state[
                "rate_limited_until"
            ] = (
                datetime.now(
                    timezone.utc
                ).timestamp()
                + 60
            )

            cached = get_cached_price(
                300
            )

            if cached is not None:
                return cached

            raise Exception(
                "Twelve Data rate limit (429). "
                "Retry after 60 seconds."
            )

        response.raise_for_status()

        data = response.json()

        if "price" not in data:

            raise Exception(
                data.get(
                    "message",
                    "لم يصل سعر الذهب."
                )
            )

        price = float(
            data["price"]
        )

        # -------------------------------------------------
        # تحديث السعر السابق والحالي
        # -------------------------------------------------

        market_state[
            "previous_price"
        ] = market_state["price"]

        market_state[
            "price"
        ] = price

        market_state[
            "updated_at"
        ] = datetime.now(
            timezone.utc
        )

        return price

    except requests.HTTPError:

        if (
            response is not None
            and response.status_code == 429
        ):

            market_state[
                "rate_limited_until"
            ] = (
                datetime.now(
                    timezone.utc
                ).timestamp()
                + 60
            )

        raise


# =========================================================
# 5 MINUTE REPORT
# =========================================================

def is_5_minute_report_due():

    now = datetime.now(
        timezone.utc
    )

    slot = (

        now.year,

        now.month,

        now.day,

        now.hour,

        now.minute // 5,

    )

    if (
        market_state[
            "last_5min_report"
        ]
        != slot
    ):

        market_state[
            "last_5min_report"
        ] = slot

        return True

    return False


# =========================================================
# M5
# =========================================================

def get_gold_m5():

    url = (
        "https://api.twelvedata.com/time_series"
    )

    params = {

        "symbol": SYMBOL,

        "interval": "5min",

        "outputsize": 288,

        "order": "asc",

        "timezone": "UTC",

        "apikey":
            TWELVE_DATA_API_KEY,

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
                "خطأ Twelve Data"
            )
        )

    values = data.get(
        "values"
    )

    if not values:

        raise Exception(
            "لم تصل شموع M5."
        )

    return values


# =========================================================
# M1
# =========================================================

def get_gold_m1():

    url = (
        "https://api.twelvedata.com/time_series"
    )

    params = {

        "symbol": SYMBOL,

        "interval": "1min",

        "outputsize": 360,

        "order": "asc",

        "timezone": "UTC",

        "apikey":
            TWELVE_DATA_API_KEY,

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
                "خطأ Twelve Data"
            )
        )

    values = data.get(
        "values"
    )

    if not values:

        raise Exception(
            "لم تصل شموع M1."
        )

    return values


# =========================================================
# FORMAT CANDLES
# =========================================================

def format_candles(
    candles
):

    result = []

    for candle in candles:

        result.append({

            "time":
                candle.get(
                    "datetime"
                ),

            "open":
                candle.get(
                    "open"
                ),

            "high":
                candle.get(
                    "high"
                ),

            "low":
                candle.get(
                    "low"
                ),

            "close":
                candle.get(
                    "close"
                ),

        })

    return result


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup([

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

    ])


def stop_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "⛔ إيقاف التحليل والمتابعة",
                callback_data="stop_all"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 رجوع للقائمة",
                callback_data="back_menu"
            )
        ],

    ])


def restart_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🔄 تشغيل التحليل من جديد",
                callback_data="gemini_analysis"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 القائمة الرئيسية",
                callback_data="back_menu"
            )
        ],

    ])


def active_trade_keyboard(
    trade_id
):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "⛔ إيقاف التحليل والمتابعة",
                callback_data="stop_all"
            )
        ],

    ])


# =========================================================
# EXTRACT GEMINI TRADE
# =========================================================

def extract_trade_from_gemini(
    text
):

    upper = text.upper()

    if re.search(
        r"\bWAIT\b",
        upper
    ):

        return None

    direction_match = re.search(
        r"(?:الاتجاه|DIRECTION)"
        r"\s*[:：\-]?\s*"
        r"(BUY|SELL)",
        text,
        re.IGNORECASE
    )

    if direction_match:

        direction = (
            direction_match
            .group(1)
            .upper()
        )

    elif re.search(
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

    entry_match = re.search(
        r"(?:Entry|الدخول|دخول)"
        r"\s*[:：\-]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE
    )

    sl_match = re.search(
        r"(?:Stop\s*Loss|SL|وقف\s*الخسارة)"
        r"\s*[:：\-]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE
    )

    tp_match = re.search(
        r"(?:Take\s*Profit|TP|جني\s*الربح)"
        r"\s*[:：\-]?\s*"
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

    if direction == "BUY":

        if not (
            sl < entry < tp
        ):

            return None

    elif direction == "SELL":

        if not (
            tp < entry < sl
        ):

            return None

    return {

        "direction":
            direction,

        "entry":
            entry,

        "sl":
            sl,

        "tp":
            tp,

    }


# =========================================================
# GEMINI PROMPT
# =========================================================

def build_gold_prompt(
    price,
    m5,
    m1
):

    return f"""
أنت محلل فني للذهب XAU/USD.

حلل بنفسك بيانات الشموع الخام.

السعر الحالي:
{price}

M5 - آخر 24 ساعة:
{m5}

M1 - آخر 6 ساعات:
{m1}

حلل:

- اتجاه M5
- اتجاه M1
- Market Structure
- Higher High
- Higher Low
- Lower High
- Lower Low
- الزخم
- الدعم
- المقاومة
- الاختراق والرفض
- توافق M1 مع M5

ثم أعطني توصية واحدة فقط.

إذا توجد فرصة واضحة:

📊 الاتجاه: BUY أو SELL
🎯 Entry: رقم واحد فقط
🛑 SL: رقم واحد فقط
💰 TP: رقم واحد فقط
📐 R/R: النسبة

🔥 السبب:
سببان مختصران فقط.

إذا لا توجد فرصة واضحة:

📊 الاتجاه: WAIT
⚠️ السبب: مختصر.

قواعد مهمة جدًا:

- لا تعط أكثر من Entry واحد.
- لا تعط Entry بديل.
- لا تعط منطقة Entry.
- لا تعط Entry ثاني.
- لا تعط SL ثاني.
- لا تعط TP ثاني.
- Entry يجب أن يكون هو مستوى الدخول الوحيد.
- لا تقل تم الدخول.
- لا تقل إن الصفقة مؤكدة.
- لا تضمن الربح.
- لا تكرر الشموع.
- اجعل الرد أقل من 2000 حرف.
"""


# =========================================================
# WAIT RE-ANALYSIS
# =========================================================

async def wait_reanalysis_loop(
    application,
    user_id
):

    try:

        await asyncio.sleep(
            WAIT_REANALYZE_SECONDS
        )

        if user_id not in autopilot_users:
            return

        if get_active_trade(user_id):
            return

        await run_auto_analysis(
            application,
            user_id
        )

    except asyncio.CancelledError:

        return

    except Exception as e:

        print(
            "WAIT REANALYSIS ERROR:",
            e
        )


def schedule_wait_analysis(
    application,
    user_id
):

    old_task = user_wait_tasks.get(
        user_id
    )

    if old_task:

        old_task.cancel()

    task = asyncio.create_task(
        wait_reanalysis_loop(
            application,
            user_id
        )
    )

    user_wait_tasks[user_id] = task


# =========================================================
# SEND AUTOMATIC ANALYSIS
# =========================================================

async def run_auto_analysis(
    application,
    user_id
):

    if user_id not in autopilot_users:
        return

    async with analysis_lock:

        if user_id not in autopilot_users:
            return

        existing = get_active_trade(
            user_id
        )

        if existing:
            return

        try:

            await application.bot.send_message(

                chat_id=user_id,

                text=(
                    "🧠 Gemini يحلل الذهب...\n\n"
                    "📊 M5: آخر 24 ساعة\n"
                    "📉 M1: آخر 6 ساعات"
                )
            )

            # -------------------------------------------------
            # استخدم آخر سعر محفوظ إذا كان حديثًا
            # -------------------------------------------------

            price = get_cached_price(
                60
            )

            if price is None:

                price = get_gold_price()

            # -------------------------------------------------
            # M5 + M1
            # -------------------------------------------------

            m5 = format_candles(
                get_gold_m5()
            )

            m1 = format_candles(
                get_gold_m1()
            )

            prompt = build_gold_prompt(
                price,
                m5,
                m1
            )

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

                await application.bot.send_message(

                    chat_id=user_id,

                    text=(
                        result
                        + "\n\n"
                        + "⏳ لا توجد صفقة الآن."
                        + "\n"
                        + "🔄 سأعيد التحليل بعد 5 دقائق."
                    ),

                    reply_markup=stop_keyboard()
                )

                if user_id in autopilot_users:

                    schedule_wait_analysis(
                        application,
                        user_id
                    )

                return

            # =================================================
            # سعر التسليح
            #
            # نفس سعر التحليل، بدون طلب API إضافي
            # =================================================

            armed_price = price

            # =================================================
            # حفظ الصفقة
            # =================================================

            trade_id = create_trade(

                user_id=user_id,

                direction=
                    trade["direction"],

                entry=
                    trade["entry"],

                sl=
                    trade["sl"],

                tp=
                    trade["tp"],

                armed_price=
                    armed_price
            )

            # =================================================
            # إرسال التوصية
            # =================================================

            message = (

                result

                + "\n\n"

                + "👀 المتابعة مفعلة."

                + "\n"

                + "⏳ الحالة: بانتظار Entry."

                + "\n\n"

                + f"💵 السعر وقت التوصية: "
                f"{armed_price}"

                + "\n\n"

                + "⚠️ لن أعتبر الصفقة داخلة "
                  "إلا عند عبور Entry فعليًا."

                + "\n"

                + "⏱️ سأفحص السعر كل 15 ثانية."

                + "\n"

                + "💰 وسأرسل تحديث السعر كل 5 دقائق."
            )

            await application.bot.send_message(

                chat_id=user_id,

                text=message,

                reply_markup=
                    active_trade_keyboard(
                        trade_id
                    )
            )

        except Exception as e:

            print(
                "AUTO ANALYSIS ERROR:",
                e
            )

            if user_id in autopilot_users:

                await application.bot.send_message(

                    chat_id=user_id,

                    text=(
                        "⚠️ تعذر جلب بيانات الذهب حاليًا.\n\n"
                        f"🔎 السبب:\n{e}\n\n"
                        "🔄 سأحاول مرة أخرى لاحقًا."
                    ),

                    reply_markup=stop_keyboard()
                )

                # إذا كان الخطأ 429
                # لا نحاول كل ثانية.
                # ننتظر 5 دقائق.

                schedule_wait_analysis(
                    application,
                    user_id
                )


# =========================================================
# PROFIT / LOSS
# =========================================================

def calculate_pnl(
    direction,
    entry,
    exit_price
):

    if direction == "BUY":

        points = (
            exit_price
            - entry
        )

    else:

        points = (
            entry
            - exit_price
        )

    percent = (
        points
        / entry
    ) * 100

    return points, percent


# =========================================================
# TRADE MONITOR
# =========================================================

async def monitor_trades(
    application
):

    print(
        "TRADE MONITOR STARTED"
    )

    while True:

        try:

            trades = get_all_active_trades()

            # -------------------------------------------------
            # لا توجد صفقات:
            # لا نطلب السعر
            # -------------------------------------------------

            if not trades:

                await asyncio.sleep(
                    MONITOR_SECONDS
                )

                continue

            # -------------------------------------------------
            # السعر السابق قبل الطلب
            # -------------------------------------------------

            previous_price = (
                market_state["price"]
            )

            # -------------------------------------------------
            # طلب سعر واحد فقط
            # كل 15 ثانية
            # -------------------------------------------------

            try:

                current_price = get_gold_price(
                    force=True
                )

            except Exception as e:

                print(
                    "MONITOR PRICE ERROR:",
                    e
                )

                await asyncio.sleep(
                    MONITOR_SECONDS
                )

                continue

            # -------------------------------------------------
            # هل وصلنا لنهاية فترة 5 دقائق؟
            #
            # لا يوجد طلب API إضافي.
            # نستخدم current_price نفسه.
            # -------------------------------------------------

            five_minute_report = (
                is_5_minute_report_due()
            )

            # -------------------------------------------------
            # معالجة الصفقات
            # -------------------------------------------------

            for trade in trades:

                trade_id = trade["id"]

                user_id = trade["user_id"]

                # إذا أوقف المستخدم المتابعة
                if user_id not in autopilot_users:

                    continue

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

                # =================================================
                # تحديث كل 5 دقائق
                # =================================================

                if five_minute_report:

                    if status == "waiting_entry":

                        status_text = (
                            "⏳ بانتظار Entry"
                        )

                    elif status == "in_trade":

                        status_text = (
                            "🔥 داخل الصفقة"
                        )

                    else:

                        status_text = status

                    await application.bot.send_message(

                        chat_id=user_id,

                        text=(
                            "⏱️ تحديث الذهب كل 5 دقائق\n\n"
                            f"💵 السعر: "
                            f"{current_price}\n"
                            f"📊 الاتجاه: "
                            f"{direction}\n"
                            f"🎯 Entry: "
                            f"{entry}\n"
                            f"🛑 SL: "
                            f"{sl}\n"
                            f"💰 TP: "
                            f"{tp}\n"
                            f"📌 الحالة: "
                            f"{status_text}"
                        )
                    )

                # =================================================
                # WAITING ENTRY
                # =================================================

                if status == "waiting_entry":

                    entered = False

                    # -------------------------------------------------
                    # BUY
                    #
                    # ندخل فقط عندما:
                    #
                    # السابق تحت Entry
                    # والحالي وصل Entry أو تجاوزه
                    #
                    # مثال:
                    # 4199 -> 4201
                    # Entry = 4200
                    #
                    # الدخول الفعلي = 4201
                    # -------------------------------------------------

                    if direction == "BUY":

                        if (
                            previous_price is not None
                            and previous_price < entry
                            and current_price >= entry
                        ):

                            entered = True

                    # -------------------------------------------------
                    # SELL
                    #
                    # السابق فوق Entry
                    # والحالي وصل Entry أو نزل تحته
                    #
                    # مثال:
                    # 4201 -> 4199
                    # Entry = 4200
                    # -------------------------------------------------

                    elif direction == "SELL":

                        if (
                            previous_price is not None
                            and previous_price > entry
                            and current_price <= entry
                        ):

                            entered = True

                    # -------------------------------------------------
                    # دخول فعلي
                    # -------------------------------------------------

                    if entered:

                        mark_entered(
                            trade_id,
                            current_price
                        )

                        await application.bot.send_message(

                            chat_id=user_id,

                            text=(
                                "🔥 دخلنا الصفقة\n\n"

                                f"📊 الاتجاه: "
                                f"{direction}\n"

                                f"🎯 Entry المخطط: "
                                f"{entry}\n"

                                f"💵 سعر الدخول الفعلي: "
                                f"{current_price}\n"

                                f"🛑 SL: "
                                f"{sl}\n"

                                f"💰 TP: "
                                f"{tp}\n\n"

                                "🔥 الحالة: داخل الصفقة.\n"
                                "👀 أراقب SL وTP."
                            ),

                            reply_markup=
                                active_trade_keyboard(
                                    trade_id
                                )
                        )

                        continue

                # =================================================
                # IN TRADE
                # =================================================

                if status == "in_trade":

                    hit_tp = False

                    hit_sl = False

                    # -------------------------------------------------
                    # فحص السعر الحالي
                    # -------------------------------------------------

                    if direction == "BUY":

                        if current_price >= tp:

                            hit_tp = True

                        elif current_price <= sl:

                            hit_sl = True

                    elif direction == "SELL":

                        if current_price <= tp:

                            hit_tp = True

                        elif current_price >= sl:

                            hit_sl = True

                    # =================================================
                    # TP
                    # =================================================

                    if hit_tp:

                        exit_price = tp

                        points, percent = (
                            calculate_pnl(
                                direction,
                                entry,
                                exit_price
                            )
                        )

                        close_trade(

                            trade_id,

                            exit_price,

                            "TP",

                            points,

                            percent
                        )

                        await application.bot.send_message(

                            chat_id=user_id,

                            text=(
                                "🎯 تحقق Take Profit\n\n"

                                f"📊 الاتجاه: "
                                f"{direction}\n"

                                f"🎯 Entry: "
                                f"{entry}\n"

                                f"💵 الخروج: "
                                f"{exit_price}\n"

                                f"📈 النتيجة: "
                                f"{points:+.2f} نقطة\n"

                                f"📊 النسبة: "
                                f"{percent:+.2f}%\n\n"

                                "✅ انتهت الصفقة.\n"
                                "🧠 سأبدأ تحليلًا جديدًا."
                            )
                        )

                        if user_id in autopilot_users:

                            await asyncio.sleep(3)

                            asyncio.create_task(

                                run_auto_analysis(

                                    application,

                                    user_id

                                )

                            )

                    # =================================================
                    # SL
                    # =================================================

                    elif hit_sl:

                        exit_price = sl

                        points, percent = (
                            calculate_pnl(
                                direction,
                                entry,
                                exit_price
                            )
                        )

                        close_trade(

                            trade_id,

                            exit_price,

                            "SL",

                            points,

                            percent
                        )

                        await application.bot.send_message(

                            chat_id=user_id,

                            text=(
                                "🛑 تحقق Stop Loss\n\n"

                                f"📊 الاتجاه: "
                                f"{direction}\n"

                                f"🎯 Entry: "
                                f"{entry}\n"

                                f"💵 الخروج: "
                                f"{exit_price}\n"

                                f"📉 النتيجة: "
                                f"{points:+.2f} نقطة\n"

                                f"📊 النسبة: "
                                f"{percent:+.2f}%\n\n"

                                "❌ انتهت الصفقة.\n"
                                "🧠 سأبدأ تحليلًا جديدًا."
                            )
                        )

                        if user_id in autopilot_users:

                            await asyncio.sleep(3)

                            asyncio.create_task(

                                run_auto_analysis(

                                    application,

                                    user_id

                                )

                            )

        except Exception as e:

            print(
                "MONITOR LOOP ERROR:",
                e
            )

        # -------------------------------------------------
        # الانتظار 15 ثانية
        # -------------------------------------------------

        await asyncio.sleep(
            MONITOR_SECONDS
        )


# =========================================================
# START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    autopilot_users.discard(
        user_id
    )

    old_task = user_wait_tasks.pop(
        user_id,
        None
    )

    if old_task:
        old_task.cancel()

    await update.message.reply_text(

        "🤖 بوت الذهب جاهز.\n\n"
        "اضغط «تحليل Gemini» لبدء "
        "التحليل والمتابعة التلقائية.",

        reply_markup=main_keyboard()
    )


# =========================================================
# MODELS
# =========================================================

async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    models = build_model_list()

    text = (
        "🤖 موديلات Gemini:\n\n"
        + "\n".join(
            f"• {model}"
            for model in models
        )
    )

    await update.message.reply_text(
        text
    )


# =========================================================
# CHAT
# =========================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        text = update.message.text

        await update.message.reply_text(
            "🧠 Gemini يفكر..."
        )

        result = send_gemini_message(

            f"""
أجب على المستخدم بالعربية
وباختصار.

رسالة المستخدم:
{text}
"""
        )

        await update.message.reply_text(
            result,
            reply_markup=main_keyboard()
        )

    except Exception as e:

        print(
            "CHAT ERROR:",
            e
        )

        await update.message.reply_text(

            "❌ حدث خطأ أثناء الاتصال بـ Gemini.",

            reply_markup=main_keyboard()
        )


# =========================================================
# CURRENT PRICE
# =========================================================

async def show_current_price(
    update: Update
):

    query = update.callback_query

    await query.answer()

    try:

        # استخدم الكاش أولًا
        price = get_cached_price(
            30
        )

        if price is None:

            price = get_gold_price()

        await query.edit_message_text(

            "💰 السعر الحالي XAU/USD:\n\n"
            f"💵 {price}",

            reply_markup=main_keyboard()
        )

    except Exception as e:

        print(
            "PRICE ERROR:",
            e
        )

        await query.edit_message_text(

            (
                "❌ تعذر الحصول على السعر.\n\n"
                f"🔎 {e}"
            ),

            reply_markup=main_keyboard()
        )


# =========================================================
# M5 ANALYSIS
# =========================================================

async def show_m5_analysis(
    update: Update
):

    query = update.callback_query

    await query.answer()

    try:

        await query.edit_message_text(
            "⏳ جاري تحليل M5..."
        )

        price = get_cached_price(
            30
        )

        if price is None:
            price = get_gold_price()

        candles = format_candles(
            get_gold_m5()
        )

        prompt = f"""
حلل XAU/USD على M5.

السعر:
{price}

الشموع:
{candles}

ركز على:

الاتجاه،
Market Structure،
الدعم،
المقاومة،
الزخم،
الاختراق والرفض.

رد مختصر جدًا مناسب لـTelegram.

لا تعط توصية دخول.
لا تكرر الشموع.
"""

        result = send_gemini_message(
            prompt
        )

        await query.edit_message_text(

            result,

            reply_markup=main_keyboard()
        )

    except Exception as e:

        print(
            "M5 ERROR:",
            e
        )

        await query.edit_message_text(

            (
                "❌ حدث خطأ في تحليل M5.\n\n"
                f"🔎 {e}"
            ),

            reply_markup=main_keyboard()
        )


# =========================================================
# M1 ANALYSIS
# =========================================================

async def show_m1_analysis(
    update: Update
):

    query = update.callback_query

    await query.answer()

    try:

        await query.edit_message_text(
            "⏳ جاري تحليل M1..."
        )

        price = get_cached_price(
            30
        )

        if price is None:
            price = get_gold_price()

        candles = format_candles(
            get_gold_m1()
        )

        prompt = f"""
حلل XAU/USD على M1.

السعر:
{price}

الشموع:
{candles}

ركز على:

الاتجاه،
Market Structure،
الدعم،
المقاومة،
الزخم،
الاختراق والرفض.

رد مختصر جدًا مناسب لـTelegram.

لا تعط توصية دخول.
لا تكرر الشموع.
"""

        result = send_gemini_message(
            prompt
        )

        await query.edit_message_text(

            result,

            reply_markup=main_keyboard()
        )

    except Exception as e:

        print(
            "M1 ERROR:",
            e
        )

        await query.edit_message_text(

            (
                "❌ حدث خطأ في تحليل M1.\n\n"
                f"🔎 {e}"
            ),

            reply_markup=main_keyboard()
        )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    data = query.data

    user_id = update.effective_user.id

    # =====================================================
    # BACK MENU
    # =====================================================

    if data == "back_menu":

        autopilot_users.discard(
            user_id
        )

        cancel_user_active_trade(
            user_id
        )

        old_task = user_wait_tasks.pop(
            user_id,
            None
        )

        if old_task:
            old_task.cancel()

        await query.edit_message_text(

            "🤖 القائمة الرئيسية:",

            reply_markup=main_keyboard()
        )

        return

    # =====================================================
    # START AUTO ANALYSIS
    # =====================================================

    if data == "gemini_analysis":

        autopilot_users.add(
            user_id
        )

        old_task = user_wait_tasks.pop(
            user_id,
            None
        )

        if old_task:
            old_task.cancel()

        existing = get_active_trade(
            user_id
        )

        if existing:

            await query.edit_message_text(

                (
                    "👀 المتابعة مفعلة أصلًا.\n\n"

                    f"📊 الاتجاه: "
                    f"{existing['direction']}\n"

                    f"🎯 Entry: "
                    f"{existing['entry']}\n"

                    f"🛑 SL: "
                    f"{existing['stop_loss']}\n"

                    f"💰 TP: "
                    f"{existing['take_profit']}\n\n"

                    f"📌 الحالة: "
                    f"{existing['status']}"
                ),

                reply_markup=
                    active_trade_keyboard(
                        existing["id"]
                    )
            )

            return

        await query.edit_message_text(

            "🚀 تم تشغيل التحليل التلقائي.\n\n"
            "🧠 Gemini سيحلل الآن...",

            reply_markup=stop_keyboard()
        )

        asyncio.create_task(

            run_auto_analysis(
                context.application,
                user_id
            )

        )

        return

    # =====================================================
    # STOP EVERYTHING
    # =====================================================

    if data == "stop_all":

        autopilot_users.discard(
            user_id
        )

        cancel_user_active_trade(
            user_id
        )

        old_task = user_wait_tasks.pop(
            user_id,
            None
        )

        if old_task:
            old_task.cancel()

        await query.edit_message_text(

            "⛔ تم إيقاف التحليل والمتابعة بالكامل.\n\n"
            "تم إلغاء أي صفقة كانت قيد المتابعة.\n"
            "لن يبدأ تحليل جديد حتى تضغط تشغيل.",

            reply_markup=restart_keyboard()
        )

        return

    # =====================================================
    # CURRENT PRICE
    # =====================================================

    if data == "current_price":

        await show_current_price(
            update
        )

        return

    # =====================================================
    # M5
    # =====================================================

    if data == "gold_analysis":

        await show_m5_analysis(
            update
        )

        return

    # =====================================================
    # M1
    # =====================================================

    if data == "m1_analysis":

        await show_m1_analysis(
            update
        )

        return

    # =====================================================
    # CHAT
    # =====================================================

    if data == "gemini_chat":

        await query.edit_message_text(

            "🟢 دردشة Gemini مفعلة.\n\n"
            "اكتب رسالتك.",

            reply_markup=main_keyboard()
        )

        return


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application
):

    print(
        "Starting trade monitor..."
    )

    asyncio.create_task(

        monitor_trades(
            application
        )

    )


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "================================="
    )

    print(
        "Starting Gold Gemini Bot"
    )

    print(
        "================================="
    )

    init_database()

    # -----------------------------------------------------
    # Flask
    # -----------------------------------------------------

    threading.Thread(

        target=run_web_server,

        daemon=True

    ).start()

    # -----------------------------------------------------
    # Telegram
    # -----------------------------------------------------

    application = (

        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()

    )

    # -----------------------------------------------------
    # Commands
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # Buttons
    # -----------------------------------------------------

    application.add_handler(

        CallbackQueryHandler(
            button_handler
        )

    )

    # -----------------------------------------------------
    # Chat
    # -----------------------------------------------------

    application.add_handler(

        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text
        )

    )

    print(
        "BOT IS RUNNING..."
    )

    application.run_polling(
        drop_pending_updates=False
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()
