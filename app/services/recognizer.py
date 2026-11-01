"""多模态识别服务：图片进，`Entry` 出。

这一层只做三件事：
1. 把图片检查、缩小、编码成 data URL（手机拍的原图动辄 3~5MB、4000px 宽，
   直接发既慢又容易超出接口限制）。
2. 调 OpenAI 兼容的 /chat/completions。
3. 把返回的自由文本解析成 JSON，再白名单过滤成 `Entry`。

两条重要约定：
- `type`、`source_image`、时间戳一律由**本地**决定，不信模型返回的内容。
- 解析失败不返回半个对象，直接抛 `RecognitionError`，让上层决定提示什么。
"""

from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from app import config
from app.models import ENTRY_TYPES, Entry
from app.prompts.recognize import build_prompt
from app.services.errors import (
    ApiRequestFailed,
    ImageInvalid,
    MissingApiKey,
    NetworkTimeout,
    ResponseNotJson,
)

__all__ = [
    "MAX_IMAGE_EDGE",
    "CROP_MIN_SIDE",
    "normalize_crop",
    "prepare_image",
    "extract_json_object",
    "build_entry",
    "recognize",
]

# 长边超过这个值就等比缩小。1600 对印刷体日语足够，再大只是浪费带宽和 token。
MAX_IMAGE_EDGE = 1600
# 单张图片上限，超过直接提示，不去赌接口的宽容度。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 小图不重新编码，避免二次压缩损失细节。
KEEP_ORIGINAL_BYTES = 6 * 1024 * 1024
# 输出额度。思考型模型（先输出 reasoning_content 再输出正文）会先把额度花在
# 思考上，设小了正文会被截断成空字符串——这是实测踩到的坑，别往下调。
MAX_TOKENS = 2000

# 关闭思考的额外参数。实测这个端点支持 enable_thinking=False：
#   开着：同一张图思考 939~3202 字不等，5~9 秒，且有时会把 max_tokens 吃光导致正文为空
#   关掉：稳定 2.3~2.5 秒，不产生 reasoning token，正文一次成功
# 换到不认这个参数的端点会返回 400，此时 recognize() 会自动去掉它重试一次。
EXTRA_BODY: dict[str, Any] = {"enable_thinking": False}

# 只有这些格式交给 Pillow 解码，其余一律按"打不开"处理。
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF"}

# 框选区域（归一化）每条边至少占原图这么多。再小就当用户手抖，直接提示重框，
# 免得把一条缝送给模型、再让用户面对一堆瞎猜出来的字段。
CROP_MIN_SIDE = 0.02

