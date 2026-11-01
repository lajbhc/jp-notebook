"""收图队列：FastAPI 线程往里放，桌面端主线程定时取。

为什么用队列而不是让服务器直接调 UI：
- 服务器跑在 uvicorn 自己的线程里，直接碰 Qt 控件会崩；
- 桌面端用 QTimer 每秒读一次，比 WebSocket 简单得多，局域网内延迟 1 秒足够。

本模块只依赖标准库，不 import app 里任何东西——服务器负责收图，不负责业务。
"""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

T = TypeVar("T")

# 一天里传几十张已经很多了；留 200 条上限防止程序开着几天把内存吃干净。
DEFAULT_MAX_ITEMS = 200


class Inbox(Generic[T]):
    """线程安全的收件箱，带一个只增不减的版本号供 UI 判断"有没有新东西"。"""

    def __init__(self, max_items: int = DEFAULT_MAX_ITEMS) -> None:
        self._items: list[T] = []
        self._lock = threading.Lock()
        self._max = max(1, int(max_items))
        self._version = 0

    def put(self, item: T) -> int:
        """放一张进来，返回放进去之后的版本号。"""
        with self._lock:
            self._items.append(item)
            overflow = len(self._items) - self._max
            if overflow > 0:
                del self._items[:overflow]
            self._version += 1
            return self._version

    def snapshot(self) -> list[T]:
        """取一份当前内容的拷贝，调用方随便遍历不会影响写入方。"""
        with self._lock:
            return list(self._items)

    def latest(self) -> T | None:
        with self._lock:
            return self._items[-1] if self._items else None

    def version(self) -> int:
        with self._lock:
            return self._version

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


__all__ = ["Inbox", "DEFAULT_MAX_ITEMS"]