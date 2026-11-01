"""系统托盘：图标 + 菜单（打开 / 去打卡 / 退出）。

托盘只为"让程序能悄悄待在后台"服务，所有按钮都只发信号，具体做什么交给
`app/main.py` 里的 AppShell。没有托盘的环境（部分 Linux、offscreen 测试）
不该让程序崩：`Tray.available()` 为 False 时调用方就不要开"关窗最小化"。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

_SNOOZE_TIP = "关掉窗口不影响打卡和手机上传，点托盘图标可以再打开。"


def make_icon() -> QIcon:
    """画一个蓝底「语」字的图标，省得额外放一张图片进仓库。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2f6f9f"))
    painter.drawEllipse(2, 2, 60, 60)
    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setPixelSize(34)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "语")
    painter.end()
    return QIcon(pixmap)


class Tray(QObject):
    """托盘图标。信号：open_requested / checkin_requested / quit_requested。"""

    open_requested = Signal()
    checkin_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._available = QSystemTrayIcon.isSystemTrayAvailable()

        self.icon = QSystemTrayIcon(make_icon(), self)
        self.icon.setToolTip("日语错题本")

        self.menu = QMenu()
        self.open_action = self.menu.addAction("打开主窗口")
        self.open_action.triggered.connect(self._on_open)
        self.menu.addSeparator()
        self.checkin_action = self.menu.addAction("去打卡")
        self.checkin_action.triggered.connect(self._on_checkin)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("退出")
        self.quit_action.triggered.connect(self._on_quit)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)

        if self._available:
            self.icon.show()

    # ---- 只读 --------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    @property
    def is_visible(self) -> bool:
        return self.icon.isVisible()

    @property
    def checked_today(self) -> bool:
        return not self.checkin_action.isEnabled()

    # ---- 对外 --------------------------------------------------------

    def set_checked_today(self, checked: bool) -> None:
        """今天打过卡就把菜单项变成一句确认，并且点不动。"""
        if checked:
            self.checkin_action.setText("今天已打卡 ✓")
            self.checkin_action.setEnabled(False)
        else:
            self.checkin_action.setText("去打卡")
            self.checkin_action.setEnabled(True)

    def set_summary(self, text: str) -> None:
        """托盘悬浮提示，一行字就够了。"""
        self.icon.setToolTip(text or "日语错题本")

    def show_message(self, title: str, body: str = "", *, msecs: int = 5000) -> bool:
        """气泡提示。没有托盘或系统不支持时返回 False，不抛异常。"""
        if not self._available or not self.icon.supportsMessages():
            return False
        self.icon.showMessage(title, body or _SNOOZE_TIP, self.icon.icon(), msecs)
        return True

    def hide(self) -> None:
        self.icon.hide()

    def close(self) -> None:
        self.icon.hide()
        self.menu.close()

    # ---- 内部 --------------------------------------------------------

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_requested.emit()

    def _on_open(self) -> None:
        self.open_requested.emit()

    def _on_checkin(self) -> None:
        if self.checkin_action.isEnabled():
            self.checkin_requested.emit()

    def _on_quit(self) -> None:
        self.quit_requested.emit()