"""窗口大小与位置的持久化。

用 `data/ui.json` 而不是 QSettings：QSettings 在 Windows 写注册表，删掉项目
目录以后还会留下残留；这个项目的所有状态都该跟着 `data/` 走（也方便整个目录搬走）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app import config

log = logging.getLogger("ui.state")

FILE_NAME = "ui.json"


def state_path() -> Path:
    return config.DATA_DIR / FILE_NAME


def load_state() -> dict:
    """读整个 ui.json（可能含 window / library_columns 等多个小节）。读不到返回空 dict。"""
    path = state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("读 %s 失败，用默认值：%s", path, exc)
        return {}

    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        log.warning("%s 不是合法 JSON，忽略：%s", path, exc)
        return {}

    return data if isinstance(data, dict) else {}


def save_state(**sections: object) -> None:
    """把若干小节合并进 ui.json；其它小节保持不动。"""
    data = load_state()
    data.update({key: value for key, value in sections.items() if value is not None})
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("写 %s 失败：%s", path, exc)


def load_window_state() -> dict:
    """读窗口大小/位置。文件不存在、坏了、格式不对都返回空 dict，不抛异常。"""
    window = load_state().get("window")
    return window if isinstance(window, dict) else {}


def save_window_state(width: int, height: int, x: int | None = None, y: int | None = None) -> None:
    """写窗口大小/位置。写不进去只记日志——这只是个便利功能，不该阻断退出。"""
    window = {"width": int(width), "height": int(height)}
    if x is not None and y is not None:
        window["x"] = int(x)
        window["y"] = int(y)
    save_state(window=window)


def load_library_columns() -> list[int] | None:
    """读词库表列宽。没存过或数据不合法返回 None。"""
    widths = load_state().get("library_columns")
    if not isinstance(widths, list) or not widths:
        return None
    result: list[int] = []
    for width in widths:
        if not isinstance(width, int) or width < 40:
            return None
        result.append(width)
    return result


def save_library_columns(widths: list[int]) -> None:
    save_state(library_columns=[int(width) for width in widths])


def load_auto_enrich() -> bool:
    """读出"识别后自动补全"开关，没存过默认开着。"""
    capture = load_state().get("capture")
    if not isinstance(capture, dict):
        return True
    value = capture.get("auto_enrich", True)
    return bool(value) if isinstance(value, bool) else True


def save_auto_enrich(enabled: bool) -> None:
    save_state(capture={"auto_enrich": bool(enabled)})