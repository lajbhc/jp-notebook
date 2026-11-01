"""阶段 6 验收脚本（服务层）：打卡的写入幂等与连续天数。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_checkin.py

不启动界面、不联网，用独立的临时库 data/_verify_checkin.db，跑完删除。
连续天数这种东西时间一长就很难手工验，所以这里直接构造"某天打过卡"的数据来跑，
今天是几号不影响结论。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.services import checkin_service
from app.services.checkin_service import CheckinFailed
from app.storage import db
from app.storage.repository import CheckinRepository

VERIFY_DB = config.DATA_DIR / "_verify_checkin.db"

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


def _reset() -> CheckinRepository:
    for leftover in (VERIFY_DB, Path(f"{VERIFY_DB}-wal"), Path(f"{VERIFY_DB}-shm")):
        leftover.unlink(missing_ok=True)
    db.init_db(VERIFY_DB)
    return CheckinRepository(VERIFY_DB)


def main() -> int:
    global _failed
    print(f"临时库路径: {VERIFY_DB}")

    # ---------------------------------------------------------------- 1
    print("\n== 1. 一张白纸：还没打过卡 ==")
    repo = _reset()
    state = checkin_service.status(repo=repo)
    check("今天未打卡", state.checked_today is False)
    check("连续 0 天", state.streak == 0, str(state.streak))
    check("累计 0 天", state.total == 0, str(state.total))
    check("本月 0 天", state.month_count == 0, str(state.month_count))
    check("首日为 None", state.first_date is None)
    check("文案：还没有连续记录", state.streak_text == "还没有连续记录", state.streak_text)
    check("文案：今天还没打卡", state.today_text == "今天还没打卡", state.today_text)
    check("文案：累计 0 天 · 本月 0 天", state.progress_text == "累计 0 天 · 本月 0 天", state.progress_text)

    # ---------------------------------------------------------------- 2
    print("\n== 2. 打第一次卡 ==")
    state, newly = checkin_service.check_today("看了第 7 课", repo=repo)
    check("这次是新写入", newly is True)
    check("立刻变成今日已打卡", state.checked_today is True)
    check("连续 1 天", state.streak == 1, str(state.streak))
    check("累计 1 天", state.total == 1, str(state.total))
    check("库里只有一行", repo.count() == 1, str(repo.count()))
    row = repo.get()
    check("备注存进去了", row is not None and row["note"] == "看了第 7 课", str(row))
    check("created_at 是时间戳", row is not None and len(row["created_at"]) == 19, str(row))

    # ---------------------------------------------------------------- 3
    print("\n== 3. 同一天重复点：不产生第二行 ==")
    state_again, newly_again = checkin_service.check_today("又点了一次", repo=repo)
    check("这次不是新写入", newly_again is False)
    check("仍然只有一行", repo.count() == 1, str(repo.count()))
    check("连续天数没翻倍", state_again.streak == 1, str(state_again.streak))
    kept = repo.get()
    check("第一次的备注没被覆盖", kept is not None and kept["note"] == "看了第 7 课", str(kept))
    for _ in range(3):
        checkin_service.check_today("连点", repo=repo)
    check("连点 3 次后还是一行", repo.count() == 1, str(repo.count()))

    # ---------------------------------------------------------------- 4
    print("\n== 4. 造历史，看连续天数怎么数 ==")
    repo = _reset()
    today = date(2026, 11, 1)
    for offset in (1, 2, 3):
        repo.check((today - timedelta(days=offset)).isoformat())
    state = checkin_service.status(today, repo=repo)
    check("今天没打、昨天打了 → 连续 3 天", state.streak == 3, str(state.streak))
    check("今天没打 → 状态是未打卡", state.checked_today is False)

    state, newly = checkin_service.check_today(today=today, repo=repo)
    check("补上今天：新写入", newly is True)
    check("补上今天：连续 4 天", state.streak == 4, str(state.streak))

    print("\n== 5. 中间断一天，从断点后重新数 ==")
    repo = _reset()
    # 今天、昨天、前天有；大前天断；再往前还有 5 天
    for offset in (0, 1, 2, 4, 5, 6, 7, 8):
        repo.check((today - timedelta(days=offset)).isoformat())
    state = checkin_service.status(today, repo=repo)
    check("今天在，连续 3 天（不越过断点）", state.streak == 3, str(state.streak))

    print("\n== 6. 昨天也没打：断了，归零 ==")
    repo = _reset()
    for offset in (2, 3, 4):
        repo.check((today - timedelta(days=offset)).isoformat())
    state = checkin_service.status(today, repo=repo)
    check("连续 0 天", state.streak == 0, str(state.streak))
    check("但累计仍是 3 天", state.total == 3, str(state.total))

    print("\n== 7. 跨年、跨月不断 ==")
    repo = _reset()
    new_year = date(2027, 1, 1)
    repo.check("2026-12-31")
    repo.check("2027-01-01")
    state = checkin_service.status(new_year, repo=repo)
    check("跨年连成 2 天", state.streak == 2, str(state.streak))
    check("1 月只算 1 天", state.month_count == 1, str(state.month_count))

    # ---------------------------------------------------------------- 8
    print("\n== 8. 月历网格 ==")
    repo = _reset()
    repo.check("2026-11-01")
    repo.check("2026-11-15")
    repo.check("2026-11-30")
    repo.check("2026-12-01")  # 不该出现在 11 月的格子里

    grid = checkin_service.month_grid(2026, 11, repo=repo)
    flat = [cell for week in grid for cell in week]
    day_cells = [cell for cell in flat if not cell.empty]
    check("11 月有 30 个日期格", len(day_cells) == 30, str(len(day_cells)))
    check("每行 7 格", all(len(week) == 7 for week in grid), str([len(w) for w in grid]))
    check("总格数是 7 的倍数", len(flat) % 7 == 0, str(len(flat)))
    check("1 号是周日在最后一列", grid[0][6].iso == "2026-11-01", grid[0][6].iso)
    check("1 号标记为已打卡", grid[0][6].checked is True)
    check("2 号没打卡", grid[1][0].iso == "2026-11-02" and grid[1][0].checked is False,
          f"{grid[1][0].iso} {grid[1][0].checked}")
    checked_isos = {cell.iso for cell in day_cells if cell.checked}
    check("11 月命中 3 天", checked_isos == {"2026-11-01", "2026-11-15", "2026-11-30"}, str(checked_isos))
    check("12-01 没混进 11 月", "2026-12-01" not in {cell.iso for cell in day_cells})
    check("日期格文本是日号", day_cells[0].text == "1" and day_cells[-1].text == "30",
          f"{day_cells[0].text} {day_cells[-1].text}")

    print("\n== 9. 闰年 2 月 29 天 ==")
    grid = checkin_service.month_grid(2028, 2, repo=repo)
    check("2028 年 2 月有 29 天",
          len([c for w in grid for c in w if not c.empty]) == 29,
          str(len([c for w in grid for c in w if not c.empty])))
    grid = checkin_service.month_grid(2026, 2, repo=repo)
    check("2026 年 2 月有 28 天",
          len([c for w in grid for c in w if not c.empty]) == 28,
          str(len([c for w in grid for c in w if not c.empty])))

    # ---------------------------------------------------------------- 10
    print("\n== 10. 仓储的边界 ==")
    repo = _reset()
    check("撤销不存在的打卡返回 False", repo.remove("2026-11-02") is False)
    repo.check("2026-11-02")
    check("撤销已有的返回 True", repo.remove("2026-11-02") is True)
    check("撤销后查不到", repo.exists("2026-11-02") is False)

    expect_error("非法日期被拒", ValueError, lambda: repo.check("2026-13-01"))
    expect_error("乱写的日期被拒", ValueError, lambda: repo.check("2026/11/01"))
    repo.check(None, "默认今天")
    check("不传日期就是今天", repo.exists(date.today().isoformat()) is True)

    repo = _reset()
    for day in ("2026-01-10", "2026-01-05", "2026-01-20"):
        repo.check(day)
    check("dates() 升序", repo.dates() == ["2026-01-05", "2026-01-10", "2026-01-20"], str(repo.dates()))
    check("区间是闭区间", repo.dates("2026-01-05", "2026-01-10") == ["2026-01-05", "2026-01-10"],
          str(repo.dates("2026-01-05", "2026-01-10")))
    check("recent 倒序", [r["date"] for r in repo.recent(2)] == ["2026-01-20", "2026-01-10"],
          str([r["date"] for r in repo.recent(2)]))
    check("first_date 是最早那天", repo.first_date() == "2026-01-05", str(repo.first_date()))
    check("count 带区间", repo.count("2026-01-06") == 2, str(repo.count("2026-01-06")))

    # ---------------------------------------------------------------- 11
    print("\n== 11. 出错时不把 sqlite 漏给界面 ==")
    broken = config.DATA_DIR / "_verify_checkin_broken"
    broken.mkdir(exist_ok=True)
    blocker = broken / "app.db"
    blocker.write_text("not a directory", encoding="utf-8")
    try:
        expect_error(
            "库路径坏掉时抛 CheckinFailed",
            CheckinFailed,
            lambda: checkin_service.status(repo=CheckinRepository(blocker / "nope.db")),
        )
        expect_error(
            "写入失败时抛 CheckinFailed",
            CheckinFailed,
            lambda: checkin_service.check_today(repo=CheckinRepository(blocker / "nope.db")),
        )
    finally:
        for child in broken.rglob("*"):
            if child.is_file():
                child.unlink()
        blocker.unlink(missing_ok=True)
        broken.rmdir()

    # ---------------------------------------------------------------- 12
    print("\n== 12. 收尾 ==")
    for leftover in (VERIFY_DB, Path(f"{VERIFY_DB}-wal"), Path(f"{VERIFY_DB}-shm")):
        leftover.unlink(missing_ok=True)
    check("临时库已清理", not VERIFY_DB.exists())

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed:
        print("打卡验收未通过。")
        return 1
    print("打卡验收全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())