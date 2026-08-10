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

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
WATCHLIST_PATH = os.path.join(HERE, "watchlist.json")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
FINNHUB_API = "https://finnhub.io/api/v1/{endpoint}"
TWELVEDATA_API = "https://api.twelvedata.com/{endpoint}"

# علامة تخلي الأرقام والحروف الإنجليزية تظهر بالاتجاه الصحيح داخل نص عربي.
# بدونها الأرقام السالبة مثل -2.43 تنقلب وتظهر 2.43- في تيليجرام.
LTR = "\u200e"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print("ما لقيت ملف config.json")
        print("سو نسخة من config.example.json وسمها config.json وعبّي البيانات فيه.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    if not config.get("telegram_bot_token"):
        print("الحقل telegram_bot_token فاضي في config.json")
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
        return False, "مقفل — نهاية الأسبوع"

    minutes = ny.hour * 60 + ny.minute
    if minutes < 9 * 60 + 30:
        return False, "مقفل — يفتح 9:30 صباحاً بتوقيت نيويورك"
    if minutes >= 16 * 60:
        return False, "مقفل — انتهى دوام اليوم"

    return True, "مفتوح"


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
        print(f"ما قدرت أوصل لتيليجرام. تأكد من الإنترنت. ({reason})")
        sys.exit(1)


def send_message(token, chat_id, text):
    return telegram_request(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text},
    )


def print_chat_ids(token):
    """يجيب آخر الرسائل الواصلة للبوت ويطبع منها رقم المحادثة."""
    result = telegram_request(token, "getUpdates")
    updates = result.get("result", [])

    if not updates:
        print("ما وصلت أي رسالة للبوت.")
        print("افتح تيليجرام، ادخل على البوت حقك، وأرسل له كلمة أي كلمة، ثم أعد هذا الأمر.")
        return

    seen = set()
    for update in updates:
        chat = update.get("message", {}).get("chat")
        if chat and chat["id"] not in seen:
            seen.add(chat["id"])
            name = chat.get("first_name") or chat.get("title") or "بدون اسم"
            print(f"chat_id = {chat['id']}   ({name})")


def finnhub_request(api_key, endpoint, params, fatal=True):
    """يرسل طلب لـ Finnhub ويرجّع الرد كـ dict.

    fatal=True  -> يطبع الخطأ ويوقف البرنامج (للأوامر من الطرفية)
    fatal=False -> يرجّع {"_error": "..."} أو None (داخل حلقة البوت المستمرة)
    """
    query = dict(params)
    query["token"] = api_key
    url = FINNHUB_API.format(endpoint=endpoint) + "?" + urllib.parse.urlencode(query)

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            messages = ["مفتاح Finnhub غلط أو ما عاد شغّال.", "تأكد من الحقل finnhub_api_key في config.json"]
        elif error.code == 429:
            messages = ["تجاوزت عدد الطلبات المسموح فيها في الباقة المجانية.", "انتظر دقيقة وأعد المحاولة."]
        else:
            messages = [f"Finnhub رجّع خطأ. الكود: {error.code}"]

        if not fatal:
            return {"_error": "\n".join(messages)}
        for line in messages:
            print(line)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        if not fatal:
            return None
        print(f"ما قدرت أوصل لـ Finnhub. تأكد من الإنترنت. ({getattr(error, 'reason', error)})")
        sys.exit(1)


def get_quote(api_key, symbol):
    """يجيب سعر سهم واحد. Finnhub يرجّع أصفار للرمز الغلط بدل ما يرجّع خطأ."""
    quote = finnhub_request(api_key, "quote", {"symbol": symbol})

    if not quote.get("c"):
        print(f"ما لقيت بيانات للرمز {symbol}.")
        print("تأكد إنه رمز سهم أمريكي صحيح، مثل AAPL أو TSLA.")
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
            f"{arrow} {LTR}{symbol}",
            "",
            f"السعر الحالي: {LTR}{quote['c']}",
            f"التغيّر: {LTR}{change} ({LTR}{change_percent}%)",
            f"أعلى سعر اليوم: {LTR}{quote.get('h')}",
            f"أقل سعر اليوم: {LTR}{quote.get('l')}",
            f"إغلاق أمس: {LTR}{quote.get('pc')}",
            "",
            f"الوقت: {LTR}{now}",
        ]
    )


