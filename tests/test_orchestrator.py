"""Tests for the ReAct orchestrator loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import httpx

from agent.orchestrator import AgentOrchestrator
from agent.perception import Perception
from eventbus.events import AgentStateChanged
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
    # A plain no-tool reply (the model did NOT call CompleteTask) still flows
    # through the verify + final-answer cycle, exactly as before.
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
async def test_tool_round_still_runs_verify(config, eventbus, killswitch):
    # When the Think step dispatches a tool, the post-action perception +
    # verify + final-answer cycle still runs (fast path is NOT taken).
    perception = FakePerception([_blank_perception(), _blank_perception()])
    llm = FakeLLM([
        _message("Listing.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=perception,
    )

    result = await agent.run_task("list files")

    assert result == "Files: a.txt."
    assert agent.state.current_state == "COMPLETED"
    assert len(perception.calls) == 2  # top-of-loop + post-action
    assert mcp.calls == [("filesystem", "list_directory", {"path": "."})]


# ---------------------------------------------------------------------------
# CompleteTask (model-decided fast path) tests
# ---------------------------------------------------------------------------

class _CompletingLLM(FakeLLM):
    """FakeLLM that routes the CompleteTask local tool to the agent's handler."""

    def __init__(self, agent: AgentOrchestrator, chat_responses: list[Any]) -> None:
        super().__init__(chat_responses)
        self._agent = agent

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in calls:
            if c.function.name == "CompleteTask":
                args = json.loads(c.function.arguments)
                content = self._agent._complete_task_impl(args.get("answer", ""))
            else:
                content = "{}"
            out.append({"role": "tool", "tool_call_id": c.id, "content": content})
        return out


def _wire_complete_task(agent: AgentOrchestrator, llm: "_CompletingLLM") -> None:
    agent.llm = llm
    agent._register_complete_task()  # adds "CompleteTask" to llm.tool_names()


@pytest.mark.asyncio
async def test_complete_task_impl_sets_pending(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    assert agent._pending_completion is None

    content = agent._complete_task_impl("done answer")

    assert agent._pending_completion == "done answer"
    assert "complete" in content.lower()


@pytest.mark.asyncio
async def test_complete_task_returns_answer_skipping_verify(config, eventbus, killswitch):
    # The model decides the greeting needs no action and calls CompleteTask, so
    # the orchestrator returns its answer right after Perceive -> Think: no second
    # perception, no _verify, no _final_answer.
    perception = FakePerception([_blank_perception()])
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch, perception=perception
    )
    llm = _CompletingLLM(
        agent,
        [
            _message(
                "你好！",
                tool_calls=[_tool_call("CompleteTask", {"answer": "你好！有什么可以帮你的？"})],
            )
        ],
    )
    _wire_complete_task(agent, llm)

    result = await agent.run_task("你好")

    assert result == "你好！有什么可以帮你的？"
    assert agent.state.current_state == "COMPLETED"
    assert len(perception.calls) == 1  # top-of-loop perceive only; no post-action


@pytest.mark.asyncio
async def test_complete_task_after_action_skips_verify_by_model_choice(
    config, eventbus, killswitch
):
    # The model acted (filesystem list) and THEN chose CompleteTask to finish,
    # explicitly opting out of verification. We honor its decision: return the
    # answer, skip the post-action perceive/verify, and still learn a skill
    # because an action was taken (action_traces is non-empty).
    perception = FakePerception([_blank_perception()])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))
    learner = FakeSkillLearner()
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), mcp, killswitch,
        perception=perception,
        skill_learner=learner,
    )
    llm = _CompletingLLM(
        agent,
        [
            _message("Listing.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
            _message("Done.", tool_calls=[_tool_call("CompleteTask", {"answer": "Here: a.txt"})]),
        ],
    )
    _wire_complete_task(agent, llm)

    result = await agent.run_task("list files")

    assert result == "Here: a.txt"
    assert agent.state.current_state == "COMPLETED"
    assert len(perception.calls) == 1  # no post-action perceive
    assert mcp.calls == [("filesystem", "list_directory", {"path": "."})]
    if agent._background_tasks:
        await asyncio.gather(*agent._background_tasks)
    assert len(learner.calls) == 1


@pytest.mark.asyncio
async def test_text_form_complete_task_is_honored(config, eventbus, killswitch):
    # The model parrots the call as plain text instead of invoking the tool:
    # content is exactly CompleteTask(answer='...') with NO tool_calls. Treat it
    # as a real CompleteTask: return the answer, skip verify + final answer.
    perception = FakePerception([_blank_perception()])
    llm = FakeLLM([_message("CompleteTask(answer='你好！很高兴为你服务。')")])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch, perception=perception,
    )

    result = await agent.run_task("你好")

    assert result == "你好！很高兴为你服务。"
    assert agent.state.current_state == "COMPLETED"
    assert len(llm.calls) == 1  # no verify / final-answer round-trips
    assert len(perception.calls) == 1  # no post-action perceive
    assert agent._pending_completion is None  # consumed


