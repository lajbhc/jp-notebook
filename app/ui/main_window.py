"""主窗口：左侧导航 + 右侧页面容器。

「录入」「词库」「手机上传」已经是真页面，打卡先放占位说明——宁可显示
"这一页还没做"，也不做一个看着能点、点了没反应的假界面。

用 QStackedWidget + QListWidget 而不是 QTabWidget：手机上传要放一整块二维码、
阶段 6 的日历也要占满整块区域，左侧导航比顶部标签更省空间。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.ui.capture_page import CapturePage
from app.ui.library_page import LibraryPage
from app.ui.upload_page import UploadPage

WINDOW_TITLE = "日语错题本"
WINDOW_SIZE = (1000, 720)


def _placeholder_page(title: str, detail: str) -> QWidget:
    """还没实现的页面：说清楚它是谁、什么时候来，而不是留一片空白。"""
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(32, 32, 32, 32)
    layout.setSpacing(10)

    heading = QLabel(title)
    heading.setStyleSheet("font-size: 17px; font-weight: 600;")
    layout.addWidget(heading)

    note = QLabel(detail)
    note.setWordWrap(True)
    note.setStyleSheet("font-size: 13px; color: #777;")
    layout.addWidget(note)
    layout.addStretch(1)
    return page


class MainWindow(QMainWindow):
    def __init__(self, settings: config.Settings | None = None) -> None:
        super().__init__()
        self._settings = settings or config.get_settings()

        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        banner = self._build_config_banner()
        if banner is not None:
            outer.addWidget(banner)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_nav())
        body.addWidget(self._build_stack(), 1)
        outer.addLayout(body, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage(f"数据库：{config.DB_PATH}")
        # setCurrentRow(0) 在建栈之前调用，那时信号还没接上，这里补一次同步标题
        self._on_nav_changed(self._nav.currentRow())

    # ------------------------------------------------------------- 构建
    def _build_config_banner(self) -> QLabel | None:
        if self._settings.recognition_ready:
            return None
        missing = "、".join(self._settings.missing_required)
        label = QLabel(
            f"未检测到 {missing}，识别功能暂不可用（仍可手动录入）。"
            f"请在项目根目录的 .env 里填写后重启程序，可参考 .env.example。"
        )
        label.setWordWrap(True)
        label.setStyleSheet(
            "padding: 10px 16px; border-bottom: 1px solid #e0b252;"
            "background: #fdf6e3; color: #7a5a00; font-size: 12px;"
        )
        return label

    def _build_nav(self) -> QWidget:
        self._nav = QListWidget()
        self._nav.setFixedWidth(132)
        self._nav.setStyleSheet(
            "QListWidget { border: none; border-right: 1px solid #e6e6e6; padding-top: 8px; }"
            "QListWidget::item { padding: 10px 14px; }"
        )
        for title in ("录入", "词库", "打卡", "手机上传"):
            QListWidgetItem(title, self._nav)
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        return self._nav

    def _build_stack(self) -> QWidget:
        self._stack = QStackedWidget()

        self.capture_page = CapturePage()
        self.capture_page.entry_saved.connect(self._on_entry_saved)

        self.library_page = LibraryPage()
        self.library_page.message.connect(self._show_status)

        self.upload_page = UploadPage()
        self.upload_page.message.connect(self._show_status)
        self.upload_page.image_chosen.connect(self._on_image_from_phone)

        self._stack.addWidget(self.capture_page)
        self._stack.addWidget(self.library_page)
        self._stack.addWidget(
            _placeholder_page("打卡", "阶段 6 接入：每天点一次「今日学习完成」，显示连续天数与月历。")
        )
        self._stack.addWidget(self.upload_page)
        return self._stack

    # ------------------------------------------------------------- 事件
    def _on_nav_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self.setWindowTitle(f"{WINDOW_TITLE} · {self._nav.currentItem().text()}")
        # 切到词库时重查一次：别的页面可能刚存/删过记录
        if index == 1:
            self.library_page.reload()

    def _on_entry_saved(self, entry) -> None:
        self._show_status(f"已保存：{entry.title}（id={entry.id}）")
        # 新记录按创建时间倒序，会落在第一页最前面，所以回到第一页
        self.library_page.reload(reset_page=True)

    def _on_image_from_phone(self, path: str) -> None:
        """手机上刚收到的那张图，直接当成"已选好的图片"塞进录入页。"""
        self.capture_page.load_image(path)
        self._nav.setCurrentRow(0)
        self._show_status("已把手机传来的图片放进录入页，接下来照常识别或手填。")

    def _show_status(self, text: str) -> None:
        self.statusBar().showMessage(text, 5000)


__all__ = ["MainWindow"]