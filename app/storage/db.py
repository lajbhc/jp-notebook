"""SQLite 连接与表结构。

设计约束
--------
1. 只做"怎么连、表长什么样"，不写任何业务 SQL（增删改查在 repository.py）。
2. 每个操作开一个短连接、用完即关（见 ``connect()``）。原因：桌面端 UI 线程、
   FastAPI 的上传线程都会碰数据库，长连接 + ``check_same_thread`` 很容易在
   后续阶段埋一个"另一个线程里报 ProgrammingError"的坑。本机单用户、数据量
   500 级别，开关连接的开销可以忽略。
3. 路径一律由 config 提供；调用方可以传自定义路径（校验脚本、测试用）。
4. 建表语句全部幂等（IF NOT EXISTS），重复调用 init_db 不会报错也不会丢数据。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app import config

# 表结构版本。将来加字段/加表时 +1，并在 init_db 里补迁移分支。
# 现在只有 v1，所以版本不匹配时直接报错，而不是猜着改用户的库。
SCHEMA_VERSION = 1

META_SCHEMA_VERSION_KEY = "schema_version"


class StorageError(RuntimeError):
    """存储层错误基类。UI 只捕获这个，具体原因是哪个子类由 services 处理。"""


class SchemaVersionError(StorageError):
    """数据库文件的表结构版本与本代码不一致（通常是用了新版程序建的库）。"""


# 生词与语法共用一张表，缺失字段留空串（NOT NULL DEFAULT ''），避免到处都是 None 判断。
# created_at / updated_at 存本地时间的 'YYYY-MM-DD HH:MM:SS'，字符串可直接比较排序。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT    NOT NULL CHECK (type IN ('word', 'grammar')),
    word            TEXT    NOT NULL DEFAULT '',
    reading         TEXT    NOT NULL DEFAULT '',
    meaning         TEXT    NOT NULL DEFAULT '',
    part_of_speech  TEXT    NOT NULL DEFAULT '',
    grammar_point   TEXT    NOT NULL DEFAULT '',
    connection      TEXT    NOT NULL DEFAULT '',
    example         TEXT    NOT NULL DEFAULT '',
    example_cn      TEXT    NOT NULL DEFAULT '',
    error_reason    TEXT    NOT NULL DEFAULT '',
    note            TEXT    NOT NULL DEFAULT '',
    source_image    TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_type       ON entries(type);
CREATE INDEX IF NOT EXISTS idx_entries_created_at ON entries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_word       ON entries(word);

-- 每日打卡：date 唯一，重复打卡靠 UNIQUE 约束在数据库层面兜住
CREATE TABLE IF NOT EXISTS checkins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL UNIQUE,   -- YYYY-MM-DD
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

-- 键值元信息，目前只存 schema_version，将来可放"上次提醒时间"等
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """确定数据库文件路径，并保证父目录存在（首次运行时 data/ 还不存在）。"""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """打开一个短连接；正常结束自动提交，异常自动回滚。"""
    path = resolve_db_path(db_path)
    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as exc:  # 目录不可写、文件被占用等
        raise StorageError(f"无法打开数据库 {path}: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        # 把 sqlite3 的原始异常统一包成 StorageError：上层（services / ui）
        # 只需认识一种异常，不用 import sqlite3，也不会漏掉"表不存在""库损坏"
        # 这类只有运行期才会暴露的错误。
        conn.rollback()
        raise StorageError(f"数据库操作失败 {path}: {exc}") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path | None = None) -> None:
    """建表并写入 schema_version（幂等）。"""
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)

        # WAL 让"桌面端读"和"局域网服务写"同时进行时少一些锁冲突。
        # journal_mode 是持久化到库文件里的，设一次即可。
        conn.execute("PRAGMA journal_mode = WAL")

        current = _read_schema_version(conn)
        if current is None:
            _write_meta(conn, META_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return

        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"数据库表结构版本为 {current}，但本程序只支持到 {SCHEMA_VERSION}。"
                f"请升级程序，或备份后删除 {resolve_db_path(db_path)} 重新开始。"
            )
        if current < SCHEMA_VERSION:
            raise SchemaVersionError(
                f"数据库表结构版本为 {current}，本程序需要 {SCHEMA_VERSION}。"
                f"缺少迁移脚本，请先备份数据库。"
            )


def get_schema_version(db_path: str | Path | None = None) -> int | None:
    """读取库里的表结构版本；库还没初始化时返回 None。"""
    with connect(db_path) as conn:
        return _read_schema_version(conn)


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_SCHEMA_VERSION_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError) as exc:
        raise SchemaVersionError(f"meta.schema_version 内容非法: {row['value']!r}") from exc


def _write_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )