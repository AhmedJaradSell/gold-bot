import os
import re
import asyncio
import threading
import sqlite3
import time
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
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود في Environment Variables")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY غير موجود في Environment Variables")

if not TWELVE_DATA_API_KEY:
    raise RuntimeError("TWELVE_DATA_API_KEY غير موجود في Environment Variables")


client = genai.Client(api_key=GEMINI_API_KEY)


# =========================================================
# GEMINI MODELS
# =========================================================

MODEL_PRIORITY = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]


# =========================================================
# SETTINGS
# =========================================================

SYMBOL = "XAU/USD"

MONITOR_SECONDS = 15

# إعادة التحليل بعد 5 دقائق
WAIT_REANALYZE_SECONDS = 300

# نبضة حالة كل 5 دقائق
STATUS_UPDATE_SECONDS = 300

# مدة حفظ السعر في الكاش
PRICE_CACHE_SECONDS = 15

# عند 429
RATE_LIMIT_BACKOFF = [15, 30, 60]


# =========================================================
# GLOBAL STATE
# =========================================================

autopilot_users = set()

user_wait_tasks = {}
user_status_tasks = {}

analysis_lock = asyncio.Lock()

last_price = None

# cache للسعر
price_cache = {
    "price": None,
    "timestamp": 0,
}

# منع إرسال طلبات كثيرة إلى Twelve Data في نفس الوقت
price_lock = asyncio.Lock()

# وقت آخر 429
last_rate_limit_time = 0

# عدد مرات 429 المتتالية
rate_limit_count = 0


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "trades.db"


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            entry REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            armed_price REAL,
            status TEXT NOT NULL,
            created_at TEXT,
            entered_at TEXT,
            closed_at TEXT,
            exit_price REAL,
            result TEXT,
            pnl_points REAL,
            pnl_percent REAL
        )
    """)

    conn.commit()
    conn.close()


def create_trade(
    user_id,
    direction,
    entry,
    stop_loss,
    take_profit,
    armed_price=None
):
    conn = get_db()

    now = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute("""
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
        stop_loss,
        take_profit,
        armed_price,
        "waiting_entry",
        now,
    ))

    trade_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return trade_id


def get_active_trade(user_id):
    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM trades
        WHERE user_id = ?
        AND status IN ('waiting_entry', 'in_trade')
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,)).fetchone()

    conn.close()

    return row


def get_trade(trade_id):
    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM trades
        WHERE id = ?
        LIMIT 1
    """, (trade_id,)).fetchone()

    conn.close()

    return row


def set_trade_in_trade(trade_id):
    conn = get_db()

    now = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        UPDATE trades
        SET status = ?,
            entered_at = ?
        WHERE id = ?
    """, (
        "in_trade",
        now,
        trade_id,
    ))

    conn.commit()
    conn.close()


