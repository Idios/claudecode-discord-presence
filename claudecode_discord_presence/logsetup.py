"""Logging configuration for the daemon process.

Only the long-lived daemon calls configure_logging(). CLI subcommands print to
stdout and the hook writes to stderr, so the rotating log file always has
exactly one writer — avoiding multi-process rotation corruption on Windows.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "claudecode-discord-presence.log"
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 2

_LOGGER_NAME = "claudecode_discord_presence"


def configure_logging(log_file: Path = LOG_FILE) -> logging.Logger:
    """Configure and return the package logger (daemon-only, idempotent)."""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger
