"""
اختبارات تشتغل على الماك بدون جوال وبدون إنترنت.

ليش موجودة: حساب توقيت السوق الأمريكي فيه تحويل صيفي/شتوي. لو غلط، البوت
بيفحص في الوقت الغلط بساعة كاملة — وهذا غلط ما ينكشف إلا بعد شهور، عند أول
تحويل توقيت. الاختبارات تكشفه الحين.

التشغيل:
    python test.py
"""

import importlib.util
import os
import sys
from datetime import datetime

spec = importlib.util.spec_from_file_location(
    "bot", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

# (وقت UTC، فرق نيويورك المتوقع، السوق مفتوح؟، وصف)
CASES = [
    # التوقيت الشتوي: نيويورك = UTC-5
    (datetime(2026, 1, 15, 14, 29), -5, False, "شتاء - 9:29 نيويورك، قبل الافتتاح بدقيقة"),
    (datetime(2026, 1, 15, 14, 30), -5, True, "شتاء - 9:30 نيويورك، لحظة الافتتاح"),
    (datetime(2026, 1, 15, 15, 0), -5, True, "شتاء - 10:00 نيويورك"),
    (datetime(2026, 1, 15, 20, 59), -5, True, "شتاء - 15:59 نيويورك، قبل الإغلاق بدقيقة"),
    (datetime(2026, 1, 15, 21, 0), -5, False, "شتاء - 16:00 نيويورك، لحظة الإغلاق"),
    # التوقيت الصيفي: نيويورك = UTC-4
    (datetime(2026, 7, 15, 13, 29), -4, False, "صيف - 9:29 نيويورك، قبل الافتتاح بدقيقة"),
    (datetime(2026, 7, 15, 13, 30), -4, True, "صيف - 9:30 نيويورك، لحظة الافتتاح"),
    (datetime(2026, 7, 15, 20, 0), -4, False, "صيف - 16:00 نيويورك، لحظة الإغلاق"),
    # حدود التحويل: 2026 الصيف يبدأ 8 مارس وينتهي 1 نوفمبر
    (datetime(2026, 3, 6, 15, 0), -5, True, "جمعة 6 مارس - قبل التحويل، لسه شتوي"),
    (datetime(2026, 3, 9, 14, 0), -4, True, "اثنين 9 مارس - بعد التحويل، صار صيفي"),
    (datetime(2026, 10, 30, 14, 0), -4, True, "جمعة 30 أكتوبر - آخر يوم صيفي"),
    (datetime(2026, 11, 2, 15, 0), -5, True, "اثنين 2 نوفمبر - رجع شتوي"),
    # نهاية الأسبوع
    (datetime(2026, 8, 15, 15, 0), -4, False, "سبت - مقفل حتى لو الوقت مناسب"),
    (datetime(2026, 8, 16, 15, 0), -4, False, "أحد - مقفل حتى لو الوقت مناسب"),
]

# تواريخ التحويل الصحيحة لكل سنة (ثاني أحد مارس، أول أحد نوفمبر)
SUNDAY_CASES = [
    (2026, 3, 2, 8),
    (2026, 11, 1, 1),
    (2027, 3, 2, 14),
    (2027, 11, 1, 7),
    (2028, 3, 2, 12),
    (2028, 11, 1, 5),
]


def main():
    failures = 0

    for utc, expected_offset, expected_open, note in CASES:
        offset = bot.new_york_offset(utc)
        is_open, description = bot.market_state(utc)
        ok = offset == expected_offset and is_open == expected_open
        failures += not ok
        status = "نجح  " if ok else "فشل  "
        print(f"{status} {note:45s} offset={offset:+d} open={is_open}")

    print()
    for year, month, n, expected_day in SUNDAY_CASES:
        day = bot._nth_sunday(year, month, n)
        ok = day == expected_day
        failures += not ok
        status = "نجح  " if ok else "فشل  "
        print(f"{status} الأحد رقم {n} في {month}/{year} = {day} (المتوقع {expected_day})")

    print()
    if failures:
        print(f"❌ فشل {failures} اختبار")
        sys.exit(1)
    print(f"✅ كل الاختبارات نجحت ({len(CASES) + len(SUNDAY_CASES)})")


if __name__ == "__main__":
    main()
