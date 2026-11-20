"""阶段「自动补全」验收脚本（第 1 步）：不联网、不花额度。

补全是纯文本调用，所以这里用本地假服务冒充 /chat/completions，把
「正常返回 / 401 / 500 / 非 JSON / 思考吃光额度 / 围栏 / 400 重试」这些分支
都走一遍；同时验证本地检查（例句里有没有这个词、词性枚举、接续里有没有 ＋）。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_enrich.py
"""

from __future__ import annotations

import contextlib
import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, models
from app.services import enricher, entry_service
from app.services.errors import (
    ApiRequestFailed,
    MissingApiKey,
    ResponseNotJson,
)

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
        message = str(getattr(exc, "user_message", exc))
        missing = [word for word in contains if word not in message]
        if missing:
            check(name, False, f"提示里缺少 {missing}，实际是：{message}")
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
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {}
        self.server.requests.append(parsed)  # type: ignore[attr-defined]
        self.server.last_request = parsed  # type: ignore[attr-defined]

        route = self.server.route  # type: ignore[attr-defined]
        if isinstance(route, list):
            status, content_type, body = route.pop(0)
        else:
            status, content_type, body = route
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音
        pass


def start_fake_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    server.route = (200, "application/json", b"{}")  # type: ignore[attr-defined]
    server.last_request = {}  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def openai_payload(content: str, finish_reason: str = "stop") -> tuple[int, str, bytes]:
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ]
    }
    return 200, "application/json", json.dumps(body, ensure_ascii=False).encode("utf-8")


def json_route(data: dict, finish_reason: str = "stop") -> tuple[int, str, bytes]:
    return openai_payload(json.dumps(data, ensure_ascii=False), finish_reason)


@contextlib.contextmanager
def use_settings(
    base_url: str,
    *,
    api_key: str = "test-key",
    vision_model: str = "test-vision",
    text_model: str = "",
):
    """临时替换 config.get_settings，避免读真实 .env。"""
    original = config.get_settings
    config.get_settings = lambda: config.Settings(  # type: ignore[assignment]
        api_key=api_key or None,
        base_url=base_url,
        vision_model=vision_model or None,
        upload_port=8765,
        reminder_time="21:00",
        text_model=text_model or None,
    )
    try:
        yield
    finally:
        config.get_settings = original  # type: ignore[assignment]


def word_entry(**overrides) -> models.Entry:
    base = dict(type=models.ENTRY_TYPE_WORD, word="食べる")
    base.update(overrides)
    return models.Entry(**base)


def grammar_entry(**overrides) -> models.Entry:
    base = dict(
        type=models.ENTRY_TYPE_GRAMMAR,
        grammar_point="〜ことになる",
        connection="動詞辞書形 ＋ ことになる",
    )
    base.update(overrides)
    return models.Entry(**base)


WORD_FILLED = {
    "reading": "たべる",
    "meaning": "吃",
    "part_of_speech": "动词・一段",
    "example": "ご飯を食べました。",
    "example_cn": "吃了饭。",
}

GRAMMAR_FILLED = {
    "meaning": "客观上决定为…",
    "connection": "動詞辞書形 ＋ ことになる",
    "example": "来月、大阪へ転勤することになった。",
    "example_cn": "定下来下个月调去大阪。",
}


