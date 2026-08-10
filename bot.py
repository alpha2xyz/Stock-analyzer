"""
بوت تنبيهات الأسهم الأمريكية - المرحلة 2
حالياً: يرسل رسالة تجريبية، ويجيب سعر سهم واحد من Finnhub ويرسله على تيليجرام.

طريقة التشغيل:
    python bot.py            -> يرسل رسالة تجريبية
    python bot.py chatid     -> يطبع رقم المحادثة (chat_id) حقك
    python bot.py price AAPL -> يجيب سعر السهم ويرسله على تيليجرام
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
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


def telegram_request(token, method, params=None):
    url = TELEGRAM_API.format(token=token, method=method)
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # تيليجرام يرجع سبب الخطأ داخل جسم الرد حتى مع أكواد الخطأ
        return json.loads(error.read().decode("utf-8"))
    except urllib.error.URLError as error:
        print(f"ما قدرت أوصل لتيليجرام. تأكد من الإنترنت. ({error.reason})")
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


def finnhub_request(api_key, endpoint, params):
    """يرسل طلب لـ Finnhub ويرجّع الرد كـ dict. يوقف البرنامج لو صار خطأ."""
    query = dict(params)
    query["token"] = api_key
    url = FINNHUB_API.format(endpoint=endpoint) + "?" + urllib.parse.urlencode(query)

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            print("مفتاح Finnhub غلط أو ما عاد شغّال.")
            print("تأكد من الحقل finnhub_api_key في config.json")
        elif error.code == 429:
            print("تجاوزت عدد الطلبات المسموح فيها في الباقة المجانية.")
            print("انتظر دقيقة وأعد المحاولة.")
        else:
            print(f"Finnhub رجّع خطأ. الكود: {error.code}")
        sys.exit(1)
    except urllib.error.URLError as error:
        print(f"ما قدرت أوصل لـ Finnhub. تأكد من الإنترنت. ({error.reason})")
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
