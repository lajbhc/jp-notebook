"""识别提示词模板。

为什么单独放一个模块：提示词是要反复调的（换模型、换题型、发现模型总漏某个字段
都得改），散在业务代码里会越改越乱；而且这些字符串长、中文多，单独放也更好读。
"""

from __future__ import annotations

from app.models import ENTRY_TYPE_GRAMMAR, ENTRY_TYPE_WORD

__all__ = ["build_prompt", "WORD_PROMPT", "GRAMMAR_PROMPT"]

# 两类共用的规矩。写提示词的经验：把「不许做什么」写清楚，比堆形容词有用。
_COMMON_RULES = """你是日语学习助手，负责把图片里的日语内容整理成结构化的错题笔记。

必须遵守：
1. 只输出一个 JSON 对象，不要输出解释、不要用 Markdown 代码块包裹。
2. 所有值都是字符串。图片里没有或你不确定的信息，填空字符串 ""，绝对不要猜、不要编造。
3. 不要输出 JSON 以外的任何字符。
4. 图片里如果有多个条目，只整理最完整的第一个。"""

_WORD_SCHEMA = """{
  "word": "词形，原形（动词用辞书形，形容词用原形）",
  "reading": "假名读音，平假名",
  "meaning": "中文释义，多个义项用；分隔",
  "part_of_speech": "词性，如 动词・一段 / 名词 / イ形容词 / ナ形容词 / 副词",
  "example": "图片里的日语例句，原样抄写；没有就留空",
  "example_cn": "例句的中文翻译；没有就留空",
  "error_reason": "我为什么错，从图片批注里提取；没有就留空",
  "note": "其他补充，如固定搭配、近义词辨析；没有就留空"
}"""

_GRAMMAR_SCHEMA = """{
  "grammar_point": "语法点，写成 〜ことになる 这种形式",
  "connection": "接续方式，如 動詞辞書形 + ことになる",
  "meaning": "中文意思",
  "example": "图片里的日语例句，原样抄写；没有就留空",
  "example_cn": "例句的中文翻译；没有就留空",
  "error_reason": "我为什么错，从图片批注里提取；没有就留空",
  "note": "其他补充，如使用场景、易混语法；没有就留空"
}"""

WORD_PROMPT = f"""{_COMMON_RULES}

任务：从图片中整理出一个**日语生词**。按下面的结构输出 JSON：

{_WORD_SCHEMA}"""

GRAMMAR_PROMPT = f"""{_COMMON_RULES}

任务：从图片中整理出一个**日语语法点**（不是单词）。按下面的结构输出 JSON：

{_GRAMMAR_SCHEMA}"""


def build_prompt(entry_type: str) -> str:
    """按类型返回提示词。未知类型按生词处理，不抛异常。"""
    if entry_type == ENTRY_TYPE_GRAMMAR:
        return GRAMMAR_PROMPT
    return WORD_PROMPT