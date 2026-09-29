import os
import re
import json
import time
import sqlite3
import threading
import asyncio
from datetime import datetime, timezone

import requests
from flask import Flask
from dotenv import load_dotenv

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


# ============================================================
# ENV
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing")

if not TWELVE_DATA_API_KEY:
    raise RuntimeError("TWELVE_DATA_API_KEY is missing")


# ============================================================
# CONFIG
# ============================================================

SYMBOL = "XAU/USD"

PORT = int(os.getenv("PORT", "10000"))

MONITOR_INTERVAL = 15

HEARTBEAT_INTERVAL = 300       # 5 minutes

PRICE_CACHE_SECONDS = 10

RATE_LIMIT_BACKOFF_SECONDS = [
    30,
    60,
    120,
    300,
]

AUTO_REANALYZE_DELAY = 5

DB_FILE = "trades.db"


# ============================================================
# FLASK
# ============================================================

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
    web_app.run(
        host="0.0.0.0",
        port=PORT
    )


# ============================================================
# GEMINI
# ============================================================

gemini_client = genai.Client(
    api_key=GEMINI_API_KEY
)


MODEL_PRIORITY = [
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


current_model = MODEL_PRIORITY[0]

available_models_cache = None
available_models_cache_time = 0

MODEL_CACHE_SECONDS = 600


# ============================================================
# GLOBAL STATE
# ============================================================

analysis_lock = asyncio.Lock()

users_chat_history = {}

autopilot_users = set()

monitor_task = None
heartbeat_task = None

bot_application = None


# ============================================================
# MARKET STATE
# ============================================================

market_state = {
    "price": None,
    "previous_price": None,
    "updated_at": None,

    "last_error": None,

    "rate_limited_until": 0,

    "backoff_index": 0,
}

market_lock = threading.Lock()


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with db_lock:

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

                created_at TEXT NOT NULL,

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
    armed_price
):

    now = datetime.now(timezone.utc).isoformat()

    with db_lock:

        conn = get_db()

        cur = conn.execute(
            """
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
            """,
            (
                user_id,
                direction,
                entry,
                stop_loss,
                take_profit,
                armed_price,
                "waiting_entry",
                now,
            )
        )

        conn.commit()

        trade_id = cur.lastrowid

        conn.close()

    return trade_id


def get_active_trade(user_id):

    with db_lock:

        conn = get_db()

        row = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE user_id = ?
            AND status IN ('waiting_entry', 'in_trade')
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()

        conn.close()

    return row


