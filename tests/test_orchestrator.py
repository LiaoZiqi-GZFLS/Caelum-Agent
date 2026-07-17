"""Tests for the ReAct orchestrator loop."""

from __future__ import annotations

import asyncio
import json
import logging
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
async def test_system_prompt_escalation_rule_and_selfwindow_policy(config, eventbus, killswitch):
    # Two behavioral gaps prompted this: the model treated PreviewPoints as a
    # forbidden "last resort" and kept retrying failed clicks, and it never hid
    # its own console even when the console covered the target. The system
    # prompt must now state both policies explicitly.
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
    # Failure-escalation rule: two failed attempts -> switch locator strategy.
    assert "ESCALATE ON FAILURE" in system_content
    assert "fails twice in a row" in system_content
    # PreviewPoints is a normal step in the chain, not a forbidden last resort.
    assert "LAST RESORT" not in system_content
    # SelfWindow policy: hide the console proactively before desktop work.
    assert "SelfWindow(action='hide')" in system_content
    assert "cover target apps" in system_content


@pytest.mark.asyncio
async def test_system_prompt_encourages_web_search_when_unsure(config, eventbus, killswitch):
    # When the model knows too little about the target (unfamiliar app/site,
    # a named button it cannot find, an unclear repeated failure) the prompt
    # must push it to search the web BEFORE blind trial-and-error. With the
    # web_search Formula tool registered, the prompt names it directly.
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("x")]), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    llm = _CompletingLLM(
        agent,
        [_message("hi", tool_calls=[_tool_call("CompleteTask", {"answer": "hi"})])],
    )
    _wire_complete_task(agent, llm)
    llm.tools.append("web_search")

    await agent.run_task("hi")

    system_content = agent.history[0]["content"]
    assert "call web_search" in system_content
    assert "blind trial-and-error" in system_content


@pytest.mark.asyncio
async def test_system_prompt_search_fallback_without_web_search_tool(config, eventbus, killswitch):
    # Without the Formula web_search tool the prompt must still push a web
    # search, via Playwright on a search engine — never leave the model with
    # only blind trial-and-error.
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
    assert "call web_search" not in system_content
    assert "blind trial-and-error" in system_content
    assert "search engine" in system_content


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
    # YoloDetector construction is side-effect free (weights load lazily on
    # the first detection), so initialize() needs no detector monkeypatch.
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


# ---------------------------------------------------------------------------
# Interrupted-task learning: queue on kill switch / API breaker, settle on
# the next startup (agent/pending_learning.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_interruption_queues_traces(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("x")]), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.current_instruction = "open notepad"
    agent.action_traces = ["click A", "type B"]

    agent._record_interruption("kill_switch")

    rows = agent.memory.list_pending_learning()
    assert len(rows) == 1
    assert rows[0]["instruction"] == "open notepad"
    assert rows[0]["reason"] == "kill_switch"
    assert rows[0]["traces"] == ["click A", "type B"]


@pytest.mark.asyncio
async def test_record_interruption_skips_empty_traces(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("x")]), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.current_instruction = "task"
    agent.action_traces = []

    agent._record_interruption("api_breaker")

    assert agent.memory.list_pending_learning() == []


@pytest.mark.asyncio
async def test_kill_switch_exit_queues_pending_learning(config, eventbus, killswitch):
    """Kill switch mid-tool: the completed action's trace must be queued."""

    class TriggeringMCP(FakeMCP):
        async def call(self, server, tool_name, arguments):
            if len(self.calls) == 0:
                await killswitch.trigger()
            return await super().call(server, tool_name, arguments)

    llm = FakeLLM([
        _message("Clicking.", tool_calls=[_tool_call("windows__Click", {"loc": [10, 10]})]),
    ])
    mcp = TriggeringMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()] * 5),
    )
    agent.set_human_confirmation_callback(lambda summary, action: True)

    result = await agent.run_task("click the button")

    assert "cancelled" in result.lower()
    rows = agent.memory.list_pending_learning()
    assert len(rows) == 1
    assert rows[0]["instruction"] == "click the button"
    assert rows[0]["reason"] == "kill_switch"
    assert rows[0]["traces"]  # the completed action left a trace


@pytest.mark.asyncio
async def test_api_breaker_exit_queues_pending_learning(config, eventbus, killswitch):
    """One successful action, then 5 consecutive API failures -> breaker."""
    llm = FakeLLM([
        _message("Listing.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        RuntimeError("API down"),
        RuntimeError("API down"),
        RuntimeError("API down"),
        RuntimeError("API down"),
        RuntimeError("API down"),
    ])
    mcp = FakeMCP([{"server": "filesystem", "name": "list_directory", "description": "", "schema": {}}])
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()] * 10),
    )

    result = await agent.run_task("list files")

    assert "api failures" in result.lower()
    rows = agent.memory.list_pending_learning()
    assert len(rows) == 1
    assert rows[0]["instruction"] == "list files"
    assert rows[0]["reason"] == "api_breaker"
    assert rows[0]["traces"]


@pytest.mark.asyncio
async def test_transient_api_failure_logs_visible_retry(config, eventbus, killswitch, caplog):
    """A transient LLM failure must surface on the CLI: one WARNING log per
    retry, showing the current failure count vs. the breaker threshold."""
    llm = FakeLLM([
        asyncio.TimeoutError("request timed out"),
        _message("All done."),
        _message("YES"),
        _message("Task complete."),
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 5),
    )

    with caplog.at_level(logging.WARNING, logger="caelum.orchestrator"):
        result = await agent.run_task("do something")

    assert result == "Task complete."
    retry_logs = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "retrying" in r.message.lower()
    ]
    assert len(retry_logs) == 1, "transient LLM failure should log one visible retry warning"
    assert "1/5" in retry_logs[0].message