def send_price(config, symbol):
    api_key = config.get("finnhub_api_key")
    if not api_key:
        print("الحقل finnhub_api_key فاضي في config.json")
        print("سجّل حساب مجاني في finnhub.io وحط المفتاح فيه.")
        sys.exit(1)

    chat_id = require_chat_id(config)
    quote = get_quote(api_key, symbol)
    response = send_message(config["telegram_bot_token"], chat_id, format_quote(symbol, quote))

    if response.get("ok"):
        print(f"تم إرسال سعر {symbol} بنجاح. شوف تيليجرام.")
    else:
        print("فشل الإرسال:")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        sys.exit(1)


# ---------------------------------------------------------------
# الأوامر
# ---------------------------------------------------------------

# مصدر واحد للأوامر: منه تتبني قائمة تيليجرام (الزر الأزرق) ورسالة /help
# مع بعض. أي أمر جديد ينضاف هنا مرة وحدة، وما يصير اختلاف بين الاثنين.
COMMANDS = [
    ("status", "حالة البوت والسوق"),
    ("price", "سعر سهم — مثال: /price AAPL"),
    ("list", "قائمة الأسهم المراقَبة"),
    ("scan", "فحص فوري الحين"),
    ("refresh", "يعيد بناء قائمة المراقبة"),
    ("mute", "يوقف التنبيهات مؤقتاً"),
    ("unmute", "يرجّع التنبيهات"),
    ("help", "يعرض الأوامر"),
]

