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


# فلتر الحجم (شرط 1 و 2).
# الخطر هنا الوحدات: فينهب يرجّع القيمة السوقية والفلوت بالمليون. لو انحسبت
# بالدولار الكامل، الفلتر ما بيطلّع ولا سهم أبداً وما راح نعرف ليش.
# (بروفايل، النتيجة المتوقعة، الوصف)
FILTER_CASES = [
    ({"marketCapitalization": 45.0, "floatingShare": 0.6}, True, "45 مليون + فلوت 600 ألف"),
    ({"marketCapitalization": 40.0, "floatingShare": 0.6}, True, "40 مليون بالضبط — الحد الأدنى"),
    ({"marketCapitalization": 60.0, "floatingShare": 0.6}, True, "60 مليون بالضبط — الحد الأعلى"),
    ({"marketCapitalization": 39.9, "floatingShare": 0.6}, False, "أقل من 40 مليون"),
    ({"marketCapitalization": 60.1, "floatingShare": 0.6}, False, "أكثر من 60 مليون"),
    ({"marketCapitalization": 45.0, "floatingShare": 0.4}, False, "فلوت 400 ألف — تحت الحد"),
    ({"marketCapitalization": 45.0, "floatingShare": 0.5}, False, "فلوت 500 ألف بالضبط — لازم يكون فوقها"),
    ({"marketCapitalization": 4572794.0, "floatingShare": 14445.7}, False, "آبل — عملاق، لازم يسقط"),
    ({"marketCapitalization": None, "floatingShare": 0.6}, False, "قيمة سوقية ناقصة"),
    ({"marketCapitalization": 45.0, "floatingShare": None}, False, "فلوت ناقص"),
    ({}, False, "بروفايل فاضي"),
]


def fake_api(quotes, vwaps, atrs=None):
    """يبدّل نداءات Twelve Data ببيانات محضّرة، عشان نختبر منطق الفلترة
    نفسه بدون إنترنت وبدون ما نصرف أرصدة."""
    atrs = atrs or {}

    def handler(api_key, endpoint, params):
        if endpoint == "quote":
            wanted = params["symbol"].split(",")
            matched = {s: dict(quotes[s], symbol=s) for s in wanted if s in quotes}
            # نقلّد شكل الرد الحقيقي: رمز واحد يرجع مباشرة، وأكثر يرجع قاموس
            if len(matched) == 1:
                return next(iter(matched.values()))
            return matched
        if endpoint == "vwap":
            return {"values": [{"vwap": str(vwaps[params["symbol"]])}]}
        if endpoint == "atr":
            symbol = params["symbol"]
            if symbol not in atrs:
                return {"code": 500, "message": "atr unavailable"}
            return {"values": [{"atr": str(atrs[symbol])}]}
        return None

    return handler


def scan_cases():
    """يرجّع (الوصف، quotes، vwaps، atrs، الرموز المتوقع تنبيهها) لكل حالة."""
    return [
        (
            "قفزة حجم + فوق VWAP + ATR سليم → تنبيه",
            {"AAA": {"close": "10.5", "volume": "3000000", "average_volume": "1000000"}},
            {"AAA": 10.0},
            {"AAA": 0.5},
            ["AAA"],
        ),
        (
            "قفزة حجم بس تحت VWAP → ما ينبّه",
            {"BBB": {"close": "9.5", "volume": "3000000", "average_volume": "1000000"}},
            {"BBB": 10.0},
            {"BBB": 0.5},
            [],
        ),
        (
            "فوق VWAP بس بدون قفزة حجم → ما ينبّه",
            {"CCC": {"close": "10.5", "volume": "1100000", "average_volume": "1000000"}},
            {"CCC": 10.0},
            {"CCC": 0.5},
            [],
        ),
        (
            "الحجم بالضبط ضعف المعدل → ينبّه (الشرط >=)",
            {"DDD": {"close": "10.5", "volume": "2000000", "average_volume": "1000000"}},
            {"DDD": 10.0},
            {"DDD": 0.5},
            ["DDD"],
        ),
        (
            "معدل حجم صفر → يتجاهله بدل ما ينهار بقسمة على صفر",
            {"EEE": {"close": "10.5", "volume": "3000000", "average_volume": "0"}},
            {"EEE": 10.0},
            {"EEE": 0.5},
            [],
        ),
        (
            "بيانات ناقصة → يتجاهله بدون انهيار",
            {"FFF": {"close": None, "volume": None, "average_volume": None}},
            {"FFF": 10.0},
            {"FFF": 0.5},
            [],
        ),
        (
            "الشروط الأربعة تحققت لكن ATR فشل → التنبيه يوصل بدون وقف خسارة",
            {"GGG": {"close": "10.5", "volume": "3000000", "average_volume": "1000000"}},
            {"GGG": 10.0},
            {},  # ATR غير متوفر لهذا الرمز
            ["GGG"],
        ),
    ]