@pytest.mark.asyncio
async def test_schedule_pending_settlement_settles_queue(config, eventbus, killswitch):
    """Startup settlement: a queued record is judged and settled in the
    background (success -> skill learner)."""
    llm = FakeLLM([
        _message('{"completed": true, "summary": "done", "lesson": "how"}'),
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
        skill_learner=FakeSkillLearner(),
        reflection=FakeReflection(),
    )
    agent.memory.add_pending_learning("past task", "kill_switch", ["step 1"])

    agent._schedule_pending_settlement()
    await asyncio.gather(*agent._background_tasks)

    assert agent.skill_learner.calls == [("past task", ["step 1"])]
    assert agent.reflection.recorded == []
    assert agent.memory.list_pending_learning() == []


@pytest.mark.asyncio
async def test_run_task_schedules_settlement_once(config, eventbus, killswitch):
    llm = FakeLLM([_message("Done.")], default_chat=_message("Done."))
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()] * 5),
    )
    assert agent._settlement_scheduled is False

    await agent.run_task("anything")
    await asyncio.gather(*agent._background_tasks)

    assert agent._settlement_scheduled is True


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
async def test_desktop_interact_double_click_sets_clicks(config, eventbus, killswitch):
    """desktop_interact double_click sends clicks=2 to Click.

    windows-mcp's Click tool takes `clicks` (0=hover, 1=single, 2=double),
    not `times` — passing `times` fails server-side pydantic validation
    ("unexpected_keyword_argument").
    """
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
    assert args["clicks"] == 2


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
async def test_desktop_interact_requires_label(config, eventbus, killswitch):
    """Label-only mode: calling without label returns a helpful error."""
    mcp = FakeMCP()
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
    )

    result = await agent._desktop_interact_impl(action="click")

    assert result.startswith("[error]")
    assert "label" in result
    assert not mcp.calls


@pytest.mark.asyncio
async def test_desktop_interact_no_annotations_error(config, eventbus, killswitch):
    """No YOLO annotations -> error pointing at the other locator tools."""
    mcp = FakeMCP()
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={"snapshot": "some tree"},
        som_annotations=[],
    )

    result = await agent._desktop_interact_impl(label=1, action="click")

    assert result.startswith("[error]")
    assert "no YOLO annotations" in result
    assert "Snapshot" in result
    assert "PreviewPoints" in result
    assert not mcp.calls


@pytest.mark.asyncio
async def test_desktop_interact_type_focuses_then_types(config, eventbus, killswitch):
    """type action clicks the label's center to focus, then types."""
    mcp = FakeMCP()
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
        screen_width=1920,
        screen_height=1080,
    )

    result = await agent._desktop_interact_impl(label=1, action="type", text="hello")

    assert "OK" in result
    assert len(mcp.calls) == 2
    assert mcp.calls[0][1] == "Click"
    assert mcp.calls[0][2]["loc"] == [960, 540]
    assert mcp.calls[1][1] == "Type"
    assert mcp.calls[1][2]["text"] == "hello"


@pytest.mark.asyncio
async def test_desktop_interact_applies_region_origin(config, eventbus, killswitch):
    """ZoomRegion perceptions carry a native-pixel origin; label centers are
    translated by it (screen = origin + center * region_native_size)."""
    mcp = FakeMCP()
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="region",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
        screen_width=1000,
        screen_height=1000,
        image_origin_x=200,
        image_origin_y=100,
    )

    result = await agent._desktop_interact_impl(label=1, action="click")

    assert "OK" in result
    server, tool, args = mcp.calls[-1]
    assert (server, tool) == ("windows", "Click")
    assert args["loc"] == [700, 600]  # 200 + 0.5*1000, 100 + 0.5*1000


@pytest.mark.asyncio
async def test_rescale_loc_args_applies_region_origin(config, eventbus, killswitch):
    """_rescale_loc_args adds the perception origin after scaling."""
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="region",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
        screen_width=2000,
        screen_height=1000,
        screenshot_width=1000,
        screenshot_height=500,
        image_origin_x=100,
        image_origin_y=50,
    )

    out = agent._rescale_loc_args({"loc": [0.5, 0.5]})

    assert out["loc"] == [1100, 550]  # 100 + 0.5*2000, 50 + 0.5*1000


# ---------------------------------------------------------------------------
# _format_perception tests
# ---------------------------------------------------------------------------


def test_format_perception_sends_both_clean_and_annotated():
    """With YOLO annotations, _format_perception sends TWO images: the clean
    screenshot first, then the annotated copy (Kimi reads both)."""
    import base64
    import tempfile

    tmpdir = Path(tempfile.gettempdir())
    raw = tmpdir / "screenshot_raw_dual_test.jpg"
    annotated = tmpdir / "screenshot_annotated_dual_test.jpg"
    try:
        raw.write_bytes(b"\xff\xd8raw-bytes\xff\xd9")
        annotated.write_bytes(b"\xff\xd8annotated-bytes\xff\xd9")

        p = Perception(
            screenshot_path=raw,
            description="test",
            ocr_text="",
            ui_tree={},
            som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
            annotated_screenshot_path=annotated,
        )
        result = AgentOrchestrator._format_perception(p)

        images = [part for part in result if part.get("type") == "image_url"]
        assert len(images) == 2, "clean + annotated images should both be sent"
        raw_b64 = base64.b64encode(b"\xff\xd8raw-bytes\xff\xd9").decode()
        ann_b64 = base64.b64encode(b"\xff\xd8annotated-bytes\xff\xd9").decode()
        assert raw_b64 in images[0]["image_url"]["url"]
        assert ann_b64 in images[1]["image_url"]["url"]
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

    def detect(self, image: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self.annotations)


def _patch_perception_capture(
    module: _PerceptionModule, monkeypatch: pytest.MonkeyPatch, ocr_text: str = ""
) -> None:
    # Instance attributes do not bind like methods, so lambdas take no `self`.
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: _PILImage.new("RGB", (100, 100))
    )
    monkeypatch.setattr(
        module, "_run_ocr_detailed", lambda img: (ocr_text, [], img.size)
    )
    monkeypatch.setattr(
        module, "_generate_annotated", lambda path, ann: _PILImage.new("RGB", (10, 10))
    )


