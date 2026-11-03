"""放大框选窗口。

录入页上的预览只有 300x220，一页纸上挤着七八条语法笔记时，框只能框个大概。
这里把同一张图铺到接近整个屏幕再拖框，精度高得多。

两点约定：

* 框选结果仍然是**归一化比例**，和小预览完全互通，可以来回传递；
* 这个窗口不碰识别、不碰存库，只负责把"用户想要哪一块"问出来。

关闭方式：Esc 或「取消」= 放弃本次框选（不会改动外面的框）；「识别整张」= 清掉框选；
「用这块识别」= 带回框选结果。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.crop_view import CropView

_OPEN_FAILED = "这张图打不开，关掉窗口重新选一张吧"


class CropDialog(QDialog):
    """把图片放大显示，让用户框得更准。"""

    def __init__(
        self,
        image_path: str | Path,
        selection: tuple[float, float, float, float] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("放大框选")
        self.setModal(True)
        self._placed = False

        self._view = CropView(_OPEN_FAILED, size=self._view_size(parent))
        self._view.selection_changed.connect(self._on_selection_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        tip = QLabel("在小图上框不准的话，可以在这里放大再拖：拖好之后点「用这块识别」。")
        tip.setStyleSheet("font-size: 12px; color: #666;")
        layout.addWidget(tip)
        layout.addWidget(self._view, 0, Qt.AlignCenter)

        self._info = QLabel("")
        self._info.setStyleSheet("font-size: 12px; color: #2f6f9f;")
        layout.addWidget(self._info)

        row = QHBoxLayout()
        self._whole_button = QPushButton("识别整张")
        self._whole_button.setToolTip("清掉框选，整张图送去识别")
        self._whole_button.clicked.connect(self._use_whole)
        self._cancel_button = QPushButton("取消")
        self._cancel_button.clicked.connect(self.reject)
        self._use_button = QPushButton("用这块识别")
        self._use_button.setDefault(True)
        self._use_button.clicked.connect(self.accept)
        row.addWidget(self._whole_button)
        row.addStretch(1)
        row.addWidget(self._cancel_button)
        row.addWidget(self._use_button)
        layout.addLayout(row)

        if self._view.set_image(image_path):
            self._view.set_selection(selection)
        else:
            self._use_button.setEnabled(False)
            self._whole_button.setEnabled(False)
        self._on_selection_changed(self._view.selection())

    # ------------------------------------------------------------- 对外接口
    def selection(self) -> tuple[float, float, float, float] | None:
        return self._view.selection()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        super().showEvent(event)
        if self._placed:
            return
        self._placed = True
        # 窗口比父窗口大得多，默认"居中于父窗口"会顶出屏幕，这里按屏幕居中
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.move(
            available.center().x() - self.width() // 2,
            available.center().y() - self.height() // 2,
        )

    # ------------------------------------------------------------- 内部
    @staticmethod
    def _view_size(parent: QWidget | None) -> tuple[int, int]:
        screen = parent.screen() if parent is not None else None
        screen = screen or QGuiApplication.primaryScreen()
        if screen is None:  # 极端情况：没有屏幕信息，退回到一个够用的固定尺寸
            return (900, 620)
        available = screen.availableGeometry()
        width = max(640, min(1280, int(available.width() * 0.8)))
        height = max(420, min(900, int(available.height() * 0.8) - 120))
        return (width, height)

    def _on_selection_changed(self, selection) -> None:
        has_box = selection is not None
        self._use_button.setEnabled(has_box)
        if has_box:
            self._info.setText(
                f"已框选：宽 {selection[2] * 100:.0f}% × 高 {selection[3] * 100:.0f}"
                " —— 点「用这块识别」带回录入页。"
            )
        else:
            self._info.setText("还没框选 —— 拖动鼠标框一块，或者点「识别整张」。")

    def _use_whole(self) -> None:
        self._view.clear_selection()
        self.accept()


__all__ = ["CropDialog"]