HELP_TEXT = "\n".join(
    ["الأوامر المتاحة:", ""] + [f"/{name} — {description}" for name, description in COMMANDS]
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
    "min_market_cap_musd": 40,      # مليون دولار
    "max_market_cap_musd": 60,      # مليون دولار
    "min_float_shares": 500_000,
    "volume_spike_ratio": 2.0,      # تجريبي — يتعدّل بعد ما نشوف نتائج حقيقية
    "scan_interval_minutes": 15,
    "atr_multiplier": 1.75,         # مضاعف وقف الخسارة، يبدأ بين 1.5 و 2 حسب المواصفات
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


def us_common_stocks(api_key):
    """قائمة كل الأسهم العادية الأمريكية. الصناديق والسندات مستبعدة."""
    symbols = finnhub_request(api_key, "stock/symbol", {"exchange": "US"}, fatal=False)
    if not isinstance(symbols, list):
        return []
    return sorted(
        {
            s["symbol"]
            for s in symbols
            if s.get("type") == "Common Stock" and "." not in s.get("symbol", "")
        }
    )


def passes_size_filter(config, profile):
    """شرط 1 و 2: القيمة السوقية والفلوت.

    انتبه: فينهب يرجّع الاثنين بالمليون. القيمة السوقية 45 تعني 45 مليون
    دولار، والفلوت 0.6 يعني 600 ألف سهم.
    """
    market_cap = profile.get("marketCapitalization")
    float_shares_m = profile.get("floatingShare")

    if not market_cap or not float_shares_m:
        return False

    return (
        setting(config, "min_market_cap_musd") <= market_cap <= setting(config, "max_market_cap_musd")
        and float_shares_m * 1_000_000 > setting(config, "min_float_shares")
    )


def build_watchlist(config, progress=None):
    """يمشي على السوق الأمريكي كله ويطلّع اللي يحقق شرط 1 و 2.

    شغل طويل (ساعة ونص تقريباً) — يشتغل في الخلفية عشان ما يعطّل الأوامر.
    """
    api_key = config["finnhub_api_key"]
    symbols = us_common_stocks(api_key)

    if not symbols:
        return None, "ما قدرت أجيب قائمة الأسهم من Finnhub."

    found = []
    checked = 0

    for symbol in symbols:
        profile = finnhub_request(api_key, "stock/profile2", {"symbol": symbol}, fatal=False)
        checked += 1

        # حد فينهب 60 طلب في الدقيقة — ثانية بين كل طلب تخلينا تحته بأمان
        time.sleep(1)

        if not isinstance(profile, dict) or profile.get("_error"):
            continue

        if passes_size_filter(config, profile):
            found.append(
                {
                    "symbol": symbol,
                    "name": profile.get("name", ""),
                    "market_cap_musd": round(profile["marketCapitalization"], 1),
                    "float_shares": int(profile["floatingShare"] * 1_000_000),
                }
            )

        if progress and checked % 250 == 0:
            progress(checked, len(symbols), len(found))

    save_watchlist(found)
    return found, None


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


def scan_watchlist(config):
    """شرط 3 و 4 على الأسهم اللي نجت من شرط 1 و 2.

    الترتيب مقصود: نجيب quote للكل مجمّعة أول (رصيد لكل سهم)، وما نطلب
    vwap إلا لللي نجح في شرط الحجم. الأغلب يسقط قبلها، فنوفّر أرصدة.
    """
    api_key = config.get("twelvedata_api_key")
    if not api_key:
        return None, "الحقل twelvedata_api_key فاضي في config.json"

    watchlist = load_watchlist()
    if not watchlist:
        return None, "قائمة المراقبة فاضية. شغّل /refresh أول."

    symbols = [item["symbol"] for item in watchlist]
    spike_ratio = setting(config, "volume_spike_ratio")

    # تيليفن داتا يقبل عدة رموز في طلب واحد، بس الأرصدة تُحسب لكل رمز.
    # نقسّمها لدفعات صغيرة عشان ما نتجاوز 8 أرصدة في الدقيقة.
    volume_passed = []
    for batch_start in range(0, len(symbols), 8):
        batch = symbols[batch_start : batch_start + 8]
        data = twelvedata_request(api_key, "quote", {"symbol": ",".join(batch)})

        if not data:
            continue
        if data.get("code"):
            return None, f"Twelve Data: {data.get('message', 'خطأ غير معروف')}"

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
                    {"symbol": symbol, "price": price, "volume": volume, "average_volume": average}
                )

        if batch_start + 8 < len(symbols):
            time.sleep(62)  # حد 8 أرصدة في الدقيقة

    # شرط 4: السعر فوق VWAP — للناجين بس
    above_vwap = []
    for candidate in volume_passed:
        vwap_data = twelvedata_request(
            api_key, "vwap", {"symbol": candidate["symbol"], "interval": "5min", "outputsize": "1"}
        )
        time.sleep(8)

        if not vwap_data or vwap_data.get("code"):
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

    # وقف الخسارة بـ ATR — للأسهم اللي حققت الشروط الأربعة بس (نادرة جداً،
    # عشان كذا رصيد إضافي لكل واحد منهم ما يكلّف شي محسوس)
    alerts = []
    for candidate in above_vwap:
        stop = compute_stop_loss(config, api_key, candidate["symbol"], candidate["price"])
        time.sleep(8)
        if stop is not None:
            candidate["stop_loss"] = stop["stop_loss"]
            candidate["atr"] = stop["atr"]
            alerts.append(candidate)
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
        f"🚨 تنبيه — {LTR}{symbol}",
        "",
        f"سعر الدخول: {LTR}{round(candidate['price'], 2)}",
    ]

    if candidate.get("stop_loss") is not None:
        lines.append(f"وقف الخسارة: {LTR}{round(candidate['stop_loss'], 2)}")
        lines.append(f"(ATR {LTR}{round(candidate['atr'], 2)})")
    else:
        lines.append("⚠️ وقف الخسارة: ما قدرت أحسبه — راجع السهم يدوياً")

    lines += [
        "",
        f"VWAP: {LTR}{round(candidate['vwap'], 2)}",
        f"الحجم: {LTR}{int(candidate['volume']):,} سهم",
        f"المعدل: {LTR}{int(candidate['average_volume']):,} سهم",
        f"القفزة: {LTR}{round(ratio, 1)}× المعدل",
        "",
        f"الشارت: {LTR}{tradingview_url(symbol)}",
    ]

    return "\n".join(lines)


