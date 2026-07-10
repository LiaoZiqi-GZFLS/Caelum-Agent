# CLI UI Facelift Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a rich, event-driven terminal presenter so the Caelum-Agent CLI shows a banner, a styled prompt, a live spinner, per-tool call/result lines, and a markdown final answer — without touching the ReAct loop.

**Architecture:** A new `agent/cli_presenter.py` (`CLIPresenter`) subscribes to the six EventBus events the orchestrator already emits (`UserInputReceived`, `AgentStateChanged`, `LLMResponseReceived`, `ToolCallRequested`, `ToolCallCompleted`, `KillSwitchTriggered`) and renders via a single `rich.Console`. `main.py` wires it in (banner, input, answer, confirm, `setup_logging(console=False)`). The orchestrator is unchanged.

**Tech Stack:** `rich` (Console, Status, Panel, Markdown, prompt.Confirm); existing `eventbus.EventBus`; pytest + `pytest-asyncio` for tests. Handlers are declared `async def` so EventBus runs them on the loop thread (keeps `rich.status.Status` thread-safe).

**Spec:** `docs/designs/cli_ui_facelift_design.md`.

**Note on location:** this plan lives in `docs/designs/` (tracked) rather than the skill default `docs/superpowers/plans/`, because the repo's `.gitignore` (line 240) treats `/docs/superpowers/` as local scratch.

---

## File Structure

- **Create `agent/cli_presenter.py`** — `CLIPresenter` + truncation/theme helpers. One responsibility: render agent events to a `rich.Console`.
- **Modify `agent/logging_config.py`** — add `console: bool = True` keyword; when `False`, skip the console `StreamHandler`.
- **Modify `main.py`** — build/attach presenter; banner; `presenter.input`; `presenter.print_answer`; delegate `confirm_interactive` to presenter when present; pass `console=False` to logging.
- **Modify `requirements.txt`** — add `rich`.
- **Create `tests/test_cli_presenter.py`** — presenter rendering + confirm tests.
- **Create `tests/test_logging_config.py`** — `console` param tests.
- **Modify `tests/test_main_extra.py`** — banner + no-console-handler assertions.

---

## Task 1: Add the `rich` dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add rich to requirements**

Append to `requirements.txt` (keep alphabetical-ish grouping with the other runtime deps):

```
rich>=13.7,<15
```

- [ ] **Step 2: Install into the project venv and verify import**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install "rich>=13.7,<15"
.\.venv\Scripts\python.exe -c "import rich; print(rich.__version__)"
```

Expected: pip installs rich (and its deps `markdown-it-py`, `pygments`); the second command prints a version like `13.9.4` or `14.x.x` with no traceback.

- [ ] **Step 3: Commit**

```powershell
git add requirements.txt
git commit -m "chore: add rich dependency for CLI UI facelift"
```

---

## Task 2: `setup_logging(console=...)` parameter

**Files:**
- Modify: `agent/logging_config.py`
- Test: `tests/test_logging_config.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_logging_config.py`:

```python
"""Tests for setup_logging console handler toggle."""

from __future__ import annotations

import logging

from agent.logging_config import setup_logging


def _reset_root() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.WARNING)


def test_console_true_attaches_stream_handler(tmp_path):
    _reset_root()
    setup_logging(level="INFO", log_dir=tmp_path, console=True)
    stream_handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]
    assert len(stream_handlers) == 1
    _reset_root()


def test_console_false_skips_stream_handler(tmp_path):
    _reset_root()
    setup_logging(level="INFO", log_dir=tmp_path, console=False)
    stream_handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]
    assert stream_handlers == []
    # File handler still present.
    assert any(isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers)
    _reset_root()


def test_default_console_is_true(tmp_path):
    _reset_root()
    setup_logging(level="INFO", log_dir=tmp_path)
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logging.getLogger().handlers
    )
    _reset_root()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_logging_config.py -q --no-cov
