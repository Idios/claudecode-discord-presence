import logging
from pathlib import Path

import pytest

from claudecode_discord_presence import logsetup


@pytest.fixture(autouse=True)
def _reset_logger():
    """Fully reset the package logger before and after each test.

    configure_logging() sets ``propagate = False``; pytest's logging plugin
    reacts by attaching its capture handler directly to this named logger
    (so it can still capture records that no longer propagate to root). That
    foreign handler would then trip configure_logging()'s ``if
    logger.handlers`` idempotency guard in a later test, making it skip its
    setup — a failure that only surfaced under the pytest version CI installs
    (9.1). Resetting ``propagate`` and ``level`` (not just the handler list),
    and closing handlers, keeps each test isolated regardless of the plugin.
    """
    logger = logging.getLogger("claudecode_discord_presence")

    def _reset() -> None:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        logger.propagate = True
        logger.setLevel(logging.NOTSET)

    _reset()
    yield
    _reset()


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