def get_all_active_trades():

    with db_lock:

        conn = get_db()

        rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE status IN ('waiting_entry', 'in_trade')
            ORDER BY id ASC
            """
        ).fetchall()

        conn.close()

    return rows


def mark_trade_entered(
    trade_id,
    actual_entry
):

    now = datetime.now(timezone.utc).isoformat()

    with db_lock:

        conn = get_db()

        conn.execute(
            """
            UPDATE trades
            SET
                status = 'in_trade',
                armed_price = ?,
                entered_at = ?
            WHERE id = ?
            """,
            (
                actual_entry,
                now,
                trade_id
            )
        )

        conn.commit()

        conn.close()


def close_trade(
    trade_id,
    exit_price,
    result,
    pnl_points,
    pnl_percent
):

    now = datetime.now(timezone.utc).isoformat()

    with db_lock:

        conn = get_db()

        conn.execute(
            """
            UPDATE trades
            SET
                status = 'closed',
                closed_at = ?,
                exit_price = ?,
                result = ?,
                pnl_points = ?,
                pnl_percent = ?
            WHERE id = ?
            """,
            (
                now,
                exit_price,
                result,
                pnl_points,
                pnl_percent,
                trade_id
            )
        )

        conn.commit()

        conn.close()


def cancel_active_trade(user_id):

    with db_lock:

        conn = get_db()

        conn.execute(
            """
            UPDATE trades
            SET status = 'cancelled'
            WHERE user_id = ?
            AND status IN ('waiting_entry', 'in_trade')
            """,
            (user_id,)
        )

        conn.commit()

        conn.close()


# ============================================================
# TWELVE DATA
# ============================================================

def parse_retry_after(response):

    retry_after = response.headers.get("Retry-After")

    if retry_after:

        try:
            return max(
                10,
                int(float(retry_after))
            )
        except:
            pass

    return None


def set_rate_limit(seconds):

    with market_lock:

        market_state["rate_limited_until"] = (
            time.time() + seconds
        )

        market_state["last_error"] = (
            f"Twelve Data rate limit (429). "
            f"Retry after {seconds} seconds."
        )


def is_rate_limited():

    with market_lock:

        return (
            time.time()
            < market_state["rate_limited_until"]
        )


def get_last_known_price():

    with market_lock:

        return market_state["price"]


def get_market_snapshot():

    with market_lock:

        return {
            "price": market_state["price"],
            "previous_price": market_state["previous_price"],
            "updated_at": market_state["updated_at"],
            "last_error": market_state["last_error"],
            "rate_limited_until": market_state["rate_limited_until"],
        }


def request_twelve_data(
    endpoint,
    params
):

    url = f"https://api.twelvedata.com/{endpoint}"

    response = requests.get(
        url,
        params=params,
        timeout=15
    )

    if response.status_code == 429:

        retry_after = parse_retry_after(response)

        if retry_after is None:

            with market_lock:

                index = market_state["backoff_index"]

                if index >= len(RATE_LIMIT_BACKOFF_SECONDS):
                    index = len(RATE_LIMIT_BACKOFF_SECONDS) - 1

                retry_after = RATE_LIMIT_BACKOFF_SECONDS[index]

                market_state["backoff_index"] = min(
                    index + 1,
                    len(RATE_LIMIT_BACKOFF_SECONDS) - 1
                )

        set_rate_limit(retry_after)

        raise RuntimeError(
            f"Twelve Data rate limit (429). "
            f"Retry after {retry_after} seconds."
        )

    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict) and data.get("status") == "error":

        message = data.get(
            "message",
            "Twelve Data error"
        )

        if "rate" in message.lower():
            set_rate_limit(60)

        raise RuntimeError(message)

    with market_lock:

        market_state["backoff_index"] = 0
        market_state["last_error"] = None

    return data


# ============================================================
# CURRENT PRICE
# ============================================================

def fetch_gold_price(force=False):

    now = time.time()

    snapshot = get_market_snapshot()

    if not force:

        if snapshot["price"] is not None:

            updated = snapshot["updated_at"]

            if updated:

                age = now - updated

                if age < PRICE_CACHE_SECONDS:

                    return snapshot["price"]

    if is_rate_limited():

        raise RuntimeError(
            market_state["last_error"]
            or "Twelve Data rate limit"
        )

    data = request_twelve_data(
        "price",
        {
            "symbol": SYMBOL,
            "apikey": TWELVE_DATA_API_KEY,
        }
    )

    price = float(data["price"])

    with market_lock:

        old_price = market_state["price"]

        market_state["previous_price"] = old_price

        market_state["price"] = price

        market_state["updated_at"] = time.time()

    return price


# ============================================================
# TIME SERIES
# ============================================================

def get_time_series(
    interval,
    outputsize
):

    if is_rate_limited():

        raise RuntimeError(
            market_state["last_error"]
            or "Twelve Data rate limit"
        )

    data = request_twelve_data(
        "time_series",
        {
            "symbol": SYMBOL,
            "interval": interval,
            "outputsize": outputsize,
            "order": "asc",
            "timezone": "UTC",
            "apikey": TWELVE_DATA_API_KEY,
        }
    )

    values = data.get("values", [])

    if not values:
        raise RuntimeError(
            f"No {interval} market data returned."
        )

    return values


def get_m5_data():

    return get_time_series(
        "5min",
        288
    )


def get_m1_data():

    return get_time_series(
        "1min",
        360
    )


# ============================================================
# FORMAT MARKET DATA
# ============================================================

def compact_market_data(values):

    output = []

    for candle in values:

        output.append({
            "datetime": candle.get("datetime"),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
        })

    return output


# ============================================================
# GEMINI MODELS
# ============================================================

def get_available_models():

    global available_models_cache
    global available_models_cache_time

    now = time.time()

    if (
        available_models_cache
        and now - available_models_cache_time
        < MODEL_CACHE_SECONDS
    ):
        return available_models_cache

    try:

        models = gemini_client.models.list()

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

        available_models_cache = result

        available_models_cache_time = now

        return result

    except Exception:

        return []


def clean_model_name(name):

    if name.startswith("models/"):
        return name[7:]

    return name


def choose_model():

    available = get_available_models()

    if not available:
        return current_model

    for preferred in MODEL_PRIORITY:

        for model in available:

            cleaned = clean_model_name(model)

            if cleaned == preferred:
                return preferred

    return current_model


def is_model_error(error):

    text = str(error).lower()

    keywords = [
        "429",
        "503",
        "unavailable",
        "resource exhausted",
        "high demand",
        "rate limit",
        "quota",
        "timeout",
        "deadline",
        "not found",
    ]

    return any(
        key in text
        for key in keywords
    )


def gemini_generate(
    prompt
):

    global current_model

    models_to_try = []

    selected = choose_model()

    models_to_try.append(selected)

    for model in MODEL_PRIORITY:

        if model not in models_to_try:
            models_to_try.append(model)

    last_error = None

    for model in models_to_try:

        try:

            response = gemini_client.models.generate_content(
                model=model,
                contents=prompt
            )

            current_model = model

            return response.text

        except Exception as e:

            last_error = e

            if not is_model_error(e):
                break

            continue

    raise RuntimeError(
        f"Gemini failed: {last_error}"
    )


# ============================================================
# PARSE GEMINI JSON
# ============================================================

def extract_json(text):

    text = text.strip()

    text = re.sub(
        r"```json",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"```",
        "",
        text
    )

    text = text.strip()

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise ValueError(
            "Gemini did not return valid JSON."
        )

    json_text = text[start:end + 1]

    return json.loads(json_text)


def normalize_direction(value):

    value = str(value).upper().strip()

    if value in ["BUY", "LONG", "شراء"]:
        return "BUY"

    if value in ["SELL", "SHORT", "بيع"]:
        return "SELL"

    raise ValueError(
        "Invalid direction from Gemini."
    )


# ============================================================
# GOLD ANALYSIS
# ============================================================

async def perform_gold_analysis(
    user_id
):

    async with analysis_lock:

        await send_text(
            user_id,
            "⏳ جاري جلب بيانات الذهب وتحليل السوق..."
        )

        try:

            current_price = fetch_gold_price(
                force=True
            )

            m5 = get_m5_data()

            m1 = get_m1_data()

        except Exception as e:

            await send_text(
                user_id,
                "⚠️ تعذر جلب بيانات الذهب حاليًا.\n\n"
                f"🔎 السبب:\n{e}"
            )

            return None

        prompt = f"""
