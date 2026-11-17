"""探针（E1）：把"转写"和"结构化"拆成两次调用，跟现在的单段式对比。

不改主流程，只是一个脚本。跑法：

    .\\venv\\Scripts\\python.exe scripts\\probe_two_stage.py 照片.jpg
    .\\venv\\Scripts\\python.exe scripts\\probe_two_stage.py 照片.jpg --type grammar
    .\\venv\\Scripts\\python.exe scripts\\probe_two_stage.py 照片.jpg --crop 0,0,1,0.5

三次 API 调用（单段式 + 转写 + 结构化），只为比较，不写库。
关心的三件事：
1. 转写能不能把红笔批注、行间小字都抄下来
2. 拆开之后 error_reason / note 这类长字段还会不会停在半句
3. 多一次调用的时间与 token 代价
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.models import TYPE_LABELS, ENTRY_TYPE_WORD, ENTRY_TYPES  # noqa: E402
from app.prompts.recognize import build_prompt  # noqa: E402
from app.services import recognizer  # noqa: E402
from app.services.errors import RecognitionError  # noqa: E402

# 第一段：只管抄，不管意思。加"红笔/批注"是因为错题本上最有价值的往往是老师那行字。
TRANSCRIBE_PROMPT = """把这张图上的日文文字逐行抄下来。

