"""打卡逻辑单测：连续天数、月历格子、幂等打卡。

连续天数是这个功能里唯一"算错了用户也未必发现"的地方，所以边界都在这里钉住：
今天打没打、昨天打没打、中间断一天、跨月跨年、闰年二月。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services import checkin_service
from app.storage.db import init_db
from app.storage.repository import CheckinRepository


# --------------------------------------------------------------- streak_from
def test_streak_without_any_checkin():
    assert checkin_service.streak_from([], today=date(2026, 11, 1)) == 0


def test_streak_counts_today():
    days = ["2026-10-30", "2026-10-31", "2026-11-01"]

    assert checkin_service.streak_from(days, today=date(2026, 11, 1)) == 3


def test_streak_counts_from_yesterday_when_today_missing():
    """今天还没打卡，但昨天打了：连续天数不该立刻掉到 0。"""
    days = ["2026-10-30", "2026-10-31"]

    assert checkin_service.streak_from(days, today=date(2026, 11, 1)) == 2


def test_streak_stops_at_gap():
    days = ["2026-10-20", "2026-10-30", "2026-10-31", "2026-11-01"]

    assert checkin_service.streak_from(days, today=date(2026, 11, 1)) == 3


def test_streak_crosses_month_and_year_boundary():
    days = ["2026-12-31", "2027-01-01"]

    assert checkin_service.streak_from(days, today=date(2027, 1, 1)) == 2


def test_streak_ignores_old_history_when_last_checkin_is_stale():
    days = ["2026-09-01", "2026-09-02"]

    assert checkin_service.streak_from(days, today=date(2026, 11, 1)) == 0


# ------------------------------------------------------------------ month_grid
def test_month_grid_has_whole_weeks_starting_monday():
    grid = checkin_service.month_grid(2026, 11)  # 2026-11-01 是周日

    assert all(len(week) == 7 for week in grid)
    # 30 天 + 前面 6 个空位 = 36 格 → 6 周
    assert len(grid) == 6
    first_week = grid[0]
    assert [cell.empty for cell in first_week[:6]] == [True] * 6
    assert first_week[6].iso == "2026-11-01"


def test_month_grid_handles_leap_february():
    grid = checkin_service.month_grid(2028, 2)

    days = [cell for week in grid for cell in week if not cell.empty]
    assert len(days) == 29
    assert days[-1].iso == "2028-02-29"


def test_month_grid_does_not_mix_neighbour_months():
    grid = checkin_service.month_grid(2026, 12)

    isos = [cell.iso for week in grid for cell in week if not cell.empty]
    assert isos[0] == "2026-12-01"
    assert isos[-1] == "2026-12-31"
    assert all(iso.startswith("2026-12") for iso in isos)


# ------------------------------------------------------------------ 打卡流程
def test_check_today_twice_writes_one_row(sandbox):
    db = sandbox / "c.db"
    init_db(db)
    repo = CheckinRepository(db)

    first, first_new = checkin_service.check_today("先写的备注", today=date(2026, 11, 1), repo=repo)
    second, second_new = checkin_service.check_today("后写的备注", today=date(2026, 11, 1), repo=repo)

    assert first_new is True
    assert second_new is False
    assert repo.count() == 1
    assert repo.get("2026-11-01")["note"] == "先写的备注"
    assert first.checked_today is True and second.checked_today is True


def test_status_reports_streak_and_month(sandbox):
    db = sandbox / "c.db"
    init_db(db)
    repo = CheckinRepository(db)
    for day in ("2026-10-31", "2026-11-01"):
        repo.check(day)

    status = checkin_service.status(today=date(2026, 11, 1), repo=repo)

    assert status.checked_today is True
    assert status.streak == 2
    assert status.total == 2
    assert status.month_count == 1
    assert "2" in status.streak_text


def test_bad_db_path_raises_checkin_failed(sandbox):
    folder = sandbox / "not-a-db"
    folder.mkdir()

    with pytest.raises(checkin_service.CheckinFailed):
        checkin_service.status(today=date(2026, 11, 1), repo=CheckinRepository(folder))