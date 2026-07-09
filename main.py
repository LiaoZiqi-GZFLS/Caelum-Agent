"""CLI entry point for Caelum-Agent."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from agent.config import Config, load_config
from agent.kill_switch import KillSwitch
from agent.llm_client import LLMClient
from agent.logging_config import setup_logging
from agent.orchestrator import AgentOrchestrator
from eventbus import EventBus
from eventbus.events import AgentStateChanged, KillSwitchTriggered
from mcp_client import MCPMultiplexer


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
    return parser


async def _run_one_shot(agent: AgentOrchestrator, task: str, logger: Any) -> int:
    try:
        result = await agent.run_task(task)
        print(result)
    except Exception as exc:
        logger.exception("Task failed: %s", exc)
        return 1
    return 0


async def _run_repl(agent: AgentOrchestrator, logger: Any) -> int:
    await agent.initialize()
    logger.info("Caelum-Agent ready. Type a command or /quit.")

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                user_input = await loop.run_in_executor(None, input, "> ")
            except KeyboardInterrupt:
                logger.info("Kill switch triggered via Ctrl+C")
                await agent.eventbus.emit(KillSwitchTriggered(reason="ctrl+c"))
                continue
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
    )

    eventbus = EventBus()
    eventbus.subscribe("AgentStateChanged", lambda e: _log_state(e, logger))

    llm = LLMClient(config.llm)
    mcp = MCPMultiplexer(config.mcp_servers)
    kill_switch = KillSwitch(eventbus)
    agent = AgentOrchestrator(config, eventbus, llm, mcp, kill_switch)

    def _confirm(summary: str, action: dict[str, Any]) -> bool:
        print(f"\n[confirm] {summary}")
        answer = input("Approve? (y/n): ").strip().lower()
        return answer in {"y", "yes"}

    agent.set_human_confirmation_callback(_confirm)

    if args.task:
        await agent.initialize()
        try:
            return await _run_one_shot(agent, args.task, logger)
        finally:
            await agent.shutdown()

    return await _run_repl(agent, logger)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