@pytest.mark.asyncio
async def test_pure_filesystem_task_skips_vision(
    config, eventbus, killswitch, monkeypatch
):
    # No OCR text -> no UIA-less compensation -> a filesystem task never runs
    # the detector.
    spy = _SpyDetector()
    perception = _PerceptionModule(config, detector=spy)
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
    agent.detector = spy  # initialize() would have constructed it

    result = await agent.run_task("list files")

    assert result == "Files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_desktop_interact_uses_last_perception_without_detection(
    config, eventbus, killswitch, monkeypatch
):
    # Label-only DesktopInteract resolves against the LAST perception's SoM
    # annotations; it must NOT run a detection pass of its own.
    spy = _SpyDetector()
    perception = _PerceptionModule(config, detector=spy)
    _patch_perception_capture(perception, monkeypatch)

    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="clicked"))

    agent = AgentOrchestrator(config, eventbus, FakeLLM(), mcp, killswitch, perception=perception)
    agent.detector = spy
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 2, "center_x": 0.5, "center_y": 0.5, "score": 0.9, "normalized": True},
        ],
        screen_width=100,
        screen_height=100,
    )

    result = await agent._desktop_interact_impl(label=2, action="click")

    assert spy.calls == 0  # no detection refresh
    assert result.startswith("OK: click")
    click_calls = [c for c in mcp.calls if c[0] == "windows" and c[1] == "Click"]
    assert len(click_calls) == 1
    # 0.5 * 100x100 screenshot -> (50, 50)
    assert click_calls[0][2] == {"loc": [50, 50]}


@pytest.mark.asyncio
async def test_uia_less_loop_runs_yolo_compensation(
    config, eventbus, killswitch, monkeypatch
):
    # UIA-less screen (empty tree via FakeMCP default + OCR text present):
    # YOLO compensation runs on the loop's perceptions.
    spy = _SpyDetector()
    perception = _PerceptionModule(config, detector=spy)
    _patch_perception_capture(perception, monkeypatch, ocr_text="登录")

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
    agent.detector = spy

    result = await agent.run_task("list files")

    assert result == "Files: a.txt."
    assert agent.state.current_state == "COMPLETED"
    # Top-of-loop + post-action perceptions in a single one-round task.
    assert spy.calls >= 1


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


# ---------------------------------------------------------------------------
# Failure-escalation nudge (two failed UI actions -> change locator strategy)
# ---------------------------------------------------------------------------

_ESCALATION_PHRASE = "Do NOT retry the same call"


def _click_fail_script(fail_rounds: int = 2):
    """LLM script: failing windows__Click rounds, then a successful query that
    verifies and ends the task. Each failing round consumes THREE chats —
    _think_and_act re-chats after tool execution, then _verify short-circuits
    on _round_tool_failed and _reflect() chats once."""
    script = []
    for _ in range(fail_rounds):
        script.append(
            _message("Click.", tool_calls=[_tool_call("windows__Click", {"loc": [10, 10]})])
        )
        script.append(_message("No further action."))  # post-tool follow-up chat
        script.append(_message("Reflecting on the miss."))  # _reflect chat
    script += [
        _message("Listing.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("Listed."),  # post-tool follow-up chat
        _message("YES"),  # _verify chat
        _message("Found a.txt."),  # _final_answer chat
    ]
    return script


def _click_fail_mcp() -> FakeMCP:
    mcp = FakeMCP([
        {"server": "windows", "name": "Click", "description": "", "schema": {}},
        {"server": "filesystem", "name": "list_directory", "description": "", "schema": {}},
    ])
    mcp.set_result("windows", "Click", ToolResult(success=False, content="[error] missed"))
    mcp.set_result("filesystem", "list_directory", ToolResult(success=True, content="a.txt"))
    return mcp


@pytest.mark.asyncio
async def test_run_task_escalation_hint_after_two_ui_failures(config, eventbus, killswitch):
    # After the 2nd consecutive UI-tool failure the next perception must carry
    # an escalation hint pushing the model toward NearbyLabels/ZoomRegion/
    # PreviewPoints instead of retrying the same failing call.
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(_click_fail_script(2), default_chat=_message("Working.")),
        _click_fail_mcp(), killswitch,
        perception=FakePerception([_blank_perception()] * 12),
    )
    config.kill_switch.action_failure_threshold = 5  # survive past 2 failures
    config.kill_switch.same_ui_loop_threshold = 99  # not what this test exercises

    await agent.run_task("click the button")

    hints = [
        m for m in agent.history
        if m.get("role") == "user" and _ESCALATION_PHRASE in str(m.get("content", ""))
    ]
    assert len(hints) == 1


@pytest.mark.asyncio
async def test_run_task_no_escalation_hint_after_single_failure(config, eventbus, killswitch):
    # One failure then a success: no hint — the model only needs pushing after
    # a repeated failure.
    script = [
        _message("Click.", tool_calls=[_tool_call("windows__Click", {"loc": [10, 10]})]),
        _message("No further action."),  # post-tool follow-up chat
        _message("Reflecting on the miss."),  # _reflect chat
        _message("Listing.", tool_calls=[_tool_call("filesystem__list_directory", {"path": "."})]),
        _message("Listed."),  # post-tool follow-up chat
        _message("YES"),
        _message("Found a.txt."),
    ]
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(script, default_chat=_message("Working.")),
        _click_fail_mcp(), killswitch,
        perception=FakePerception([_blank_perception()] * 8),
    )
    config.kill_switch.same_ui_loop_threshold = 99

    await agent.run_task("click the button")

    hints = [
        m for m in agent.history
        if m.get("role") == "user" and _ESCALATION_PHRASE in str(m.get("content", ""))
    ]
    assert hints == []


@pytest.mark.asyncio
async def test_run_task_escalation_hint_once_per_streak(config, eventbus, killswitch):
    # Four consecutive failures: the hint fires once per failure streak, not on
    # every round — nagging the same text burns tokens without adding signal.
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(_click_fail_script(4), default_chat=_message("Working.")),
        _click_fail_mcp(), killswitch,
        perception=FakePerception([_blank_perception()] * 16),
    )
    config.kill_switch.action_failure_threshold = 5
    config.kill_switch.same_ui_loop_threshold = 99

    await agent.run_task("click the button")

    hints = [
        m for m in agent.history
        if m.get("role") == "user" and _ESCALATION_PHRASE in str(m.get("content", ""))
    ]
    assert len(hints) == 1


# ---------------------------------------------------------------------------
# Local tool security gating (CodeRunner -> write_risky)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coderunner_blocked_when_confirmation_denied(
    config, eventbus, killswitch
):
    llm = FakeLLM(tool_names=["CodeRunner"])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.security.confirm_callback = lambda summary, action: False

    results = await agent._execute_tool_calls(
        [_tool_call("CodeRunner", {"code": "print(1)"})]
    )

    assert "[blocked]" in results[0]["content"]
    assert llm.calls == []  # never dispatched to the LLM client


