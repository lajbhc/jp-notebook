"""词库表格的数据模型。

只做展示：数据从 `library_service.search_entries()` 拿来，一页一批。
不在客户端排序——排序会让"这是当前页排序还是全库排序"变得含糊，
需要排序时应该走 SQL 的 ORDER BY，那是阶段 8 之后的事。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from app import models

HEADERS: tuple[str, ...] = ("类型", "词形 / 语法点", "读音 / 接续", "释义", "例句", "创建时间")

# 建议列宽（像素）。第 3、4 列会被视图拉伸占满剩余空间。
COLUMN_WIDTHS: tuple[int, ...] = (58, 150, 120, 180, 200, 132)

# 类型列的颜色：两类记录一眼区分，比加图标省事
_TYPE_COLORS = {
    models.ENTRY_TYPE_WORD: QColor("#2f6f9f"),
    models.ENTRY_TYPE_GRAMMAR: QColor("#8a5a9e"),
}


def _secondary_text(entry: models.Entry) -> str:
    """生词给读音，语法给接续。表格里这一列两类共用。"""
    if entry.type == models.ENTRY_TYPE_WORD:
        return entry.reading
    return entry.connection


def _single_line(text: str) -> str:
    """表格里不换行，换行符会让行高忽高忽低。完整内容走 tooltip。"""
    return " ".join(text.split()) if text else ""


class EntryTableModel(QAbstractTableModel):
    """一页记录的只读表格模型。"""

    def __init__(self, entries: tuple[models.Entry, ...] | list[models.Entry] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._entries: list[models.Entry] = list(entries or ())

    # ------------------------------------------------------------- Qt 接口
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(HEADERS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        if 0 <= section < len(HEADERS):
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self.entry_at(index.row())
        if entry is None:
            return None

        column = index.column()
        if role == Qt.DisplayRole:
            return self._display_text(entry, column)
        if role == Qt.ToolTipRole:
            # 表格列宽有限，一定要能看全文
            full = self._display_text(entry, column)
            if column == 3 and entry.example:
                return f"{entry.meaning}\n例句：{entry.example}"
            if column == 0:
                return entry.type_label
            return full or None
        if role == Qt.ForegroundRole and column == 0:
            return _TYPE_COLORS.get(entry.type, QColor("#444"))
        if role == Qt.TextAlignmentRole and column == 0:
            return int(Qt.AlignCenter)
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    # ------------------------------------------------------------- 业务
    def entry_at(self, row: int) -> models.Entry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def entries(self) -> tuple[models.Entry, ...]:
        return tuple(self._entries)

    def set_entries(self, entries: tuple[models.Entry, ...] | list[models.Entry]) -> None:
        """整页替换。一页数据一次性换掉，所以用 reset 而不是逐个 insertRows。"""
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    # ------------------------------------------------------------- 内部
    @staticmethod
    def _display_text(entry: models.Entry, column: int) -> str:
        if column == 0:
            return entry.type_label
        if column == 1:
            return _single_line(entry.title)
        if column == 2:
            return _single_line(_secondary_text(entry))
        if column == 3:
            return _single_line(entry.meaning)
        if column == 4:
            return _single_line(entry.example)
        if column == 5:
            return entry.created_at
        return ""


__all__ = ["COLUMN_WIDTHS", "HEADERS", "EntryTableModel"]