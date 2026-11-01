"""接口探针：确认 base_url / Key / 模型「能不能收图」这件事。

为什么单独写一个脚本，而不是直接用正式代码：正式代码里有提示词、字段校验、
错误包装，一旦失败你看到的是"识别失败，请检查配置"，看不出到底是地址错、
Key 错，还是模型根本不支持图片。探针只做一件事——把请求打出去，把原始响应
原样打印出来。

用法：
    .\\venv\\Scripts\\python.exe scripts\\probe_vision.py
    .\\venv\\Scripts\\python.exe scripts\\probe_vision.py 拍的照片.jpg

不带图片参数时会现造一张 64x64 的小图，只能验证"接口收不收图"，验证不了
"认不认得出日文"。想验证识别质量，请传一张真题照片。
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from PIL import Image, ImageDraw

from app import config

TIMEOUT = (10.0, 60.0)


def make_tiny_image() -> bytes:
    """造一张小 PNG：白底 + 几个黑框，够证明"模型收到了图"。"""
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((12, 12, 52, 52), outline=(0, 0, 0), width=2)
    draw.line((12, 12, 52, 52), fill=(0, 0, 0), width=2)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def mask_key(key: str) -> str:
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]} ({len(key)} 位)"


def main(argv: list[str]) -> int:
    settings = config.reload_settings()

    print("== 配置 ==")
    print(f"  base_url : {settings.base_url}")
    print(f"  model    : {settings.vision_model or '(未设置)'}")
    print(f"  api_key  : {mask_key(settings.api_key) if settings.api_key else '(未设置)'}")
    if not settings.recognition_ready:
        print(f"\n[配置不完整] 缺少: {', '.join(settings.missing_required)}")
        print("请在项目根目录的 .env 里补齐，例如：")
        print("  OPENAI_API_KEY=sk-xxxx")
        print("  OPENAI_BASE_URL=https://api.openai.com/v1")
        print("  VISION_MODEL=gpt-4o-mini")
        return 2

    if len(argv) > 1:
        image_path = Path(argv[1])
        if not image_path.is_file():
            print(f"\n[图片不存在] {image_path}")
            return 2
        raw = image_path.read_bytes()
        suffix = image_path.suffix.lower().lstrip(".") or "jpeg"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "jpeg")
        print(f"\n== 图片 ==\n  {image_path} ({len(raw)} 字节)")
    else:
        raw = make_tiny_image()
        mime = "png"
        print("\n== 图片 ==\n  内置 64x64 测试图（仅验证接口收图能力）")

    data_url = f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")
    url = f"{settings.base_url.rstrip('/')}/chat/completions"
    body = {
        "model": settings.vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图里有什么？用一句中文回答。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 200,
    }

    print(f"\n== 请求 ==\n  POST {url}")
    started = time.time()
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=TIMEOUT,
        )
    except requests.Timeout:
        print(f"  [超时] {TIMEOUT[1]:.0f} 秒内没等到响应（接口不通，或网络被墙）")
        print("  可以先用国内可直连的兼容端点试，例如把 .env 改成：")
        print("    OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1")
        print("    VISION_MODEL=qwen-vl-max")
        return 1
    except requests.RequestException as exc:
        print(f"  [连不上] {type(exc).__name__}: {exc}")
        print("  检查：base_url 是否写全（要带 /v1）、是否需要代理、网络是否通")
        return 1

    elapsed = time.time() - started
    print(f"  HTTP {response.status_code}  用时 {elapsed:.2f}s")

    print("\n== 原始响应 ==")
    print(response.text[:1500] or "(空)")

    print("\n== 结论 ==")
    if response.status_code != 200:
        print("  ✗ 接口返回了错误，上面是服务端的原话，按它改配置。")
        return 1

    try:
        payload = response.json()
    except ValueError:
        print("  ✗ 响应不是 JSON，接口地址可能指向了别的服务。")
        return 1

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print("  ✗ 响应不是 OpenAI 兼容格式，返回里只有这些键: " + str(list(payload)[:8]))
        return 1

    print(f"  ✓ 通了。模型对这张图的回答：{str(content)[:200]}")
    print("\n  → 换成真题照片再跑一次，确认它认得出日文：")
    print(f"    .\\venv\\Scripts\\python.exe scripts\\probe_vision.py your_photo.jpg")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))