@pytest.mark.asyncio
async def test_coderunner_auto_approved_with_yes(config, eventbus, killswitch):
    llm = FakeLLM(tool_names=["CodeRunner"])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.security.auto_approve = True

    results = await agent._execute_tool_calls(
        [_tool_call("CodeRunner", {"code": "print(1)"})]
    )

    assert "[blocked]" not in results[0]["content"]
    assert llm.calls  # dispatched to the LLM client


@pytest.mark.asyncio
async def test_other_local_tools_stay_ungated(config, eventbus, killswitch):
    llm = FakeLLM(tool_names=["UpdateTaskList"])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.security.confirm_callback = lambda summary, action: False

    results = await agent._execute_tool_calls(
        [_tool_call("UpdateTaskList", {"tasks": []})]
    )

    assert "[blocked]" not in results[0]["content"]


@pytest.mark.asyncio
async def test_audit_log_redacts_sensitive_args(config, eventbus, killswitch):
    mcp = FakeMCP([{"server": "windows", "name": "Type", "description": "", "schema": {}}])
    mcp.set_result("windows", "Type", ToolResult(success=True, content="typed"))
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    audited: list[dict[str, Any]] = []
    agent.memory.audit = lambda **kw: audited.append(kw)

    await agent._execute_tool_calls(
        [_tool_call("windows__Type", {"label": "3", "text": "hunter2"})]
    )

    assert audited
    assert "hunter2" not in audited[0]["action"]
    assert "***" in audited[0]["action"]


def test_redact_args_masks_only_sensitive_keys():
    redacted = AgentOrchestrator._redact_args(
        {"text": "secret", "label": "3", "Password": "pw"}
    )
    assert redacted == {"text": "***", "label": "3", "Password": "***"}


# ---------------------------------------------------------------------------
# State persistence (history is NOT persisted)
# ---------------------------------------------------------------------------


def test_save_state_excludes_history(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch
    )
    agent.history = [{"role": "user", "content": "big base64 blob"}]
    agent.current_instruction = "do things"
    agent.consecutive_action_failures = 2

    agent._save_state()

    import json as _json

    raw = agent.memory.get_state(agent.STATE_KEY)
    payload = _json.loads(raw)
    assert "history" not in payload
    assert payload["current_instruction"] == "do things"
    assert payload["consecutive_action_failures"] == 2


def test_load_state_never_restores_history(config, eventbus, killswitch):
    import json as _json

    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch
    )
    # Simulate a legacy payload that still carries a history field.
    agent.memory.set_state(agent.STATE_KEY, _json.dumps({
        "state": "EXECUTING",
        "current_instruction": "old task",
        "consecutive_action_failures": 1,
        "consecutive_api_failures": 0,
        "history": [{"role": "user", "content": "stale"}],
    }))

    agent._load_state()

    assert agent.history == []
    assert agent.current_instruction == "old task"
    assert agent.consecutive_action_failures == 1


@pytest.mark.asyncio
async def test_run_task_writes_history_archive(config, eventbus, killswitch):
    llm = FakeLLM([_message("done."), _message("YES"), _message("finished.")])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.run_task("archive me")

    archives = list((config.cache_dir_absolute().parent / "archives").glob("*.jsonl"))
    assert len(archives) == 1
    first_line = archives[0].read_text(encoding="utf-8").splitlines()[0]
    import json as _json
    meta = _json.loads(first_line)
    assert meta["instruction"] == "archive me"
    assert meta["outcome"] == "finished."


# ---------------------------------------------------------------------------
# ViewMedia: ms:// media reference injection + task-end remote sweep
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_view_media_result_injected_as_media_message(config, eventbus, killswitch):
    llm = FakeLLM(
        [
            _message("Let me look.", tool_calls=[_tool_call("ViewMedia", {"path": "shot.png"}, call_id="c1")]),
            _message("I see it."),
            _message("YES"),
            _message("done."),
        ],
        tool_responses=[
            [{"role": "tool", "tool_call_id": "c1",
              "content": "[media_ref] image ms://files/abc123\nattached"}],
        ],
        tool_names=["ViewMedia"],
    )
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.run_task("describe the image")

    media_msgs = [
        m for m in agent.history
        if m["role"] == "user" and isinstance(m["content"], list)
        and any(
            isinstance(p, dict) and p.get("type") == "image_url"
            and p.get("image_url", {}).get("url") == "ms://files/abc123"
            for p in m["content"]
        )
    ]
    assert len(media_msgs) == 1


@pytest.mark.asyncio
async def test_run_task_sweeps_remote_uploads_on_exit(config, eventbus, killswitch):
    class _FakeSweeper:
        def __init__(self):
            self.sweeps = 0

        async def sweep_remote(self):
            self.sweeps += 1
            return 0

    llm = FakeLLM([_message("done."), _message("YES"), _message("finished.")])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.file_extractor = _FakeSweeper()
    agent.media_uploader = _FakeSweeper()

    await agent.run_task("sweep after me")
    await asyncio.sleep(0)  # let the fire-and-forget sweep tasks run

    assert agent.file_extractor.sweeps == 1
    assert agent.media_uploader.sweeps == 1


@pytest.mark.asyncio
async def test_stale_label_error_gets_recovery_hint(config, eventbus, killswitch):
    """windows-mcp 'Label N out of range' must tell the model to re-Snapshot."""
    llm = FakeLLM([
        _message("Typing.", tool_calls=[_tool_call("windows__Type", {"text": "微信", "label": 967})]),
        _message("retrying"),
        _message("YES"),
        _message("done."),
    ])
    mcp = FakeMCP([{"server": "windows", "name": "Type", "description": "", "schema": {}}])
    mcp.set_result("windows", "Type", ToolResult(
        success=False,
        content="Error calling tool 'Type': Failed to find element with label 967: "
                "Label 967 out of range",
    ))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.set_human_confirmation_callback(lambda summary, action: True)

    await agent.run_task("type something")

    tool_msgs = [m for m in agent.history if m["role"] == "tool"]
    assert any("967" in m["content"] for m in tool_msgs)
    hint_msg = next(m for m in tool_msgs if "967" in m["content"])
    assert "windows__Snapshot" in hint_msg["content"]
    assert "invalidat" in hint_msg["content"].lower()