# 允许从模型返回里取用的字段（与 entries 表的列一一对应）。
_TEXT_FIELDS = (
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

DEFAULT_TIMEOUT = (10.0, 120.0)  # (连接超时, 读取超时)


# --------------------------------------------------------------------- 图片
def normalize_crop(
    crop: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """把界面给的框选区域收拾干净。

    约定：`(x, y, w, h)` 都是 0~1 的比例，相对"原图"（不是屏幕上缩放后的预览）。
    越界往里夹，比例小于 `CROP_MIN_SIDE` 的直接报错——框得太小手一抖就点到空白处，
    与其让模型对着一条缝瞎猜，不如让用户重拖。
    """
    if crop is None:
        return None
    try:
        x, y, w, h = (float(value) for value in crop)
    except (TypeError, ValueError) as exc:
        raise ImageInvalid(
            f"bad crop: {crop!r}: {exc}",
            "框选区域不对劲，重新拖一个框，或者点「还原整张」识别整张图。",
        ) from exc

    if not all(math.isfinite(value) for value in (x, y, w, h)):
        raise ImageInvalid(
            f"bad crop: {crop!r}",
            "框选区域不对劲，重新拖一个框，或者点「还原整张」识别整张图。",
        )

    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0 - x)
    h = min(max(h, 0.0), 1.0 - y)
    if w < CROP_MIN_SIDE or h < CROP_MIN_SIDE:
        raise ImageInvalid(
            f"crop too small: {crop!r}",
            "框选的区域太小了。重新拖一个大一点的框，或者点「还原整张」识别整张图。",
        )
    return (x, y, w, h)


def prepare_image(
    image_path: str | Path,
    crop: tuple[float, float, float, float] | None = None,
) -> str:
    """读图 → 校正方向 →（可选）裁出框选区域 → 必要时缩小 → 返回 data URL。"""
    path = Path(image_path)
    box = normalize_crop(crop)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImageInvalid(f"cannot stat {path}: {exc}") from exc

    if size == 0:
        raise ImageInvalid(f"empty file: {path}")
    if size > MAX_IMAGE_BYTES:
        raise ImageInvalid(f"file too large: {size} bytes")

    try:
        with Image.open(path) as img:
            img.load()  # 强制真正解码，截断的文件在这一步就会抛错
            detected = (img.format or "").upper()
            if detected not in ALLOWED_FORMATS:
                raise ImageInvalid(f"unsupported format: {detected or 'unknown'}")

            # 手机拍的照片方向信息藏在 EXIF 里，不校正会横躺着送进模型。
            # 注意：只要 EXIF 里写了方向，就必须重新编码——否则下面会走"原样发原文件"
            # 的捷径，把带方向标记的字节丢给模型，而模型多半不看 EXIF，等于没校正。
            orientation = img.getexif().get(274, 1)
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            if width <= 0 or height <= 0:
                raise ImageInvalid(f"bad image size: {width}x{height}")

            if box is not None:
                left = min(max(int(round(box[0] * width)), 0), width - 1)
                top = min(max(int(round(box[1] * height)), 0), height - 1)
                right = min(max(int(round((box[0] + box[2]) * width)), left + 1), width)
                bottom = min(max(int(round((box[1] + box[3]) * height)), top + 1), height)
                img = img.crop((left, top, right, bottom))
                # 裁出来的是一块新图，不能再偷懒直接发原文件字节
                need_reencode = True
            else:
                need_reencode = bool(orientation and orientation != 1)

            longest = max(img.size)
            if longest > MAX_IMAGE_EDGE:
                img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
                need_reencode = True
            else:
                need_reencode = need_reencode or (
                    detected not in ("JPEG", "PNG") or size > KEEP_ORIGINAL_BYTES
                )

            if not need_reencode:
                mime = "image/png" if detected == "PNG" else "image/jpeg"
                return _to_data_url(path.read_bytes(), mime)

            if img.mode not in ("RGB", "L"):
                # 带透明通道的图存成 JPEG 会报错，先铺白底
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode in ("RGBA", "LA", "P"):
                    converted = img.convert("RGBA")
                    background.paste(converted, mask=converted.split()[-1])
                    img = background
                else:
                    img = img.convert("RGB")
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85, optimize=True)
            return _to_data_url(buffer.getvalue(), "image/jpeg")
    except ImageInvalid:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageInvalid(f"cannot open image {path.name}: {exc}") from exc


