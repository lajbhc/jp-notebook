"""记录表的增删改查。

设计约束
--------
1. 所有 SQL 只出现在本文件里。ui 层不写 SQL，services 层也不写，它只调这里的方法。
2. 参数一律走占位符 ``?``；模糊查询里的 ``%`` ``_`` 由 ``_like_pattern()`` 转义，
   避免用户搜 ``100%`` 时把通配符当语法用。
3. 每次调用开一个短连接（见 db.connect 的说明），因此本类可以被 UI 线程和
   FastAPI 线程共用，不需要额外加锁。
4. 时间戳由本层统一生成，不接受调用方传入，避免各处格式不一致。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app import models
from app.storage.db import StorageError, connect

# 关键词会搜这些列（都是文本列）。加字段时记得同步这里。
_SEARCHABLE_COLUMNS: tuple[str, ...] = (
    "word",
    "reading",
    "meaning",
    "part_of_speech",
    "grammar_point",
    "connection",
    "example",
    "example_cn",
    "error_reason",
    "note",
)

# 允许通过 create/update 写入的列（id 与 created_at 不在此列）
_WRITABLE_COLUMNS: tuple[str, ...] = (
    "type",
    *_SEARCHABLE_COLUMNS,
    "source_image",
)

# 主字段的中文名，用于报错信息
_PRIMARY_LABELS: dict[str, str] = {
    "word": "词形",
    "grammar_point": "语法点",
}

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class EntryNotFound(StorageError):
    """要操作的记录不存在（通常是被另一个窗口删掉了）。"""


def now_text() -> str:
    """当前本地时间，统一成 'YYYY-MM-DD HH:MM:SS'。"""
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def _like_pattern(keyword: str) -> str:
    """把用户输入转成 LIKE 模式，转义通配符。配合 SQL 里的 ESCAPE '\\' 使用。"""
    escaped = (
        keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _row_to_entry(row) -> models.Entry:
    """sqlite3.Row -> Entry。只在缺失列时抛出 KeyError，说明表结构与代码不一致。"""
    return models.Entry(
        id=row["id"],
        type=row["type"],
        word=row["word"],
        reading=row["reading"],
        meaning=row["meaning"],
        part_of_speech=row["part_of_speech"],
        grammar_point=row["grammar_point"],
        connection=row["connection"],
        example=row["example"],
        example_cn=row["example_cn"],
        error_reason=row["error_reason"],
        note=row["note"],
        source_image=row["source_image"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate(entry: models.Entry) -> None:
    """入库前的最低限度校验。真正面向用户的提示由 services 层负责。"""
    if entry.type not in models.ENTRY_TYPES:
        raise ValueError(f"未知的记录类型：{entry.type!r}，只支持 {models.ENTRY_TYPES}")

    primary = models.PRIMARY_FIELD[entry.type]
    if not getattr(entry, primary, "").strip():
        label = _PRIMARY_LABELS[primary]
        raise ValueError(f"{entry.type_label}记录的「{label}」不能为空")


class EntryRepository:
    """生词 / 语法记录的数据访问对象。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        from app.storage.db import resolve_db_path

        return resolve_db_path(self._db_path)

    # ------------------------------------------------------------- 写
    def create(self, entry: models.Entry) -> models.Entry:
        """插入一条记录，返回带 id 和时间戳的新对象。"""
        _validate(entry)
        stamp = now_text()
        columns = ", ".join(_WRITABLE_COLUMNS)
        placeholders = ", ".join("?" for _ in _WRITABLE_COLUMNS)
        values = [getattr(entry, column) for column in _WRITABLE_COLUMNS]

        with connect(self._db_path) as conn:
            cursor = conn.execute(
                f"INSERT INTO entries ({columns}, created_at, updated_at) "
                f"VALUES ({placeholders}, ?, ?)",
                (*values, stamp, stamp),
            )
            new_id = int(cursor.lastrowid)

        created = self.get(new_id)
        if created is None:  # 理论上不可能，除非刚插入就被别处删掉
            raise EntryNotFound(f"插入后读取不到记录 id={new_id}")
        return created

    def update(self, entry: models.Entry) -> models.Entry:
        """按 id 更新可写字段，并刷新 updated_at。created_at 永远不动。"""
        if entry.id is None:
            raise ValueError("更新记录时 entry.id 不能为空")
        _validate(entry)

        assignments = ", ".join(f"{column} = ?" for column in _WRITABLE_COLUMNS)
        values = [getattr(entry, column) for column in _WRITABLE_COLUMNS]

        with connect(self._db_path) as conn:
            cursor = conn.execute(
                f"UPDATE entries SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, now_text(), entry.id),
            )
            if cursor.rowcount == 0:
                raise EntryNotFound(f"记录 id={entry.id} 不存在，可能已被删除")

        updated = self.get(entry.id)
        if updated is None:
            raise EntryNotFound(f"更新后读取不到记录 id={entry.id}")
        return updated

    def delete(self, entry_id: int) -> bool:
        """删除一条记录，返回是否真的删掉了。"""
        with connect(self._db_path) as conn:
            cursor = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------- 读
    def get(self, entry_id: int) -> models.Entry | None:
        with connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row is not None else None

    def search(
        self,
        keyword: str = "",
        entry_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[models.Entry]:
        """按关键词 / 类型 / 日期区间筛选，按创建时间倒序分页。

        keyword 为空表示不按关键词过滤；entry_type 为 None 表示两类都要；
        date_from / date_to 是 'YYYY-MM-DD'，闭区间。
        """
        where, params = self._build_filters(keyword, entry_type, date_from, date_to)
        limit = max(1, int(limit))
        offset = max(0, int(offset))

        with connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM entries {where} "
                f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def count(
        self,
        keyword: str = "",
        entry_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        """与 search 同条件的总数，用于分页。"""
        where, params = self._build_filters(keyword, entry_type, date_from, date_to)
        with connect(self._db_path) as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM entries {where}", params).fetchone()
        return int(row["n"])

    def count_by_type(self) -> dict[str, int]:
        """各类记录条数，例如 {'word': 12, 'grammar': 5}。首页统计用。"""
        result = {entry_type: 0 for entry_type in models.ENTRY_TYPES}
        with connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT type, COUNT(*) AS n FROM entries GROUP BY type"
            ).fetchall()
        for row in rows:
            result[row["type"]] = int(row["n"])
        return result

    def today_count(self, today: str | None = None) -> int:
        """今天新增了几条（按本地日期比较 created_at 前 10 位）。"""
        day = today or date.today().isoformat()
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE substr(created_at, 1, 10) = ?",
                (day,),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------- 内部
    def _build_filters(
        self,
        keyword: str,
        entry_type: str | None,
        date_from: str | None,
        date_to: str | None,
    ) -> tuple[str, list[object]]:
        """拼 WHERE 子句。只有这里会拼 SQL 片段，且值一律用占位符。"""
        clauses: list[str] = []
        params: list[object] = []

        keyword = (keyword or "").strip()
        if keyword:
            pattern = _like_pattern(keyword)
            or_parts = " OR ".join(
                f"{column} LIKE ? ESCAPE '\\'" for column in _SEARCHABLE_COLUMNS
            )
            clauses.append(f"({or_parts})")
            params.extend([pattern] * len(_SEARCHABLE_COLUMNS))

        if entry_type:
            if entry_type not in models.ENTRY_TYPES:
                raise ValueError(f"未知的记录类型：{entry_type!r}")
            clauses.append("type = ?")
            params.append(entry_type)

        if date_from:
            clauses.append("substr(created_at, 1, 10) >= ?")
            params.append(date_from)

        if date_to:
            clauses.append("substr(created_at, 1, 10) <= ?")
            params.append(date_to)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params