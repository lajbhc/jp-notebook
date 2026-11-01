"""带框选功能的图片预览框。

为什么不用 `QLabel` 显示图片了：一张纸上常常有好几道题，整张送去识别时模型容易
挑错条、或者把几条混着抄。让用户拖一个框，只把那一块发过去，又准又省 token。

两个约定写在这里，别的模块别再自己算：

* `selection()` 返回的是**归一化比例** `(x, y, w, h)`，相对原图，与窗口大小、
  缩放比例都无关——服务层要的就是这个，它自己按原图尺寸去裁；
* 框太小（小于 `MIN_SIDE` 像素）当作没框：拖动时手抖点一下很常见，不该让模型
  对着一两条像素瞎猜。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

_HINT_TEXT = "拖动框选要识别的区域 · 右键取消"


class CropView(QWidget):
    """预览图 + 拖拽框选 + 拖文件进来。"""

    file_dropped = Signal(str)
    selection_changed = Signal(object)  # (x, y, w, h) 归一化比例，或 None

    MIN_SIDE = 14  # 小于这么多像素的框视为误触

    def __init__(
        self,
        placeholder: str = "",
        parent: QWidget | None = None,
        *,
        size: tuple[int, int] = (300, 220),
    ) -> None:
        super().__init__(parent)
        self._placeholder = placeholder
        self._image_path: Path | None = None
        self._pixmap: QPixmap | None = None
        self._scaled: QPixmap | None = None
        self._image_rect = QRect()
        self._selection: QRect | None = None
        self._origin: QPoint | None = None
        self._dragging = False

        self.setFixedSize(QSize(*size))
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.CrossCursor)
        self.setToolTip("在这张图上拖动可以框选识别范围；右键取消框选")

    # ------------------------------------------------------------- 对外接口
    @property
    def image_path(self) -> Path | None:
        return self._image_path

    def has_selection(self) -> bool:
        return self._selection is not None

    def selection(self) -> tuple[float, float, float, float] | None:
        """归一化的框选区域；没框选返回 None。"""
        if self._selection is None or self._image_rect.width() <= 0:
            return None
        rect = self._image_rect
        left = (self._selection.left() - rect.left()) / rect.width()
        top = (self._selection.top() - rect.top()) / rect.height()
        width = self._selection.width() / rect.width()
        height = self._selection.height() / rect.height()
        return (
            min(max(left, 0.0), 1.0),
            min(max(top, 0.0), 1.0),
            min(max(width, 0.0), 1.0),
            min(max(height, 0.0), 1.0),
        )

    def selection_text(self) -> str:
        box = self.selection()
        if box is None:
            return ""
        return f"只识别框选区域：宽 {box[2] * 100:.0f}% × 高 {box[3] * 100:.0f}%"

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def set_image(self, path: str | Path) -> bool:
        """载入图片；打不开返回 False（并清空当前图）。

        这里必须用 `QImageReader` 并打开 `autoTransform`：手机竖着拍的照片在 EXIF 里
        写着方向，`QPixmap(path)` 会照着原始像素横着显示，而服务层是先
        `ImageOps.exif_transpose()` 再裁剪的。两边空间不一致，用户框的那块就会错位。
        """
        path = Path(path)
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            self.clear_image()
            return False
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self.clear_image()
            return False
        self._image_path = Path(path)
        self._pixmap = pixmap
        self._selection = None
        self._dragging = False
        self._origin = None
        self._rebuild()
        self.update()
        self.selection_changed.emit(None)
        return True

    def clear_image(self) -> None:
        had_selection = self._selection is not None
        self._image_path = None
        self._pixmap = None
        self._scaled = None
        self._image_rect = QRect()
        self._selection = None
        self._dragging = False
        self._origin = None
        self.update()
        if had_selection:
            self.selection_changed.emit(None)

    def clear_selection(self) -> None:
        if self._selection is None:
            return
        self._selection = None
        self.update()
        self.selection_changed.emit(None)

    # ------------------------------------------------------------- 内部几何
    def _rebuild(self) -> None:
        if self._pixmap is None:
            self._scaled = None
            self._image_rect = QRect()
            return
        self._scaled = self._pixmap.scaled(
            self.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        width = self._scaled.width()
        height = self._scaled.height()
        self._image_rect = QRect(
            (self.width() - width) // 2,
            (self.height() - height) // 2,
            width,
            height,
        )

    def _clamp_to_image(self, point: QPoint) -> QPoint:
        rect = self._image_rect
        return QPoint(
            min(max(point.x(), rect.left()), rect.right()),
            min(max(point.y(), rect.top()), rect.bottom()),
        )

    # ------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        frame = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#d5d5d5"), 1, Qt.DashLine))
        painter.setBrush(QColor("#fbfbfb"))
        painter.drawRoundedRect(frame, 8, 8)

        if self._scaled is None:
            painter.setPen(QColor("#888"))
            font = painter.font()
            font.setPixelSize(12)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder)
            return

        painter.setPen(Qt.NoPen)
        painter.drawPixmap(self._image_rect, self._scaled)

        if self._selection is None:
            self._paint_hint(painter)
        else:
            self._paint_dim(painter)
            self._paint_selection(painter)

    def _paint_dim(self, painter: QPainter) -> None:
        """把框外压暗，一眼能看出"只认这一块"。"""
        area = self._image_rect
        box = self._selection
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 90))
        painter.drawRect(QRect(area.left(), area.top(), area.width(), box.top() - area.top()))
        painter.drawRect(QRect(area.left(), box.bottom() + 1, area.width(), area.bottom() - box.bottom()))
        painter.drawRect(QRect(area.left(), box.top(), box.left() - area.left(), box.height()))
        painter.drawRect(QRect(box.right() + 1, box.top(), area.right() - box.right(), box.height()))

    def _paint_selection(self, painter: QPainter) -> None:
        box = self._selection
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#2f6f9f"), 2))
        painter.drawRect(box)

        painter.setPen(QPen(QColor("#2f6f9f"), 1))
        painter.setBrush(QColor("#ffffff"))
        for corner in (
            QPoint(box.left(), box.top()),
            QPoint(box.right(), box.top()),
            QPoint(box.left(), box.bottom()),
            QPoint(box.right(), box.bottom()),
        ):
            painter.drawRect(QRect(corner.x() - 3, corner.y() - 3, 6, 6))

    def _paint_hint(self, painter: QPainter) -> None:
        band = QRect(
            self._image_rect.left(),
            self._image_rect.bottom() - 22,
            self._image_rect.width(),
            22,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 205))
        painter.drawRect(band)
        painter.setPen(QColor("#555"))
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        painter.drawText(band, Qt.AlignCenter, _HINT_TEXT)

    # ------------------------------------------------------------- 鼠标
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._scaled is None:
            return
        if event.button() == Qt.RightButton:
            self.clear_selection()
            return
        if event.button() != Qt.LeftButton:
            return
        self.setFocus(Qt.MouseFocusReason)
        self._origin = self._clamp_to_image(event.position().toPoint())
        self._selection = QRect(self._origin, QSize(1, 1))
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging or self._origin is None:
            return
        current = self._clamp_to_image(event.position().toPoint())
        self._selection = QRect(self._origin, current).normalized()
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return
        self._dragging = False
        self._origin = None
        box = self._selection
        if box is None or box.width() < self.MIN_SIDE or box.height() < self.MIN_SIDE:
            self._selection = None
            self.update()
            return
        self.update()
        self.selection_changed.emit(self.selection())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.clear_selection()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------- 拖文件
    @staticmethod
    def _first_local_file(event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                return url.toLocalFile()
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._first_local_file(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._first_local_file(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._first_local_file(event)
        if path:
            self.file_dropped.emit(path)
            event.acceptProposedAction()


__all__ = ["CropView"]