"""录入页：选图 → 识别 → 人工校正 → 保存。

这一页只做界面和交互，不做任何识别/存库细节：
- 识别走 `RecognitionWorker`（子线程），失败只拿 `user_message` 显示
- 保存走 `entry_service.save_entry()`，字段校验也在那一层
- 图片复制进 data/uploads 的动作同样在 entry_service，本页只把选中的路径传下去

识别失败不是死路：界面保留表单，用户可以照着原图手填完再保存。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app import models
from app.services import entry_service
from app.services.entry_service import EntryValidationError, SaveFailed
from app.services.errors import RecognitionError
from app.ui import entry_fields
from app.ui.recognition_worker import RecognitionWorker

_IMAGE_FILTER = "图片 (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff)"

_PREVIEW_SIZE = (300, 220)

_BANNER_STYLES = {
    "error": "border: 1px solid #e0a1a1; background: #fdecec; color: #a12622;",
    "ok": "border: 1px solid #a1d8b2; background: #eaf7ee; color: #1f6b3a;",
    "info": "border: 1px solid #a8c3e8; background: #eef3fb; color: #1f4e79;",
}

# (字段名, 中文标签, 只属于哪一类；None 表示两类共用)
# 字段清单只有 entry_fields.FIELDS 一份，编辑对话框用同一份，避免标签漂移。
_FIELDS: tuple[tuple[str, str, str | None], ...] = tuple(
    (spec.name, spec.label, spec.only_type) for spec in entry_fields.FIELDS
)


class _DropPreview(QLabel):
    """支持把图片拖进来的预览框。拖拽比"点选择图片 → 找目录"快得多。"""

    file_dropped = Signal(str)

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setAcceptDrops(True)

    @staticmethod
    def _first_local_file(event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                return url.toLocalFile()
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
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


class CapturePage(QWidget):
    """录入页。"""

    entry_saved = Signal(object)  # models.Entry，主窗口可以据此刷新列表

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image_path: Path | None = None
        self._worker: RecognitionWorker | None = None
        self._editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        self._build_ui()
        self._apply_type()

    # ------------------------------------------------------------- 构建界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("录入一条记录")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        hint = QLabel("选图 → 识别 → 对照原图改正 → 保存。识别结果只是草稿。")
        hint.setStyleSheet("font-size: 12px; color: #666;")
        root.addWidget(hint)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        root.addLayout(self._build_type_row())

        body = QHBoxLayout()
        body.setSpacing(18)
        body.addLayout(self._build_image_column(), 0)
        body.addLayout(self._build_form_column(), 1)
        root.addLayout(body, 1)

    def _build_type_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(QLabel("类型"))

        self._word_radio = QRadioButton("生词")
        self._grammar_radio = QRadioButton("语法")
        self._word_radio.setChecked(True)

        self._type_group = QButtonGroup(self)
        self._type_group.addButton(self._word_radio)
        self._type_group.addButton(self._grammar_radio)
        self._type_group.buttonToggled.connect(self._on_type_toggled)

        row.addWidget(self._word_radio)
        row.addWidget(self._grammar_radio)
        row.addStretch(1)
        row.addWidget(self._type_hint())
        return row

    def _type_hint(self) -> QLabel:
        self._type_note = QLabel()
        self._type_note.setStyleSheet("font-size: 12px; color: #888;")
        return self._type_note

    def _build_image_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(8)

        self._preview = _DropPreview("还没有选择图片\n\n点「选择图片」，或把图片拖到这里")
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setFixedSize(*_PREVIEW_SIZE)
        self._preview.setStyleSheet(
            "border: 1px dashed #bbb; border-radius: 8px; color: #888; font-size: 12px;"
        )
        self._preview.file_dropped.connect(self._load_image)
        column.addWidget(self._preview)

        self._image_label = QLabel("")
        self._image_label.setWordWrap(True)
        self._image_label.setStyleSheet("font-size: 11px; color: #888;")
        column.addWidget(self._image_label)

        pick = QPushButton("选择图片")
        pick.clicked.connect(self._pick_image)
        column.addWidget(pick)

        self._recognize_button = QPushButton("开始识别")
        self._recognize_button.setEnabled(False)
        self._recognize_button.clicked.connect(self._start_recognition)
        column.addWidget(self._recognize_button)

        clear = QPushButton("清空重来")
        clear.clicked.connect(lambda: self._reset_form(keep_banner=False))
        column.addWidget(clear)

        column.addStretch(1)
        return column

    def _build_form_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(10)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 6, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        for name, label, _group in _FIELDS:
            if name == "note":
                editor: QLineEdit | QPlainTextEdit = QPlainTextEdit()
                editor.setFixedHeight(64)
            else:
                editor = QLineEdit()
                editor.setClearButtonEnabled(True)
            self._editors[name] = editor
            form.addRow(label, editor)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(form_widget)
        column.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet("font-size: 12px; color: #666;")
        bottom.addWidget(self._status, 1)

        self._save_button = QPushButton("保存到词库")
        self._save_button.setMinimumWidth(110)
        self._save_button.clicked.connect(self._save)
        bottom.addWidget(self._save_button)
        column.addLayout(bottom)

        return column

    # ------------------------------------------------------------- 类型
    def current_type(self) -> str:
        return (
            models.ENTRY_TYPE_GRAMMAR
            if self._grammar_radio.isChecked()
            else models.ENTRY_TYPE_WORD
        )

    def _on_type_toggled(self, _button, checked: bool) -> None:
        if checked:
            self._apply_type()

    def _apply_type(self) -> None:
        entry_type = self.current_type()
        for name, _label, group in _FIELDS:
            editor = self._editors[name]
            applicable = group is None or group == entry_type
            editor.setEnabled(applicable)
            editor.setPlaceholderText("" if applicable else f"这一项只在{models.TYPE_LABELS[group]}记录里用")

        if entry_type == models.ENTRY_TYPE_GRAMMAR:
            self._type_note.setText("语法记录：请填语法点与接续，生词字段会被清空。")
            self._editors["grammar_point"].setPlaceholderText("如：〜ことになる")
        else:
            self._type_note.setText("生词记录：请填词形与读音，语法字段会被清空。")
            self._editors["word"].setPlaceholderText("如：食べる")

    # ------------------------------------------------------------- 选图
    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择图片", str(Path.home()), _IMAGE_FILTER)
        if path:
            self._load_image(path)

    def _load_image(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._image_path = None
            self._recognize_button.setEnabled(False)
            self._show_banner(
                "这个文件打不开或不是图片。iPhone 的 HEIC 格式不支持，"
                "请用微信/QQ 传图，或先转成 JPG。",
                "error",
            )
            return

        self._image_path = Path(path)
        self._preview.setPixmap(
            pixmap.scaled(
                *_PREVIEW_SIZE,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        self._image_label.setText(str(self._image_path))
        self._recognize_button.setEnabled(True)
        self._show_banner(f"已选择：{self._image_path.name}。点「开始识别」，或直接手填后保存。", "info")

    # ------------------------------------------------------------- 识别
    def _start_recognition(self) -> None:
        if self._image_path is None:
            self._show_banner("请先选择一张图片。", "error")
            return

        self._set_busy(True)
        self._show_banner("正在识别…（几秒到几十秒，界面不会卡住）", "info")

        self._worker = RecognitionWorker(self._image_path, self.current_type(), self)
        self._worker.succeeded.connect(self._on_recognized)
        self._worker.failed.connect(self._on_recognize_failed)
        self._worker.finished.connect(lambda: self._set_busy(False))
        self._worker.start()

    def _on_recognized(self, entry: models.Entry) -> None:
        self._fill_form(entry)
        missing = [
            label
            for name, label, group in _FIELDS
            if getattr(entry, name) == ""
            and (group is None or group == entry.type)
            and name not in ("error_reason", "note")
        ]
        if missing:
            self._show_banner(
                "识别完成，但「" + "、".join(missing) + "」没认出来，请对照原图补上再保存。",
                "ok",
            )
        else:
            self._show_banner("识别完成，请对照原图核对一遍再保存。", "ok")

    def _on_recognize_failed(self, user_message: str, detail: str) -> None:
        # detail 只进终端/日志，界面上只给人话
        print(f"[recognize] {detail}", file=sys.stderr)
        self._show_banner(f"{user_message}\n也可以照着原图手动填完再保存。", "error")

    def _fill_form(self, entry: models.Entry) -> None:
        for name, _label, _group in _FIELDS:
            value = getattr(entry, name, "")
            editor = self._editors[name]
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText(value)
            else:
                editor.setText(value)

    # ------------------------------------------------------------- 保存
    def _collect_entry(self) -> models.Entry:
        entry = models.Entry(type=self.current_type())
        for name, _label, _group in _FIELDS:
            editor = self._editors[name]
            text = (
                editor.toPlainText()
                if isinstance(editor, QPlainTextEdit)
                else editor.text()
            )
            setattr(entry, name, text)
        return entry

    def _save(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._show_banner("识别还没结束，等一下再保存。", "info")
            return

        entry = self._collect_entry()
        try:
            saved = entry_service.save_entry(entry, image_source=self._image_path)
        except EntryValidationError as exc:
            self._show_banner(exc.user_message, "error")
            return
        except RecognitionError as exc:
            # 图片复制失败（原图被移走 / 格式不支持 / 磁盘满）
            print(f"[save] {exc.detail}", file=sys.stderr)
            self._show_banner(exc.user_message, "error")
            return
        except SaveFailed as exc:
            print(f"[save] {exc.detail}", file=sys.stderr)
            self._show_banner(exc.user_message, "error")
            return

        self.entry_saved.emit(saved)
        self._reset_form(keep_banner=False)
        self._show_banner(f"已保存：{saved.title}（id={saved.id}）。", "ok")

    # ------------------------------------------------------------- 杂项
    def _set_busy(self, busy: bool) -> None:
        self._recognize_button.setEnabled(not busy and self._image_path is not None)
        self._recognize_button.setText("识别中…" if busy else "开始识别")
        self._save_button.setEnabled(not busy)
        self._status.setText("正在识别…" if busy else "")

    def _show_banner(self, text: str, level: str = "info") -> None:
        self._banner.setText(text)
        self._banner.setStyleSheet(
            "padding: 9px 12px; border-radius: 6px; " + _BANNER_STYLES.get(level, "")
        )
        self._banner.setVisible(True)

    def _reset_form(self, *, keep_banner: bool = True) -> None:
        for editor in self._editors.values():
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText("")
            else:
                editor.clear()

        self._image_path = None
        self._preview.clear()
        self._preview.setText("还没有选择图片\n\n点「选择图片」，或把图片拖到这里")
        self._image_label.setText("")
        self._recognize_button.setEnabled(False)
        if not keep_banner:
            self._banner.setVisible(False)


__all__ = ["CapturePage"]