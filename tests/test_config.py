"""Tests for Pydantic configuration."""

from pathlib import Path

import pytest
import yaml

from agent.config import Config, LLMConfig, load_config


def test_llm_model_name_default_is_k3():
    cfg = LLMConfig(api_key="test")
    assert cfg.model == "kimi-k3"


def test_llm_config_repr_hides_api_key():
    cfg = LLMConfig(api_key="sk-secret-key", model="kimi-k3")
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


# ---------------------------------------------------------------------------
# IconCaptionConfig (Florence-2 icon captioning of YOLO detections)
# ---------------------------------------------------------------------------

def test_icon_caption_config_defaults():
    from agent.config import IconCaptionConfig

    cfg = IconCaptionConfig()
    assert cfg.enabled is True
    assert cfg.model_path == "./models/omniparser/icon_caption"
    assert cfg.processor_path == "./models/omniparser/icon_caption_processor"
    assert cfg.device == "cuda:0"
    assert cfg.max_icons == 30
    assert cfg.batch_size == 8
    assert cfg.max_new_tokens == 20


def test_config_exposes_icon_caption_section():
    from agent.config import Config, IconCaptionConfig

    cfg = Config.model_validate({"llm": {"api_key": "test"}})
    assert isinstance(cfg.icon_caption, IconCaptionConfig)
    assert cfg.icon_caption.enabled is True


def test_icon_caption_config_from_yaml(tmp_path: Path):
    data = {
        "llm": {"api_key": "test"},
        "icon_caption": {"enabled": False, "max_icons": 12, "device": "cpu"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    cfg = load_config(config_path)

    assert cfg.icon_caption.enabled is False
    assert cfg.icon_caption.max_icons == 12
    assert cfg.icon_caption.device == "cpu"