def tradingview_url(symbol):
    return f"https://www.tradingview.com/symbols/{symbol}/"


def cmd_list(config):
    watchlist = load_watchlist()
    if not watchlist:
        return "قائمة المراقبة فاضية.\nشغّل /refresh عشان أبنيها من السوق."

    lines = [f"📋 قائمة المراقبة — {LTR}{len(watchlist)} سهم", ""]
    for item in watchlist[:40]:
        lines.append(
            f"{LTR}{item['symbol']} — {LTR}{item['market_cap_musd']}M — فلوت {LTR}{item['float_shares']:,}"
        )
    if len(watchlist) > 40:
        lines.append(f"... و {LTR}{len(watchlist) - 40} غيرهم")

    return "\n".join(lines)


def cmd_status(config, state):
    is_open, description = market_state()
    ny = new_york_now()
    muted = state.get("muted", False)

    watchlist = load_watchlist()

    lines = [
        "🤖 حالة البوت",
        "",
        "البوت: شغّال ✅",
        f"التنبيهات: {'موقوفة 🔕' if muted else 'شغّالة 🔔'}",
        "",
        f"السوق الأمريكي: {description}",
        f"توقيت نيويورك الحين: {LTR}{ny.strftime('%Y-%m-%d %H:%M')}",
        f"توقيت السعودية الحين: {LTR}{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"قائمة المراقبة: {LTR}{len(watchlist)} سهم" if watchlist else "قائمة المراقبة: فاضية — شغّل /refresh",
    ]

    if watchlist:
        interval = setting(config, "scan_interval_minutes")
        lines.append(f"الفحص التلقائي: كل {LTR}{interval} دقيقة وقت السوق")

    last_scan = state.get("last_scan")
    if last_scan:
        lines.append(f"آخر فحص: {LTR}{last_scan}")

    if BUSY.get("refresh"):
        lines.append("⏳ بناء القائمة شغّال الحين")
    if BUSY.get("scan"):
        lines.append("⏳ فحص شغّال الحين")

    started = state.get("started_at")
    if started:
        lines.append(f"يشتغل من: {LTR}{started}")

    if not is_open:
        lines += ["", "البوت يستقبل أوامرك على مدار اليوم،", "لكن فحص الأسهم يصير وقت السوق بس."]

    return "\n".join(lines)


def cmd_price(config, args):
    if not args:
        return "اكتب رمز السهم بعد الأمر.\nمثال: /price AAPL"

    api_key = config.get("finnhub_api_key")
    if not api_key:
        return "الحقل finnhub_api_key فاضي في config.json"

    symbol = args[0].upper()
    quote = finnhub_request(api_key, "quote", {"symbol": symbol}, fatal=False)

    if quote is None:
        return "ما قدرت أوصل لمصدر البيانات. جرّب بعد شوي."
    if quote.get("_error"):
        return quote["_error"]
    if not quote.get("c"):
        return f"ما لقيت بيانات للرمز {symbol}.\nتأكد إنه رمز سهم أمريكي صحيح، مثل AAPL."

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
        return "التنبيهات موقوفة 🔕\nالبوت يضل شغّال. ارجّعها بـ /unmute"
    if command == "/unmute":
        state["muted"] = False
        save_state(state)
        return "التنبيهات رجعت 🔔"
    if command == "/list":
        return cmd_list(config)
    if command == "/refresh":
        return start_refresh(config, state)
    if command == "/scan":
        return start_scan(config, state, manual=True)

    return f"ما أعرف الأمر {command}\nاكتب /help تشوف الأوامر المتاحة."


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
            print(f"خطأ في مهمة {name}: {error}")
        finally:
            BUSY[name] = False

    threading.Thread(target=wrapper, daemon=True).start()
    return True


