"""阶段 7 验收（一）：提醒时间解析与"该不该弹"的判定。

这些全是纯函数/纯状态，不建 QApplication、不联网、不碰数据库，
所以一秒内跑完，失败信息也直白。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.reminder_service import (  # noqa: E402
    ReminderGate,
    format_reminder_time,
    parse_reminder_time,
)

_passed = 0
_failed = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"[PASS] {name}")
    else:
        _failed += 1
        print(f"[FAIL] {name}" + (f" -> {extra}" if extra else ""))


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got={got!r} want={want!r}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def main() -> int:
    section("1. 时间解析：能读的都读对，读不懂就用默认值")
    eq("21:00", parse_reminder_time("21:00"), time(21, 0))
    eq("7:05 补零", format_reminder_time(parse_reminder_time("7:05")), "07:05")
    eq("23:59", parse_reminder_time("23:59"), time(23, 59))
    eq("0:00", parse_reminder_time("0:00"), time(0, 0))
    eq("带秒 21:30:45 只取时分", parse_reminder_time("21:30:45"), time(21, 30))
    eq("两端空格", parse_reminder_time("  20:15  "), time(20, 15))
    eq("空串回落 21:00", parse_reminder_time(""), time(21, 0))
    eq("乱码回落 21:00", parse_reminder_time("晚上九点"), time(21, 0))
    eq("25:00 越界回落", parse_reminder_time("25:00"), time(21, 0))
    eq("21:70 越界回落", parse_reminder_time("21:70"), time(21, 0))
    eq("只有小时回落", parse_reminder_time("21"), time(21, 0))
    eq("自定义默认值", parse_reminder_time("坏值", default="08:30"), time(8, 30))
    eq("格式化补零", format_reminder_time(time(9, 5)), "09:05")

    section("2. 到点判定（提醒时间 21:00）")
    gate = ReminderGate("21:00")
    eq("暴露的时间", gate.reminder_text, "21:00")
    check("20:59 不提醒", not gate.should_notify(datetime(2026, 11, 2, 20, 59), checked_today=False))
    check("21:00 整点提醒", gate.should_notify(datetime(2026, 11, 2, 21, 0), checked_today=False))
    check("22:30 也提醒（错过了到点那一刻）", gate.should_notify(datetime(2026, 11, 2, 22, 30), checked_today=False))
    check("已打卡则 22:30 不提醒", not gate.should_notify(datetime(2026, 11, 2, 22, 30), checked_today=True))
    check("判定本身不改状态（连问三次都是 True）",
          gate.should_notify(datetime(2026, 11, 2, 21, 0), checked_today=False)
          and gate.should_notify(datetime(2026, 11, 2, 21, 0), checked_today=False)
          and gate.should_notify(datetime(2026, 11, 2, 21, 0), checked_today=False))

    section("3. 同一天只自动提醒一次")
    gate.mark_notified(datetime(2026, 11, 2, 21, 0))
    eq("记下了提醒日期", gate.notified_on, date(2026, 11, 2))
    check("21:01 不再提醒", not gate.should_notify(datetime(2026, 11, 2, 21, 1), checked_today=False))
    check("23:59 不再提醒", not gate.should_notify(datetime(2026, 11, 2, 23, 59), checked_today=False))

    section("4. 跨零点算新的一天")
    check("00:30 还没到点，不提醒",
          not gate.should_notify(datetime(2026, 11, 3, 0, 30), checked_today=False))
    check("第二天 21:00 重新提醒",
          gate.should_notify(datetime(2026, 11, 3, 21, 0), checked_today=False))

    section("5. 延后再提醒")
    gate = ReminderGate("21:00", snooze_minutes=10)
    gate.mark_notified(datetime(2026, 11, 2, 21, 0))
    until = gate.snooze(datetime(2026, 11, 2, 21, 2))
    eq("延到 21:12", until, datetime(2026, 11, 2, 21, 12))
    eq("暴露的延后时间", gate.snoozed_until, datetime(2026, 11, 2, 21, 12))
    check("21:05 还在延后期，不提醒",
          not gate.should_notify(datetime(2026, 11, 2, 21, 5), checked_today=False))
    check("21:12 到点，提醒",
          gate.should_notify(datetime(2026, 11, 2, 21, 12), checked_today=False))
    gate.mark_notified(datetime(2026, 11, 2, 21, 12))
    eq("提醒后清掉延后时间", gate.snoozed_until, None)
    check("再往后又不提醒了",
          not gate.should_notify(datetime(2026, 11, 2, 21, 30), checked_today=False))

    section("6. 延后期跨零点也照样弹")
    gate = ReminderGate("23:50", snooze_minutes=10)
    gate.mark_notified(datetime(2026, 11, 2, 23, 50))
    gate.snooze(datetime(2026, 11, 2, 23, 55))
    check("23:59 还没到延后点", not gate.should_notify(datetime(2026, 11, 2, 23, 59), checked_today=False))
    check("次日 00:05 到延后点，弹", gate.should_notify(datetime(2026, 11, 3, 0, 5), checked_today=False))

    section("7. 打完卡：延后作废，当天彻底安静")
    gate = ReminderGate("21:00")
    gate.mark_notified(datetime(2026, 11, 2, 21, 0))
    gate.snooze(datetime(2026, 11, 2, 21, 1))
    gate.on_checked(datetime(2026, 11, 2, 21, 3))
    eq("延后被清掉", gate.snoozed_until, None)
    check("21:30 不提醒", not gate.should_notify(datetime(2026, 11, 2, 21, 30), checked_today=True))
    check("就算判定时忘了传 checked_today 也不提醒（已经记成今天提醒过）",
          not gate.should_notify(datetime(2026, 11, 2, 21, 30), checked_today=False))

    section("8. 改时间 / 重置")
    gate = ReminderGate("21:00")
    gate.set_reminder_time("07:30")
    eq("改成 07:30", gate.reminder_text, "07:30")
    check("07:29 不提醒", not gate.should_notify(datetime(2026, 11, 2, 7, 29), checked_today=False))
    check("07:30 提醒", gate.should_notify(datetime(2026, 11, 2, 7, 30), checked_today=False))
    gate.set_reminder_time("这不是时间")
    eq("写错了回落到 21:00", gate.reminder_text, "21:00")
    gate.mark_notified(datetime(2026, 11, 2, 21, 0))
    gate.reset()
    eq("reset 清掉提醒日期", gate.notified_on, None)
    check("reset 后当天又能提醒",
          gate.should_notify(datetime(2026, 11, 2, 21, 5), checked_today=False))

    section("9. 延后分钟数的边界")
    gate = ReminderGate("21:00", snooze_minutes=0)
    gate.mark_notified(datetime(2026, 11, 2, 21, 0))
    until = gate.snooze(datetime(2026, 11, 2, 21, 0))
    eq("0 分钟被夹成 1 分钟", until, datetime(2026, 11, 2, 21, 1))
    gate.set_snooze_minutes(30)
    eq("改成 30 分钟", gate.snooze(datetime(2026, 11, 2, 21, 0)), datetime(2026, 11, 2, 21, 30))

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())