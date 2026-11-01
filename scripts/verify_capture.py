"""阶段 3 验收脚本（服务层部分）：不启动界面，直接验证"选图 → 落库"这条链路。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_capture.py

脚本用独立的临时图片目录与临时库（跑完删除），不会弄脏 data/app.db
和 data/uploads/。界面部分见 scripts/verify_ui.py。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app import config, models
from app.services import entry_service
from app.services.errors import ImageInvalid
from app.storage.db import init_db
from app.storage.repository import EntryRepository

WORK_DIR = config.DATA_DIR / "_verify_capture"
UPLOAD_ROOT = WORK_DIR / "uploads"
VERIFY_DB = WORK_DIR / "verify.db"

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


def expect_error(name: str, exc_type: type[BaseException], func) -> BaseException | None:
    try:
        func()
    except exc_type as exc:
        check(name, True)
        return exc
    except Exception as exc:  # noqa: BLE001 - 报告类型不符
        check(name, False, f"抛出的是 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有抛出异常")
    return None


def make_jpeg(path: Path, size: tuple[int, int] = (40, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 40, 40)).save(path, "JPEG")
    return path


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"临时目录: {WORK_DIR}")
    init_db(VERIFY_DB)
    repo = EntryRepository(VERIFY_DB)
    source = make_jpeg(WORK_DIR / "src" / "card.jpg")

    # ---------------------------------------------------------------- 1
    print("\n== 1. 图片落盘 ==")
    relative = entry_service.store_image(source, root=UPLOAD_ROOT, today="2026-11-01")
    parts = relative.split("/")
    check("返回相对路径（日期/文件名）", len(parts) == 2, relative)
    check("目录是传入的日期", parts[0] == "2026-11-01", relative)
    check("保留 .jpg 后缀", parts[1].endswith(".jpg"), relative)
    check("文件真的复制过去了", (UPLOAD_ROOT / relative).is_file(), relative)
    check("原图没被移动", source.is_file())
    check(
        "文件名被随机化（不撞名）",
        parts[1] != "card.jpg",
        relative,
    )

    again = entry_service.store_image(source, root=UPLOAD_ROOT, today="2026-11-01")
    check("同一张图存两次得到两个文件名", again != relative, f"{relative} vs {again}")

    png_source = WORK_DIR / "src" / "shot.png"
    Image.new("RGBA", (20, 20), (0, 0, 255, 128)).save(png_source, "PNG")
    png_relative = entry_service.store_image(png_source, root=UPLOAD_ROOT, today="2026-11-01")
    check("PNG 保持 .png 后缀", png_relative.endswith(".png"), png_relative)

    print("\n== 2. 图片错误分支 ==")
    exc = expect_error(
        "原图不存在抛 ImageInvalid",
        ImageInvalid,
        lambda: entry_service.store_image(WORK_DIR / "src" / "nope.jpg", root=UPLOAD_ROOT),
    )
    check(
        "提示语是给人看的（不含路径）",
        exc is not None and "nope.jpg" not in exc.user_message,
        getattr(exc, "user_message", ""),
    )

    not_image = WORK_DIR / "src" / "note.jpg"
    not_image.write_text("这不是图片", encoding="utf-8")
    exc = expect_error(
        "伪装成 .jpg 的文本抛 ImageInvalid",
        ImageInvalid,
        lambda: entry_service.store_image(not_image, root=UPLOAD_ROOT),
    )
    check(
        "坏图提示里提到 HEIC/格式",
        exc is not None and ("HEIC" in exc.user_message or "格式" in exc.user_message),
        getattr(exc, "user_message", ""),
    )

    print("\n== 3. 相对路径解析 ==")
    resolved = entry_service.absolute_image_path(relative, root=UPLOAD_ROOT)
    check("能拼回绝对路径", resolved is not None and resolved.is_file(), str(resolved))
    check("空字符串返回 None", entry_service.absolute_image_path("", root=UPLOAD_ROOT) is None)
    check(
        "文件不存在返回 None",
        entry_service.absolute_image_path("2026-11-01/ghost.jpg", root=UPLOAD_ROOT) is None,
    )

    # ---------------------------------------------------------------- 4
    print("\n== 4. 表单整理 ==")
    word = models.Entry(type=models.ENTRY_TYPE_WORD, word="  食べる  ", reading=" たべる ")
    word.grammar_point = "残留的语法点"
    word.connection = "残留的接续"
    cleaned = entry_service.normalize(word)
    check("首尾空白被去掉", cleaned.word == "食べる", repr(cleaned.word))
    check("生词记录清空语法点", cleaned.grammar_point == "", cleaned.grammar_point)
    check("生词记录清空接续", cleaned.connection == "", cleaned.connection)
    check("normalize 不改动传入对象", word.word.strip() == "食べる" and word.grammar_point != "")

    grammar = models.Entry(type=models.ENTRY_TYPE_GRAMMAR, grammar_point="〜ことになる")
    grammar.word = "残留的词形"
    grammar.reading = "残留的读音"
    grammar.part_of_speech = "残留的词性"
    cleaned = entry_service.normalize(grammar)
    check("语法记录清空词形", cleaned.word == "")
    check("语法记录清空读音", cleaned.reading == "")
    check("语法记录清空词性", cleaned.part_of_speech == "")

    print("\n== 5. 校验分支 ==")
    expect_error(
        "生词缺词形被拒",
        entry_service.EntryValidationError,
        lambda: entry_service.normalize(models.Entry(type=models.ENTRY_TYPE_WORD)),
    )
    expect_error(
        "语法缺语法点被拒",
        entry_service.EntryValidationError,
        lambda: entry_service.normalize(models.Entry(type=models.ENTRY_TYPE_GRAMMAR)),
    )
    expect_error(
        "非法类型被拒",
        entry_service.EntryValidationError,
        lambda: entry_service.normalize(models.Entry(type="bogus")),
    )
    exc = expect_error(
        "缺主字段的提示点明字段名",
        entry_service.EntryValidationError,
        lambda: entry_service.normalize(models.Entry(type=models.ENTRY_TYPE_WORD)),
    )
    check(
        "提示里写了「词形」",
        exc is not None and "词形" in exc.user_message,
        getattr(exc, "user_message", ""),
    )

    # ---------------------------------------------------------------- 6
    print("\n== 6. 保存 ==")
    new_entry = models.Entry(
        type=models.ENTRY_TYPE_WORD,
        word="食べる",
        reading="たべる",
        meaning="吃",
        part_of_speech="动词・一段",
        example="ご飯を食べる。",
        example_cn="吃饭。",
        error_reason="ここは辞書形で答える",
        note="他動詞なので「を」を使う。",
    )
    saved = entry_service.save_entry(new_entry, image_source=source, repo=repo, root=UPLOAD_ROOT)
    check("返回的对象带 id", saved.id is not None, str(saved.id))
    check("source_image 是相对路径", "/" in saved.source_image and ":" not in saved.source_image, saved.source_image)
    check(
        "记录的图片能在 uploads 下找到",
        entry_service.absolute_image_path(saved.source_image, root=UPLOAD_ROOT) is not None,
        saved.source_image,
    )
    check("created_at 已填", len(saved.created_at) == 19, saved.created_at)
    check("repo 里能读回来", repo.get(saved.id) is not None)
    check("列表计数为 1", repo.count() == 1, str(repo.count()))

    saved.word = "食べる（辞書形）"
    updated = entry_service.save_entry(saved, repo=repo, root=UPLOAD_ROOT)
    check("按 id 更新而不是新增", repo.count() == 1, str(repo.count()))
    check("词形被改掉", updated.word == "食べる（辞書形）", updated.word)
    check("created_at 不变", updated.created_at == saved.created_at)
    check("图片路径保持不变", updated.source_image == saved.source_image, updated.source_image)

    print("\n== 7. 保存失败不写库 ==")
    before = repo.count()
    expect_error(
        "图片不存在时保存被拒",
        ImageInvalid,
        lambda: entry_service.save_entry(
            models.Entry(type=models.ENTRY_TYPE_WORD, word="壊れた"),
            image_source=WORK_DIR / "src" / "ghost.jpg",
            repo=repo,
            root=UPLOAD_ROOT,
        ),
    )
    check("失败后没有多出记录", repo.count() == before, str(repo.count()))

    expect_error(
        "词形空着保存被拒",
        entry_service.EntryValidationError,
        lambda: entry_service.save_entry(
            models.Entry(type=models.ENTRY_TYPE_WORD, word="   "),
            image_source=source,
            repo=repo,
            root=UPLOAD_ROOT,
        ),
    )
    check("校验失败后没有多出记录", repo.count() == before, str(repo.count()))

    grammar_saved = entry_service.save_entry(
        models.Entry(
            type=models.ENTRY_TYPE_GRAMMAR,
            grammar_point="〜ことになる",
            connection="動詞辞書形 + ことになる",
            meaning="（客观上）决定为…",
        ),
        repo=repo,
        root=UPLOAD_ROOT,
    )
    check("语法记录可以不配图", grammar_saved.source_image == "", grammar_saved.source_image)
    check("两类记录共用一个计数", repo.count() == before + 1, str(repo.count()))

    print("\n== 8. 写库失败被包成服务层异常 ==")
    # 把一个普通文件当作"数据库目录"，mkdir 会抛 NotADirectoryError。
    # 界面不该看到 OSError，而应该看到 entry_service.SaveFailed。
    broken_repo = EntryRepository(WORK_DIR / "src" / "note.jpg" / "nested" / "app.db")
    exc = expect_error(
        "路径不可写时抛 SaveFailed",
        entry_service.SaveFailed,
        lambda: entry_service.save_entry(
            models.Entry(type=models.ENTRY_TYPE_WORD, word="测试"),
            repo=broken_repo,
            root=UPLOAD_ROOT,
        ),
    )
    check(
        "SaveFailed 带人话提示",
        exc is not None and "data" in exc.user_message,
        getattr(exc, "user_message", ""),
    )

    # ---------------------------------------------------------------- 收尾
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("录入链路（服务层）验收全部通过。界面请跑 scripts/verify_ui.py。")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())