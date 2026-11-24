"""数据库备份与完整性检查。

为什么单独一个模块：`data/app.db` 是这个软件里**唯一不可再生**的东西（图片、日志都能重来，
记录不能）。所以备份不该散落在 `db.py` 和 `main.py` 里各写一半，而是集中在这里，
由启动流程统一调用。

三条原则：

1. **用 SQLite 自己的备份 API**（`Connection.backup`），不是 `shutil.copy2`。
   库开着 WAL 时，`.db` 文件本身可能缺少最近提交的内容，直接复制文件会得到一个
   "看起来正常、但少了几条记录"的备份——这种备份比没有更危险。
2. **备份失败不阻止启动**。用户真正要的是"能进去看记录"，备份是保险；
   保险买不到就先记一条 ERROR（在 `data/logs/app.log` 里），别把人挡在门外。
3. **只留有限份数**。每天一份、留 7 天；迁移前一份、留 5 份，避免把磁盘再吃满
   （这也是我们自己体检出来的问题之一）。

备份放在**库文件旁边**的 `backups/` 目录里：`data/app.db` → `data/backups/`。
这样校验脚本拿一个临时库跑，备份也落在它自己的临时目录，不会污染真实数据。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from app import config
from app.logger import get_logger

log = get_logger("storage.backup")

BACKUP_DIR_NAME = "backups"
DAILY_KEEP = 7
MIGRATE_KEEP = 5
DAILY_PATTERN = "app-daily-*.db"
MIGRATE_PATTERN = "app-before-migrate-*.db"


# --------------------------------------------------------------------- 路径
def _db_path(db_path: str | Path | None) -> Path:
    return Path(db_path) if db_path is not None else config.DB_PATH


def backup_dir(db_path: str | Path | None = None) -> Path:
    """备份目录：库文件旁边的 `backups/`（真实库就是 `data/backups/`）。"""
    return _db_path(db_path).parent / BACKUP_DIR_NAME


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------- 复制
def backup_database(target: str | Path, *, db_path: str | Path | None = None) -> Path | None:
    """把当前数据库完整复制到 `target`，返回目标路径。

    源库不存在（全新安装）时返回 None，不制造空备份。
    用 SQLite 的 backup API：它会正确处理 WAL 里还没落盘的事务。
    """
    source = _db_path(db_path)
    if not source.exists():
        return None

    destination = Path(target)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(source), timeout=5.0)) as src:
            with closing(sqlite3.connect(str(destination))) as dst:
                src.backup(dst)
    except (sqlite3.Error, OSError) as exc:
        log.error("备份失败 %s -> %s: %s", source, destination, exc)
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return destination


# --------------------------------------------------------------------- 每日
def daily_backup(
    *,
    today: str | None = None,
    db_path: str | Path | None = None,
    keep: int = DAILY_KEEP,
) -> Path | None:
    """每天第一次启动留一份快照，文件名带日期，重复调用不会重复备份。"""
    day = today or date.today().isoformat()
    target = backup_dir(db_path) / f"app-daily-{day}.db"
    if target.exists():
        return None

    written = backup_database(target, db_path=db_path)
    if written is not None:
        log.info("每日备份：%s（%d 字节）", written.name, written.stat().st_size)
    prune_backups(DAILY_PATTERN, keep, db_path=db_path)
    return written


# --------------------------------------------------------------------- 迁移前
def backup_before_migrate(
    from_version: int,
    to_version: int,
    *,
    db_path: str | Path | None = None,
    keep: int = MIGRATE_KEEP,
) -> Path | None:
    """在就地改表结构之前留一份，出事能原样退回去。

    注意：v2 的迁移只是 `ALTER TABLE ADD COLUMN`，本身很安全；但"改结构前有备份"
    这个习惯要从现在养成，免得将来某次重建表的迁移把人坑了。
    """
    name = f"app-before-migrate-v{from_version}-to-v{to_version}-{_stamp()}.db"
    written = backup_database(backup_dir(db_path) / name, db_path=db_path)
    if written is not None:
        log.warning("迁移 v%d→v%d 前已备份：%s", from_version, to_version, written.name)
    prune_backups(MIGRATE_PATTERN, keep, db_path=db_path)
    return written


# --------------------------------------------------------------------- 清理
def prune_backups(pattern: str, keep: int, *, db_path: str | Path | None = None) -> list[Path]:
    """按修改时间保留最新 `keep` 份，其余删掉，返回被删的路径。"""
    if keep < 1:
        keep = 1
    try:
        files = sorted(
            backup_dir(db_path).glob(pattern),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        log.warning("列备份文件失败：%s", exc)
        return []

    removed: list[Path] = []
    for path in files[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            log.warning("删旧备份失败 %s: %s", path.name, exc)
    return removed


# --------------------------------------------------------------------- 检查
def integrity_check(*, db_path: str | Path | None = None) -> str:
    """返回 `"ok"` 或一句人话问题描述；库不存在时也返回 ok（全新安装）。"""
    source = _db_path(db_path)
    if not source.exists():
        return "ok"
    try:
        with closing(sqlite3.connect(str(source), timeout=5.0)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        return f"打不开：{exc}"
    result = (row[0] if row else "") or ""
    if result.lower() == "ok":
        return "ok"
    return result or "完整性检查没有返回结果"


# --------------------------------------------------------------------- 启动
def startup_prepare(*, today: str | None = None, db_path: str | Path | None = None) -> str:
    """启动时该做的安全动作：先查完整性，再留每日快照。

    返回一句话，交给 `app.log` 记录；任何一步失败都只影响这句话的内容，
    不影响程序继续启动。
    """
    verdict = integrity_check(db_path=db_path)
    if verdict != "ok":
        log.error("数据库完整性检查没通过：%s", verdict)

    written = daily_backup(today=today, db_path=db_path)
    parts = [f"完整性检查：{verdict}"]
    parts.append(f"每日备份：{written.name}" if written else "每日备份：今天已经有了")
    return "；".join(parts)