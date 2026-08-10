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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
FINNHUB_API = "https://finnhub.io/api/v1/{endpoint}"

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
    change = quote.get("d") or 0
    change_percent = quote.get("dp") or 0
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

HELP_TEXT = "\n".join(
    [
        "الأوامر المتاحة:",
        "",
        "/price AAPL — سعر سهم الحين",
        "/status — حالة البوت والسوق",
        "/mute — يوقف التنبيهات مؤقتاً",
        "/unmute — يرجّع التنبيهات",
        "/help — هذي القائمة",
        "",
        "أوامر القائمة والفحص (/list و /refresh و /scan) تجي مع المرحلة 3.",
    ]
)


def cmd_status(config, state):
    is_open, description = market_state()
    ny = new_york_now()
    muted = state.get("muted", False)

    lines = [
        "🤖 حالة البوت",
        "",
        "البوت: شغّال ✅",
        f"التنبيهات: {'موقوفة 🔕' if muted else 'شغّالة 🔔'}",
        "",
        f"السوق الأمريكي: {description}",
        f"توقيت نيويورك الحين: {LTR}{ny.strftime('%Y-%m-%d %H:%M')}",
        f"توقيت السعودية الحين: {LTR}{datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]

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
    if command in ("/list", "/refresh", "/scan"):
        return "هذا الأمر يجي مع المرحلة 3 (بناء القائمة والفلترة)."

    return f"ما أعرف الأمر {command}\nاكتب /help تشوف الأوامر المتاحة."


def run_bot(config):
    """الحلقة الرئيسية: يستمع لأوامر تيليجرام على مدار اليوم."""
    token = config["telegram_bot_token"]
    allowed_chat_id = str(require_chat_id(config))

    state = load_state()
    state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(state)

    is_open, description = market_state()
    send_message(token, allowed_chat_id, f"البوت اشتغل ✅\nالسوق: {description}\n\nاكتب /help تشوف الأوامر.")
    print(f"البوت شغّال. السوق: {description}. اضغط Ctrl+C للإيقاف.")

    offset = state.get("update_offset", 0)

    while True:
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
