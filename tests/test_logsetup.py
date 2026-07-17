import logging
from pathlib import Path

import pytest

from claudecode_discord_presence import logsetup


@pytest.fixture(autouse=True)
def _reset_logger():
    """Ensure a clean package logger before and after each test."""
    logger = logging.getLogger("claudecode_discord_presence")
    saved = logger.handlers[:]
    logger.handlers.clear()
    yield
    logger.handlers.clear()
    logger.handlers.extend(saved)


def test_configures_two_handlers(tmp_path):
    logger = logsetup.configure_logging(tmp_path / "x.log")
    kinds = {type(h).__name__ for h in logger.handlers}
    assert "RotatingFileHandler" in kinds
    assert "StreamHandler" in kinds


def test_idempotent(tmp_path):
    logsetup.configure_logging(tmp_path / "x.log")
    logger = logsetup.configure_logging(tmp_path / "x.log")
    assert len(logger.handlers) == 2  # not doubled


def test_writes_to_file(tmp_path):
    log_file = tmp_path / "x.log"
    logger = logsetup.configure_logging(log_file)
    logger.info("hello-marker")
    for h in logger.handlers:
        h.flush()
    assert "hello-marker" in log_file.read_text(encoding="utf-8")


def test_creates_parent_dir(tmp_path):
    log_file = tmp_path / "sub" / "deep" / "x.log"
    logsetup.configure_logging(log_file)
    assert log_file.parent.exists()


def test_log_file_default_path():
    assert logsetup.LOG_FILE.name == "claudecode-discord-presence.log"
    assert logsetup.LOG_FILE.parent.name == ".claude"
