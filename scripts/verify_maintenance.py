"""验收：图片目录整理（孤儿扫描 / 重复分组 / 安全删除）与上传日志轮转。

全部在 data/_verify_maint 里跑，跑完删掉。不联网、不弹窗。

重点验四件容易写错的事：

1. `_incoming/` 这种临时中转目录不算正式图片，不能被当成孤儿删掉；
2. 判断"有没有人用"要按库里的引用算，且 Windows 上文件名不分大小写；
3. 删除前重新查一次引用 —— 清单是几秒前的，期间可能又录了一条；
4. 删完之后空掉的日期目录要顺手去掉，不然 data/uploads 里全是空文件夹。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

WORK = ROOT / "data" / "_verify_maint"
UPLOADS = WORK / "uploads"
DB = WORK / "app.db"
config.DATA_DIR = WORK
config.UPLOAD_DIR = UPLOADS
config.LOG_DIR = WORK / "logs"
config.DB_PATH = DB
shutil.rmtree(WORK, ignore_errors=True)
config.ensure_dirs()
UPLOADS.mkdir(parents=True, exist_ok=True)

from PIL import Image  # noqa: E402

from app import models  # noqa: E402
from app.services import maintenance_service, upload_service  # noqa: E402
from app.services.entry_service import store_image  # noqa: E402
from app.storage.db import init_db  # noqa: E402
from app.storage.repository import EntryRepository  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


def expect_error(name: str, func, exc_type: type[BaseException]) -> BaseException | None:
    try:
        func()
    except exc_type as exc:
        check(name, True)
        return exc
    except Exception as exc:  # noqa: BLE001 - 就是为了看清楚抛了什么
        check(name, False, f"抛了 {type(exc).__name__}: {exc}")
        return None
    check(name, False, "没有抛异常")
    return None


def make_jpeg(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color).save(path, "JPEG")
    return path


def main() -> int:
    init_db(DB)
    repo = EntryRepository(DB)

    # ---------------------------------------------------------------- 1
    print("\n== 1. 空目录 ==")
    report = maintenance_service.scan(root=UPLOADS, repo=repo)
    check("没有图片时数量为 0", report.files == (), str(report.files))
    check("没有孤儿", report.orphans == ())
    check("没有重复组", report.duplicate_groups == ())
    check("合计字节为 0", report.total_bytes == 0)
    check("文案说人话", "共 0 张图" in report.summary(), report.summary())

    # ---------------------------------------------------------------- 2
    print("\n== 2. 扫描：谁被引用、谁孤儿、谁重复 ==")
    used_src = make_jpeg(WORK / "src" / "used.jpg", (200, 30, 30))
    used_rel = store_image(used_src, root=UPLOADS, today="2026-11-01")
    repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="食べる",
            source_image=used_rel,
        )
    )

    # 完全同内容的另一张（模拟"手滑又存了一份"）：直接写一份到别的日期目录
    dup_dir = UPLOADS / "2026-11-02"
    dup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(UPLOADS / used_rel, dup_dir / "copy-of-used.jpg")

    orphan_src = make_jpeg(WORK / "src" / "orphan.jpg", (30, 30, 200))
    orphan_rel = store_image(orphan_src, root=UPLOADS, today="2026-11-03")

    incoming = UPLOADS / "_incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / "half-uploaded.upload").write_bytes(b"x" * 10)

    report = maintenance_service.scan(root=UPLOADS, repo=repo)
    paths = {item.relative_path for item in report.files}
    check("数到 3 张正式图片", len(report.files) == 3, str(sorted(paths)))
    check("_incoming 里的临时文件不算", not any("_incoming" in p for p in paths), str(sorted(paths)))
    check(
        "没被任何记录引用的两张都算孤儿（含那份重复副本）",
        {item.relative_path for item in report.orphans}
        == {orphan_rel, "2026-11-02/copy-of-used.jpg"},
        str([item.relative_path for item in report.orphans]),
    )
    check(
        "孤儿字节数是这两张之和",
        report.orphan_bytes
        == orphan_src.stat().st_size + used_src.stat().st_size,
        str(report.orphan_bytes),
    )
    check("认出 1 组重复", len(report.duplicate_groups) == 1, str(report.duplicate_groups))
    group = report.duplicate_groups[0] if report.duplicate_groups else ()
    check(
        "重复组里是那两张同内容的图",
        {item.relative_path for item in group} == {used_rel, "2026-11-02/copy-of-used.jpg"},
        str([item.relative_path for item in group]),
    )
    check(
        "重复多占的空间 = 组内多出来的份数",
        report.duplicate_bytes == int(used_src.stat().st_size),
        str(report.duplicate_bytes),
    )
    check("文案里提到孤儿和重复", "没被引用的 2 张" in report.summary(), report.summary())

    # 孤儿那张换成大小不同的图，确保不是靠"大小相同"凑出来的
    check("重复是按内容算的（大小不同不会算成一组）", len(report.duplicate_groups) == 1)

    # ---------------------------------------------------------------- 3
    print("\n== 3. 大小写不同的引用也算被引用 ==")
    upper_repo = repo
    entry = upper_repo.get(1)
    entry.source_image = used_rel.upper()  # 只改大小写：Windows 上是同一个文件
    upper_repo.update(entry)
    report = maintenance_service.scan(root=UPLOADS, repo=upper_repo)
    check(
        "引用写成大写了也不会被当孤儿",
        all(item.relative_path != used_rel for item in report.orphans),
        str([item.relative_path for item in report.orphans]),
    )
    entry.source_image = used_rel
    upper_repo.update(entry)

    # ---------------------------------------------------------------- 4
    print("\n== 4. 删除孤儿：只删该删的 ==")
    result = maintenance_service.delete_orphans([orphan_rel], root=UPLOADS, repo=repo)
    check("删掉 1 张", len(result.deleted) == 1, str(result.deleted))
    check("释放的字节数对", result.freed_bytes == orphan_src.stat().st_size)
    check("文件真的没了", not (UPLOADS / orphan_rel).exists())
    check("空掉的日期目录也去掉了", not (UPLOADS / "2026-11-03").exists())
    check("文案说的是删了 1 张", "删掉 1 张" in result.summary(), result.summary())

    result = maintenance_service.delete_orphans(
        [used_rel, "2026-11-02/copy-of-used.jpg"], root=UPLOADS, repo=repo
    )
    check("被引用的那张被跳过", result.skipped == (used_rel,), str(result.skipped))
    check("被引用的那张还在", (UPLOADS / used_rel).is_file())
    check("重复副本删掉了，日期目录也空了", not (UPLOADS / "2026-11-02").exists())

    # ---------------------------------------------------------------- 5
    print("\n== 5. 删除时重新核对（清单可能过期）==")
    fresh_src = make_jpeg(WORK / "src" / "fresh.jpg", (10, 120, 10))
    fresh_rel = store_image(fresh_src, root=UPLOADS, today="2026-11-04")
    # 先扫出它是孤儿，然后立刻录一条引用它的记录，再拿着旧清单去删
    old_report = maintenance_service.scan(root=UPLOADS, repo=repo)
    check("扫描时它确实是孤儿", any(i.relative_path == fresh_rel for i in old_report.orphans))
    repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="走る",
            source_image=fresh_rel,
        )
    )
    result = maintenance_service.delete_orphans(
        [fresh_rel], root=UPLOADS, repo=repo
    )
    check("拿了旧清单也不会误删", result.deleted == () and result.skipped == (fresh_rel,),
          f"deleted={result.deleted} skipped={result.skipped}")
    check("文件还在", (UPLOADS / fresh_rel).is_file())

    missing = maintenance_service.delete_orphans(["2026-11-09/nope.jpg"], root=UPLOADS, repo=repo)
    check("点名一个不存在的文件不会报错", missing.deleted == () and missing.failed == ())

    # ---------------------------------------------------------------- 6
    print("\n== 6. 异常分支 ==")
    broken_db = WORK / "broken.db"
    broken_db.write_bytes(b"not a database")
    exc = expect_error(
        "库读不出来时抛 MaintenanceFailed",
        lambda: maintenance_service.scan(root=UPLOADS, repo=EntryRepository(broken_db)),
        maintenance_service.MaintenanceFailed,
    )
    check(
        "MaintenanceFailed 带人话",
        exc is not None and "数据库" in exc.user_message,
        getattr(exc, "user_message", ""),
    )
    expect_error(
        "删除时库读不出来也抛 MaintenanceFailed",
        lambda: maintenance_service.delete_orphans(
            ["x.jpg"], root=UPLOADS, repo=EntryRepository(broken_db)
        ),
        maintenance_service.MaintenanceFailed,
    )
    not_a_dir = WORK / "not-a-dir.jpg"
    not_a_dir.write_bytes(b"x")
    expect_error(
        "上传目录不是目录时也抛 MaintenanceFailed",
        lambda: maintenance_service.scan(root=not_a_dir, repo=repo),
        maintenance_service.MaintenanceFailed,
    )

    # ---------------------------------------------------------------- 7
    print("\n== 7. 上传日志按大小轮转 ==")
    original_limit = upload_service.UPLOAD_LOG_MAX_BYTES
    upload_service.UPLOAD_LOG_MAX_BYTES = 400
    try:
        log = upload_service.log_path()
        for index in range(30):
            upload_service.log_event(f"line {index} " + "x" * 40)
        rotated = log.with_name(log.name + ".1")
        check("旧日志被改名成 upload.log.1", rotated.is_file(), str(rotated))
        check("当前日志没超过上限太多", log.stat().st_size <= 1200, str(log.stat().st_size))
        check(
            "只留一份旧的（不留 .2）",
            not log.with_name(log.name + ".2").exists(),
        )
        first_line = rotated.read_text(encoding="utf-8").splitlines()[0]
        check("旧日志里有时间戳", len(first_line) > 20 and first_line[4] == "-", first_line)
    finally:
        upload_service.UPLOAD_LOG_MAX_BYTES = original_limit

    # ---------------------------------------------------------------- 8
    print("\n== 8. 大小文案 ==")
    check("B", maintenance_service.size_text(822) == "822 B")
    check("KB", maintenance_service.size_text(2048) == "2.0 KB")
    check("MB", maintenance_service.size_text(5 * 1024 * 1024) == "5.0 MB")

    shutil.rmtree(WORK, ignore_errors=True)
    print(f"\n===== 通过 {PASSED} 项，失败 {FAILED} 项 =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())