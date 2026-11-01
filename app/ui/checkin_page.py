"""打卡页：一个大按钮 + 本月日历。

这一页的规则全在 `app/services/checkin_service.py` 里（连续天数怎么算、重复打卡怎么办），
这里只做三件事：把状态显示出来、把点击转成一次服务调用、把日历格子涂上色。

日历不用第三方控件：一个月最多 6 行 x 7 列，用 QGridLayout 摆 QLabel 就够了，
省掉一个依赖，也让"哪一格代表哪天"这件事在代码里是明摆着的。
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services import checkin_service
from app.services.checkin_service import CheckinFailed, Status

_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

_BANNER_STYLES = {
    "error": "border: 1px solid #e0a1a1; background: #fdecec; color: #a12622;",
    "ok": "border: 1px solid #a9d5b0; background: #eef8f0; color: #1f6f36;",
    "info": "border: 1px solid #a8c4e0; background: #eef4fb; color: #1f4f7a;",
}

_CELL_BASE = "min-width: 34px; min-height: 32px; font-size: 12px;"
_CELL_CHECKED = "background: #2f6f9f; color: #ffffff; font-weight: bold; border-radius: 4px;"
_CELL_TODAY = "border: 2px solid #2f6f9f; border-radius: 4px; font-weight: bold;"


class CheckinPage(QWidget):
    """每日打卡页。`message` 给状态栏用，`checked_in` 给托盘/提醒层用。"""

    message = Signal(str)
    # 打卡成功后带上最新的 Status，外层据此更新托盘提示、抑制今晚的提醒
    checked_in = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        today = date.today()
        self._year = today.year
        self._month = today.month
        self._status: Status | None = None
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------- 界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        title = QLabel("每日打卡")
        title.setStyleSheet("font-size: 17px; font-weight: bold;")
        root.addWidget(title)

        hint = QLabel("学完一点就来点一下。断了不丢人，重点是第二天还回来。")
        hint.setStyleSheet("font-size: 12px; color: #777;")
        root.addWidget(hint)

        root.addLayout(self._build_summary())
        root.addLayout(self._build_action())

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.hide()
        root.addWidget(self.banner)

        root.addLayout(self._build_calendar())
        root.addStretch(1)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("font-size: 12px; color: #666;")
        root.addWidget(self.progress_label)

    def _build_summary(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.streak_label = QLabel("0")
        self.streak_label.setStyleSheet("font-size: 34px; font-weight: bold; color: #2f6f9f;")
        # 34px 的字需要四十来像素才不会被切掉顶部，给个下限，别指望布局算准
        self.streak_label.setMinimumHeight(44)
        row.addWidget(self.streak_label, 0, Qt.AlignBottom)

        side = QVBoxLayout()
        side.setSpacing(0)
        self.streak_text_label = QLabel("还没有连续记录")
        self.streak_text_label.setStyleSheet("font-size: 13px; color: #444;")
        self.today_label = QLabel("今天还没打卡")
        self.today_label.setStyleSheet("font-size: 12px; color: #888;")
        side.addWidget(self.streak_text_label)
        side.addWidget(self.today_label)
        row.addLayout(side)
        row.addStretch(1)
        return row

    def _build_action(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("今天学了什么（可不填）")
        self.note_edit.setClearButtonEnabled(True)
        self.note_edit.setMinimumHeight(38)  # 和右边的按钮差不多高，看着才是一行
        row.addWidget(self.note_edit, 1)

        self.check_button = QPushButton("今日学习完成")
        self.check_button.setMinimumHeight(40)
        self.check_button.setMinimumWidth(150)
        self.check_button.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.check_button.clicked.connect(self._check)
        row.addWidget(self.check_button)
        return row

    def _build_calendar(self) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(6)

        head = QHBoxLayout()
        self.prev_button = QPushButton("‹")
        self.prev_button.setFixedWidth(32)
        self.prev_button.setToolTip("上个月")
        self.prev_button.clicked.connect(lambda: self._shift_month(-1))
        self.next_button = QPushButton("›")
        self.next_button.setFixedWidth(32)
        self.next_button.setToolTip("下个月")
        self.next_button.clicked.connect(lambda: self._shift_month(1))

        self.month_label = QLabel("")
        self.month_label.setStyleSheet("font-size: 14px; font-weight: bold;")

        head.addWidget(self.prev_button)
        head.addWidget(self.month_label)
        head.addWidget(self.next_button)
        head.addStretch(1)
        box.addLayout(head)

        weekdays = QHBoxLayout()
        weekdays.setSpacing(4)
        for name in _WEEKDAYS:
            label = QLabel(name)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("font-size: 11px; color: #888;")
            weekdays.addWidget(label)
        box.addLayout(weekdays)

        self.grid = QGridLayout()
        self.grid.setSpacing(4)
        box.addLayout(self.grid)
        return box

    # ------------------------------------------------------------- 动作
    def refresh(self) -> None:
        """重读一遍状态并重画。切到本页、跨过零点、打卡之后都调它。"""
        try:
            self._status = checkin_service.status()
            grid = checkin_service.month_grid(self._year, self._month)
        except CheckinFailed as exc:
            print(f"[checkin] {exc.detail}", flush=True)
            self._show_banner(exc.user_message, "error")
            return

        status = self._status
        self.streak_label.setText(str(status.streak))
        self.streak_text_label.setText(status.streak_text)
        self.today_label.setText(status.today_text)
        self.progress_label.setText(status.progress_text)

        checked = status.checked_today
        self.check_button.setText("今日已打卡" if checked else "今日学习完成")
        self.check_button.setEnabled(not checked)
        self.note_edit.setEnabled(not checked)
        if checked:
            self.note_edit.setPlaceholderText("今天的打卡已经记下了")

        self._draw_calendar(grid, status)

    def _check(self) -> None:
        note = self.note_edit.text()
        try:
            status, newly = checkin_service.check_today(note)
        except CheckinFailed as exc:
            print(f"[checkin] {exc.detail}", flush=True)
            self._show_banner(exc.user_message, "error")
            return

        self._show_banner(
            f"记下了，{status.streak_text}。明天再来。" if newly else "今天已经打过卡了，不用重复点。",
            "ok" if newly else "info",
        )
        self.note_edit.clear()
        self.refresh()
        self.message.emit(f"{status.streak_text} 累计 {status.total} 天" if newly else "今天已经打过卡了")
        self.checked_in.emit(status)

    def _shift_month(self, delta: int) -> None:
        month = self._month + delta
        year = self._year
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        self._year, self._month = year, month
        self.refresh()

    # ------------------------------------------------------------- 内部
    def _draw_calendar(self, grid: list[list[checkin_service.DayCell]], status: Status) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        checked_count = 0
        for row_index, week in enumerate(grid):
            for column_index, cell in enumerate(week):
                label = QLabel(cell.text)
                label.setAlignment(Qt.AlignCenter)
                if cell.empty:
                    label.setStyleSheet(f"{_CELL_BASE} color: transparent;")
                else:
                    parts = [_CELL_BASE]
                    if cell.checked:
                        parts.append(_CELL_CHECKED)
                        checked_count += 1
                    elif cell.is_today:
                        parts.append(_CELL_TODAY)
                    label.setStyleSheet(" ".join(parts))
                    if cell.is_today:
                        label.setToolTip("今天")
                    elif cell.checked:
                        label.setToolTip(f"{cell.iso} 已打卡")
                self.grid.addWidget(label, row_index, column_index)

        self.month_label.setText(
            f"{self._year} 年 {self._month} 月 · 打卡 {checked_count} 天"
        )
        if status.checked_today and (self._year, self._month) == (date.today().year, date.today().month):
            self.month_label.setText(self.month_label.text() + "（含今天）")

    def _show_banner(self, text: str, level: str = "info") -> None:
        style = _BANNER_STYLES.get(level, _BANNER_STYLES["info"])
        self.banner.setStyleSheet(f"{style} padding: 6px 10px; font-size: 12px;")
        self.banner.setText(text)
        self.banner.show()

    # ------------------------------------------------------------- 生命周期
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # 切回本页、或者程序挂了一夜跨过零点后再点进来，都应该是新的状态
        today = date.today()
        self._year, self._month = today.year, today.month
        self.refresh()