@pytest.mark.asyncio
async def test_text_answer_mentioning_complete_task_still_verifies(
    config, eventbus, killswitch
):
    # Only an ENTIRE-content match counts. A text answer that merely starts
    # with the call syntax (e.g. explaining it) stays on the normal verify path.
    llm = FakeLLM([
        _message("CompleteTask(answer='x') is the fast-finish tool."),
        _message("YES"),
        _message("It lets the model finish a conversational turn directly."),
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("what does CompleteTask do?")

    assert result == "It lets the model finish a conversational turn directly."
    assert len(llm.calls) == 3  # answer + verify + final answer


# ---------------------------------------------------------------------------
# RequestHumanHelp (human handoff) tests
# ---------------------------------------------------------------------------

class _HumanHelpLLM(FakeLLM):
    """FakeLLM that routes RequestHumanHelp to the agent's real handler."""

    def __init__(self, agent: AgentOrchestrator, chat_responses: list[Any]) -> None:
        super().__init__(chat_responses)
        self._agent = agent

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in calls:
            args = json.loads(c.function.arguments)
            content = await self._agent._request_human_help_impl(**args)
            out.append({"role": "tool", "tool_call_id": c.id, "content": content})
        return out


def _wire_human_help(agent: AgentOrchestrator, llm: "_HumanHelpLLM") -> None:
    agent.llm = llm
    agent._register_human_help()  # adds "RequestHumanHelp" to llm.tool_names()


@pytest.mark.asyncio
async def test_request_human_help_pauses_and_resumes(config, eventbus, killswitch):
    scripted = [
        _message("需要登录", tool_calls=[_tool_call(
            "RequestHumanHelp",
            {"question": "是否已经手动完成登录？",
             "options": ["是，已完成登录", "否，我暂时无法完成登录"]},
        )]),
        _message("继续完成任务。"),
        _message("YES"),
        _message("热榜前三：……"),
    ]
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    _wire_human_help(agent, _HumanHelpLLM(agent, scripted))
    agent.set_human_question_callback(lambda q, o: "是，已完成登录")

    states: list[str] = []

    async def _rec(e: Any) -> None:
        if isinstance(e, AgentStateChanged):
            states.append(e.new_state)

    eventbus.subscribe("AgentStateChanged", _rec)

    result = await agent.run_task("总结知乎热榜")

    assert result == "热榜前三：……"
    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    assert any("Human answered: 是，已完成登录" in m["content"] for m in tool_msgs)
    assert "WAITING_HUMAN" in states
    # The handler restores EXECUTING after the human answers.
    assert "EXECUTING" in states[states.index("WAITING_HUMAN"):]


@pytest.mark.asyncio
async def test_request_human_help_cancel_returns_cancelled(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.set_human_question_callback(lambda q, o: None)
    await agent.state.transition("PLANNING", task_id="t1")
    await agent.state.transition("EXECUTING", task_id="t1")

    content = await agent._request_human_help_impl("q", ["a", "b"])

    assert content.startswith("[cancelled]")
    assert agent.state.current_state == "EXECUTING"  # restored after cancel


@pytest.mark.asyncio
async def test_request_human_help_callback_exception_returns_cancelled(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    def boom(q, o):
        raise RuntimeError("menu exploded")

    agent.set_human_question_callback(boom)
    await agent.state.transition("PLANNING", task_id="t1")
    await agent.state.transition("EXECUTING", task_id="t1")

    content = await agent._request_human_help_impl("q", ["a", "b"])

    assert content.startswith("[cancelled]")
    assert agent.state.current_state == "EXECUTING"  # restored even on callback failure


@pytest.mark.asyncio
async def test_request_human_help_without_callback_is_unavailable(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    content = await agent._request_human_help_impl("q", ["a", "b"])
    assert content.startswith("[unavailable]")


@pytest.mark.asyncio
async def test_request_human_help_rejects_bad_options(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.set_human_question_callback(lambda q, o: "x")
    assert (await agent._request_human_help_impl("q", [])).startswith("[error]")
    assert (await agent._request_human_help_impl("q", ["a"])).startswith("[error]")
    assert (await agent._request_human_help_impl("", ["a", "b"])).startswith("[error]")
    assert (await agent._request_human_help_impl("q", ["a", "b", "c", "d", "e"])).startswith("[error]")


@pytest.mark.asyncio
async def test_system_prompt_guides_complete_task(config, eventbus, killswitch):
    # The system prompt must tell the model when to use CompleteTask vs a normal
    # final answer, so the skip-verify decision stays the model's.
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("x")]), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    llm = _CompletingLLM(
        agent,
        [_message("hi", tool_calls=[_tool_call("CompleteTask", {"answer": "hi"})])],
    )
    _wire_complete_task(agent, llm)

    await agent.run_task("hi")

    system_content = agent.history[0]["content"]
    assert "CompleteTask" in system_content
    assert "verified" in system_content
    # Browser/website tasks must be steered to Playwright, not the desktop icon.
    assert "browser_navigate" in system_content
    # No copy-pasteable call literal: models parrot "CompleteTask(answer=..."
    # as plain text instead of invoking the tool.
    assert "CompleteTask(answer=" not in system_content
    # The human-handoff tool must be advertised in the system prompt.
    assert "RequestHumanHelp" in system_content
    # Scratch files must be steered to the cache directory, not the repo root.
    assert str(config.cache_dir_absolute()) in system_content
    # Interactive by default: the prompt says a human is available.
    assert "A human is at the keyboard" in system_content


@pytest.mark.asyncio
async def test_system_prompt_non_interactive_forbids_human_help(config, eventbus, killswitch):
    # Piped/scripted runs must tell the model up front that no human can
    # answer, so it skips RequestHumanHelp instead of burning a round-trip.
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("x")]), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    llm = _CompletingLLM(
        agent,
        [_message("hi", tool_calls=[_tool_call("CompleteTask", {"answer": "hi"})])],
    )
    _wire_complete_task(agent, llm)
    agent.set_interactive(False)

    await agent.run_task("hi")

    system_content = agent.history[0]["content"]
    assert "non-interactive" in system_content
    assert "A human is at the keyboard" not in system_content


@pytest.mark.asyncio
async def test_initialize_creates_cache_dir(monkeypatch, config, eventbus, killswitch):
    # The cache directory must exist by the time tools run, so scratch files
    # steered there by the system prompt can actually be written.
    monkeypatch.setattr("ui_detector.UIDetector", _SpyUIDetector)
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.perception.shutdown = lambda: None

    await agent.initialize()
    try:
        assert config.cache_dir_absolute().is_dir()
    finally:
        await agent.shutdown()


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
async def test_kill_switch_cancels_remaining_tool_calls(config, eventbus, killswitch):
    """If the kill switch fires during a tool call, subsequent calls are skipped."""

    class TriggeringMCP(FakeMCP):
        async def call(
            self, server: str, tool_name: str, arguments: dict[str, Any]
        ) -> ToolResult:
            if len(self.calls) == 0:
                await killswitch.trigger()
            return await super().call(server, tool_name, arguments)

    llm = FakeLLM([
        _message("Clicking twice.", tool_calls=[
            _tool_call("windows__Click", {"loc": [10, 10]}),
            _tool_call("windows__Click", {"loc": [20, 20]}),
        ]),
    ])
    mcp = TriggeringMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.set_human_confirmation_callback(lambda summary, action: True)

    result = await agent.run_task("click twice")

    assert len(mcp.calls) == 1
    assert "cancelled" in result.lower()


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
            _message("Clicking.", tool_calls=[_tool_call("windows__Click", {"loc": [10, 10]})]),
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
    # Same-UI-loop detection only applies to UI tools (windows/playwright). A
    # repeating Click that never changes the screen should trip the guard.
    llm = FakeLLM(
        [
            # loop 1
            _message("Clicking.", tool_calls=[_tool_call("windows__Click", {"loc": [10, 10]})]),
            _message("Clicked."),
            _message("NO"),
            _message("reflect 1"),
            # loop 2
            _message("Clicking again.", tool_calls=[_tool_call("windows__Click", {"loc": [10, 10]})]),
            _message("Clicked again."),
            _message("NO"),
            _message("reflect 2"),
        ],
    )
    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="ok"))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_same_hash_perception("same")] * 5),
    )
    agent.set_human_confirmation_callback(lambda summary, action: True)

    result = await agent.run_task("click something")

    assert "UI state unchanged" in result
    assert agent.state.current_state == "STUCK"
    assert agent._used_ui_tool is True