要求：
- 只抄原文，不要解释、不要翻译、不要补全、不要总结
- 手写的批注、红笔勾叉旁边的字、行间距里的小字，也都要抄
- 一行原文输出一行，保持原来的先后顺序
- 看不清的字用 □ 占位，不要猜
- 不要用 Markdown 代码块，不要加标题
"""

# 判断"可能没抄完"的粗糙线索：以助词/接续收尾，说明句子还没落地。
_PARTICLE_TAILS = tuple("はがをにでとものからのでけどて、")


def _tail_suspect(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped.endswith(_PARTICLE_TAILS)


def _usage(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "usage?"
    usage = payload.get("usage") or {}
    return "in=%s out=%s total=%s" % (
        usage.get("prompt_tokens", "?"),
        usage.get("completion_tokens", "?"),
        usage.get("total_tokens", "?"),
    )


def _chat(messages: list[dict], *, what: str):
    """发一次 chat/completions，返回 (正文, 耗时秒, usage 文本)。"""
    settings = config.get_settings()
    url = f"{settings.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.vision_model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": recognizer.MAX_TOKENS,
        **recognizer.EXTRA_BODY,
    }
    started = time.perf_counter()
    response = recognizer._post(url, headers, body, recognizer.DEFAULT_TIMEOUT)
    elapsed = time.perf_counter() - started

    if response.status_code == 400 and recognizer.EXTRA_BODY:
        fallback = {k: v for k, v in body.items() if k not in recognizer.EXTRA_BODY}
        retry = recognizer._post(url, headers, fallback, recognizer.DEFAULT_TIMEOUT)
        if retry.status_code == 200:
            response = retry
            elapsed = time.perf_counter() - started

    if response.status_code != 200:
        raise recognizer.ApiRequestFailed(response.status_code, recognizer._body_snippet(response))

    try:
        payload = response.json()
    except ValueError as exc:
        raise recognizer.ResponseNotJson(f"body is not json: {response.text[:200]}") from exc

    text = recognizer._content_text(payload)
    print(f"  [{what}] {elapsed:.1f}s {_usage(response)}")
    return text, elapsed


def _image_message(text: str, data_url: str) -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


def _fields(entry) -> dict[str, str]:
    names = (
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
    return {name: getattr(entry, name) for name in names}


def _show_fields(title: str, fields: dict[str, str]) -> None:
    print(f"\n  {title}")
    for name, value in fields.items():
        if not value:
            continue
        mark = "  ⚠ 半句?" if _tail_suspect(value) else ""
        print(f"    {name:<16} {value}{mark}")


def main() -> int:
    parser = argparse.ArgumentParser(description="E1：转写 + 结构化 两段式对比探针")
    parser.add_argument("image", help="图片路径")
    parser.add_argument("--type", default=ENTRY_TYPE_WORD, choices=list(ENTRY_TYPES))
    parser.add_argument("--crop", default="", help="框选区域，形如 0,0,1,0.5（归一化比例）")
    args = parser.parse_args()

    settings = config.get_settings()
    if not settings.recognition_ready:
        print("缺少配置项: " + "、".join(settings.missing_required))
        print("（把 OPENAI_API_KEY / VISION_MODEL 写进 .env）")
        return 2

    path = Path(args.image)
    if not path.is_file():
        print(f"找不到图片：{path}")
        return 1

    crop = None
    if args.crop.strip():
        try:
            crop = tuple(float(part) for part in args.crop.split(","))
        except ValueError:
            print("--crop 要写成 0,0,1,0.5 这样的四个数字")
            return 1
        if len(crop) != 4:
            print("--crop 需要正好四个数字")
            return 1

    print(f"图：{path.name}  类型：{TYPE_LABELS[args.type]}  框选：{crop or '整张'}")

    try:
        data_url = recognizer.prepare_image(path, crop)
    except RecognitionError as exc:
        print(f"图片有问题：{exc.user_message}")
        return 1
    print(f"  送出去的图：{len(data_url) // 1024} KB（base64 后）")

    # ① 现在主流程用的单段式：一次调用既读图又填字段
    print("\n=== A. 现在的单段式（读图 + 填字段一次完成） ===")
    try:
        started = time.perf_counter()
        single = recognizer.recognize(path, args.type, crop=crop)
        single_elapsed = time.perf_counter() - started
    except RecognitionError as exc:
        print(f"  失败：{exc.user_message}")
        print(f"  detail: {exc.detail}")
        return 1
    print(f"  [single] {single_elapsed:.1f}s")
    single_fields = _fields(single)

    # ② 第一段：只转写
    print("\n=== B. 两段式 第 1 段：只转写 ===")
    try:
        transcript, t1 = _chat(_image_message(TRANSCRIBE_PROMPT, data_url), what="transcribe")
    except RecognitionError as exc:
        print(f"  失败：{exc.user_message}")
        print(f"  detail: {exc.detail}")
        return 1

    lines = [line for line in transcript.splitlines() if line.strip()]
    print(f"  抄到 {len(lines)} 行 / {len(transcript)} 字")
    for index, line in enumerate(lines[:40], start=1):
        print(f"    {index:>2}| {line}")
    if len(lines) > 40:
        print(f"    …（还有 {len(lines) - 40} 行）")

    # ③ 第二段：纯文本结构化（不发图）
    print("\n=== C. 两段式 第 2 段：纯文本填字段 ===")
    stage2_prompt = (
        build_prompt(args.type)
        + "\n\n下面是别人从图片上抄下来的文字（可能有错字、可能带 □ 占位）。"
        + "请据此填写字段；抄下来的文字里没有的内容一律留空，不要靠常识补。\n\n"
        + "<transcript>\n"
        + transcript
        + "\n</transcript>"
    )
    try:
        structured, t2 = _chat(
            [{"role": "user", "content": stage2_prompt}], what="structure"
        )
        payload = recognizer.extract_json_object(structured)
        two_stage = recognizer.build_entry(payload, args.type)
    except RecognitionError as exc:
        print(f"  失败：{exc.user_message}")
        print(f"  detail: {exc.detail}")
        return 1
    two_fields = _fields(two_stage)

    # ④ 对比
    print("\n=== 对比 ===")
    _show_fields("A. 单段式", single_fields)
    _show_fields("B. 两段式", two_fields)

    print("\n  字段长度对比（左单段 / 右两段）")
    for name in single_fields:
        left, right = single_fields[name], two_fields[name]
        if not left and not right:
            continue
        flag = ""
        if _tail_suspect(left) and not _tail_suspect(right):
            flag = "  ← 两段式抄全了"
        elif _tail_suspect(left) and _tail_suspect(right):
            flag = "  ← 两边都像没抄完"
        print(f"    {name:<16} {len(left):>3} / {len(right):>3}{flag}")

    print("\n  只在转写里出现、但没进字段的行（可能是被漏掉的信息）：")
    field_text = " ".join(two_fields.values())
    missed = [line for line in lines if line.strip() and line.strip()[:12] not in field_text]
    if not missed:
        print("    没有，字段基本覆盖了转写内容")
    for line in missed[:12]:
        print(f"    - {line}")

    print(
        "\n  耗时：单段式 %.1fs　两段式 %.1fs（转写 %.1fs + 结构化 %.1fs）"
        % (single_elapsed, t1 + t2, t1, t2)
    )
    print("\n  两段式的原始 JSON：")
    print("    " + json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())