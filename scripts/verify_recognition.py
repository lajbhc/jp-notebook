"""阶段 2 验收脚本：不联网、不花额度，验证识别的「图片处理 + 解析 + 错误映射」。

真实的多模态调用需要 API Key，用 scripts/probe_vision.py 单独验证。
这里用一个本地假服务（127.0.0.1 随机端口）冒充识别接口，把 200/401/404/500、
非 JSON、连不上这些分支都过一遍——因为真实接口不会配合你演错误。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_recognition.py
"""

from __future__ import annotations

import base64
import contextlib
import http.server
import io
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app import config, models
from app.services import recognizer
from app.services.errors import (
    ApiRequestFailed,
    ImageInvalid,
    MissingApiKey,
    NetworkTimeout,
    ResponseNotJson,
)

WORK_DIR = config.DATA_DIR / "_verify_images"
_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [OK] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


def expect_error(name: str, exc_type: type[BaseException], func, *contains: str) -> None:
    try:
        func()
    except exc_type as exc:
        missing = [word for word in contains if word not in str(getattr(exc, "user_message", exc))]
        if missing:
            check(name, False, f"提示里缺少 {missing}，实际是：{getattr(exc, 'user_message', exc)}")
        else:
            check(name, True)
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"抛出的是 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有抛出异常")


# ------------------------------------------------------------------ 假接口
class _FakeHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.server.last_request = {  # type: ignore[attr-defined]
            "path": self.path,
            "headers": dict(self.headers),
            "body": raw,
        }
        status, content_type, body = self.server.route  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音，测试输出才干净
        pass


def start_fake_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    server.route = (200, "application/json", b"{}")  # type: ignore[attr-defined]
    server.last_request = {}  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def openai_payload(content: str) -> tuple[int, str, bytes]:
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return 200, "application/json", json.dumps(body, ensure_ascii=False).encode("utf-8")


@contextlib.contextmanager
def use_settings(base_url: str, api_key: str = "test-key", vision_model: str = "test-model"):
    """临时替换 config.get_settings，避免读真实 .env。"""
    original = config.get_settings
    config.get_settings = lambda: config.Settings(  # type: ignore[assignment]
        api_key=api_key or None,
        base_url=base_url,
        vision_model=vision_model or None,
        upload_port=8765,
        reminder_time="21:00",
    )
    try:
        yield
    finally:
        config.get_settings = original  # type: ignore[assignment]


def make_image(name: str, size: tuple[int, int], mode: str = "RGB", fmt: str = "JPEG") -> Path:
    path = WORK_DIR / name
    Image.new(mode, size, (210, 210, 210) if mode == "RGB" else (210, 210, 210, 255)).save(
        path, format=fmt
    )
    return path