```

Expected: FAIL — `setup_logging() got an unexpected keyword argument 'console'`.

- [ ] **Step 3: Implement the `console` parameter**

In `agent/logging_config.py`, change the signature and gate the console handler:

```python
def setup_logging(
    level: str = "INFO",
    log_dir: Path | str = "./data/logs",
    fmt: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    console: bool = True,
) -> logging.Logger:
    """Configure root logger with optional console + rotating file handlers."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers if setup_logging is called multiple times.
    if root.handlers:
        return logging.getLogger("caelum")

    formatter = _ExtraFormatter(fmt)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

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
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_logging_config.py -q --no-cov
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```powershell
git add agent/logging_config.py tests/test_logging_config.py
git commit -m "feat(logging): add console toggle to setup_logging"
```

---

## Task 3: `CLIPresenter` core — construction, attach/detach, event rendering, answer panel

**Files:**
- Create: `agent/cli_presenter.py`
- Test: `tests/test_cli_presenter.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_presenter.py`:

```python
"""Tests for the rich, event-driven CLI presenter."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from agent.cli_presenter import CLIPresenter
from eventbus import EventBus
from eventbus.events import (
    LLMResponseReceived,
    ToolCallCompleted,
    ToolCallRequested,
)


def _make_presenter() -> tuple[CLIPresenter, io.StringIO]:
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=120,
    )
    return CLIPresenter(console=console), buf


@pytest.mark.asyncio
async def test_tool_requested_renders_arrow_and_name():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(ToolCallRequested(
        server="windows", tool_name="Click", arguments={"label": 5}
    ))

    out = buf.getvalue()
    assert "windows__Click" in out
    assert "▶" in out
    presenter.detach()


@pytest.mark.asyncio
async def test_tool_completed_success_renders_check():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(ToolCallCompleted(
        server="windows", tool_name="Click", result="OK: clicked", success=True
    ))

    out = buf.getvalue()
    assert "✓" in out
    assert "Click" in out
    assert "OK: clicked" in out
    presenter.detach()


@pytest.mark.asyncio
async def test_tool_completed_failure_renders_cross():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(ToolCallCompleted(
        server="windows", tool_name="Type", result="[error] no focus", success=False
    ))

    out = buf.getvalue()
    assert "✗" in out
    assert "Type" in out
    presenter.detach()


@pytest.mark.asyncio
async def test_long_result_is_truncated_to_first_line():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    long = "line1\nline2\nline3"
    await bus.emit(ToolCallCompleted(
        server="fs", tool_name="read_file", result=long, success=True
    ))

    out = buf.getvalue()
    assert "line1" in out
    assert "line2" not in out  # only the first line is shown
    presenter.detach()


@pytest.mark.asyncio
async def test_llm_narration_printed_dim():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(LLMResponseReceived(content="I will click the button.", tool_calls=[]))

    assert "I will click the button." in buf.getvalue()
    presenter.detach()


def test_print_answer_contains_text():
    presenter, buf = _make_presenter()
    presenter.print_answer("The answer is **42**.")
    out = buf.getvalue()
    # Markdown keeps the literal text (bold markers become ANSI, not the word '**').
    assert "The answer is" in out
    assert "42" in out


def test_detach_stops_rendering():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)
    presenter.detach()
    # After detach, the bus no longer holds our handlers.
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        bus.emit(ToolCallRequested(server="x", tool_name="y", arguments={}))
    )
    assert buf.getvalue() == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_presenter.py -q --no-cov
