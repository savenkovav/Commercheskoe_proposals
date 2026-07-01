"""Централизованная настройка логирования приложения."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import LOG_DIR, LOG_FILE, LOG_LEVEL, PROJECT_ROOT


def setup_logging() -> None:
    root = logging.getLogger()
    if getattr(setup_logging, "_configured", False):
        return

    level_name = LOG_LEVEL.upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root.handlers.clear()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = Path(LOG_FILE) if LOG_FILE else Path(LOG_DIR) / "kp-assistant.log"
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "urllib3", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    setup_logging._configured = True  # type: ignore[attr-defined]
    logging.getLogger(__name__).info("Logging initialized: level=%s file=%s", level_name, log_path)
