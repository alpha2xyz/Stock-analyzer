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

    finnhub_wait_for_slot()  # كل نداء Finnhub يمر من هنا — لا تتجاوزه في أي مسار

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
    ("refresh", "يعيد بناء قائمة المراقبة من السوق كله"),
    ("restore", "يرجّع القائمة اللي كانت قبل آخر /refresh"),
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
        return None, "ما قدرت أجيب قائمة الأسهم من Finnhub."

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
        return [], "الحقل finnhub_api_key فاضي في config.json"

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
        return None, f"وصلت سقف الميزانية اليومي ({setting(config, 'daily_credit_budget')} رصيد)"

    data = twelvedata_request(api_key, endpoint, params)
    record_credits(state, 1)
    time.sleep(8)  # حد 8 أرصدة في الدقيقة

    if not data:
        return None, None
    if data.get("code"):
        message = f"Twelve Data: {data.get('message', 'خطأ غير معروف')}"
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
        return None, "الحقل twelvedata_api_key فاضي في config.json"

    state = {} if state is None else state

    watchlist = load_watchlist()
    if not watchlist:
        return None, "قائمة المراقبة فاضية. شغّل /refresh أول."

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
            print(f"وقفت الفحص: وصلت سقف الميزانية اليومي ({budget} رصيد)")
            break

        data = twelvedata_request(api_key, "quote", {"symbol": ",".join(batch)})
        record_credits(state, len(batch))
        for symbol in batch:
            record_escalation(state, symbol)

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
            print(f"وقفت مرحلة VWAP: وصلت سقف الميزانية اليومي ({budget} رصيد)")
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
            message = f"Twelve Data: {vwap_data.get('message', 'خطأ غير معروف')}"
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
            print(f"وقفت مرحلة المؤشرات: {error}")
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
        f"الحجم: {LTR}{int(candidate['volume']):,} سهم",
        f"المعدل: {LTR}{int(candidate['average_volume']):,} سهم",
        f"القفزة: {LTR}{round(ratio, 1)}× المعدل",
    ]

    if candidate.get("change_percent") is not None:
        lines.append(f"التغير اليوم: {LTR}{round(candidate['change_percent'], 1)}%")

    lines += ["", "✅ الشروط المتحققة:", f"فوق VWAP: {LTR}{round(candidate['vwap'], 2)}"]

    for period in (5, 10, 20):
        ema = candidate.get(f"ema{period}")
        if ema is not None:
            lines.append(f"فوق EMA{LTR}{period}: {LTR}{round(ema, 2)}")

    if candidate.get("macd_hist") is not None:
        lines.append(f"هيستوجرام MACD موجب: {LTR}{round(candidate['macd_hist'], 4)}")

    if candidate.get("supertrend") is not None:
        lines.append(f"تقاطع SuperTrend صاعد: {LTR}{round(candidate['supertrend'], 2)}")

    lines += [
        "",
        f"الشارت: {LTR}{tradingview_url(symbol)}",
    ]

    return "\n".join(lines)


def tradingview_url(symbol):
    return f"https://www.tradingview.com/symbols/{symbol}/"


def cmd_restore():
    if BUSY.get("refresh"):
        return "فيه بناء قائمة شغّال الحين. انتظره يخلص قبل ما ترجع للنسخة القديمة."

    backup = load_watchlist_backup()
    if backup is None:
        return "ما في نسخة احتياطية محفوظة — النسخة الاحتياطية تُحفظ تلقائياً أول ما تشغّل /refresh."

    save_watchlist(backup)
    return f"✅ رجعت القائمة القديمة: {LTR}{len(backup)} سهم."