def close_trade(
    trade_id,
    exit_price,
    result,
    pnl_points,
    pnl_percent
):
    conn = get_db()

    now = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        UPDATE trades
        SET status = ?,
            closed_at = ?,
            exit_price = ?,
            result = ?,
            pnl_points = ?,
            pnl_percent = ?
        WHERE id = ?
    """, (
        "closed",
        now,
        exit_price,
        result,
        pnl_points,
        pnl_percent,
        trade_id,
    ))

    conn.commit()
    conn.close()


def cancel_active_trade(user_id):
    conn = get_db()

    conn.execute("""
        UPDATE trades
        SET status = ?
        WHERE user_id = ?
        AND status IN ('waiting_entry', 'in_trade')
    """, (
        "cancelled",
        user_id,
    ))

    conn.commit()
    conn.close()


# =========================================================
# TWELVE DATA
# =========================================================

TWELVE_URL = "https://api.twelvedata.com"


def _extract_price(data):
    if not isinstance(data, dict):
        return None

    value = data.get("price")

    if value is None:
        value = data.get("close")

    if value is None:
        return None

    try:
        return float(value)
    except Exception:
        return None


def _request_twelve_data(endpoint, params, timeout=15):
    """
    طلب آمن من Twelve Data.
    يعالج 429 بدون انهيار البوت.
    """

    global last_rate_limit_time
    global rate_limit_count

    url = f"{TWELVE_URL}/{endpoint}"

    response = requests.get(
        url,
        params=params,
        timeout=timeout
    )

    # -----------------------------------------------------
    # RATE LIMIT
    # -----------------------------------------------------

    if response.status_code == 429:

        rate_limit_count += 1
        last_rate_limit_time = time.time()

        index = min(
            rate_limit_count - 1,
            len(RATE_LIMIT_BACKOFF) - 1
        )

        wait_seconds = RATE_LIMIT_BACKOFF[index]

        print(
            f"Twelve Data 429 - "
            f"rate limit #{rate_limit_count}. "
            f"Waiting {wait_seconds}s"
        )

        raise RuntimeError(
            f"Twelve Data rate limit (429). "
            f"Retry after {wait_seconds} seconds."
        )

    # -----------------------------------------------------
    # OTHER HTTP ERRORS
    # -----------------------------------------------------

    response.raise_for_status()

    data = response.json()

    # Twelve Data أحيانًا يرجع status=error مع HTTP 200
    if isinstance(data, dict):

        if data.get("status") == "error":

            message = data.get(
                "message",
                "Unknown Twelve Data error"
            )

            raise RuntimeError(
                f"Twelve Data: {message}"
            )

    # نجح الطلب، نصفر عداد 429
    rate_limit_count = 0

    return data


def get_gold_price(force=False):
    """
    جلب السعر الحالي.

    مهم:
    لا نطلب Twelve Data كل مرة.
    نستخدم cache لمدة PRICE_CACHE_SECONDS.
    """

    global last_price

    now = time.time()

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    if not force:

        cached_price = price_cache["price"]
        cached_time = price_cache["timestamp"]

        if (
            cached_price is not None
            and (now - cached_time) < PRICE_CACHE_SECONDS
        ):
            last_price = cached_price
            return cached_price

    # -----------------------------------------------------
    # LOCK
    # -----------------------------------------------------

    # منع أكثر من coroutine من إرسال طلب السعر بنفس اللحظة
    async_lock = None

    # هنا نستخدم threading lock بسيط عبر العملية
    if not hasattr(get_gold_price, "_lock"):
        get_gold_price._lock = threading.Lock()

    with get_gold_price._lock:

        # فحص الكاش مرة ثانية بعد انتظار الـlock
        now = time.time()

        if not force:

            cached_price = price_cache["price"]
            cached_time = price_cache["timestamp"]

            if (
                cached_price is not None
                and (now - cached_time) < PRICE_CACHE_SECONDS
            ):
                last_price = cached_price
                return cached_price

        params = {
            "symbol": SYMBOL,
            "apikey": TWELVE_DATA_API_KEY,
        }

        data = _request_twelve_data(
            "price",
            params
        )

        price = _extract_price(data)

        if price is None:
            raise RuntimeError(
                "Twelve Data لم يرجع سعرًا صالحًا."
            )

        price_cache["price"] = price
        price_cache["timestamp"] = time.time()

        last_price = price

        return price


def get_gold_timeseries(interval, outputsize):
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "order": "asc",
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    }

    return _request_twelve_data(
        "time_series",
        params
    )


def get_gold_m5():
    return get_gold_timeseries(
        "5min",
        288
    )


def get_gold_m1():
    return get_gold_timeseries(
        "1min",
        360
    )


def get_latest_m1():
    params = {
        "symbol": SYMBOL,
        "interval": "1min",
        "outputsize": 1,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    }

    return _request_twelve_data(
        "time_series",
        params
    )


# =========================================================
# GEMINI
# =========================================================

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

        print(
            "Available Gemini models:",
            result
        )

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

                print(
                    "Gemini success:",
                    model_name
                )

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
# GEMINI TRADE PARSER
# =========================================================

def extract_trade_from_gemini(text):

    clean = text.replace(
        ",",
        ""
    )

    direction_match = re.search(
        r"(BUY|SELL)",
        clean,
        re.IGNORECASE
    )

    entry_match = re.search(
        r"(?:ENTRY|ENTRY PRICE|الدخول)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        clean,
        re.IGNORECASE
    )

    sl_match = re.search(
        r"(?:SL|STOP LOSS|STOP-LOSS|وقف الخسارة)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        clean,
        re.IGNORECASE
    )

    tp_match = re.search(
        r"(?:TP|TAKE PROFIT|TAKE-PROFIT|الهدف)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        clean,
        re.IGNORECASE
    )

    if not direction_match:
        raise ValueError(
            "Gemini لم يحدد BUY أو SELL."
        )

    if not entry_match:
        raise ValueError(
            "Gemini لم يحدد Entry."
        )

    if not sl_match:
        raise ValueError(
            "Gemini لم يحدد Stop Loss."
        )

    if not tp_match:
        raise ValueError(
            "Gemini لم يحدد Take Profit."
        )

    direction = direction_match.group(1).upper()

    entry = float(
        entry_match.group(1)
    )

    stop_loss = float(
        sl_match.group(1)
    )

    take_profit = float(
        tp_match.group(1)
    )

    if direction == "BUY":

        if not (
            stop_loss < entry < take_profit
        ):
            raise ValueError(
                "مستويات BUY غير منطقية."
            )

    elif direction == "SELL":

        if not (
            take_profit < entry < stop_loss
        ):
            raise ValueError(
                "مستويات SELL غير منطقية."
            )

    return {
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


# =========================================================
# GOLD PROMPT
# =========================================================

def build_gold_prompt(
    current_price,
    m5_data,
    m1_data
):

    return f"""