```

Expected: FAIL — `ModuleNotFoundError: No module named 'agent.cli_presenter'`.

- [ ] **Step 3: Implement `agent/cli_presenter.py`**

Create `agent/cli_presenter.py`:

```python
"""Rich, event-driven terminal presenter for the Caelum-Agent CLI.

The presenter subscribes to the EventBus events the orchestrator already emits
and renders a live spinner, per-tool call/result lines, model narration, and a
markdown final answer. It is a pure consumer: a rendering error is logged and
swallowed so it can never abort a task.

Handlers are declared ``async def`` on purpose: EventBus runs coroutine handlers
on the event-loop thread, which keeps ``rich.status.Status`` (not thread-safe)
on a single thread. Sync handlers would be dispatched via ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.status import Status

from eventbus import EventBus
from eventbus.events import (
    AgentStateChanged,
    KillSwitchTriggered,
    LLMResponseReceived,
    ToolCallCompleted,
    ToolCallRequested,
    UserInputReceived,
)

logger = logging.getLogger("caelum.cli")

# Fixed theme (no user-configurable theme in this iteration).
STYLE_ARROW = "bold cyan"
STYLE_OK = "green"
STYLE_ERR = "red"
STYLE_NARRATION = "dim italic"
STYLE_PANEL_BORDER = "cyan"

MAX_ARG_CHARS = 60
MAX_RESULT_CHARS = 120

# FSM states whose spinner label we surface.
_STATE_LABELS = {
    "PLANNING": "Thinking…",
    "EXECUTING": "Thinking…",
    "VERIFYING": "Verifying…",
    "REFLECT": "Reflecting…",
    "WAITING_HUMAN": "Waiting for input…",
}
_TERMINAL_STATES = {"COMPLETED", "ERROR", "STUCK", "IDLE"}


def _first_line(text: Any, n: int = MAX_RESULT_CHARS) -> str:
    s = "" if text is None else str(text)
    line = s.splitlines()[0] if s.splitlines() else s
    return line if len(line) <= n else line[: n - 1] + "…"


def _short_args(args: dict[str, Any] | None, n: int = MAX_ARG_CHARS) -> str:
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        sv = str(v)
        if len(sv) > 24:
            sv = sv[:23] + "…"
        parts.append(f"{k}={sv}")
    s = ", ".join(parts)
    return s if len(s) <= n else s[: n - 1] + "…"


class CLIPresenter:
    def __init__(self, console: Console | None = None, *, enabled: bool = True) -> None:
        self.console = console or Console()
        self.enabled = enabled
        self._bus: EventBus | None = None
        self._status: Status | None = None

    # -- wiring ---------------------------------------------------------------

    def attach(self, bus: EventBus) -> None:
        self._bus = bus
        bus.subscribe("UserInputReceived", self._on_user_input)
        bus.subscribe("AgentStateChanged", self._on_state)
        bus.subscribe("LLMResponseReceived", self._on_llm)
        bus.subscribe("ToolCallRequested", self._on_tool_requested)
        bus.subscribe("ToolCallCompleted", self._on_tool_completed)
        bus.subscribe("KillSwitchTriggered", self._on_kill)

    def detach(self) -> None:
        if self._bus is None:
            return
        for name, handler in (
            ("UserInputReceived", self._on_user_input),
            ("AgentStateChanged", self._on_state),
            ("LLMResponseReceived", self._on_llm),
            ("ToolCallRequested", self._on_tool_requested),
            ("ToolCallCompleted", self._on_tool_completed),
            ("KillSwitchTriggered", self._on_kill),
        ):
            self._bus.unsubscribe(name, handler)
        self._bus = None
        self._stop_status()

    # -- direct calls from main.py -------------------------------------------

    def banner(self) -> None:
        self.console.print(
            "[bold cyan]Caelum-Agent[/] [dim]· Kimi K2.6 · type /help for commands[/]"
        )

    def input(self, prompt: str = "[bold cyan]›[/] ") -> str:
        # Console.input renders the prompt through the console then reads stdin;
        # it raises EOFError on EOF, matching builtin input()'s contract.
        return self.console.input(prompt)

    def print_answer(self, text: str) -> None:
        self._stop_status()
        self.console.print(Panel(Markdown(text or ""), title="Caelum", border_style=STYLE_PANEL_BORDER))

    def confirm(self, summary: str, action: dict[str, Any]) -> bool:
        self.console.print(f"\n[bold yellow]Confirm:[/] {summary}")
        if not sys.stdin.isatty():
            self.console.print(
                "[dim]Non-interactive: action requires approval but stdin is not a TTY; "
                "denying. Re-run with --yes / --yes-destructive.[/]"
            )
            return False
        try:
            return bool(Confirm.ask("[yellow]Approve?[/]", console=self.console, default=False))
        except EOFError:
            self.console.print("[dim]EOF on stdin; denying.[/]")
            return False

    # -- status helpers -------------------------------------------------------

    def _start_status(self, label: str) -> None:
        if not self.enabled or self._status is not None:
            if self._status is not None:
                self._status.update(label)
            return
        self._status = Status(label, console=self.console, spinner="dots")
        self._status.start()

    def _update_status(self, label: str) -> None:
        if self._status is not None:
            self._status.update(label)

    def _stop_status(self) -> None:
        if self._status is not None:
            try:
                self._status.stop()
            except Exception:
                pass
            self._status = None

    # -- event handlers (async: run on the loop thread) -----------------------

    async def _on_user_input(self, event: Any) -> None:
        try:
            if isinstance(event, UserInputReceived):
                self._start_status("Thinking…")
        except Exception as exc:
            logger.warning("presenter _on_user_input failed: %s", exc)

    async def _on_state(self, event: Any) -> None:
        try:
            if not isinstance(event, AgentStateChanged):
                return
            if event.new_state in _TERMINAL_STATES:
                self._stop_status()
                return
            label = _STATE_LABELS.get(event.new_state)
            if label:
                self._update_status(label)
        except Exception as exc:
            logger.warning("presenter _on_state failed: %s", exc)

    async def _on_llm(self, event: Any) -> None:
        try:
            if not isinstance(event, LLMResponseReceived):
                return
            if event.content:
                self.console.print(f"[{STYLE_NARRATION}]{event.content}[/]")
            self._update_status("Thinking…")
        except Exception as exc:
            logger.warning("presenter _on_llm failed: %s", exc)

    async def _on_tool_requested(self, event: Any) -> None:
        try:
            if not isinstance(event, ToolCallRequested):
                return
            name = f"{event.server}__{event.tool_name}"
            args = _short_args(event.arguments)
            suffix = f"({args})" if args else ""
            self.console.print(f"  [{STYLE_ARROW}]▶[/] {name}{suffix}")
            self._update_status(f"Running {event.tool_name}…")
        except Exception as exc:
            logger.warning("presenter _on_tool_requested failed: %s", exc)

    async def _on_tool_completed(self, event: Any) -> None:
        try:
            if not isinstance(event, ToolCallCompleted):
                return
            mark = "✓" if event.success else "✗"
            style = STYLE_OK if event.success else STYLE_ERR
            line = _first_line(event.result)
            self.console.print(f"  [{style}]{mark}[/] {event.tool_name} — {line}")
            self._update_status("Thinking…")
        except Exception as exc:
            logger.warning("presenter _on_tool_completed failed: %s", exc)

    async def _on_kill(self, event: Any) -> None:
        try:
            if isinstance(event, KillSwitchTriggered):
                self._stop_status()
                self.console.print("[yellow]Cancelled.[/]")
        except Exception as exc:
            logger.warning("presenter _on_kill failed: %s", exc)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_presenter.py -q --no-cov
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```powershell
git add agent/cli_presenter.py tests/test_cli_presenter.py
git commit -m "feat(cli): add rich event-driven CLIPresenter"
```

---

## Task 4: `CLIPresenter.confirm` tests (approve / deny / non-TTY)

**Files:**
- Modify: `tests/test_cli_presenter.py`

- [ ] **Step 1: Append the failing confirm tests**

Append to `tests/test_cli_presenter.py`:

```python
def test_confirm_approve_returns_true(monkeypatch):
    presenter, _ = _make_presenter()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("agent.cli_presenter.Confirm.ask", lambda *a, **kw: True)
    assert presenter.confirm("delete file", {"action": "delete"}) is True


def test_confirm_deny_returns_false(monkeypatch):
    presenter, _ = _make_presenter()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("agent.cli_presenter.Confirm.ask", lambda *a, **kw: False)
    assert presenter.confirm("delete file", {"action": "delete"}) is False


def test_confirm_non_tty_returns_false_without_prompt(monkeypatch):
    presenter, _ = _make_presenter()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("Confirm.ask must not be called on a non-TTY")

    monkeypatch.setattr("agent.cli_presenter.Confirm.ask", boom)
    assert presenter.confirm("delete file", {"action": "delete"}) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_presenter.py -q --no-cov -k "confirm"
```

Expected: these already PASS against the Task 3 implementation (confirm was implemented there). If any fail, fix `confirm` in `agent/cli_presenter.py` before committing. This task exists to lock the confirm contract with explicit tests.

- [ ] **Step 3: Run the full presenter file**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_presenter.py -q --no-cov
```

Expected: 10 passed.

- [ ] **Step 4: Commit**

```powershell
git add tests/test_cli_presenter.py
git commit -m "test(cli): cover CLIPresenter.confirm approve/deny/non-tty"
```

---

## Task 5: Wire the presenter into `main.py`

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main_extra.py`

- [ ] **Step 1: Append failing tests for banner + no-console-handler**

Append to `tests/test_main_extra.py`:

```python
@pytest.mark.asyncio
async def test_repl_prints_banner(monkeypatch, tmp_path, capsys):
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    _feed(monkeypatch, ["/quit"])

    await main.main([])

    out = capsys.readouterr().out
    assert "Caelum-Agent" in out  # banner


@pytest.mark.asyncio
async def test_presenter_active_suppresses_console_log_handler(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    def spy_setup_logging(**kw):
        captured.update(kw)
        return logging.getLogger("test.repl")

    monkeypatch.setattr(main, "setup_logging", spy_setup_logging)
    agent = _ReplAgent()
    _wire(monkeypatch, _cfg(tmp_path), agent)
    # Re-apply our spy after _wire (which also sets setup_logging).
    monkeypatch.setattr(main, "setup_logging", spy_setup_logging)
    _feed(monkeypatch, ["/quit"])

    await main.main([])

    assert captured.get("console") is False
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_extra.py -q --no-cov -k "banner or console_log_handler"
```

Expected: FAIL — banner not printed / `console` kwarg not passed.

- [ ] **Step 3: Modify `main.py`**

Apply these edits:

(a) Add the import near the other imports (after `from agent.orchestrator import ...`):

```python
from agent.cli_presenter import CLIPresenter
```

(b) Add a module-level presenter slot near the top of the file (after the UTF-8 reconfigure block, before the function defs):

```python
_presenter: CLIPresenter | None = None
```

(c) Replace the body of `confirm_interactive` so it delegates to the presenter when one is installed, preserving the legacy behaviour otherwise (keeps existing tests green):

```python
def confirm_interactive(summary: str, action: dict[str, Any]) -> bool:
    """Default human-confirmation callback for risky and destructive actions.

    Delegates to the rich presenter when one is installed (REPL / one-shot);
    otherwise falls back to a plain input() prompt. In a non-TTY it prints a
    warning and denies the action instead of blocking.
    """
    if _presenter is not None:
        return _presenter.confirm(summary, action)
    print(f"\n[confirm] {summary}")
    if not sys.stdin.isatty():
        print(
            "[warning] Non-interactive mode: this action requires approval but stdin "
            "is not a TTY.\n"
            "          Re-run with --yes (write_risky) or --yes-destructive to "
            "auto-approve.\n"
            "          Denying this action."
        )
        return False
    try:
        answer = input("Approve? (y/n): ").strip().lower()
    except EOFError:
        print(
            "[warning] EOF on stdin; denying action. "
            "Re-run with --yes to auto-approve."
        )
        return False
    return answer in {"y", "yes"}
```

(d) In `_run_repl`, print the banner after `await agent.initialize()` and switch the input/answer calls to the presenter:

```python
async def _run_repl(agent: AgentOrchestrator, logger: Any) -> int:
    await agent.initialize()
    if _presenter is not None:
        _presenter.banner()
    logger.info("Caelum-Agent ready. Type a command or /quit.")

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                if _presenter is not None:
                    user_input = await loop.run_in_executor(None, _presenter.input)
                else:
                    user_input = await loop.run_in_executor(None, input, "> ")
            except EOFError:
                break
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input == "/quit":
                break
            if user_input == "/stop":
                logger.info("Kill switch triggered via /stop")
                await agent.eventbus.emit(KillSwitchTriggered(reason="/stop"))
                continue
            if user_input == "/help":
                _print_help()
                continue
            if user_input == "/status":
                await _print_status(agent)
                continue
            if user_input == "/approve":
                print("Use the interactive prompt shown before risky actions.")
                continue
            try:
                result = await agent.run_task(user_input)
                if _presenter is not None:
                    _presenter.print_answer(result)
                else:
                    print(result)
            except Exception as exc:
                logger.exception("Task failed: %s", exc)
    finally:
        await agent.shutdown()

    return 0
```

(e) In `_run_one_shot`, render the answer via the presenter:

```python
async def _run_one_shot(agent: AgentOrchestrator, task: str, logger: Any) -> int:
    try:
        result = await agent.run_task(task)
        if _presenter is not None:
            _presenter.print_answer(result)
        else:
            print(result)
    except Exception as exc:
        logger.exception("Task failed: %s", exc)
        return 1
    return 0
```

(f) In `main()`, pass `console=False` to logging and build/attach/detach the presenter. Replace the `setup_logging(...)` call and the agent construction block:

```python
    log_level = args.log_level or config.logging.level
    logger = setup_logging(
        level=log_level,
        log_dir=Path(config.logging.data_dir) / "logs",
        console=False,
    )

    eventbus = EventBus()
    eventbus.subscribe("AgentStateChanged", lambda e: _log_state(e, logger))

    presenter = CLIPresenter()
    presenter.attach(eventbus)
    global _presenter
    _presenter = presenter

    llm = LLMClient(config.llm)
    mcp = MCPMultiplexer(config.mcp_servers)
    kill_switch = KillSwitch(eventbus)
    agent = AgentOrchestrator(config, eventbus, llm, mcp, kill_switch)

    agent.set_human_confirmation_callback(confirm_interactive)
```

(g) Detach the presenter on every exit path. Wrap the tail of `main()` so `detach()` runs in `finally`:

```python
    try:
        if args.task:
            await agent.initialize()
            try:
                return await _run_one_shot(agent, args.task, logger)
            finally:
                await agent.shutdown()
        return await _run_repl(agent, logger)
    finally:
        presenter.detach()
        global _presenter
        _presenter = None
```

(Remove the now-duplicated `if args.task: ... return await _run_repl(...)` block that previously followed the yes/yes-destructive handling — it is subsumed by the `try/finally` above.)

- [ ] **Step 4: Run the main tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_extra.py tests/test_main.py -q --no-cov
```

Expected: all pass (existing behaviour preserved via the `_presenter is None` fallback).

- [ ] **Step 5: Commit**

```powershell
git add main.py tests/test_main_extra.py
git commit -m "feat(cli): wire rich presenter into REPL and one-shot"
```

---

## Task 6: Full-suite + manual verification

**Files:** none (verification only; commit fixes if any).

- [ ] **Step 1: Run the full non-smoke suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q -m "not smoke" --no-cov
```

Expected: the prior 299 baseline + the new presenter/logging/main tests all pass; nothing regressed (the orchestrator was not touched).

- [ ] **Step 2: Syntax/import sanity**

Run:

```powershell
.\.venv\Scripts\python.exe -c "import agent.cli_presenter, main; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Manual REPL check (interactive)**

Run (real config required; uses Kimi + MCP):

```powershell
python main.py
```

Then:
- Observe the banner line and a `›` prompt.
- Type `hello` and Enter → spinner shows, then a markdown answer panel (the model should call `CompleteTask`; no tool lines).
- Type `open notepad and type 'hello'` and Enter → expect `▶ windows__Snapshot`, `✓`, `▶ windows__Click`/`Type`, `✓`, spinner `Verifying…`, then the answer panel. Confirm no `YYYY-MM-DD | INFO | caelum...` log lines appear on screen.
- Open another terminal and `Get-Content data/logs/agent.log -Wait` → confirm logs still append there.
- Type `/quit` → clean exit; no spinner left running.

- [ ] **Step 4: Manual one-shot + non-TTY check**

```powershell
python main.py --task "what is 2+2" --yes
"pipeline" | python main.py --task "say hi" --yes
```

Expected: first prints a spinner + answer panel and exits 0; second (piped stdin) prints plain output without ANSI crashes and exits 0 (rich downgrades on non-TTY).

- [ ] **Step 5: Commit any verification fixes**

If Steps 3–4 surfaced issues, fix them in the relevant file and commit with a descriptive message (e.g. `fix(cli): stop spinner before printing answer`). Otherwise nothing to commit.

---

## Self-Review

**1. Spec coverage:**
- Banner / styled prompt → Task 5 (`banner`, `input`). ✓
- Live spinner through the cycle → Task 3 (`_start_status`/`_update_status`/`_stop_status` driven by events). ✓
- Per-tool call/result lines → Task 3 (`_on_tool_requested`/`_on_tool_completed`). ✓
- Markdown final answer in a panel → Task 3 (`print_answer`). ✓
- Confirm via rich, non-TTY deny preserved → Task 3/4 (`confirm`). ✓
- Logging file-only when presenter active → Task 2 (`console` param) + Task 5 (`console=False`). ✓
- Event-driven, orchestrator untouched → Task 3/5 (no orchestrator edits). ✓
- REPL + one-shot both covered → Task 5 (`_run_repl` + `_run_one_shot`). ✓
- Non-TTY downgrade → rich handles it; confirm non-TTY path tested (Task 4). ✓
- Presenter exceptions never abort a task → Task 3 (try/except in every handler). ✓
- Tests → Tasks 2/3/4/5; full suite in Task 6. ✓
- Out-of-scope items (prompt_toolkit, streaming, dashboard, theme) → not in plan. ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling". Every code step contains complete code. `rich` pin is a concrete range with an explicit install command. No "similar to Task N" references.

**3. Type/signature consistency:**
- `CLIPresenter(console=None, *, enabled=True)` used identically in Task 3 impl, Task 3 tests (`CLIPresenter(console=console)`), and Task 5 main wiring (`CLIPresenter()`). ✓
- `attach(bus)` / `detach()` consistent. ✓
- Event class names match `eventbus/events.py` exactly (`UserInputReceived`, `AgentStateChanged`, `LLMResponseReceived`, `ToolCallRequested`, `ToolCallCompleted`, `KillSwitchTriggered`). ✓
- `confirm_interactive(summary, action) -> bool` signature unchanged; `_presenter` global used consistently. ✓
- `setup_logging(level, log_dir, fmt, console)` — Task 2 signature matches Task 5 call (`console=False`). ✓
- `EventBus.subscribe(name, handler)` / `unsubscribe(name, handler)` signatures match `eventbus/__init__.py`. ✓
- Handlers are `async def` consistently (loop-thread safety), and `tests/test_cli_presenter.py` drives them via `await bus.emit(...)`. ✓
