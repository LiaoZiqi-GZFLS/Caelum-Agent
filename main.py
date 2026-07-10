"""CLI entry point for Caelum-Agent."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# Ensure the Windows console can print Unicode (emoji, CJK) without crashing on
# the GBK code page. reconfigure() is a no-op when the stream is already UTF-8
# (modern terminals, pytest capture). `errors="replace"` guarantees we never
# raise UnicodeEncodeError even on legacy terminals.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from agent.config import Config, load_config
from agent.kill_switch import KillSwitch
from agent.llm_client import LLMClient
from agent.logging_config import setup_logging
from agent.orchestrator import AgentOrchestrator
from agent.cli_presenter import CLIPresenter
from eventbus import EventBus
from eventbus.events import AgentStateChanged, KillSwitchTriggered
from mcp_client import MCPMultiplexer


_presenter: CLIPresenter | None = None


def _log_state(event: Any, logger: Any) -> None:
    if isinstance(event, AgentStateChanged):
        logger.info("State transition: %s -> %s", event.old_state, event.new_state)


def _print_help() -> None:
    print(
        "Available commands:\n"
        "  /help    - show this message\n"
        "  /status  - show agent status\n"
        "  /stop    - cancel the current task\n"
        "  /quit    - exit the agent"
    )


async def _print_status(agent: AgentOrchestrator) -> None:
    health: list[str] = []
    for name, client in agent.mcp.clients.items():
        if client._connected:
            health.append(f"{name}: connected")
        else:
            health.append(f"{name}: disconnected")
    print(
        f"State: {agent.state.current_state}\n"
        f"Task ID: {agent.task_id}\n"
        f"Last action: {agent.last_action_summary}\n"
        f"Consecutive action failures: {agent.consecutive_action_failures}\n"
        f"Consecutive API failures: {agent.consecutive_api_failures}\n"
        f"MCP health: {', '.join(health)}"
    )


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Caelum-Agent desktop automation CLI")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--task", type=str, default=None, help="Run one task and exit")
    parser.add_argument("--no-vision", action="store_true", help="Disable UI detector and OCR")
    parser.add_argument("--log-level", type=str, default=None, help="Override logging level")
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Auto-approve write_risky confirmations (Click/Type/App/browser edits). "
            "Destructive actions still require typed confirmation unless "
            "--yes-destructive is set."
        ),
    )
    parser.add_argument(
        "--yes-destructive",
        action="store_true",
        help=(
            "Also auto-approve destructive actions, skipping typed confirmation. "
            "Implies --yes. Use with caution."
        ),
    )
    return parser


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


async def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print("Config not found. Copy config.yaml.example to config.yaml and edit it.")
        return 1

    if args.no_vision:
        config.ui_detector.enabled = False
        config.ocr.enabled = False

    log_level = args.log_level or config.logging.level
    logger = setup_logging(
        level=log_level,
        log_dir=Path(config.logging.data_dir) / "logs",
        console=False,
    )

    eventbus = EventBus()
    eventbus.subscribe("AgentStateChanged", lambda e: _log_state(e, logger))

    global _presenter
    presenter = CLIPresenter()
    presenter.attach(eventbus)
    _presenter = presenter

    llm = LLMClient(config.llm)
    mcp = MCPMultiplexer(config.mcp_servers)
    kill_switch = KillSwitch(eventbus)
    agent = AgentOrchestrator(config, eventbus, llm, mcp, kill_switch)
    agent.set_human_confirmation_callback(confirm_interactive)

    if args.yes_destructive:
        agent.security.auto_approve = True
        agent.security.auto_approve_destructive = True
        logger.warning(
            "--yes-destructive: ALL confirmations (including destructive) "
            "will be auto-approved."
        )
    elif args.yes:
        agent.security.auto_approve = True
        logger.info(
            "--yes: write_risky confirmations will be auto-approved; "
            "destructive actions still require typed input."
        )

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
        _presenter = None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