@pytest.mark.asyncio
async def test_state_based_verifier_rejects_unchanged_ui_after_mutating_action(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Clicking.", tool_calls=[_tool_call("windows__Click", {"loc": [10, 10]})]),
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

    # Skill learning is fire-and-forget now; drain the background task before
    # asserting on the learner.
    if agent._background_tasks:
        await asyncio.gather(*agent._background_tasks)

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


@pytest.mark.asyncio
async def test_pure_compute_task_completes_without_stuck(config, eventbus, killswitch):
    # A pure-compute Formula tool (quickjs) never touches the screen, so an
    # unchanged UI must not trip the same-UI-loop guard or fail verification.
    llm = FakeLLM(
        [
            _message("Computing.", tool_calls=[_tool_call("quickjs", {"code": "2**20"})]),
            _message("The answer is 1048576."),
            _message("YES"),
            _message("1048576"),
        ],
        tool_responses=[
            [{"role": "tool", "tool_call_id": "call_1", "content": "1048576"}],
        ],
        tool_names=["quickjs"],
    )
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_same_hash_perception("same")] * 3),
    )

    result = await agent.run_task("compute 2^20")

    assert result == "1048576"
    assert agent.state.current_state == "COMPLETED"
    assert agent._used_ui_tool is False


@pytest.mark.asyncio
async def test_verify_trusts_llm_for_non_ui_task(config, eventbus, killswitch):
    # When no UI tool was used, _verify trusts the model's YES even though the
    # screen did not change.
    llm = FakeLLM([_message("YES")])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_same_hash_perception("same")]),
    )
    assert agent._used_ui_tool is False

    prev = _same_hash_perception("same")
    curr = _same_hash_perception("same")
    assert await agent._verify(prev, curr) is True


