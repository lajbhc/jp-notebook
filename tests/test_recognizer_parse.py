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


# ------------------------------------------------------------------- 框选裁剪
def _quad_image(path, size=400):
    """左上红、右上绿、左下蓝、右下白，方便用颜色确认裁的是哪一块。"""
    half = size // 2
    image = Image.new("RGB", (size, size), (255, 255, 255))
    image.paste(Image.new("RGB", (half, half), (220, 30, 30)), (0, 0))
    image.paste(Image.new("RGB", (size - half, half), (30, 160, 60)), (half, 0))
    image.paste(Image.new("RGB", (half, size - half), (30, 60, 200)), (0, half))
    image.save(path, "PNG")
    return path


def _decoded(data_url):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB")


def test_normalize_crop_returns_none_for_no_selection():
    assert recognizer.normalize_crop(None) is None


@pytest.mark.parametrize(
    ("crop", "expected"),
    [
        ((0, 0, 1, 1), (0.0, 0.0, 1.0, 1.0)),
        ((-1, -2, 5, 5), (0.0, 0.0, 1.0, 1.0)),          # 负起点 + 超出宽高
        ((0.8, 0.8, 0.5, 0.5), (0.8, 0.8, 0.2, 0.2)),    # 只夹宽高，不挪起点
        ((0.1, 0.2, 0.3, 0.4), (0.1, 0.2, 0.3, 0.4)),    # 正常值原样返回
    ],
)
def test_normalize_crop_clamps_into_unit_square(crop, expected):
    result = recognizer.normalize_crop(crop)

    assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    "crop",
    [
        (0.5, 0.5, 0.001, 0.5),      # 宽比 CROP_MIN_SIDE 窄
        (0.5, 0.5, 0.5, 0.001),      # 高太窄
        (0.2, 0.2, 0.0, 0.5),        # 宽度为 0
        (float("nan"), 0, 1, 1),     # NaN
        ("a", "b", "c", "d"),        # 根本不是数字
    ],
)
def test_normalize_crop_rejects_unusable_selection(crop):
    with pytest.raises(ImageInvalid) as excinfo:
        recognizer.normalize_crop(crop)

    assert "还原整张" in excinfo.value.user_message


def test_prepare_image_without_crop_keeps_whole_photo(sandbox):
    path = _quad_image(sandbox / "quad.png")

    image = _decoded(recognizer.prepare_image(path))

    assert image.size == (400, 400)
    assert image.getpixel((100, 100))[0] > 180      # 左上红
    assert image.getpixel((300, 100))[1] > 120      # 右上绿


@pytest.mark.parametrize(
    ("crop", "expected_size", "point", "channel"),
    [
        ((0.0, 0.0, 0.5, 0.5), (200, 200), (100, 100), 0),   # 红：R 高
        ((0.5, 0.0, 0.5, 0.5), (200, 200), (100, 100), 1),   # 绿：G 高
        ((0.0, 0.5, 0.5, 0.5), (200, 200), (100, 100), 2),   # 蓝：B 高
    ],
)
def test_prepare_image_crops_to_selected_region(sandbox, crop, expected_size, point, channel):
    path = _quad_image(sandbox / "quad.png")

    image = _decoded(recognizer.prepare_image(path, crop))

    assert image.size == expected_size
    pixel = image.getpixel(point)
    assert pixel[channel] == max(pixel), pixel


def test_prepare_image_crop_still_respects_max_edge(sandbox):
    """先裁再缩：裁出 2000x1000 之后照样要缩到长边 1600 以内。"""
    path = sandbox / "wide.png"
    image = Image.new("RGB", (4000, 2000), (255, 255, 255))
    image.paste(Image.new("RGB", (2000, 1000), (220, 30, 30)), (0, 0))
    image.save(path, "PNG")

    cropped = _decoded(recognizer.prepare_image(path, (0.0, 0.0, 0.5, 0.5)))

    assert max(cropped.size) <= recognizer.MAX_IMAGE_EDGE
    assert cropped.getpixel((cropped.width // 2, cropped.height // 2))[0] > 180


def _save_rotated_jpeg(path):
    """原始 400x200（左红右绿），EXIF 写 Orientation=6 → 显示时应为 200x400（上红下绿）。"""
    image = Image.new("RGB", (400, 200), (220, 30, 30))
    image.paste(Image.new("RGB", (200, 200), (30, 160, 60)), (200, 0))
    exif = image.getexif()
    exif[274] = 6
    image.save(path, "JPEG", exif=exif)
    return path


def test_prepare_image_applies_exif_orientation(sandbox):
    """EXIF 说"转 90 度"就必须真转——不能把带方向标记的原始字节直接发出去。"""
    path = _save_rotated_jpeg(sandbox / "rotated.jpg")

    image = _decoded(recognizer.prepare_image(path))

    assert image.size == (200, 400)
    assert image.getpixel((100, 100))[0] > 180      # 上半红
    assert image.getpixel((100, 300))[1] > 120      # 下半绿


def test_prepare_image_crops_in_upright_space(sandbox):
    """框选比例是相对"摆正后的图"算的，裁出来必须上下对得上。"""
    path = _save_rotated_jpeg(sandbox / "rotated.jpg")

    top = _decoded(recognizer.prepare_image(path, (0.0, 0.0, 1.0, 0.5)))
    bottom = _decoded(recognizer.prepare_image(path, (0.0, 0.5, 1.0, 0.5)))

    assert top.size == (200, 200)
    assert top.getpixel((100, 100))[0] == max(top.getpixel((100, 100)))
    assert bottom.getpixel((100, 100))[1] == max(bottom.getpixel((100, 100)))