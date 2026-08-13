"""
بوت تنبيهات الأسهم الأمريكية

طريقة التشغيل:
    python bot.py run        -> يشغّل البوت ويستقبل الأوامر من تيليجرام
    python bot.py            -> يرسل رسالة تجريبية
    python bot.py chatid     -> يطبع رقم المحادثة (chat_id) حقك
    python bot.py price AAPL -> يجيب سعر السهم ويرسله على تيليجرام
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# مهام طويلة شغّالة الحين — يمنع تشغيل نفس المهمة مرتين مع بعض
BUSY = {}

# منظّم معدّل Finnhub — مشترك بين كل المسارات.
#
# ليش على مستوى الملف مو داخل كل دالة: بعد إضافة الغربلة السريعة (المرحلة ب)
# صار في مسارين يطلبون من Finnhub بالتوازي — /refresh في خيط، والفحص في خيط
# ثاني. لو كل واحد نظّم نفسه على 55 طلب/دقيقة، مجموعهم 110 وهذا فوق حد
# Finnhub (60) ويجيب أخطاء 429. منظّم واحد يمر منه الكل يمنع هذا نهائياً.
FINNHUB_RATE_LIMIT_PER_MINUTE = 55  # هامش أمان تحت حد Finnhub الفعلي (60)
_finnhub_lock = threading.Lock()
_finnhub_next_slot = 0.0


def finnhub_wait_for_slot():
    """يحجز الفتحة التالية لطلب Finnhub وينتظرها. آمن مع الخيوط المتعددة."""
    global _finnhub_next_slot

    with _finnhub_lock:
        now = time.time()
        slot = max(now, _finnhub_next_slot)
        _finnhub_next_slot = slot + (60 / FINNHUB_RATE_LIMIT_PER_MINUTE)

    wait = slot - time.time()
    if wait > 0:
        time.sleep(wait)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
WATCHLIST_PATH = os.path.join(HERE, "watchlist.json")
WATCHLIST_BACKUP_PATH = os.path.join(HERE, "watchlist.backup.json")
PAPER_TRADES_PATH = os.path.join(HERE, "paper_trades.json")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
FINNHUB_API = "https://finnhub.io/api/v1/{endpoint}"
TWELVEDATA_API = "https://api.twelvedata.com/{endpoint}"

# علامة تخلي الأرقام والحروف الإنجليزية تظهر بالاتجاه الصحيح داخل نص عربي.
# بدونها الأرقام السالبة مثل -2.43 تنقلب وتظهر 2.43- في تيليجرام.
LTR = "\u200e"


def plural(count, word):
    """Simple English pluralisation for bot messages: 1 stock, 2 stocks."""
    return word if count == 1 else word + "s"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print("config.json not found")
        print("Copy config.example.json to config.json and fill in your details.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    if not config.get("telegram_bot_token"):
        print("telegram_bot_token is empty in config.json")
        sys.exit(1)

    return config


# ---------------------------------------------------------------
# حالة السوق الأمريكي
#
# الجوال ما عنده قاعدة بيانات المناطق الزمنية (zoneinfo ما يشتغل عليه)،
# فنحسب توقيت نيويورك بأنفسنا. قاعدة التوقيت الصيفي في أمريكا ثابتة بقانون
# من 2007: يبدأ ثاني أحد في مارس، وينتهي أول أحد في نوفمبر.
# ---------------------------------------------------------------

def _nth_sunday(year, month, n):
    """يرجّع رقم يوم الأحد رقم n في الشهر. مثال: ثاني أحد في مارس."""
    weekday_of_first = datetime(year, month, 1).weekday()  # الاثنين=0، الأحد=6
    first_sunday = 1 + (6 - weekday_of_first) % 7
    return first_sunday + (n - 1) * 7


def new_york_offset(utc_now):
    """فرق نيويورك عن UTC: -4 ساعات صيفاً، -5 شتاءً."""
    year = utc_now.year
    # التحويل يصير الساعة 2 فجراً بالتوقيت المحلي
    summer_starts = datetime(year, 3, _nth_sunday(year, 3, 2), 7)   # 2:00 EST = 07:00 UTC
    summer_ends = datetime(year, 11, _nth_sunday(year, 11, 1), 6)   # 2:00 EDT = 06:00 UTC
    return -4 if summer_starts <= utc_now < summer_ends else -5


def new_york_now(utc_now=None):
    utc_now = utc_now or datetime.now(timezone.utc).replace(tzinfo=None)
    return utc_now + timedelta(hours=new_york_offset(utc_now))


def market_state(utc_now=None):
    """يرجّع (مفتوح؟، وصف الحالة). السوق: 9:30 - 16:00 نيويورك، من الاثنين للجمعة.

    ملاحظة: ما يعرف الإجازات الرسمية الأمريكية بعد. أسوأ نتيجة إن البوت
    يفحص في يوم إجازة ويصرف شوي أرصدة بدون فايدة — ما يضر.
    """
    ny = new_york_now(utc_now)

    if ny.weekday() >= 5:
        return False, "Closed — weekend"

    minutes = ny.hour * 60 + ny.minute
    if minutes < 9 * 60 + 30:
        return False, "Closed — opens 9:30 AM New York time"
    if minutes >= 16 * 60:
        return False, "Closed — session ended"

    return True, "Open"


# ---------------------------------------------------------------
# الحالة المحفوظة (تبقى بعد إعادة التشغيل)
# ---------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # ملف حالة تالف ما يستاهل يوقف البوت
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def telegram_request(token, method, params=None, fatal=True):
    url = TELEGRAM_API.format(token=token, method=method)
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    # المهلة أطول من مهلة الانتظار الطويل (long polling) عشان ما تقطعها
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # تيليجرام يرجع سبب الخطأ داخل جسم الرد حتى مع أكواد الخطأ
        return json.loads(error.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        if not fatal:
            # داخل حلقة التشغيل المستمر، انقطاع النت لحظة ما يوقف البوت
            return None
        print(f"Could not reach Telegram. Check your internet. ({reason})")
        sys.exit(1)


def send_message(token, chat_id, text):
    return telegram_request(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text},
    )


TELEGRAM_MAX_MESSAGE_LENGTH = 4096  # حد تيليجرام الفعلي لطول الرسالة الواحدة


def chunk_message(text, limit=TELEGRAM_MAX_MESSAGE_LENGTH):
    """يقسّم رسالة طويلة لعدة أجزاء تحت حد تيليجرام، بدون ما يقطع سطر من نصه."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_message_chunked(token, chat_id, text):
    """زي send_message، بس يقسّم لعدة رسائل لو تجاوز حد تيليجرام — ما نخفي بيانات أبداً.

    حادثة 2026-08-12: /list كان يقصّ القائمة عند 40 سهم ويكتب "و X غيرهم"
    بدل ما يعرض الباقي فعلياً. هذي دالة عامة تحل المشكلة لكل أمر، مو /list بس.
    """
    for chunk in chunk_message(text):
        send_message(token, chat_id, chunk)


def print_chat_ids(token):
    """يجيب آخر الرسائل الواصلة للبوت ويطبع منها رقم المحادثة."""
    result = telegram_request(token, "getUpdates")
    updates = result.get("result", [])

    if not updates:
        print("The bot has not received any messages.")
        print("Open Telegram, message your bot anything, then run this command again.")
        return

    seen = set()
    for update in updates:
        chat = update.get("message", {}).get("chat")
        if chat and chat["id"] not in seen:
            seen.add(chat["id"])
            name = chat.get("first_name") or chat.get("title") or "no name"
            print(f"chat_id = {chat['id']}   ({name})")


