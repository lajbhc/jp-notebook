"""识别层里"不联网就能测"的部分：剥围栏、取 JSON、字段白名单、图片预处理。

真调接口的验收在 `scripts/verify_recognition.py`（本地假服务器），这里只测纯函数，
所以 CI 里跑它不需要网络，也不会花钱。
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app import models
from app.services import recognizer
from app.services.errors import ImageInvalid, ResponseNotJson


# ------------------------------------------------------------------- 剥围栏
@pytest.mark.parametrize(
    "raw",
    [
        '{"word": "食べる"}',
        '```json\n{"word": "食べる"}\n```',
        '```\n{"word": "食べる"}\n```',
        '好的，结果如下：\n{"word": "食べる"}\n希望有帮助。',
        '  \n\n{"word": "食べる"}  \n',
    ],
)
def test_extract_json_object_accepts_common_shapes(raw):
    assert recognizer.extract_json_object(raw) == {"word": "食べる"}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "抱歉，我看不清这张图。",
        "[1, 2, 3]",  # 是 JSON，但不是对象
        "```json\n{不是合法 JSON}\n```",
    ],
)
def test_extract_json_object_rejects_garbage(raw):
    with pytest.raises(ResponseNotJson):
        recognizer.extract_json_object(raw)


# --------------------------------------------------------------- 字段白名单
def test_build_entry_keeps_only_known_text_fields():
    payload = {
        "word": "食べる",
        "reading": "たべる",
        "meaning": 42,  # 不是字符串，丢掉
        "part_of_speech": "動詞",
        "extra_field": "模型自己编的字段",
        "confidence": 0.98,
        "id": 1234,  # 模型不该决定 id
        "created_at": "1999-01-01 00:00:00",
    }

    entry = recognizer.build_entry(payload, models.ENTRY_TYPE_WORD, source_image="2026-11-01/a.jpg")

    assert entry.word == "食べる"
    assert entry.part_of_speech == "動詞"
    assert entry.meaning == ""  # 非字符串被丢弃，不是 "42"
    assert entry.source_image == "2026-11-01/a.jpg"
    assert entry.created_at == ""  # 时间戳由本地写，不采信模型
    assert not hasattr(entry, "extra_field")


def test_build_entry_type_comes_from_caller_not_model():
    payload = {"word": "食べる", "type": "grammar"}

    entry = recognizer.build_entry(payload, models.ENTRY_TYPE_WORD)

    assert entry.type == models.ENTRY_TYPE_WORD


def test_build_entry_strips_whitespace():
    payload = {"word": "  食べる \n", "reading": "\tたべる "}

    entry = recognizer.build_entry(payload, models.ENTRY_TYPE_WORD)

    assert entry.word == "食べる"
    assert entry.reading == "たべる"


# --------------------------------------------------------------- 响应正文提取
def test_content_text_reads_normal_message():
    payload = {"choices": [{"message": {"content": '{"word": "食べる"}'}}]}

    assert recognizer._content_text(payload) == '{"word": "食べる"}'


def test_content_text_explains_thinking_model_ate_the_budget():
    payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "", "reasoning_content": "思考" * 100},
            }
        ],
        "usage": {"completion_tokens": 2000},
    }

    with pytest.raises(ResponseNotJson) as info:
        recognizer._content_text(payload)

    assert "思考" in info.value.user_message
    assert "length" in info.value.detail  # 详情里带着诊断信息


def test_content_text_rejects_empty_without_length_flag():
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}

    with pytest.raises(ResponseNotJson):
        recognizer._content_text(payload)


def test_content_text_rejects_payload_without_choices():
    with pytest.raises(ResponseNotJson):
        recognizer._content_text({"error": {"message": "bad request"}})


# --------------------------------------------------------------- 图片预处理
def _save_jpeg(path, size=(400, 300)):
    Image.new("RGB", size, "white").save(path, "JPEG")
    return path


def test_prepare_image_returns_data_url(sandbox):
    path = _save_jpeg(sandbox / "small.jpg")

    data_url = recognizer.prepare_image(path)

    assert data_url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    assert Image.open(io.BytesIO(raw)).size == (400, 300)


def test_prepare_image_shrinks_huge_photos(sandbox):
    path = _save_jpeg(sandbox / "big.jpg", size=(3200, 1600))

    data_url = recognizer.prepare_image(path)
    raw = base64.b64decode(data_url.split(",", 1)[1])

    assert max(Image.open(io.BytesIO(raw)).size) <= recognizer.MAX_IMAGE_EDGE


def test_prepare_image_flattens_transparency_to_white(sandbox):
    """需要重编码的大图：透明区域必须铺成白底，不能压成黑块。"""
    path = sandbox / "alpha.png"
    Image.new("RGBA", (2000, 1200), (0, 0, 0, 0)).save(path, "PNG")

    data_url = recognizer.prepare_image(path)
    assert data_url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    pixel = Image.open(io.BytesIO(raw)).convert("RGB").getpixel((5, 5))

    assert all(channel > 250 for channel in pixel), pixel


def test_prepare_image_rejects_missing_file(sandbox):
    with pytest.raises(ImageInvalid):
        recognizer.prepare_image(sandbox / "没有这个文件.jpg")


def test_prepare_image_rejects_text_file(sandbox):
    path = sandbox / "note.txt"
    path.write_text("这不是图片", encoding="utf-8")

    with pytest.raises(ImageInvalid):
        recognizer.prepare_image(path)