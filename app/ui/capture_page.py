"""录入页：选图 → （可选）框选范围 → 识别 → 人工校正 → 保存。

这一页只做界面和交互，不做任何识别/存库细节：
- 识别走 `RecognitionWorker`（子线程），失败只拿 `user_message` 显示
- 保存走 `entry_service.save_entry()`，字段校验也在那一层
- 图片复制进 data/uploads 的动作同样在 entry_service，本页只把选中的路径传下去
- 框选只算"归一化比例"，真正裁剪在 services/recognizer 里做

识别失败不是死路：界面保留表单，用户可以照着原图手填完再保存。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app import models
from app.logger import get_logger
from app.services import entry_service
from app.services.entry_service import EntryValidationError, SaveFailed
from app.services.errors import RecognitionError
from app.ui import entry_fields, style, window_state
from app.ui.crop_dialog import CropDialog
from app.ui.crop_view import CropView
from app.ui.recognition_worker import EnrichWorker, RecognitionWorker

_IMAGE_FILTER = "图片 (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff)"

_PREVIEW_MIN = (300, 220)   # 预览的最小尺寸；实际会随窗口拉伸（见 QSplitter）

_PLACEHOLDER = "还没有选择图片\n\n点「选择图片」，或把图片拖到这里"

# (字段名, 中文标签, 只属于哪一类；None 表示两类共用)
# 字段清单只有 entry_fields.FIELDS 一份，编辑对话框用同一份，避免标签漂移。
_FIELDS: tuple[tuple[str, str, str | None], ...] = tuple(
    (spec.name, spec.label, spec.only_type) for spec in entry_fields.FIELDS
)


class CapturePage(QWidget):
    """录入页。"""

    entry_saved = Signal(object)  # models.Entry，主窗口可以据此刷新列表

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image_path: Path | None = None
        # 同一张图连续录入时，第一条保存后把 uploads 里的相对路径记下来，
        # 后面几条直接复用，不再把同一张图复制好几份。
        self._image_rel: str | None = None
        self._worker: RecognitionWorker | None = None
        self._enrich_worker: EnrichWorker | None = None
        self._editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        self._field_labels: dict[str, QLabel] = {}
        # 当前表单里哪些字段是 AI 补的（保存时原样写进 entry.generated_fields）
        self._generated: tuple[str, ...] = ()
        self._build_ui()
        self._apply_type()

    # ------------------------------------------------------------- 构建界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("录入一条记录")
        title.setStyleSheet(style.TITLE)
        root.addWidget(title)

        hint = QLabel("选图 → 识别 → 对照原图改正 → 保存。识别结果只是草稿。")
        hint.setStyleSheet(style.HINT)
        root.addWidget(hint)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        root.addLayout(self._build_type_row())

        # 预览和表单分成可拖动的两栏：大屏上把预览拉大能看清字，小屏上也能各自压窄
        body = QSplitter(Qt.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(8)
        left = QWidget()
        self._build_image_column(QVBoxLayout(left))
        right = QWidget()
        self._build_form_column(QVBoxLayout(right))
        body.addWidget(left)
        body.addWidget(right)
        body.setStretchFactor(0, 4)
        body.setStretchFactor(1, 5)
        body.setSizes([400, 460])
        self._body_splitter = body
        root.addWidget(body, 1)

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
        self._type_note.setStyleSheet(style.HINT_FAINT)
        return self._type_note

    def _build_image_column(self, column: QVBoxLayout) -> None:
        column.setSpacing(style.ROW_GAP)
        column.setContentsMargins(0, 0, 0, 0)

        self._preview = CropView(_PLACEHOLDER, size=_PREVIEW_MIN, fixed=False)
        self._preview.file_dropped.connect(self._load_image)
        self._preview.selection_changed.connect(self._on_selection_changed)
        self._preview.double_clicked.connect(self._open_crop_dialog)
        self._preview.setToolTip(
            "拖动框选识别范围；双击图片打开放大框选；右键取消框选"
        )
        column.addWidget(self._preview)

        self._crop_label = QLabel("")
        self._crop_label.setWordWrap(True)
        self._crop_label.setStyleSheet(style.ACCENT_TEXT)
        column.addWidget(self._crop_label)

        crop_row = QHBoxLayout()
        crop_row.setSpacing(6)
        self._clear_crop_button = QPushButton("还原整张")
        self._clear_crop_button.setEnabled(False)
        self._clear_crop_button.setToolTip("取消框选，识别整张图")
        self._clear_crop_button.clicked.connect(self._preview.clear_selection)
        crop_row.addWidget(self._clear_crop_button)

        self._zoom_button = QPushButton("放大框选")
        self._zoom_button.setToolTip("把图放大到整屏来框，对得更准（双击图片同效）")
        self._zoom_button.clicked.connect(self._open_crop_dialog)
        crop_row.addWidget(self._zoom_button)
        column.addLayout(crop_row)

        self._image_label = QLabel("")
        self._image_label.setWordWrap(True)
        self._image_label.setStyleSheet(style.CAPTION)
        column.addWidget(self._image_label)

        pick = QPushButton("选择图片")
        pick.setMinimumHeight(style.BUTTON_HEIGHT)
        pick.clicked.connect(self._pick_image)
        column.addWidget(pick)

        self._recognize_button = QPushButton("开始识别")
        self._recognize_button.setMinimumHeight(style.BUTTON_HEIGHT)
        self._recognize_button.setEnabled(False)
        self._recognize_button.clicked.connect(self._start_recognition)
        column.addWidget(self._recognize_button)

        clear = QPushButton("清空重来")
        clear.setMinimumHeight(style.BUTTON_HEIGHT)
        clear.clicked.connect(lambda: self._reset_form(keep_banner=False))
        column.addWidget(clear)

        self._clear_crop_button.setMinimumHeight(style.BUTTON_HEIGHT)
        self._zoom_button.setMinimumHeight(style.BUTTON_HEIGHT)

    def _build_form_column(self, column: QVBoxLayout) -> None:
        column.setSpacing(style.ROW_GAP)
        column.setContentsMargins(0, 0, 0, 0)

        tools = QHBoxLayout()
        tools.setSpacing(style.TIGHT_GAP)
        self._auto_enrich_check = QCheckBox("识别后自动补全空缺字段")
        self._auto_enrich_check.setChecked(window_state.load_auto_enrich())
        self._auto_enrich_check.setToolTip(
            "识别完成后，让文字模型补上图片里没有的读音、释义、例句等。\n"
            "补出来的字段会标成浅蓝底，保存前请核对。"
        )
        self._auto_enrich_check.toggled.connect(window_state.save_auto_enrich)
        tools.addWidget(self._auto_enrich_check)
        tools.addStretch(1)
        self._reenrich_button = QPushButton("重新补全")
        self._reenrich_button.setMinimumHeight(style.BUTTON_HEIGHT)
        self._reenrich_button.setEnabled(False)
        self._reenrich_button.setToolTip("按现在填好的内容，再把空着的字段补一遍")
        self._reenrich_button.clicked.connect(self._start_reenrich)
        tools.addWidget(self._reenrich_button)
        column.addLayout(tools)

        self._ai_hint = QLabel("")
        self._ai_hint.setStyleSheet(style.CAPTION)
        self._ai_hint.setWordWrap(True)
        self._ai_hint.setVisible(False)
        column.addWidget(self._ai_hint)

        form_widget = QWidget()
        # 用 QGridLayout 而不是 QFormLayout：读音/词性是短词，挤一行能少滚动一屏；
        # 其余字段横跨整行，长日语句子有地方显示。
        form = QGridLayout(form_widget)
        form.setContentsMargins(0, 0, 6, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(style.ROW_GAP)
        form.setColumnMinimumWidth(0, 56)
        form.setColumnMinimumWidth(2, 56)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(3, 1)

        row = 0
        half_open = False
        for name, label, _group in _FIELDS:
            if name == "note":
                editor: QLineEdit | QPlainTextEdit = QPlainTextEdit()
                editor.setMinimumHeight(style.NOTE_MIN_HEIGHT)
                editor.setMaximumHeight(style.NOTE_MAX_HEIGHT)
            else:
                editor = QLineEdit()
                editor.setClearButtonEnabled(True)
            self._editors[name] = editor
            # 主字段一有内容就允许「重新补全」
            editor.textChanged.connect(self._sync_reenrich_button)

            name_label = QLabel(label)
            name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._field_labels[name] = name_label

            if name == "part_of_speech" and half_open:
                # 接着"读音"那一行的右半边放
                form.addWidget(name_label, row, 2)
                form.addWidget(editor, row, 3)
                row += 1
                half_open = False
                continue
            if name == "reading":
                form.addWidget(name_label, row, 0)
                form.addWidget(editor, row, 1)
                half_open = True  # 这一行右半边留给词性
                continue
            form.addWidget(name_label, row, 0)
            form.addWidget(editor, row, 1, 1, 3)
            row += 1

        # 多出来的高度给"备注下面那一行"：备注框到 160 就够，再高就是浪费空间，
        # 让空白留在底部，字段保持紧凑（否则备注会被拉成一整屏）。
        form.setRowStretch(row, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(form_widget)
        column.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet(style.HINT)
        bottom.addWidget(self._status, 1)

        self._save_button = QPushButton("保存到词库")
        self._save_button.setMinimumWidth(120)
        self._save_button.setMinimumHeight(style.BUTTON_HEIGHT)
        self._save_button.setStyleSheet(style.PRIMARY_BUTTON)
        self._save_button.clicked.connect(self._save)
        bottom.addWidget(self._save_button)
        column.addLayout(bottom)

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

        self._sync_reenrich_button()

    # ------------------------------------------------------------- 选图
    def load_image(self, path: str | Path) -> None:
        """给外部页面用的入口（手机上传页、以后的剪贴板导入）。"""
        self._load_image(str(path))

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择图片", str(Path.home()), _IMAGE_FILTER)
        if path:
            self._load_image(path)

    def _load_image(self, path: str) -> None:
        if not self._preview.set_image(path):
            self._image_path = None
            self._image_rel = None
            self._recognize_button.setEnabled(False)
            self._show_banner(
                "这个文件打不开或不是图片。iPhone 的 HEIC 格式不支持，"
                "请用微信/QQ 传图，或先转成 JPG。",
                "error",
            )
            return

        self._image_path = self._preview.image_path
        self._image_rel = None  # 换了图，之前存下的那份相对路径作废
        self._image_label.setText(str(self._image_path))
        self._recognize_button.setEnabled(True)
        self._show_banner(
            f"已选择：{self._image_path.name}。"
            "可以在图上拖一个框只识别那一块；框不准就双击图片放大来框，不框就是整张。",
            "info",
        )

    def _on_selection_changed(self, selection) -> None:
        self._crop_label.setText(self._preview.selection_text())
        self._clear_crop_button.setEnabled(selection is not None)

    def _open_crop_dialog(self) -> None:
        """放大框选：把小预览里框不准的问题解决在源头。"""
        if self._image_path is None:
            self._show_banner("请先选择一张图片，再放大框选。", "error")
            return

        dialog = CropDialog(self._image_path, self._preview.selection(), self)
        try:
            accepted = dialog.exec() == QDialog.Accepted
            selection = dialog.selection() if accepted else None
        finally:
            dialog.deleteLater()

        if not accepted:
            return
        self._preview.set_selection(selection)
        if selection is None:
            self._show_banner("已设为识别整张图。", "info")
        else:
            self._show_banner(
                f"已框选：{self._preview.selection_text()}"
                "。点「开始识别」就只认这一块。",
                "ok",
            )

    # ------------------------------------------------------------- 识别
    def _start_recognition(self) -> None:
        if self._image_path is None:
            self._show_banner("请先选择一张图片。", "error")
            return
        if self._enrich_running():
            self._show_banner("补全还没结束，等一下再识别。", "info")
            return

        crop = self._preview.selection()
        auto_enrich = self._auto_enrich_check.isChecked()
        self._set_busy(True)
        where = "整张图" if crop is None else "框选的那一块"
        if auto_enrich:
            self._show_banner(
                f"正在识别{where}，识别完还会补全空着的字段…（十几秒，界面不会卡住）",
                "info",
            )
        else:
            self._show_banner(f"正在识别{where}…（几秒到几十秒，界面不会卡住）", "info")

        self._worker = RecognitionWorker(
            self._image_path,
            self.current_type(),
            crop,
            self,
            auto_enrich=auto_enrich,
            source_image=self._image_rel or "",
        )
        self._worker.succeeded.connect(self._on_recognized)
        self._worker.failed.connect(self._on_recognize_failed)
        self._worker.finished.connect(lambda: self._set_busy(False))
        self._worker.start()

    def _on_recognized(self, result) -> None:
        entry = result.entry
        self._fill_form(entry)
        self._mark_generated(entry.generated_names)

        if getattr(result, "enrich_error", ""):
            self._show_banner(
                f"{result.enrich_error}\n识别结果已经填好了，可以照着原图补完再保存。",
                "info",
            )
            return

        missing = [
            label
            for name, label, group in _FIELDS
            if getattr(entry, name) == ""
            and (group is None or group == entry.type)
            and name not in ("error_reason", "note")
        ]
        note = getattr(result, "note", "")
        if missing:
            text = (
                "识别完成，但「" + "、".join(missing) + "」没认出来，请对照原图补上再保存。"
            )
        else:
            text = "识别完成，请对照原图核对一遍再保存。"
        if note:
            text = f"{text}\n{note}"
        self._show_banner(text, "ok")

    def _on_recognize_failed(self, user_message: str, detail: str) -> None:
        # detail 只进终端/日志，界面上只给人话
        get_logger("ui.capture").warning("识别失败：%s", detail)
        self._show_banner(f"{user_message}\n也可以照着原图手动填完再保存。", "error")

    def _fill_form(self, entry: models.Entry) -> None:
        for name, _label, _group in _FIELDS:
            value = getattr(entry, name, "")
            editor = self._editors[name]
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText(value)
            else:
                editor.setText(value)
        self._sync_reenrich_button()

    # ------------------------------------------------------------- AI 标记
    def _enrich_running(self) -> bool:
        return self._enrich_worker is not None and self._enrich_worker.isRunning()

    def _mark_generated(self, names) -> None:
        """给 AI 补过的字段上浅蓝底、在标签后面加「AI 补」，并汇总一句提示。"""
        self._generated = tuple(name for name in names if name in self._editors)
        entry_fields.mark_generated(self._editors, self._generated)

        for name, name_label in self._field_labels.items():
            text = name_label.text().replace(entry_fields.AI_LABEL_SUFFIX, "")
            if name in self._generated:
                text += entry_fields.AI_LABEL_SUFFIX
            name_label.setText(text)

        count = len(self._generated)
        if count:
            self._ai_hint.setText(
                f"有 {count} 个字段是 AI 补的（浅蓝底），图片上未必有，保存前请核对。"
            )
        self._ai_hint.setVisible(bool(count))
        self._sync_reenrich_button()

    def _sync_reenrich_button(self) -> None:
        """表单里只要有主字段，就允许再补一次。"""
        primary = "grammar_point" if self.current_type() == models.ENTRY_TYPE_GRAMMAR else "word"
        editor = self._editors.get(primary)
        filled = entry_fields.editor_value(editor) != "" if editor is not None else False
        busy = self._enrich_running() or (
            self._worker is not None and self._worker.isRunning()
        )
        self._reenrich_button.setEnabled(filled and not busy)

    def _start_reenrich(self) -> None:
        if self._enrich_running():
            self._show_banner("上一次补全还没结束，等一下。", "info")
            return
        if self._worker is not None and self._worker.isRunning():
            self._show_banner("识别还没结束，等一下再补全。", "info")
            return

        entry = self._collect_entry()
        if entry.title == "":
            self._show_banner("先把词形或语法点填上，再点「重新补全」。", "error")
            return

        self._set_busy(True)
        self._show_banner("正在补全空着的字段…（几秒，界面不会卡住）", "info")
        self._enrich_worker = EnrichWorker(entry, self)
        self._enrich_worker.finished_with.connect(self._on_enriched)
        self._enrich_worker.finished.connect(lambda: self._set_busy(False))
        self._enrich_worker.start()

    def _on_enriched(self, result) -> None:
        self._fill_form(result.entry)
        # 补全只往空字段里写，所以之前的标记要留着
        merged = tuple(dict.fromkeys((*self._generated, *result.entry.generated_names)))
        self._mark_generated(merged)

        if getattr(result, "enrich_error", ""):
            self._show_banner(result.enrich_error, "error")
            return
        enrichment = getattr(result, "enrichment", None)
        if enrichment is None or enrichment.is_empty:
            self._show_banner("没有可补的字段了 —— 该有的都已经有内容。", "info")
        else:
            self._show_banner(
                f"补全完成：{enrichment.summary}\n浅蓝底的字段请核对后再保存。",
                "ok",
            )

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
        entry.generated_fields = ",".join(self._generated)
        return entry

    def _persist(self, entry: models.Entry) -> models.Entry:
        """存一条记录。同一张图连着录第二条时不重复复制图片。"""
        if self._image_path is None:
            return entry_service.save_entry(entry)
        if self._image_rel is not None:
            entry.source_image = self._image_rel
            return entry_service.save_entry(entry)
        saved = entry_service.save_entry(entry, image_source=self._image_path)
        self._image_rel = saved.source_image
        return saved

    def _save(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._show_banner("识别还没结束，等一下再保存。", "info")
            return
        if self._enrich_running():
            self._show_banner("补全还没结束，等一下再保存。", "info")
            return

        entry = self._collect_entry()
        try:
            saved = self._persist(entry)
        except EntryValidationError as exc:
            self._show_banner(exc.user_message, "error")
            return
        except RecognitionError as exc:
            # 图片复制失败（原图被移走 / 格式不支持 / 磁盘满）
            get_logger("ui.capture").warning("保存失败：%s", exc.detail)
            self._show_banner(exc.user_message, "error")
            return
        except SaveFailed as exc:
            get_logger("ui.capture").warning("保存失败：%s", exc.detail)
            self._show_banner(exc.user_message, "error")
            return

        self.entry_saved.emit(saved)
        has_image = self._image_path is not None
        # 保存后保留图片：一页纸上往往有好几条，接着拖下一个框就能录下一条
        self._reset_form(keep_banner=False, keep_image=True)
        if has_image:
            self._show_banner(
                f"已保存：{saved.title}（id={saved.id}）。"
                "这张图还留着 —— 再拖一个框（或双击放大框选）就能接着录下一条；"
                "换图请点「选择图片」，全部清掉点「清空重来」。",
                "ok",
            )
        else:
            self._show_banner(f"已保存：{saved.title}（id={saved.id}）。", "ok")

    # ------------------------------------------------------------- 杂项
    def _set_busy(self, busy: bool) -> None:
        self._recognize_button.setEnabled(not busy and self._image_path is not None)
        self._recognize_button.setText("识别中…" if busy else "开始识别")
        self._save_button.setEnabled(not busy)
        self._auto_enrich_check.setEnabled(not busy)
        self._status.setText("正在识别…" if busy else "")
        self._sync_reenrich_button()

    def _show_banner(self, text: str, level: str = "info") -> None:
        self._banner.setText(text)
        self._banner.setStyleSheet(style.banner_qss(level))
        self._banner.setVisible(True)

    def _reset_form(self, *, keep_banner: bool = True, keep_image: bool = False) -> None:
        """清空表单。

        `keep_image=True` 只清字段和框选、把图片留在预览里——同一张纸接着录下一条时用；
        默认连图片一起清掉（「清空重来」就是这条路径）。
        """
        for editor in self._editors.values():
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText("")
            else:
                editor.clear()
        self._mark_generated(())

        if keep_image and self._preview.image_path is not None:
            self._preview.clear_selection()
            self._crop_label.setText("")
            self._clear_crop_button.setEnabled(False)
            self._recognize_button.setEnabled(True)
            if not keep_banner:
                self._banner.setVisible(False)
            return

        self._image_path = None
        self._image_rel = None
        self._preview.clear_image()
        self._crop_label.setText("")
        self._clear_crop_button.setEnabled(False)
        self._image_label.setText("")
        self._recognize_button.setEnabled(False)
        if not keep_banner:
            self._banner.setVisible(False)


__all__ = ["CapturePage"]