"""词库页：列表 + 关键词/类型/日期筛选 + 分页 + 编辑 + 删除。

分工：
- 查询与删除走 `library_service`（它再往下找 repository）
- 表格只负责显示，见 `entry_table_model.py`
- 编辑走 `EntryEditDialog`，保存规则复用 `entry_service.save_entry`

这一页不缓存全库数据：只持有当前页的几十条。500 条以内这么做毫无压力，
而且"列表和库里不一致"这类问题从根上就不会出现。
"""

from __future__ import annotations


from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app import models
from app.logger import get_logger
from app.services import library_service, maintenance_service
from app.services.library_service import FilterInvalid, LibraryFailed
from app.ui.entry_edit_dialog import EntryEditDialog
from app.ui.entry_table_model import COLUMN_WIDTHS, EntryTableModel
from app.ui import style, window_state

_DATE_FORMAT = "yyyy-MM-dd"


class LibraryPage(QWidget):
    """词库页。`message` 给状态栏用。"""

    message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._page = 1
        self._page_size = library_service.DEFAULT_PAGE_SIZE
        self._filling = False
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------- 列宽
    def _restored_widths(self) -> list[int]:
        """上次拖好的列宽；没存过或存坏了就用默认值。"""
        saved = window_state.load_library_columns()
        widths = list(COLUMN_WIDTHS)
        if saved is None:
            return widths
        for index, width in enumerate(saved[: len(widths)]):
            widths[index] = width
        return widths

    def _on_section_resized(self, *_args: object) -> None:
        if self._filling:
            return  # 自动补宽不算用户拖动，别把补出来的宽度写进配置
        self._column_timer.start()

    def _persist_widths(self) -> None:
        widths = [self.table.columnWidth(i) for i in range(len(COLUMN_WIDTHS))]
        window_state.save_library_columns(widths)

    def _fill_extra_width(self) -> None:
        """列宽总和小于表格宽度时，多出来的都给释义列，右边不留一条空白。"""
        self._filling = True
        try:
            total = sum(self.table.columnWidth(i) for i in range(len(COLUMN_WIDTHS)))
            extra = self.table.viewport().width() - total
            if extra > 0:
                self.table.setColumnWidth(3, self.table.columnWidth(3) + extra)
        finally:
            self._filling = False

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        super().resizeEvent(event)
        self._fill_extra_width()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        super().showEvent(event)
        self._fill_extra_width()

    # ------------------------------------------------------------- 界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)

        root.addLayout(self._build_header())
        root.addLayout(self._build_filters())

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(style.field_error_qss(compact=True))
        self.error_label.hide()
        root.addWidget(self.error_label)

        root.addWidget(self._build_table_area(), 1)
        root.addLayout(self._build_pager())

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        title = QLabel("词库")
        title.setStyleSheet(style.TITLE)
        row.addWidget(title)

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet(style.HINT_DIM)
        row.addWidget(self.stats_label)
        row.addStretch(1)

        self.cleanup_button = QPushButton("整理图片")
        self.cleanup_button.setMinimumWidth(88)
        self.cleanup_button.setToolTip(
            "看看有没有删记录时留下的孤儿图片。只会删没有任何记录引用的图。"
        )
        self.cleanup_button.clicked.connect(self._cleanup_images)
        row.addWidget(self.cleanup_button)

        self.edit_button = QPushButton("编辑")
        self.edit_button.setMinimumWidth(72)
        self.edit_button.clicked.connect(self._edit_selected)
        row.addWidget(self.edit_button)

        self.delete_button = QPushButton("删除")
        self.delete_button.setMinimumWidth(72)
        self.delete_button.clicked.connect(self._delete_selected)
        row.addWidget(self.delete_button)
        return row

    def _build_filters(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("搜词形、读音、释义、例句、错因…")
        self.keyword_edit.setClearButtonEnabled(True)
        self.keyword_edit.setMinimumWidth(240)
        # 输完直接回车就搜，不用去点按钮
        self.keyword_edit.returnPressed.connect(self._on_search)
        row.addWidget(self.keyword_edit, 1)

        self.type_combo = QComboBox()
        self.type_combo.addItem("全部类型", None)
        for entry_type in models.ENTRY_TYPES:
            self.type_combo.addItem(models.TYPE_LABELS[entry_type], entry_type)
        self.type_combo.setMinimumWidth(96)
        self.type_combo.currentIndexChanged.connect(lambda _index: self.reload(reset_page=True))
        row.addWidget(self.type_combo)

        self.date_check = QCheckBox("按日期")
        self.date_check.setToolTip("只按创建日期筛选")
        self.date_check.toggled.connect(self._on_date_toggled)
        row.addWidget(self.date_check)

        self.date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        for editor in (self.date_from, self.date_to):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat(_DATE_FORMAT)
            editor.setEnabled(False)
            editor.setMinimumWidth(104)
            editor.dateChanged.connect(lambda _date: self._on_date_changed())
        row.addWidget(self.date_from)

        dash = QLabel("—")
        dash.setStyleSheet("color: #999;")
        row.addWidget(dash)
        row.addWidget(self.date_to)

        self.search_button = QPushButton("搜索")
        self.search_button.setMinimumWidth(64)
        self.search_button.clicked.connect(self._on_search)
        row.addWidget(self.search_button)

        self.reset_button = QPushButton("重置")
        self.reset_button.setMinimumWidth(64)
        self.reset_button.clicked.connect(self.reset_filters)
        row.addWidget(self.reset_button)
        return row

    def _build_table_area(self) -> QWidget:
        self.model = EntryTableModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.doubleClicked.connect(lambda _index: self._edit_selected())

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)  # 六列都能拖，宽度存进 ui.json
        header.setStretchLastSection(False)
        widths = self._restored_widths()
        for column, width in enumerate(widths):
            self.table.setColumnWidth(column, width)
        header.sectionResized.connect(self._on_section_resized)
        self._column_timer = QTimer(self)
        self._column_timer.setSingleShot(True)
        self._column_timer.setInterval(400)
        self._column_timer.timeout.connect(self._persist_widths)

        # 选中变化要立刻反映到「编辑 / 删除」按钮的可用状态
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._sync_buttons())

        # Delete 键删除当前行（仍然会二次确认）。要留引用，否则会被回收掉。
        self._delete_shortcut = QShortcut(QKeySequence("Delete"), self.table)
        self._delete_shortcut.activated.connect(self._delete_selected)

        self.empty_label = QLabel("")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(style.EMPTY)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty_label)
        return self.stack

    def _build_pager(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.range_label = QLabel("")
        self.range_label.setStyleSheet("font-size: 12px; color: #666;")
        row.addWidget(self.range_label)
        row.addStretch(1)

        row.addWidget(QLabel("每页"))
        self.page_size_combo = QComboBox()
        for size in library_service.PAGE_SIZE_OPTIONS:
            self.page_size_combo.addItem(str(size), size)
        self.page_size_combo.setCurrentIndex(
            library_service.PAGE_SIZE_OPTIONS.index(self._page_size)
        )
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        row.addWidget(self.page_size_combo)

        self.prev_button = QPushButton("上一页")
        self.prev_button.setMinimumWidth(76)
        self.prev_button.clicked.connect(lambda: self._go_page(self._page - 1))
        row.addWidget(self.prev_button)

        self.page_label = QLabel("第 1 / 1 页")
        self.page_label.setStyleSheet("font-size: 12px; color: #666;")
        self.page_label.setMinimumWidth(78)
        self.page_label.setAlignment(Qt.AlignCenter)
        row.addWidget(self.page_label)

        self.next_button = QPushButton("下一页")
        self.next_button.setMinimumWidth(76)
        self.next_button.clicked.connect(lambda: self._go_page(self._page + 1))
        row.addWidget(self.next_button)
        return row

    # ------------------------------------------------------------- 查询
    def filters(self) -> dict[str, object]:
        """当前筛选条件。日期没勾选时传 None，而不是传个"今天"。"""
        checked = self.date_check.isChecked()
        return {
            "keyword": self.keyword_edit.text(),
            "entry_type": self.type_combo.currentData(),
            "date_from": self.date_from.date().toString(_DATE_FORMAT) if checked else None,
            "date_to": self.date_to.date().toString(_DATE_FORMAT) if checked else None,
        }

    def reload(self, *, reset_page: bool = False) -> None:
        """按当前条件重新查一页。"""
        if reset_page:
            self._page = 1
        self._clear_error()

        try:
            page = library_service.search_entries(
                page=self._page, page_size=self._page_size, **self.filters()
            )
        except FilterInvalid as exc:
            self._show_error(exc.user_message)
            return
        except LibraryFailed as exc:
            get_logger("ui.library").warning("词库操作失败：%s", exc.detail)
            self._show_error(exc.user_message)
            return

        self._page = page.page
        self.model.set_entries(page.items)
        self.stack.setCurrentIndex(0 if page.items else 1)

        self._refresh_stats()
        self.empty_label.setText(self._empty_text(bool(page.items)))

        self.range_label.setText(page.range_text)
        self.page_label.setText(page.page_text)
        self.prev_button.setEnabled(page.has_prev)
        self.next_button.setEnabled(page.has_next)

        self._sync_buttons()

    def _refresh_stats(self) -> None:
        try:
            self.stats_label.setText(library_service.stats().text())
        except LibraryFailed as exc:
            get_logger("ui.library").warning("词库操作失败：%s", exc.detail)
            self.stats_label.setText("统计不可用")

    def _empty_text(self, has_rows: bool) -> str:
        if has_rows:
            return ""
        stats_known = self.stats_label.text()
        if "共 0 条" in stats_known:
            return "词库还是空的。去左侧「录入」页拍照或选图，存下第一条记录。"
        return "没有符合条件的记录。试试换个关键词，或者点「重置」。"

    # ------------------------------------------------------------- 交互
    def _on_search(self) -> None:
        self.reload(reset_page=True)

    def _on_date_toggled(self, checked: bool) -> None:
        self.date_from.setEnabled(checked)
        self.date_to.setEnabled(checked)
        self.reload(reset_page=True)

    def _on_date_changed(self) -> None:
        # 勾选日期时 setDate 也会触发这里，所以只在启用状态下才重查
        if self.date_check.isChecked():
            self.reload(reset_page=True)

    def _on_page_size_changed(self) -> None:
        size = self.page_size_combo.currentData()
        if size:
            self._page_size = int(size)
            self.reload(reset_page=True)

    def _go_page(self, page: int) -> None:
        self._page = max(1, page)
        self.reload()

    def reset_filters(self) -> None:
        self.keyword_edit.clear()
        self.type_combo.setCurrentIndex(0)
        self.date_check.setChecked(False)
        self.reload(reset_page=True)

    def current_entry(self) -> models.Entry | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.model.entry_at(rows[0].row())

    def select_entry(self, entry_id: int | None) -> bool:
        """在当前页里选中指定记录。编辑保存后用来把光标放回原位。"""
        if entry_id is None:
            return False
        for row, entry in enumerate(self.model.entries()):
            if entry.id == entry_id:
                self.table.selectRow(row)
                return True
        return False

    def _sync_buttons(self) -> None:
        has_selection = self.current_entry() is not None
        self.edit_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

    def _edit_selected(self) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        dialog = EntryEditDialog(entry, self)
        if dialog.exec() != QDialog.Accepted or dialog.saved_entry is None:
            return
        self.message.emit(f"已更新：{dialog.saved_entry.title}")
        self.reload()
        self.select_entry(dialog.saved_entry.id)

    def _delete_selected(self) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        answer = QMessageBox.question(
            self,
            "删除记录",
            f"确定要删除「{entry.title}」吗？\n\n删除后无法恢复（原图仍保留在 data/uploads 里）。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            deleted = library_service.delete_entry(entry.id)
        except LibraryFailed as exc:
            get_logger("ui.library").warning("词库操作失败：%s", exc.detail)
            self._show_error(exc.user_message)
            return

        if deleted:
            self.message.emit(f"已删除：{entry.title}")
        else:
            self.message.emit("这条记录已经不存在了，列表已刷新")
        self.reload()

    def _cleanup_images(self) -> None:
        """看一眼上传目录，只删"没有任何记录引用"的图。

        删记录时故意保留原图，所以这些图迟早会攒起来。这里先算清楚账再问一遍，
        绝不静默删除。
        """
        self._clear_error()
        try:
            report = maintenance_service.scan()
        except maintenance_service.MaintenanceFailed as exc:
            get_logger("ui.library").warning("整理图片失败：%s", exc.detail)
            self._show_error(exc.user_message)
            return

        if not report.orphans:
            self.message.emit(f"图片都很干净：{report.summary()}")
            return

        answer = QMessageBox.question(
            self,
            "整理图片",
            f"{report.summary()}\n\n要删掉这 {len(report.orphans)} 张没被引用的图片吗？\n"
            "删除后不可恢复；记录里正在用的图不会动。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            result = maintenance_service.delete_orphans(
                [item.relative_path for item in report.orphans]
            )
        except maintenance_service.MaintenanceFailed as exc:
            get_logger("ui.library").warning("删除孤儿图片失败：%s", exc.detail)
            self._show_error(exc.user_message)
            return

        self.message.emit(result.summary())

    # ------------------------------------------------------------- 提示
    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.show()

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()


__all__ = ["LibraryPage"]