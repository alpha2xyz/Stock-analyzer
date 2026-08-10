"""
بوت تنبيهات الأسهم الأمريكية - المرحلة 1
حالياً: يرسل رسالة تجريبية على تيليجرام عند التشغيل، ولا شي غير كذا.

طريقة التشغيل:
    python bot.py            -> يرسل رسالة تجريبية
    python bot.py chatid     -> يطبع رقم المحادثة (chat_id) حقك
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


def main():
    config = load_config()
    token = config["telegram_bot_token"]

    if len(sys.argv) > 1 and sys.argv[1] == "chatid":
        print_chat_ids(token)
        return

    chat_id = config.get("telegram_chat_id")
    if not chat_id:
        print("الحقل telegram_chat_id فاضي في config.json")
        print("شغّل: python bot.py chatid   عشان تعرف الرقم حقك.")
        sys.exit(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"البوت اشتغل\nالوقت: {now}\nالمرحلة: 1 (اختبار الاتصال)"

    response = send_message(token, chat_id, text)

    if response.get("ok"):
        print("تم إرسال الرسالة بنجاح. شوف تيليجرام.")
    else:
        print("فشل الإرسال:")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
