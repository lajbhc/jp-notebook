"""提醒小窗：无边框、置顶、右下角，两个按钮。

只做"显示 + 抛信号"，不判断该不该提醒（那是 services/reminder_service.py），
也不自己定时（定时器在 app/main.py）。这样这个窗口在测试里可以直接
`show_reminder(status)` 然后点按钮验证信号。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_TITLE = "该学日语了"
_CARD_STYLE = """
#card {
    background: #ffffff;
    border: 1px solid #dfe4ea;
    border-radius: 12px;
}
#title { font-size: 16px; font-weight: bold; }
#streak { font-size: 22px; font-weight: bold; }
#detail { font-size: 12px; }
#close {
    border: none;
    font-size: 16px;
    padding: 0px 6px;
}
#close:hover { background: #eef1f5; border-radius: 4px; }
#primary {
    padding: 8px 14px;
    border: none;
    border-radius: 6px;
    background: #2f6f9f;
    color: #ffffff;
    font-weight: bold;
}
#primary:hover { background: #28608b; }
#secondary {
    padding: 8px 14px;
    border: 1px solid #cfd6de;
    border-radius: 6px;
    background: #f7f9fb;
}
#secondary:hover { background: #eef1f5; }
"""


class ReminderPopup(QWidget):
    """右下角的提醒小窗。"""

    checkin_requested = Signal()
    snooze_requested = Signal()
    dismissed = Signal()

    WIDTH = 340

    def __init__(self, parent: QWidget | None = None, *, snooze_minutes: int = 10) -> None:
        super().__init__(parent)
        self._snooze_minutes = snooze_minutes
        self._drag_from: QPoint | None = None

        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(self.WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(_CARD_STYLE)
        outer.addWidget(card)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)
        self._card = card

        box = QVBoxLayout(card)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.title_label = QLabel(_TITLE)
        self.title_label.setObjectName("title")
        head.addWidget(self.title_label, 1)
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("close")
        self.close_button.setToolTip("今天先不提醒了")
        self.close_button.setFixedSize(24, 24)
        self.close_button.clicked.connect(self._on_dismiss)
        head.addWidget(self.close_button, 0, Qt.AlignTop)
        box.addLayout(head)

        self.streak_label = QLabel("今天还没打卡")
        self.streak_label.setObjectName("streak")
        box.addWidget(self.streak_label)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("detail")
        self.detail_label.setWordWrap(True)
        box.addWidget(self.detail_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.checkin_button = QPushButton("现在去打卡")
        self.checkin_button.setObjectName("primary")
        self.checkin_button.clicked.connect(self._on_checkin)
        buttons.addWidget(self.checkin_button, 1)
        self.snooze_button = QPushButton(f"{snooze_minutes} 分钟后再提醒")
        self.snooze_button.setObjectName("secondary")
        self.snooze_button.clicked.connect(self._on_snooze)
        buttons.addWidget(self.snooze_button, 1)
        box.addLayout(buttons)

    # ---- 对外 --------------------------------------------------------

    def show_reminder(self, status=None) -> None:
        """弹出来。`status` 是 checkin_service.Status，传 None 也能显示。"""
        if status is None:
            self.streak_label.setText("今天还没打卡")
            self.detail_label.setText("打开程序点一下「打卡」就算今天学过。")
        else:
            self.streak_label.setText(status.streak_text)
            text = status.progress_text
            if not status.checked_today:
                text = f"{status.today_text}　·　{text}"
            self.detail_label.setText(text)
        self.set_snooze_minutes(self._snooze_minutes)
        self.adjustSize()
        self._place_bottom_right()
        self.show()
        # 只抬到最上层，不抢键盘焦点：提醒弹出来时用户可能正在别的窗口打字
        self.raise_()

    def set_snooze_minutes(self, minutes: int) -> None:
        self._snooze_minutes = max(1, int(minutes))
        self.snooze_button.setText(f"{self._snooze_minutes} 分钟后再提醒")

    # ---- 内部 --------------------------------------------------------

    def _place_bottom_right(self) -> None:
        screen = self.screen() or self.windowHandle()
        if screen is not None and hasattr(screen, "availableGeometry"):
            area = screen.availableGeometry()
        else:
            from PySide6.QtGui import QGuiApplication

            app_screen = QGuiApplication.primaryScreen()
            if app_screen is None:
                return
            area = app_screen.availableGeometry()
        margin = 24
        self.move(
            area.x() + area.width() - self.width() - margin,
            area.y() + area.height() - self.height() - margin,
        )

    def _on_checkin(self) -> None:
        self.hide()
        self.checkin_requested.emit()

    def _on_snooze(self) -> None:
        self.hide()
        self.snooze_requested.emit()

    def _on_dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()

    # ---- 无边框窗口的拖动 --------------------------------------------

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        self._drag_from = None
        super().mouseReleaseEvent(event)