@pytest.mark.asyncio
async def test_windows_tool_sets_used_ui_flag(config, eventbus, killswitch):
    # Executing a windows MCP tool flips _used_ui_tool; a filesystem tool does not.
    mcp = FakeMCP([
        {"server": "windows", "name": "Click", "description": "", "schema": {}},
        {"server": "filesystem", "name": "list_directory", "description": "", "schema": {}},
    ])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="ok"))
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))

    ui_agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)
    ui_agent.set_human_confirmation_callback(lambda summary, action: True)
    await ui_agent._execute_tool_calls([_tool_call("windows__Click", {"loc": [1, 1]})])
    assert ui_agent._used_ui_tool is True

    fs_agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)
    await fs_agent._execute_tool_calls([_tool_call("filesystem__list_directory", {"path": "."})])
    assert fs_agent._used_ui_tool is False


# ---------------------------------------------------------------------------
# identical-batch dedup tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_batch_skipped_after_success(config, eventbus, killswitch):
    # Re-emitting the exact same (tool, args) that already succeeded is
    # short-circuited: MCP is NOT called again and a notice is returned.
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)

    first = [_tool_call("filesystem__list_directory", {"path": "."}, call_id="1")]
    r1 = await agent._execute_tool_calls(first)
    assert r1[0]["content"] == "a.txt"
    assert len(mcp.calls) == 1

    again = [_tool_call("filesystem__list_directory", {"path": "."}, call_id="2")]
    r2 = await agent._execute_tool_calls(again)
    assert len(mcp.calls) == 1  # no second execution
    assert r2[0]["tool_call_id"] == "2"
    assert r2[0]["content"].startswith("[notice]")


@pytest.mark.asyncio
async def test_identical_batch_not_skipped_after_failure(config, eventbus, killswitch):
    # If the first batch failed, an identical retry must still execute (the
    # failure may have been transient), so dedup stays OFF.
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=False, content="permission denied"))
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)

    batch = [_tool_call("filesystem__list_directory", {"path": "."})]
    r1 = await agent._execute_tool_calls(batch)
    assert r1[0]["content"] == "permission denied"
    assert agent._last_batch_all_succeeded is False

    r2 = await agent._execute_tool_calls([_tool_call("filesystem__list_directory", {"path": "."})])
    assert len(mcp.calls) == 2  # executed again, not deduped
    assert not r2[0]["content"].startswith("[notice]")


@pytest.mark.asyncio
async def test_different_args_not_deduped(config, eventbus, killswitch):
    # Same tool but different arguments are a different batch and both run.
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)

    await agent._execute_tool_calls([_tool_call("filesystem__list_directory", {"path": "."})])
    await agent._execute_tool_calls([_tool_call("filesystem__list_directory", {"path": "./other"})])
    assert len(mcp.calls) == 2


# ---------------------------------------------------------------------------
# skill-learning (non-blocking) tests
# ---------------------------------------------------------------------------


class _SlowLearner(FakeSkillLearner):
    def __init__(self, delay: float = 0.2) -> None:
        super().__init__()
        self.delay = delay
        self.finished = False

    async def learn(self, task: str, trajectory: list[str]) -> dict[str, Any]:
        await asyncio.sleep(self.delay)
        self.finished = True
        return await super().learn(task, trajectory)


@pytest.mark.asyncio
async def test_skill_learning_scheduled_without_blocking(config, eventbus, killswitch):
    learner = _SlowLearner(delay=0.3)
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch, skill_learner=learner
    )

    agent._schedule_skill_learning()

    # Returns immediately: the slow learn has not finished yet, and a task is
    # tracked so it won't be garbage-collected.
    assert learner.finished is False
    assert len(agent._background_tasks) == 1

    await asyncio.sleep(0.45)
    assert learner.finished is True