أنت محلل محترف لسوق الذهب XAU/USD.

حلل البيانات التالية فقط، ولا تخترع أسعارًا غير موجودة.

السعر الحالي:
{current_price}

بيانات M5:
{json.dumps(
    compact_market_data(m5),
    ensure_ascii=False
)}

بيانات M1:
{json.dumps(
    compact_market_data(m1),
    ensure_ascii=False
)}

أريد توصية تداول منظمة.

مهم جدًا:
- حدد BUY أو SELL.
- حدد منطقة دخول واحدة واضحة.
- حدد Stop Loss.
- حدد Take Profit.
- حدد الدعم والمقاومة.
- اشرح سبب الاتجاه.
- يجب أن تكون الأرقام منطقية بالنسبة للسعر الحالي.
- لا تعتبر الصفقة داخلة بمجرد إنشاء التوصية.
- الدخول سيتم لاحقًا عندما يعبر السعر مستوى الدخول.

أرجع JSON فقط بهذا الشكل:

{{
  "direction": "BUY",
  "entry": 4200.0,
  "stop_loss": 4195.0,
  "take_profit": 4210.0,
  "support": [4195.0, 4190.0],
  "resistance": [4205.0, 4210.0],
  "trend": "صاعد",
  "confidence": 75,
  "reason": "شرح مختصر"
}}

