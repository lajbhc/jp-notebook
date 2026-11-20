"""补全服务的纯逻辑测试（不发请求、不碰数据库）。

网络与错误分支在 `scripts/verify_enrich.py` 里用假接口跑；这里只保证
"哪些字段要补、补出来的内容怎么判、怎么合并"这几条规则不被后续改动破坏。
"""

from __future__ import annotations

from app import models
from app.services import enricher

WORD_FILLED = {
    "reading": "たべる",
    "meaning": "吃",
    "part_of_speech": "动词・一段",
    "example": "ご飯を食べました。",
    "example_cn": "吃了饭。",
}


def _word(**overrides) -> models.Entry:
    base = dict(type=models.ENTRY_TYPE_WORD, word="食べる")
    base.update(overrides)
    return models.Entry(**base)


def _grammar(**overrides) -> models.Entry:
    base = dict(
        type=models.ENTRY_TYPE_GRAMMAR,
        grammar_point="〜ことになる",
        connection="動詞辞書形 ＋ ことになる",
    )
    base.update(overrides)
    return models.Entry(**base)


def test_missing_fields_follow_entry_type() -> None:
    assert enricher.missing_fields(_word()) == (
        "reading",
        "meaning",
        "part_of_speech",
        "example",
        "example_cn",
    )
    assert enricher.missing_fields(_grammar(connection="")) == (
        "meaning",
        "connection",
        "example",
        "example_cn",
    )
    assert enricher.missing_fields(_word(**WORD_FILLED)) == ()
    assert enricher.missing_fields(models.Entry(type="other")) == ()


def test_normalize_pos_maps_common_spellings() -> None:
    assert enricher.normalize_pos("动词・一段") == "动词・一段"
    assert enricher.normalize_pos("動詞（一段）") == "动词・一段"
    assert enricher.normalize_pos("ichidan verb") == "动词・一段"
    assert enricher.normalize_pos("godan") == "动词・五段"
    assert enricher.normalize_pos("ナ形容詞") == "ナ形容词"
    assert enricher.normalize_pos("名詞") == "名词"
    # 只写「動詞」说不清是哪一类，宁可让用户自己选，也不要猜
    assert enricher.normalize_pos("動詞") is None
    assert enricher.normalize_pos("") is None


def test_parse_payload_keeps_only_targets_and_strips() -> None:
    payload = {"meaning": "  吃  ", "word": "書き換え", "example": "ご飯を食べました。"}
    result = enricher.parse_payload(payload, _word(), ("meaning",))
    assert result.fields == {"meaning": "吃"}
    assert result.rejected == {}


def test_parse_payload_rejects_bad_values() -> None:
    result = enricher.parse_payload(
        {
            "reading": "taberu",
            "meaning": "eat",
            "part_of_speech": "動詞",
            "example": "これはペンです。",
            "example_cn": "这是笔。",
        },
        _word(),
        ("reading", "meaning", "part_of_speech", "example", "example_cn"),
    )
    assert set(result.rejected) == {"reading", "meaning", "part_of_speech", "example"}
    assert result.fields == {"example_cn": "这是笔。"}
    assert "没过检查" in result.summary


def test_parse_payload_accepts_conjugated_example() -> None:
    for example in ("毎日、野菜を食べます。", "ご飯を食べました。", "食べるのが好きだ。"):
        result = enricher.parse_payload({"example": example}, _word(), ("example",))
        assert result.fields.get("example") == example


def test_parse_payload_accepts_grammar_example_in_past_tense() -> None:
    # 学生笔记里几乎总是过去式「ことになった」，本地检查要认
    for example in (
        "来月、大阪へ転勤することになった。",
        "来月、大阪へ転勤することになります。",
    ):
        result = enricher.parse_payload({"example": example}, _grammar(), ("example",))
        assert result.fields.get("example") == example, result.rejected


def test_grammar_connection_needs_plus_sign() -> None:
    bad = enricher.parse_payload({"connection": "動詞辞書形"}, _grammar(), ("connection",))
    assert "connection" not in bad.fields
    assert "＋" in bad.rejected["connection"]

    good = enricher.parse_payload(
        {"connection": "動詞辞書形 ＋ ことになる"}, _grammar(), ("connection",)
    )
    assert good.fields["connection"] == "動詞辞書形 ＋ ことになる"


def test_apply_marks_generated_fields_and_keeps_input_intact() -> None:
    original = _word(reading="たべる")
    result = enricher.parse_payload(
        {"meaning": "吃", "example": "ご飯を食べました。"},
        original,
        ("meaning", "example"),
    )
    merged = enricher.apply(original, result)

    assert merged is not original
    assert merged.reading == "たべる"  # 原有的不动
    assert merged.meaning == "吃"
    assert merged.generated_names == ("example", "meaning")
    assert original.generated_fields == ""


def test_apply_does_not_duplicate_names() -> None:
    once = enricher.apply(
        _word(), enricher.parse_payload({"meaning": "吃"}, _word(), ("meaning",))
    )
    twice = enricher.apply(
        once, enricher.parse_payload({"meaning": "吃（他动词）"}, once, ("meaning",))
    )
    assert twice.generated_fields == "meaning"
    assert twice.meaning == "吃（他动词）"


def test_apply_with_empty_result_returns_same_object() -> None:
    entry = _word()
    empty = enricher.Enrichment(fields={}, rejected={}, raw={})
    assert enricher.apply(entry, empty) is entry


def test_build_request_is_text_only_and_lists_targets() -> None:
    import json

    body = enricher.build_request(_word(), ("example", "example_cn"), "m")
    content = body["messages"][0]["content"]

    assert body["model"] == "m"
    assert body["temperature"] == enricher.ENRICH_TEMPERATURE
    assert body["max_tokens"] == enricher.ENRICH_MAX_TOKENS
    assert "example、example_cn" in content
    assert "食べる" in content
    assert "image_url" not in json.dumps(body, ensure_ascii=False)