@pytest.mark.asyncio
async def test_shutdown_drains_background_skill_learning(config, eventbus, killswitch):
    learner = _SlowLearner(delay=0.2)
    agent = AgentOrchestrator(
        config,
        eventbus,
        FakeLLM(),
        FakeMCP(),
        killswitch,
        skill_learner=learner,
        perception=FakePerception([_blank_perception()]),
    )
    # FakePerception does not initialize the real IO executor; stub shutdown.
    agent.perception.shutdown = lambda: None

    agent._schedule_skill_learning()
    await agent.shutdown()  # drain timeout (45s) is far larger than the 0.2s learn

    assert learner.finished is True


def test_is_ui_tool_classification():
    # Screen-touching servers/tools are classified as UI; everything else is not.
    assert AgentOrchestrator._is_ui_tool("windows__Click", "windows") is True
    assert AgentOrchestrator._is_ui_tool("playwright__browser_click", "playwright") is True
    assert AgentOrchestrator._is_ui_tool("desktop_interact", None) is True
    assert AgentOrchestrator._is_ui_tool("DesktopInteract", None) is True
    assert AgentOrchestrator._is_ui_tool("filesystem__list_directory", "filesystem") is False
    assert AgentOrchestrator._is_ui_tool("quickjs", None) is False


# ---------------------------------------------------------------------------
# Lazy-vision tests
# ---------------------------------------------------------------------------

from PIL import Image as _PILImage

from agent.perception import PerceptionModule as _PerceptionModule


class _SpyDetector:
    def __init__(self, annotations: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self.annotations = annotations or []

    async def annotate(
        self, image: Any, instruction: str
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls += 1
        return list(self.annotations), 0


def _patch_perception_capture(
    module: _PerceptionModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Instance attributes do not bind like methods, so lambdas take no `self`.
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: _PILImage.new("RGB", (100, 100))
    )
    monkeypatch.setattr(module, "_run_ocr", lambda img: "")
    monkeypatch.setattr(
        module, "_generate_annotated", lambda path, ann: _PILImage.new("RGB", (10, 10))
    )


@pytest.mark.asyncio
async def test_pure_filesystem_task_skips_vision(
    config, eventbus, killswitch, monkeypatch
):
    # Lazy mode (default): a filesystem task never runs the detector.
    spy = _SpyDetector()
    perception = _PerceptionModule(config, ui_detector=spy)
    _patch_perception_capture(perception, monkeypatch)

    llm = FakeLLM([
        _message("Listing.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt, b.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt\nb.txt"))

    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch, perception=perception,
    )
    agent.ui_detector = spy  # lazy mode would have constructed it

    result = await agent.run_task("list files")

    assert result == "Files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_desktop_interact_triggers_vision_and_clicks(
    config, eventbus, killswitch, monkeypatch
):
    # DesktopInteract refreshes SoM on demand, then clicks the resolved coords.
    annotations = [
        {"label": 2, "center_x": 0.5, "center_y": 0.5, "score": 0.9, "normalized": True},
    ]
    spy = _SpyDetector(annotations)
    perception = _PerceptionModule(config, ui_detector=spy)
    _patch_perception_capture(perception, monkeypatch)

    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="clicked"))

    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch, perception=perception)
    agent.ui_detector = spy
    agent.current_instruction = "click the OK button"

    result = await agent._desktop_interact_impl(label=2, action="click")

    assert spy.calls == 1
    assert result.startswith("OK: click")
    click_calls = [c for c in mcp.calls if c[0] == "windows" and c[1] == "Click"]
    assert len(click_calls) == 1
    # 0.5 * 100x100 screenshot -> (50, 50)
    assert click_calls[0][2] == {"loc": [50, 50]}


class _DispatchingLLM(FakeLLM):
    """FakeLLM that routes DesktopInteract calls to the orchestrator's real impl.

    The shared FakeLLM returns canned tool results without invoking local
    handlers; the SoM follow-up behaviour lives in the real impl, so dispatch.
    """

    def __init__(
        self, chat_responses: list[Any], agent_holder: dict[str, Any]
    ) -> None:
        super().__init__(chat_responses=chat_responses, tool_names=["DesktopInteract"])
        self._holder = agent_holder

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        results = []
        for call in calls:
            args = json.loads(call.function.arguments)
            output = await self._holder["agent"]._desktop_interact_impl(**args)
            results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
            })
        return results


def _som_followup_agent(
    config: Any,
    eventbus: Any,
    killswitch: Any,
    monkeypatch: pytest.MonkeyPatch,
    annotations: list[dict[str, Any]],
) -> AgentOrchestrator:
    """Wire an agent whose LLM dispatches DesktopInteract to the real impl."""
    spy = _SpyDetector(annotations)
    perception = _PerceptionModule(config, ui_detector=spy)
    _patch_perception_capture(perception, monkeypatch)

    holder: dict[str, Any] = {}
    llm = _DispatchingLLM(
        [
            _message(
                "Clicking.",
                tool_calls=[_tool_call("DesktopInteract", {"label": 1, "action": "click"})],
            ),
            _message("Clicked the OK button."),
        ],
        holder,
    )
    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="clicked"))

    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch, perception=perception)
    holder["agent"] = agent
    agent.ui_detector = spy
    agent.current_instruction = "click the OK button"
    agent.history = [{"role": "system", "content": "sys"}]
    return agent