لا تضع Markdown.
لا تضع أي كلام خارج JSON.
"""

        try:

            raw = await asyncio.to_thread(
                gemini_generate,
                prompt
            )

            analysis = extract_json(raw)

            direction = normalize_direction(
                analysis["direction"]
            )

            entry = float(
                analysis["entry"]
            )

            stop_loss = float(
                analysis["stop_loss"]
            )

            take_profit = float(
                analysis["take_profit"]
            )

            if entry <= 0:
                raise ValueError(
                    "Invalid entry."
                )

            if stop_loss <= 0:
                raise ValueError(
                    "Invalid stop loss."
                )

            if take_profit <= 0:
                raise ValueError(
                    "Invalid take profit."
                )

            analysis["direction"] = direction
            analysis["entry"] = entry
            analysis["stop_loss"] = stop_loss
            analysis["take_profit"] = take_profit

        except Exception as e:

            await send_text(
                user_id,
                "⚠️ حصل خطأ أثناء تحليل Gemini.\n\n"
                f"🔎 السبب:\n{e}"
            )

            return None

        # ----------------------------------------------------
        # IMPORTANT:
        # The price used to ARM the trade is the price NOW.
        #
        # We do NOT enter immediately if current price is
        # already beyond entry.
        # ----------------------------------------------------

        trade_id = create_trade(
            user_id=user_id,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            armed_price=current_price
        )

        autopilot_users.add(user_id)

        trend = analysis.get(
            "trend",
            "غير محدد"
        )

        confidence = analysis.get(
            "confidence",
            "غير محدد"
        )

        reason = analysis.get(
            "reason",
            "غير محدد"
        )

        support = analysis.get(
            "support",
            []
        )

        resistance = analysis.get(
            "resistance",
            []
        )

        support_text = ", ".join(
            str(x) for x in support
        )

        resistance_text = ", ".join(
            str(x) for x in resistance
        )

        message = f"""
📊 تحليل الذهب XAU/USD

💰 السعر وقت التحليل:
{current_price:.2f}

📈 الاتجاه:
{trend}

🎯 الاتجاه المقترح:
{direction}

🎯 منطقة الدخول:
{entry:.2f}

🛑 Stop Loss:
{stop_loss:.2f}

💰 Take Profit:
{take_profit:.2f}

📉 الدعم:
{support_text}

📈 المقاومة:
{resistance_text}

🎯 الثقة:
{confidence}%

📝 السبب:
{reason}

━━━━━━━━━━━━━━

⏳ الحالة:
بانتظار دخول السعر لمنطقة الدخول.

⚠️ مهم:
السعر الحالي لا يعني أننا دخلنا الصفقة.

