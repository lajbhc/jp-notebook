"""验收：数据库备份与完整性检查（阶段 9 · 第 1 步）。

不联网、不弹窗。全部在自己的临时目录里跑，跑完删掉。

重点验四件容易写错的事：

1. 备份用的是 SQLite 备份 API 而不是复制文件 —— WAL 里还没落盘的那条记录也得进备份；
2. 每天只备份一次（第二次调用返回 None，不重复写）；
3. 备份份数有上限（旧文件会被删掉，不是只增不减）；
4. 备份失败不能把启动搞崩 —— 目录不可写时返回 None 而不是抛异常。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

WORK = ROOT / "data" / "_verify_backup"
DB = WORK / "app.db"
config.DATA_DIR = WORK
config.UPLOAD_DIR = WORK / "uploads"
config.LOG_DIR = WORK / "logs"
config.DB_PATH = DB
shutil.rmtree(WORK, ignore_errors=True)
config.ensure_dirs()

from app import models  # noqa: E402
from app.storage import backup  # noqa: E402
from app.storage.db import SCHEMA_VERSION, init_db  # noqa: E402
from app.storage.repository import EntryRepository  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}" + (f" -> {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n【{title}】")


def rows_in(path: Path) -> int:
    with closing(sqlite3.connect(str(path))) as conn:
        return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


def make_repo() -> EntryRepository:
    return EntryRepository(DB)


# ---------------------------------------------------------------- 1. 全新安装
section("1. 全新安装：没有库就不制造空备份")
check("库文件还不存在", not DB.exists())
check("daily_backup 返回 None", backup.daily_backup() is None)
check("备份目录没有被建出来（或建了也是空的）",
      not (WORK / "backups").exists() or not any((WORK / "backups").iterdir()))
check("源库不存在时 backup_database 返回 None",
      backup.backup_database(WORK / "backups" / "nope.db") is None)
check("库不存在时完整性检查返回 ok", backup.integrity_check() == "ok")

# ---------------------------------------------------------------- 2. 正常备份
section("2. 有数据之后：备份内容与源库一致")
init_db()
repo = make_repo()
repo.create(models.Entry(type=models.ENTRY_TYPE_WORD, word="食べる", meaning="吃"))
repo.create(models.Entry(type=models.ENTRY_TYPE_GRAMMAR, grammar_point="〜ことになる"))

first = backup.daily_backup(today="2026-11-24")
check("第一次备份生成了文件", first is not None and first.exists(), str(first))
check("文件名带日期", first is not None and first.name == "app-daily-2026-11-24.db")
check("备份里的记录数与源库一致", first is not None and rows_in(first) == 2, str(first))

second = backup.daily_backup(today="2026-11-24")
check("同一天再调一次返回 None", second is None)
check("备份目录里仍然只有 1 份",
      len(list((WORK / "backups").glob(backup.DAILY_PATTERN))) == 1)

# ---------------------------------------------------------------- 3. WAL 里的提交
section("3. WAL 里还没合并的提交也要备份到（复制文件会漏）")
writer = sqlite3.connect(str(DB))
writer.execute("PRAGMA journal_mode = WAL")
writer.execute(
    "INSERT INTO entries (type, word, created_at, updated_at) VALUES (?, ?, ?, ?)",
    ("word", "走る", "2026-11-24 10:00:00", "2026-11-24 10:00:00"),
)
writer.commit()

wal = DB.with_name(DB.name + "-wal")
check("WAL 文件存在且非空（这条提交还只在 WAL 里）",
      wal.exists() and wal.stat().st_size > 0, str(wal))

live_backup = WORK / "backups" / "wal-check.db"
written = backup.backup_database(live_backup)
check("备份成功", written is not None)
check("备份里有刚提交但还没 checkpoint 的记录", written is not None and rows_in(written) == 3,
      str(written))

naive = WORK / "backups" / "naive-copy.db"
shutil.copy2(DB, naive)
naive_rows = rows_in(naive)
check("（对照）只复制 .db 文件会漏掉这条记录", naive_rows == 2, f"naive_rows={naive_rows}")
writer.close()

# ---------------------------------------------------------------- 4. 保留份数
section("4. 旧备份会被删掉，不是只增不减")
for day in range(1, 6):
    backup.daily_backup(today=f"2026-12-0{day}", keep=3)
time.sleep(0.01)
files = sorted((WORK / "backups").glob(backup.DAILY_PATTERN))
check("保留上限生效（3 份）", len(files) == 3, f"{[f.name for f in files]}")
check("留下的是最新那几天",
      {f.name for f in files} == {"app-daily-2026-12-03.db", "app-daily-2026-12-04.db",
                                  "app-daily-2026-12-05.db"},
      str(sorted(f.name for f in files)))
check("keep=0 也会被夹到至少留 1 份",
      len(backup.prune_backups(backup.DAILY_PATTERN, 0)) == 2
      and len(list((WORK / "backups").glob(backup.DAILY_PATTERN))) == 1,
      str(sorted(p.name for p in (WORK / "backups").glob(backup.DAILY_PATTERN))))

# ---------------------------------------------------------------- 5. 迁移前备份
section("5. 迁移前自动备份：备份里是迁移前的结构")
shutil.rmtree(WORK / "backups", ignore_errors=True)
legacy = WORK / "legacy.db"
init_db(legacy)
with closing(sqlite3.connect(str(legacy))) as conn:
    conn.execute(
        "INSERT INTO entries (type, word, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("word", "移行テスト", "2026-11-01 09:00:00", "2026-11-01 09:00:00"),
    )
    conn.execute("ALTER TABLE entries DROP COLUMN generated_fields")
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

check("退化后的库版本是 1", backup.integrity_check(db_path=legacy) == "ok")
init_db(legacy)

migrated = sorted((WORK / "backups").glob(backup.MIGRATE_PATTERN))
check("迁移前留下一份备份", len(migrated) == 1, str([p.name for p in migrated]))
check("备份文件名标了版本", migrated and migrated[0].name.startswith("app-before-migrate-v1-to-v2"))
with closing(sqlite3.connect(str(migrated[0]))) as conn:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    kept = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
check("备份是迁移前的结构（没有 generated_fields）", "generated_fields" not in columns)
check("备份里的版本还是 1", version == "1", version)
check("备份里的数据还在", kept == 1, str(kept))

with closing(sqlite3.connect(str(legacy))) as conn:
    now_columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    now_version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
check("正式库已经升到最新版本", now_version == str(SCHEMA_VERSION), now_version)
check("正式库补上了新列", "generated_fields" in now_columns)
check("迁移没有重复备份（第二次 init_db 不再产生备份）", len(backup.prune_backups(backup.MIGRATE_PATTERN, 5)) == 0)
check("再次 init_db 后备份数不变",
      len(list((WORK / "backups").glob(backup.MIGRATE_PATTERN))) == 1)
init_db(legacy)
check("第三次 init_db 也不再多备份",
      len(list((WORK / "backups").glob(backup.MIGRATE_PATTERN))) == 1)

# ---------------------------------------------------------------- 6. 完整性检查
section("6. 完整性检查能认出损坏的文件")
check("好库返回 ok", backup.integrity_check(db_path=legacy) == "ok")
broken = WORK / "broken.db"
broken.write_bytes(b"this is definitely not a sqlite database\n" * 50)
verdict = backup.integrity_check(db_path=broken)
check("坏文件不会返回 ok", verdict != "ok", verdict)
check("坏文件给出的是人话", isinstance(verdict, str) and len(verdict) > 0, verdict)

# ---------------------------------------------------------------- 7. 失败不崩
section("7. 备份失败不能把启动搞崩")
blocked = WORK / "blocked"
blocked.mkdir()
blocked_db = blocked / "app.db"
init_db(blocked_db)
(blocked / "backups").write_text("我是一个文件，不是目录", encoding="utf-8")
check("备份目录被文件占住时 daily_backup 返回 None 而不是抛异常",
      backup.daily_backup(today="2026-12-31", db_path=blocked_db) is None)
check("startup_prepare 也不会抛异常", isinstance(backup.startup_prepare(db_path=blocked_db), str))

# ---------------------------------------------------------------- 8. startup_prepare
section("8. startup_prepare 返回一句能进日志的话")
summary = backup.startup_prepare(today="2027-01-01")
check("提到了完整性检查", "完整性检查：ok" in summary, summary)
check("提到了每日备份", "每日备份：app-daily-2027-01-01.db" in summary, summary)
again = backup.startup_prepare(today="2027-01-01")
check("同一天第二次说明已经有了", "今天已经有了" in again, again)

# ---------------------------------------------------------------- 收尾
shutil.rmtree(WORK, ignore_errors=True)
print(f"\n===== 通过 {PASSED} 项，失败 {FAILED} 项 =====")
sys.exit(0 if FAILED == 0 else 1)