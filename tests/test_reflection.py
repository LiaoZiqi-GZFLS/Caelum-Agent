"""Tests for ReflectionEngine with optional Kimi rethink."""

from __future__ import annotations

from typing import Any

import pytest

from agent.config import Config
from agent.kimi_memory import KimiMemoryClient
from agent.memory import MemoryStore
from agent.reflection import ReflectionEngine


class FakeLLMForRethink:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[Any]] = []

    def tool_names(self) -> list[str]:
        return ["rethink"]

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        self.calls.append(calls)
        if self.responses:
            return self.responses.pop(0)
        return [{"role": "tool", "tool_call_id": calls[0].id, "content": "Retry."}]


@pytest.fixture
def memory(tmp_path):
    return MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )


@pytest.mark.asyncio
async def test_record_uses_rethink_when_available(memory):
    llm = FakeLLMForRethink([[{"role": "tool", "tool_call_id": "call_rethink", "content": "Use a different path."}]])
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 1
    assert llm.calls[0][0].function.name == "rethink"

    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "list files"
    assert stored[0]["fix_action"] == "Use a different path."


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_when_rethink_unavailable(memory):
    llm = FakeLLMForRethink()
    llm.tool_names = lambda: []
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 0
    assert len(engine.retrieve()) == 1


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_on_rethink_exception(memory):
    class FakeLLMBoom(FakeLLMForRethink):
        async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

    llm = FakeLLMBoom()
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "list files"
    assert stored[0]["fix_action"] == "tried ./docs"


def test_build_context_without_reflections(memory):
    config = Config(llm={"api_key": "test"}, mcp_servers={})
    engine = ReflectionEngine(config, memory)
    assert engine.build_context("anything") == ""


def test_record_sync_persists_without_llm(memory):
    llm = FakeLLMForRethink()
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory, kimi=KimiMemoryClient(llm))

    rid = engine.record_sync("sync task", "it broke", "reboot")

    assert rid > 0
    assert len(llm.calls) == 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "sync task"
    assert stored[0]["fix_action"] == "reboot"


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_when_rethink_disabled(memory):
    llm = FakeLLMForRethink([[{"role": "tool", "tool_call_id": "call_rethink", "content": "Ignored."}]])
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": False},
    )
    engine = ReflectionEngine(config, memory, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["fix_action"] == "tried ./docs"


def test_build_context_formats_stored_reflection(memory):
    config = Config(llm={"api_key": "test"}, mcp_servers={})
    engine = ReflectionEngine(config, memory)
    engine.record_sync("resize window", "window too small", "maximize it")

    context = engine.build_context("resize window")

    assert context.startswith("Past reflections that may help:")
    assert "resize window" in context
    assert "maximize it" in context