سيتم تسجيل الدخول فقط عندما يعبر السعر مستوى الدخول.
وعندها سيتم تسجيل السعر الفعلي الذي اكتشف فيه البوت الدخول.
"""

        await send_text(
            user_id,
            message
        )

        return trade_id


# ============================================================
# BOT SEND HELPERS
# ============================================================

async def send_text(
    user_id,
    text
):

    try:

        await bot_application.bot.send_message(
            chat_id=user_id,
            text=text
        )

    except Exception as e:

        print(
            f"Telegram send error: {e}"
        )


# ============================================================
# MAIN MENU
# ============================================================

def main_menu():

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
            ),

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

        [
            InlineKeyboardButton(
                "⛔ إيقاف التحليل والمتابعة",
                callback_data="stop_all"
            )
        ],
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


def back_button():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔙 رجوع للقائمة",
                callback_data="back_menu"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    users_chat_history.pop(
        user_id,
        None
    )

    await update.message.reply_text(
        "🤖 بوت الذهب + Gemini\n\n"
        "اختر العملية:",
        reply_markup=main_menu()
    )


# ============================================================
# CURRENT PRICE
# ============================================================

async def current_price(
    query,
    user_id
):

    await query.edit_message_text(
        "⏳ جاري جلب سعر الذهب..."
    )

    try:

        price = await asyncio.to_thread(
            fetch_gold_price,
            True
        )

        await query.edit_message_text(
            f"💰 السعر الحالي للذهب XAU/USD:\n\n"
            f"**{price:.2f}**",
            parse_mode="Markdown",
            reply_markup=back_button()
        )

    except Exception as e:

        snapshot = get_market_snapshot()

        last_price = snapshot["price"]

        if last_price is not None:

            updated = snapshot["updated_at"]

            age = (
                time.time() - updated
                if updated
                else None
            )

            age_text = (
                f"{age:.0f} ثانية"
                if age is not None
                else "غير معروف"
            )

            await query.edit_message_text(
                "⚠️ تعذر الحصول على سعر جديد.\n\n"
                f"💰 آخر سعر معروف: {last_price:.2f}\n"
                f"🕐 عمر السعر: {age_text}\n\n"
                f"🔎 {e}",
                reply_markup=back_button()
            )

        else:

            await query.edit_message_text(
                f"⚠️ تعذر جلب السعر.\n\n"
                f"🔎 {e}",
                reply_markup=back_button()
            )


# ============================================================
# M5 ANALYSIS
# ============================================================

async def m5_analysis(
    query
):

    await query.edit_message_text(
        "⏳ جاري جلب بيانات M5..."
    )

    try:

        values = await asyncio.to_thread(
            get_m5_data
        )

        latest = values[-1]

        text = (
            "📊 تحليل بيانات الذهب M5\n\n"
            f"🕐 آخر شمعة: {latest.get('datetime')}\n"
            f"Open: {latest.get('open')}\n"
            f"High: {latest.get('high')}\n"
            f"Low: {latest.get('low')}\n"
            f"Close: {latest.get('close')}\n\n"
            f"📦 عدد الشموع: {len(values)}"
        )

        await query.edit_message_text(
            text,
            reply_markup=back_button()
        )

    except Exception as e:

        await query.edit_message_text(
            f"⚠️ خطأ في M5:\n\n{e}",
            reply_markup=back_button()
        )


# ============================================================
# M1 ANALYSIS
# ============================================================

async def m1_analysis(
    query
):

    await query.edit_message_text(
        "⏳ جاري جلب بيانات M1..."
    )

    try:

        values = await asyncio.to_thread(
            get_m1_data
        )

        latest = values[-1]

        text = (
            "📉 تحليل بيانات الذهب M1\n\n"
            f"🕐 آخر شمعة: {latest.get('datetime')}\n"
            f"Open: {latest.get('open')}\n"
            f"High: {latest.get('high')}\n"
            f"Low: {latest.get('low')}\n"
            f"Close: {latest.get('close')}\n\n"
            f"📦 عدد الشموع: {len(values)}"
        )

        await query.edit_message_text(
            text,
            reply_markup=back_button()
        )

    except Exception as e:

        await query.edit_message_text(
            f"⚠️ خطأ في M1:\n\n{e}",
            reply_markup=back_button()
        )


# ============================================================
# GEMINI CHAT
# ============================================================

async def gemini_chat_start(
    query,
    user_id
):

    users_chat_history.setdefault(
        user_id,
        []
    )

    await query.edit_message_text(
        "🟢 دردشة مع Gemini\n\n"
        "اكتب سؤالك الآن.\n"
        "للعودة للقائمة اضغط الزر:",
        reply_markup=back_button()
    )


async def handle_chat_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id not in users_chat_history:
        return

    message = update.message.text.strip()

    if not message:
        return

    history = users_chat_history[user_id]

    history.append({
        "role": "user",
        "text": message
    })

    history = history[-10:]

    conversation = ""

    for item in history:

        conversation += (
            f"{item['role']}: "
            f"{item['text']}\n"
        )

    prompt = f"""
أنت مساعد Gemini داخل بوت Telegram.

المحادثة:

{conversation}

