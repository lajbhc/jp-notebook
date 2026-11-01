"""词库的查询与维护门面。

界面只调本模块，不 import repository、更不写 SQL。这样做的直接好处：等阶段 6
做「今日新增」统计、阶段 8 做导出时，SQL 仍然只长在 storage 一层。

异常约定（与录入链路一致）：
- `FilterInvalid`：筛选条件本身不成立（比如开始日期晚于结束日期），只带提示文案。
- `LibraryFailed`：读/删库失败，带 `user_message` 与 `detail`（detail 只进终端）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app import models
from app.storage.db import StorageError
from app.storage.repository import EntryRepository

# 每页条数可选值。20 条在 720 高的窗口里刚好一屏放得下，不用滚。
PAGE_SIZE_OPTIONS: tuple[int, ...] = (20, 50, 100)
DEFAULT_PAGE_SIZE = 20


class LibraryFailed(RuntimeError):
    """读库或删库失败。UI 只显示 user_message，详细原因写日志/终端。"""

    def __init__(self, user_message: str, detail: str = "") -> None:
        self.user_message = user_message
        self.detail = detail
        super().__init__(detail or user_message)


class FilterInvalid(ValueError):
    """筛选条件不成立。提示语由界面直接显示在输入框下方。"""

    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


@dataclass(frozen=True)
class EntryPage:
    """一页查询结果。界面需要的信息都在这里，避免它自己算页数。"""

    items: tuple[models.Entry, ...] = field(default_factory=tuple)
    total: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def page_text(self) -> str:
        return f"第 {self.page} / {self.pages} 页"

    @property
    def range_text(self) -> str:
        """'第 1–20 条，共 37 条'。空结果说清楚是"没有"，而不是'第 0 条'。"""
        if self.total == 0 or not self.items:
            return "没有匹配的记录"
        first = (self.page - 1) * self.page_size + 1
        last = first + len(self.items) - 1
        return f"第 {first}–{last} 条，共 {self.total} 条"


@dataclass(frozen=True)
class LibraryStats:
    """词库概览，显示在标题右边。"""

    total: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    today: int = 0

    def text(self) -> str:
        parts = [f"共 {self.total} 条"]
        for entry_type in models.ENTRY_TYPES:
            parts.append(f"{models.TYPE_LABELS[entry_type]} {self.by_type.get(entry_type, 0)}")
        parts.append(f"今日新增 {self.today}")
        return " · ".join(parts)


def _check_filters(entry_type: str | None, date_from: str | None, date_to: str | None) -> None:
    if entry_type is not None and entry_type not in models.ENTRY_TYPES:
        raise FilterInvalid(f"类型只能是{ ' / '.join(models.ENTRY_TYPES) }。")
    if date_from and date_to and date_from > date_to:
        # 不自动调换：用户可能只是填反了，静默纠正会让筛选结果看起来"莫名其妙"
        raise FilterInvalid("开始日期晚于结束日期，请调换两个日期或取消日期筛选。")


def search_entries(
    keyword: str = "",
    entry_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    repo: EntryRepository | None = None,
) -> EntryPage:
    """按条件分页查询。页号越界时自动收到有效范围内，不报错。"""
    keyword = (keyword or "").strip()
    _check_filters(entry_type, date_from, date_to)

    if page_size < 1:
        raise FilterInvalid("每页条数必须大于 0。")
    page_size = int(page_size)
    repository = repo or EntryRepository()

    try:
        total = repository.count(keyword, entry_type, date_from, date_to)
        pages = max(1, (total + page_size - 1) // page_size)
        # 删掉最后一页的最后一条后页号会越界，这里顺手夹回来，省得界面显示"第 3 / 2 页"
        page = min(max(1, int(page)), pages)
        items = repository.search(
            keyword,
            entry_type,
            date_from,
            date_to,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
    except (StorageError, OSError) as exc:
        raise LibraryFailed(
            "读取词库失败，请检查 data 目录和 app.db 是否正常。",
            str(exc),
        ) from exc

    return EntryPage(items=tuple(items), total=total, page=page, page_size=page_size)


def stats(*, repo: EntryRepository | None = None) -> LibraryStats:
    """总条数、两类各多少、今天新增多少。"""
    repository = repo or EntryRepository()
    try:
        by_type = repository.count_by_type()
        today = repository.today_count()
    except (StorageError, OSError) as exc:
        raise LibraryFailed("统计词库失败，数据库可能被占用。", str(exc)) from exc
    return LibraryStats(total=sum(by_type.values()), by_type=by_type, today=today)


def get_entry(entry_id: int, *, repo: EntryRepository | None = None) -> models.Entry | None:
    """按 id 取一条。记录已被删掉时返回 None，让界面自己提示。"""
    repository = repo or EntryRepository()
    try:
        return repository.get(entry_id)
    except (StorageError, OSError) as exc:
        raise LibraryFailed("读取这条记录失败，数据库可能被占用。", str(exc)) from exc


def delete_entry(entry_id: int, *, repo: EntryRepository | None = None) -> bool:
    """删除一条记录，返回是否真的删掉了。

    只删数据库行，不删 data/uploads 里的原图：留着才有可能手工找回，
    而且误删时图片还在，重新录入的成本低得多。代价是磁盘上会有孤儿图片，
    MVP 阶段先接受（README 里写明了）。
    """
    repository = repo or EntryRepository()
    try:
        return repository.delete(entry_id)
    except (StorageError, OSError) as exc:
        raise LibraryFailed(
            "删除失败，数据库可能被占用，请稍后重试。",
            str(exc),
        ) from exc


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "EntryPage",
    "FilterInvalid",
    "LibraryFailed",
    "LibraryStats",
    "PAGE_SIZE_OPTIONS",
    "delete_entry",
    "get_entry",
    "search_entries",
    "stats",
]