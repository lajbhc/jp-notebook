"""数据库备份与完整性检查的单元测试。

和 `scripts/verify_backup.py` 的区别：那边是完整的验收故事（含 WAL 对照实验），
这里只留最该被 CI 守着的那几条，跑得快。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.storage import backup
from app.storage.db import SCHEMA_VERSION, init_db
from app.storage.repository import EntryRepository
from tests.conftest import make_entry


def _add(repo: EntryRepository, word: str = "食べる") -> None:
    repo.create(make_entry(word=word))


def _rows(path: Path) -> int:
    with closing(sqlite3.connect(str(path))) as conn:
        return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


def test_no_backup_before_database_exists(sandbox: Path) -> None:
    db = sandbox / "app.db"

    assert backup.daily_backup(db_path=db) is None
    assert backup.backup_database(sandbox / "backups" / "x.db", db_path=db) is None
    assert backup.integrity_check(db_path=db) == "ok"
    assert not (sandbox / "backups").exists()


def test_daily_backup_runs_once_per_day(sandbox: Path) -> None:
    db = sandbox / "app.db"
    init_db(db)
    _add(EntryRepository(db))

    first = backup.daily_backup(today="2026-11-24", db_path=db)
    assert first is not None
    assert first.name == "app-daily-2026-11-24.db"
    assert _rows(first) == 1

    assert backup.daily_backup(today="2026-11-24", db_path=db) is None
    assert len(list((sandbox / "backups").glob(backup.DAILY_PATTERN))) == 1


def test_old_backups_are_pruned(sandbox: Path) -> None:
    db = sandbox / "app.db"
    init_db(db)
    _add(EntryRepository(db))

    for day in range(1, 6):
        backup.daily_backup(today=f"2026-12-0{day}", db_path=db, keep=3)

    kept = sorted(p.name for p in (sandbox / "backups").glob(backup.DAILY_PATTERN))
    assert kept == [
        "app-daily-2026-12-03.db",
        "app-daily-2026-12-04.db",
        "app-daily-2026-12-05.db",
    ]


def test_backup_includes_uncheckpointed_wal_commit(sandbox: Path) -> None:
    db = sandbox / "app.db"
    init_db(db)
    _add(EntryRepository(db))

    writer = sqlite3.connect(str(db))
    try:
        writer.execute(
            "INSERT INTO entries (type, word, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("word", "走る", "2026-11-24 10:00:00", "2026-11-24 10:00:00"),
        )
        writer.commit()
        wal = db.with_name(db.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0

        target = sandbox / "backups" / "wal.db"
        assert backup.backup_database(target, db_path=db) is not None
        assert _rows(target) == 2
    finally:
        writer.close()


def test_migration_backs_up_old_schema_first(sandbox: Path) -> None:
    db = sandbox / "legacy.db"
    init_db(db)
    _add(EntryRepository(db))
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("ALTER TABLE entries DROP COLUMN generated_fields")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    init_db(db)

    saved = list((sandbox / "backups").glob(backup.MIGRATE_PATTERN))
    assert len(saved) == 1
    assert saved[0].name.startswith("app-before-migrate-v1-to-v2")
    with closing(sqlite3.connect(str(saved[0]))) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert "generated_fields" not in columns
    assert version == "1"

    # 已经是最新版本就不再产生迁移备份
    init_db(db)
    assert len(list((sandbox / "backups").glob(backup.MIGRATE_PATTERN))) == 1


def test_integrity_check_flags_garbage_file(sandbox: Path) -> None:
    db = sandbox / "app.db"
    db.write_bytes(b"not a sqlite database\n" * 100)

    verdict = backup.integrity_check(db_path=db)
    assert verdict != "ok"
    assert isinstance(verdict, str) and verdict


def test_backup_failure_is_not_fatal(sandbox: Path) -> None:
    db = sandbox / "app.db"
    init_db(db)
    (sandbox / "backups").write_text("我是一个文件，不是目录", encoding="utf-8")

    assert backup.daily_backup(today="2026-12-31", db_path=db) is None
    assert isinstance(backup.startup_prepare(db_path=db), str)


def test_startup_prepare_summary(sandbox: Path) -> None:
    db = sandbox / "app.db"
    init_db(db)

    summary = backup.startup_prepare(today="2027-01-01", db_path=db)
    assert "完整性检查：ok" in summary
    assert "app-daily-2027-01-01.db" in summary

    again = backup.startup_prepare(today="2027-01-01", db_path=db)
    assert "今天已经有了" in again
    with closing(sqlite3.connect(str(db))) as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == str(SCHEMA_VERSION)