أجب باللغة العربية بشكل واضح ومفيد.
"""

    try:

        answer = await asyncio.to_thread(
            gemini_generate,
            prompt
        )

        history.append({
            "role": "assistant",
            "text": answer
        })

        users_chat_history[user_id] = history[-10:]

        await update.message.reply_text(
            answer,
            reply_markup=back_button()
        )

    except Exception as e:

        await update.message.reply_text(
            f"⚠️ حصل خطأ أثناء الاتصال بجيميني.\n\n"
            f"{e}",
            reply_markup=back_button()
        )


# ============================================================
# ENTRY DETECTION
# ============================================================

def check_entry_crossing(
    trade,
    previous_price,
    current_price
):

    direction = trade["direction"]

    entry = float(
        trade["entry"]
    )

    # --------------------------------------------------------
    # BUY
    #
    # Example:
    #
    # previous = 4199
    # entry    = 4200
    # current  = 4201
    #
    # ENTER at 4201
    # --------------------------------------------------------

    if direction == "BUY":

        if current_price == entry:
            return True

        if (
            previous_price is not None
            and previous_price < entry
            and current_price >= entry
        ):
            return True

        return False

    # --------------------------------------------------------
    # SELL
    #
    # Example:
    #
    # previous = 4201
    # entry    = 4200
    # current  = 4199
    #
    # ENTER at 4199
    # --------------------------------------------------------

    if direction == "SELL":

        if current_price == entry:
            return True

        if (
            previous_price is not None
            and previous_price > entry
            and current_price <= entry
        ):
            return True

        return False

    return False


# ============================================================
# SL / TP
# ============================================================

def check_sl_tp(
    trade,
    current_price
):

    direction = trade["direction"]

    stop_loss = float(
        trade["stop_loss"]
    )

    take_profit = float(
        trade["take_profit"]
    )

    if direction == "BUY":

        if current_price <= stop_loss:
            return "SL"

        if current_price >= take_profit:
            return "TP"

    elif direction == "SELL":

        if current_price >= stop_loss:
            return "SL"

        if current_price <= take_profit:
            return "TP"

    return None


# ============================================================
# PNL
# ============================================================

def calculate_pnl(
    direction,
    entry,
    exit_price
):

    if direction == "BUY":

        points = exit_price - entry

    else:

        points = entry - exit_price

    if entry != 0:

        percent = (
            points / entry
        ) * 100

    else:

        percent = 0

    return points, percent


# ============================================================
# ENTER TRADE
# ============================================================

async def process_entry(
    trade,
    current_price
):

    trade_id = trade["id"]

    user_id = trade["user_id"]

    direction = trade["direction"]

    planned_entry = float(
        trade["entry"]
    )

    actual_entry = current_price

    mark_trade_entered(
        trade_id,
        actual_entry
    )

    await send_text(
        user_id,
        f"""
🔥 دخلنا الصفقة

📈 الاتجاه:
{direction}

🎯 الدخول المخطط:
{planned_entry:.2f}

🔥 الدخول الفعلي:
{actual_entry:.2f}

🛑 Stop Loss:
{float(trade["stop_loss"]):.2f}

🎯 Take Profit:
{float(trade["take_profit"]):.2f}

━━━━━━━━━━━━━━

🔥 الحالة: داخل الصفقة.
👀 أراقب SL و TP.

ملاحظة:
الدخول الفعلي هو السعر الذي اكتشف فيه البوت عبور منطقة الدخول.
"""
    )


# ============================================================
# CLOSE TRADE
# ============================================================

async def process_close(
    trade,
    current_price,
    result
):

    trade_id = trade["id"]

    user_id = trade["user_id"]

    direction = trade["direction"]

    entry = float(
        trade["armed_price"]
        if trade["status"] == "in_trade"
        and trade["armed_price"] is not None
        else trade["entry"]
    )

    points, percent = calculate_pnl(
        direction,
        entry,
        current_price
    )

    close_trade(
        trade_id,
        current_price,
        result,
        points,
        percent
    )

    if result == "TP":

        result_text = "🎯 تم ضرب Take Profit"

    else:

        result_text = "🛑 تم ضرب Stop Loss"

    sign = "+" if points >= 0 else ""

    await send_text(
        user_id,
        f"""
{result_text}

📈 الاتجاه:
{direction}

💵 الدخول الفعلي:
{entry:.2f}

🚪 الخروج:
{current_price:.2f}

📊 النتيجة:
{sign}{points:.2f} نقطة

📈 النسبة:
{sign}{percent:.2f}%

━━━━━━━━━━━━━━

