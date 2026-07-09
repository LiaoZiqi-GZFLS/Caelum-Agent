"""End-to-end single task test for Caelum-Agent.

Run: python spikes/e2e_single_task.py "open notepad"
"""

from __future__ import annotations

import asyncio
import sys

from agent.config import load_config
from agent.kill_switch import KillSwitch
from agent.llm_client import LLMClient
from agent.logging_config import setup_logging
from agent.orchestrator import AgentOrchestrator
from eventbus import EventBus
from eventbus.events import AgentStateChanged, ToolCallCompleted, ToolCallRequested
from mcp_client import MCPMultiplexer


async def main() -> int:
    logger = setup_logging(level="INFO", log_dir="./data/logs")
    config = load_config()
    # For a fast pipeline test, disable heavy vision/OCR components.
    config.ui_detector.enabled = False
    config.ocr.enabled = False
    config.screenshot.backend = "PIL"

    eventbus = EventBus()

    def log_state(event):
        if isinstance(event, AgentStateChanged):
            logger.info("State: %s -> %s", event.old_state, event.new_state)

    def log_tool_request(event):
        if isinstance(event, ToolCallRequested):
            logger.info("Tool request: %s/%s", event.server, event.tool_name)

    def log_tool_complete(event):
        if isinstance(event, ToolCallCompleted):
            logger.info(
                "Tool complete: %s/%s success=%s",
                event.server,
                event.tool_name,
                event.success,
            )

    eventbus.subscribe("AgentStateChanged", log_state)
    eventbus.subscribe("ToolCallRequested", log_tool_request)
    eventbus.subscribe("ToolCallCompleted", log_tool_complete)

    llm = LLMClient(config.llm)
    mcp = MCPMultiplexer(config.mcp_servers)
    kill_switch = KillSwitch(eventbus)
    agent = AgentOrchestrator(config, eventbus, llm, mcp, kill_switch)

    # Always deny risky/destructive actions in unattended test.
    agent.set_human_confirmation_callback(lambda summary, action: False)

    try:
        await agent.initialize()
    except Exception as exc:
        logger.exception("Initialization failed: %s", exc)
        return 1

    instruction = sys.argv[1] if len(sys.argv) > 1 else "open notepad"
    logger.info("Running task: %s", instruction)
    try:
        result = await asyncio.wait_for(
            agent.run_task(instruction, task_id="e2e-test-1"),
            timeout=120.0,
        )
        logger.info("Result: %s", result)
        print("RESULT:", result)
    except asyncio.TimeoutError:
        logger.error("Task timed out")
        print("RESULT: timeout")
    except Exception as exc:
        logger.exception("Task failed: %s", exc)
        print(f"RESULT: error {exc}")
    finally:
        await agent.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
