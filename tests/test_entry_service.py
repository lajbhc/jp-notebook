"""录入服务（app/services/entry_service.py）的单元测试。

重点守两件事：

* 校验在复制图片**之前**——表单填错了不该在 uploads 里留下一张没人引用的图；
* 同一张图连续录入时，后一条能复用前一条已经存下的相对路径，不再重复复制。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app import config, models
from app.services import entry_service
from app.services.entry_service import EntryValidationError
from app.services.errors import ImageInvalid
from tests.conftest import make_entry


@pytest.fixture()
def upload_dir(sandbox: Path, monkeypatch) -> Path:
    """把 uploads 指到临时目录，别弄脏真实的 data/uploads。"""
    root = sandbox / "uploads"
    root.mkdir()
    monkeypatch.setattr(config, "UPLOAD_DIR", root)
    return root


@pytest.fixture()
def sample_image(sandbox: Path) -> Path:
    path = sandbox / "page.png"
    Image.new("RGB", (240, 160), (250, 250, 250)).save(path, "PNG")
    return path


def test_normalize_strips_and_clears_other_type_fields() -> None:
    entry = make_entry(
        models.ENTRY_TYPE_GRAMMAR,
        word="  食べる  ",
        grammar_point="  〜ことになる ",
        meaning=" 客观上决定 ",
    )

    cleaned = entry_service.normalize(entry)

    assert cleaned.grammar_point == "〜ことになる"
    assert cleaned.meaning == "客观上决定"
    # 语法记录里不该留着生词的词形
    assert cleaned.word == ""
    # 不修改传进来的对象
    assert entry.word == "  食べる  "


def test_normalize_requires_primary_field() -> None:
    with pytest.raises(EntryValidationError) as excinfo:
        entry_service.normalize(models.Entry(type=models.ENTRY_TYPE_WORD, meaning="吃"))
    assert "词形" in excinfo.value.user_message


def test_normalize_rejects_unknown_type() -> None:
    with pytest.raises(EntryValidationError):
        entry_service.normalize(models.Entry(type="kanji", word="食べる"))


def test_invalid_entry_does_not_copy_image(repo, upload_dir, sample_image) -> None:
    """校验没过时不该落图：不然连续录几条失败就会攒一堆孤儿文件。"""
    bad = models.Entry(type=models.ENTRY_TYPE_WORD, meaning="吃")  # 缺词形

    with pytest.raises(EntryValidationError):
        entry_service.save_entry(bad, image_source=sample_image, repo=repo)

    assert list(upload_dir.rglob("*.*")) == []
    assert repo.count() == 0


def test_save_entry_with_image_returns_relative_path(repo, upload_dir, sample_image) -> None:
    saved = entry_service.save_entry(
        make_entry(), image_source=sample_image, repo=repo
    )

    assert saved.id is not None
    assert saved.source_image.endswith(".png")
    assert ":" not in saved.source_image  # 绝不存绝对路径
    assert (upload_dir / saved.source_image).is_file()


def test_second_entry_can_reuse_stored_image(repo, upload_dir, sample_image) -> None:
    """同一张纸录第二条：把上一条的 source_image 传进去，就不该再复制一份。"""
    first = entry_service.save_entry(make_entry(), image_source=sample_image, repo=repo)
    copied = len(list(upload_dir.rglob("*.png")))
    assert copied == 1

    second = models.Entry(
        type=models.ENTRY_TYPE_WORD,
        word="走る",
        reading="はしる",
        meaning="跑",
        source_image=first.source_image,
    )
    saved = entry_service.save_entry(second, repo=repo)  # 不传 image_source

    assert saved.source_image == first.source_image
    assert len(list(upload_dir.rglob("*.png"))) == copied


def test_store_image_rejects_missing_file(upload_dir, sandbox: Path) -> None:
    with pytest.raises(ImageInvalid) as excinfo:
        entry_service.store_image(sandbox / "nope.jpg")
    assert "重新选择图片" in excinfo.value.user_message


def test_store_image_rejects_non_image(upload_dir, sandbox: Path) -> None:
    fake = sandbox / "fake.jpg"
    fake.write_bytes(b"not an image")
    with pytest.raises(ImageInvalid):
        entry_service.store_image(fake)


def test_absolute_image_path_returns_none_for_missing(upload_dir) -> None:
    assert entry_service.absolute_image_path("") is None
    assert entry_service.absolute_image_path("2026-01-01/ghost.jpg") is None