def cmd_list(config):
    watchlist = load_watchlist()
    if not watchlist:
        return "قائمة المراقبة فاضية.\nشغّل /refresh عشان أبنيها من السوق."

    lines = [f"📋 قائمة المراقبة — {LTR}{len(watchlist)} سهم", ""]
    for item in watchlist:
        line = f"{LTR}{item['symbol']} — {LTR}{item['market_cap_musd']}M — فلوت {LTR}{item['float_shares']:,}"
        if item.get("avg_volume"):
            line += f" — حجم {LTR}{item['avg_volume']:,}"
        lines.append(line)

    return "\n".join(lines)  # send_message_chunked() يقسّمها لعدة رسائل لو طولت، ما نقصّها هنا


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
        f"يخدم: {LTR}{len(allowed_chat_ids(config))} محادثة",
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
        lines.append(f"حد التغير للفحص المدفوع: {LTR}{setting(config, 'min_change_percent')}%")

    budget = setting(config, "daily_credit_budget")
    lines.append(f"أرصدة Twelve Data اليوم: {LTR}{credits_used_today(state)} من {LTR}{budget}")

    last_scan = state.get("last_scan")
    if last_scan:
        lines.append(f"آخر فحص: {LTR}{last_scan}")

    blocked_until = state.get("twelvedata_blocked_until")
    if blocked_until:
        try:
            blocked_dt = datetime.fromisoformat(blocked_until)
        except ValueError:
            blocked_dt = None
        if blocked_dt and datetime.now(timezone.utc) < blocked_dt:
            remaining = blocked_dt - datetime.now(timezone.utc)
            hours, minutes = divmod(int(remaining.total_seconds() // 60), 60)
            lines.append(f"⏸️ أرصدة Twelve Data خلصت — يرجع الفحص بعد حوالي {LTR}{hours} س {LTR}{minutes} د")

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
    if command == "/restore":
        return cmd_restore()
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
    ids = allowed_chat_ids(config)

    if BUSY.get("refresh"):
        return "فيه بناء قائمة شغّال الحين. انتظر لين يخلص."

    # نحفظ القائمة الحالية احتياطياً قبل ما /refresh يبدأ يستبدلها — لو ما
    # عجبت النتيجة الجديدة، /restore يرجّع هذي النسخة. مرة وحدة، قبل ما
    # نبدأ الخيط، عشان ما تصير سباق مع أول حفظ داخل build_watchlist().
    had_backup = backup_current_watchlist()

    max_size = setting(config, "max_watchlist_size")

    def job():
        def progress(checked, total, found):
            broadcast(token, ids, f"⏳ فحصت {LTR}{checked} — لقيت {LTR}{found} من {LTR}{max_size}")

        found, error = build_watchlist(config, progress)
        if error:
            broadcast(token, ids, f"❌ {error}")
            return

        reached_cap = len(found) >= max_size
        headline = (
            f"✅ وصلت الحد: {LTR}{len(found)} سهم — وقفت الفحص."
            if reached_cap
            else f"✅ خلص فحص السوق كله: {LTR}{len(found)} سهم."
        )
        broadcast(
            token,
            ids,
            f"{headline}\nاكتب /list تشوفها.\nما عجبتك؟ اكتب /restore ترجع للقائمة القديمة.",
        )

    if not run_in_background("refresh", job):
        return "فيه بناء قائمة شغّال الحين. انتظر لين يخلص."

    note = "\nحفظت نسخة من القائمة الحالية، /restore يرجعها لو احتجتها." if had_backup else ""
    return f"بديت أبني القائمة.\nبوقف تلقائياً فور ما أوصل {LTR}{max_size} سهم، وبخبرك بالتقدم. لو انقطع لأي سبب، اللي اتلقى محفوظ ومو راح.{note}"


def start_scan(config, state, manual=False):
    token = config["telegram_bot_token"]
    ids = allowed_chat_ids(config)

    watchlist = load_watchlist()
    if not watchlist:
        return "قائمة المراقبة فاضية. شغّل /refresh أول."

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
                "⏸️ أرصدة Twelve Data خلصت لهذا اليوم.\n"
                f"الفحص التلقائي متوقف مؤقتاً، يرجع يشتغل تلقائياً بعد حوالي {LTR}{hours} س {LTR}{minutes} د."
            )

    def job():
        started = time.time()
        print(f"بدأ الفحص: {len(watchlist)} سهم")

        alerts, error = scan_watchlist(config, state)
        state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        took = round((time.time() - started) / 60, 1)
        print(f"خلص الفحص في {took} دقيقة — {len(alerts or [])} تنبيه، خطأ: {error}")

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
                        "الفحص التلقائي بيتوقف لين تتجدد الأرصدة غداً، بدل ما يحاول كل "
                        f"{LTR}{setting(config, 'scan_interval_minutes')} دقيقة ويفشل من فوق الحد.",
                    )
                return

            save_state(state)
            broadcast(token, ids, f"❌ {error}")
            return

        save_state(state)

        if not alerts:
            if manual:
                broadcast(token, ids, f"الفحص خلص ({LTR}{took} دقيقة) — ما في سهم حقق الشروط الأربعة.")
            return

        if load_state().get("muted"):
            print(f"في {len(alerts)} تنبيه بس التنبيهات موقوفة")
            return

        for candidate in alerts:
            broadcast(token, ids, format_alert(candidate))

    if not run_in_background("scan", job):
        return "فيه فحص شغّال الحين ⏳\nالفحص ياخذ عدة دقائق — انتظره يخلص ويجيك رده."

    if not manual:
        return None

    # الوقت المتوقع: حد Twelve Data المجاني 8 أسهم بالدقيقة، ما نقدر نتجاوزه
    minutes = max(1, round(len(watchlist) / 8))
    return (
        f"بديت أفحص {LTR}{len(watchlist)} سهم ⏳\n"
        f"ياخذ حوالي {LTR}{minutes} دقائق — الباقة المجانية تسمح بـ8 أسهم بالدقيقة بس.\n"
        f"بجيك رد أول ما يخلص، سواء لقيت شي أو لا."
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
    print("قائمة الأوامر:", "انسجلت عند تيليجرام ✅" if menu_ok else "ما انسجلت ⚠️")

    is_open, description = market_state()
    broadcast(token, allowed_ids, f"البوت اشتغل ✅\nالسوق: {description}\n\nاضغط زر القائمة تشوف الأوامر.")
    print(f"البوت شغّال. السوق: {description}. يخدم {len(allowed_ids)} محادثة. اضغط Ctrl+C للإيقاف.")

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

            # أمان: البوت ما يرد إلا على محادثة مصرّح لها. بدون هذا، أي شخص
            # يلقى اسم البوت يقدر يشغّله ويصرف أرصدتنا. لغريب راسل البوت،
            # ننبّه المالك برقم محادثته عشان يضيفه بنفسه لو يبي — وما نرد
            # على الغريب نفسه بأي شي يكشف تفاصيل البوت.
            if chat_id not in allowed_ids:
                name = message.get("chat", {}).get("first_name") or "بدون اسم"
                print(f"تجاهلت رسالة من محادثة غير مصرّح لها: {chat_id} ({name})")
                send_message(
                    token,
                    owner_chat_id,
                    f"👤 شخص جديد راسل البوت ولم أرد عليه.\nالاسم: {LTR}{name}\nرقم المحادثة: {LTR}{chat_id}\n\nلو تبي تضيفه، حط رقمه في telegram_chat_ids داخل config.json وأعد تشغيل البوت.",
                )
                continue

            reply = handle_command(config, state, text)
            if reply:
                print(f"أمر من {chat_id}: {text.strip()}")
                send_message_chunked(token, chat_id, reply)


def require_chat_id(config):
    chat_id = config.get("telegram_chat_id")
    if not chat_id:
        print("الحقل telegram_chat_id فاضي في config.json")
        print("شغّل: python bot.py chatid   عشان تعرف الرقم حقك.")
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