🔄 سيتم بدء تحليل جديد تلقائيًا.
"""
    )

    # --------------------------------------------------------
    # AUTO RE-ANALYSIS
    # --------------------------------------------------------

    if user_id in autopilot_users:

        await asyncio.sleep(
            AUTO_REANALYZE_DELAY
        )

        # Check that no new trade was created
        # while waiting.

        active = get_active_trade(
            user_id
        )

        if active is None:

            await perform_gold_analysis(
                user_id
            )


# ============================================================
# MONITOR LOOP
# ============================================================

async def monitor_trades_loop():

    print(
        "Trade monitor started."
    )

    last_heartbeat_price = None

    while True:

        try:

            active_trades = get_all_active_trades()

            # ------------------------------------------------
            # NO ACTIVE RECOMMENDATION / TRADE
            #
            # DO NOT ASK TWELVE DATA.
            # ------------------------------------------------

            if not active_trades:

                await asyncio.sleep(
                    MONITOR_INTERVAL
                )

                continue

            # ------------------------------------------------
            # There is at least one waiting_entry or in_trade.
            #
            # Only now do we ask Twelve Data for current price.
            # ------------------------------------------------

            snapshot = get_market_snapshot()

            previous_price = snapshot["price"]

            try:

                current_price = await asyncio.to_thread(
                    fetch_gold_price,
                    True
                )

            except Exception as e:

                print(
                    f"Monitor price error: {e}"
                )

                await asyncio.sleep(
                    MONITOR_INTERVAL
                )

                continue

            # ------------------------------------------------
            # Save latest known price.
            # ------------------------------------------------

            last_heartbeat_price = current_price

            # fetch_gold_price has already updated market_state
            # but we need the previous price that existed BEFORE
            # this request.
            #
            # Therefore we use the local previous_price captured
            # above.
            # ------------------------------------------------

            for trade in active_trades:

                try:

                    status = trade["status"]

                    # ========================================
                    # WAITING FOR ENTRY
                    # ========================================

                    if status == "waiting_entry":

                        entered = check_entry_crossing(
                            trade,
                            previous_price,
                            current_price
                        )

                        if entered:

                            await process_entry(
                                trade,
                                current_price
                            )

                            continue

                    # ========================================
                    # IN TRADE
                    # ========================================

                    elif status == "in_trade":

                        result = check_sl_tp(
                            trade,
                            current_price
                        )

                        if result:

                            await process_close(
                                trade,
                                current_price,
                                result
                            )

                except Exception as e:

                    print(
                        f"Trade processing error: {e}"
                    )

        except Exception as e:

            print(
                f"Monitor loop error: {e}"
            )

        await asyncio.sleep(
            MONITOR_INTERVAL
        )


# ============================================================
# HEARTBEAT
# ============================================================

async def heartbeat_loop():

    print(
        "Heartbeat loop started."
    )

    while True:

        await asyncio.sleep(
            HEARTBEAT_INTERVAL
        )

        try:

            active_trades = get_all_active_trades()

            if not active_trades:
                continue

            # ------------------------------------------------
            # IMPORTANT:
            #
            # DO NOT call Twelve Data here.
            #
            # We use the latest price obtained by the
            # 15-second monitor.
            # ------------------------------------------------

            snapshot = get_market_snapshot()

            current_price = snapshot["price"]

            updated_at = snapshot["updated_at"]

            if current_price is None:

                for trade in active_trades:

                    await send_text(
                        trade["user_id"],
                        "⚠️ نبضة المتابعة\n\n"
                        "🤖 البوت ما زال يعمل، "
                        "لكن لا يوجد سعر حديث متاح حاليًا.\n\n"
                        "👀 سيستمر النظام بالمحاولة."
                    )

                continue

            if updated_at:

                age = (
                    time.time()
                    - updated_at
                )

            else:

                age = 0

            for trade in active_trades:

                user_id = trade["user_id"]

                direction = trade["direction"]

                entry = float(
                    trade["armed_price"]
                    if trade["status"] == "in_trade"
                    and trade["armed_price"] is not None
                    else trade["entry"]
                )

                if trade["status"] == "in_trade":

                    points, percent = calculate_pnl(
                        direction,
                        entry,
                        current_price
                    )

                    sign = (
                        "+"
                        if points >= 0
                        else ""
                    )

                    await send_text(
                        user_id,
                        f"""
⏱️ نبضة المتابعة

💰 السعر الحالي:
{current_price:.2f}

📈 الاتجاه:
{direction}

💵 الدخول الفعلي:
{entry:.2f}

📊 الربح/الخسارة الحالية:
{sign}{points:.2f} نقطة

📈 النسبة:
{sign}{percent:.2f}%

🔥 الحالة: داخل الصفقة.
👀 أراقب SL و TP.

