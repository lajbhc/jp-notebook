"""打卡的业务规则。

只有一件事是"写"：今天学完了 → 记一笔。其余全是读，给界面画日历用。

连续天数怎么算（这条规则决定了界面上那个数字，所以写清楚）：

- 今天打了卡：从今天往前数，连续多少天有打卡。
- 今天还没打、但昨天打了：从昨天往前数。也就是"今天还有机会不断"，
  打开程序时看到的应该是昨天那段，而不是 0。
- 今天没打、昨天也没打：断了，返回 0。

这样一来，"点完打卡按钮后显示的数字"和"第二天早上打开程序看到的数字"是一致的，
不会出现昨天连续 7 天、今天一早变成 0 这种让人泄气的情况。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from app.storage.db import StorageError
from app.storage.repository import CheckinRepository

# 往前找连续天数最多找这么多天，防止有人在库里塞了奇怪数据导致死循环
_MAX_STREAK_DAYS = 4000

# 存储层可能冒出来的东西：sqlite 出错（StorageError）、目录不可写或磁盘满（OSError）、
# 日期不合法（ValueError）。界面只该看到 CheckinFailed，所以在这里统一收口。
_STORAGE_ERRORS = (StorageError, OSError, ValueError)


class CheckinFailed(RuntimeError):
    """打卡相关的失败。UI 只认这一个异常，不用知道是 sqlite 还是磁盘满了。"""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass(frozen=True)
class Status:
    """打卡页一屏需要的全部信息。"""

    today: str
    checked_today: bool
    streak: int
    total: int
    month_count: int
    month_dates: tuple[str, ...]
    first_date: str | None

    @property
    def streak_text(self) -> str:
        if self.streak <= 0:
            return "还没有连续记录"
        return f"已连续 {self.streak} 天"

    @property
    def today_text(self) -> str:
        return "今天已打卡" if self.checked_today else "今天还没打卡"

    @property
    def progress_text(self) -> str:
        return f"累计 {self.total} 天 · 本月 {self.month_count} 天"


@dataclass(frozen=True)
class DayCell:
    """日历上的一格。空位（月初月末的补白）三个字段都是空/False。"""

    text: str
    iso: str
    checked: bool

    @property
    def empty(self) -> bool:
        return not self.iso

    @property
    def is_today(self) -> bool:
        return self.iso == date.today().isoformat()


_EMPTY_CELL = DayCell("", "", False)


def today_text(today: date | None = None) -> str:
    return (today or date.today()).isoformat()


def streak_from(dates: Iterable[str], today: date | None = None) -> int:
    """按上面 docstring 的规则算连续天数。纯函数，方便单测。"""
    day = today or date.today()
    have = set(dates)
    if not have:
        return 0

    cursor = day
    if cursor.isoformat() not in have:
        # 今天还没打卡：昨天打了就从昨天数，昨天也没打就是断了
        cursor = day - timedelta(days=1)
        if cursor.isoformat() not in have:
            return 0

    count = 0
    while count < _MAX_STREAK_DAYS and cursor.isoformat() in have:
        count += 1
        cursor -= timedelta(days=1)
    return count


def status(
    today: date | None = None,
    *,
    repo: CheckinRepository | None = None,
) -> Status:
    """读当前状态。失败抛 CheckinFailed，不把 sqlite 的异常漏给界面。"""
    day = today or date.today()
    repo = repo or CheckinRepository()
    try:
        all_dates = repo.dates()
        month = repo.month_dates(day.year, day.month)
        first = repo.first_date()
    except _STORAGE_ERRORS as exc:
        raise CheckinFailed("读取打卡记录失败，稍后再试一次。", str(exc)) from exc

    return Status(
        today=day.isoformat(),
        checked_today=day.isoformat() in set(all_dates),
        streak=streak_from(all_dates, day),
        total=len(all_dates),
        month_count=len(month),
        month_dates=tuple(month),
        first_date=first,
    )


def check_today(
    note: str = "",
    today: date | None = None,
    *,
    repo: CheckinRepository | None = None,
) -> tuple[Status, bool]:
    """给"今天"打卡。返回 (最新状态, 是否这次真的新记了一笔)。

    重复点不会产生第二行，也不会覆盖第一次的备注——已经有记录了就当无事发生，
    这样按钮就算被连点两下也不会出现"备注被后来的空值洗掉"。
    """
    day = today or date.today()
    repo = repo or CheckinRepository()
    try:
        newly = repo.check(day.isoformat(), (note or "").strip())
        fresh = status(day, repo=repo)
    except _STORAGE_ERRORS as exc:
        raise CheckinFailed("打卡没写进去，请检查 data 目录是否可写。", str(exc)) from exc
    return fresh, newly


def month_grid(year: int, month: int, *, repo: CheckinRepository | None = None) -> list[list[DayCell]]:
    """把某个月排成"周一到周日"的二维表，空位用空 DayCell 占。

    界面只负责按 checked 上色，日期该排在哪一周由这里算好，
    免得 UI 里再写一遍日历逻辑（闰年、月初是周几都很容易写错）。
    """
    repo = repo or CheckinRepository()
    try:
        checked = set(repo.month_dates(year, month))
    except _STORAGE_ERRORS as exc:
        raise CheckinFailed("读取本月打卡记录失败。", str(exc)) from exc

    first = date(year, month, 1)
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    total_days = (next_month - first).days
    # 周一=0 ... 周日=6，日历从周一开始排
    offset = first.weekday()

    cells: list[DayCell] = [_EMPTY_CELL] * offset
    for day_number in range(1, total_days + 1):
        iso = date(year, month, day_number).isoformat()
        cells.append(DayCell(str(day_number), iso, iso in checked))
    while len(cells) % 7:
        cells.append(_EMPTY_CELL)

    return [cells[index : index + 7] for index in range(0, len(cells), 7)]