أنت محلل فني للذهب XAU/USD.

السعر الحالي:
{current_price}

بيانات M5 لآخر 24 ساعة:
{m5_data}

بيانات M1 لآخر 6 ساعات:
{m1_data}

حلل السوق اعتمادًا على البيانات المعطاة فقط.

حدد:

1. الاتجاه الحالي.
2. أهم مناطق الدعم.
3. أهم مناطق المقاومة.
4. هل الأفضل BUY أو SELL من الناحية الفنية.
5. Entry.
6. Stop Loss.
7. Take Profit.

أريد صفقة واحدة فقط.

يجب أن يكون الرد في هذا الشكل الواضح:

TREND: ...
DIRECTION: BUY أو SELL
ENTRY: رقم
SL: رقم
TP: رقم

SUPPORT:
...

RESISTANCE:
...

REASON:
...

لا تعطِ أكثر من صفقة.
"""


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
        ]

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
                "🔙 رجوع للقائمة",
                callback_data="back_menu"
            )
        ]

    ])


# =========================================================
# PNL
# =========================================================

def calculate_unrealized_points(
    direction,
    entry,
    current_price
):

    if direction == "BUY":

        return current_price - entry

    return entry - current_price


def calculate_pnl(
    direction,
    entry,
    exit_price
):

    if direction == "BUY":

        points = exit_price - entry

    else:

        points = entry - exit_price

    percent = (
        points / entry
    ) * 100

    return points, percent


# =========================================================
# STATUS HEARTBEAT
# =========================================================

async def status_heartbeat_loop(
    application,
    user_id
):

    print(
        "Status heartbeat started:",
        user_id
    )

    while user_id in autopilot_users:

        try:

            await asyncio.sleep(
                STATUS_UPDATE_SECONDS
            )

            if user_id not in autopilot_users:
                break

            try:

                current_price = get_gold_price()

            except Exception as e:

                error_text = str(e)

                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ نبضة المتابعة\n\n"
                        "🤖 البوت ما زال يعمل، "
                        "لكن تعذر جلب السعر حاليًا.\n\n"
                        f"🔎 السبب:\n{error_text[:500]}\n\n"
                        "إذا استمر الخطأ، اضغط "
                        "«إيقاف التحليل والمتابعة» "
                        "ثم «تشغيل التحليل من جديد»."
                    ),
                    reply_markup=restart_keyboard()
                )

                continue

            trade = get_active_trade(
                user_id
            )

            # -------------------------------------------------
            # لا توجد صفقة
            # -------------------------------------------------

            if not trade:

                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🟢 نبضة المتابعة\n\n"
                        "🤖 البوت يعمل بشكل طبيعي.\n"
                        f"💰 السعر الحالي: {current_price:.2f}\n\n"
                        "📌 لا توجد صفقة مفتوحة حاليًا."
                    )
                )

                continue

            direction = trade["direction"]
            entry = float(trade["entry"])
            sl = float(trade["stop_loss"])
            tp = float(trade["take_profit"])

            # -------------------------------------------------
            # WAITING ENTRY
            # -------------------------------------------------

            if trade["status"] == "waiting_entry":

                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🟢 نبضة المتابعة\n\n"
                        "🤖 البوت يعمل.\n\n"
                        f"💰 السعر الحالي: {current_price:.2f}\n"
                        f"📈 الاتجاه: {direction}\n"
                        f"🎯 Entry: {entry:.2f}\n"
                        f"🛑 SL: {sl:.2f}\n"
                        f"💰 TP: {tp:.2f}\n\n"
                        "⏳ الحالة: بانتظار وصول السعر إلى Entry."
                    )
                )

                continue

            # -------------------------------------------------
            # IN TRADE
            # -------------------------------------------------

            if trade["status"] == "in_trade":

                points = calculate_unrealized_points(
                    direction,
                    entry,
                    current_price
                )

                sign = "+" if points >= 0 else ""

                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🟢 نبضة المتابعة\n\n"
                        f"💰 السعر الحالي: {current_price:.2f}\n"
                        f"📊 الربح أو الخسارة: {sign}{points:.2f} نقطة\n\n"
                        "🔥 الحالة: داخل الصفقة.\n"
                        "👀 أراقب SL و TP.\n\n"
                        f"📈 الاتجاه: {direction}\n"
                        f"🎯 Entry: {entry:.2f}\n"
                        f"🛑 SL: {sl:.2f}\n"
                        f"💰 TP: {tp:.2f}"
                    )
                )

        except asyncio.CancelledError:

            print(
                "Status heartbeat cancelled:",
                user_id
            )

            break

        except Exception as e:

            print(
                "STATUS HEARTBEAT ERROR:",
                e
            )


def start_status_heartbeat(
    application,
    user_id
):

    stop_status_heartbeat(
        user_id
    )

    task = asyncio.create_task(
        status_heartbeat_loop(
            application,
            user_id
        )
    )

    user_status_tasks[user_id] = task


def stop_status_heartbeat(
    user_id
):

    task = user_status_tasks.pop(
        user_id,
        None
    )

    if task:

        task.cancel()


# =========================================================
# WAIT REANALYSIS
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

        await run_auto_analysis(
            application,
            user_id
        )

    except asyncio.CancelledError:

        pass

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
# AUTO ANALYSIS
# =========================================================

async def run_auto_analysis(
    application,
    user_id
):

    if user_id not in autopilot_users:
        return

    # إذا عنده صفقة بالفعل، لا ننشئ صفقة جديدة
    active_trade = get_active_trade(
        user_id
    )

    if active_trade:
        print(
            "Active trade exists. Skip new analysis:",
            user_id
        )
        return

    async with analysis_lock:

        if user_id not in autopilot_users:
            return

        # -----------------------------------------------------
        # PRICE
        # -----------------------------------------------------

        try:

            current_price = get_gold_price()

        except Exception as e:

            error_text = str(e)

            # 429 لا نرسل رسالة مزعجة كل مرة
            if "429" in error_text:

                print(
                    "Twelve Data rate limit during analysis:",
                    error_text
                )

                schedule_wait_analysis(
                    application,
                    user_id
                )

                return

            raise

        # -----------------------------------------------------
        # M5
        # -----------------------------------------------------

        try:

            m5_data = get_gold_m5()

        except Exception as e:

            error_text = str(e)

            if "429" in error_text:

                print(
                    "Twelve Data 429 on M5:",
                    error_text
                )

                schedule_wait_analysis(
                    application,
                    user_id
                )

                return

            raise

        # -----------------------------------------------------
        # M1
        # -----------------------------------------------------

        try:

            m1_data = get_gold_m1()

        except Exception as e:

            error_text = str(e)

            if "429" in error_text:

                print(
                    "Twelve Data 429 on M1:",
                    error_text
                )

                schedule_wait_analysis(
                    application,
                    user_id
                )

                return

            raise

        # -----------------------------------------------------
        # GEMINI
        # -----------------------------------------------------

        prompt = build_gold_prompt(
            current_price,
            m5_data,
            m1_data
        )

        gemini_text = send_gemini_message(
            prompt
        )

        print(
            "Gemini analysis:",
            gemini_text
        )

        trade_data = extract_trade_from_gemini(
            gemini_text
        )

        direction = trade_data["direction"]
        entry = trade_data["entry"]
        sl = trade_data["stop_loss"]
        tp = trade_data["take_profit"]

        # -----------------------------------------------------
        # CREATE TRADE
        # -----------------------------------------------------

        trade_id = create_trade(
            user_id=user_id,
            direction=direction,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            armed_price=current_price
        )

        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "🤖 تحليل الذهب اكتمل\n\n"
                f"💰 السعر الحالي: {current_price:.2f}\n\n"
                f"📈 الاتجاه: {direction}\n"
                f"🎯 Entry: {entry:.2f}\n"
                f"🛑 SL: {sl:.2f}\n"
                f"💰 TP: {tp:.2f}\n\n"
                f"🆔 رقم الصفقة: {trade_id}\n\n"
                "⏳ الحالة: بانتظار Entry.\n"
                "👀 أراقب السعر للدخول."
            ),
            reply_markup=stop_keyboard()
        )

        print(
            "Trade created:",
            trade_id
        )


# =========================================================
# TRADE MONITOR
# =========================================================

async def monitor_trades(
    application
):

    global last_price

    print(
        "Trade monitor started."
    )

    while True:

        try:

            if not autopilot_users:

                await asyncio.sleep(
                    MONITOR_SECONDS
                )

                continue

            try:

                current_price = get_gold_price()

            except Exception as e:

                print(
                    "MONITOR PRICE ERROR:",
                    e
                )

                await asyncio.sleep(
                    MONITOR_SECONDS
                )

                continue

            previous_price = last_price

            # لأن get_gold_price يحدث last_price،
            # نستخدم السعر السابق من الكاش قبل التحديث قدر الإمكان.
            #
            # في حال لم يكن هناك previous_price، نستخدم None.
            #
            # نعيد تعيينه بعد القراءة.
            last_price = current_price

            for user_id in list(
                autopilot_users
            ):

                try:

                    trade = get_active_trade(
                        user_id
                    )

                    if not trade:
                        continue

                    trade_id = trade["id"]

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
                    # WAITING ENTRY
                    # =================================================

                    if status == "waiting_entry":

                        entered = False

                        # BUY:
                        # يدخل فقط عندما يعبر السعر Entry من تحت لفوق
                        if direction == "BUY":

                            if (
                                previous_price is not None
                                and previous_price < entry
                                and current_price >= entry
                            ):
                                entered = True

                            elif current_price == entry:
                                entered = True

                        # SELL:
                        # يدخل فقط عندما يعبر السعر Entry من فوق لتحت
                        elif direction == "SELL":

                            if (
                                previous_price is not None
                                and previous_price > entry
                                and current_price <= entry
                            ):
                                entered = True

                            elif current_price == entry:
                                entered = True

                        if entered:

                            set_trade_in_trade(
                                trade_id
                            )

                            await application.bot.send_message(
                                chat_id=user_id,
                                text=(
                                    "🔥 دخلنا الصفقة\n\n"
                                    f"📈 الاتجاه: {direction}\n"
                                    f"💰 سعر الدخول: {entry:.2f}\n"
                                    f"🛑 SL: {sl:.2f}\n"
                                    f"🎯 TP: {tp:.2f}\n\n"
                                    "🔥 الحالة: داخل الصفقة.\n"
                                    "👀 أراقب SL و TP.\n\n"
                                    "⏱️ سأرسل لك حالة الصفقة كل 5 دقائق."
                                ),
                                reply_markup=stop_keyboard()
                            )

                        continue

                    # =================================================
                    # IN TRADE
                    # =================================================

                    if status == "in_trade":

                        # ---------------------------------------------
                        # الحصول على M1 لمعرفة High / Low
                        # ---------------------------------------------

                        try:

                            m1_data = get_latest_m1()

                            values = (
                                m1_data.get(
                                    "values",
                                    []
                                )
                                if isinstance(
                                    m1_data,
                                    dict
                                )
                                else []
                            )

                            candle = (
                                values[0]
                                if values
                                else {}
                            )

                            high = float(
                                candle.get(
                                    "high",
                                    current_price
                                )
                            )

                            low = float(
                                candle.get(
                                    "low",
                                    current_price
                                )
                            )

                        except Exception as e:

                            print(
                                "M1 monitor error:",
                                e
                            )

                            high = current_price
                            low = current_price

                        hit_tp = False
                        hit_sl = False

                        if direction == "BUY":

                            if high >= tp:
                                hit_tp = True

                            if low <= sl:
                                hit_sl = True

                        else:

                            if low <= tp:
                                hit_tp = True

                            if high >= sl:
                                hit_sl = True

                        # ---------------------------------------------
                        # إذا ضرب الاثنين بنفس الشمعة
                        # نستخدم السعر الحالي كحل تقريبي
                        # ---------------------------------------------

                        result = None
                        exit_price = None

                        if hit_tp and hit_sl:

                            distance_tp = abs(
                                current_price - tp
                            )

                            distance_sl = abs(
                                current_price - sl
                            )

                            if distance_tp <= distance_sl:

                                result = "TP"
                                exit_price = tp

                            else:

                                result = "SL"
                                exit_price = sl

                        elif hit_tp:

                            result = "TP"
                            exit_price = tp

                        elif hit_sl:

                            result = "SL"
                            exit_price = sl

                        # ---------------------------------------------
                        # CLOSE
                        # ---------------------------------------------

                        if result:

                            points, percent = calculate_pnl(
                                direction,
                                entry,
                                exit_price
                            )

                            close_trade(
                                trade_id,
                                exit_price,
                                result,
                                points,
                                percent
                            )

                            sign = (
                                "+"
                                if points >= 0
                                else ""
                            )

                            emoji = (
                                "🎯"
                                if result == "TP"
                                else "🛑"
                            )

                            await application.bot.send_message(
                                chat_id=user_id,
                                text=(
                                    f"{emoji} الصفقة أغلقت: {result}\n\n"
                                    f"📈 الاتجاه: {direction}\n"
                                    f"🎯 Entry: {entry:.2f}\n"
                                    f"🚪 الخروج: {exit_price:.2f}\n\n"
                                    f"📊 النتيجة: {sign}{points:.2f} نقطة\n"
                                    f"📈 النسبة: {sign}{percent:.3f}%\n\n"
                                    "🤖 سأبدأ تحليلًا جديدًا تلقائيًا "
                                    "بعد قليل."
                                )
                            )

                            await asyncio.sleep(3)

                            if user_id in autopilot_users:

                                schedule_wait_analysis(
                                    application,
                                    user_id
                                )

                except Exception as e:

                    print(
                        f"Monitor user {user_id} error:",
                        e
                    )

            await asyncio.sleep(
                MONITOR_SECONDS
            )

        except asyncio.CancelledError:

            break

        except Exception as e:

            print(
                "MONITOR LOOP ERROR:",
                e
            )

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

    # إلغاء المتابعة القديمة
    autopilot_users.discard(
        user_id
    )

    stop_status_heartbeat(
        user_id
    )

    old_task = user_wait_tasks.pop(
        user_id,
        None
    )

    if old_task:
        old_task.cancel()

    # تنظيف الصفقة النشطة
    cancel_active_trade(
        user_id
    )

    await update.message.reply_text(
        "👋 أهلاً بك في بوت الذهب وGemini.\n\n"
        "اختر من القائمة:",
        reply_markup=main_keyboard()
    )


# =========================================================
# CALLBACKS
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    data = query.data

    # =====================================================
    # BACK MENU
    # =====================================================

    if data == "back_menu":

        await query.edit_message_text(
            "📋 القائمة الرئيسية:",
            reply_markup=main_keyboard()
        )

        return

    # =====================================================
    # STOP
    # =====================================================

    if data == "stop_all":

        autopilot_users.discard(
            user_id
        )

        stop_status_heartbeat(
            user_id
        )

        old_task = user_wait_tasks.pop(
            user_id,
            None
        )

        if old_task:
            old_task.cancel()

        cancel_active_trade(
            user_id
        )

        await query.edit_message_text(
            "⛔ تم إيقاف التحليل والمتابعة.\n\n"
            "تم إلغاء أي صفقة متابعة حاليًا.",
            reply_markup=restart_keyboard()
        )

        return

    # =====================================================
    # CURRENT PRICE
    # =====================================================

    if data == "current_price":

        try:

            price = get_gold_price()

            await query.edit_message_text(
                f"💰 السعر الحالي للذهب XAU/USD:\n\n"
                f"{price:.2f}",
                reply_markup=main_keyboard()
            )

        except Exception as e:

            await query.edit_message_text(
                "⚠️ تعذر جلب السعر.\n\n"
                f"🔎 الخطأ:\n{str(e)[:600]}",
                reply_markup=main_keyboard()
            )

        return

    # =====================================================
    # GEMINI CHAT
    # =====================================================

    if data == "gemini_chat":

        context.user_data["chat_mode"] = True

        await query.edit_message_text(
            "🟢 تم تشغيل دردشة Gemini.\n\n"
            "اكتب سؤالك الآن.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 رجوع للقائمة",
                        callback_data="back_menu"
                    )
                ]
            ])
        )

        return

    # =====================================================
    # M5
    # =====================================================

    if data == "gold_analysis":

        try:

            current_price = get_gold_price()

            m5 = get_gold_m5()

            prompt = build_gold_prompt(
                current_price,
                m5,
                "لم يتم استخدام M1 في هذا التحليل."
            )

            answer = send_gemini_message(
                prompt
            )

            await query.edit_message_text(
                "📊 تحليل الذهب M5\n\n"
                f"💰 السعر: {current_price:.2f}\n\n"
                f"{answer[:3500]}",
                reply_markup=main_keyboard()
            )

        except Exception as e:

            await query.edit_message_text(
                "⚠️ تعذر تحليل M5.\n\n"
                f"🔎 الخطأ:\n{str(e)[:700]}",
                reply_markup=main_keyboard()
            )

        return

    # =====================================================
    # M1
    # =====================================================

    if data == "m1_analysis":

        try:

            current_price = get_gold_price()

            m1 = get_gold_m1()

            prompt = f"""
