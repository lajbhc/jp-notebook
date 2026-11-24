"""阶段 1 验收脚本：不启动界面，直接用命令行验证存储层。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_storage.py

脚本使用独立的临时库 data/_verify_storage/app.db（跑完整个目录删掉）。放在子目录里
是有意的：备份落在"库文件旁边的 backups/"，如果直接用 data/_verify.db，迁移测试
就会往真实的 data/backups 里塞文件、还会挤掉保留份数。

最后会顺带把正式库 data/app.db 建好，方便你确认文件确实生成了。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, models
from app.storage import db
from app.storage.repository import EntryNotFound, EntryRepository

VERIFY_DIR = config.DATA_DIR / "_verify_storage"
VERIFY_DB = VERIFY_DIR / "app.db"

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


def expect_error(name: str, exc_type: type[BaseException], func) -> None:
    try:
        func()
    except exc_type:
        check(name, True)
    except Exception as exc:  # noqa: BLE001 - 报告类型不符
        check(name, False, f"抛出的是 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有抛出异常")


def _columns(db_path: Path) -> set[str]:
    """读出 entries 表现有的列名，用来判断迁移有没有真的加列。"""
    with db.connect(db_path) as conn:
        return {row["name"] for row in conn.execute("PRAGMA table_info(entries)")}


def main() -> int:
    print(f"临时库路径: {VERIFY_DB}")
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 1
    print("\n== 1. 建库与建表 ==")
    for leftover in (VERIFY_DB, Path(f"{VERIFY_DB}-wal"), Path(f"{VERIFY_DB}-shm")):
        leftover.unlink(missing_ok=True)
    check("建库前文件不存在", not VERIFY_DB.exists())

    db.init_db(VERIFY_DB)
    check("建库后文件存在", VERIFY_DB.exists())
    check("schema_version 写入正确", db.get_schema_version(VERIFY_DB) == db.SCHEMA_VERSION)

    with db.connect(VERIFY_DB) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    for table in ("entries", "checkins", "meta"):
        check(f"表 {table} 已创建", table in tables)

    # ---------------------------------------------------------------- 2
    print("\n== 2. 重复初始化（幂等）==")
    repo = EntryRepository(VERIFY_DB)
    first = repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="食べる",
            reading="たべる",
            meaning="吃",
            part_of_speech="动词・一段",
            example="ご飯を食べる。",
            example_cn="吃饭。",
            source_image="2026-11-01/ab12cd.jpg",
        )
    )
    db.init_db(VERIFY_DB)
    check("二次初始化后数据还在", repo.count() == 1)
    check("二次初始化后版本不变", db.get_schema_version(VERIFY_DB) == db.SCHEMA_VERSION)

    # ---------------------------------------------------------------- 3
    print("\n== 3. 新建记录 ==")
    check("create 返回了 id", isinstance(first.id, int) and first.id > 0)
    check("created_at 格式为 YYYY-MM-DD HH:MM:SS", len(first.created_at) == 19 and first.created_at[4] == "-")
    check("created_at 与 updated_at 初次相同", first.created_at == first.updated_at)
    check("type_label 为「生词」", first.type_label == "生词")
    check("title 取词形", first.title == "食べる")

    grammar = repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_GRAMMAR,
            grammar_point="〜ことになる",
            connection="動詞辞書形 + ことになる",
            meaning="（客观上）决定为…",
            example="来月、東京へ転勤することになった。",
            example_cn="下个月决定调去东京工作。",
            error_reason="误用成了「ことにした」，主观决定和客观决定的区别没分清",
        )
    )
    check("语法记录 type_label 为「语法」", grammar.type_label == "语法")
    check("语法记录 title 取语法点", grammar.title == "〜ことになる")
    check("两条记录总数", repo.count() == 2)

    # ---------------------------------------------------------------- 4
    print("\n== 4. 入库校验 ==")
    expect_error(
        "非法 type 被拒绝",
        ValueError,
        lambda: repo.create(models.Entry(type="phrase", word="気になる")),
    )
    expect_error(
        "生词缺词形被拒绝",
        ValueError,
        lambda: repo.create(models.Entry(type=models.ENTRY_TYPE_WORD, meaning="只有释义")),
    )
    expect_error(
        "语法缺语法点被拒绝",
        ValueError,
        lambda: repo.create(models.Entry(type=models.ENTRY_TYPE_GRAMMAR, example="只有例句")),
    )
    check("被拒绝的记录没有入库", repo.count() == 2)

    # ---------------------------------------------------------------- 5
    print("\n== 5. 读取与更新 ==")
    check("get 能取回原记录", repo.get(first.id).word == "食べる")  # type: ignore[union-attr]
    check("get 不存在的 id 返回 None", repo.get(999999) is None)

    time.sleep(1.05)  # 时间戳精度到秒，等一下才能看出更新
    first.meaning = "吃（他动词）"
    first.note = "五段以外，注意是他动词"
    updated = repo.update(first)
    check("update 后内容生效", updated.meaning == "吃（他动词）")
    check("update 不动 created_at", updated.created_at == first.created_at)
    check("update 刷新 updated_at", updated.updated_at > first.created_at)
    expect_error(
        "更新不存在的记录报 EntryNotFound",
        EntryNotFound,
        lambda: repo.update(models.Entry(id=999999, type=models.ENTRY_TYPE_WORD, word="幽霊")),
    )

    # ---------------------------------------------------------------- 6
    print("\n== 6. 搜索 ==")
    check("按词形搜「食べる」命中", len(repo.search("食べる")) == 1)
    check("按假名搜「たべる」命中", len(repo.search("たべる")) == 1)
    check("按中文释义搜「吃」命中", len(repo.search("吃")) >= 1)
    check("按语法点搜「こと」命中", len(repo.search("こと")) == 1)
    check("按接续搜「辞書形」命中", len(repo.search("辞書形")) == 1)
    check("按错因搜「ことにした」命中", len(repo.search("ことにした")) == 1)
    check("搜不存在的词返回空", repo.search("見つからない単語") == [])
    check("空关键词返回全部", len(repo.search("")) == 2)

    # LIKE 通配符必须被转义，否则搜 "100%" 会匹配到所有记录
    repo.create(models.Entry(type=models.ENTRY_TYPE_WORD, word="100%理解できない"))
    check("通配符 % 被转义（只命中含百分号的记录）", len(repo.search("100%")) == 1)
    check("通配符 _ 被转义", len(repo.search("_")) == 0)

    # ---------------------------------------------------------------- 7
    print("\n== 7. 筛选、分页、统计 ==")
    check("按类型筛选 type=word", repo.count(entry_type=models.ENTRY_TYPE_WORD) == 2)
    check("按类型筛选 type=grammar", repo.count(entry_type=models.ENTRY_TYPE_GRAMMAR) == 1)
    today = time.strftime("%Y-%m-%d")
    check("按日期区间筛选（今天）", repo.count(date_from=today, date_to=today) == 3)
    check("按日期区间筛选（过去）", repo.count(date_from="2000-01-01", date_to="2000-01-02") == 0)
    check("分页 limit=1 只返回 1 条", len(repo.search(limit=1)) == 1)
    check("分页 offset 生效", repo.search(limit=1)[0].id != repo.search(limit=1, offset=1)[0].id)

    by_type = repo.count_by_type()
    check("count_by_type 统计正确", by_type == {"word": 2, "grammar": 1}, str(by_type))
    check("today_count 统计今天", repo.today_count() == 3)
    check("today_count 指定历史日期为 0", repo.today_count("2000-01-01") == 0)

    # ---------------------------------------------------------------- 8
    print("\n== 8. 删除 ==")
    check("delete 返回 True", repo.delete(first.id) is True)
    check("删除后 get 为 None", repo.get(first.id) is None)
    check("重复删除返回 False", repo.delete(first.id) is False)
    check("剩余记录数正确", repo.count() == 2)

    # ---------------------------------------------------------------- 9
    print("\n== 9. 删库自愈 ==")
    for path in (VERIFY_DB, Path(f"{VERIFY_DB}-wal"), Path(f"{VERIFY_DB}-shm")):
        path.unlink(missing_ok=True)
    check("库文件已删除", not VERIFY_DB.exists())
    db.init_db(VERIFY_DB)
    repo2 = EntryRepository(VERIFY_DB)
    check("重建后能正常查询", repo2.count() == 0)
    check("重建后版本正确", db.get_schema_version(VERIFY_DB) == db.SCHEMA_VERSION)

    # ---------------------------------------------------------------- 10
    print("\n== 10. AI 标记字段（generated_fields）==")
    marked = repo2.create(
        models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="取り組み",
            meaning="举措",
            generated_fields="meaning,example",
        )
    )
    fetched = repo2.get(marked.id)
    check("generated_fields 能存进去", fetched.generated_fields == "meaning,example")
    check("generated_names 能拆出两个字段", fetched.generated_names == ("meaning", "example"))
    marked.generated_fields = ""
    repo2.update(marked)
    check("generated_fields 能被清空", repo2.get(marked.id).generated_fields == "")
    check("关键词不会搜到标记内容", repo2.search("example") == [])

    # ---------------------------------------------------------------- 11
    print("\n== 11. v1 -> v2 迁移 ==")
    v1_db = VERIFY_DB.parent / "verify_v1.db"
    leftover_files = (v1_db, Path(f"{v1_db}-wal"), Path(f"{v1_db}-shm"))
    for path in leftover_files:
        path.unlink(missing_ok=True)

    db.init_db(v1_db)  # 先造一个当前版本的库
    v1_repo = EntryRepository(v1_db)
    legacy = v1_repo.create(
        models.Entry(type=models.ENTRY_TYPE_WORD, word="移行テスト", meaning="迁移测试")
    )
    # 手工退化成 v1：删掉新列 + 把版本号写回 1
    with db.connect(v1_db) as conn:
        conn.execute("ALTER TABLE entries DROP COLUMN generated_fields")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.execute("PRAGMA user_version = 1")

    check("已退化成 v1（没有 generated_fields 列）", "generated_fields" not in _columns(v1_db))
    check("退化后版本读出来是 1", db.get_schema_version(v1_db) == 1)

    db.init_db(v1_db)  # 触发迁移
    check("迁移后版本升到最新", db.get_schema_version(v1_db) == db.SCHEMA_VERSION)
    check("迁移后补上了 generated_fields 列", "generated_fields" in _columns(v1_db))
    migrated = EntryRepository(v1_db).get(legacy.id)
    check("迁移没有丢旧数据", migrated is not None and migrated.word == "移行テスト")
    check("迁移后旧记录的 AI 标记为空", migrated.generated_fields == "")

    for path in leftover_files:
        path.unlink(missing_ok=True)

    # ---------------------------------------------------------------- 12
    print("\n== 12. 正式库初始化 ==")
    db.init_db()  # 不传路径 -> config.DB_PATH
    check(f"正式库已生成 {config.DB_PATH}", config.DB_PATH.exists())
    print(f"  正式库记录数: {EntryRepository().count()}")

    # ---------------------------------------------------------------- 收尾
    shutil.rmtree(VERIFY_DIR, ignore_errors=True)

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed:
        print("有失败项，请看上面标 [FAIL] 的行。")
        return 1
    print("存储层验收全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())