@pytest.mark.asyncio
async def test_lazy_mode_appends_som_image_after_desktop_interact(
    config, eventbus, killswitch, monkeypatch
):
    """Lazy mode: the SoM-annotated image is appended to history right after
    the DesktopInteract tool result, so the model sees the markers."""
    agent = _som_followup_agent(
        config, eventbus, killswitch, monkeypatch,
        annotations=[
            {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.9, "normalized": True},
        ],
    )

    await agent._think_and_act()

    roles = [m["role"] for m in agent.history]
    assert roles == ["system", "assistant", "tool", "user", "assistant"]
    content = agent.history[3]["content"]
    assert isinstance(content, list)
    assert any(item.get("type") == "image_url" for item in content)
    text = next(item["text"] for item in content if item.get("type") == "text")
    assert "[1]" in text


@pytest.mark.asyncio
async def test_eager_mode_does_not_append_som_followup(
    config, eventbus, killswitch, monkeypatch
):
    """Eager mode already sends the annotated image with every perception, so
    DesktopInteract must not append a duplicate follow-up message."""
    config.ui_detector.lazy = False
    agent = _som_followup_agent(
        config, eventbus, killswitch, monkeypatch,
        annotations=[
            {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.9, "normalized": True},
        ],
    )

    await agent._think_and_act()

    roles = [m["role"] for m in agent.history]
    assert roles == ["system", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_no_som_followup_when_detection_empty(
    config, eventbus, killswitch, monkeypatch
):
    """No annotations -> no follow-up image message."""
    agent = _som_followup_agent(
        config, eventbus, killswitch, monkeypatch, annotations=[],
    )

    await agent._think_and_act()

    roles = [m["role"] for m in agent.history]
    assert roles == ["system", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_eager_mode_runs_vision_each_loop(
    config, eventbus, killswitch, monkeypatch
):
    # lazy=False restores legacy behaviour: vision runs on every perception.
    config.ui_detector.lazy = False
    spy = _SpyDetector()
    perception = _PerceptionModule(config, ui_detector=spy)
    _patch_perception_capture(perception, monkeypatch)

    llm = FakeLLM([
        _message("Listing.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("I listed them."),
        _message("YES"),
        _message("Files: a.txt."),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))

    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch, perception=perception,
    )
    agent.ui_detector = spy

    result = await agent.run_task("list files")

    assert result == "Files: a.txt."
    assert agent.state.current_state == "COMPLETED"
    # Top-of-loop + post-action perceptions in a single one-round task.
    assert spy.calls >= 1


# ---------------------------------------------------------------------------
# preload (warm load + lazy inference) tests
# ---------------------------------------------------------------------------

class _SpyUIDetector:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.load_calls = 0
        self.shutdown_calls = 0

    def load(self) -> None:
        self.load_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.mark.asyncio
async def test_preload_loads_detector_at_startup(
    monkeypatch, config, eventbus, killswitch
):
    # lazy=True + preload=True -> initialize() loads the model (warm), even though
    # perceive() will still skip annotate (lazy inference).
    config.ui_detector.lazy = True
    config.ui_detector.preload = True
    monkeypatch.setattr("ui_detector.UIDetector", _SpyUIDetector)
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.perception.shutdown = lambda: None

    await agent.initialize()
    try:
        assert isinstance(agent.ui_detector, _SpyUIDetector)
        assert agent.ui_detector.load_calls == 1
    finally:
        await agent.shutdown()


def test_preload_default_is_on():
    # Out-of-the-box, lazy mode preloads so the first SoM click never stalls.
    from agent.config import UIDetectorConfig

    assert UIDetectorConfig().lazy is True
    assert UIDetectorConfig().preload is True


@pytest.mark.asyncio
async def test_lazy_preload_off_defers_load_to_first_click(
    monkeypatch, config, eventbus, killswitch
):
    # lazy=True + preload=False (explicit opt-out) -> model is NOT loaded at
    # startup; it loads on the first DesktopInteract instead.
    config.ui_detector.lazy = True
    config.ui_detector.preload = False
    monkeypatch.setattr("ui_detector.UIDetector", _SpyUIDetector)
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.perception.shutdown = lambda: None

    await agent.initialize()
    try:
        assert agent.ui_detector.load_calls == 0
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_eager_loads_at_startup_regardless_of_preload(
    monkeypatch, config, eventbus, killswitch
):
    # lazy=False (eager) loads at startup no matter what preload is set to.
    config.ui_detector.lazy = False
    config.ui_detector.preload = False
    monkeypatch.setattr("ui_detector.UIDetector", _SpyUIDetector)
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.perception.shutdown = lambda: None

    await agent.initialize()
    try:
        assert agent.ui_detector.load_calls == 1
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# Desktop Type/Click (Snapshot->label) + verify failure-gate tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_windows_type_without_label_returns_actionable_error(
    config, eventbus, killswitch
):
    mcp = FakeMCP([{"server": "windows", "name": "Type", "description": "", "schema": {}}])
    mcp.set_result("windows", "Type", ToolResult(success=True, content="typed"))
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)
    agent.set_human_confirmation_callback(lambda summary, action: True)

    # Missing loc/label -> client-side actionable error, never reaches the server.
    results = await agent._execute_tool_calls(
        [_tool_call("windows__Type", {"text": "hi"})]
    )
    assert len(results) == 1
    assert results[0]["content"].startswith("[error]")
    assert "Snapshot" in results[0]["content"]
    assert mcp.calls == []
    assert agent.consecutive_action_failures == 1
    assert agent._round_tool_failed is True

    # With a label, the call is forwarded to the server normally.
    await agent._execute_tool_calls(
        [_tool_call("windows__Type", {"text": "hi", "label": 5})]
    )
    assert any(c[0] == "windows" and c[1] == "Type" for c in mcp.calls)