@pytest.mark.asyncio
async def test_stale_label_error_appends_fresh_snapshot(config, eventbus, killswitch):
    """On a stale-label error, the agent auto-fetches a fresh Snapshot and
    appends it to the error so the model can retry with valid labels in the
    SAME round instead of spending a round trip on windows__Snapshot.

    Perception calls windows__Snapshot every round, which rebuilds
    windows-mcp's label space — model-held labels from earlier rounds go
    stale even without any UI change."""
    llm = FakeLLM([
        _message("Clicking.", tool_calls=[_tool_call("windows__Click", {"label": 2445})]),
        _message("retrying"),
        _message("YES"),
        _message("done."),
    ])
    mcp = FakeMCP([
        {"server": "windows", "name": "Click", "description": "", "schema": {}},
        {"server": "windows", "name": "Snapshot", "description": "", "schema": {}},
    ])
    mcp.set_result("windows", "Click", ToolResult(
        success=False,
        content="Error calling tool 'Click': Failed to find element with label 2445: "
                "Label 2445 out of range",
    ))
    mcp.set_result("windows", "Snapshot", ToolResult(
        success=True,
        content='["UI Tree:\\ndesktop\\n├── [3] 按钮 \\"确定\\"  [action: click]"]',
    ))
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.set_human_confirmation_callback(lambda summary, action: True)

    await agent.run_task("click something")

    tool_msgs = [m for m in agent.history if m["role"] == "tool"]
    err_msg = next(m for m in tool_msgs if "2445" in m["content"])
    assert "[fresh snapshot" in err_msg["content"]
    assert "[3]" in err_msg["content"], "fresh snapshot labels must be included"


@pytest.mark.asyncio
async def test_local_tool_calls_emit_presenter_events(config, eventbus, killswitch):
    """Local (non-MCP) tools must emit ToolCallRequested/Completed so the
    terminal shows what the agent is doing (e.g. DesktopInteract inference)."""
    from eventbus.events import ToolCallCompleted, ToolCallRequested

    events: list[Any] = []
    eventbus.subscribe("ToolCallRequested", lambda e: events.append(e))
    eventbus.subscribe("ToolCallCompleted", lambda e: events.append(e))

    llm = FakeLLM(
        [
            _message("Detecting.", tool_calls=[_tool_call("ViewMedia", {"path": "x.png"}, call_id="c9")]),
            _message("done looking"),
            _message("YES"),
            _message("finished."),
        ],
        tool_responses=[
            [{"role": "tool", "tool_call_id": "c9", "content": "[media_ref] image ms://zz"}],
        ],
        tool_names=["ViewMedia"],
    )
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.run_task("look at x")

    requested = [e for e in events if isinstance(e, ToolCallRequested)]
    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert any(e.server == "local" and e.tool_name == "ViewMedia" for e in requested)
    assert any(e.server == "local" and e.tool_name == "ViewMedia" for e in completed)


@pytest.mark.asyncio
async def test_system_prompt_prefers_desktop_interact_when_uia_fails(
    config, eventbus, killswitch
):
    """The system prompt must steer the model to DesktopInteract when the UIA
    tree is missing/inaccurate (Qt/Electron/custom-drawn apps, stale labels)."""
    llm = FakeLLM([_message("hi"), _message("YES"), _message("done.")])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.run_task("click something")

    system = agent.history[0]["content"]
    # Explicit fallback triggers from UIA failure to vision interaction.
    assert "Qt" in system or "Electron" in system
    lowered = system.lower()
    assert "snapshot" in lowered
    assert "desktopinteract" in lowered
    # The preference must be stated as a decision rule, not a bare mention:
    # DesktopInteract guidance must appear AFTER (i.e. as the fallback for)
    # the Snapshot guidance.
    assert system.rfind("DesktopInteract") > system.find("windows__Snapshot")


@pytest.mark.asyncio
async def test_upgrade_vision_raises_resolution_and_reperceives(config, eventbus, killswitch):
    """UpgradeVision switches screenshots to the ORIGINAL resolution and
    immediately injects a fresh full-res perception so the model can keep
    working without a wasted round."""
    class _UpgradeLLM(FakeLLM):
        def __init__(self, agent: AgentOrchestrator, responses: list[Any]) -> None:
            super().__init__(responses)
            self._agent = agent

        async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
            out = []
            for c in calls:
                if c.function.name == "UpgradeVision":
                    content = await self._agent._upgrade_vision_impl()
                else:
                    content = "{}"
                out.append({"role": "tool", "tool_call_id": c.id, "content": content})
            return out

    perception = FakePerception([_blank_perception()] * 3)
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch, perception=perception,
    )
    llm = _UpgradeLLM(
        agent,
        [
            _message("Too blurry.", tool_calls=[_tool_call("UpgradeVision", {}, call_id="c7")]),
            _message("now clear"),
            _message("YES"),
            _message("done."),
        ],
    )
    agent.llm = llm
    agent._register_upgrade_vision()

    await agent.run_task("read tiny text")

    assert perception.original_resolution is True
    # A fresh perception was taken right after the upgrade call and injected.
    assert len(perception.calls) >= 2
    upgrade_notes = [
        m for m in agent.history
        if m["role"] == "user" and "original resolution" in str(m.get("content", ""))
    ]
    assert upgrade_notes, "no upgraded-screenshot note was injected"


@pytest.mark.asyncio
async def test_new_task_resets_vision_upgrade(config, eventbus, killswitch):
    llm = FakeLLM([_message("done."), _message("YES"), _message("finished.")])
    perception = FakePerception([_blank_perception()] * 3)
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch, perception=perception,
    )
    perception.original_resolution = True

    await agent.run_task("fresh task")

    assert perception.original_resolution is False


# ---------------------------------------------------------------------------
# SelfWindow guardrails (console auto-restore)
# ---------------------------------------------------------------------------

class _FakeSelfWindow:
    def __init__(self) -> None:
        self.show_calls = 0

    def show(self) -> dict[str, Any]:
        self.show_calls += 1
        return {"state": "normal"}


@pytest.mark.asyncio
async def test_self_window_registered_on_initialize(config, eventbus, killswitch):
    # No need for the real YOLO model here: tool registration only.
    config.yolo.enabled = False
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.initialize()

    assert agent.self_window is not None
    assert "SelfWindow" in agent.llm.tool_names()