def finnhub_request(api_key, endpoint, params, fatal=True):
    """يرسل طلب لـ Finnhub ويرجّع الرد كـ dict.

    fatal=True  -> يطبع الخطأ ويوقف البرنامج (للأوامر من الطرفية)
    fatal=False -> يرجّع {"_error": "..."} أو None (داخل حلقة البوت المستمرة)
    """
    query = dict(params)
    query["token"] = api_key
    url = FINNHUB_API.format(endpoint=endpoint) + "?" + urllib.parse.urlencode(query)

    finnhub_wait_for_slot()  # كل نداء Finnhub يمر من هنا — لا تتجاوزه في أي مسار

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            messages = ["Finnhub key is invalid or no longer active.", "Check finnhub_api_key in config.json"]
        elif error.code == 429:
            messages = ["Exceeded the free plan request limit.", "Wait a minute and try again."]
        else:
            messages = [f"Finnhub returned an error. Code: {error.code}"]

        if not fatal:
            return {"_error": "\n".join(messages)}
        for line in messages:
            print(line)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        if not fatal:
            return None
        print(f"Could not reach Finnhub. Check your internet. ({getattr(error, 'reason', error)})")
        sys.exit(1)


def get_quote(api_key, symbol):
    """يجيب سعر سهم واحد. Finnhub يرجّع أصفار للرمز الغلط بدل ما يرجّع خطأ."""
    quote = finnhub_request(api_key, "quote", {"symbol": symbol})

    if not quote.get("c"):
        print(f"No data found for {symbol}.")
        print("Make sure it is a valid US stock symbol, like AAPL or TSLA.")
        sys.exit(1)

    return quote


def format_quote(symbol, quote):
    """يحوّل رد Finnhub لرسالة عربية مرتبة."""
    # فينهب يرجّع النسبة بأربع خانات عشرية (-2.1702) — نقصّرها لخانتين
    change = round(quote.get("d") or 0, 2)
    change_percent = round(quote.get("dp") or 0, 2)
    arrow = "🟢" if change >= 0 else "🔴"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return "\n".join(
        [
            f"{arrow} {symbol}",
            "",
            f"Price: {quote['c']}",
            f"Change: {change} ({change_percent}%)",
            f"Day high: {quote.get('h')}",
            f"Day low: {quote.get('l')}",
            f"Prev close: {quote.get('pc')}",
            "",
            f"Time: {now}",
        ]
    )


def send_price(config, symbol):
    api_key = config.get("finnhub_api_key")
    if not api_key:
        print("finnhub_api_key is empty in config.json")
        print("Sign up free at finnhub.io and put the key there.")
        sys.exit(1)

    chat_id = require_chat_id(config)
    quote = get_quote(api_key, symbol)
    response = send_message(config["telegram_bot_token"], chat_id, format_quote(symbol, quote))

    if response.get("ok"):
        print(f"Sent the price for {symbol}. Check Telegram.")
    else:
        print("Send failed:")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        sys.exit(1)


# ---------------------------------------------------------------
# الأوامر
# ---------------------------------------------------------------

# مصدر واحد للأوامر: منه تتبني قائمة تيليجرام (الزر الأزرق) ورسالة /help
# مع بعض. أي أمر جديد ينضاف هنا مرة وحدة، وما يصير اختلاف بين الاثنين.
COMMANDS = [
    ("status", "Bot and market status"),
    ("price", "Stock price — example: /price AAPL"),
    ("list", "Show the watchlist"),
    ("scan", "Scan the watchlist now"),
    ("refresh", "Rebuild the watchlist from the whole market"),
    ("restore", "Restore the watchlist from before the last /refresh"),
    ("mute", "Pause alerts"),
    ("unmute", "Resume alerts"),
    ("journal", "Paper trading results — open positions and win rate"),
    ("help", "Show commands"),
]

HELP_TEXT = "\n".join(
    ["Available commands:", ""] + [f"/{name} — {description}" for name, description in COMMANDS]
)


def publish_command_menu(token):
    """يسجّل الأوامر عند تيليجرام عشان تطلع في زر القائمة داخل المحادثة.

    ينرسل مع كل تشغيل، فالقائمة تتحدث تلقائياً لما نضيف أمر جديد.
    """
    commands = [{"command": name, "description": description} for name, description in COMMANDS]
    result = telegram_request(
        token, "setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)}, fatal=False
    )
    return bool(result and result.get("ok"))


# ---------------------------------------------------------------
# المرحلة 3 — بناء قائمة المراقبة والفلترة
#
# التقسيم: Finnhub يبني القائمة (قيمة سوقية + فلوت)، و Twelve Data يفحصها
# (حجم + VWAP). السبب في CLAUDE.md — كل مصدر يحجب اللي الثاني يعطيه.
# ---------------------------------------------------------------

# شروط الفلترة. قابلة للتعديل من config.json.
DEFAULTS = {
    "min_market_cap_musd": 0.5,     # قراره 2026-08-12 — وُسّع من 40 لـ 500 ألف دولار
    "max_market_cap_musd": 60,      # مليون دولار
    # حد أدنى للسعر: بعد ما نزل الحد الأدنى للقيمة السوقية لـ 500 ألف، ضاعت
    # الحماية الضمنية اللي كانت تجي من (قيمة سوقية 40 مليون ÷ فلوت 15 مليون
    # = سعر ~$2.67). بدونه ترجع أسهم البنسات.
    "min_price": 1.0,
    "min_float_shares": 500_000,
    "max_float_shares": 15_000_000, # قراره 2026-08-12 — فلوت صغير = حركة أوضح
    "min_avg_volume_shares": 50_000,   # مقاس على بيانات حقيقية 2026-08-12 — 300 ألف كانت ترجّع قائمة فاضية
    "min_change_percent": 5.0,      # الغربلة السريعة (المرحلة ب) — مجانية من Finnhub
    # بورصات حقيقية بس. OOTC (أسهم OTC) مستبعدة عمداً: 73% من السوق، وأغلبها
    # أسهم أجنبية بأجزاء من السنت ما تصلح لاستراتيجية قفزة حجم خلال اليوم.
    "allowed_exchanges": ["XNAS", "XNYS", "XASE"],  # ناسداك، نيويورك، نيويورك أمريكان
    # الشرط الرابع الموسّع (قراره 2026-08-12): مو VWAP لحاله، بل حزمة مؤشرات
    # على فريم 5 دقائق — نفس فريم VWAP، متسق مع استراتيجية قفزة الحجم اليومية.
    "indicator_interval": "5min",
    "ema_periods": [5, 10, 20],     # السعر لازم يكون فوق الثلاثة
    "volume_spike_ratio": 2.0,      # تجريبي — يتعدّل بعد ما نشوف نتائج حقيقية
    "scan_interval_minutes": 15,
    "atr_multiplier": 1.75,         # مضاعف وقف الخسارة، يبدأ بين 1.5 و 2 حسب المواصفات
    "max_watchlist_size": 300,      # Twelve Data ما عادت القيد بعد الغربلة المجانية
    "escalation_cooldown_minutes": 60,  # سهم طالع طول اليوم ما يستهلك رصيد كل فحصة
    "daily_credit_budget": 750,     # هامش أمان تحت حد Twelve Data اليومي (800)
}


def setting(config, key):
    return config.get(key, DEFAULTS[key])


def load_watchlist():
    if not os.path.exists(WATCHLIST_PATH):
        return []
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_watchlist(watchlist):
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)


