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
from app.services import enricher, entry_service
from app.services.entry_service import EntryValidationError
from app.services.errors import ApiRequestFailed, ImageInvalid
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


def test_store_image_dedupes_identical_content(
    repo, upload_dir, sample_image
) -> None:
    """同一张图存两次只落一份：文件名是内容哈希。"""
    first = entry_service.store_image(sample_image, root=upload_dir, today="2026-11-01")
    second = entry_service.store_image(sample_image, root=upload_dir, today="2026-11-01")

    assert first == second
    assert len(list(upload_dir.rglob("*.png"))) == 1
    assert first.split("/")[1] != sample_image.name  # 不是原文件名


def test_store_image_dedupes_across_days(upload_dir, sample_image) -> None:
    """第二天再传同一张图，直接复用已有文件，不再新建日期目录。"""
    first = entry_service.store_image(sample_image, root=upload_dir, today="2026-11-01")
    second = entry_service.store_image(sample_image, root=upload_dir, today="2026-11-02")

    assert first == second
    assert not (upload_dir / "2026-11-02").exists()
    assert len(list(upload_dir.rglob("*.png"))) == 1


def test_store_image_keeps_different_images_apart(upload_dir, sandbox: Path) -> None:
    one = sandbox / "one.png"
    two = sandbox / "two.png"
    Image.new("RGB", (12, 12), (255, 0, 0)).save(one, "PNG")
    Image.new("RGB", (12, 12), (0, 255, 0)).save(two, "PNG")

    first = entry_service.store_image(one, root=upload_dir, today="2026-11-01")
    second = entry_service.store_image(two, root=upload_dir, today="2026-11-01")

    assert first != second
    assert len(list(upload_dir.rglob("*.png"))) == 2


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


# ---------------------------------------------------------------- 补全接线
def test_normalize_keeps_markers_only_for_filled_fields() -> None:
    entry = make_entry(
        models.ENTRY_TYPE_WORD,
        word="食べる",
        reading="たべる",
        meaning="吃",
        generated_fields="reading,meaning,example",
    )
    cleaned = entry_service.normalize(entry)
    # example 现在是空的（用户删了），就不该继续标成"AI 补的"
    assert cleaned.generated_fields == "reading,meaning"


def test_normalize_drops_markers_of_other_type() -> None:
    entry = make_entry(
        models.ENTRY_TYPE_GRAMMAR,
        grammar_point="〜ことになる",
        meaning="客观上决定",
        generated_fields="reading,meaning",
    )
    cleaned = entry_service.normalize(entry)
    assert cleaned.generated_fields == "meaning"


def test_recognize_and_enrich_merges_filled_fields(monkeypatch, sandbox: Path) -> None:
    monkeypatch.setattr(
        entry_service.recognizer,
        "recognize",
        lambda *args, **kwargs: make_entry(models.ENTRY_TYPE_WORD, word="食べる"),
    )
    monkeypatch.setattr(
        entry_service.enricher,
        "enrich",
        lambda entry, **kwargs: enricher.Enrichment(
            fields={"reading": "たべる", "meaning": "吃"}, rejected={}, raw={}
        ),
    )

    result = entry_service.recognize_and_enrich(sandbox / "x.jpg", models.ENTRY_TYPE_WORD)

    assert result.entry.reading == "たべる"
    assert result.entry.generated_names == ("meaning", "reading")
    assert result.enriched is True
    assert "补了 2 个字段" in result.note


def test_recognize_and_enrich_skips_when_disabled(monkeypatch, sandbox: Path) -> None:
    monkeypatch.setattr(
        entry_service.recognizer,
        "recognize",
        lambda *args, **kwargs: make_entry(models.ENTRY_TYPE_WORD, word="食べる"),
    )

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("auto_enrich=False 时不该调用补全")

    monkeypatch.setattr(entry_service.enricher, "enrich", should_not_be_called)

    result = entry_service.recognize_and_enrich(
        sandbox / "x.jpg", models.ENTRY_TYPE_WORD, auto_enrich=False
    )

    assert result.entry.word == "食べる"
    assert result.enriched is False
    assert result.note == ""


def test_enrichment_failure_keeps_recognized_entry(monkeypatch, sandbox: Path) -> None:
    monkeypatch.setattr(
        entry_service.recognizer,
        "recognize",
        lambda *args, **kwargs: make_entry(models.ENTRY_TYPE_WORD, word="食べる"),
    )

    def boom(entry, **kwargs):
        raise ApiRequestFailed(401, "bad key")

    monkeypatch.setattr(entry_service.enricher, "enrich", boom)

    result = entry_service.recognize_and_enrich(sandbox / "x.jpg", models.ENTRY_TYPE_WORD)

    assert result.entry.word == "食べる"
    assert result.enriched is False
    assert "API Key" in result.enrich_error
    assert "手动填写" in result.note