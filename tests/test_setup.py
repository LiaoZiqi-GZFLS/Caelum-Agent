"""Tests for setup.py helpers."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import setup


@pytest.fixture
def temp_config(tmp_path: Path, monkeypatch: Any) -> tuple[Path, Path]:
    example = tmp_path / "config.yaml.example"
    config = tmp_path / "config.yaml"
    example.write_text(
        "llm:\n"
        "  provider: kimi\n"
        "  base_url: https://api.moonshot.cn/v1\n"
        "  api_key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "  model: kimi-k2.6\n"
        "mcp_servers:\n"
        "  playwright:\n"
        "    command: npx\n"
        "    args: []\n"
        "    env: {}\n"
        "  windows:\n"
        "    command: windows-mcp\n"
        "    args: [serve]\n"
        "    env: {}\n"
        "  filesystem:\n"
        "    command: npx\n"
        "    args: []\n"
        "    env: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup, "CONFIG_EXAMPLE", example)
    monkeypatch.setattr(setup, "CONFIG_FILE", config)
    return example, config


def test_copy_config_creates_file_from_example(temp_config: tuple[Path, Path]) -> None:
    example, config = temp_config
    assert not config.exists()

    setup.copy_config()

    assert config.exists()
    assert "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" in config.read_text(encoding="utf-8")


def test_copy_config_skips_existing_file(temp_config: tuple[Path, Path]) -> None:
    example, config = temp_config
    config.write_text("existing: true", encoding="utf-8")

    setup.copy_config()

    assert config.read_text(encoding="utf-8") == "existing: true"


def test_inject_api_key_replaces_placeholder(temp_config: tuple[Path, Path]) -> None:
    _, config = temp_config
    setup.copy_config()

    setup.inject_api_key_into_config("sk-real-key")

    text = config.read_text(encoding="utf-8")
    assert "api_key: sk-real-key" in text
    assert "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in text


def test_validate_config_fails_with_placeholder(temp_config: tuple[Path, Path]) -> None:
    _, config = temp_config
    setup.copy_config()

    ok, message = setup.validate_config()

    assert not ok
    assert "placeholder" in message


def test_validate_config_passes_with_real_key(temp_config: tuple[Path, Path]) -> None:
    _, config = temp_config
    setup.copy_config()
    setup.inject_api_key_into_config("sk-real-key")

    ok, message = setup.validate_config()

    assert ok
    assert "valid" in message


def test_validate_config_fails_when_file_missing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(setup, "CONFIG_FILE", tmp_path / "missing.yaml")

    ok, message = setup.validate_config()

    assert not ok
    assert "not found" in message


def test_prompt_for_api_key_returns_arg() -> None:
    assert setup.prompt_for_api_key("  sk-from-arg  ") == "sk-from-arg"


def test_prompt_for_api_key_reads_input(monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "sk-from-stdin")

    assert setup.prompt_for_api_key() == "sk-from-stdin"


def test_prompt_for_api_key_returns_none_on_eof(monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))

    assert setup.prompt_for_api_key() is None


def test_prompt_for_api_key_returns_none_when_not_tty(monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert setup.prompt_for_api_key() is None
