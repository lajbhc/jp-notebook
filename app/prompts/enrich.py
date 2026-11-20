"""补全提示词：把一条残缺的记录补成能复习的卡片。

和 `prompts/recognize.py` 的分工：

- 识别提示词的任务是「照抄」，所以它反复强调"图片里没有的一律留空、不要猜"。
- 这里是「补」，只填空字段，而且要按日语教学的标准写法来。

两块提示词分开写、分两次调用，是刻意的：如果混在一次调用里，模型很容易把
补出来的例句写进"照抄"的字段，我们就再也分不清哪个字来自图片、哪个字是编的。
"""

from __future__ import annotations

from app.models import (
    ENTRY_TYPE_GRAMMAR,
    ENTRY_TYPE_WORD,
    PART_OF_SPEECH_OPTIONS,
)

__all__ = ["build_prompt", "FILLABLE_FIELDS"]

# 每类记录允许被补全的字段。顺序即提示词里的顺序，也影响界面上「补了哪些」的展示。
FILLABLE_FIELDS: dict[str, tuple[str, ...]] = {
    ENTRY_TYPE_WORD: ("reading", "meaning", "part_of_speech", "example", "example_cn"),
    ENTRY_TYPE_GRAMMAR: ("meaning", "connection", "example", "example_cn"),
}

_POS_LINE = " / ".join(PART_OF_SPEECH_OPTIONS)

_COMMON_RULES = f"""你是日语老师，负责把学生的一条错题笔记补成可以复习的卡片。

必须遵守：
1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释，不要输出任何别的字符。
2. 所有值都是字符串。拿不准就留空字符串 ""，宁可空着让人自己填，也不要编。
3. 只输出下面「需要补的字段」里列出的键，其他键一个都不要出现。
4. 学生已经写好的内容是他的笔记，视为正确：不要修改、不要改写、不要重复输出。
5. 例句要求：一句话即可，8~40 个日文字符，N3 左右的难度，自然、日常、不要生僻词。

词性只能从下面这些里选一个，原样抄写，不要自己造写法：
{_POS_LINE}"""

_WORD_RULES = """这是一条**生词**记录。
- reading：这个单词的平假名读音，不要罗马字、不要汉字。
- meaning：中文释义，多个义项用「；」分隔，不要超过 25 个字。
- part_of_speech：按上面给的词性列表选一个。
- example：必须真的用上这个单词（可以用活用形），并且能看出它的典型用法。
- example_cn：把 example 那句翻成中文。如果 example 是学生已经写好的，就翻译他那句。"""

_GRAMMAR_RULES = """这是一条**语法**记录。
- meaning：这个语法点的中文意思，一句话说清，不要超过 30 个字。
- connection：接续方式，写成「動詞辞書形 ＋ ことになる」这种形式，中间必须有 ＋。
- example：必须真的体现这个语法点，并且用上 connection 里 ＋ 右边的那部分。
- example_cn：把 example 那句翻成中文。如果 example 是学生已经写好的，就翻译他那句。"""


def build_prompt(entry_type: str, targets: tuple[str, ...] | list[str]) -> str:
    """拼出补全提示词。

    `targets` 是这次要补的字段，由服务层按"哪些字段还是空的"算出来。
    未知类型按生词处理（与 `prompts/recognize.py` 保持一致，不抛异常）。
    """
    rules = _GRAMMAR_RULES if entry_type == ENTRY_TYPE_GRAMMAR else _WORD_RULES
    wanted = "、".join(targets) if targets else "（无）"
    return (
        f"{_COMMON_RULES}\n\n{rules}\n\n"
        f"需要补的字段：{wanted}\n\n"
        '输出示例：{"example": "…", "example_cn": "…"}'
    )