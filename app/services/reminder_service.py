"""提醒的判定逻辑：只回答"现在这一刻该不该弹提醒"。

这一层故意不 import PySide6、也不自己持有 QTimer：

* 定时器在 `app/main.py` 里（它是界面的一部分，一分钟问一次就够了）；
* 判定本身是纯函数 + 一点状态，这样 `scripts/verify_reminder.py` 能在没有
  图形环境的情况下把"到点、已打卡、今天已经提醒过、延后再提醒、跨零点"
  这些情况一次性验完。

提醒的规则（写在这里，免得以后自己忘了为什么这么设计）：

1. 今天已经打卡 → 永远不提醒，并且把"延后再提醒"清掉；
2. 没到 REMINDER_TIME（默认 21:00）→ 不提醒；
3. 到了点、今天还没提醒过 → 提醒一次；
4. 同一天只自动提醒一次，用户点了"10 分钟后再提醒"才允许再弹；
5. 过了零点就是新的一天，第二天到点还会提醒。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

DEFAULT_REMINDER_TIME = "21:00"
DEFAULT_SNOOZE_MINUTES = 10


def parse_reminder_time(text: str, *, default: str = DEFAULT_REMINDER_TIME) -> time:
    """把 "21:00" / "7:5" / "21:00:00" 解析成 time；看不懂就用默认值。

    配置写错不该让程序起不来，所以这里永远返回一个可用的 time。
    """
    raw = (text or "").strip()
    for candidate in (raw, default):
        parts = candidate.split(":")
        if len(parts) < 2:
            continue
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except (TypeError, ValueError):
            continue
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        return time(hour=hour, minute=minute)
    return time(hour=21, minute=0)


def format_reminder_time(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


class ReminderGate:
    """提醒闸门：记住"今天提醒过没有"和"延后到什么时候"。

    调用方每一分钟调一次 `should_notify()`，返回 True 时弹窗，然后调
    `mark_notified()`。把"弹窗"和"判定"分开，是为了弹窗失败（比如托盘没了、
    显示器拔了）时不会把状态记成"已提醒"。
    """

    def __init__(
        self,
        reminder_time: str = DEFAULT_REMINDER_TIME,
        *,
        snooze_minutes: int = DEFAULT_SNOOZE_MINUTES,
    ) -> None:
        self._time = parse_reminder_time(reminder_time)
        self._snooze_minutes = max(1, int(snooze_minutes))
        self._notified_on: date | None = None
        self._snooze_until: datetime | None = None

    # ---- 只读状态 ----------------------------------------------------

    @property
    def reminder_time(self) -> time:
        return self._time

    @property
    def reminder_text(self) -> str:
        return format_reminder_time(self._time)

    @property
    def snoozed_until(self) -> datetime | None:
        return self._snooze_until

    @property
    def notified_on(self) -> date | None:
        return self._notified_on

    # ---- 设置 --------------------------------------------------------

    def set_reminder_time(self, text: str) -> None:
        """改提醒时间。改完不影响"今天已经提醒过"这件事。"""
        self._time = parse_reminder_time(text)

    def set_snooze_minutes(self, minutes: int) -> None:
        self._snooze_minutes = max(1, int(minutes))

    def reset(self) -> None:
        """清空状态（测试和"退出重开"时用）。"""
        self._notified_on = None
        self._snooze_until = None

    # ---- 判定 --------------------------------------------------------

    def should_notify(self, now: datetime, *, checked_today: bool) -> bool:
        """这一刻要不要弹提醒。纯查询，不改状态。"""
        if checked_today:
            return False

        # 用户主动点的"10 分钟后再提醒"优先于"今天已经提醒过"和"还没到点"
        if self._snooze_until is not None:
            return now >= self._snooze_until

        if now.time() < self._time:
            return False

        return self._notified_on != now.date()

    def mark_notified(self, now: datetime | None = None) -> None:
        """记下"今天提醒过了"，并清掉延后时间。"""
        moment = now or datetime.now()
        self._notified_on = moment.date()
        self._snooze_until = None

    def snooze(self, now: datetime | None = None) -> datetime:
        """延后再提醒，返回延到的时间点。"""
        moment = now or datetime.now()
        self._snooze_until = moment + timedelta(minutes=self._snooze_minutes)
        return self._snooze_until

    def on_checked(self, now: datetime | None = None) -> None:
        """今天打完卡了：清掉延后，今天也不再提醒。"""
        moment = now or datetime.now()
        self._notified_on = moment.date()
        self._snooze_until = None