@pytest.mark.asyncio
async def test_verify_returns_false_when_round_had_tool_failure(
    config, eventbus, killswitch
):
    # Failure gate fires before the LLM is even asked.
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._used_ui_tool = True
    agent._round_tool_failed = True
    prev = _same_hash_perception("before")
    curr = _same_hash_perception("after")  # different hash => has_changed True
    assert await agent._verify(prev, curr) is False

    # Same inputs but no failure this round -> gate is the only variable.
    agent2 = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("YES")]), FakeMCP(), killswitch
    )
    agent2._used_ui_tool = True
    agent2._round_tool_failed = False
    assert await agent2._verify(prev, curr) is True


@pytest.mark.asyncio
async def test_system_prompt_guides_snapshot_before_type(
    config, eventbus, killswitch
):
    llm = FakeLLM([
        _message("done."),
        _message("YES"),
        _message("finished."),
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.run_task("do nothing")

    system_content = agent.history[0]["content"]
    assert "windows__Snapshot" in system_content
    assert "label=<id>" in system_content


# ---------------------------------------------------------------------------
# Loop-limit extension checkpoint
# ---------------------------------------------------------------------------


def _bare_agent(config, eventbus, killswitch, llm):
    return AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )


@pytest.mark.asyncio
async def test_extend_loop_limit_extends_on_yes(config, eventbus, killswitch):
    llm = FakeLLM([_message("YES")])
    agent = _bare_agent(config, eventbus, killswitch, llm)
    agent.history = [{"role": "user", "content": "do the thing"}]

    new_limit = await agent._maybe_extend_loop_limit(10)

    assert new_limit == 20
    # History: checkpoint question, then the YES answer. The confirmation
    # notice is stashed for the next perception message (avoids back-to-back
    # user turns, which Kimi rejects).
    assert agent.history[-1]["content"].upper().startswith("YES")
    assert "more loops" in agent._pending_loop_notice


@pytest.mark.asyncio
async def test_extend_loop_limit_stops_on_no(config, eventbus, killswitch):
    llm = FakeLLM([_message("NO, the approach is wrong")])
    agent = _bare_agent(config, eventbus, killswitch, llm)
    agent.history = [{"role": "user", "content": "do the thing"}]

    assert await agent._maybe_extend_loop_limit(10) == 10


@pytest.mark.asyncio
async def test_extend_loop_limit_caps_at_50(config, eventbus, killswitch):
    llm = FakeLLM([_message("YES")])
    agent = _bare_agent(config, eventbus, killswitch, llm)
    agent.history = [{"role": "user", "content": "do the thing"}]

    # At the hard cap: no LLM call, no extension.
    assert await agent._maybe_extend_loop_limit(50) == 50
    assert llm.calls == []
    # One step below the cap: extension clamps to 50.
    assert await agent._maybe_extend_loop_limit(40) == 50


@pytest.mark.asyncio
async def test_extend_loop_limit_reflection_failure_stops(
    config, eventbus, killswitch
):
    llm = FakeLLM([RuntimeError("api down")])
    agent = _bare_agent(config, eventbus, killswitch, llm)
    agent.history = [{"role": "user", "content": "do the thing"}]

    assert await agent._maybe_extend_loop_limit(10) == 10


@pytest.mark.asyncio
async def test_run_task_extends_limit_then_completes(config, eventbus, killswitch):
    # Loops 1-10 grind without progress (3 chats each: think, verify, reflect);
    # the checkpoint at loop 10 says YES (extend to 20); loop 11 grinds; loop 12
    # verifies YES and produces the final answer. Without the extension the
    # task would have died as STUCK at loop 10.
    grind = _message("Working.")
    llm = FakeLLM(
        [grind] * 30                # loops 1-10
        + [_message("YES")]         # checkpoint: extend
        + [grind] * 3               # loop 11
        + [_message("Working."), _message("YES"), _message("Done!")],  # loop 12
        default_chat=grind,
    )
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 30),
    )

    result = await agent.run_task("finish eventually")

    assert result == "Done!"
    assert agent.state.current_state == "COMPLETED"
    assert len(llm.calls) == 37  # 30 grind + 1 checkpoint + 3 + 3
    assert any(
        "Approach confirmed sound" in str(m.get("content", ""))
        for m in agent.history
    )


