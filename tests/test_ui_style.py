"""界面样式的兜底行为：样式表本身没什么逻辑，但"坏数据不许崩"值得钉住。"""

from __future__ import annotations

import json

from app.ui import style, window_state


# ------------------------------------------------------------------ 样式
def test_banner_falls_back_to_info_for_unknown_level() -> None:
    unknown = style.banner_qss("whatever")
    assert unknown == style.banner_qss("info")
    assert "background" in unknown


def test_banner_compact_only_changes_padding() -> None:
    full = style.banner_qss("ok")
    compact = style.banner_qss("ok", compact=True)
    assert style.BANNER_STYLES["ok"] in full
    assert style.BANNER_STYLES["ok"] in compact
    assert style.BANNER_PADDING in full
    assert style.BANNER_PADDING_COMPACT in compact


def test_field_error_has_two_paddings() -> None:
    assert "padding: 8px 10px;" in style.field_error_qss()
    assert "padding: 6px 10px;" in style.field_error_qss(compact=True)


def test_style_tokens_are_non_empty_strings() -> None:
    for name in ("TITLE", "TITLE_SMALL", "BODY", "BODY_BOLD", "HINT", "HINT_DIM", "HINT_FAINT", "CAPTION", "EMPTY"):
        value = getattr(style, name)
        assert isinstance(value, str) and "font-size" in value


def test_min_window_not_larger_than_default() -> None:
    assert style.MIN_WINDOW_SIZE[0] < style.DEFAULT_WINDOW_SIZE[0]
    assert style.MIN_WINDOW_SIZE[1] < style.DEFAULT_WINDOW_SIZE[1]


# ------------------------------------------------------- 窗口状态持久化
def test_window_state_roundtrip(sandbox) -> None:
    import app.config as config

    original = config.DATA_DIR
    config.DATA_DIR = sandbox
    try:
        assert window_state.load_window_state() == {}      # 还没写过
        window_state.save_window_state(1200, 800, 40, 60)
        assert (sandbox / "ui.json").is_file()
        state = window_state.load_window_state()
        assert state == {"width": 1200, "height": 800, "x": 40, "y": 60}
    finally:
        config.DATA_DIR = original


def test_window_state_without_position(sandbox) -> None:
    import app.config as config

    original = config.DATA_DIR
    config.DATA_DIR = sandbox
    try:
        window_state.save_window_state(1000, 700)
        assert window_state.load_window_state() == {"width": 1000, "height": 700}
    finally:
        config.DATA_DIR = original


def test_broken_json_is_ignored(sandbox) -> None:
    import app.config as config

    original = config.DATA_DIR
    config.DATA_DIR = sandbox
    try:
        (sandbox / "ui.json").write_text("{ 这不是 json", encoding="utf-8")
        assert window_state.load_window_state() == {}
    finally:
        config.DATA_DIR = original


def test_json_without_window_key_is_ignored(sandbox) -> None:
    import app.config as config

    original = config.DATA_DIR
    config.DATA_DIR = sandbox
    try:
        (sandbox / "ui.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
        assert window_state.load_window_state() == {}
        (sandbox / "ui.json").write_text(json.dumps({"window": "nope"}), encoding="utf-8")
        assert window_state.load_window_state() == {}
    finally:
        config.DATA_DIR = original


def test_library_columns_roundtrip(sandbox) -> None:
    import app.config as config

    original = config.DATA_DIR
    config.DATA_DIR = sandbox
    try:
        assert window_state.load_library_columns() is None  # 还没存过
        window_state.save_library_columns([58, 260, 120, 200, 180, 132])
        assert window_state.load_library_columns() == [58, 260, 120, 200, 180, 132]

        # 写窗口尺寸不该把列宽冲掉（两个小节共存于同一个 ui.json）
        window_state.save_window_state(1400, 900)
        assert window_state.load_library_columns() == [58, 260, 120, 200, 180, 132]
        assert window_state.load_window_state()["width"] == 1400
    finally:
        config.DATA_DIR = original


def test_bad_library_columns_are_ignored(sandbox) -> None:
    import app.config as config

    original = config.DATA_DIR
    config.DATA_DIR = sandbox
    path = sandbox / "ui.json"
    try:
        for bad in ("nope", [], [1, 2], [58, "260"]):
            path.write_text(json.dumps({"library_columns": bad}), encoding="utf-8")
            assert window_state.load_library_columns() is None, bad

        # 小于 40 像素的宽度视为坏数据（拖这么窄基本是误操作）
        path.write_text(json.dumps({"library_columns": [58, 10]}), encoding="utf-8")
        assert window_state.load_library_columns() is None
    finally:
        config.DATA_DIR = original