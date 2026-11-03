"""界面样式的唯一来源：字号、颜色、间距、提示条。

之前每个页面各自写一份内联 `setStyleSheet(...)`，同样是"12px 的灰色小字"
在四个文件里有四种写法（#666 / #777 / #888 / #999 混用），想整体调一次要去
四个地方改。这里把这些收成常量，页面只引用、不再自己拼字符串。

约定：
- 只放"字符串常量"和"纯函数"，不放任何 Qt 控件（避免 import QtWidgets 拖慢导入）。
- 需要按状态变的（提示条三色、错误框）用函数返回，别让调用方拼。
"""

from __future__ import annotations

# --------------------------------------------------------------- 间距
PAGE_MARGIN = 16      # 页面离窗口边缘
SECTION_GAP = 12      # 大块之间
ROW_GAP = 8           # 同一块里的行间距
TIGHT_GAP = 6         # 紧凑排列

# --------------------------------------------------------------- 颜色
ACCENT = "#2f6f9f"        # 主色：生词、链接、框选
ACCENT_DEEP = "#255a84"
MUTED = "#666"
MUTED_DIM = "#777"
FAINT = "#888"
FAINTEST = "#999"
BORDER = "#e2e2e2"
BG_SOFT = "#fafafa"

# --------------------------------------------------------------- 字号
# 只有五档，页面里不要再写"14px"这种临时值
TITLE = "font-size: 18px; font-weight: 600;"
TITLE_SMALL = "font-size: 17px; font-weight: 600;"
BODY = "font-size: 13px;"
HINT = f"font-size: 12px; color: {MUTED};"
HINT_DIM = f"font-size: 12px; color: {MUTED_DIM};"
HINT_FAINT = f"font-size: 12px; color: {FAINT};"
CAPTION = f"font-size: 11px; color: {FAINT};"
BODY_BOLD = "font-size: 13px; font-weight: 600;"
EMPTY = f"font-size: 13px; color: {FAINT};"
ACCENT_TEXT = f"font-size: 11px; color: {ACCENT};"

# --------------------------------------------------------------- 提示条
# 三色：出错（红）、成功（绿）、普通说明（蓝）。浅底 + 深字，不刺眼。
BANNER_STYLES = {
    "error": "background: #fdecec; color: #a33; border-radius: 6px; ",
    "ok": "background: #eef8f0; color: #2f7a43; border-radius: 6px; ",
    "info": "background: #eef4fb; color: #2f5c86; border-radius: 6px; ",
}
BANNER_PADDING = "padding: 9px 12px; font-size: 12px;"
BANNER_PADDING_COMPACT = "padding: 6px 10px; font-size: 12px;"


def banner_qss(level: str = "info", *, compact: bool = False) -> str:
    """提示条样式。level 不认识时按 info 处理，绝不抛异常。"""
    base = BANNER_STYLES.get(level, BANNER_STYLES["info"])
    padding = BANNER_PADDING_COMPACT if compact else BANNER_PADDING
    return base + padding


def field_error_qss(*, compact: bool = False) -> str:
    """表单里那条"这里填错了 / 校验没过"的红字条。"""
    padding = "padding: 6px 10px;" if compact else "padding: 8px 10px;"
    return f"background: #fdecec; color: #a33; border-radius: 6px; {padding} font-size: 12px;"


# --------------------------------------------------------------- 窗口
MIN_WINDOW_SIZE = (960, 640)   # 再小排版就挤了，见 verify_ui 的几何断言
DEFAULT_WINDOW_SIZE = (1000, 720)
NAV_WIDTH = 132                # 左侧导航条宽度

# 顶部那条"没配 Key"的黄色提示：跟普通 banner 不一样，它贴在窗口顶上
CONFIG_BANNER = (
    "padding: 10px 16px; border-bottom: 1px solid #e0b252;"
    "background: #fdf6e3; color: #7a5a00; font-size: 12px;"
)
NAV_QSS = (
    "QListWidget { border: none; border-right: 1px solid #e6e6e6; padding-top: 8px; }"
    "QListWidget::item { padding: 10px 14px; }"
)

# --------------------------------------------------------------- 控件
BUTTON_HEIGHT = 30          # 普通按钮最小高度：26px 太薄，点着费劲
NOTE_MIN_HEIGHT = 64        # 备注框最小高度
NOTE_MAX_HEIGHT = 160       # 备注框最多长到这么高，再高就是白占地方
PRIMARY_BUTTON = (
    "QPushButton { background: %s; color: #ffffff; border: none;"
    " border-radius: 4px; padding: 5px 16px; font-weight: 600; }"
    "QPushButton:hover { background: %s; }"
    "QPushButton:disabled { background: #b9c6cf; color: #f0f0f0; }"
) % (ACCENT, ACCENT_DEEP)