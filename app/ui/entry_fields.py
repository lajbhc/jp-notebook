"""记录表单的字段清单与编辑控件工具。

字段中文名只在这里定义一份：录入页、编辑对话框都从这里取。
如果两处各写一份，早晚会出现"录入页叫「释义」、编辑框叫「意思」"这种事。

本模块只管"字段长什么样"，不管业务规则——哪些字段必填、切类型要清哪些字段，
都在 `entry_service.normalize()` 里。
"""

from __future__ import annotations

from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QWidget

from app import models

# 这些字段用多行输入框，其余用单行
MULTILINE_FIELDS = frozenset({"note"})

# 多行输入框的高度（像素）
MULTILINE_HEIGHT = 64


class FieldSpec:
    """一个表单字段：数据列名、中文标签、只属于哪一类（None 表示两类共用）。"""

    __slots__ = ("name", "label", "only_type")

    def __init__(self, name: str, label: str, only_type: str | None = None) -> None:
        self.name = name
        self.label = label
        self.only_type = only_type

    @property
    def multiline(self) -> bool:
        return self.name in MULTILINE_FIELDS

    def disabled_hint(self, current_type: str) -> str:
        """字段与当前类型无关时给输入框的占位提示。"""
        if self.only_type is None or self.only_type == current_type:
            return ""
        return f"这一项只在{models.TYPE_LABELS[self.only_type]}记录里用"

    def __repr__(self) -> str:  # 便于脚本里打印调试
        return f"FieldSpec({self.name!r}, {self.label!r}, {self.only_type!r})"


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("word", "词形", models.ENTRY_TYPE_WORD),
    FieldSpec("reading", "读音", models.ENTRY_TYPE_WORD),
    FieldSpec("part_of_speech", "词性", models.ENTRY_TYPE_WORD),
    FieldSpec("grammar_point", "语法点", models.ENTRY_TYPE_GRAMMAR),
    FieldSpec("connection", "接续", models.ENTRY_TYPE_GRAMMAR),
    FieldSpec("meaning", "释义"),
    FieldSpec("example", "例句"),
    FieldSpec("example_cn", "例句翻译"),
    FieldSpec("error_reason", "错因"),
    FieldSpec("note", "备注"),
)

FIELD_NAMES: tuple[str, ...] = tuple(spec.name for spec in FIELDS)


def fields_for(entry_type: str) -> tuple[FieldSpec, ...]:
    """当前类型下真正会用到的字段（共用字段 + 本类型专属字段）。"""
    return tuple(spec for spec in FIELDS if spec.only_type in (None, entry_type))


# --------------------------------------------------------------- 控件工具
def make_editor(spec: FieldSpec, parent: QWidget | None = None):
    """按字段规格建输入控件：备注用多行，其余单行。"""
    if spec.multiline:
        editor = QPlainTextEdit(parent)
        editor.setFixedHeight(MULTILINE_HEIGHT)
        editor.setPlaceholderText(spec.label)
        return editor
    editor = QLineEdit(parent)
    editor.setPlaceholderText(spec.label)
    editor.setClearButtonEnabled(True)
    return editor


def editor_value(editor: QLineEdit | QPlainTextEdit) -> str:
    if isinstance(editor, QPlainTextEdit):
        return editor.toPlainText()
    return editor.text()


def set_editor_value(editor: QLineEdit | QPlainTextEdit, value: str) -> None:
    if isinstance(editor, QPlainTextEdit):
        editor.setPlainText(value)
    else:
        editor.setText(value)


def fill(editors: dict[str, QLineEdit | QPlainTextEdit], entry: models.Entry) -> None:
    """把一条记录填进控件集合。"""
    for name, editor in editors.items():
        set_editor_value(editor, getattr(entry, name, "") or "")


def read(
    editors: dict[str, QLineEdit | QPlainTextEdit],
    *,
    entry_type: str,
    entry_id: int | None = None,
    source_image: str = "",
) -> models.Entry:
    """从控件集合读出 Entry。必填校验交给 entry_service.normalize()。"""
    values = {name: editor_value(editor) for name, editor in editors.items()}
    return models.Entry(
        id=entry_id,
        type=entry_type,
        source_image=source_image,
        **values,
    )


def apply_enabled(
    editors: dict[str, QLineEdit | QPlainTextEdit],
    entry_type: str,
) -> None:
    """与当前类型无关的字段禁用掉，并说明为什么用不了。

    不禁用直接留着，用户会在生词记录里认真填「接续」，然后保存时被清空。
    """
    for spec in FIELDS:
        editor = editors.get(spec.name)
        if editor is None:
            continue
        usable = spec.only_type in (None, entry_type)
        editor.setEnabled(usable)
        if not usable:
            set_editor_value(editor, "")
        hint = spec.disabled_hint(entry_type)
        if isinstance(editor, QPlainTextEdit):
            editor.setPlaceholderText(hint or spec.label)
        else:
            editor.setPlaceholderText(hint or spec.label)


__all__ = [
    "FIELD_NAMES",
    "FIELDS",
    "FieldSpec",
    "MULTILINE_FIELDS",
    "apply_enabled",
    "editor_value",
    "fields_for",
    "fill",
    "make_editor",
    "read",
    "set_editor_value",
]