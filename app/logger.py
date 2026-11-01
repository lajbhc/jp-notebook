"""日志：写到 `data/logs/app.log`，按大小滚动。

为什么要有它：识别失败、接口报错、数据库写不进去，这些事在界面上只能给一句
人话，真正的原因（HTTP 状态码、异常栈）得有地方落下来，否则用户报"又失败了"
的时候完全没法查。手机上传另外有一份 `upload.log`（那是给排查手机端用的）。

两条纪律：

* 日志失败绝不能让程序挂掉——磁盘满了、目录没权限，都只是少一份日志；
* `setup_logging()` 可以重复调用（测试、重启都无所谓），不会重复挂 handler。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import config

LOGGER_NAME = "jp"
LOG_FILE_NAME = "app.log"
MAX_BYTES = 1024 * 1024  # 单文件 1MB
BACKUP_COUNT = 3  # 一共留 4 个文件（app.log + 3 个备份）
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def log_path() -> Path:
    """日志文件位置。每次现算，方便测试把 LOG_DIR 换到临时目录。"""
    return Path(config.LOG_DIR) / LOG_FILE_NAME


def _level_from_env(default: int = logging.INFO) -> int:
    raw = (os.environ.get("LOG_LEVEL") or "").strip().upper()
    if not raw:
        return default
    return getattr(logging, raw, default)


def setup_logging(
    level: int | None = None,
    *,
    console: bool | None = None,
    force: bool = False,
) -> logging.Logger:
    """准备日志，返回本项目的根 logger（名字是 `jp`）。

    :param level: 日志级别，默认读环境变量 `LOG_LEVEL`，再不行 INFO
    :param console: 是否同时打到 stderr，默认"有 stderr 就打"
    :param force: 强制重建 handler（测试里换目录后需要）
    """
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured and not force:
        return logger

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    resolved = _level_from_env(logging.INFO) if level is None else level
    logger.setLevel(resolved)
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    want_console = sys.stderr is not None if console is None else console
    if want_console:
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    try:
        target = log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            target,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # 目录不可写、磁盘满——只警告，不影响启动
        logger.warning("日志文件不可用（%s），本次只输出到终端", exc)

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """取子 logger，例如 `get_logger("services.recognizer")`。"""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    """把"给用户看的说明"和"给开发者看的详情"一起记下来。"""
    detail = getattr(exc, "detail", "") or str(exc)
    logger.error("%s：%s | %s", message, exc, detail)