حلل XAU/USD على فريم M1.

السعر الحالي:
{current_price}

بيانات M1:
{m1}

حدد:
- الاتجاه
- الدعم
- المقاومة
- أهم مناطق السعر

لا تعطِ توصية مالية مؤكدة.
"""

            answer = send_gemini_message(
                prompt
            )

            await query.edit_message_text(
                "📉 تحليل الذهب M1\n\n"
                f"💰 السعر: {current_price:.2f}\n\n"
                f"{answer[:3500]}",
                reply_markup=main_keyboard()
            )

        except Exception as e:

            await query.edit_message_text(
                "⚠️ تعذر تحليل M1.\n\n"
                f"🔎 الخطأ:\n{str(e)[:700]}",
                reply_markup=main_keyboard()
            )

        return

    # =====================================================
    # AUTO GEMINI ANALYSIS
    # =====================================================

    if data == "gemini_analysis":

        if user_id in autopilot_users:

            await query.edit_message_text(
                "🤖 المتابعة مفعلة بالفعل.\n\n"
                "البوت يراقب الصفقة.",
                reply_markup=stop_keyboard()
            )

            return

        autopilot_users.add(
            user_id
        )

        start_status_heartbeat(
            context.application,
            user_id
        )

        await query.edit_message_text(
            "🤖 تم تشغيل التحليل والمتابعة.\n\n"
            "⏳ جاري تحليل الذهب الآن...\n\n"
            "📡 سيتم مراقبة Entry وSL وTP.\n"
            "⏱️ سأرسل نبضة حالة كل 5 دقائق.",
            reply_markup=stop_keyboard()
        )

        try:

            await run_auto_analysis(
                context.application,
                user_id
            )

        except Exception as e:

            error_text = str(e)

            print(
                "AUTO ANALYSIS ERROR:",
                error_text
            )

            # -------------------------------------------------
            # 429
            # -------------------------------------------------

            if "429" in error_text:

                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⏳ Twelve Data وصل إلى حد الطلبات مؤقتًا.\n\n"
                        "🤖 المتابعة ما زالت مفعلة.\n"
                        "لن أوقف البوت.\n\n"
                        "🔄 سأحاول التحليل مرة أخرى تلقائيًا "
                        "بعد قليل."
                    ),
                    reply_markup=stop_keyboard()
                )

                schedule_wait_analysis(
                    context.application,
                    user_id
                )

            else:

                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ تعذر إكمال التحليل.\n\n"
                        "🤖 المتابعة ما زالت مفعلة، "
                        "لكن هذا التحليل لم يكتمل.\n\n"
                        f"🔎 الخطأ:\n"
                        f"{error_text[:700]}\n\n"
                        "إذا تكرر الخطأ، اضغط "
                        "«إيقاف التحليل والمتابعة» "
                        "ثم شغّل التحليل من جديد."
                    ),
                    reply_markup=stop_keyboard()
                )

                schedule_wait_analysis(
                    context.application,
                    user_id
                )

        return


# =========================================================
# CHAT MESSAGE
# =========================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user_id = update.effective_user.id

    text = update.message.text.strip()

    if not context.user_data.get(
        "chat_mode",
        False
    ):
        return

    if text.lower() == "/start":

        return

    try:

        answer = send_gemini_message(
            text
        )

        await update.message.reply_text(
            answer[:4000]
        )

    except Exception as e:

        await update.message.reply_text(
            "⚠️ تعذر الاتصال بـ Gemini.\n\n"
            f"🔎 الخطأ:\n{str(e)[:700]}"
        )


# =========================================================
# MODELS COMMAND
# =========================================================

async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    models = get_available_models()

    if not models:

        await update.message.reply_text(
            "⚠️ لم أستطع جلب قائمة موديلات Gemini."
        )

        return

    text = (
        "🤖 موديلات Gemini المتاحة:\n\n"
        + "\n".join(
            f"• {model}"
            for model in models
        )
    )

    await update.message.reply_text(
        text[:4000]
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "BOT ERROR:",
        context.error
    )


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


def run_web():

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
# POST INIT
# =========================================================

async def post_init(
    application
):

    print(
        "Bot post_init started."
    )

    asyncio.create_task(
        monitor_trades(
            application
        )
    )

    print(
        "Trade monitor task started."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    # Flask في thread منفصل
    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
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
            message_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "================================"
    )

    print(
        "Gold Gemini Bot started."
    )

    print(
        "================================"
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