🕐 آخر تحديث للسعر:
منذ {age:.0f} ثانية
"""
                    )

                else:

                    planned_entry = float(
                        trade["entry"]
                    )

                    if direction == "BUY":

                        distance = (
                            planned_entry
                            - current_price
                        )

                    else:

                        distance = (
                            current_price
                            - planned_entry
                        )

                    await send_text(
                        user_id,
                        f"""
⏱️ نبضة المتابعة

💰 السعر الحالي:
{current_price:.2f}

📈 الاتجاه:
{direction}

🎯 الدخول المخطط:
{planned_entry:.2f}

📏 المسافة عن الدخول:
{distance:.2f}

⏳ الحالة:
بانتظار عبور منطقة الدخول.

👀 ما دخلنا الصفقة بعد.
"""
                    )

        except Exception as e:

            print(
                f"Heartbeat error: {e}"
            )


# ============================================================
# CALLBACKS
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    data = query.data

    # --------------------------------------------------------
    # BACK
    # --------------------------------------------------------

    if data == "back_menu":

        users_chat_history.pop(
            user_id,
            None
        )

        await query.edit_message_text(
            "🤖 القائمة الرئيسية:",
            reply_markup=main_menu()
        )

        return

    # --------------------------------------------------------
    # GEMINI CHAT
    # --------------------------------------------------------

    if data == "gemini_chat":

        await gemini_chat_start(
            query,
            user_id
        )

        return

    # --------------------------------------------------------
    # CURRENT PRICE
    # --------------------------------------------------------

    if data == "current_price":

        await current_price(
            query,
            user_id
        )

        return

    # --------------------------------------------------------
    # M5
    # --------------------------------------------------------

    if data == "gold_analysis":

        await m5_analysis(
            query
        )

        return

    # --------------------------------------------------------
    # M1
    # --------------------------------------------------------

    if data == "m1_analysis":

        await m1_analysis(
            query
        )

        return

    # --------------------------------------------------------
    # FULL GEMINI ANALYSIS
    # --------------------------------------------------------

    if data == "gemini_analysis":

        active = get_active_trade(
            user_id
        )

        if active:

            if active["status"] == "waiting_entry":

                await query.edit_message_text(
                    "⏳ عندك توصية بالفعل تنتظر الدخول.\n\n"
                    "لا يمكن إنشاء توصية ثانية حتى يتم "
                    "إغلاق الحالية أو إيقافها.",
                    reply_markup=back_button()
                )

            else:

                await query.edit_message_text(
                    "🔥 عندك صفقة مفتوحة بالفعل.\n\n"
                    "البوت يراقب SL و TP.",
                    reply_markup=back_button()
                )

            return

        await query.edit_message_text(
            "⏳ جاري تحليل الذهب بالكامل..."
        )

        await perform_gold_analysis(
            user_id
        )

        return

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    if data == "stop_all":

        autopilot_users.discard(
            user_id
        )

        cancel_active_trade(
            user_id
        )

        await query.edit_message_text(
            "⛔ تم إيقاف التحليل والمتابعة.\n\n"
            "تم إلغاء أي توصية أو صفقة مسجلة كحالة نشطة.",
            reply_markup=main_menu()
        )

        return


# ============================================================
# MODELS COMMAND
# ============================================================

async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        models = get_available_models()

        if not models:

            await update.message.reply_text(
                "⚠️ تعذر جلب قائمة Gemini models."
            )

            return

        text = (
            "🤖 نماذج Gemini المتاحة:\n\n"
            + "\n".join(
                f"• {model}"
                for model in models
            )
        )

        await update.message.reply_text(
            text
        )

    except Exception as e:

        await update.message.reply_text(
            f"⚠️ خطأ:\n{e}"
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    print(
        "Telegram error:",
        context.error
    )


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application
):

    global monitor_task
    global heartbeat_task

    monitor_task = asyncio.create_task(
        monitor_trades_loop()
    )

    heartbeat_task = asyncio.create_task(
        heartbeat_loop()
    )

    print(
        "Background monitoring tasks started."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global bot_application

    init_db()

    # --------------------------------------------------------
    # Start Flask
    # --------------------------------------------------------

    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    bot_application = application

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
            button_handler
        )
    )

    # Gemini chat messages

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_chat_message
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "================================="
    )

    print(
        "Gold Gemini Bot starting..."
    )

    print(
        f"PORT: {PORT}"
    )

    print(
        "================================="
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
