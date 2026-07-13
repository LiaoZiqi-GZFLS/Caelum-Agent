"""Tests for agent.pending_learning — startup settlement of interrupted tasks.

Uses the real MemoryStore (conftest memory_store fixture) with scripted fake
LLM / skill learner / reflection engine.
"""

from __future__ import annotations

import json

import pytest

from agent.pending_learning import LearningSettler
from tests.fakes import FakeLLM, FakeReflection, FakeSkillLearner, _message


def _verdict(completed: bool, summary: str = "s", lesson: str = "l") -> str:
    return json.dumps({"completed": completed, "summary": summary, "lesson": lesson})


def _settler(memory_store, llm):
    return LearningSettler(
        memory=memory_store,
        llm=llm,
        skill_learner=FakeSkillLearner(),
        reflection=FakeReflection(),
    )


@pytest.mark.asyncio
async def test_completed_verdict_learns_skill_and_deletes(memory_store):
    memory_store.add_pending_learning("open notepad", "kill_switch", ["click A", "type B"])
    llm = FakeLLM(chat_responses=[_message(_verdict(True, "notepad opened", "use UIA"))])
    settler = _settler(memory_store, llm)

    settled = await settler.settle_all()

    assert settled == 1
    assert settler.skill_learner.calls == [("open notepad", ["click A", "type B"])]
    assert settler.reflection.recorded == []
    assert memory_store.list_pending_learning() == []


@pytest.mark.asyncio
async def test_incomplete_verdict_records_reflection_and_deletes(memory_store):
    memory_store.add_pending_learning("book flight", "api_breaker", ["search"])
    llm = FakeLLM(chat_responses=[_message(_verdict(False, "only searched", "retry booking"))])
    settler = _settler(memory_store, llm)

    settled = await settler.settle_all()

    assert settled == 1
    assert settler.skill_learner.calls == []
    assert len(settler.reflection.recorded) == 1
    rec = settler.reflection.recorded[0]
    assert rec["task_summary"] == "book flight"
    assert "api_breaker" in rec["failure_reason"]
    assert "only searched" in rec["failure_reason"]
    assert rec["fix_action"] == "retry booking"
    assert memory_store.list_pending_learning() == []


@pytest.mark.asyncio
async def test_judge_failure_bumps_attempts_and_keeps_row(memory_store):
    memory_store.add_pending_learning("task", "kill_switch", ["a"])
    llm = FakeLLM(chat_responses=[RuntimeError("API down")])
    settler = _settler(memory_store, llm)

    settled = await settler.settle_all()

    assert settled == 0
    rows = memory_store.list_pending_learning()
    assert len(rows) == 1
    assert rows[0]["attempts"] == 1
    assert settler.skill_learner.calls == []
    assert settler.reflection.recorded == []


@pytest.mark.asyncio
async def test_attempts_exhausted_falls_back_to_reflection_and_deletes(memory_store):
    rid = memory_store.add_pending_learning("task", "api_breaker", ["a", "b"])
    memory_store.bump_pending_learning_attempts(rid)
    memory_store.bump_pending_learning_attempts(rid)  # already 2 failed attempts
    llm = FakeLLM(chat_responses=[RuntimeError("API down")])
    settler = _settler(memory_store, llm)

    settled = await settler.settle_all()

    assert settled == 1
    assert settler.skill_learner.calls == []
    assert len(settler.reflection.recorded) == 1
    rec = settler.reflection.recorded[0]
    assert rec["task_summary"] == "task"
    assert "api_breaker" in rec["failure_reason"]
    assert memory_store.list_pending_learning() == []


@pytest.mark.asyncio
async def test_malformed_verdict_counts_as_judge_failure(memory_store):
    memory_store.add_pending_learning("task", "kill_switch", ["a"])
    llm = FakeLLM(chat_responses=[_message("I think it went pretty well overall")])
    settler = _settler(memory_store, llm)

    settled = await settler.settle_all()

    assert settled == 0
    rows = memory_store.list_pending_learning()
    assert rows[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_empty_queue_is_noop(memory_store):
    llm = FakeLLM()
    settler = _settler(memory_store, llm)

    assert await settler.settle_all() == 0
    assert llm.calls == []


@pytest.mark.asyncio
async def test_multiple_rows_settled_independently(memory_store):
    memory_store.add_pending_learning("done task", "kill_switch", ["x"])
    memory_store.add_pending_learning("half task", "api_breaker", ["y"])
    llm = FakeLLM(chat_responses=[
        _message(_verdict(True, "finished", "skill")),
        _message(_verdict(False, "halfway", "lesson")),
    ])
    settler = _settler(memory_store, llm)

    settled = await settler.settle_all()

    assert settled == 2
    assert settler.skill_learner.calls == [("done task", ["x"])]
    assert [r["task_summary"] for r in settler.reflection.recorded] == ["half task"]
    assert memory_store.list_pending_learning() == []
