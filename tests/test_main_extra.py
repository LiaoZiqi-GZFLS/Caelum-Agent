"""Extra coverage for main.py: _print_help, _log_state, _print_status, _run_repl,
config-not-found and one-shot exception path (lines not hit by test_main.py)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

import main


# ---------------------------------------------------------------------------
# _print_help / _log_state / _print_status
# ---------------------------------------------------------------------------

def test_print_help_lists_commands(capsys):
    main._print_help()
    out = capsys.readouterr().out
    for cmd in ("/help", "/status", "/stop", "/quit"):
        assert cmd in out


def test_log_state_logs_transition(caplog):
    event = main.AgentStateChanged(old_state="IDLE", new_state="PLANNING")
    with caplog.at_level(logging.INFO):
        main._log_state(event, logging.getLogger("test.state"))
    assert "IDLE -> PLANNING" in caplog.text


def test_log_state_ignores_other_events(caplog):
    main._log_state(SimpleNamespace(), logging.getLogger("test.state"))  # no raise


@pytest.mark.asyncio
async def test_print_status_reports_health(capsys):
    agent = SimpleNamespace(
        state=SimpleNamespace(current_state="EXECUTING"),
        task_id="abc-123",
        last_action_summary="clicked OK",
        consecutive_action_failures=2,
        consecutive_api_failures=0,
        mcp=SimpleNamespace(clients={
            "playwright": SimpleNamespace(_connected=True),
            "windows": SimpleNamespace(_connected=False),
        }),
    )
    await main._print_status(agent)
    out = capsys.readouterr().out
    assert "EXECUTING" in out
    assert "abc-123" in out
    assert "playwright: connected" in out
    assert "windows: disconnected" in out
    assert "2" in out  # consecutive action failures


# ---------------------------------------------------------------------------
# main() error paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_config_not_found_returns_1(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda path: (_ for _ in ()).throw(FileNotFoundError()))
    assert await main.main(["--config", "missing.yaml"]) == 1


@pytest.mark.asyncio
async def test_main_one_shot_exception_returns_1(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    agent = _ReplAgent()

    async def boom(task):
        raise RuntimeError("explode")

    agent.run_task = boom  # type: ignore[assignment]
    _wire(monkeypatch, cfg, agent)

    assert await main.main(["--task", "x"]) == 1
    assert agent.shutdown_called is True


# ---------------------------------------------------------------------------
# _run_repl
# ---------------------------------------------------------------------------

class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def subscribe(self, *a: Any) -> None:
        pass

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _ReplAgent:
    def __init__(self) -> None:
        self.state = SimpleNamespace(current_state="IDLE")
        self.task_id = None
        self.last_action_summary = ""
        self.consecutive_action_failures = 0
        self.consecutive_api_failures = 0
        self.eventbus = _FakeBus()
        self.mcp = SimpleNamespace(
            clients={
                "playwright": SimpleNamespace(_connected=True),
                "windows": SimpleNamespace(_connected=False),
                "filesystem": SimpleNamespace(_connected=True),
            },
            all_tools=lambda: [],
        )
        self.initialized = False
        self.shutdown_called = False
        self.ran: list[str] = []

    def set_human_confirmation_callback(self, cb) -> None:
        self.cb = cb

    def set_human_question_callback(self, cb) -> None:
        self.help_cb = cb

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.shutdown_called = True

    async def run_task(self, task: str) -> str:
        self.ran.append(task)
        return f"ran:{task}"


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        ui_detector=SimpleNamespace(enabled=True),
        ocr=SimpleNamespace(enabled=True),
        logging=SimpleNamespace(level="INFO", data_dir=str(tmp_path)),
        llm=SimpleNamespace(),
        mcp_servers=SimpleNamespace(),
    )


def _wire(monkeypatch, cfg, agent: _ReplAgent) -> None:
    monkeypatch.setattr(main, "load_config", lambda path: cfg)
    monkeypatch.setattr(main, "setup_logging", lambda **kw: logging.getLogger("test.repl"))
    monkeypatch.setattr(main, "EventBus", lambda: SimpleNamespace(subscribe=lambda *a: None, unsubscribe=lambda *a: None))
    monkeypatch.setattr(main, "LLMClient", lambda llm: SimpleNamespace())
    monkeypatch.setattr(main, "MCPMultiplexer", lambda c: agent.mcp)
    monkeypatch.setattr(main, "KillSwitch", lambda eb: SimpleNamespace())
    monkeypatch.setattr(main, "AgentOrchestrator", lambda *a, **kw: agent)


def _feed(monkeypatch, lines: list[str]) -> None:
    it = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)


@pytest.mark.asyncio
async def test_repl_quit_immediately(monkeypatch, tmp_path):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    _feed(monkeypatch, ["/quit"])

    rc = await main.main([])

    assert rc == 0
    assert agent.initialized is True
    assert agent.shutdown_called is True
    assert agent.ran == []


@pytest.mark.asyncio
async def test_repl_help_status_stop_task_then_quit(monkeypatch, tmp_path, capsys):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    _feed(monkeypatch, ["/help", "/status", "/stop", "do thing", "", "/quit"])

    rc = await main.main([])

    assert rc == 0
    assert agent.ran == ["do thing"]  # /help, /status, /stop and empty are not tasks
    # /stop emitted a KillSwitchTriggered
    assert any(type(e).__name__ == "KillSwitchTriggered" for e in agent.eventbus.events)
    out = capsys.readouterr().out
    assert "ran:do thing" in out
    assert "Available commands" in out  # /help printed


@pytest.mark.asyncio
async def test_repl_eof_breaks_loop(monkeypatch, tmp_path):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    _feed(monkeypatch, [])  # immediate EOF

    rc = await main.main([])
    assert rc == 0
    assert agent.shutdown_called is True


@pytest.mark.asyncio
async def test_repl_prints_banner(monkeypatch, tmp_path, capsys):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    _feed(monkeypatch, ["/quit"])

    await main.main([])

    out = capsys.readouterr().out
    assert "Caelum-Agent" in out  # banner


@pytest.mark.asyncio
async def test_repl_shows_mcp_status(monkeypatch, tmp_path, capsys):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    _feed(monkeypatch, ["/quit"])

    await main.main([])

    out = capsys.readouterr().out
    assert "MCP" in out
    assert "playwright" in out
    assert "windows" in out


@pytest.mark.asyncio
async def test_one_shot_shows_mcp_status(monkeypatch, tmp_path, capsys):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)

    rc = await main.main(["--task", "hi"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "MCP" in out
    assert "playwright" in out
    assert "windows" in out


@pytest.mark.asyncio
async def test_presenter_active_suppresses_console_log_handler(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    def spy_setup_logging(**kw):
        captured.update(kw)
        return logging.getLogger("test.repl")

    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    # Re-apply our spy after _wire (which also sets setup_logging).
    monkeypatch.setattr(main, "setup_logging", spy_setup_logging)
    _feed(monkeypatch, ["/quit"])

    await main.main([])

    assert captured.get("console") is False


# ---------------------------------------------------------------------------
# spinner stop on unexpected task failure
# ---------------------------------------------------------------------------

class _SpyPresenter:
    """Wraps the real CLIPresenter and records stop() calls."""

    def __init__(self, real):
        self.real = real
        self.stop_calls = 0

    def attach(self, bus):
        self.real.attach(bus)

    def detach(self):
        self.real.detach()

    def banner(self):
        self.real.banner()

    def mcp_status(self, servers):
        self.real.mcp_status(servers)

    def input(self):
        return self.real.input()

    def print_answer(self, text):
        self.real.print_answer(text)

    def stop(self):
        self.stop_calls += 1
        self.real.stop()


def _install_spy_presenter(monkeypatch) -> dict:
    """Make main() build a recording presenter; returns a holder dict."""
    from agent.cli_presenter import CLIPresenter as _RealPresenter

    holder: dict[str, Any] = {}
    monkeypatch.setattr(
        main,
        "CLIPresenter",
        lambda: holder.setdefault("spy", _SpyPresenter(_RealPresenter())),
    )
    return holder


@pytest.mark.asyncio
async def test_repl_stops_spinner_when_task_raises(monkeypatch, tmp_path):
    holder = _install_spy_presenter(monkeypatch)
    agent = _ReplAgent()

    async def boom(task):
        raise RuntimeError("perception exploded")

    agent.run_task = boom  # type: ignore[assignment]
    _wire(monkeypatch, _cfg(tmp_path), agent)
    _feed(monkeypatch, ["boom", "/quit"])

    rc = await main.main([])

    assert rc == 0
    assert holder["spy"].stop_calls >= 1


@pytest.mark.asyncio
async def test_one_shot_stops_spinner_when_task_raises(monkeypatch, tmp_path):
    holder = _install_spy_presenter(monkeypatch)
    agent = _ReplAgent()

    async def boom(task):
        raise RuntimeError("perception exploded")

    agent.run_task = boom  # type: ignore[assignment]
    _wire(monkeypatch, _cfg(tmp_path), agent)

    rc = await main.main(["--task", "boom"])

    assert rc == 1
    assert holder["spy"].stop_calls >= 1


@pytest.mark.asyncio
async def test_presenter_cleared_when_agent_construction_fails(monkeypatch, tmp_path):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)

    def boom(llm):
        raise RuntimeError("llm construct failed")

    monkeypatch.setattr(main, "LLMClient", boom)

    with pytest.raises(RuntimeError):
        await main.main([])

    assert main._presenter is None


# ---------------------------------------------------------------------------
# ask_human_interactive
# ---------------------------------------------------------------------------

def test_ask_human_interactive_non_tty_returns_none(monkeypatch):
    monkeypatch.setattr(main, "_presenter", None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main.ask_human_interactive("q", ["a", "b"]) is None


def test_ask_human_interactive_delegates_to_presenter(monkeypatch):
    seen = {}

    def fake_ask_choice(q, o):
        seen["call"] = (q, o)
        return "是"

    monkeypatch.setattr(main, "_presenter", SimpleNamespace(ask_choice=fake_ask_choice))
    assert main.ask_human_interactive("q", ["a", "b"]) == "是"
    assert seen["call"] == ("q", ["a", "b"])


def test_ask_human_interactive_legacy_number_choice(monkeypatch, capsys):
    monkeypatch.setattr(main, "_presenter", None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    assert main.ask_human_interactive("q", ["a", "b"]) == "b"


def test_ask_human_interactive_legacy_free_text(monkeypatch, capsys):
    monkeypatch.setattr(main, "_presenter", None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "帮我点跳过")
    assert main.ask_human_interactive("q", ["a", "b"]) == "帮我点跳过"
