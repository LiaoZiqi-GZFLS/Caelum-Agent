"""Shared pytest fixtures and markers for Caelum-Agent tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import Config
from eventbus import EventBus
from tests.fakes import FakeKillSwitch


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "smoke: real API/MCP integration tests (needs config.yaml)",
    )
    config.addinivalue_line(
        "markers",
        "slow: tests that take >5 seconds",
    )


@pytest.fixture
def eventbus():
    """A fresh EventBus instance."""
    return EventBus()


@pytest.fixture
def tiny_config(tmp_path: Path) -> Config:
    """Minimal Config for unit tests that don't need full orchestrator setup."""
    return Config(
        llm={
            "provider": "kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key": "test",
            "model": "kimi-k2.6",
        },
        mcp_servers={},
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={
            "skills_dir": str(tmp_path / "skills"),
            "cache_dir": str(tmp_path / "cache"),
            "audit_log": str(tmp_path / "audit.log"),
        },
        security={
            "default_level": "read",
            "auto_execute_levels": ["read", "write_safe"],
            "confirm_levels": ["write_risky"],
            "destructive_requires_approval": True,
        },
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Full Config with all MCP stubs — used by orchestrator tests."""
    return Config(
        llm={
            "provider": "kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key": "test",
            "model": "kimi-k2.6",
            "enable_builtin_tools": False,
            "builtin_tools": [],
        },
        mcp_servers={
            "playwright": {"command": "npx", "args": [], "env": {}},
            "windows": {"command": "windows-mcp", "args": ["serve"], "env": {}},
            "filesystem": {"command": "npx", "args": [], "env": {}},
        },
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={
            "skills_dir": str(tmp_path / "skills"),
            "cache_dir": str(tmp_path / "cache"),
            "audit_log": str(tmp_path / "audit.log"),
        },
        security={
            "default_level": "read",
            "auto_execute_levels": ["read", "write_safe"],
            "confirm_levels": ["write_risky"],
            "destructive_requires_approval": True,
        },
    )


@pytest.fixture
def killswitch(eventbus):
    """A FakeKillSwitch wired to the shared event bus."""
    return FakeKillSwitch(eventbus)


@pytest.fixture
def memory_store(tmp_path: Path):
    """A MemoryStore backed by tmp_path — shared across test files."""
    from agent.memory import MemoryStore

    return MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )
