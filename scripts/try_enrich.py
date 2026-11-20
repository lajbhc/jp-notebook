"""手动试一次「识别 → 补全」，看真实补出来的内容好不好。

不写数据库，只打印；用来判断补全质量够不够用，够再往主流程接。

用法：
    # 只补全（不识别图片）
    .\\venv\\Scripts\\python.exe scripts\\try_enrich.py --type word --word 取り組み

    # 先识别一张图，再补全空字段（最接近真实流程）
    .\\venv\\Scripts\\python.exe scripts\\try_enrich.py --image data\\_probe_word.jpg --type word
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models
from app.services import enricher, recognizer
from app.services.errors import RecognitionError

_FIELDS = (
    ("word", "词形"),
    ("reading", "读音"),
    ("meaning", "释义"),
    ("part_of_speech", "词性"),
    ("grammar_point", "语法点"),
    ("connection", "接续"),
    ("example", "例句"),
    ("example_cn", "例句翻译"),
    ("error_reason", "错因"),
    ("note", "备注"),
)


def _print_entry(entry: models.Entry, title: str) -> None:
    print(f"\n{title}")
    for name, label in _FIELDS:
        value = getattr(entry, name, "")
        if value:
            mark = " [AI补]" if name in entry.generated_names else ""
            print(f"  {label}：{value}{mark}")


def main() -> int:
    parser = argparse.ArgumentParser(description="试一次识别 + 补全")
    parser.add_argument("--type", choices=models.ENTRY_TYPES, default=models.ENTRY_TYPE_WORD)
    parser.add_argument("--word", default="", help="直接给词或语法点，不识别图片")
    parser.add_argument("--image", default="", help="先识别这张图，再补全空字段")
    parser.add_argument("--crop", default="", help="框选区域 x,y,w,h（0~1）")
    parser.add_argument("--no-enrich", action="store_true", help="只识别，不补全")
    args = parser.parse_args()

    crop = None
    if args.crop:
        try:
            crop = tuple(float(part) for part in args.crop.split(","))
        except ValueError:
            print("--crop 要写成 x,y,w,h，例如 0.1,0.2,0.5,0.4")
            return 2
        if len(crop) != 4:
            print("--crop 要 4 个数字")
            return 2

    if args.image:
        try:
            entry = recognizer.recognize(args.image, args.type, crop=crop)
        except RecognitionError as exc:
            print(f"识别失败：{exc.user_message}")
            print(f"细节：{exc.detail}")
            return 1
        _print_entry(entry, "识别结果（来自图片）")
    elif args.word:
        entry = models.Entry(type=args.type)
        if args.type == models.ENTRY_TYPE_GRAMMAR:
            entry.grammar_point = args.word
        else:
            entry.word = args.word
    else:
        parser.error("要么给 --image，要么给 --word")
        return 2

    if args.no_enrich:
        return 0

    missing = enricher.missing_fields(entry)
    print(f"\n缺的字段：{'、'.join(missing) if missing else '（没有，不需要补）'}")
    if not missing:
        return 0

    try:
        result = enricher.enrich(entry, targets=missing)
    except RecognitionError as exc:
        print(f"补全失败：{exc.user_message}")
        print(f"细节：{exc.detail}")
        return 1

    print(f"\n补全用了 {result.elapsed:.1f}s，模型 {result.model}")
    print(f"结果：{result.summary}")
    if result.rejected:
        for name, reason in result.rejected.items():
            value = result.raw.get(name, "")
            print(f"  被挡下：{name} = {value!r}（{reason}）")
    if result.fields:
        _print_entry(enricher.apply(entry, result), "合并后的记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())