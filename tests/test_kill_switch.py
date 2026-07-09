"""Tests for kill switch integration."""

from pathlib import Path
from typing import Any

import asyncio
import signal

import pytest
from pynput import keyboard
from pynput.keyboard import Key, KeyCode

from agent.config import Config, LLMConfig, MCPConfig, MCPServerConfig
from agent.kill_switch import KillSwitch
from agent.llm_client import LLMClient
from agent.orchestrator import AgentOrchestrator
from eventbus import EventBus
from eventbus.events import KillSwitchTriggered
from mcp_client import MCPMultiplexer


class _FakeListener:
    """Stub pynput listener that captures callbacks without starting OS input."""

    def __init__(self, on_press, on_release):
        self.on_press = on_press
        self.on_release = on_release

    def start(self):
        pass

    def stop(self):
        pass


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


def test_is_triggered_and_reset():
    eventbus = EventBus()
    kill = KillSwitch(eventbus)
    assert not kill.is_triggered()
    kill._triggered.set()
    assert kill.is_triggered()
    kill.reset()
    assert not kill.is_triggered()


@pytest.mark.asyncio
async def test_sigint_handler_emits_kill_switch_event(monkeypatch):
    eventbus = EventBus()
    kill = KillSwitch(eventbus)
    received: list[KillSwitchTriggered] = []

    async def handler(event: KillSwitchTriggered) -> None:
        received.append(event)

    eventbus.subscribe("KillSwitchTriggered", handler)

    captured_coro: Any = None

    def fake_run_coroutine_threadsafe(coro: Any, loop: Any) -> None:
        nonlocal captured_coro
        captured_coro = coro

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    class MockLoop:
        def is_running(self) -> bool:
            return True

    kill._loop = MockLoop()
    kill._on_sigint(2, None)

    assert kill.is_triggered()
    assert captured_coro is not None
    await captured_coro
    assert len(received) == 1
    assert received[0].reason == "sigint"


def test_sigint_real_loop_emits_kill_switch_event(monkeypatch):
    """Start a real asyncio loop, call _on_sigint, and verify the event."""
    eventbus = EventBus()
    kill = KillSwitch(eventbus)
    received: list[KillSwitchTriggered] = []

    async def handler(event: KillSwitchTriggered) -> None:
        received.append(event)

    eventbus.subscribe("KillSwitchTriggered", handler)
    monkeypatch.setattr(keyboard, "Listener", _FakeListener)

    async def run():
        kill.start()
        kill._on_sigint(signal.SIGINT, None)
        # Allow run_coroutine_threadsafe to schedule and emit.
        await asyncio.sleep(0.05)
        kill.stop()

    asyncio.run(run())
    assert len(received) == 1
    assert received[0].reason == "sigint"


def test_pynput_ctrl_c_emits_kill_switch_event(monkeypatch):
    """Patch keyboard.Listener, invoke ctrl_l + 'c', and verify the event."""
    eventbus = EventBus()
    kill = KillSwitch(eventbus)
    received: list[KillSwitchTriggered] = []

    async def handler(event: KillSwitchTriggered) -> None:
        received.append(event)

    eventbus.subscribe("KillSwitchTriggered", handler)

    captured_on_press = None
    captured_on_release = None

    class CaptureListener(_FakeListener):
        def __init__(self, on_press, on_release):
            nonlocal captured_on_press, captured_on_release
            super().__init__(on_press, on_release)
            captured_on_press = on_press
            captured_on_release = on_release

    monkeypatch.setattr(keyboard, "Listener", CaptureListener)

    async def run():
        kill.start()
        captured_on_press(Key.ctrl_l)
        captured_on_press(KeyCode(char="c"))
        await asyncio.sleep(0.05)
        kill.stop()

    asyncio.run(run())
    assert len(received) == 1
    assert received[0].reason == "ctrl+c"


def test_start_installs_sigint_handler_and_stop_restores(monkeypatch):
    """Verify start() installs the SIGINT handler and stop() restores it."""
    eventbus = EventBus()
    kill = KillSwitch(eventbus)
    original_handler = signal.getsignal(signal.SIGINT)
    installed_handler = None

    def fake_signal(sig, handler):
        nonlocal installed_handler
        if sig == signal.SIGINT:
            installed_handler = handler
        return original_handler

    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(keyboard, "Listener", _FakeListener)

    async def run():
        kill.start()
        # Bound methods compare by underlying function and instance, not identity.
        assert installed_handler == kill._on_sigint
        assert kill._original_sigint is original_handler
        kill.stop()
        assert kill._original_sigint is None

    asyncio.run(run())


def test_trigger_debounce_ignores_duplicate_within_100ms(monkeypatch):
    """Two rapid triggers should emit only one event."""
    eventbus = EventBus()
    kill = KillSwitch(eventbus)
    received: list[KillSwitchTriggered] = []

    async def handler(event: KillSwitchTriggered) -> None:
        received.append(event)

    eventbus.subscribe("KillSwitchTriggered", handler)
    monkeypatch.setattr(keyboard, "Listener", _FakeListener)

    async def run():
        kill.start()
        kill._trigger("sigint")
        kill._trigger("sigint")
        await asyncio.sleep(0.05)
        kill.stop()

    asyncio.run(run())
    assert len(received) == 1


@pytest.mark.asyncio
async def test_kill_switch_cancels_task(orchestrator, monkeypatch):
    monkeypatch.setattr(orchestrator, "_check_cancelled", lambda: True)
    result = await orchestrator.run_task("do something")
    assert "cancelled" in result.lower()
    assert orchestrator.state.current_state == "IDLE"
