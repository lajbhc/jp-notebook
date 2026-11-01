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

# 只有这些格式交给 Pillow 解码，其余一律按"打不开"处理。
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF"}

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
def prepare_image(image_path: str | Path) -> str:
    """读图 → 校正方向 → 必要时缩小 → 返回 data URL。"""
    path = Path(image_path)
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

            # 手机拍的照片方向信息藏在 EXIF 里，不校正会横躺着送进模型
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            if width <= 0 or height <= 0:
                raise ImageInvalid(f"bad image size: {width}x{height}")

            longest = max(width, height)
            if longest > MAX_IMAGE_EDGE:
                img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
                need_reencode = True
            else:
                need_reencode = detected not in ("JPEG", "PNG") or size > KEEP_ORIGINAL_BYTES

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
    """从 chat/completions 的返回里取出正文。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ResponseNotJson(f"response has no choices: {list(payload)[:6]}")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None

    if isinstance(content, list):
        # 少数接口把正文拆成 [{type: text, text: ...}] 的数组
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise ResponseNotJson(f"response content is empty: {str(content)[:120]}")
    return content


# --------------------------------------------------------------------- 调用
def recognize(
    image_path: str | Path,
    entry_type: str,
    *,
    source_image: str = "",
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> Entry:
    """识别一张图片。失败时抛 `RecognitionError` 的子类。"""
    if entry_type not in ENTRY_TYPES:
        raise ValueError(f"unknown entry_type: {entry_type!r}")

    settings = config.get_settings()
    if not settings.recognition_ready:
        raise MissingApiKey(settings.missing_required)

    data_url = prepare_image(image_path)

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
        "max_tokens": 800,
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.base_url.rstrip('/')}/chat/completions"

    try:
        response = requests.post(url, headers=headers, json=request_body, timeout=timeout)
    except requests.Timeout as exc:
        raise NetworkTimeout(f"timeout talking to {url}: {exc}") from exc
    except requests.RequestException as exc:
        raise ApiRequestFailed(None, f"{type(exc).__name__}: {exc}") from exc

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