@pytest.mark.asyncio
async def test_self_window_restored_on_task_end(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("done.")]), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    fake = _FakeSelfWindow()
    agent.self_window = fake

    await agent.run_task("x")

    assert fake.show_calls >= 1


@pytest.mark.asyncio
async def test_request_human_help_restores_console_first(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    fake = _FakeSelfWindow()
    agent.self_window = fake
    agent.set_human_question_callback(lambda q, o: "ok")
    await agent.state.transition("PLANNING", task_id="t1")
    await agent.state.transition("EXECUTING", task_id="t1")

    await agent._request_human_help_impl("q", ["a", "b"])

    assert fake.show_calls >= 1


# ---------------------------------------------------------------------------
# FocusGuard guardrails (watchdog auto-stop)
# ---------------------------------------------------------------------------

class _FakeFocusGuard:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> str:
        self.stop_calls += 1
        return "[focus_guard] stopped."


@pytest.mark.asyncio
async def test_focus_guard_registered_on_initialize(config, eventbus, killswitch):
    # No need for the real YOLO model here: tool registration only.
    config.yolo.enabled = False
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    await agent.initialize()

    assert agent.focus_guard is not None
    assert "FocusGuard" in agent.llm.tool_names()


@pytest.mark.asyncio
async def test_focus_guard_stopped_on_task_end(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM([_message("done.")]), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    fake = _FakeFocusGuard()
    agent.focus_guard = fake

    await agent.run_task("x")

    assert fake.stop_calls >= 1


# ---------------------------------------------------------------------------
# loc coordinate rescaling (normalized [0,1] -> native screen pixels)
# ---------------------------------------------------------------------------

def _scaled_perception() -> "Perception":
    p = _blank_perception()
    p.screen_width, p.screen_height = 2560, 1440
    p.screenshot_width, p.screenshot_height = 1280, 720
    return p


@pytest.mark.asyncio
async def test_windows_loc_rescaled_to_native_screen(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("click", tool_calls=[_tool_call("windows__Click", {"loc": [0.5, 0.5]})]),
        _message("done"),
        _message("YES"),
        _message("final"),
    ])
    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="clicked"))
    perception = FakePerception([_scaled_perception()] * 3)
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch, perception=perception,
    )
    agent.set_human_confirmation_callback(lambda s, a: True)

    await agent.run_task("click it")

    clicks = [c for c in mcp.calls if c[0] == "windows" and c[1] == "Click"]
    assert clicks, "windows__Click was never dispatched"
    assert clicks[0][2]["loc"] == [1280, 720]


@pytest.mark.asyncio
async def test_windows_loc_passes_through_without_dimensions(config, eventbus, killswitch):
    llm = FakeLLM([
        _message("click", tool_calls=[_tool_call("windows__Click", {"loc": [0.5, 0.5]})]),
        _message("done"),
        _message("YES"),
        _message("final"),
    ])
    mcp = FakeMCP([{"server": "windows", "name": "Click", "description": "", "schema": {}}])
    mcp.set_result("windows", "Click", ToolResult(success=True, content="clicked"))
    # _blank_perception has zero dims -> no rescale possible.
    agent = AgentOrchestrator(
        config, eventbus, llm, mcp, killswitch,
        perception=FakePerception([_blank_perception()] * 3),
    )
    agent.set_human_confirmation_callback(lambda s, a: True)

    await agent.run_task("click it")

    clicks = [c for c in mcp.calls if c[0] == "windows" and c[1] == "Click"]
    assert clicks[0][2]["loc"] == [0.5, 0.5]


# ---------------------------------------------------------------------------
# CaptureWindow view: loc coordinates after CaptureWindow map through the
# window's own screen rect, not the last full-screen perception.
# ---------------------------------------------------------------------------

def _agent_with_capture_view(config, eventbus, killswitch, view):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent._capture_view = view
    return agent


def test_capture_view_rescales_loc_with_window_origin(config, eventbus, killswitch):
    # 1:1 capture: image space == window rect; only the origin translates.
    agent = _agent_with_capture_view(
        config, eventbus, killswitch,
        {"ox": 500, "oy": 300, "w": 600, "h": 400, "iw": 600, "ih": 400},
    )

    out = agent._rescale_loc_args({"loc": [0.1, 0.1]})

    assert out["loc"] == [560, 340]


def test_capture_view_scales_when_image_differs_from_rect(config, eventbus, killswitch):
    agent = _agent_with_capture_view(
        config, eventbus, killswitch,
        {"ox": 0, "oy": 0, "w": 1000, "h": 500, "iw": 500, "ih": 250},
    )

    out = agent._rescale_loc_args({"loc": [0.5, 0.5]})

    assert out["loc"] == [500, 250]


def test_capture_view_takes_precedence_over_last_perception(config, eventbus, killswitch):
    agent = _agent_with_capture_view(
        config, eventbus, killswitch,
        {"ox": 500, "oy": 300, "w": 600, "h": 400, "iw": 600, "ih": 400},
    )
    agent._last_perception = _scaled_perception()

    out = agent._rescale_loc_args({"loc": [0.1, 0.1]})

    assert out["loc"] == [560, 340]  # capture view wins, not 2x screen scaling


