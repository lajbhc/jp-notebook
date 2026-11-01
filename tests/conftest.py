"""pytest 的公共准备。

两个作用：

1. 把项目根目录加进 `sys.path`，这样 `import app` 在任何工作目录下都能用
   （`python -m pytest` 从根目录跑其实不需要，但 `pytest tests/` 就需要）；
2. 提供 `make_entry` / `temp_db` 这类小工具，少写重复代码。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import models  # noqa: E402
from app.storage.db import init_db  # noqa: E402
from app.storage.repository import EntryRepository  # noqa: E402


def make_entry(entry_type: str = models.ENTRY_TYPE_WORD, **fields) -> models.Entry:
    """造一条能通过校验的记录。"""
    if entry_type == models.ENTRY_TYPE_WORD:
        base = {"word": "食べる", "reading": "たべる", "meaning": "吃"}
    else:
        base = {"grammar_point": "〜ことになる", "meaning": "客观上决定"}
    base.update(fields)
    base.setdefault("type", entry_type)
    return models.Entry(**base)


@pytest.fixture()
def sandbox(request) -> Path:
    """临时工作目录，用来代替 pytest 自带的 `tmp_path`。

    为什么不直接用 `tmp_path`：这台机器的进程跑在受限令牌下，Python 3.13 在
    Windows 上会真的按 `mode` 设 ACL，而 `tempfile.mkdtemp()` 固定用 `0o700`，
    于是系统临时目录里建出来的目录谁都进不去（详见 `docs/venv-bootstrap.md`）。
    pytest 的 `tmp_path` 正好走这条路，会直接 `PermissionError`。

    这里改成在项目自己的 ignored 目录里建普通目录（不指定 mode），跑完尽力删掉。
    """
    root = ROOT / ".tmp" / "pytest-work"
    root.mkdir(parents=True, exist_ok=True)
    path = root / request.node.name.replace("::", "_").replace("[", "_").replace("]", "")
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def db_path(sandbox: Path) -> Path:
    path = sandbox / "test.db"
    init_db(path)
    return path


@pytest.fixture()
def repo(db_path: Path) -> EntryRepository:
    return EntryRepository(db_path)


@pytest.fixture()
def entry_factory():
    return make_entry