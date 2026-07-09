"""Tests for the CLI entry point."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.initialize = AsyncMock()
    agent.run_task = AsyncMock(return_value="Done.")
    agent.shutdown = AsyncMock()
    agent.state.current_state = "IDLE"
    agent.task_id = None
    agent.last_action_summary = ""
    agent.consecutive_action_failures = 0
    agent.consecutive_api_failures = 0
    agent.mcp.clients = {
        "playwright": MagicMock(_connected=True),
        "windows": MagicMock(_connected=False),
        "filesystem": MagicMock(_connected=True),
    }
    return agent


@pytest.fixture
def mock_load_config(tmp_path):
    config = SimpleNamespace(
        ui_detector=SimpleNamespace(enabled=True),
        ocr=SimpleNamespace(enabled=True),
        logging=SimpleNamespace(level="INFO", data_dir=str(tmp_path / "logs")),
        mcp_servers={},
        llm={},
        kill_switch=SimpleNamespace(
            api_failure_threshold=5,
            action_failure_threshold=3,
            same_ui_loop_threshold=3,
        ),
    )
    return lambda path=None: config


@pytest.mark.asyncio
async def test_main_one_shot_task(mock_agent, mock_load_config):
    with patch("main.load_config", mock_load_config), \
         patch("main.setup_logging", MagicMock(return_value=MagicMock())), \
         patch("main.LLMClient", MagicMock()), \
         patch("main.MCPMultiplexer", MagicMock()), \
         patch("main.KillSwitch", MagicMock()), \
         patch("main.AgentOrchestrator", return_value=mock_agent), \
         patch("main.EventBus", MagicMock()):
        code = await main.main(["--task", "list files"])

    assert code == 0
    mock_agent.initialize.assert_awaited_once()
    mock_agent.run_task.assert_awaited_once_with("list files")
    mock_agent.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_no_vision_disables_vision_and_ocr(mock_agent, mock_load_config):
    captured: dict[str, Any] = {}
    original_load = mock_load_config()

    def capturing_load(path=None):
        captured["config"] = original_load
        return original_load

    with patch("main.load_config", capturing_load), \
         patch("main.setup_logging", MagicMock(return_value=MagicMock())), \
         patch("main.LLMClient", MagicMock()), \
         patch("main.MCPMultiplexer", MagicMock()), \
         patch("main.KillSwitch", MagicMock()), \
         patch("main.AgentOrchestrator", return_value=mock_agent), \
         patch("main.EventBus", MagicMock()):
        await main.main(["--task", "list files", "--no-vision"])

    assert captured["config"].ui_detector.enabled is False
    assert captured["config"].ocr.enabled is False


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc_info:
        main._build_argparser().parse_args(["--help"])
    assert exc_info.value.code == 0