def _to_data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------- 解析
def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏。很多兼容接口不认 response_format，只能自己剥。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里挖出一个 JSON 对象。挖不到就抛 ResponseNotJson。"""
    if not isinstance(text, str) or not text.strip():
        raise ResponseNotJson("empty model content")

    candidates = [_strip_code_fence(text)]
    start, end = candidates[0].find("{"), candidates[0].rfind("}")
    if start != -1 and end > start:
        candidates.append(candidates[0][start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ResponseNotJson(f"cannot parse json, text starts with: {text.strip()[:200]}")


def build_entry(payload: dict[str, Any], entry_type: str, source_image: str = "") -> Entry:
    """白名单过滤模型返回的字段。type / source_image 由本地决定。"""
    if entry_type not in ENTRY_TYPES:
        raise ValueError(f"unknown entry_type: {entry_type!r}")

    values: dict[str, str] = {}
    for field in _TEXT_FIELDS:
        raw = payload.get(field, "")
        # 模型有时会把字段返回成列表或数字，这里一律丢弃而不是硬转字符串
        values[field] = raw.strip() if isinstance(raw, str) else ""

    return Entry(type=entry_type, source_image=source_image, **values)


def _content_text(payload: dict[str, Any]) -> str:
    """从 chat/completions 的返回里取出正文。

    注意：思考型模型会先输出 `reasoning_content`，正文放在 `content`。如果正文是
    空的且 `finish_reason == "length"`，说明输出额度被思考过程吃光了——这不是模型
    坏了，是我们的 max_tokens 给小了，所以单独给一句明确的提示，别让用户以为是自己
    的图片有问题。
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ResponseNotJson(f"response has no choices: {list(payload)[:6]}")

    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    finish_reason = choice.get("finish_reason")

    content = message.get("content")
    if isinstance(content, list):
        # 少数接口把正文拆成 [{type: text, text: ...}] 的数组
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )

    if not isinstance(content, str) or not content.strip():
        reasoning = message.get("reasoning_content") or ""
        reasoning_len = len(reasoning) if isinstance(reasoning, str) else 0
        detail = (
            f"empty content; finish_reason={finish_reason}; "
            f"reasoning_chars={reasoning_len}; usage={payload.get('usage')}"
        )
        if finish_reason == "length":
            raise ResponseNotJson(
                detail,
                "模型把输出额度全用在思考过程上了，没能给出结果。重试一次；"
                "如果反复出现，说明这个模型思考太长，建议换一个非思考的视觉模型。",
            )
        raise ResponseNotJson(detail)

    return content


# --------------------------------------------------------------------- 调用
def _post(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: tuple[float, float]
) -> requests.Response:
    """发一次请求，把 requests 的异常翻译成本项目的错误类型。"""
    try:
        return requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout as exc:
        raise NetworkTimeout(f"timeout talking to {url}: {exc}") from exc
    except requests.RequestException as exc:
        raise ApiRequestFailed(None, f"{type(exc).__name__}: {exc}") from exc


def recognize(
    image_path: str | Path,
    entry_type: str,
    *,
    crop: tuple[float, float, float, float] | None = None,
    source_image: str = "",
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> Entry:
    """识别一张图片。失败时抛 `RecognitionError` 的子类。

    `crop` 是界面上框出来的区域（归一化 `(x, y, w, h)`，相对原图）。
    传 None 就识别整张。
    """
    if entry_type not in ENTRY_TYPES:
        raise ValueError(f"unknown entry_type: {entry_type!r}")

    settings = config.get_settings()
    if not settings.recognition_ready:
        raise MissingApiKey(settings.missing_required)

    data_url = prepare_image(image_path, crop)

    request_body = {
        "model": settings.vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(entry_type)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
        **EXTRA_BODY,
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.base_url.rstrip('/')}/chat/completions"

    response = _post(url, headers, request_body, timeout)

    # 有的 OpenAI 兼容端点不认识 enable_thinking，会直接 400。
    # 这时候把额外参数去掉再试一次，不要让用户因为一个可选项就完全用不了识别。
    if response.status_code == 400 and EXTRA_BODY:
        fallback = {k: v for k, v in request_body.items() if k not in EXTRA_BODY}
        retry = _post(url, headers, fallback, timeout)
        if retry.status_code == 200:
            response = retry

    if response.status_code != 200:
        raise ApiRequestFailed(response.status_code, _body_snippet(response))

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseNotJson(f"body is not json: {response.text[:200]}") from exc

    entry = build_entry(extract_json_object(_content_text(payload)), entry_type, source_image)
    return entry


def _body_snippet(response: requests.Response, limit: int = 300) -> str:
    try:
        return response.text[:limit]
    except Exception:  # noqa: BLE001 - 只为记日志，不允许它再抛
        return "<unreadable body>"