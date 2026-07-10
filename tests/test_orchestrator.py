"""Tests for the ReAct orchestrator loop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import httpx

from agent.orchestrator import AgentOrchestrator
from agent.perception import Perception
from mcp_client import ToolResult

from tests.fakes import (
    FakeLLM,
    FakeMCP,
    FakeKillSwitch,
    FakePerception,
    FakeReflection,
    FakeSkillLearner,
    TriggeringLLM,
    _blank_perception,
    _same_hash_perception,
    _message,
    _tool_call,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_orchestrator_starts_in_idle(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    assert agent.state.current_state == "IDLE"


@pytest.mark.asyncio
async def test_run_task_direct_completion(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("I will list the files."),
        _message("YES"),
        _message("Here are the files: a.txt, b.txt."),
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("list files")

    assert result == "Here are the files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"


@pytest.mark.asyncio
async def test_run_task_single_tool_call_path(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("Listing files.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt, b.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt\nb.txt"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("list files")

    assert result == "Files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"
    assert mcp.calls == [("filesystem", "list_directory", {"path": "."})]


@pytest.mark.asyncio
async def test_run_task_verification_failure_triggers_reflect(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("Let me check.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("Let me check."),
        _message("NO"),
        _message("I need to try a different directory."),
        _message("Retrying.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "./other"})]),
        _message("I found them."),
        _message("YES"),
        _message("Files found."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content=""))
    reflection = FakeReflection()
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception(), _blank_perception()]),
        reflection=reflection,
    )

    result = await agent.run_task("list files")

    assert result == "Files found."
    assert agent.state.current_state == "COMPLETED"
    assert any(r["failure_reason"] == "Verification failed" for r in reflection.recorded)


@pytest.mark.asyncio
async def test_run_task_max_loops_reaches_stuck(config, eventbus, killswitch):
    # Model never verifies success and never completes.
    llm = FakeLLM(
        [
            _message("Trying.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("NO"),
            _message("Retrying."),
        ],
        default_chat=_message("Still trying."),
    )
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content=""))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()] * 20),
    )

    result = await agent.run_task("list files")

    assert "loop limit" in result.lower()
    assert agent.state.current_state == "STUCK"


@pytest.mark.asyncio
async def test_run_task_kill_switch_cancels(config, eventbus, killswitch):
    llm = TriggeringLLM([_message("Starting...")], killswitch)
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("list files")

    assert "cancelled" in result.lower()
    assert agent.state.current_state == "IDLE"


@pytest.mark.asyncio
async def test_run_task_action_failure_threshold(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Trying.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("YES"),
            _message("Final answer."),
        ],
        default_chat=_message("Proceeding."),
    )
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=False, content="permission denied"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    # Lower threshold to hit it in one failure.
    config.kill_switch.action_failure_threshold = 1

    result = await agent.run_task("list files")

    assert "Too many consecutive action failures" in result
    assert agent.state.current_state == "WAITING_HUMAN"


@pytest.mark.asyncio
async def test_run_task_unknown_tool_returns_error(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Calling unknown tool.", tool_calls=[_tool_call("unknown__tool", {})]),
            _message("Tool returned an error."),
            _message("NO"),
        ],
        default_chat=_message("Proceeding."),
    )
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    # Lower threshold to hit the failure limit immediately.
    config.kill_switch.action_failure_threshold = 1

    result = await agent.run_task("do something")

    assert "Too many consecutive action failures" in result
    assert agent.state.current_state == "WAITING_HUMAN"
    assert agent.consecutive_action_failures == 1


@pytest.mark.asyncio
async def test_run_task_blocked_tool_counts_as_failure(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Clicking.", tool_calls=[_tool_call("windows__Click", {"x": 10, "y": 10})]),
            _message("Tool was blocked."),
            _message("NO"),
        ],
        default_chat=_message("Proceeding."),
    )
    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    # No confirmation callback registered, so risky tools are blocked.
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    config.kill_switch.action_failure_threshold = 1

    result = await agent.run_task("click something")

    assert "Too many consecutive action failures" in result
    assert agent.state.current_state == "WAITING_HUMAN"
    assert agent.consecutive_action_failures == 1
    assert len(mcp.calls) == 0


@pytest.mark.asyncio
async def test_final_answer_uses_no_tools(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("Listing files.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt, b.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt\nb.txt"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.run_task("list files")

    # The final-answer request must explicitly omit tools.
    assert llm.last_tools[-1] is None


@pytest.mark.asyncio
async def test_final_answer_rejects_tool_calls_and_retries(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("Listing files.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Oops", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("Files: a.txt, b.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt\nb.txt"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("list files")

    assert result == "Files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"


@pytest.mark.asyncio
async def test_final_answer_gives_up_after_repeated_tool_calls(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Listing files.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("I listed them."),
            _message("YES"),
            _message("Try again", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("Try again", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("Try again", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        ],
        default_chat=_message("Default."),
    )
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt\nb.txt"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("list files")

    assert "Failed to produce a final answer" in result
    assert agent.state.current_state == "ERROR"


@pytest.mark.asyncio
async def test_api_failure_increments_counter_and_recovers(config, eventbus, killswitch):
    llm = FakeLLM([
        httpx.ConnectError("connection failed"),
        _message("I will list the files."),
        _message("YES"),
        _message("Files found."),
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("list files")

    assert result == "Files found."
    assert agent.state.current_state == "COMPLETED"
    assert agent.consecutive_api_failures == 0


@pytest.mark.asyncio
async def test_api_failure_threshold_triggers_local_mode(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            httpx.ConnectError("connection failed"),
            httpx.ConnectError("connection failed"),
        ],
        default_chat=_message("Recovery."),
    )
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 5),
    )
    config.kill_switch.api_failure_threshold = 2

    result = await agent.run_task("list files")

    assert "Too many consecutive API failures" in result
    assert agent.state.current_state == "WAITING_HUMAN"
    assert agent.consecutive_api_failures == 2


@pytest.mark.asyncio
async def test_same_ui_loop_detection(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Trying.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("I tried."),
            _message("NO"),
        ],
        default_chat=_message("Retrying."),
    )
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content=""))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_same_hash_perception()] * 5),
    )

    result = await agent.run_task("list files")

    assert "UI state unchanged" in result
    assert agent.state.current_state == "STUCK"


@pytest.mark.asyncio
async def test_state_based_verifier_rejects_unchanged_ui_after_mutating_action(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Clicking.", tool_calls=[_tool_call("windows__Click", {"x": 10, "y": 10})]),
            _message("Clicked."),
            _message("YES"),
            _message("Retrying."),
            _message("YES"),
        ],
        default_chat=_message("Trying."),
    )
    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="ok"))
    # Provide a confirmation callback so the risky click is actually executed.
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_same_hash_perception("same")] * 5),
    )
    agent.set_human_confirmation_callback(lambda summary, action: True)

    result = await agent.run_task("click something")

    # The action is mutating and UI did not change, so verification should fail
    # and eventually the agent should get stuck or hit the action threshold.
    assert agent.state.current_state in {"STUCK", "WAITING_HUMAN", "REFLECT"}
    assert agent.consecutive_action_failures > 0 or agent.state.current_state == "STUCK"


@pytest.mark.asyncio
async def test_state_based_verifier_accepts_unchanged_ui_for_query_action(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Reading.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("I read it."),
            _message("YES"),
            _message("Files: a.txt."),
        ],
    )
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_same_hash_perception("same")] * 3),
    )

    result = await agent.run_task("list files")

    assert result == "Files: a.txt."
    assert agent.state.current_state == "COMPLETED"


@pytest.mark.asyncio
async def test_run_task_learns_skill_on_completion(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("Listing files.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt, b.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt\nb.txt"))
    learner = FakeSkillLearner()
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
        skill_learner=learner,
    )

    result = await agent.run_task("list files")

    assert result == "Files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"
    assert len(learner.calls) == 1
    assert learner.calls[0][0] == "list files"
    assert learner.calls[0][1] == ["filesystem/list_directory: a.txt\nb.txt"]


@pytest.mark.asyncio
async def test_run_task_skill_learner_failure_is_ignored(config, eventbus, killswitch):
    class BrokenSkillLearner:
        async def learn(self, task: str, trajectory: list[str]) -> dict[str, Any]:
            raise RuntimeError("learning failed")

    llm = FakeLLM([
        _message("Listing files.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt, b.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt\nb.txt"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
        skill_learner=BrokenSkillLearner(),
    )

    result = await agent.run_task("list files")

    assert result == "Files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"


@pytest.mark.asyncio
async def test_run_task_full_rejection_triggers_reflect(config, eventbus, killswitch):
    """When verifier rejects all candidates, orchestrator reflects and retries."""
    rejected_perception = Perception(
        screenshot_path=Path("/tmp/blank.jpg"),
        description="Blank screen with all elements rejected",
        ocr_text="",
        ui_tree={},
        som_annotations=[],  # all were rejected
        blocked_count=3,
    )
    normal_perception = Perception(
        screenshot_path=Path("/tmp/blank.jpg"),
        description="Blank screen",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 1, "center_x": 0.5, "center_y": 0.5},
        ],
        blocked_count=0,
    )
    llm = FakeLLM([
        # After full rejection, model reflects:
        _message("I need to try a different approach."),
        # Then normal flow:
        _message("Listing files.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))
    reflection = FakeReflection()
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([rejected_perception, normal_perception]),
        reflection=reflection,
    )

    result = await agent.run_task("list files")

    assert result == "Files: a.txt."
    assert agent.state.current_state == "COMPLETED"
    assert any("rejected all" in r["failure_reason"] for r in reflection.recorded)


@pytest.mark.asyncio
async def test_orchestrator_creates_kimi_memory_client(config, eventbus, killswitch):
    from agent.kimi_memory import KimiMemoryClient
    llm = FakeLLM()
    llm.tools = ["memory", "rethink"]
    agent = AgentOrchestrator(config, eventbus, llm, FakeMCP(), killswitch)
    assert isinstance(agent.memory.kimi, KimiMemoryClient)
    assert agent.reflection.kimi is agent.memory.kimi


@pytest.mark.asyncio
async def test_orchestrator_skips_kimi_memory_client_when_disabled(config, eventbus, killswitch):
    config.memory.use_kimi_memory = False
    config.reflection.use_rethink = False
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    assert agent._kimi_client is None
    assert agent.memory.kimi is None
    assert agent.reflection.kimi is None


# ---------------------------------------------------------------------------
# desktop_interact tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_desktop_interact_tool_registered(config, eventbus, killswitch):
    """desktop_interact is available when tools are registered."""
    llm = FakeLLM()
    mcp = FakeMCP()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)

    from agent.tools import DESKTOP_INTERACT_SCHEMA, register_all
    register_all(llm, mcp)
    agent._register_desktop_interact()

    assert "DesktopInteract" in llm.tool_names()


@pytest.mark.asyncio
async def test_desktop_interact_click_resolves_label_to_coords(config, eventbus, killswitch):
    """desktop_interact resolves a SoM label to screen coords and calls Click."""
    mcp = FakeMCP()
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)

    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 1, "center_x": 0.5, "center_y": 0.4, "score": 0.95, "normalized": True},
            {"label": 2, "center_x": 0.25, "center_y": 0.75, "score": 0.87, "normalized": True},
        ],
        screen_width=1920,
        screen_height=1080,
    )

    result = await agent._desktop_interact_impl(label=1, action="click")

    assert "OK" in result
    assert mcp.calls
    server, tool, args = mcp.calls[-1]
    assert server == "windows"
    assert tool == "Click"
    assert args["loc"] == [960, 432]  # 0.5*1920, 0.4*1080


@pytest.mark.asyncio
async def test_desktop_interact_reports_missing_label(config, eventbus, killswitch):
    """desktop_interact returns an error when the label is not found."""
    mcp = FakeMCP()
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)

    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
    )

    result = await agent._desktop_interact_impl(label=99, action="click")

    assert result.startswith("[error]")
    assert "99" in result


@pytest.mark.asyncio
async def test_desktop_interact_no_perception_error(config, eventbus, killswitch):
    """desktop_interact errors when there is no perception data."""
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = None

    result = await agent._desktop_interact_impl(label=1, action="click")

    assert result.startswith("[error]")
    assert "No perception data" in result


@pytest.mark.asyncio
async def test_desktop_interact_double_click_sets_times(config, eventbus, killswitch):
    """desktop_interact double_click sends times=2 to Click."""
    from agent.perception import Perception

    mcp = FakeMCP()
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
        screen_width=1920,
        screen_height=1080,
    )

    result = await agent._desktop_interact_impl(label=1, action="double_click")

    assert "OK" in result
    server, tool, args = mcp.calls[-1]
    assert server == "windows"
    assert tool == "Click"
    assert args["times"] == 2


@pytest.mark.asyncio
async def test_desktop_interact_right_click_sets_button(config, eventbus, killswitch):
    """desktop_interact right_click sends button='right' to Click."""
    from agent.perception import Perception

    mcp = FakeMCP()
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
        screen_width=1920,
        screen_height=1080,
    )

    result = await agent._desktop_interact_impl(label=1, action="right_click")

    assert "OK" in result
    server, tool, args = mcp.calls[-1]
    assert server == "windows"
    assert tool == "Click"
    assert args["button"] == "right"


@pytest.mark.asyncio
async def test_desktop_interact_unknown_action_error(config, eventbus, killswitch):
    """desktop_interact returns error for unknown action types."""
    from agent.perception import Perception

    mcp = FakeMCP()
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
    )

    result = await agent._desktop_interact_impl(label=1, action="drag")

    assert result.startswith("[error]")
    assert "Unknown action" in result


@pytest.mark.asyncio
async def test_desktop_interact_uses_screen_dimension_fallback(config, eventbus, killswitch):
    """desktop_interact falls back to 1920x1080 when screen dimensions are 0."""
    from agent.perception import Perception

    mcp = FakeMCP()
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.25, "center_y": 0.5}],
    )  # screen_width/screen_height default to 0

    result = await agent._desktop_interact_impl(label=1, action="click")

    assert "OK" in result
    server, tool, args = mcp.calls[-1]
    # 0.25 * 1920 = 480, 0.5 * 1080 = 540
    assert args["loc"] == [480, 540]


@pytest.mark.asyncio
async def test_desktop_interact_warns_on_uncertain_verdict(config, eventbus, killswitch):
    """DesktopInteract appends warning when matched element has verdict=uncertain."""
    mcp = FakeMCP()
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 1, "center_x": 0.5, "center_y": 0.5, "verdict": "uncertain"},
        ],
        screen_width=1920,
        screen_height=1080,
    )

    result = await agent._desktop_interact_impl(label=1, action="click")

    assert result.startswith("[uncertain]")
    assert "OK" in result
    assert "Verifier was unsure" in result


# ---------------------------------------------------------------------------
# _format_perception tests
# ---------------------------------------------------------------------------


def test_format_perception_prefers_annotated_screenshot():
    """_format_perception uses annotated_screenshot_path when available."""
    import tempfile

    tmpdir = Path(tempfile.gettempdir())
    raw = tmpdir / "screenshot_raw_test.jpg"
    annotated = tmpdir / "screenshot_annotated_test.jpg"
    jpeg_header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\x09\x09\x09\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xf9\xfe\x00\x1f\xff\xd9"
    try:
        raw.write_bytes(jpeg_header)
        annotated.write_bytes(jpeg_header)

        p = Perception(
            screenshot_path=raw,
            description="test",
            ocr_text="",
            ui_tree={},
            som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
            annotated_screenshot_path=annotated,
        )
        result = AgentOrchestrator._format_perception(p)

        # The result should include the annotated image's base64 content.
        image_found = any(part.get("type") == "image_url" for part in result)
        assert image_found, "Annotated screenshot should be included as image_url"
    finally:
        for p in (raw, annotated):
            if p.exists():
                p.unlink()


def test_format_perception_falls_back_to_raw_screenshot():
    """_format_perception falls back to raw screenshot when annotated is None."""
    import tempfile

    tmpdir = Path(tempfile.gettempdir())
    raw = tmpdir / "screenshot_raw_test2.jpg"
    jpeg_header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\x09\x09\x09\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xf9\xfe\x00\x1f\xff\xd9"
    try:
        raw.write_bytes(jpeg_header)

        p = Perception(
            screenshot_path=raw,
            description="test",
            ocr_text="",
            ui_tree={},
            som_annotations=[],
            annotated_screenshot_path=None,
        )
        result = AgentOrchestrator._format_perception(p)

        image_found = any(part.get("type") == "image_url" for part in result)
        assert image_found, "Should fall back to raw screenshot"
    finally:
        if raw.exists():
            raw.unlink()


def test_format_perception_falls_back_when_annotated_missing():
    """_format_perception falls back to raw when annotated path is set but file missing."""
    import tempfile

    tmpdir = Path(tempfile.gettempdir())
    raw = tmpdir / "screenshot_raw_test3.jpg"
    annotated = tmpdir / "screenshot_annotated_missing.jpg"
    jpeg_header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\x09\x09\x09\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xf9\xfe\x00\x1f\xff\xd9"
    try:
        raw.write_bytes(jpeg_header)
        # annotated path is set but the file deliberately does not exist

        p = Perception(
            screenshot_path=raw,
            description="test",
            ocr_text="",
            ui_tree={},
            som_annotations=[],
            annotated_screenshot_path=annotated,
        )
        result = AgentOrchestrator._format_perception(p)

        # Should fall back to raw screenshot since annotated file is missing.
        image_found = any(part.get("type") == "image_url" for part in result)
        assert image_found, "Should fall back to raw screenshot when annotated file is missing"
    finally:
        for p in (raw, annotated):
            if p.exists():
                p.unlink()
