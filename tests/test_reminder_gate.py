"""提醒规则单测：时间解析与"什么时候该弹、什么时候闭嘴"。

`ReminderGate` 故意不碰任何 Qt / 定时器，就是为了这一组用例——不用开窗口、
不用等一分钟，直接喂时间进去看结论。
"""

from __future__ import annotations

from datetime import datetime, time

import pytest

from app.services.reminder_service import (
    DEFAULT_REMINDER_TIME,
    ReminderGate,
    format_reminder_time,
    parse_reminder_time,
)


def at(hour: int, minute: int, day: int = 1) -> datetime:
    return datetime(2026, 11, day, hour, minute, 0)


# ---------------------------------------------------------------- 时间解析
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("21:00", time(21, 0)),
        ("7:5", time(7, 5)),
        ("06:30", time(6, 30)),
        ("23:59", time(23, 59)),
        (" 8:15 ", time(8, 15)),
    ],
)
def test_parse_valid_times(text, expected):
    assert parse_reminder_time(text) == expected


@pytest.mark.parametrize("text", ["", "  ", "25:00", "21:70", "21", "晚上九点", "abc"])
def test_parse_bad_times_fall_back_to_default(text):
    assert parse_reminder_time(text) == time(21, 0)
    assert parse_reminder_time(text, default="08:30") == time(8, 30)


def test_format_reminder_time():
    assert format_reminder_time(time(9, 5)) == "09:05"
    assert DEFAULT_REMINDER_TIME == "21:00"


# ---------------------------------------------------------------- 到点判定
def test_silent_before_reminder_time():
    gate = ReminderGate("21:00")

    assert gate.should_notify(at(20, 59), checked_today=False) is False
    assert gate.should_notify(at(21, 0), checked_today=False) is True


def test_late_check_is_still_due():
    gate = ReminderGate("21:00")

    assert gate.should_notify(at(23, 30), checked_today=False) is True


def test_never_remind_after_checkin():
    gate = ReminderGate("21:00")

    assert gate.should_notify(at(21, 0), checked_today=True) is False


def test_should_notify_has_no_side_effect():
    gate = ReminderGate("21:00")

    assert all(gate.should_notify(at(21, 0), checked_today=False) for _ in range(3))
    assert gate.notified_on is None


def test_notifies_only_once_per_day():
    gate = ReminderGate("21:00")

    assert gate.should_notify(at(21, 0), checked_today=False) is True
    gate.mark_notified(at(21, 0))

    assert gate.should_notify(at(21, 5), checked_today=False) is False
    # 第二天照样要提醒
    assert gate.should_notify(at(21, 0, day=2), checked_today=False) is True


# ------------------------------------------------------------------ 延后
def test_snooze_delays_next_reminder():
    gate = ReminderGate("21:00", snooze_minutes=10)
    gate.mark_notified(at(21, 2))

    assert gate.snooze(at(21, 2)) == at(21, 12)
    assert gate.should_notify(at(21, 5), checked_today=False) is False
    assert gate.should_notify(at(21, 12), checked_today=False) is True


def test_snooze_across_midnight():
    gate = ReminderGate("23:50", snooze_minutes=10)
    gate.mark_notified(at(23, 50))

    assert gate.snooze(at(23, 50)) == datetime(2026, 11, 2, 0, 0)
    assert gate.should_notify(datetime(2026, 11, 2, 0, 0), checked_today=False) is True


def test_snooze_minutes_are_clamped():
    """0 分钟、负数分钟都夹到至少 1 分钟，免得变成"立刻再弹"的死循环。"""
    gate = ReminderGate("21:00", snooze_minutes=0)
    gate.mark_notified(at(21, 0))
    assert gate.snooze(at(21, 0)) == at(21, 1)

    gate.set_snooze_minutes(-5)
    gate.mark_notified(at(21, 0))
    assert gate.snooze(at(21, 0)) == at(21, 1)

    gate.set_snooze_minutes(30)
    gate.mark_notified(at(21, 0))
    assert gate.snooze(at(21, 0)) == at(21, 30)


# ------------------------------------------------------------------ 打卡后
def test_checkin_silences_even_if_not_marked_notified():
    gate = ReminderGate("21:00")
    gate.on_checked(at(20, 0))

    assert gate.should_notify(at(21, 0), checked_today=False) is False


def test_reset_clears_state():
    gate = ReminderGate("21:00")
    gate.mark_notified(at(21, 0))

    gate.reset()

    assert gate.notified_on is None
    assert gate.snoozed_until is None
    assert gate.should_notify(at(21, 0), checked_today=False) is True