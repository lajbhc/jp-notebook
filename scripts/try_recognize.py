"""命令行跑一次识别，不启动界面。

用途：界面还没做的时候先验证识别链路；以后调提示词也用它，比开窗口点来点去快。

用法：
    .\\venv\\Scripts\\python.exe scripts\\try_recognize.py 照片.jpg
    .\\venv\\Scripts\\python.exe scripts\\try_recognize.py 照片.jpg --type grammar
    .\\venv\\Scripts\\python.exe scripts\\try_recognize.py 照片.jpg --json

它只打印识别结果，**不写数据库**，可以放心反复跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models
from app.services import recognizer
from app.services.errors import RecognitionError

FIELD_LABELS = [
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
]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="识别一张日语错题照片")
    parser.add_argument("image", help="图片路径")
    parser.add_argument(
        "--type",
        dest="entry_type",
        choices=list(models.ENTRY_TYPES),
        default=models.ENTRY_TYPE_WORD,
        help="生词(word) 或 语法(grammar)，默认 word",
    )
    parser.add_argument("--json", action="store_true", help="直接打印 JSON")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    image_path = Path(args.image)
    if not image_path.is_file():
        print(f"[图片不存在] {image_path}")
        return 2

    print(f"识别中… {image_path.name} / 类型={models.TYPE_LABELS[args.entry_type]}")
    try:
        entry = recognizer.recognize(image_path, args.entry_type)
    except RecognitionError as exc:
        print(f"\n[识别失败] {exc.user_message}")
        if exc.detail:
            print(f"[排查信息] {exc.detail[:500]}")
        return 1

    if args.json:
        print(json.dumps(entry.__dict__, ensure_ascii=False, indent=2))
        return 0

    print("\n== 识别结果 ==")
    print(f"  类型   : {entry.type_label}")
    for field, label in FIELD_LABELS:
        value = getattr(entry, field)
        if value:
            print(f"  {label:<4}: {value}")

    empty = [label for field, label in FIELD_LABELS if not getattr(entry, field)]
    if empty:
        print(f"\n  (这些字段是空的，需要人工补：{'、'.join(empty)})")
    print("\n注意：这里只是打印，没有写库。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))