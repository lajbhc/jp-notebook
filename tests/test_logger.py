"""日志单测：能写文件、能重复初始化、目录不可写也不崩。"""

from __future__ import annotations

import logging

from app import config, logger


def test_setup_logging_writes_to_file(sandbox, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", sandbox)
    log = logger.setup_logging(logging.INFO, console=False, force=True)

    logger.get_logger("tests").warning("识别失败：%s", "HTTP 500")

    text = (sandbox / "app.log").read_text(encoding="utf-8")
    assert "识别失败：HTTP 500" in text
    assert "WARNING" in text
    assert log.name == logger.LOGGER_NAME


def test_setup_logging_is_idempotent(sandbox, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", sandbox)
    logger.setup_logging(logging.INFO, console=False, force=True)
    handlers = list(logging.getLogger(logger.LOGGER_NAME).handlers)

    logger.setup_logging(logging.INFO, console=False)

    assert logging.getLogger(logger.LOGGER_NAME).handlers == handlers
    assert len([h for h in handlers if hasattr(h, "baseFilename")]) == 1


def test_setup_logging_survives_unwritable_dir(sandbox, monkeypatch):
    blocked = sandbox / "as-file"
    blocked.write_text("占位", encoding="utf-8")
    monkeypatch.setattr(config, "LOG_DIR", blocked)  # 目录位置是个文件

    log = logger.setup_logging(logging.INFO, console=False, force=True)

    log.warning("这条只会出现在终端")  # 不该抛异常
    assert log is not None


def test_log_exception_records_detail(sandbox, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", sandbox)
    log = logger.setup_logging(logging.INFO, console=False, force=True)

    class Boom(RuntimeError):
        detail = "sqlite3.OperationalError: no such table"

    logger.log_exception(log, "数据库初始化失败", Boom("炸了"))

    text = (sandbox / "app.log").read_text(encoding="utf-8")
    assert "数据库初始化失败" in text
    assert "no such table" in text