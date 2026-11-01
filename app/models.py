"""领域模型。

这里只放"数据长什么样"，不放任何存取逻辑（那是 storage 的事）和业务判断
（那是 services 的事）。storage 层负责在 sqlite3.Row 与本模型之间转换，
ui 层只跟本模型打交道，因此换存储实现时模型不用动。
"""

from __future__ import annotations

from dataclasses import dataclass

# 只支持两类，顺序即界面上的展示顺序
ENTRY_TYPE_WORD = "word"
ENTRY_TYPE_GRAMMAR = "grammar"
ENTRY_TYPES: tuple[str, ...] = (ENTRY_TYPE_WORD, ENTRY_TYPE_GRAMMAR)

TYPE_LABELS: dict[str, str] = {
    ENTRY_TYPE_WORD: "生词",
    ENTRY_TYPE_GRAMMAR: "语法",
}

# 两类记录各自的核心字段：生词看 word，语法看 grammar_point
PRIMARY_FIELD: dict[str, str] = {
    ENTRY_TYPE_WORD: "word",
    ENTRY_TYPE_GRAMMAR: "grammar_point",
}


@dataclass
class Entry:
    """一条录入记录。生词与语法共用，缺失字段留空字符串（不用 None）。

    使用 str 默认值而非 Optional，是为了让界面层不必到处判空：
    QLineEdit.setText(None) 会直接抛异常，统一成空串能省掉一层防御代码。
    """

    type: str
    id: int | None = None

    # ---- 生词字段 ----
    word: str = ""            # 词形，如「食べる」
    reading: str = ""         # 读音 / 假名，如「たべる」
    meaning: str = ""         # 中文释义
    part_of_speech: str = ""  # 词性，如「动词・一段」

    # ---- 语法字段 ----
    grammar_point: str = ""   # 语法点，如「〜ことになる」
    connection: str = ""      # 接续，如「動詞辞書形 + ことになる」

    # ---- 两类共用 ----
    example: str = ""         # 日语例句
    example_cn: str = ""      # 例句中文翻译
    error_reason: str = ""    # 错因（为什么这道题错了）
    note: str = ""            # 备注

    # ---- 溯源与时间 ----
    source_image: str = ""    # 图片相对路径，如 2026-11-01/ab12cd.jpg
    created_at: str = ""      # YYYY-MM-DD HH:MM:SS（本地时间）
    updated_at: str = ""

    @property
    def type_label(self) -> str:
        """中文类型名，界面上直接显示。"""
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def title(self) -> str:
        """列表里显示的主标题：生词取词形，语法取语法点。"""
        if self.type == ENTRY_TYPE_GRAMMAR:
            return self.grammar_point or self.word or "（未命名语法）"
        return self.word or self.grammar_point or "（未命名生词）"

    @property
    def primary_text(self) -> str:
        """当前类型对应的核心字段内容。"""
        name = PRIMARY_FIELD.get(self.type, "word")
        return getattr(self, name, "")

    def matches(self, keyword: str) -> bool:
        """纯内存版关键词匹配，供校验脚本与单元测试使用。

        真正的检索以 SQL 为准（见 repository），这里只是为了能在不碰数据库的
        情况下验证"哪些字段该被搜到"这一约定。
        """
        needle = keyword.strip().lower()
        if not needle:
            return True
        haystack = " ".join(
            [
                self.word,
                self.reading,
                self.meaning,
                self.part_of_speech,
                self.grammar_point,
                self.connection,
                self.example,
                self.example_cn,
                self.error_reason,
                self.note,
            ]
        ).lower()
        return needle in haystack