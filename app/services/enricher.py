"""补全服务：把一条只有「词」或「语法点」的记录补成能复习的卡片。

为什么要单独一层
----------------
识别（`recognizer`）的任务是**照抄**：图片里没有的一律留空。但学生拍下来的
往往只是题册上的一个生词或一条语法，光有词本身没法复习。补全服务负责用纯文本
调用把「释义 / 读音 / 词性 / 接续 / 例句 / 例句翻译」这些空字段填上。

三条设计原则
------------
1. **只补空字段**：已有的内容一律不动（学生自己的笔记优先），补完之后由
   `apply()` 把"哪些字段是补的"记进 `entry.generated_fields`，界面据此打标。
2. **本地能查的就别信模型**：例句里有没有真的用上这个单词、接续里有没有 ＋、
   读音是不是纯假名、词性在不在固定枚举里——这些都能在本地判，判不过就丢掉
   并说明原因，不把可疑内容塞进词库。
3. **失败只影响补全**：识别结果该进表单还是进表单，补全挂了只提示一句
   "可以手动写，或点重新补全"，不拖垮主流程。
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from app import config
from app.models import (
    ENTRY_TYPE_GRAMMAR,
    ENTRY_TYPE_WORD,
    ENTRY_TYPES,
    PART_OF_SPEECH_OPTIONS,
    Entry,
)
from app.prompts.enrich import FILLABLE_FIELDS, build_prompt
from app.services.errors import MissingApiKey, ResponseNotJson
from app.services.recognizer import EXTRA_BODY, chat_json

__all__ = [
    "FILLABLE_FIELDS",
    "ENRICH_MAX_TOKENS",
    "MAX_FIELD_CHARS",
    "Enrichment",
    "missing_fields",
    "normalize_pos",
    "build_request",
    "parse_payload",
    "apply",
    "enrich",
]

# 补全是创作型任务，温度给太低会让例句千篇一律；0.2 在"稳定"和"有点变化"之间。
ENRICH_TEMPERATURE = 0.2
# 纯文本、最多五个字段，900 够用；思考型模型仍可能吃光，所以下面还留着截断识别。
ENRICH_MAX_TOKENS = 900
# 单个补全字段的长度上限。超过这个数基本是模型跑题了，直接丢弃。
MAX_FIELD_CHARS = 200
# 纯文本调用比看图快得多，读超时不用给到 120 秒。
DEFAULT_TIMEOUT = (10.0, 60.0)

FIELD_LABELS: dict[str, str] = {
    "reading": "读音",
    "meaning": "释义",
    "part_of_speech": "词性",
    "connection": "接续",
    "example": "例句",
    "example_cn": "例句翻译",
}

# 发给模型的"现有记录"里带哪些字段（空值不发送，省 token）。
_CONTEXT_FIELDS = (
    "word",
    "reading",
    "meaning",
    "part_of_speech",
    "grammar_point",
    "connection",
    "example",
    "example_cn",
    "error_reason",
    "note",
)

_KANA_ONLY = re.compile(r"^[ぁ-んァ-ヶー・\s]+$")
_KANA_ANY = re.compile(r"[ぁ-んァ-ヶ]")
_CJK_ANY = re.compile(r"[\u4e00-\u9fff]")

# 模型常见的词性写法 → 固定枚举。顺序有讲究：先匹配更具体的（サ変 在 一段 之前
# 无所谓，但 ナ形容词 必须在 形容词 类之前，否则会被泛化掉）。
_POS_ALIASES: tuple[tuple[str, str], ...] = (
    ("サ変", "动词・サ変"),
    ("サ行変格", "动词・サ変"),
    ("カ変", "动词・カ変"),
    ("五段", "动词・五段"),
    ("godan", "动词・五段"),
    ("一类", "动词・五段"),
    ("一段", "动词・一段"),
    ("ichidan", "动词・一段"),
    ("二类", "动词・一段"),
    ("ナ形容詞", "ナ形容词"),
    ("ナ形容词", "ナ形容词"),
    ("形容動詞", "ナ形容词"),
    ("形容动词", "ナ形容词"),
    ("イ形容詞", "イ形容词"),
    ("イ形容词", "イ形容词"),
    ("i形容词", "イ形容词"),
    ("名詞", "名词"),
    ("noun", "名词"),
    ("副詞", "副词"),
    ("adverb", "副词"),
    ("助詞", "助词"),
    ("particle", "助词"),
    ("接続詞", "接续词"),
    ("conjunction", "接续词"),
    ("慣用句", "惯用句"),
    ("惯用语", "惯用句"),
    ("idiom", "惯用句"),
)


@dataclass(frozen=True)
class Enrichment:
    """一次补全的结果。

    `fields` 是通过本地检查、可以写进记录的字段；`rejected` 是被挡下的字段和
    原因（界面可以显示成"例句没通过检查：例句里没找到这个词"）；`raw` 是模型
    原样返回的内容，只用于排查，不要写进数据库。
    """

    fields: dict[str, str]
    rejected: dict[str, str]
    raw: dict[str, str]
    elapsed: float = 0.0
    model: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.fields

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.fields))

    @property
    def summary(self) -> str:
        """给界面用的一句话说明。"""
        if self.fields:
            labels = "、".join(FIELD_LABELS.get(name, name) for name in self.names)
            text = f"补了 {len(self.fields)} 个字段：{labels}"
            if self.rejected:
                text += f"；另有 {len(self.rejected)} 个字段没过检查"
            return text
        if self.rejected:
            parts = [
                f"{FIELD_LABELS.get(name, name)}（{reason}）"
                for name, reason in self.rejected.items()
            ]
            return "补全结果没过检查：" + "；".join(parts)
        return "没有需要补的字段"


def _is_kana(char: str) -> bool:
    return "ぁ" <= char <= "ん" or "ァ" <= char <= "ヶ" or char == "ー"


def missing_fields(entry: Entry) -> tuple[str, ...]:
    """按类型算出这条记录里还空着的、允许补全的字段。"""
    return tuple(
        name
        for name in FILLABLE_FIELDS.get(entry.type, ())
        if not getattr(entry, name, "").strip()
    )


def normalize_pos(value: str) -> str | None:
    """把模型写的词性收敛到固定枚举；认不出来返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    if text in PART_OF_SPEECH_OPTIONS:
        return text
    lowered = text.lower()
    for needle, canonical in _POS_ALIASES:
        if needle.lower() in lowered:
            return canonical
    return None