def test_set_capture_view_from_rect_and_image_size(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    agent._set_capture_view((10, 20, 300, 200), (60, 40))

    assert agent._capture_view == {
        "ox": 10, "oy": 20, "w": 300, "h": 200, "iw": 60, "ih": 40,
    }


def test_set_last_perception_clears_capture_view(config, eventbus, killswitch):
    agent = _agent_with_capture_view(
        config, eventbus, killswitch,
        {"ox": 500, "oy": 300, "w": 600, "h": 400, "iw": 600, "ih": 400},
    )

    agent._set_last_perception(_blank_perception())

    assert agent._capture_view is None
    assert agent._last_perception is not None


# ---------------------------------------------------------------------------
# PreviewPoints (coordinate preview before raw-coordinate clicking)
# ---------------------------------------------------------------------------

def _perception_with_real_screenshot(tmp_path: Path) -> Perception:
    from PIL import Image as _Image

    shot = tmp_path / "shot.jpg"
    _Image.new("RGB", (200, 200), "white").save(shot, "JPEG")
    return Perception(
        screenshot_path=shot,
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
        screen_width=200,
        screen_height=200,
        screenshot_width=200,
        screenshot_height=200,
    )


def test_desktop_interact_schema_is_label_only():
    from agent.tools import DESKTOP_INTERACT_SCHEMA

    assert "target" not in DESKTOP_INTERACT_SCHEMA["properties"]
    assert "label" in DESKTOP_INTERACT_SCHEMA["properties"]
    assert DESKTOP_INTERACT_SCHEMA["required"] == ["label"]


# ---------------------------------------------------------------------------
# ZoomRegion (region zoom with full re-perception)
# ---------------------------------------------------------------------------


def _region_perception_stub(record: dict[str, Any], tmp_path: Path) -> Any:
    from PIL import Image as _Image

    clean = tmp_path / "region_clean.jpg"
    annotated = tmp_path / "region_annotated.jpg"
    _Image.new("RGB", (60, 60), "white").save(clean, "JPEG")
    _Image.new("RGB", (60, 60), "red").save(annotated, "JPEG")

    async def perceive_region(center_x: int, center_y: int, size: int) -> Perception:
        record["args"] = (center_x, center_y, size)
        return Perception(
            screenshot_path=clean,
            description="region view",
            ocr_text="",
            ui_tree={},
            som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
            screen_width=480,
            screen_height=480,
            screenshot_width=480,
            screenshot_height=480,
            image_origin_x=100,
            image_origin_y=200,
            annotated_screenshot_path=annotated,
        )

    return perceive_region


def _full_perception() -> Perception:
    return Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="full",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 3, "center_x": 0.5, "center_y": 0.4},
            {"label": 7, "center_x": 0.1, "center_y": 0.1},
        ],
        screen_width=1000,
        screen_height=1000,
        screenshot_width=1000,
        screenshot_height=1000,
    )


@pytest.mark.asyncio
async def test_zoom_region_centers_on_label(config, eventbus, killswitch, tmp_path, monkeypatch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _full_perception()
    record: dict[str, Any] = {}
    monkeypatch.setattr(agent.perception, "perceive_region", _region_perception_stub(record, tmp_path))

    result = await agent._zoom_region_impl(size="small", label=3)

    assert result.startswith("[ok]")
    assert record["args"] == (500, 400, 480)  # 0.5*1000, 0.4*1000, small=480
    assert agent._last_perception.image_origin_x == 100  # replaced by region
    assert agent._pending_region is not None


@pytest.mark.asyncio
async def test_zoom_region_centers_on_loc(config, eventbus, killswitch, tmp_path, monkeypatch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    p = _full_perception()
    p.screen_width, p.screen_height = 2000, 1000
    p.screenshot_width, p.screenshot_height = 1000, 500
    agent._last_perception = p
    record: dict[str, Any] = {}
    monkeypatch.setattr(agent.perception, "perceive_region", _region_perception_stub(record, tmp_path))

    result = await agent._zoom_region_impl(size="large", loc=[0.5, 0.5])

    assert result.startswith("[ok]")
    assert record["args"] == (1000, 500, 1680)  # 0.5*2000, 0.5*1000, large=1680


@pytest.mark.asyncio
async def test_zoom_region_requires_exactly_one_center(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _full_perception()

    both = await agent._zoom_region_impl(size="small", label=3, loc=[1, 2])
    neither = await agent._zoom_region_impl(size="small")

    assert both.startswith("[error]")
    assert neither.startswith("[error]")


@pytest.mark.asyncio
async def test_zoom_region_rejects_unknown_size(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _full_perception()

    result = await agent._zoom_region_impl(size="huge", label=3)

    assert result.startswith("[error]")
    assert "huge" in result


@pytest.mark.asyncio
async def test_zoom_region_rejects_unknown_label(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _full_perception()

    result = await agent._zoom_region_impl(size="small", label=99)

    assert result.startswith("[error]")
    assert "99" in result
    assert "3" in result  # available labels listed


@pytest.mark.asyncio
async def test_zoom_region_followup_carries_both_images(
    config, eventbus, killswitch, tmp_path, monkeypatch
):
    """After a ZoomRegion tool call, _think_and_act appends the region's
    clean + annotated images as the post-tool user message."""

    class _ZoomDispatchingLLM(FakeLLM):
        def __init__(self, chat_responses, holder):
            super().__init__(chat_responses=chat_responses, tool_names=["ZoomRegion"])
            self._holder = holder

        async def execute_tool_calls(self, calls):
            results = []
            for call in calls:
                args = json.loads(call.function.arguments)
                output = await self._holder["agent"]._zoom_region_impl(**args)
                results.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": output,
                })
            return results

    holder: dict[str, Any] = {}
    llm = _ZoomDispatchingLLM(
        [
            _message(
                "Zooming.",
                tool_calls=[_tool_call("ZoomRegion", {"size": "small", "label": 3})],
            ),
            _message("Done zooming."),
        ],
        holder,
    )
    agent = AgentOrchestrator(config, eventbus, llm, FakeMCP(), killswitch)
    holder["agent"] = agent
    agent._last_perception = _full_perception()
    record: dict[str, Any] = {}
    monkeypatch.setattr(agent.perception, "perceive_region", _region_perception_stub(record, tmp_path))
    agent.history = [{"role": "system", "content": "sys"}]

    await agent._think_and_act()

    roles = [m["role"] for m in agent.history]
    assert roles == ["system", "assistant", "tool", "user", "assistant"]
    content = agent.history[3]["content"]
    images = [part for part in content if part.get("type") == "image_url"]
    assert len(images) == 2
    texts = [part.get("text", "") for part in content if part.get("type") == "text"]
    assert any("ZoomRegion" in t for t in texts)
    assert agent._pending_region is None  # cleared after append


@pytest.mark.asyncio
async def test_zoom_region_tool_registered(config, eventbus, killswitch):
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, FakeMCP(), killswitch)
    agent._register_zoom_region()
    assert "ZoomRegion" in llm.tool_names()


# ---------------------------------------------------------------------------
# NearbyLabels (find markers near a point)
# ---------------------------------------------------------------------------


def _annotations_perception() -> Perception:
    return Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="full",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 1, "center_x": 0.1, "center_y": 0.1},
            {"label": 2, "center_x": 0.2, "center_y": 0.15},
            {"label": 3, "center_x": 0.9, "center_y": 0.9},
        ],
        screen_width=1000,
        screen_height=1000,
        screenshot_width=1000,
        screenshot_height=1000,
    )