def main() -> int:
    print("== 1. 缺哪些字段 ==")
    check(
        "生词只填了词形 → 要补 5 个字段",
        enricher.missing_fields(word_entry())
        == ("reading", "meaning", "part_of_speech", "example", "example_cn"),
        str(enricher.missing_fields(word_entry())),
    )
    check("生词字段齐全 → 没有要补的", enricher.missing_fields(word_entry(**WORD_FILLED)) == ())
    check(
        "语法只填了语法点 → 要补 4 个字段",
        enricher.missing_fields(grammar_entry(connection=""))
        == ("meaning", "connection", "example", "example_cn"),
        str(enricher.missing_fields(grammar_entry(connection=""))),
    )
    check("未知类型 → 没有要补的", enricher.missing_fields(models.Entry(type="other")) == ())
    check(
        "只差例句翻译时只补那一个",
        enricher.missing_fields(word_entry(**{k: v for k, v in WORD_FILLED.items() if k != "example_cn"}))
        == ("example_cn",),
    )

    print("== 2. 请求组装 ==")
    body = enricher.build_request(word_entry(), ("example", "example_cn"), "some-model")
    content = body["messages"][0]["content"]
    check("带上模型名", body["model"] == "some-model")
    check("温度是补全专用值", body["temperature"] == enricher.ENRICH_TEMPERATURE)
    check("输出额度是补全专用值", body["max_tokens"] == enricher.ENRICH_MAX_TOKENS)
    check("关掉思考模式", body.get("enable_thinking") is False)
    check("提示词里列了要补的字段", "example、example_cn" in content, content[:120])
    check("提示词里带了已有内容", "食べる" in content)
    check("请求里没有图片", "image_url" not in json.dumps(body, ensure_ascii=False))
    check("明确要求不要改已有内容", "不要改" in content)
    grammar_body = enricher.build_request(grammar_entry(), ("meaning",), "m")
    grammar_content = grammar_body["messages"][0]["content"]
    check("语法用的是语法提示词", "语法" in grammar_content and "接续" in grammar_content)
    check(
        "语法请求里带了接续原文",
        "動詞辞書形 ＋ ことになる" in grammar_content,
    )

    print("== 3. 正常解析 ==")
    payload = dict(WORD_FILLED)
    result = enricher.parse_payload(payload, word_entry(), ("reading", "meaning", "part_of_speech", "example", "example_cn"))
    check("五个字段都收下", result.fields == WORD_FILLED, str(result.fields))
    check("没有字段被拒", result.rejected == {})
    check("summary 说明了补了哪些", result.summary.startswith("补了 5 个字段"), result.summary)
    check("names 是排过序的", result.names == ("example", "example_cn", "meaning", "part_of_speech", "reading"))
    stripped = enricher.parse_payload({"meaning": "  吃  "}, word_entry(), ("meaning",))
    check("值会去空白", stripped.fields.get("meaning") == "吃", str(stripped.fields))
    ignored = enricher.parse_payload({"meaning": "吃", "word": "書き換え"}, word_entry(), ("meaning",))
    check("不认识的键一律忽略（防止它改已有字段）", ignored.fields == {"meaning": "吃"}, str(ignored.fields))
    alias = enricher.parse_payload({"part_of_speech": "動詞（一段）"}, word_entry(), ("part_of_speech",))
    check("词性别名被收敛成固定枚举", alias.fields.get("part_of_speech") == "动词・一段", str(alias.fields))
    alias2 = enricher.parse_payload({"part_of_speech": "godan"}, word_entry(), ("part_of_speech",))
    check("英文词性也认得", alias2.fields.get("part_of_speech") == "动词・五段", str(alias2.fields))
    alias3 = enricher.parse_payload({"part_of_speech": "ナ形容詞"}, word_entry(), ("part_of_speech",))
    check("ナ形容词 收成固定写法", alias3.fields.get("part_of_speech") == "ナ形容词", str(alias3.fields))

    print("== 4. 本地检查会挡掉可疑内容 ==")
    bad_reading = enricher.parse_payload({"reading": "taberu"}, word_entry(), ("reading",))
    check("罗马字读音被拒", "reading" not in bad_reading.fields and "非假名" in bad_reading.rejected.get("reading", ""), str(bad_reading.rejected))
    bad_pos = enricher.parse_payload({"part_of_speech": "動詞"}, word_entry(), ("part_of_speech",))
    check("只有「动词」两个字 → 拒（说不清哪类）", "part_of_speech" not in bad_pos.fields)
    bad_meaning = enricher.parse_payload({"meaning": "eat"}, word_entry(), ("meaning",))
    check("英文释义被拒", "meaning" not in bad_meaning.fields)
    bad_example = enricher.parse_payload({"example": "これはペンです。"}, word_entry(), ("example",))
    check("例句里没这个词 → 拒", bad_example.rejected.get("example", "").find("没找到这个词") >= 0, str(bad_example.rejected))
    good_example = enricher.parse_payload({"example": "ご飯を食べました。"}, word_entry(), ("example",))
    check("例句里有活用形 → 收下", good_example.fields.get("example") == "ご飯を食べました。")
    stem_example = enricher.parse_payload({"example": "毎日、野菜を食べます。"}, word_entry(), ("example",))
    check("词干匹配也认（食べる→食べ…ます）", stem_example.fields.get("example", "") != "")
    chinese_example = enricher.parse_payload({"example": "吃饭了。"}, word_entry(), ("example",))
    check("纯中文例句被拒", "example" not in chinese_example.fields)
    bad_conn = enricher.parse_payload({"connection": "動詞辞書形"}, grammar_entry(), ("connection",))
    check("接续里没有 ＋ → 拒", "connection" not in bad_conn.fields and "＋" in bad_conn.rejected.get("connection", ""))
    good_conn = enricher.parse_payload({"connection": "動詞辞書形 ＋ ことになる"}, grammar_entry(), ("connection",))
    check("接续带 ＋ → 收下", good_conn.fields.get("connection", "") != "")
    bad_gram = enricher.parse_payload({"example": "毎日、日本語を勉強しています。"}, grammar_entry(), ("example",))
    check("语法例句没体现这个语法 → 拒", "example" not in bad_gram.fields, str(bad_gram.rejected))
    good_gram = enricher.parse_payload({"example": "来月、大阪へ転勤することになった。"}, grammar_entry(), ("example",))
    check("语法例句用了后半部分 → 收下", good_gram.fields.get("example", "") != "")
    point_only = enricher.parse_payload(
        {"example": "来月、大阪へ転勤することになった。"},
        grammar_entry(connection=""),
        ("example",),
    )
    check("没有接续时退回语法点匹配", "example" in point_only.fields, str(point_only.rejected))
    same_batch = enricher.parse_payload(
        {
            "connection": "動詞辞書形 ＋ ことになる",
            "example": "来月から大阪で働くことになりました。",
        },
        grammar_entry(connection=""),
        ("connection", "example"),
    )
    check("同一次返回里补了接续 → 例句按新接续判定", "example" in same_batch.fields, str(same_batch.rejected))
    check("ます形（なりました）也认", same_batch.fields.get("connection", "") != "")
    long_text = enricher.parse_payload({"meaning": "吃" * (enricher.MAX_FIELD_CHARS + 1)}, word_entry(), ("meaning",))
    check("超长字段被拒", "meaning" not in long_text.fields and "太长" in long_text.rejected.get("meaning", ""))
    empty = enricher.parse_payload({"meaning": ""}, word_entry(), ("meaning",))
    check("空字符串既不收也不报错", empty.fields == {} and empty.rejected == {})
    wrong_type = enricher.parse_payload({"meaning": ["吃"]}, word_entry(), ("meaning",))
    check("非字符串被拒", wrong_type.rejected.get("meaning") == "模型返回的不是字符串")
    mixed = enricher.parse_payload(
        {"meaning": "吃", "example": "これはペンです。"}, word_entry(), ("meaning", "example")
    )
    check("一对一错时只收对的那个", mixed.fields == {"meaning": "吃"} and "example" in mixed.rejected)
    check("summary 里同时提到没过的字段", "没过检查" in mixed.summary, mixed.summary)

    print("== 5. 合并（apply）==")
    original = word_entry()
    result = enricher.parse_payload(dict(WORD_FILLED), original, tuple(WORD_FILLED))
    merged = enricher.apply(original, result)
    check("补的字段进了新对象", merged.reading == "たべる" and merged.example_cn == "吃了饭。")
    check("原有内容没被改", merged.word == "食べる")
    check("记下了哪些字段是补的", set(merged.generated_names) == set(WORD_FILLED), merged.generated_fields)
    check("原对象没被就地修改", original.reading == "" and original.generated_fields == "")
    second = enricher.apply(merged, enricher.parse_payload({"meaning": "吃（他动词）"}, merged, ("meaning",)))
    check("第二次补全不重复记字段", second.generated_fields.count("meaning") == 1, second.generated_fields)
    check("第二次补全覆盖同名旧值", second.meaning == "吃（他动词）")
    untouched = enricher.apply(merged, enricher.Enrichment(fields={}, rejected={}, raw={}))
    check("空结果返回原对象", untouched is merged)

    print("== 6. 端到端（假接口）==")
    server, port = start_fake_server()
    try:
        with use_settings(f"http://127.0.0.1:{port}/v1"):
            server.route = json_route(dict(WORD_FILLED))  # type: ignore[attr-defined]
            result = enricher.enrich(word_entry())
            check("端到端收到 5 个字段", result.fields == WORD_FILLED, str(result.fields))
            check("记录了耗时", result.elapsed >= 0)
            sent = server.last_request  # type: ignore[attr-defined]
            check("用的是视觉模型作兜底", sent.get("model") == "test-vision", str(sent.get("model")))
            check("请求体里没有图片内容", "image_url" not in json.dumps(sent, ensure_ascii=False))

        with use_settings(f"http://127.0.0.1:{port}/v1", text_model="text-only-model"):
            server.route = json_route(dict(WORD_FILLED))  # type: ignore[attr-defined]
            enricher.enrich(word_entry())
            check(
                "配了 TEXT_MODEL 就用它",
                server.last_request.get("model") == "text-only-model",  # type: ignore[attr-defined]
                str(server.last_request.get("model")),  # type: ignore[attr-defined]
            )

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            server.requests.clear()  # type: ignore[attr-defined]
            empty = enricher.enrich(word_entry(**WORD_FILLED))
            check("字段齐全时根本不发请求", server.requests == [] and empty.is_empty)  # type: ignore[attr-defined]

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            server.requests.clear()  # type: ignore[attr-defined]
            enricher.enrich(word_entry(), targets=("example",))
            body = server.last_request["messages"][0]["content"]  # type: ignore[attr-defined]
            check("指定 targets 时只补那一项", "需要补的字段：example" in body, body[-200:])

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            fenced = "```json\n" + json.dumps(dict(WORD_FILLED), ensure_ascii=False) + "\n```"
            server.route = openai_payload(fenced)  # type: ignore[attr-defined]
            result = enricher.enrich(word_entry())
            check("围栏包裹的 JSON 也能解析", len(result.fields) == 5, str(result.fields))

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            server.route = (401, "application/json", b'{"error":"bad key"}')  # type: ignore[attr-defined]
            expect_error("401 → 提示检查 API Key", ApiRequestFailed, lambda: enricher.enrich(word_entry()), "API Key")

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            server.route = (500, "text/plain", b"boom")  # type: ignore[attr-defined]
            expect_error("500 → 提示服务端出错", ApiRequestFailed, lambda: enricher.enrich(word_entry()), "服务端")

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            server.route = (200, "application/json", b"<html>not json</html>")  # type: ignore[attr-defined]
            expect_error("响应不是 JSON → ResponseNotJson", ResponseNotJson, lambda: enricher.enrich(word_entry()))

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            server.route = (200, "application/json", b'{"choices":[{"message":{"content":""},"finish_reason":"length"}]}')  # type: ignore[attr-defined]
            expect_error(
                "思考吃光额度 → 提示换模型",
                ResponseNotJson,
                lambda: enricher.enrich(word_entry()),
                "思考",
            )

        with use_settings(f"http://127.0.0.1:{port}/v1"):
            good = json_route(dict(WORD_FILLED))
            server.route = [(400, "application/json", b'{"error":"unknown param"}'), good]  # type: ignore[attr-defined]
            server.requests.clear()  # type: ignore[attr-defined]
            result = enricher.enrich(word_entry())
            check("端点不认 enable_thinking 时自动去掉重试", len(result.fields) == 5, str(result.fields))
            check("重试时确实发了两次", len(server.requests) == 2)  # type: ignore[attr-defined]
            check("第二次请求里没有 enable_thinking", "enable_thinking" not in server.requests[1])  # type: ignore[attr-defined]

        with use_settings(f"http://127.0.0.1:1/v1"):
            expect_error(
                "连不上 → 提示检查网络",
                ApiRequestFailed,
                lambda: enricher.enrich(word_entry()),
                "连不上",
            )

        with use_settings(f"http://127.0.0.1:{port}/v1", api_key="", vision_model=""):
            expect_error(
                "没配 Key → MissingApiKey",
                MissingApiKey,
                lambda: enricher.enrich(word_entry()),
                "缺少配置项",
            )
    finally:
        server.shutdown()
        server.server_close()

    print("== 7. 识别之后自动补全（服务层）==")
    real_recognize = entry_service.recognizer.recognize
    real_enrich = entry_service.enricher.enrich
    calls = {"enrich": 0}

    def fake_recognize(path, entry_type, **kwargs):
        return word_entry()

    def fake_enrich(entry, *, targets=None, timeout=None):
        calls["enrich"] += 1
        return enricher.parse_payload(dict(WORD_FILLED), entry, tuple(WORD_FILLED))

    try:
        entry_service.recognizer.recognize = fake_recognize
        entry_service.enricher.enrich = fake_enrich

        result = entry_service.recognize_and_enrich("x.jpg", models.ENTRY_TYPE_WORD)
        check("识别后自动补全并合进 entry", result.entry.reading == "たべる" and result.enriched)
        check("记下了补的字段", set(result.entry.generated_names) == set(WORD_FILLED))
        check("note 说明了补了哪些", "补了" in result.note, result.note)

        calls["enrich"] = 0
        off = entry_service.recognize_and_enrich(
            "x.jpg", models.ENTRY_TYPE_WORD, auto_enrich=False
        )
        check("关掉补全就不调用", calls["enrich"] == 0 and not off.enriched)
        check("关掉补全时 note 为空", off.note == "")

        calls["enrich"] = 0
        entry_service.recognizer.recognize = lambda *a, **k: word_entry(**WORD_FILLED)
        complete = entry_service.recognize_and_enrich("x.jpg", models.ENTRY_TYPE_WORD)
        check("字段齐全时不调用补全", calls["enrich"] == 0 and not complete.enriched)

        entry_service.recognizer.recognize = fake_recognize

        def boom(entry, *, targets=None, timeout=None):
            calls["enrich"] += 1
            raise ApiRequestFailed(401, "bad key")

        entry_service.enricher.enrich = boom
        failed = entry_service.recognize_and_enrich("x.jpg", models.ENTRY_TYPE_WORD)
        check("补全失败不影响识别结果", failed.entry.word == "食べる" and not failed.enriched)
        check("补全失败给出一句人话", "API Key" in failed.enrich_error, failed.enrich_error)
        check("补全失败时 note 提示可以手填", "手动填写" in failed.note, failed.note)

        def crash(entry, *, targets=None, timeout=None):
            raise RuntimeError("boom")

        entry_service.enricher.enrich = crash
        crashed = entry_service.recognize_and_enrich("x.jpg", models.ENTRY_TYPE_WORD)
        check("补全抛未预期异常也不崩", crashed.entry.word == "食べる" and bool(crashed.enrich_error))

        entry_service.enricher.enrich = fake_enrich
        again = entry_service.enrich_entry(word_entry())
        check("enrich_entry 能单独补一条已有记录", again.entry.meaning == "吃")

        kept = entry_service.normalize(result.entry)
        check("保存前整理时保留 AI 标记", set(kept.generated_names) == set(WORD_FILLED), kept.generated_fields)
        trimmed = models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="食べる",
            reading="たべる",
            generated_fields="reading,example",
        )
        check("被用户删掉的字段不再标记", entry_service.normalize(trimmed).generated_fields == "reading")
        as_grammar = models.Entry(
            type=models.ENTRY_TYPE_GRAMMAR,
            grammar_point="〜ことになる",
            generated_fields="reading,meaning",
        )
        check("切成语法后不保留生词专属标记", entry_service.normalize(as_grammar).generated_fields == "")
    finally:
        entry_service.recognizer.recognize = real_recognize
        entry_service.enricher.enrich = real_enrich

    print()
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())