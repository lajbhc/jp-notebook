"""编辑一条已有记录。

为什么要单独一个对话框，而不是复用录入页：
录入页是"选图 → 识别 → 校正"的长流程，打开它只是为了改一个错字太重了。
编辑对话框只做一件事——把 10 个字段摆出来，改完就地保存。

保存仍然走 `entry_service.save_entry()`：校验、清对侧字段、刷新 updated_at
的规则和录入页完全一致，不在这里重写一遍。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app import models
from app.services import entry_service
from app.services.entry_service import EntryValidationError, SaveFailed
from app.ui import entry_fields

_PREVIEW_SIZE = (168, 126)


class EntryEditDialog(QDialog):
    """编辑对话框。`show_and_save()` 之外不要去碰 `saved_entry`。"""

    def __init__(self, entry: models.Entry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entry = entry
        self.saved_entry: models.Entry | None = None
        self._editors: dict[str, QWidget] = {}

        self.setWindowTitle(f"编辑 · {entry.title or '未命名'}")
        self.setMinimumWidth(720)
        self._build_ui()
        entry_fields.fill(self._editors, entry)
        self._apply_type(entry.type)

    # ------------------------------------------------------------- 界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        root.addLayout(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_form(), 1)
        body.addWidget(self._build_image_panel(), 0)
        root.addLayout(body, 1)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(
            "border: 1px solid #e0a1a1; background: #fdecec; color: #a12622;"
            "padding: 8px 10px; font-size: 12px;"
        )
        self._error.hide()
        root.addWidget(self._error)

        buttons = QDialogButtonBox()
        self._save_button = buttons.addButton("保存修改", QDialogButtonBox.AcceptRole)
        self._save_button.setMinimumWidth(96)
        cancel = buttons.addButton("取消", QDialogButtonBox.RejectRole)
        cancel.setMinimumWidth(72)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        label = QLabel("类型")
        label.setStyleSheet("font-size: 13px; color: #555;")
        row.addWidget(label)

        self._type_group = QButtonGroup(self)
        self._type_buttons: dict[str, QRadioButton] = {}
        for entry_type in models.ENTRY_TYPES:
            button = QRadioButton(models.TYPE_LABELS[entry_type])
            self._type_group.addButton(button)
            self._type_buttons[entry_type] = button
            button.toggled.connect(
                lambda checked, t=entry_type: checked and self._apply_type(t)
            )
            row.addWidget(button)

        row.addStretch(1)

        stamp = QLabel(f"创建 {self._entry.created_at}　修改 {self._entry.updated_at}")
        stamp.setStyleSheet("font-size: 11px; color: #888;")
        row.addWidget(stamp)
        return row

    def _build_form(self) -> QWidget:
        container = QWidget()
        form = QFormLayout(container)
        form.setContentsMargins(0, 0, 4, 0)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        for spec in entry_fields.FIELDS:
            editor = entry_fields.make_editor(spec)
            self._editors[spec.name] = editor
            form.addRow(self._field_label(spec), editor)

        # 字段多，窗口矮时要能滚
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(container)
        scroll.setMinimumHeight(420)
        return scroll

    @staticmethod
    def _field_label(spec: entry_fields.FieldSpec) -> QLabel:
        label = QLabel(spec.label if spec.only_type is None else f"{spec.label} *")
        label.setStyleSheet("font-size: 13px;")
        if spec.name in models.PRIMARY_FIELD.values():
            label.setToolTip("这是主字段，不能为空")
        return label

    def _build_image_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(_PREVIEW_SIZE[0] + 12)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 26, 0, 0)
        column.setSpacing(6)

        caption = QLabel("原图")
        caption.setStyleSheet("font-size: 12px; color: #666;")
        column.addWidget(caption)

        self._preview = QLabel()
        self._preview.setFixedSize(*_PREVIEW_SIZE)
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setStyleSheet(
            "border: 1px dashed #ccc; background: #fafafa; color: #999; font-size: 12px;"
        )
        path = entry_service.absolute_image_path(self._entry.source_image)
        if path is None:
            self._preview.setText("无原图" if not self._entry.source_image else "原图已丢失")
        else:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                self._preview.setText("图片打不开")
            else:
                self._preview.setPixmap(
                    pixmap.scaled(*_PREVIEW_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
        column.addWidget(self._preview)

        if path is not None:
            open_button = QPushButton("用系统看图打开")
            open_button.setStyleSheet("font-size: 12px;")
            open_button.clicked.connect(lambda: self._open_image(path))
            column.addWidget(open_button)

        column.addStretch(1)
        return panel

    # ------------------------------------------------------------- 交互
    def _apply_type(self, entry_type: str) -> None:
        button = self._type_buttons.get(entry_type)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        entry_fields.apply_enabled(self._editors, entry_type)

    def current_type(self) -> str:
        for entry_type, button in self._type_buttons.items():
            if button.isChecked():
                return entry_type
        return models.ENTRY_TYPE_WORD

    @staticmethod
    def _open_image(path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.show()

    def _save(self) -> None:
        entry = entry_fields.read(
            self._editors,
            entry_type=self.current_type(),
            entry_id=self._entry.id,
            source_image=self._entry.source_image,
        )
        try:
            self.saved_entry = entry_service.save_entry(entry)
        except EntryValidationError as exc:
            self._show_error(exc.user_message)
            return
        except SaveFailed as exc:
            print(f"[edit] {exc.detail}", file=sys.stderr)
            self._show_error(exc.user_message)
            return
        self.accept()


__all__ = ["EntryEditDialog"]