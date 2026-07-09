"""Tests for kill switch integration."""

from pathlib import Path

import pytest

from agent.config import Config, LLMConfig, MCPConfig, MCPServerConfig
from agent.kill_switch import KillSwitch
from agent.llm_client import LLMClient
from agent.orchestrator import AgentOrchestrator
from eventbus import EventBus
from eventbus.events import KillSwitchTriggered
from mcp_client import MCPMultiplexer


@pytest.fixture
def orchestrator(tmp_path: Path):
    config = Config(
        llm=LLMConfig(api_key="test"),
        mcp_servers=MCPConfig(
            playwright=MCPServerConfig(command="npx"),
            windows=MCPServerConfig(command="windows-mcp"),
            filesystem=MCPServerConfig(command="npx"),
        ),
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={"skills_dir": str(tmp_path / "skills"), "cache_dir": str(tmp_path / "cache")},
    )
    eventbus = EventBus()
    llm = LLMClient(config.llm)
    mcp = MCPMultiplexer(config.mcp_servers)
    kill = KillSwitch(eventbus)
    return AgentOrchestrator(config, eventbus, llm, mcp, kill)


def test_kill_switch_event_handler_sets_cancel(orchestrator):
    orchestrator.eventbus.subscribe("KillSwitchTriggered", orchestrator._on_kill_switch)
    # Handler is async and scheduled via run_coroutine_threadsafe by KillSwitch,
    # but _on_kill_switch itself can be awaited directly for testing.
    import asyncio

    asyncio.run(orchestrator._on_kill_switch(KillSwitchTriggered(reason="test")))
    assert orchestrator._cancel_event.is_set()


@pytest.mark.asyncio
async def test_kill_switch_cancels_task(orchestrator, monkeypatch):
    monkeypatch.setattr(orchestrator, "_check_cancelled", lambda: True)
    result = await orchestrator.run_task("do something")
    assert "cancelled" in result.lower()
    assert orchestrator.state.current_state == "IDLE"