# ---------------------------------------------------------------------------
# Task list injection
# ---------------------------------------------------------------------------


class _TaskListLLM(FakeLLM):
    """FakeLLM that routes UpdateTaskList calls to the real registered handler."""

    def __init__(self, chat_responses: list[Any]) -> None:
        super().__init__(chat_responses)
        self._handlers: dict[str, Any] = {}

    def register_local_function(self, name: str, fn: Any, **kwargs: Any) -> None:
        super().register_local_function(name, fn, **kwargs)
        self._handlers[name] = fn

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        import json as _json

        results = []
        for call in calls:
            handler = self._handlers.get(call.function.name)
            if handler is None:
                results.append(
                    {"role": "tool", "tool_call_id": call.id, "content": "{}"}
                )
                continue
            args = _json.loads(call.function.arguments)
            results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": handler(**args),
            })
        return results


@pytest.mark.asyncio
async def test_run_task_injects_task_list_each_loop(config, eventbus, killswitch):
    from agent.task_list import register_task_list

    llm = _TaskListLLM([
        _message("planning", tool_calls=[_tool_call("UpdateTaskList", {"tasks": [
            {"content": "step one", "status": "in_progress"},
            {"content": "step two", "status": "pending"},
        ]})]),
        _message("working."),       # inner think loop ends round 1
        _message("NO"),             # round 1 verify fails
        _message("reflection"),     # round 1 reflect
        _message("done."),          # round 2 think, no tools
        _message("YES"),            # round 2 verify passes
        _message("final answer."),  # final answer -> COMPLETED
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 6),
    )
    register_task_list(llm, agent.task_list)

    result = await agent.run_task("two step task")

    assert result == "final answer."
    # The list created in round 1 was injected as a user message visible to
    # the model in round 2.
    injections = [
        m for m in agent.history
        if m.get("role") == "user" and "Task list:" in str(m.get("content", ""))
    ]
    assert injections, "task list was never injected into the history"
    assert "step two" in str(injections[-1]["content"])


@pytest.mark.asyncio
async def test_run_task_starts_with_cleared_task_list(config, eventbus, killswitch):
    llm = FakeLLM([_message("done."), _message("YES"), _message("finished.")])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 3),
    )
    agent.task_list.update([{"content": "stale", "status": "pending"}])

    await agent.run_task("fresh task")

    # The stale entry must not leak into the new task's injections.
    assert all(
        "stale" not in str(m.get("content", "")) for m in agent.history
    )


@pytest.mark.asyncio
async def test_run_task_nudges_task_list_at_loop_5(config, eventbus, killswitch):
    # Five rounds of grinding with no task list created; the loop-5 perception
    # must carry a one-time nudge to use UpdateTaskList.
    grind = _message("Working.")
    llm = FakeLLM([grind] * 20, default_chat=grind)
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 12),
    )

    await agent.run_task("long grind")

    nudges = [
        m for m in agent.history
        if m.get("role") == "user"
        and "UpdateTaskList" in str(m.get("content", ""))
        and "consider" in str(m.get("content", "")).lower()
    ]
    assert len(nudges) == 1  # exactly once, at loop 5


@pytest.mark.asyncio
async def test_run_task_no_nudge_when_task_list_exists(config, eventbus, killswitch):
    # The model creates a task list in round 1 (via the real registered
    # handler), so the loop-5 nudge must NOT fire.
    from agent.task_list import register_task_list

    llm = _TaskListLLM([
        _message("planning", tool_calls=[_tool_call("UpdateTaskList", {"tasks": [
            {"content": "only step", "status": "in_progress"},
        ]})]),
    ] + [_message("Working.")] * 20)
    llm._default_chat = _message("Working.")
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 12),
    )
    register_task_list(llm, agent.task_list)

    await agent.run_task("planned grind")

    nudges = [
        m for m in agent.history
        if m.get("role") == "user"
        and "consider" in str(m.get("content", "")).lower()
        and "UpdateTaskList" in str(m.get("content", ""))
    ]
    assert nudges == []
