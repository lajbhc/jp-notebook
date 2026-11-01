"""阶段 4 验收脚本（服务层）：词库的筛选、分页、统计、删除。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_library.py

不联网、不弹窗。用临时库 data/_verify_library/，跑完删除。
为了让日期筛选可断言，脚本会直接改 created_at（正常代码里这个字段由 storage 层
生成，测试里不这么做就没法造"上个月的记录"）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, models
from app.services import library_service
from app.services.library_service import FilterInvalid, LibraryFailed
from app.storage.db import connect, init_db
from app.storage.repository import EntryRepository

WORK_DIR = config.DATA_DIR / "_verify_library"
VERIFY_DB = WORK_DIR / "library.db"

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


def expect_error(name: str, error_type, action) -> BaseException | None:
    """断言 action 抛出指定异常，并把异常对象回给调用方看提示语。"""
    global _passed, _failed
    try:
        action()
    except error_type as exc:
        _passed += 1
        print(f"  [OK] {name}")
        return exc
    except Exception as exc:  # noqa: BLE001 - 类型不对也要算失败
        _failed += 1
        print(f"  [FAIL] {name} -> 抛的是 {type(exc).__name__}: {exc}")
        return None
    _failed += 1
    print(f"  [FAIL] {name} -> 没有抛异常")
    return None


def set_created_at(entry_id: int, day: str) -> None:
    with connect(VERIFY_DB) as conn:
        conn.execute(
            "UPDATE entries SET created_at = ?, updated_at = ? WHERE id = ?",
            (f"{day} 09:00:00", f"{day} 09:00:00", entry_id),
        )


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    init_db(VERIFY_DB)
    repo = EntryRepository(VERIFY_DB)
    print(f"临时库: {VERIFY_DB}")

    # ---------------------------------------------------------------- 1
    print("\n== 1. 造数据 ==")
    word1 = repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="食べる",
            reading="たべる",
            meaning="吃",
            part_of_speech="动词・一段",
            example="ご飯を食べる。",
            example_cn="吃饭。",
            error_reason="× ご飯を食べます。→ 这里要用辞书形",
        )
    )
    word2 = repo.create(
        models.Entry(type=models.ENTRY_TYPE_WORD, word="飲む", reading="のむ", meaning="喝")
    )
    word3 = repo.create(
        models.Entry(type=models.ENTRY_TYPE_WORD, word="走る", reading="はしる", meaning="跑")
    )
    grammar1 = repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_GRAMMAR,
            grammar_point="〜ことになる",
            connection="動詞辞書形 + ことになる",
            meaning="（客观上）决定…",
            example="来月、大阪へ転勤することになった。",
        )
    )
    grammar2 = repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_GRAMMAR,
            grammar_point="〜ようにする",
            connection="動詞辞書形 + ようにする",
            meaning="尽量做到…",
            example="毎日、日本語を勉強するようにしている。",
        )
    )
    grammar3 = repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_GRAMMAR,
            grammar_point="〜てしまう",
            connection="動詞て形 + しまう",
            meaning="做完 / 不小心做了",
            example="ケーキを全部食べてしまった。",
        )
    )
    check("造了 6 条记录", repo.count() == 6, str(repo.count()))

    # 生词放在 10 月初，语法放在 10 月中，方便断言日期区间
    for entry, day in (
        (word1, "2026-10-01"),
        (word2, "2026-10-02"),
        (word3, "2026-10-03"),
        (grammar1, "2026-10-10"),
        (grammar2, "2026-10-11"),
        (grammar3, "2026-10-12"),
    ):
        set_created_at(entry.id, day)

    # ---------------------------------------------------------------- 2
    print("\n== 2. 无条件查询与分页信息 ==")
    page = library_service.search_entries(repo=repo)
    check("默认查到 6 条", page.total == 6, str(page.total))
    check("默认每页 20 条", page.page_size == library_service.DEFAULT_PAGE_SIZE, str(page.page_size))
    check("只有 1 页", page.pages == 1, str(page.pages))
    check("页号停在 1", page.page == 1, str(page.page))
    check("没有上一页", not page.has_prev)
    check("没有下一页", not page.has_next)
    check("页码范围文案", page.range_text == "第 1–6 条，共 6 条", page.range_text)
    check("页号文案", page.page_text == "第 1 / 1 页", page.page_text)
    check(
        "按创建时间倒序（最新在最前）",
        page.items[0].id == grammar3.id,
        f"{page.items[0].title}",
    )

    # ---------------------------------------------------------------- 3
    print("\n== 3. 关键词搜索 ==")
    hit = library_service.search_entries("食べる", repo=repo)
    check("搜「食べる」命中", hit.total == 1 and hit.items[0].id == word1.id, str(hit.total))
    hit = library_service.search_entries("たべる", repo=repo)
    check("搜读音「たべる」也命中", hit.total == 1, str(hit.total))
    hit = library_service.search_entries("食べ", repo=repo)
    check("搜「食べ」命中 2 条（词形 + 例句）", hit.total == 2, str(hit.total))
    hit = library_service.search_entries("こと", repo=repo)
    check("搜「こと」命中语法点", hit.total == 1, str(hit.total))
    hit = library_service.search_entries("  食べる  ", repo=repo)
    check("关键词两端空格被忽略", hit.total == 1, str(hit.total))
    hit = library_service.search_entries("%", repo=repo)
    check("搜 % 不会当成通配符", hit.total == 0, str(hit.total))
    hit = library_service.search_entries("不存在的词", repo=repo)
    check("搜不到时 total 为 0", hit.total == 0, str(hit.total))
    check("空结果是空元组", hit.items == (), str(hit.items))
    check("空结果页码文案不显示「第 0 条」", hit.range_text == "没有匹配的记录", hit.range_text)

    # ---------------------------------------------------------------- 4
    print("\n== 4. 类型筛选 ==")
    words = library_service.search_entries(entry_type=models.ENTRY_TYPE_WORD, repo=repo)
    grammars = library_service.search_entries(entry_type=models.ENTRY_TYPE_GRAMMAR, repo=repo)
    check("生词 3 条", words.total == 3, str(words.total))
    check("语法 3 条", grammars.total == 3, str(grammars.total))
    check("两类都不含对方", all(e.type == models.ENTRY_TYPE_WORD for e in words.items))
    check(
        "类型 + 关键词可以叠加",
        library_service.search_entries(
            "食べ", models.ENTRY_TYPE_GRAMMAR, repo=repo
        ).total
        == 1,
    )

    # ---------------------------------------------------------------- 5
    print("\n== 5. 日期区间 ==")
    oct_late = library_service.search_entries(date_from="2026-10-10", repo=repo)
    check("10-10 之后 3 条", oct_late.total == 3, str(oct_late.total))
    mid = library_service.search_entries(
        date_from="2026-10-02", date_to="2026-10-11", repo=repo
    )
    check("10-02 ~ 10-11 闭区间 4 条", mid.total == 4, str(mid.total))
    single = library_service.search_entries(
        date_from="2026-10-03", date_to="2026-10-03", repo=repo
    )
    check("起止同一天只含当天", single.total == 1, str(single.total))
    only_to = library_service.search_entries(date_to="2026-10-02", repo=repo)
    check("只给结束日期", only_to.total == 2, str(only_to.total))

    exc = expect_error(
        "开始晚于结束时报 FilterInvalid",
        FilterInvalid,
        lambda: library_service.search_entries(
            date_from="2026-10-12", date_to="2026-10-01", repo=repo
        ),
    )
    check(
        "提示语说清楚了怎么改",
        exc is not None and "调换" in exc.user_message,
        getattr(exc, "user_message", ""),
    )

    # ---------------------------------------------------------------- 6
    print("\n== 6. 翻页 ==")
    p1 = library_service.search_entries(page=1, page_size=2, repo=repo)
    check("每页 2 条共 3 页", p1.pages == 3, str(p1.pages))
    check("第 1 页 2 条", len(p1.items) == 2, str(len(p1.items)))
    check("第 1 页能往后翻", p1.has_next and not p1.has_prev)
    check("第 1 页文案", p1.range_text == "第 1–2 条，共 6 条", p1.range_text)
    p3 = library_service.search_entries(page=3, page_size=2, repo=repo)
    check("第 3 页 2 条", len(p3.items) == 2, str(len(p3.items)))
    check("第 3 页能往前翻", p3.has_prev and not p3.has_next)
    check("第 3 页文案", p3.range_text == "第 5–6 条，共 6 条", p3.range_text)
    clamped = library_service.search_entries(page=99, page_size=2, repo=repo)
    check("页号越界自动收到最后一页", clamped.page == 3, str(clamped.page))
    check(
        "三页拼起来正好 6 条且不重复",
        len(
            {
                entry.id
                for page_no in (1, 2, 3)
                for entry in library_service.search_entries(
                    page=page_no, page_size=2, repo=repo
                ).items
            }
        )
        == 6,
    )

    # ---------------------------------------------------------------- 7
    print("\n== 7. 非法筛选条件 ==")
    expect_error(
        "每页 0 条报 FilterInvalid",
        FilterInvalid,
        lambda: library_service.search_entries(page_size=0, repo=repo),
    )
    expect_error(
        "未知类型报 FilterInvalid",
        FilterInvalid,
        lambda: library_service.search_entries(entry_type="kanji", repo=repo),
    )

    # ---------------------------------------------------------------- 8
    print("\n== 8. 统计 ==")
    stats = library_service.stats(repo=repo)
    check("总数 6", stats.total == 6, str(stats.total))
    check("生词 3", stats.by_type.get(models.ENTRY_TYPE_WORD) == 3, str(stats.by_type))
    check("语法 3", stats.by_type.get(models.ENTRY_TYPE_GRAMMAR) == 3, str(stats.by_type))
    check("今天新增 0（都是改过日期的）", stats.today == 0, str(stats.today))
    check(
        "统计文案",
        stats.text() == "共 6 条 · 生词 3 · 语法 3 · 今日新增 0",
        stats.text(),
    )
    repo.create(models.Entry(type=models.ENTRY_TYPE_WORD, word="泳ぐ"))
    stats = library_service.stats(repo=repo)
    check("新存一条后今天新增 1", stats.today == 1, str(stats.today))
    check("总数变成 7", stats.total == 7, str(stats.total))

    # ---------------------------------------------------------------- 9
    print("\n== 9. 单条读取 ==")
    got = library_service.get_entry(word1.id, repo=repo)
    check("按 id 取到记录", got is not None and got.word == "食べる", str(got))
    check("不存在的 id 返回 None", library_service.get_entry(999999, repo=repo) is None)

    # ---------------------------------------------------------------- 10
    print("\n== 10. 删除 ==")
    before = library_service.search_entries(repo=repo).total
    check("删除成功返回 True", library_service.delete_entry(word2.id, repo=repo) is True)
    after = library_service.search_entries(repo=repo).total
    check("总数少 1", after == before - 1, f"{before} -> {after}")
    check("重复删除返回 False", library_service.delete_entry(word2.id, repo=repo) is False)
    check("删掉的记录查不到", library_service.get_entry(word2.id, repo=repo) is None)

    # ---------------------------------------------------------------- 11
    print("\n== 11. 库坏了要点到服务层异常 ==")
    # 用一个普通文件占住"数据库目录"的位置，mkdir 时会抛 NotADirectoryError
    blocker = WORK_DIR / "blocker"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    broken = EntryRepository(blocker / "nested" / "app.db")
    exc = expect_error(
        "读库失败抛 LibraryFailed",
        LibraryFailed,
        lambda: library_service.search_entries(repo=broken),
    )
    check(
        "LibraryFailed 带人话提示",
        exc is not None and "data" in exc.user_message,
        getattr(exc, "user_message", ""),
    )

    empty_db = WORK_DIR / "empty.db"
    empty_db.write_text("", encoding="utf-8")  # 0 字节库：能打开但没有任何表
    exc = expect_error(
        "库文件存在但没建表时也抛 LibraryFailed",
        LibraryFailed,
        lambda: library_service.search_entries(repo=EntryRepository(empty_db)),
    )
    check(
        "提示语指向数据库本身",
        exc is not None and "词库" in exc.user_message,
        getattr(exc, "user_message", ""),
    )

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("词库服务层验收全部通过。")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    code = main()
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    raise SystemExit(code)