"""Tests for agent.logging_config (structured logging setup)."""

from __future__ import annotations

import io
import logging

import pytest

from agent.logging_config import _ExtraFormatter, get_logger, setup_logging


@pytest.fixture(autouse=True)
def clean_root_logger():
    """Give every test a clean root logger, then restore the session's handlers.

    setup_logging() early-returns when the root already has handlers, so each
    test must start with none. We detach (without closing) the session's
    handlers up front and re-attach them afterwards, closing only the handlers
    a test added so RotatingFileHandler releases agent.log on Windows.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for h in saved_handlers:
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        try:
            h.flush()
            h.close()
        except Exception:
            pass
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _detach_root_handlers() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_setup_logging_attaches_console_and_file(tmp_path):
    _detach_root_handlers()
    logger = setup_logging(log_dir=tmp_path)

    assert logger.name == "caelum"
    root = logging.getLogger()
    assert len(root.handlers) == 2
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    assert (tmp_path / "agent.log").exists()


def test_setup_logging_idempotent_no_duplicate_handlers(tmp_path):
    _detach_root_handlers()
    setup_logging(log_dir=tmp_path)
    root = logging.getLogger()
    first = len(root.handlers)

    setup_logging(log_dir=tmp_path)  # second call must early-return
    assert len(root.handlers) == first


def test_setup_logging_respects_level(tmp_path):
    _detach_root_handlers()
    setup_logging(level="WARNING", log_dir=tmp_path)
    assert logging.getLogger().level == logging.WARNING

    setup_logging(level="not-a-real-level", log_dir=tmp_path)
    # Unknown level falls back to INFO rather than raising.
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_continues_when_file_handler_fails(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("logging.handlers.RotatingFileHandler", boom)
    _detach_root_handlers()

    setup_logging(log_dir=tmp_path)
    root = logging.getLogger()
    # Console handler still attached even though the file handler raised.
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], logging.StreamHandler)


def test_extra_formatter_flattens_extras():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_ExtraFormatter("%(message)s"))
    log = logging.getLogger("test.extra.formatter")
    log.propagate = False
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        log.info("connected", extra={"server": "windows", "tool": "Click"})
        handler.flush()
        out = stream.getvalue()
        assert "connected" in out
        assert "server='windows'" in out
        assert "tool='Click'" in out
    finally:
        log.removeHandler(handler)
        handler.close()


def test_get_logger_returns_named_logger():
    assert get_logger("caelum.foo").name == "caelum.foo"
