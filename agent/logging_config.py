"""Structured logging utilities for Caelum-Agent."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any


class _ExtraFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Flatten extra fields into the message for console/file readability.
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in logging.LogRecord(None, None, "", 0, "", (), None).__dict__
            and not k.startswith("_")
        }
        if extras:
            pairs = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
            record.msg = f"{record.msg} | {pairs}"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str = "./data/logs",
    fmt: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
) -> logging.Logger:
    """Configure root logger with console + rotating file handlers."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers if setup_logging is called multiple times.
    if root.handlers:
        return logging.getLogger("caelum")

    formatter = _ExtraFormatter(fmt)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    try:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_path / "agent.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as exc:
        root.warning("Failed to create file logger: %s", exc)

    return logging.getLogger("caelum")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
