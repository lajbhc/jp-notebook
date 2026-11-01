"""存储层单测：建表、增删改查、搜索转义、打卡表。

这些用例和 `scripts/verify_storage.py` 有意重叠一部分：脚本是给人看的"跑一遍
看结果"，单测是给 CI / 回归用的"失败就红"。两边都不贵，都留着。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from app import models
from app.storage.db import SCHEMA_VERSION, SchemaVersionError, get_schema_version, init_db
from app.storage.repository import CheckinRepository, EntryNotFound, EntryRepository

from .conftest import make_entry

TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


# --------------------------------------------------------------- 建表 / 版本
def test_init_db_creates_schema(db_path):
    assert get_schema_version(db_path) == SCHEMA_VERSION

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"entries", "checkins", "meta"} <= tables


def test_init_db_is_idempotent_and_keeps_data(db_path):
    repo = EntryRepository(db_path)
    created = repo.create(make_entry())

    init_db(db_path)  # 再跑一次

    assert EntryRepository(db_path).get(created.id).word == "食べる"


def test_refuses_unknown_schema_version(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")

    with pytest.raises(SchemaVersionError):
        init_db(db_path)


# ------------------------------------------------------------------ 增删改查
def test_create_fills_id_and_timestamps(repo):
    created = repo.create(make_entry())

    assert created.id > 0
    assert TIME_PATTERN.match(created.created_at), created.created_at
    assert created.updated_at == created.created_at
    assert created.type_label == models.TYPE_LABELS[models.ENTRY_TYPE_WORD]


def test_create_rejects_bad_type_and_empty_primary(repo):
    with pytest.raises(ValueError):
        repo.create(make_entry(type="kanji"))
    with pytest.raises(ValueError):
        repo.create(make_entry(word="   "))


def test_update_keeps_created_at(repo):
    created = repo.create(make_entry())
    created.meaning = "吃（东西）"
    updated = repo.update(created)

    assert updated.meaning == "吃（东西）"
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.created_at


def test_update_missing_row_raises(repo):
    ghost = make_entry()
    ghost.id = 999

    with pytest.raises(EntryNotFound):
        repo.update(ghost)


def test_delete_reports_whether_row_existed(repo):
    created = repo.create(make_entry())

    assert repo.delete(created.id) is True
    assert repo.delete(created.id) is False
    assert repo.get(created.id) is None


def test_grammar_and_word_share_one_table(repo):
    repo.create(make_entry())
    repo.create(make_entry(models.ENTRY_TYPE_GRAMMAR))

    assert repo.count_by_type() == {"word": 1, "grammar": 1}


# --------------------------------------------------------------------- 搜索
def test_search_hits_every_text_field(repo):
    repo.create(make_entry(meaning="吃", note="他動詞なので「を」を使う"))
    repo.create(make_entry(models.ENTRY_TYPE_GRAMMAR, connection="動詞辞書形 ＋ ことになる"))

    assert len(repo.search("食べる")) == 1
    assert len(repo.search("たべる")) == 1
    assert len(repo.search("ことになる")) == 1
    assert len(repo.search("辞書形")) == 1
    assert len(repo.search("完全没有的词")) == 0


def test_search_escapes_like_wildcards(repo):
    repo.create(make_entry(note="正答率 100% 的那题", word="一"))
    repo.create(make_entry(note="划线_标记", word="二"))

    assert len(repo.search("100%")) == 1
    assert len(repo.search("_")) == 1  # 匹配字面下划线，不是"任意一个字符"


def test_search_filters_by_type_and_orders_newest_first(repo):
    first = repo.create(make_entry(word="あ"))
    second = repo.create(make_entry(word="い"))

    words = repo.search(entry_type=models.ENTRY_TYPE_WORD)
    assert [item.id for item in words] == [second.id, first.id]
    assert repo.search(entry_type=models.ENTRY_TYPE_GRAMMAR) == []
    assert repo.count(entry_type=models.ENTRY_TYPE_WORD) == 2


def test_search_paginates(repo):
    for index in range(5):
        repo.create(make_entry(word=f"語{index}"))

    page1 = repo.search(limit=2, offset=0)
    page2 = repo.search(limit=2, offset=2)

    assert len(page1) == 2 and len(page2) == 2
    assert {item.id for item in page1}.isdisjoint({item.id for item in page2})


# ------------------------------------------------------------------ 打卡表
def test_checkin_insert_once_and_keeps_note(db_path):
    checkins = CheckinRepository(db_path)

    assert checkins.check("2026-11-01", note="第一天的备注") is True
    assert checkins.check("2026-11-01", note="第二次点") is False
    assert checkins.get("2026-11-01")["note"] == "第一天的备注"


def test_checkin_month_and_range_queries(db_path):
    checkins = CheckinRepository(db_path)
    checkins.check("2026-10-31")
    checkins.check("2026-11-01")
    checkins.check("2026-11-02")

    assert checkins.month_dates(2026, 11) == ["2026-11-01", "2026-11-02"]
    assert checkins.count("2026-11-01", "2026-11-30") == 2
    assert checkins.dates() == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert checkins.first_date() == "2026-10-31"


def test_checkin_remove(db_path):
    checkins = CheckinRepository(db_path)
    checkins.check("2026-11-01")

    assert checkins.remove("2026-11-01") is True
    assert checkins.exists("2026-11-01") is False
    assert checkins.remove("2026-11-01") is False