def backup_current_watchlist():
    """ينسخ القائمة الحالية لملف احتياطي واحد، قبل ما /refresh يبدأ يستبدلها.

    نسخة وحدة بس (مو تاريخ كامل) — كل /refresh جديد يستبدل النسخة
    الاحتياطية القديمة بالقائمة اللي كانت شغالة قبله مباشرة.
    """
    current = load_watchlist()
    if not current:
        return False
    with open(WATCHLIST_BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    return True


def load_watchlist_backup():
    if not os.path.exists(WATCHLIST_BACKUP_PATH):
        return None
    try:
        with open(WATCHLIST_BACKUP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------
# دفتر الصفقات الورقية (paper trading journal) — قرار 2026-08-13
#
# قياس دقة التنبيهات قبل الثقة فيها للتداول الحقيقي، بدل الاشتراك بمصادر
# مدفوعة (Ortex/Fintel/Polygon) لبيانات شورت/CTB ما لها بديل مجاني كافي.
# كل صفقة تُسوّى بـ Finnhub quote المجاني بس — صفر تكلفة على أرصدة
# Twelve Data.
# ---------------------------------------------------------------

def load_paper_trades():
    if not os.path.exists(PAPER_TRADES_PATH):
        return []
    try:
        with open(PAPER_TRADES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_paper_trades(trades):
    with open(PAPER_TRADES_PATH, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


def open_paper_trade(symbol, entry_price, stop_loss):
    """يفتح صفقة ورقية لتنبيه جديد. يتجاهل لو فيه صفقة مفتوحة على نفس
    الرمز أصلاً (يمنع ازدواج الإحصائيات)."""
    trades = load_paper_trades()
    if any(t["symbol"] == symbol and t["status"] == "open" for t in trades):
        return

    trades.append(
        {
            "symbol": symbol,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "opened_at_ny": new_york_now().isoformat(),
            "status": "open",
            "exit_price": None,
            "exit_reason": None,
            "closed_at_ny": None,
            "pct_return": None,
        }
    )
    save_paper_trades(trades)


def settle_paper_trades(api_key, now_ny=None):
    """يفحص كل صفقة مفتوحة ويقفلها لو ضربت وقف الخسارة أو انتهى يوم
    التداول اللي فُتحت فيه.

    مدة الصفقة يوم تداول واحد (قراره 2026-08-13): تُقفل عند إغلاق السوق
    (16:00 نيويورك) لنفس يوم الفتح، أو فوراً لو صار يوم تقويمي جديد —
    هذا الشرط الثاني يغطي حالة إعادة تشغيل البوت بعد توقف تلقائياً، بدون
    أي منطق إضافي.
    """
    now_ny = now_ny or new_york_now()
    trades = load_paper_trades()
    changed = False

    for trade in trades:
        if trade["status"] != "open":
            continue

        opened_ny = datetime.fromisoformat(trade["opened_at_ny"])
        same_day = now_ny.date() == opened_ny.date()
        market_closed_today = now_ny.hour * 60 + now_ny.minute >= 16 * 60
        time_to_exit = (not same_day) or (same_day and market_closed_today)

        quote = finnhub_request(api_key, "quote", {"symbol": trade["symbol"]}, fatal=False)
        price = quote.get("c") if isinstance(quote, dict) else None
        if not price:
            continue  # ما قدرنا نجيب سعر — نجرب الدورة الجاية، ما نقفل بلا سعر

        exit_reason = None
        if price <= trade["stop_loss"]:
            exit_reason = "stop_loss"
        elif time_to_exit:
            exit_reason = "time_exit"

        if exit_reason:
            trade["status"] = "closed"
            trade["exit_price"] = price
            trade["exit_reason"] = exit_reason
            trade["closed_at_ny"] = now_ny.isoformat()
            trade["pct_return"] = round((price - trade["entry_price"]) / trade["entry_price"] * 100, 2)
            changed = True

    if changed:
        save_paper_trades(trades)

    return trades


def paper_trade_stats(trades):
    """ملخص الصفقات المقفلة: العدد، نسبة النجاح، متوسط العائد."""
    closed = [t for t in trades if t["status"] == "closed"]
    if not closed:
        return {"count": 0, "win_rate": None, "avg_return": None}

    wins = sum(1 for t in closed if t["pct_return"] > 0)
    return {
        "count": len(closed),
        "win_rate": round(wins / len(closed) * 100, 1),
        "avg_return": round(sum(t["pct_return"] for t in closed) / len(closed), 2),
    }


def cmd_journal(config):
    api_key = config.get("finnhub_api_key")
    trades = load_paper_trades()
    if not trades:
        return "Journal is empty — no alert has opened a paper trade yet."

    open_trades = [t for t in trades if t["status"] == "open"]
    closed = [t for t in trades if t["status"] == "closed"]

    lines = ["📔 Paper Trading Journal", ""]

    if open_trades:
        lines.append(f"Open ({len(open_trades)}):")
        for t in open_trades:
            quote = finnhub_request(api_key, "quote", {"symbol": t["symbol"]}, fatal=False) if api_key else None
            price = quote.get("c") if isinstance(quote, dict) else None
            if price:
                unrealized = round((price - t["entry_price"]) / t["entry_price"] * 100, 2)
                lines.append(f"  {t['symbol']} — entry {t['entry_price']}, now {price} ({unrealized:+}%)")
            else:
                lines.append(f"  {t['symbol']} — entry {t['entry_price']}")
        lines.append("")

    stats = paper_trade_stats(trades)
    if stats["count"]:
        lines += [
            f"Closed ({stats['count']}):",
            f"  Win rate: {stats['win_rate']}%",
            f"  Average return: {stats['avg_return']:+}%",
        ]
    else:
        lines.append("No closed trades yet.")

    return "\n".join(lines)


def daily_journal_summary(trades, now_ny):
    """ملخص اليوم عند إغلاق السوق: كم صفقة فُتحت وقُفلت اليوم، ونسبة نجاح
    اليوم، مع الإحصائية الكلية للدفتر بالكامل."""
    today = now_ny.date()
    today_closed = [
        t for t in trades
        if t["status"] == "closed" and datetime.fromisoformat(t["closed_at_ny"]).date() == today
    ]

    lines = ["📔 Daily Journal Summary", ""]
    if not today_closed:
        lines.append("No paper trades closed today.")
    else:
        wins = sum(1 for t in today_closed if t["pct_return"] > 0)
        stopped = sum(1 for t in today_closed if t["exit_reason"] == "stop_loss")
        lines += [
            f"Closed today: {len(today_closed)} ({stopped} by stop loss, {len(today_closed) - stopped} at day-end)",
            f"Today's win rate: {round(wins / len(today_closed) * 100, 1)}%",
        ]

    overall = paper_trade_stats(trades)
    if overall["count"]:
        lines += ["", f"All-time: {overall['count']} closed, {overall['win_rate']}% win rate, {overall['avg_return']:+}% avg return"]

    return "\n".join(lines)


def us_common_stocks(api_key, config=None):
    """أسهم عادية مدرجة في بورصات حقيقية. الصناديق والسندات وأسهم OTC مستبعدة.

    فلترة البورصة **مجانية تماماً** — حقل `mic` يجي مع نفس الرد، بدون أي طلب
    إضافي. أثرها كبير: 18,434 سهم "عادي" تصير 4,951، لأن 13,483 منها OOTC
    (أسهم OTC)، وأغلبها أجنبية بأجزاء من السنت. هذا وحده يقصّر /refresh من
    ~5.6 ساعة إلى ~1.5 ساعة، ويشيل أغلب الأسهم اللي ما تصلح للتداول أصلاً.
    """
    symbols = finnhub_request(api_key, "stock/symbol", {"exchange": "US"}, fatal=False)
    if not isinstance(symbols, list):
        return []

    allowed = set(setting(config or {}, "allowed_exchanges"))
    return sorted(
        {
            s["symbol"]
            for s in symbols
            if s.get("type") == "Common Stock"
            and "." not in s.get("symbol", "")
            and s.get("mic") in allowed
        }
    )


def passes_size_filter(config, profile):
    """شرط 1 و 2: القيمة السوقية والفلوت.

    انتبه: فينهب يرجّع الاثنين بالمليون. القيمة السوقية 45 تعني 45 مليون
    دولار، والفلوت 0.6 يعني 600 ألف سهم.

    **الحد الأقصى للفلوت (قراره 2026-08-12)** له فايدة مزدوجة: فلوت صغير يعني
    حركة سعرية أوضح عند قفزة الحجم، **وكمان يفرض حد أدنى للسعر تلقائياً** —
    قيمة سوقية 40-60 مليون مقسومة على فلوت أقصاه 15 مليون سهم = سعر ~$2.67
    على الأقل. فمشكلة أسهم أجزاء السنت تنحل بدون فلتر سعر منفصل.
    """
    market_cap = profile.get("marketCapitalization")
    float_shares_m = profile.get("floatingShare")

    if not market_cap or not float_shares_m:
        return False

    float_shares = float_shares_m * 1_000_000
    return (
        setting(config, "min_market_cap_musd") <= market_cap <= setting(config, "max_market_cap_musd")
        and setting(config, "min_float_shares") < float_shares <= setting(config, "max_float_shares")
    )


def average_daily_volume(api_key, symbol):
    """متوسط حجم التداول اليومي (10 أيام) من Finnhub — مجاني، ما يكلف رصيد Twelve Data.

    ⚠️ فخ وحدات: `10DayAverageTradingVolume` يرجع **بالمليون** — القيمة
    0.19654 تعني 196,540 سهم. نفس فخ القيمة السوقية بالضبط.

    يرجّع None لو الطلب فشل أو الحقل ناقص (فيسقط السهم بدل ما نخمّن).
    """
    data = finnhub_request(api_key, "stock/metric", {"symbol": symbol, "metric": "all"}, fatal=False)

    if not isinstance(data, dict) or data.get("_error"):
        return None

    metric = data.get("metric") or {}
    raw = metric.get("10DayAverageTradingVolume")
    if raw in (None, ""):
        return None

    try:
        return float(raw) * 1_000_000
    except (TypeError, ValueError):
        return None


def build_watchlist(config, progress=None):
    """المرحلة أ — يبني قائمة المراقبة من Finnhub وحده (مجاناً، بدون أي رصيد
    Twelve Data)، ويوقف فور ما يوصل `max_watchlist_size`.

    **ثلاث بوابات، مرتبة من الأرخص للأغلى:**
    1. البورصة — مجانية تماماً، تجي مع قائمة الرموز نفسها (`us_common_stocks`)
    2. القيمة السوقية والفلوت — طلب `profile2` واحد لكل رمز
    3. متوسط حجم التداول — طلب `stock/metric` **للناجين من (2) بس**

    ترتيب (2) قبل (3) مقصود ومهم: لو طلبنا `metric` لكل رمز، تصير الطلبات
    ضعف العدد والمدة ~3 ساعات بدل ~1.5. الأغلبية الساحقة تسقط عند (2)، فما
    توصل (3) أصلاً.

    شغل طويل يشتغل في الخلفية عشان ما يعطّل الأوامر. **يحفظ القائمة فور ما
    يلقى سهم جديد، مو بس في النهاية** — لو انقطع الشغل لأي سبب (إعادة تشغيل،
    انهيار، انقطاع نت) اللي اتلقى يبقى محفوظ. (درس 2026-08-11: إعادة تشغيل
    أثناء فحص وصل 1500/18424 مسحت 25 سهم لأن الحفظ كان بالنهاية بس.)

    التنظيم الزمني للطلبات صار داخل `finnhub_request()` عبر المنظّم المشترك —
    ما نحسبه هنا، عشان الفحص المتزامن ما يتجاوز حد Finnhub معنا.
    """
    api_key = config["finnhub_api_key"]
    max_size = setting(config, "max_watchlist_size")
    min_avg_volume = setting(config, "min_avg_volume_shares")
    symbols = us_common_stocks(api_key, config)

    if not symbols:
        return None, "Could not fetch the stock list from Finnhub."

    found = []
    checked = 0

    for symbol in symbols:
        profile = finnhub_request(api_key, "stock/profile2", {"symbol": symbol}, fatal=False)
        checked += 1

        if not isinstance(profile, dict) or profile.get("_error"):
            continue

        if passes_size_filter(config, profile):
            # البوابة الثالثة: السيولة. تُطلب هنا بس — بعد ما السهم عدّى
            # القيمة السوقية والفلوت — عشان ما نضاعف عدد الطلبات على السوق كله.
            avg_volume = average_daily_volume(api_key, symbol)
            if avg_volume is None or avg_volume < min_avg_volume:
                continue

            found.append(
                {
                    "symbol": symbol,
                    "name": profile.get("name", ""),
                    "market_cap_musd": round(profile["marketCapitalization"], 1),
                    "float_shares": int(profile["floatingShare"] * 1_000_000),
                    "avg_volume": int(avg_volume),
                }
            )
            save_watchlist(found)  # حفظ فوري، مو بانتظار نهاية المسح الكامل

            if len(found) >= max_size:
                break

        if progress and checked % 250 == 0:
            progress(checked, len(symbols), len(found))

    if progress:
        progress(checked, len(symbols), len(found))  # آخر تحديث قبل ما نرجع، حتى لو ما كان مضاعف 250

    return found, None


def is_credit_exhausted(message):
    """يتعرّف على رسالة "خلصت أرصدة اليوم" من Twelve Data تحديداً."""
    return "run out of api credits" in (message or "").lower()


def next_utc_midnight(now=None):
    """أرصدة Twelve Data تتجدد عند منتصف الليل UTC، مو منتصف الليل السعودي."""
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------
# حماية ميزانية Twelve Data — عدّاد يومي + تهدئة لكل رمز
#
# درس حادثة 2026-08-11: الأرصدة نفدت وسط دوام السوق فبقي البوت أعمى ساعتين.
# الغربلة المجانية (المرحلة ب) قللت الاستهلاك كثير، لكن ما تلغي الحاجة لسقف
# صريح — يوم فيه حركة قوية بالسوق ممكن يرفع عدد المرشحين فجأة.
# ---------------------------------------------------------------

def credits_used_today(state, now=None):
    """الأرصدة المستهلكة اليوم. يُصفّر تلقائياً مع بداية يوم UTC جديد."""
    now = now or datetime.now(timezone.utc)
    if state.get("credits_date") != now.strftime("%Y-%m-%d"):
        return 0
    try:
        return int(state.get("credits_used", 0))
    except (TypeError, ValueError):
        return 0


def record_credits(state, count, now=None):
    """يسجّل استهلاك أرصدة. رصيد واحد لكل رمز، مو لكل طلب HTTP."""
    now = now or datetime.now(timezone.utc)
    state["credits_date"] = now.strftime("%Y-%m-%d")
    state["credits_used"] = credits_used_today(state, now) + count


def escalation_allowed(state, symbol, cooldown_minutes, now=None):
    """هل مسموح نصرف رصيد على هذا الرمز الحين؟ (تهدئة بعد آخر تصعيد له)"""
    now = now or datetime.now(timezone.utc)
    last = (state.get("last_escalation") or {}).get(symbol)
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=cooldown_minutes)
    except (TypeError, ValueError):
        return True  # طابع وقت تالف ما يستاهل يعطّل الرمز للأبد


def record_escalation(state, symbol, now=None):
    now = now or datetime.now(timezone.utc)
    state.setdefault("last_escalation", {})[symbol] = now.isoformat()


def find_movers(config, state, watchlist):
    """المرحلة ب — الغربلة السريعة المجانية.

    Finnhub `quote` يعطي نسبة التغير اليومي (`dp`) **مجاناً**، بحد 60 طلب في
    الدقيقة. فبدل ما نصرف رصيد Twelve Data على كل سهم في القائمة كل فحصة،
    نغربل هنا أول ونمرّر المتحركين بس.

    يرجّع المرشحين **مرتبين تنازلياً بنسبة التغير** — لو ضربنا سقف الميزانية،
    الأقوى حركةً يتفحص أول.
    """
    api_key = config.get("finnhub_api_key")
    if not api_key:
        return [], "finnhub_api_key is empty in config.json"

    threshold = setting(config, "min_change_percent")
    cooldown = setting(config, "escalation_cooldown_minutes")

    movers = []
    for item in watchlist:
        symbol = item["symbol"]

        # التهدئة تُفحص قبل الطلب — توفّر وقت Finnhub كمان، مو الأرصدة بس
        if not escalation_allowed(state, symbol, cooldown):
            continue

        quote = finnhub_request(api_key, "quote", {"symbol": symbol}, fatal=False)
        if not isinstance(quote, dict) or quote.get("_error"):
            continue

        try:
            change = float(quote.get("dp") or 0)
            price = float(quote.get("c") or 0)
        except (TypeError, ValueError):
            continue

        if price >= setting(config, "min_price") and change >= threshold:
            movers.append({"symbol": symbol, "change_percent": change})

    movers.sort(key=lambda m: m["change_percent"], reverse=True)
    return movers, None


def twelvedata_request(api_key, endpoint, params):
    """طلب لـ Twelve Data. يرجّع None لو صار خطأ شبكة."""
    query = dict(params)
    query["apikey"] = api_key
    url = TWELVEDATA_API.format(endpoint=endpoint) + "?" + urllib.parse.urlencode(query)

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def td_indicator(config, state, api_key, endpoint, params):
    """طلب مؤشر من Twelve Data مع احتساب الرصيد وفحص السقف اليومي.

    يرجّع (القيم، خطأ_قاتل). الخطأ القاتل يوقف الفحص كله (نفاد أرصدة أو
    بلوغ السقف). أي فشل آخر يرجّع (None, None) — يعني "السهم ما عدّى"، مو
    "الفحص انهار".
    """
    if credits_used_today(state) + 1 > setting(config, "daily_credit_budget"):
        return None, f"Reached the daily credit budget ({setting(config, 'daily_credit_budget')} credits)"

    data = twelvedata_request(api_key, endpoint, params)
    record_credits(state, 1)
    time.sleep(8)  # حد 8 أرصدة في الدقيقة

    if not data:
        return None, None
    if data.get("code"):
        message = f"Twelve Data: {data.get('message', 'unknown error')}"
        return None, message if is_credit_exhausted(message) else None

    return data.get("values") or [], None


def supertrend_crossed_up(st_values, ts_values):
    """تقاطع SuperTrend صاعد خلال آخر شمعتين (قراره 2026-08-12).

    المطلوب: السعر **فوق** SuperTrend الحين، **وكان تحته** في إحدى الشمعتين
    السابقتين — يعني التقاطع صار للتو، مو من زمان.

    ⚠️ القيم تجي من Twelve Data **الأحدث أولاً** — مؤكد باختبار حقيقي
    2026-08-12. لو انقلب الترتيب يوماً، منطق التقاطع كله ينعكس.
    """
    if not st_values or not ts_values:
        return False, None

    supertrend = {}
    for v in st_values:
        try:
            supertrend[v["datetime"]] = float(v["supertrend"])
        except (KeyError, TypeError, ValueError):
            continue

    closes = {}
    for v in ts_values:
        try:
            closes[v["datetime"]] = float(v["close"])
        except (KeyError, TypeError, ValueError):
            continue

    # نمشي بترتيب رد supertrend نفسه (الأحدث أولاً)، وناخذ اللي له سعر مطابق
    times = [v["datetime"] for v in st_values if v.get("datetime") in closes]
    if len(times) < 2:
        return False, None

    above = [closes[t] > supertrend[t] for t in times]

    if not above[0]:
        return False, None  # مو فوقه الحين أصلاً

    # كان تحته في إحدى الشمعتين السابقتين → التقاطع حديث
    if any(not a for a in above[1:3]):
        return True, supertrend[times[0]]

    return False, None


def passes_indicators(config, state, api_key, symbol, price):
    """الشرط الرابع الموسّع: فوق EMA5/10/20، وهيستوجرام MACD موجب، وتقاطع
    SuperTrend صاعد حديث.

    **الترتيب مقصود — يوقف عند أول فشل ويوفّر باقي الأرصدة.** MACD أول لأنه
    رصيد واحد ويرفض قرابة النصف، وSuperTrend آخر شي لأنه يكلف رصيدين.

    يرجّع (نجح؟، التفاصيل، خطأ_قاتل).
    """
    interval = setting(config, "indicator_interval")
    details = {}

    # 1) هيستوجرام MACD موجب
    values, error = td_indicator(
        config, state, api_key, "macd", {"symbol": symbol, "interval": interval, "outputsize": "1"}
    )
    if error:
        return False, details, error
    if not values:
        return False, details, None
    try:
        histogram = float(values[0]["macd_hist"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False, details, None
    if histogram <= 0:
        return False, details, None
    details["macd_hist"] = histogram

    # 2) السعر فوق كل المتوسطات
    for period in setting(config, "ema_periods"):
        values, error = td_indicator(
            config,
            state,
            api_key,
            "ema",
            {"symbol": symbol, "interval": interval, "time_period": str(period), "outputsize": "1"},
        )
        if error:
            return False, details, error
        if not values:
            return False, details, None
        try:
            ema = float(values[0]["ema"])
        except (KeyError, IndexError, TypeError, ValueError):
            return False, details, None
        if price <= ema:
            return False, details, None
        details[f"ema{period}"] = ema

    # 3) تقاطع SuperTrend صاعد حديث — يحتاج المؤشر والأسعار مع بعض
    st_values, error = td_indicator(
        config, state, api_key, "supertrend", {"symbol": symbol, "interval": interval, "outputsize": "3"}
    )
    if error:
        return False, details, error
    ts_values, error = td_indicator(
        config, state, api_key, "time_series", {"symbol": symbol, "interval": interval, "outputsize": "3"}
    )
    if error:
        return False, details, error

    crossed, supertrend_value = supertrend_crossed_up(st_values, ts_values)
    if not crossed:
        return False, details, None

    details["supertrend"] = supertrend_value
    return True, details, None


def scan_watchlist(config, state=None):
    """المرحلتان ب و ج — الغربلة المجانية ثم التأكيد المدفوع.

    **ب (Finnhub، مجانية):** من تحرّك اليوم ≥ `min_change_percent`؟
    **ج (Twelve Data، أرصدة):** للمتحركين بس — حجم اليوم مقابل معدله (شرط 3)،
    ثم VWAP للناجين (شرط 4)، ثم ATR لمن حقق الأربعة (وقف الخسارة).

    كل مرحلة أغلى من اللي قبلها، والترتيب مقصود عشان الأغلب يسقط وهو رخيص.
    """
    api_key = config.get("twelvedata_api_key")
    if not api_key:
        return None, "twelvedata_api_key is empty in config.json"

    state = {} if state is None else state

    watchlist = load_watchlist()
    if not watchlist:
        return None, "Watchlist is empty. Run /refresh first."

    # المرحلة ب — مجانية بالكامل، ما تصرف ولا رصيد
    movers, error = find_movers(config, state, watchlist)
    if error:
        return None, error
    if not movers:
        return [], None

    symbols = [m["symbol"] for m in movers]
    change_by_symbol = {m["symbol"]: m["change_percent"] for m in movers}
    spike_ratio = setting(config, "volume_spike_ratio")
    budget = setting(config, "daily_credit_budget")

    # تيليفن داتا يقبل عدة رموز في طلب واحد، بس الأرصدة تُحسب لكل رمز.
    # نقسّمها لدفعات صغيرة عشان ما نتجاوز 8 أرصدة في الدقيقة.
    volume_passed = []
    for batch_start in range(0, len(symbols), 8):
        batch = symbols[batch_start : batch_start + 8]

        # سقف الميزانية اليومي — نوقف قبل ما نتجاوزه، مو بعد
        if credits_used_today(state) + len(batch) > budget:
            print(f"Stopped scanning: reached the daily credit budget ({budget} credits)")
            break

        data = twelvedata_request(api_key, "quote", {"symbol": ",".join(batch)})
        record_credits(state, len(batch))
        for symbol in batch:
            record_escalation(state, symbol)

        if not data:
            continue
        if data.get("code"):
            return None, f"Twelve Data: {data.get('message', 'unknown error')}"

        # تيلف داتا يرجّع شكلين مختلفين: طلب برمز واحد يرجّع الكائن مباشرة،
        # وبعدة رموز يرجّع قاموس مفاتيحه الرموز. نتعرّف على الشكل من محتواه
        # مو من عدد الرموز اللي طلبناها — أمتن لو رجع رمز ناقص.
        quotes = {data["symbol"]: data} if "symbol" in data else data

        for symbol, quote in quotes.items():
            if not isinstance(quote, dict) or quote.get("code"):
                continue
            try:
                volume = float(quote.get("volume") or 0)
                average = float(quote.get("average_volume") or 0)
                price = float(quote.get("close") or 0)
            except (TypeError, ValueError):
                continue

            if average > 0 and price > 0 and volume >= average * spike_ratio:
                volume_passed.append(
                    {
                        "symbol": symbol,
                        "price": price,
                        "volume": volume,
                        "average_volume": average,
                        "change_percent": change_by_symbol.get(symbol),
                    }
                )

        if batch_start + 8 < len(symbols):
            time.sleep(62)  # حد 8 أرصدة في الدقيقة

    # شرط 4: السعر فوق VWAP — للناجين بس
    above_vwap = []
    for candidate in volume_passed:
        if credits_used_today(state) + 1 > budget:
            print(f"Stopped at VWAP stage: reached the daily credit budget ({budget} credits)")
            break

        vwap_data = twelvedata_request(
            api_key,
            "vwap",
            {"symbol": candidate["symbol"], "interval": setting(config, "indicator_interval"), "outputsize": "1"},
        )
        record_credits(state, 1)
        time.sleep(8)

        if not vwap_data:
            continue
        if vwap_data.get("code"):
            message = f"Twelve Data: {vwap_data.get('message', 'unknown error')}"
            if is_credit_exhausted(message):
                return None, message  # نفس معاملة مرحلة quote — يوقف الحظر من هنا برضو
            continue

        values = vwap_data.get("values") or []
        if not values:
            continue

        try:
            vwap = float(values[0]["vwap"])
        except (KeyError, TypeError, ValueError):
            continue

        if candidate["price"] > vwap:
            candidate["vwap"] = vwap
            above_vwap.append(candidate)

    # الشرط الرابع الموسّع: EMA + MACD + SuperTrend — للي عدّى VWAP بس
    confirmed = []
    for candidate in above_vwap:
        passed, details, error = passes_indicators(
            config, state, api_key, candidate["symbol"], candidate["price"]
        )
        if error:
            if is_credit_exhausted(error):
                return None, error
            print(f"Stopped at indicator stage: {error}")
            break
        if passed:
            candidate.update(details)
            confirmed.append(candidate)

    # وقف الخسارة بـ ATR — للأسهم اللي حققت كل الشروط بس (نادرة جداً،
    # عشان كذا رصيد إضافي لكل واحد منهم ما يكلّف شي محسوس)
    alerts = []
    for candidate in confirmed:
        stop = compute_stop_loss(config, api_key, candidate["symbol"], candidate["price"])
        record_credits(state, 1)
        time.sleep(8)
        if stop is not None:
            candidate["stop_loss"] = stop["stop_loss"]
            candidate["atr"] = stop["atr"]
        else:
            # الشروط الأربعة تحققت لكن ATR فشل — التنبيه بدون وقف خسارة
            # أفضل من ما يوصل أبداً؛ نرسله وننبّه إنه ناقص
            candidate["stop_loss"] = None
            candidate["atr"] = None

        alerts.append(candidate)

    return alerts, None


def compute_stop_loss(config, api_key, symbol, entry_price):
    """وقف الخسارة = سعر الدخول − (ATR × مضاعف). يرجّع None لو فشل الطلب."""
    data = twelvedata_request(api_key, "atr", {"symbol": symbol, "interval": "1day", "time_period": "14", "outputsize": "1"})

    if not data or data.get("code"):
        return None

    values = data.get("values") or []
    if not values:
        return None

    try:
        atr = float(values[0]["atr"])
    except (KeyError, TypeError, ValueError):
        return None

    multiplier = setting(config, "atr_multiplier")
    return {"atr": atr, "stop_loss": entry_price - (atr * multiplier)}


def format_alert(candidate):
    ratio = candidate["volume"] / candidate["average_volume"]
    symbol = candidate["symbol"]

    lines = [
        f"🚨 ALERT — {symbol}",
        "",
        f"Entry: {round(candidate['price'], 2)}",
    ]

    if candidate.get("stop_loss") is not None:
        lines.append(f"Stop Loss: {round(candidate['stop_loss'], 2)}")
        lines.append(f"(ATR {round(candidate['atr'], 2)})")
    else:
        lines.append("⚠️ Stop Loss: could not calculate — review manually")

    lines += [
        "",
        f"Volume: {int(candidate['volume']):,} shares",
        f"Average: {int(candidate['average_volume']):,} shares",
        f"Spike: {round(ratio, 1)}x average",
    ]

    if candidate.get("change_percent") is not None:
        lines.append(f"Change today: {round(candidate['change_percent'], 1)}%")

    lines += ["", "✅ Conditions met:", f"Above VWAP: {round(candidate['vwap'], 2)}"]

    for period in (5, 10, 20):
        ema = candidate.get(f"ema{period}")
        if ema is not None:
            lines.append(f"Above EMA{period}: {round(ema, 2)}")

    if candidate.get("macd_hist") is not None:
        lines.append(f"MACD histogram positive: {round(candidate['macd_hist'], 4)}")

    if candidate.get("supertrend") is not None:
        lines.append(f"SuperTrend bullish cross: {round(candidate['supertrend'], 2)}")

    lines += [
        "",
        f"Chart: {tradingview_url(symbol)}",
    ]

    return "\n".join(lines)


def tradingview_url(symbol):
    return f"https://www.tradingview.com/symbols/{symbol}/"


def cmd_restore():
    if BUSY.get("refresh"):
        return "A watchlist build is running. Wait for it to finish before restoring."

    backup = load_watchlist_backup()
    if backup is None:
        return "No backup saved yet — a backup is created automatically when you run /refresh."

    save_watchlist(backup)
    return f"✅ Restored the previous watchlist: {len(backup)} {plural(len(backup), 'stock')}."


def cmd_list(config):
    watchlist = load_watchlist()
    if not watchlist:
        return "Watchlist is empty.\nRun /refresh to build it from the market."

    lines = [f"📋 Watchlist — {len(watchlist)} {plural(len(watchlist), 'stock')}", ""]
    for item in watchlist:
        line = f"{item['symbol']} — {item['market_cap_musd']}M cap — float {item['float_shares']:,}"
        if item.get("avg_volume"):
            line += f" — vol {item['avg_volume']:,}"
        lines.append(line)

    return "\n".join(lines)  # send_message_chunked() يقسّمها لعدة رسائل لو طولت، ما نقصّها هنا


def cmd_status(config, state):
    is_open, description = market_state()
    ny = new_york_now()
    muted = state.get("muted", False)

    watchlist = load_watchlist()

    lines = [
        "🤖 Bot Status",
        "",
        "Bot: running ✅",
        f"Alerts: {'muted 🔕' if muted else 'on 🔔'}",
        f"Serving: {len(allowed_chat_ids(config))} {plural(len(allowed_chat_ids(config)), 'chat')}",
        "",
        f"US market: {description}",
        f"New York time: {ny.strftime('%Y-%m-%d %H:%M')}",
        f"Saudi time: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Watchlist: {len(watchlist)} {plural(len(watchlist), 'stock')}" if watchlist else "Watchlist: empty — run /refresh",
    ]

    if watchlist:
        interval = setting(config, "scan_interval_minutes")
        lines.append(f"Auto-scan: every {interval} min during market hours")
        lines.append(f"Change threshold before paid checks: {setting(config, 'min_change_percent')}%")

    budget = setting(config, "daily_credit_budget")
    lines.append(f"Twelve Data credits today: {credits_used_today(state)} of {budget}")

    last_scan = state.get("last_scan")
    if last_scan:
        lines.append(f"Last scan: {last_scan}")

    blocked_until = state.get("twelvedata_blocked_until")
    if blocked_until:
        try:
            blocked_dt = datetime.fromisoformat(blocked_until)
        except ValueError:
            blocked_dt = None
        if blocked_dt and datetime.now(timezone.utc) < blocked_dt:
            remaining = blocked_dt - datetime.now(timezone.utc)
            hours, minutes = divmod(int(remaining.total_seconds() // 60), 60)
            lines.append(f"⏸️ Twelve Data credits exhausted — scanning resumes in ~{hours}h {minutes}m")

    if BUSY.get("refresh"):
        lines.append("⏳ Building the watchlist right now")
    if BUSY.get("scan"):
        lines.append("⏳ A scan is running right now")

    started = state.get("started_at")
    if started:
        lines.append(f"Up since: {started}")

    if not is_open:
        lines += ["", "The bot accepts your commands 24/7,", "but scanning only runs during market hours."]

    return "\n".join(lines)


def cmd_price(config, args):
    if not args:
        return "Add a stock symbol after the command.\nExample: /price AAPL"

    api_key = config.get("finnhub_api_key")
    if not api_key:
        return "finnhub_api_key is empty in config.json"

    symbol = args[0].upper()
    quote = finnhub_request(api_key, "quote", {"symbol": symbol}, fatal=False)

    if quote is None:
        return "Could not reach the data source. Try again shortly."
    if quote.get("_error"):
        return quote["_error"]
    if not quote.get("c"):
        return f"No data found for {symbol}.\nMake sure it is a valid US stock symbol, like AAPL."

    return format_quote(symbol, quote)


def handle_command(config, state, text):
    """يرجّع نص الرد، أو None لو الرسالة مو أمر نعرفه."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None

    # تيليجرام أحياناً يضيف اسم البوت للأمر: /status@Stock7oot_bot
    command = parts[0].split("@")[0].lower()
    args = parts[1:]

    if command in ("/start", "/help"):
        return HELP_TEXT
    if command == "/status":
        return cmd_status(config, state)
    if command == "/price":
        return cmd_price(config, args)
    if command == "/mute":
        state["muted"] = True
        save_state(state)
        return "Alerts muted 🔕\nThe bot keeps running. Resume with /unmute"
    if command == "/unmute":
        state["muted"] = False
        save_state(state)
        return "Alerts resumed 🔔"
    if command == "/list":
        return cmd_list(config)
    if command == "/refresh":
        return start_refresh(config, state)
    if command == "/restore":
        return cmd_restore()
    if command == "/scan":
        return start_scan(config, state, manual=True)
    if command == "/journal":
        return cmd_journal(config)

    return f"Unknown command {command}\nSend /help to see available commands."


# ---------------------------------------------------------------
# المهام الطويلة — تشتغل في الخلفية عشان ما تجمّد الأوامر
# ---------------------------------------------------------------

def run_in_background(name, target):
    """يشغّل مهمة في خيط منفصل. يمنع تشغيل نفس المهمة مرتين مع بعض."""
    if BUSY.get(name):
        return False
    BUSY[name] = True

    def wrapper():
        try:
            target()
        except Exception as error:  # ما نخلي خطأ في مهمة يسقط البوت كله
            print(f"Error in task {name}: {error}")
        finally:
            BUSY[name] = False

    threading.Thread(target=wrapper, daemon=True).start()
    return True


def start_refresh(config, state):
    token = config["telegram_bot_token"]
    ids = allowed_chat_ids(config)

    if BUSY.get("refresh"):
        return "A watchlist build is already running. Wait for it to finish."

    # نحفظ القائمة الحالية احتياطياً قبل ما /refresh يبدأ يستبدلها — لو ما
    # عجبت النتيجة الجديدة، /restore يرجّع هذي النسخة. مرة وحدة، قبل ما
    # نبدأ الخيط، عشان ما تصير سباق مع أول حفظ داخل build_watchlist().
    had_backup = backup_current_watchlist()

    max_size = setting(config, "max_watchlist_size")

    def job():
        def progress(checked, total, found):
            broadcast(token, ids, f"⏳ Checked {checked} — found {found} of {max_size}")

        found, error = build_watchlist(config, progress)
        if error:
            broadcast(token, ids, f"❌ {error}")
            return

        reached_cap = len(found) >= max_size
        headline = (
            f"✅ Reached the cap: {len(found)} {plural(len(found), 'stock')} — stopped scanning."
            if reached_cap
            else f"✅ Finished scanning the whole market: {len(found)} {plural(len(found), 'stock')}."
        )
        broadcast(
            token,
            ids,
            f"{headline}\nSend /list to see it.\nNot happy with it? Send /restore to bring back the previous one.",
        )

    if not run_in_background("refresh", job):
        return "A watchlist build is already running. Wait for it to finish."

    note = "\nSaved a backup of the current watchlist — /restore brings it back." if had_backup else ""
    return f"Building the watchlist.\nI stop automatically at {max_size} stocks and will report progress. If it is interrupted, whatever was found is already saved.{note}"


def start_scan(config, state, manual=False):
    token = config["telegram_bot_token"]
    ids = allowed_chat_ids(config)

    watchlist = load_watchlist()
    if not watchlist:
        return "Watchlist is empty. Run /refresh first."

    blocked_until = state.get("twelvedata_blocked_until")
    if blocked_until:
        try:
            blocked_dt = datetime.fromisoformat(blocked_until)
        except ValueError:
            blocked_dt = None
        if blocked_dt and datetime.now(timezone.utc) < blocked_dt:
            if not manual:
                return None  # فحص تلقائي: نتجاهله بصمت، ما نصرف رصيد ولا نزعج بإشعار مكرر
            remaining = blocked_dt - datetime.now(timezone.utc)
            hours, minutes = divmod(int(remaining.total_seconds() // 60), 60)
            return (
                "⏸️ Twelve Data credits are exhausted for today.\n"
                f"Auto-scanning is paused and resumes automatically in about {hours}h {minutes}m."
            )

    def job():
        started = time.time()
        print(f"Scan started: {len(watchlist)} {plural(len(watchlist), 'stock')}")

        # يسوّي أي صفقة ورقية مفتوحة قبل الفحص — مجاني (Finnhub quote بس)،
        # ما يعتمد على نتيجة scan_watchlist()
        api_key = config.get("finnhub_api_key")
        if api_key:
            settle_paper_trades(api_key)

        alerts, error = scan_watchlist(config, state)
        state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        took = round((time.time() - started) / 60, 1)
        print(f"Scan finished in {took} min — {len(alerts or [])} {plural(len(alerts or []), 'alert')}, error: {error}")

        if error:
            if is_credit_exhausted(error):
                now_utc = datetime.now(timezone.utc)
                previous_block = state.get("twelvedata_blocked_until")
                was_active_block = False
                if previous_block:
                    try:
                        was_active_block = datetime.fromisoformat(previous_block) > now_utc
                    except ValueError:
                        was_active_block = False

                reset_at = next_utc_midnight(now_utc)
                state["twelvedata_blocked_until"] = reset_at.isoformat()
                save_state(state)

                if not was_active_block:
                    broadcast(
                        token,
                        ids,
                        f"⏸️ {error}\n\n"
                        "Auto-scanning will pause until credits reset tomorrow, instead of retrying every "
                        f"{setting(config, 'scan_interval_minutes')} minutes and failing past the limit.",
                    )
                return

            save_state(state)
            broadcast(token, ids, f"❌ {error}")
            return

        save_state(state)

        if not alerts:
            if manual:
                broadcast(token, ids, f"Scan finished ({took} min) — no stock met all conditions.")
            return

        if load_state().get("muted"):
            print(f"{len(alerts)} alerts found but alerts are muted")
            return

        for candidate in alerts:
            broadcast(token, ids, format_alert(candidate))
            if candidate.get("stop_loss") is not None:
                open_paper_trade(candidate["symbol"], candidate["price"], candidate["stop_loss"])

    if not run_in_background("scan", job):
        return "A scan is already running ⏳\nIt takes a few minutes — you will get the result when it finishes."

    if not manual:
        return None

    # الوقت المتوقع: حد Twelve Data المجاني 8 أسهم بالدقيقة، ما نقدر نتجاوزه
    minutes = max(1, round(len(watchlist) / 8))
    return (
        f"Scanning {len(watchlist)} {plural(len(watchlist), 'stock')} ⏳\n"
        f"Takes about {minutes} min — the free plan allows only 8 stocks per minute.\n"
        f"You will get a reply when it finishes, whether or not anything is found."
    )


def run_bot(config):
    """الحلقة الرئيسية: يستمع لأوامر تيليجرام على مدار اليوم."""
    token = config["telegram_bot_token"]
    allowed_ids = allowed_chat_ids(config)
    # أول محادثة بالقائمة = المالك — هو اللي يوصله تنبيه "شخص جديد راسل البوت"
    owner_chat_id = allowed_ids[0]

    state = load_state()
    state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(state)

    menu_ok = publish_command_menu(token)
    print("Command menu:", "registered with Telegram ✅" if menu_ok else "not registered ⚠️")

    is_open, description = market_state()
    broadcast(token, allowed_ids, f"Bot started ✅\nMarket: {description}\n\nTap the menu button to see commands.")
    print(f"Bot running. Market: {description}. Serving {len(allowed_ids)} chat(s). Press Ctrl+C to stop.")

    offset = state.get("update_offset", 0)
    last_auto_scan = 0.0
    was_open = is_open  # لرصد لحظة إغلاق السوق — تسوية آخر صفقات اليوم والملخص اليومي

    while True:
        # الفحص التلقائي: وقت السوق بس، وكل فترة محددة.
        # السماع لأوامرك يشتغل على مدار اليوم — هو مجاني، الفحص هو اللي يصرف.
        is_open, _ = market_state()
        interval_seconds = setting(config, "scan_interval_minutes") * 60
        if is_open and time.time() - last_auto_scan >= interval_seconds and load_watchlist():
            last_auto_scan = time.time()
            start_scan(config, state, manual=False)

        # السوق أغلق للتو — تسوية آخر صفقات اليوم (يوم التداول انتهى) وبث
        # الملخص اليومي. يشتغل مرة وحدة عند الانتقال، مو كل دورة.
        if was_open and not is_open:
            api_key = config.get("finnhub_api_key")
            if api_key:
                trades = settle_paper_trades(api_key)
                broadcast(token, allowed_ids, daily_journal_summary(trades, new_york_now()))
        was_open = is_open

        result = telegram_request(
            token, "getUpdates", {"offset": offset, "timeout": 30}, fatal=False
        )

        if result is None or not result.get("ok"):
            # انقطاع نت أو خطأ مؤقت — نستنى ونعيد المحاولة
            time.sleep(5)
            continue

        for update in result.get("result", []):
            # نثبّت الموقع قبل المعالجة: لو انهار البوت على رسالة معيّنة،
            # ما يعيد نفس الرسالة كل مرة ويدخل في حلقة انهيار
            offset = update["update_id"] + 1
            state["update_offset"] = offset
            save_state(state)

            message = update.get("message") or {}
            text = message.get("text")
            chat_id = str(message.get("chat", {}).get("id", ""))

            if not text:
                continue

            # أمان: البوت ما يرد إلا على محادثة مصرّح لها. بدون هذا، أي شخص
            # يلقى اسم البوت يقدر يشغّله ويصرف أرصدتنا. لغريب راسل البوت،
            # ننبّه المالك برقم محادثته عشان يضيفه بنفسه لو يبي — وما نرد
            # على الغريب نفسه بأي شي يكشف تفاصيل البوت.
            if chat_id not in allowed_ids:
                name = message.get("chat", {}).get("first_name") or "no name"
                print(f"Ignored a message from an unauthorized chat: {chat_id} ({name})")
                send_message(
                    token,
                    owner_chat_id,
                    f"👤 Someone new messaged the bot and I did not reply.\nName: {name}\nChat ID: {chat_id}\n\nTo add them, put this ID in telegram_chat_ids in config.json and restart the bot.",
                )
                continue

            reply = handle_command(config, state, text)
            if reply:
                print(f"Command from {chat_id}: {text.strip()}")
                send_message_chunked(token, chat_id, reply)


def require_chat_id(config):
    chat_id = config.get("telegram_chat_id")
    if not chat_id:
        print("telegram_chat_id is empty in config.json")
        print("Run: python bot.py chatid   to find your chat ID.")
        sys.exit(1)
    return chat_id


def allowed_chat_ids(config):
    """يرجّع قائمة كل المحادثات المصرّح لها.

    telegram_chat_ids (قائمة) هو الجديد — يخدم أكثر من شخص بنفس البوت.
    لو مو موجود، نرجع لـ telegram_chat_id القديم (شخص واحد) عشان الإعدادات
    الحالية على الجوال تشتغل بدون تعديل.
    """
    ids = config.get("telegram_chat_ids")
    if ids:
        return [str(c) for c in ids]
    return [str(require_chat_id(config))]


def broadcast(token, chat_ids, text):
    for chat_id in chat_ids:
        send_message_chunked(token, chat_id, text)


def main():
    config = load_config()
    token = config["telegram_bot_token"]

    if len(sys.argv) > 1 and sys.argv[1] == "chatid":
        print_chat_ids(token)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "run":
        try:
            run_bot(config)
        except KeyboardInterrupt:
            print("\nBot stopped.")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "price":
        if len(sys.argv) < 3:
            print("You must pass a stock symbol after the command.")
            print("Example: python bot.py price AAPL")
            sys.exit(1)
        send_price(config, sys.argv[2].upper())
        return

    chat_id = require_chat_id(config)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"Bot started\nTime: {now}\nStage: 2 (connection test)"

    response = send_message(token, chat_id, text)

    if response.get("ok"):
        print("Message sent. Check Telegram.")
    else:
        print("Send failed:")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