def start_refresh(config, state):
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]

    def job():
        def progress(checked, total, found):
            send_message(token, chat_id, f"⏳ فحصت {LTR}{checked} من {LTR}{total} — لقيت {LTR}{found}")

        found, error = build_watchlist(config, progress)
        if error:
            send_message(token, chat_id, f"❌ {error}")
            return
        send_message(
            token,
            chat_id,
            f"✅ قائمة المراقبة جاهزة: {LTR}{len(found)} سهم.\nاكتب /list تشوفها.",
        )

    if not run_in_background("refresh", job):
        return "فيه بناء قائمة شغّال الحين. انتظر لين يخلص."

    return "بديت أبني القائمة من السوق الأمريكي كله.\nياخذ ساعة ونص تقريباً، وبخبرك بالتقدم."


def start_scan(config, state, manual=False):
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]

    watchlist = load_watchlist()
    if not watchlist:
        return "قائمة المراقبة فاضية. شغّل /refresh أول."

    def job():
        alerts, error = scan_watchlist(config)
        state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_state(state)

        if error:
            send_message(token, chat_id, f"❌ {error}")
            return

        if not alerts:
            if manual:
                send_message(token, chat_id, "الفحص خلص — ما في سهم حقق الشروط الأربعة.")
            return

        if load_state().get("muted"):
            print(f"في {len(alerts)} تنبيه بس التنبيهات موقوفة")
            return

        for candidate in alerts:
            send_message(token, chat_id, format_alert(candidate))

    if not run_in_background("scan", job):
        return "فيه فحص شغّال الحين. انتظر لين يخلص."

    return f"بديت أفحص {LTR}{len(watchlist)} سهم..." if manual else None


def run_bot(config):
    """الحلقة الرئيسية: يستمع لأوامر تيليجرام على مدار اليوم."""
    token = config["telegram_bot_token"]
    allowed_chat_id = str(require_chat_id(config))

    state = load_state()
    state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(state)

    menu_ok = publish_command_menu(token)
    print("قائمة الأوامر:", "انسجلت عند تيليجرام ✅" if menu_ok else "ما انسجلت ⚠️")

    is_open, description = market_state()
    send_message(token, allowed_chat_id, f"البوت اشتغل ✅\nالسوق: {description}\n\nاضغط زر القائمة تشوف الأوامر.")
    print(f"البوت شغّال. السوق: {description}. اضغط Ctrl+C للإيقاف.")

    offset = state.get("update_offset", 0)
    last_auto_scan = 0.0

    while True:
        # الفحص التلقائي: وقت السوق بس، وكل فترة محددة.
        # السماع لأوامرك يشتغل على مدار اليوم — هو مجاني، الفحص هو اللي يصرف.
        is_open, _ = market_state()
        interval_seconds = setting(config, "scan_interval_minutes") * 60
        if is_open and time.time() - last_auto_scan >= interval_seconds and load_watchlist():
            last_auto_scan = time.time()
            start_scan(config, state, manual=False)

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

            # أمان: البوت ما يرد إلا على المحادثة المصرّح لها. بدون هذا،
            # أي شخص يلقى اسم البوت يقدر يشغّله ويصرف أرصدتنا.
            if chat_id != allowed_chat_id:
                print(f"تجاهلت رسالة من محادثة غير مصرّح لها: {chat_id}")
                continue

            reply = handle_command(config, state, text)
            if reply:
                print(f"أمر: {text.strip()}")
                send_message(token, allowed_chat_id, reply)


def require_chat_id(config):
    chat_id = config.get("telegram_chat_id")
    if not chat_id:
        print("الحقل telegram_chat_id فاضي في config.json")
        print("شغّل: python bot.py chatid   عشان تعرف الرقم حقك.")
        sys.exit(1)
    return chat_id


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
            print("\nتم إيقاف البوت.")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "price":
        if len(sys.argv) < 3:
            print("لازم تكتب رمز السهم بعد الأمر.")
            print("مثال: python bot.py price AAPL")
            sys.exit(1)
        send_price(config, sys.argv[2].upper())
        return

    chat_id = require_chat_id(config)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"البوت اشتغل\nالوقت: {LTR}{now}\nالمرحلة: 2 (اختبار الاتصال)"

    response = send_message(token, chat_id, text)

    if response.get("ok"):
        print("تم إرسال الرسالة بنجاح. شوف تيليجرام.")
    else:
        print("فشل الإرسال:")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
