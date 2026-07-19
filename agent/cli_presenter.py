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
from rich.text import Text

from agent import choice_menu
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
    lines = s.splitlines()
    line = lines[0] if lines else s
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
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._bus: EventBus | None = None
        self._status: Status | None = None
        # Bind each handler exactly once and reuse the SAME objects for subscribe
        # and unsubscribe. EventBus.unsubscribe compares handlers by identity
        # (``h is not handler``); re-accessing ``self._on_x`` yields a fresh bound
        # method every time, which would never match and detach would silently no-op.
        self._handlers: dict[str, Any] = {
            "UserInputReceived": self._on_user_input,
            "AgentStateChanged": self._on_state,
            "LLMResponseReceived": self._on_llm,
            "ToolCallRequested": self._on_tool_requested,
            "ToolCallCompleted": self._on_tool_completed,
            "KillSwitchTriggered": self._on_kill,
        }

    # -- wiring ---------------------------------------------------------------

    def attach(self, bus: EventBus) -> None:
        self._bus = bus
        for name, handler in self._handlers.items():
            bus.subscribe(name, handler)

    def detach(self) -> None:
        if self._bus is None:
            return
        for name, handler in self._handlers.items():
            self._bus.unsubscribe(name, handler)
        self._bus = None
        self._stop_status()

    # -- direct calls from main.py -------------------------------------------

    def banner(self) -> None:
        self.console.print(
            "[bold cyan]Caelum-Agent[/] [dim]· Kimi K3 · type /help for commands[/]"
        )

    def input(self, prompt: str = "[bold cyan]›[/] ") -> str:
        # Console.input renders the prompt through the console then reads stdin;
        # it raises EOFError on EOF, matching builtin input()'s contract.
        return self.console.input(prompt)

    def print_answer(self, text: str) -> None:
        self._stop_status()
        self.console.print(Panel(Markdown(text or ""), title="Caelum", border_style=STYLE_PANEL_BORDER))

    def stop(self) -> None:
        """Stop the live spinner if one is running (used on unexpected error paths)."""
        self._stop_status()

    def confirm(self, summary: str, action: dict[str, Any]) -> bool:
        # Build the confirm line from styled + plain Text spans so the untrusted
        # ``summary`` is never parsed as rich markup (a ``[/]`` or ``[x]`` in it
        # would otherwise be eaten or raise MarkupError).
        confirm_line = Text("\n")
        confirm_line.append("Confirm:", style="bold yellow")
        confirm_line.append(f" {summary}")
        self.console.print(confirm_line)
        if not sys.stdin.isatty():
            self.console.print(
                "[dim]Non-interactive: action requires approval but stdin is not a TTY; "
                "denying. Re-run with --yes / --yes-all.[/]"
            )
            return False
        try:
            # Suspend the live spinner while waiting for the answer: Status (a
            # Live) and Console.input both write to the terminal, and rich does
            # not suspend a running Live around input() — the two fight over the
            # same line and the prompt appears to hang.
            was_running = self._status is not None
            self._stop_status()
            try:
                return bool(Confirm.ask("[yellow]Approve?[/]", console=self.console, default=False))
            finally:
                if was_running:
                    self._start_status("Thinking…")
        except EOFError:
            self.console.print("[dim]EOF on stdin; denying.[/]")
            return False

    def mcp_status(self, servers: list[tuple[str, bool, int]]) -> None:
        """Print a one-line MCP connection summary: ``MCP  ✓ name (n tools)  ✗ name (failed)``."""
        if not servers:
            return
        line = Text("MCP  ")
        for i, (name, connected, tool_count) in enumerate(servers):
            if i:
                line.append("  ")
            if connected:
                line.append("✓ ", style=STYLE_OK)
                line.append(f"{name} ({tool_count} tools)")
            else:
                line.append("✗ ", style=STYLE_ERR)
                line.append(f"{name} (failed)")
        self.console.print(line)

    def ask_choice(self, question: str, options: list[str]) -> str | None:
        """Ask the human a multiple-choice question via the raw console menu.

        Returns the chosen/typed text, or None on cancel / non-TTY. Suspends
        the spinner exactly like confirm() so the menu owns the terminal.
        """
        self.console.print()  # separate the menu from tool output
        if not sys.stdin.isatty():
            self.console.print(
                "[dim]Non-interactive: cannot ask the human; skipping.[/]"
            )
            return None
        was_running = self._status is not None
        self._stop_status()
        try:
            return choice_menu.ask_choice(question, options, self.console)
        finally:
            if was_running:
                self._start_status("Thinking…")

    # -- status helpers -------------------------------------------------------

    def _start_status(self, label: str) -> None:
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
                # Text applies the style without parsing markup, so brackets in
                # narration ("[1]", "[link](url)", paths) render literally.
                self.console.print(Text(event.content, style=STYLE_NARRATION))
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
            line = Text("  ")
            line.append("▶", style=STYLE_ARROW)
            line.append(f" {name}{suffix}")
            self.console.print(line)
            self._update_status(f"Running {event.tool_name}…")
        except Exception as exc:
            logger.warning("presenter _on_tool_requested failed: %s", exc)

    async def _on_tool_completed(self, event: Any) -> None:
        try:
            if not isinstance(event, ToolCallCompleted):
                return
            mark = "✓" if event.success else "✗"
            style = STYLE_OK if event.success else STYLE_ERR
            result = _first_line(event.result)
            line = Text("  ")
            line.append(mark, style=style)
            line.append(f" {event.tool_name} — {result}")
            self.console.print(line)
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