def _word_candidates(word: str) -> tuple[str, ...]:
    """例句里可能出现的词形：原形、去掉最后一个假名（食べる→食べ）、去掉する。"""
    text = (word or "").strip()
    if not text:
        return ()
    candidates = [text]
    if len(text) >= 3 and _is_kana(text[-1]):
        candidates.append(text[:-1])
    if len(text) >= 3 and text.endswith(("する", "くる")):
        candidates.append(text[:-2])
    return tuple(dict.fromkeys(candidates))


def _contains_word(example: str, word: str) -> bool:
    return any(candidate and candidate in example for candidate in _word_candidates(word))


def _connection_tails(connection: str) -> tuple[str, ...]:
    """取接续里 ＋ 右边那部分，并放宽到"去掉末尾一个假名"的形态。

    「動詞辞書形 ＋ ことになる」在人写的例句里常是过去式「ことになった」、
    敬体「ことになります」、连用形「ことになり」……所以除了原样，还接受去掉
    最后一个假名的形式（ことにな）。只放宽一个假名：再短就分不清
    「ことになる」和「ことにする」了。
    """
    text = (connection or "").replace("+", "＋").strip()
    if "＋" not in text:
        return ()
    tail = text.split("＋")[-1].strip(" 　〜～・、。")
    return _tail_candidates(tail)


def _tail_candidates(tail: str) -> tuple[str, ...]:
    if len(tail) < 2:
        return ()
    candidates = [tail]
    if len(tail) >= 3 and _is_kana(tail[-1]):
        candidates.append(tail[:-1])
    return tuple(dict.fromkeys(candidates))


def _grammar_candidates(entry: Entry) -> tuple[str, ...]:
    """例句该体现出哪一串日语：优先用接续的右半，没有接续就退回语法点。"""
    tails = _connection_tails(entry.connection)
    if tails:
        return tails
    point = entry.grammar_point.replace("〜", "").replace("～", "").strip(" 　・、。")
    return _tail_candidates(point)


def _contains_connection(example: str, entry: Entry) -> bool:
    return any(candidate in example for candidate in _grammar_candidates(entry))


def _reject_reason(field: str, value: str, entry: Entry) -> str | None:
    """本地检查。返回 None 表示这个值可以用，否则返回给用户看的原因。"""
    if len(value) > MAX_FIELD_CHARS:
        return f"太长（{len(value)} 字）"
    if field == "reading":
        return None if _KANA_ONLY.match(value) else "读音里出现了非假名字符"
    if field == "meaning":
        return None if _CJK_ANY.search(value) else "释义里没有中文"
    if field == "example_cn":
        return None if _CJK_ANY.search(value) else "翻译里没有中文"
    if field == "connection":
        has_sep = "＋" in value or "+" in value
        return None if has_sep else "接续里没有 ＋ 分隔"
    if field == "example":
        if not _KANA_ANY.search(value):
            return "例句里没有假名，看着像中文"
        if entry.type == ENTRY_TYPE_WORD:
            return None if _contains_word(value, entry.word) else "例句里没找到这个词"
        return None if _contains_connection(value, entry) else "例句里没看到接续的后半部分"
    return None