def main() -> int:
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 1
    print("\n== 1. 图片预处理 ==")
    big = make_image("big.jpg", (3000, 2000))
    url = recognizer.prepare_image(big)
    check("data URL 前缀正确", url.startswith("data:image/jpeg;base64,"))
    raw = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as img:
        check("大图被等比缩小到长边 <= 1600", max(img.size) == recognizer.MAX_IMAGE_EDGE, str(img.size))
        check("缩小后宽高比不变（3:2）", abs(img.size[0] / img.size[1] - 1.5) < 0.01, str(img.size))

    small = make_image("small.png", (600, 400), fmt="PNG")
    url = recognizer.prepare_image(small)
    check("小图标记为 png", url.startswith("data:image/png;base64,"))
    check("小图不做二次编码（字节完全一致）", base64.b64decode(url.split(",", 1)[1]) == small.read_bytes())

    rgba = WORK_DIR / "alpha.png"
    Image.new("RGBA", (2000, 1000), (255, 0, 0, 128)).save(rgba, format="PNG")
    url = recognizer.prepare_image(rgba)
    decoded = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(decoded)) as img:
        check("透明图被转成 JPEG", url.startswith("data:image/jpeg;base64,"))
        check("透明图转码后是 RGB（不会有 alpha 通道）", img.mode == "RGB", img.mode)

    broken = WORK_DIR / "broken.jpg"
    broken.write_bytes(b"this is definitely not an image")
    expect_error("坏图片抛 ImageInvalid", ImageInvalid, lambda: recognizer.prepare_image(broken))
    expect_error("不存在的文件抛 ImageInvalid", ImageInvalid, lambda: recognizer.prepare_image(WORK_DIR / "nope.jpg"))
    empty = WORK_DIR / "empty.jpg"
    empty.write_bytes(b"")
    expect_error("空文件抛 ImageInvalid", ImageInvalid, lambda: recognizer.prepare_image(empty))

    original_limit = recognizer.MAX_IMAGE_BYTES
    recognizer.MAX_IMAGE_BYTES = 10
    try:
        expect_error("超过体积上限抛 ImageInvalid", ImageInvalid, lambda: recognizer.prepare_image(small))
    finally:
        recognizer.MAX_IMAGE_BYTES = original_limit

    # ---------------------------------------------------------------- 2
    print("\n== 2. 模型输出解析 ==")
    plain = '{"word": "食べる", "reading": "たべる"}'
    check("裸 JSON 能解析", recognizer.extract_json_object(plain)["word"] == "食べる")

    fenced = '```json\n{"word": "食べる"}\n```'
    check("```json 围栏能剥掉", recognizer.extract_json_object(fenced)["word"] == "食べる")

    inline = '```json{"word": "食べる"}```'
    check("单行围栏能剥掉", recognizer.extract_json_object(inline)["word"] == "食べる")

    chatty = '好的，识别结果如下：\n{"word": "食べる"}\n希望有帮助。'
    check("前后有废话也能挖出 JSON", recognizer.extract_json_object(chatty)["word"] == "食べる")

    expect_error("纯文字抛 ResponseNotJson", ResponseNotJson, lambda: recognizer.extract_json_object("我识别不出来"))
    expect_error("空字符串抛 ResponseNotJson", ResponseNotJson, lambda: recognizer.extract_json_object("   "))
    expect_error("JSON 数组抛 ResponseNotJson", ResponseNotJson, lambda: recognizer.extract_json_object("[1, 2, 3]"))
    expect_error("截断的 JSON 抛 ResponseNotJson", ResponseNotJson, lambda: recognizer.extract_json_object('{"word": "食'))

    # ---------------------------------------------------------------- 3
    print("\n== 3. 字段白名单 ==")
    dirty = {
        "word": "食べる",
        "reading": "  たべる  ",
        "type": "grammar",            # 模型乱填的类型必须被忽略
        "source_image": "/etc/passwd",  # 模型乱填的路径必须被忽略
        "id": 123,
        "meaning": ["吃"],             # 非字符串字段必须丢弃
        "part_of_speech": "动词・一段",
        "unknown_field": "不该出现",
    }
    entry = recognizer.build_entry(dirty, models.ENTRY_TYPE_WORD, source_image="2026-11-01/ab12.jpg")
    check("type 由本地参数决定", entry.type == models.ENTRY_TYPE_WORD, entry.type)
    check("source_image 由本地参数决定", entry.source_image == "2026-11-01/ab12.jpg", entry.source_image)
    check("文本字段去掉首尾空白", entry.reading == "たべる", repr(entry.reading))
    check("非字符串字段丢弃为空串", entry.meaning == "", repr(entry.meaning))
    check("未知字段不进对象", not hasattr(entry, "unknown_field"))
    check("id 不会被模型覆盖", entry.id is None, str(entry.id))

    expect_error(
        "未知 entry_type 抛 ValueError",
        ValueError,
        lambda: recognizer.build_entry({}, "phrase"),
    )

    # ---------------------------------------------------------------- 4
    print("\n== 4. 响应结构 ==")
    check("标准 choices 结构能取出正文", recognizer._content_text(
        {"choices": [{"message": {"content": "hi"}}]}
    ) == "hi")
    check("数组形式的 content 能拼接", recognizer._content_text(
        {"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}]}
    ) == "ab")
    expect_error("缺 choices 抛 ResponseNotJson", ResponseNotJson, lambda: recognizer._content_text({}))
    expect_error(
        "content 为空抛 ResponseNotJson",
        ResponseNotJson,
        lambda: recognizer._content_text({"choices": [{"message": {"content": "  "}}]}),
    )

    # ---------------------------------------------------------------- 5
    print("\n== 5. 端到端（本地假接口）==")
    server, port = start_fake_server()
    base = f"http://127.0.0.1:{port}"
    image = small

    good_json = '{"word": "食べる", "reading": "たべる", "meaning": "吃", "part_of_speech": "动词・一段", "example": "ご飯を食べる。", "error_reason": "自他动词搞混"}'
    server.route = openai_payload(good_json)  # type: ignore[attr-defined]
    with use_settings(base):
        result = recognizer.recognize(image, models.ENTRY_TYPE_WORD, source_image="a/b.jpg")
    check("正常返回得到 Entry", result.word == "食べる" and result.meaning == "吃")
    check("请求发到了 /chat/completions", server.last_request["path"] == "/chat/completions")  # type: ignore[index]
    sent = json.loads(server.last_request["body"].decode("utf-8"))  # type: ignore[index]
    check("请求体带上了模型名", sent["model"] == "test-model")
    check("请求体把图片以 data URL 发送", sent["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/"))
    check("请求体里没有明文 Key", "test-key" not in json.dumps(sent, ensure_ascii=False))
    check(
        "Key 放在 Authorization 头里",
        server.last_request["headers"].get("Authorization") == "Bearer test-key",  # type: ignore[index]
    )
    text_part = sent["messages"][0]["content"][0]["text"]
    check("生词提示词已带入", "生词" in text_part)
    check("提示词要求只输出 JSON", "JSON" in text_part)

    server.route = (401, "application/json", b'{"error":{"message":"Invalid API key"}}')  # type: ignore[attr-defined]
    with use_settings(base):
        expect_error("401 提示检查 API Key", ApiRequestFailed, lambda: recognizer.recognize(image, "word"), "API Key")

    server.route = (404, "application/json", b'{"error":"not found"}')  # type: ignore[attr-defined]
    with use_settings(base):
        expect_error("404 提示检查地址或模型名", ApiRequestFailed, lambda: recognizer.recognize(image, "word"), "接口地址或模型名")

    server.route = (500, "text/plain", b"internal error")  # type: ignore[attr-defined]
    with use_settings(base):
        expect_error("500 提示服务端出错", ApiRequestFailed, lambda: recognizer.recognize(image, "word"), "服务端")

    server.route = (200, "text/html", b"<html>hello</html>")  # type: ignore[attr-defined]
    with use_settings(base):
        expect_error("返回 HTML 抛 ResponseNotJson", ResponseNotJson, lambda: recognizer.recognize(image, "word"))

    server.route = openai_payload("我无法识别这张图片")  # type: ignore[attr-defined]
    with use_settings(base):
        expect_error("模型只说人话时抛 ResponseNotJson", ResponseNotJson, lambda: recognizer.recognize(image, "word"))

    server.route = openai_payload('```json\n{"grammar_point": "〜ことになる", "connection": "動詞辞書形 + ことになる", "meaning": "客观上决定"}\n```')  # type: ignore[attr-defined]
    with use_settings(base):
        grammar_entry = recognizer.recognize(image, models.ENTRY_TYPE_GRAMMAR)
    check("语法类型解析正确", grammar_entry.grammar_point == "〜ことになる")
    check("语法记录的 type 是 grammar", grammar_entry.type == models.ENTRY_TYPE_GRAMMAR)

    with use_settings("http://127.0.0.1:9"):  # 9 端口基本不会有人监听
        try:
            recognizer.recognize(image, "word")
            check("连不上时报错", False, "没有抛出异常")
        except (ApiRequestFailed, NetworkTimeout) as exc:
            check("连不上时给出可读提示", "接口" in exc.user_message or "网络" in exc.user_message, exc.user_message)

    with use_settings(base, api_key=""):
        expect_error("没配 Key 抛 MissingApiKey", MissingApiKey, lambda: recognizer.recognize(image, "word"), "OPENAI_API_KEY")
    with use_settings(base, vision_model=""):
        expect_error("没配模型名抛 MissingApiKey", MissingApiKey, lambda: recognizer.recognize(image, "word"), "VISION_MODEL")

    server.shutdown()
    server.server_close()

    # ---------------------------------------------------------------- 收尾
    for leftover in WORK_DIR.glob("*"):
        leftover.unlink(missing_ok=True)
    WORK_DIR.rmdir()

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed:
        print("有失败项，请看上面标 [FAIL] 的行。")
        return 1
    print("识别服务（离线部分）验收全部通过。真实接口请跑 scripts/probe_vision.py。")
    return 0


if __name__ == "__main__":
    sys.exit(main())