# استقبال أكثر من محادثة على نفس البوت (2026-08-11)
CHAT_ID_CASES = [
    ({"telegram_chat_ids": [111, 222]}, ["111", "222"], "قائمة صريحة — شخصين"),
    ({"telegram_chat_id": "333"}, ["333"], "الإعداد القديم لسه يشتغل — شخص واحد"),
    ({"telegram_chat_ids": ["444"]}, ["444"], "قائمة بعنصر واحد"),
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
    for profile, expected, note in FILTER_CASES:
        got = bot.passes_size_filter({}, profile)
        ok = got == expected
        failures += not ok
        status = "نجح  " if ok else "فشل  "
        print(f"{status} فلتر الحجم: {note:45s} -> {got}")

    print()
    cases = scan_cases()
    original_request = bot.twelvedata_request
    original_sleep = bot.time.sleep
    bot.time.sleep = lambda seconds: None  # ما ننتظر حدود المعدل في الاختبار
    try:
        for note, quotes, vwaps, atrs, expected in cases:
            bot.twelvedata_request = fake_api(quotes, vwaps, atrs)
            bot.save_watchlist([{"symbol": s} for s in quotes])
            alerts, error = bot.scan_watchlist({"twelvedata_api_key": "x"})
            got = sorted(a["symbol"] for a in (alerts or []))
            ok = error is None and got == sorted(expected)
            failures += not ok
            status = "نجح  " if ok else "فشل  "
            print(f"{status} الفحص: {note:55s} -> {got}")

        # حالة GGG لازم يوصل تنبيهها بدون وقف خسارة، مو يسقط بصمت
        bot.twelvedata_request = fake_api(
            {"GGG": {"close": "10.5", "volume": "3000000", "average_volume": "1000000"}}, {"GGG": 10.0}, {}
        )
        bot.save_watchlist([{"symbol": "GGG"}])
        alerts, _ = bot.scan_watchlist({"twelvedata_api_key": "x"})
        ok = len(alerts) == 1 and alerts[0]["stop_loss"] is None
        failures += not ok
        print(f"{'نجح ' if ok else 'فشل '} فشل ATR: التنبيه يحمل stop_loss=None، ما يسقط بالكامل")

        # حساب وقف الخسارة نفسه: entry - (ATR × مضاعف)
        bot.twelvedata_request = fake_api(
            {"HHH": {"close": "10.5", "volume": "3000000", "average_volume": "1000000"}}, {"HHH": 10.0}, {"HHH": 2.0}
        )
        bot.save_watchlist([{"symbol": "HHH"}])
        alerts, _ = bot.scan_watchlist({"twelvedata_api_key": "x", "atr_multiplier": 1.5})
        expected_stop = 10.5 - (2.0 * 1.5)  # = 7.5
        got_stop = alerts[0]["stop_loss"] if alerts else None
        ok = got_stop is not None and abs(got_stop - expected_stop) < 0.001
        failures += not ok
        print(f"{'نجح ' if ok else 'فشل '} حساب وقف الخسارة: entry=10.5 ATR=2.0 x1.5 -> {got_stop} (متوقع {expected_stop})")
    finally:
        bot.twelvedata_request = original_request
        bot.time.sleep = original_sleep
        if os.path.exists(bot.WATCHLIST_PATH):
            os.remove(bot.WATCHLIST_PATH)

    print()
    for config, expected, note in CHAT_ID_CASES:
        got = bot.allowed_chat_ids(config)
        ok = got == expected
        failures += not ok
        status = "نجح  " if ok else "فشل  "
        print(f"{status} allowed_chat_ids: {note:40s} -> {got}")

    print()
    # درس 2026-08-11: إعادة تشغيل البوت أثناء /refresh مسحت 25 سهم كانوا
    # اتلقوا لأن الحفظ كان بس في النهاية. هذا الاختبار يتأكد إن الحفظ يصير
    # فور كل سهم جديد، مو بانتظار نهاية المسح الكامل.
    original_finnhub = bot.finnhub_request
    original_us_stocks = bot.us_common_stocks
    original_sleep2 = bot.time.sleep
    bot.time.sleep = lambda seconds: None

    profiles = {
        "AAA": {"marketCapitalization": 45.0, "floatingShare": 0.6, "name": "AAA Inc"},
        "BBB": {"marketCapitalization": 999.0, "floatingShare": 0.6, "name": "BBB Inc"},  # يسقط بالحجم
        "CCC": {"marketCapitalization": 50.0, "floatingShare": 0.55, "name": "CCC Inc"},
    }
    saves_seen = []

    def fake_finnhub(api_key, endpoint, params, fatal=False):
        return profiles.get(params["symbol"], {"_error": "missing"})

    bot.us_common_stocks = lambda api_key: list(profiles.keys())
    bot.finnhub_request = fake_finnhub
    original_save = bot.save_watchlist
    bot.save_watchlist = lambda wl: (saves_seen.append(len(wl)), original_save(wl))

    try:
        found, error = bot.build_watchlist({"finnhub_api_key": "x"})
        # لازم ينحفظ مرتين (بعد AAA، وبعد CCC) — مو مرة وحدة بالنهاية بس
        ok = error is None and saves_seen == [1, 2] and len(found) == 2
        failures += not ok
        print(f"{'نجح ' if ok else 'فشل '} build_watchlist يحفظ تدريجياً، مو بالنهاية بس -> saves={saves_seen} found={len(found)}")

        # القائمة على القرص لازم تطابق النتيجة حتى لو ما استدعينا save يدوياً
        on_disk = bot.load_watchlist()
        ok2 = {s["symbol"] for s in on_disk} == {"AAA", "CCC"}
        failures += not ok2
        print(f"{'نجح ' if ok2 else 'فشل '} القائمة على القرص مطابقة للنتيجة -> {[s['symbol'] for s in on_disk]}")
    finally:
        bot.finnhub_request = original_finnhub
        bot.us_common_stocks = original_us_stocks
        bot.save_watchlist = original_save
        bot.time.sleep = original_sleep2
        if os.path.exists(bot.WATCHLIST_PATH):
            os.remove(bot.WATCHLIST_PATH)

    print()
    total = (
        len(CASES) + len(SUNDAY_CASES) + len(FILTER_CASES) + len(cases) + 2
        + len(CHAT_ID_CASES) + 2
    )
    if failures:
        print(f"❌ فشل {failures} اختبار")
        sys.exit(1)
    print(f"✅ كل الاختبارات نجحت ({total})")


if __name__ == "__main__":
    main()