@pytest.mark.asyncio
async def test_nearby_labels_from_loc_sorted_by_distance(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _annotations_perception()

    result = await agent._nearby_labels_impl(loc=[0.11, 0.10])

    assert result.startswith("[nearby labels]")
    lines = [l for l in result.splitlines() if l.strip().startswith("label")]
    assert len(lines) == 3
    assert lines[0].startswith("  label 1")  # distance ~0.01
    assert lines[1].startswith("  label 2")  # distance ~0.103
    assert lines[2].startswith("  label 3")  # far
    assert "(0.1000, 0.1000)" in lines[0]
    assert "DesktopInteract(label=N)" in result


@pytest.mark.asyncio
async def test_nearby_labels_from_label_excludes_self(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _annotations_perception()

    result = await agent._nearby_labels_impl(label=2)

    lines = [l for l in result.splitlines() if l.strip().startswith("label")]
    assert not any(l.strip().startswith("label 2 ") or l.strip() == "label 2" for l in lines)
    assert lines[0].strip().startswith("label 1")  # closest to (200,150)


@pytest.mark.asyncio
async def test_nearby_labels_respects_k(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _annotations_perception()

    result = await agent._nearby_labels_impl(loc=[0.11, 0.10], k=2)

    lines = [l for l in result.splitlines() if l.strip().startswith("label")]
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_nearby_labels_requires_exactly_one_query(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _annotations_perception()

    both = await agent._nearby_labels_impl(label=1, loc=[1, 2])
    neither = await agent._nearby_labels_impl()

    assert both.startswith("[error]")
    assert neither.startswith("[error]")


@pytest.mark.asyncio
async def test_nearby_labels_no_annotations_error(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="t",
        ocr_text="",
        ui_tree={"snapshot": "x"},
        som_annotations=[],
    )

    result = await agent._nearby_labels_impl(loc=[10, 10])

    assert result.startswith("[error]")
    assert "Snapshot" in result
    assert "ZoomRegion" in result
    assert "PreviewPoints" in result


@pytest.mark.asyncio
async def test_nearby_labels_unknown_label_error(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _annotations_perception()

    result = await agent._nearby_labels_impl(label=99)

    assert result.startswith("[error]")
    assert "99" in result


@pytest.mark.asyncio
async def test_nearby_labels_tool_registered(config, eventbus, killswitch):
    llm = FakeLLM()
    agent = AgentOrchestrator(config, eventbus, llm, FakeMCP(), killswitch)
    agent._register_nearby_labels()
    assert "NearbyLabels" in llm.tool_names()


@pytest.mark.asyncio
async def test_preview_points_draws_and_stashes_followup(config, eventbus, killswitch, tmp_path):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _perception_with_real_screenshot(tmp_path)

    result = await agent._preview_points_impl(points=[[0.5, 0.25]])

    assert "marker" in result.lower()
    assert agent._pending_preview is not None
    path, pts = agent._pending_preview
    assert path.exists() and pts == [(0.5, 0.25)]


@pytest.mark.asyncio
async def test_preview_points_replace_semantics(config, eventbus, killswitch, tmp_path):
    from PIL import Image as _Image

    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _perception_with_real_screenshot(tmp_path)

    await agent._preview_points_impl(points=[[0.25, 0.25]])
    await agent._preview_points_impl(points=[[0.75, 0.75]])

    path, pts = agent._pending_preview
    assert pts == [(0.75, 0.75)]
    marked = _Image.open(path)
    # The second call redrew from the clean base: the first point is not red.
    r, g, b = marked.getpixel((50, 50))
    assert not (r > 200 and g < 150 and b < 150)
    r, g, b = marked.getpixel((150, 150))
    assert r > 200 and g < 150 and b < 150


@pytest.mark.asyncio
async def test_preview_points_validates_input(config, eventbus, killswitch, tmp_path):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = _perception_with_real_screenshot(tmp_path)

    assert (await agent._preview_points_impl(points=[])).startswith("[error]")
    assert (await agent._preview_points_impl(points=[[1, 2], [3, 4], [5, 6], [7, 8]])).startswith("[error]")
    assert (await agent._preview_points_impl(points=[["a", "b"]])).startswith("[error]")
    assert agent._pending_preview is None


@pytest.mark.asyncio
async def test_preview_points_without_screenshot(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    agent._last_perception = None

    result = await agent._preview_points_impl(points=[[10, 10]])

    assert result.startswith("[error]")
    assert agent._pending_preview is None


@pytest.mark.asyncio
async def test_preview_points_followup_injected_in_run_task(config, eventbus, killswitch, tmp_path):
    """The annotated preview image is appended to history right after the
    PreviewPoints tool result, so the model can see its markers."""

    class _PreviewLLM(FakeLLM):
        def __init__(self, agent_holder, responses):
            super().__init__(responses)
            self._holder = agent_holder

        async def execute_tool_calls(self, calls):
            out = []
            for c in calls:
                args = json.loads(c.function.arguments)
                content = await self._holder["agent"]._preview_points_impl(**args)
                out.append({"role": "tool", "tool_call_id": c.id, "content": content})
            return out

    perception = FakePerception([_perception_with_real_screenshot(tmp_path)] * 2)
    holder: dict[str, Any] = {}
    llm = _PreviewLLM(holder, [
        _message("preview", tool_calls=[_tool_call("PreviewPoints", {"points": [[0.5, 0.25]]})]),
        _message("Marker 1 is on the button."),
        _message("YES"),
        _message("done"),
    ])
    agent = AgentOrchestrator(config, eventbus, llm, FakeMCP(), killswitch, perception=perception)
    holder["agent"] = agent

    await agent.run_task("click the button by coordinates")

    user_msgs = [m for m in agent.history if m["role"] == "user" and isinstance(m["content"], list)]
    assert any(
        any(part.get("type") == "image_url" for part in m["content"])
        for m in user_msgs
    ), "no annotated preview image was injected into history"


def test_preview_points_schema():
    from agent.tools import PREVIEW_POINTS_SCHEMA

    assert PREVIEW_POINTS_SCHEMA["required"] == ["points"]
    assert PREVIEW_POINTS_SCHEMA["properties"]["points"]["maxItems"] == 3
