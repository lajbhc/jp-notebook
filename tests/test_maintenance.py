"""图片目录整理的单元测试：谁没被引用、谁重复、删的时候安不安全。

`scripts/verify_maintenance.py` 是完整故事，这里只留 CI 该守住的几条。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app import models
from app.services import entry_service, maintenance_service, upload_service
from app.storage.db import init_db
from app.storage.repository import EntryRepository


def _jpeg(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 14), color).save(path, "JPEG")
    return path


@pytest.fixture()
def uploads(sandbox: Path) -> Path:
    root = sandbox / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def repo(sandbox: Path) -> EntryRepository:
    db = sandbox / "app.db"
    init_db(db)
    return EntryRepository(db)


def test_scan_reports_orphans_and_incoming_is_ignored(uploads: Path, repo) -> None:
    used_source = _jpeg(uploads.parent / "src" / "used.jpg", (200, 30, 30))
    used = entry_service.store_image(used_source, root=uploads, today="2026-11-01")
    repo.create(models.Entry(type=models.ENTRY_TYPE_WORD, word="食べる", source_image=used))

    orphan_source = _jpeg(uploads.parent / "src" / "orphan.jpg", (30, 30, 200))
    orphan = entry_service.store_image(orphan_source, root=uploads, today="2026-11-02")

    incoming = uploads / "_incoming"
    incoming.mkdir()
    (incoming / "half.upload").write_bytes(b"x")

    report = maintenance_service.scan(root=uploads, repo=repo)

    assert len(report.files) == 2
    assert [item.relative_path for item in report.orphans] == [orphan]
    assert report.orphan_bytes == orphan_source.stat().st_size


def test_scan_groups_duplicates_by_content(uploads: Path, repo) -> None:
    source = _jpeg(uploads.parent / "src" / "dup.jpg", (10, 10, 10))
    stored = entry_service.store_image(source, root=uploads, today="2026-11-01")
    copy_dir = uploads / "2026-11-02"
    copy_dir.mkdir()
    shutil.copy2(uploads / stored, copy_dir / "another-name.jpg")

    report = maintenance_service.scan(root=uploads, repo=repo)

    assert len(report.duplicate_groups) == 1
    group = {item.relative_path for item in report.duplicate_groups[0]}
    assert group == {stored, "2026-11-02/another-name.jpg"}
    assert report.duplicate_bytes == source.stat().st_size


def test_reference_comparison_ignores_case(uploads: Path, repo) -> None:
    source = _jpeg(uploads.parent / "src" / "case.jpg", (5, 40, 5))
    stored = entry_service.store_image(source, root=uploads, today="2026-11-01")
    repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="走る",
            source_image=stored.upper(),  # Windows 上同一个文件
        )
    )

    report = maintenance_service.scan(root=uploads, repo=repo)

    assert report.orphans == ()


def test_delete_orphans_removes_empty_day_dir(uploads: Path, repo) -> None:
    source = _jpeg(uploads.parent / "src" / "gone.jpg", (9, 9, 200))
    orphan = entry_service.store_image(source, root=uploads, today="2026-11-05")

    result = maintenance_service.delete_orphans([orphan], root=uploads, repo=repo)

    assert [item.relative_path for item in result.deleted] == [orphan]
    assert result.freed_bytes == source.stat().st_size
    assert not (uploads / orphan).exists()
    assert not (uploads / "2026-11-05").exists()


def test_delete_orphans_skips_newly_referenced_file(uploads: Path, repo) -> None:
    """清单可能是几秒前的：删之前必须重新查一遍引用。"""
    source = _jpeg(uploads.parent / "src" / "race.jpg", (120, 120, 0))
    path = entry_service.store_image(source, root=uploads, today="2026-11-06")
    assert maintenance_service.scan(root=uploads, repo=repo).orphans

    repo.create(models.Entry(type=models.ENTRY_TYPE_WORD, word="食べる", source_image=path))
    result = maintenance_service.delete_orphans([path], root=uploads, repo=repo)

    assert result.deleted == ()
    assert result.skipped == (path,)
    assert (uploads / path).is_file()


def test_delete_orphans_ignores_missing_file(uploads: Path, repo) -> None:
    result = maintenance_service.delete_orphans(["2026-11-09/none.jpg"], root=uploads, repo=repo)

    assert result.deleted == ()
    assert result.failed == ()


def test_scan_fails_politely_when_db_is_broken(uploads: Path, sandbox: Path) -> None:
    broken = sandbox / "broken.db"
    broken.write_bytes(b"not a database")

    with pytest.raises(maintenance_service.MaintenanceFailed) as excinfo:
        maintenance_service.scan(root=uploads, repo=EntryRepository(broken))

    assert "数据库" in excinfo.value.user_message


def test_upload_log_rotates(monkeypatch, sandbox: Path) -> None:
    monkeypatch.setattr(upload_service.config, "LOG_DIR", sandbox / "logs")
    monkeypatch.setattr(upload_service, "UPLOAD_LOG_MAX_BYTES", 300)

    for index in range(20):
        upload_service.log_event(f"line {index} " + "x" * 40)

    log = upload_service.log_path()
    rotated = log.with_name(log.name + ".1")
    assert rotated.is_file()
    assert not log.with_name(log.name + ".2").exists()
    assert log.stat().st_size <= 1000