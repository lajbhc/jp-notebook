"""应用入口。

阶段 0 只负责：创建 QApplication、打开一个空主窗口、在配置不全时给出可见提示。
录入页、词库页、打卡页、托盘提醒在后续阶段接入 app/ui/ 下的具体模块。
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget

from app import config

WINDOW_TITLE = "日语错题本"
WINDOW_SIZE = (520, 680)


def _build_main_window(settings: config.Settings) -> QMainWindow:
    window = QMainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    window.resize(*WINDOW_SIZE)

    central = QWidget()
    layout = QVBoxLayout(central)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(14)

    title = QLabel("日语错题本")
    title.setStyleSheet("font-size: 20px; font-weight: 600;")
    layout.addWidget(title)

    subtitle = QLabel("生词 / 语法 拍照录入与学习督促")
    subtitle.setStyleSheet("font-size: 13px; opacity: 0.7;")
    layout.addWidget(subtitle)

    if not settings.recognition_ready:
        missing = "、".join(settings.missing_required)
        banner = QLabel(
            f"未检测到 {missing}，识别功能暂不可用。\n"
            f"请在项目根目录的 .env 中填写后重启程序（可参考 .env.example）。"
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "padding: 10px 12px; border-radius: 6px;"
            "border: 1px solid #e0b252; background: #fdf6e3; color: #7a5a00;"
        )
        layout.addWidget(banner)

    placeholder = QLabel("界面骨架已就绪，录入与词库页面将在下一阶段接入。")
    placeholder.setWordWrap(True)
    placeholder.setStyleSheet("font-size: 13px; opacity: 0.6;")
    layout.addWidget(placeholder)

    layout.addStretch(1)
    window.setCentralWidget(central)
    return window


def main() -> int:
    config.ensure_dirs()
    settings = config.get_settings()

    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)

    window = _build_main_window(settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())