def parse_payload(
    payload: dict[str, Any],
    entry: Entry,
    targets: tuple[str, ...],
    *,
    model: str = "",
    elapsed: float = 0.0,
) -> Enrichment:
    """把模型返回的 JSON 变成 `Enrichment`（纯函数，不联网，方便测试）。

    只认 `targets` 里的键：模型多给的字段一律忽略——防止它顺手改掉学生写好的内容。
    """
    if not isinstance(payload, dict):
        raise ResponseNotJson(f"enrichment payload is not an object: {type(payload).__name__}")

    raw: dict[str, str] = {}
    fields: dict[str, str] = {}
    rejected: dict[str, str] = {}
    pending_examples: list[tuple[str, str]] = []

    for field in targets:
        value = payload.get(field, "")
        if not isinstance(value, str):
            rejected[field] = "模型返回的不是字符串"
            continue
        value = value.strip()
        if not value:
            continue
        raw[field] = value

        # 例句要等其它字段先落定再判：补全常常在同一次返回里把 connection 也补上，
        # 这时"例句有没有体现这个接续"必须按新补的接续来查，而不是按空的旧值。
        if field == "example":
            pending_examples.append((field, value))
            continue

        if field == "part_of_speech":
            normalized = normalize_pos(value)
            if normalized is None:
                rejected[field] = "词性不在预设范围里"
            else:
                fields[field] = normalized
            continue

        reason = _reject_reason(field, value, entry)
        if reason:
            rejected[field] = reason
        else:
            fields[field] = value

    draft = dataclasses.replace(entry, **fields) if fields else entry
    for field, value in pending_examples:
        reason = _reject_reason(field, value, draft)
        if reason:
            rejected[field] = reason
        else:
            fields[field] = value

    return Enrichment(
        fields=fields, rejected=rejected, raw=raw, elapsed=elapsed, model=model
    )


def build_request(entry: Entry, targets: tuple[str, ...], model: str) -> dict[str, Any]:
    """组装一次补全请求（纯函数，不联网）。"""
    context: dict[str, str] = {"类型": entry.type_label}
    for name in _CONTEXT_FIELDS:
        value = getattr(entry, name, "").strip()
        if value:
            context[name] = value

    prompt = build_prompt(entry.type, targets)
    user_content = (
        f"{prompt}\n\n现有记录（这些不要改）：\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )

    return {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": ENRICH_TEMPERATURE,
        "max_tokens": ENRICH_MAX_TOKENS,
        **EXTRA_BODY,
    }


def apply(entry: Entry, enrichment: Enrichment) -> Entry:
    """把补全结果合进记录，并在 `generated_fields` 里记下哪些字段是补的。

    返回新对象，不改入参——调用方可以安全地"先看结果再决定要不要采用"。
    """
    if not enrichment.fields:
        return entry
    merged = dataclasses.replace(entry, **enrichment.fields)
    names = list(dict.fromkeys((*entry.generated_names, *enrichment.names)))
    merged.generated_fields = ",".join(names)
    return merged


def enrich(
    entry: Entry,
    *,
    targets: tuple[str, ...] | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> Enrichment:
    """补全一条记录里空着的字段。失败时抛 `RecognitionError` 的子类。

    `targets` 不传就按"哪些字段是空的"自动算；传了就只补这几个（界面上的
    "重新补全例句"会用它）。
    """
    if entry.type not in ENTRY_TYPES:
        raise ValueError(f"unknown entry_type: {entry.type!r}")

    allowed = FILLABLE_FIELDS[entry.type]
    wanted = tuple(
        name for name in (missing_fields(entry) if targets is None else targets)
        if name in allowed
    )
    if not wanted:
        return Enrichment(fields={}, rejected={}, raw={})

    settings = config.get_settings()
    if not settings.recognition_ready:
        raise MissingApiKey(settings.missing_required)

    model = settings.enrichment_model or ""
    body = build_request(entry, wanted, model)

    started = time.monotonic()
    payload = chat_json(body, timeout=timeout)
    elapsed = time.monotonic() - started
    return parse_payload(payload, entry, wanted, model=model, elapsed=elapsed)