"""Tests for Pydantic configuration."""

from pathlib import Path

import pytest
import yaml

from agent.config import Config, LLMConfig, load_config


def test_llm_model_name_normalization():
    cfg = LLMConfig(api_key="test", model="kimi-k2-6")
    assert cfg.model == "kimi-k2.6"


def test_llm_config_repr_hides_api_key():
    cfg = LLMConfig(api_key="sk-secret-key", model="kimi-k2.6")
    representation = repr(cfg)
    assert "sk-secret-key" not in representation
    assert "api_key" not in representation or "***" in representation


def test_config_from_dict():
    data = {
        "llm": {"api_key": "test"},
        "mcp_servers": {
            "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
            "windows": {"command": "windows-mcp", "args": ["serve"]},
            "filesystem": {"command": "npx", "args": ["-y", "filesystem-mcp"]},
        },
    }
    cfg = Config.model_validate(data)
    assert cfg.llm.api_key == "test"
    assert cfg.mcp_servers.playwright.command == "npx"


def test_load_config_from_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    data = {
        "llm": {"api_key": "yaml-test"},
        "mcp_servers": {
            "playwright": {"command": "npx"},
            "windows": {"command": "windows-mcp"},
            "filesystem": {"command": "npx"},
        },
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_config(config_path)
    assert cfg.llm.api_key == "yaml-test"
