"""Tests for ReflectionEngine with optional Kimi rethink."""

from __future__ import annotations

from typing import Any

import pytest

from agent.config import Config
from agent.kimi_memory import KimiMemoryClient
from agent.memory import MemoryStore
from agent.reflection import ReflectionEngine
from tests.fakes import FakeLLM


@pytest.mark.asyncio
async def test_record_uses_rethink_when_available(memory_store):
    llm = FakeLLM(tool_responses=[[{"role": "tool", "tool_call_id": "call_rethink", "content": "Use a different path."}]], tool_names=['rethink'])
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 1
    assert llm.calls[0][0].function.name == "rethink"

    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "list files"
    assert stored[0]["fix_action"] == "Use a different path."


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_when_rethink_unavailable(memory_store):
    llm = FakeLLM()
    llm.tool_names = lambda: []
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 0
    assert len(engine.retrieve()) == 1


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_on_rethink_exception(memory_store):
    class FakeLLMBoom(FakeLLM):
        async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

    llm = FakeLLMBoom()
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "list files"
    assert stored[0]["fix_action"] == "tried ./docs"


def test_build_context_without_reflections(memory_store):
    config = Config(llm={"api_key": "test"}, mcp_servers={})
    engine = ReflectionEngine(config, memory_store)
    assert engine.build_context("anything") == ""


def test_record_sync_persists_without_llm(memory_store):
    llm = FakeLLM()
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = engine.record_sync("sync task", "it broke", "reboot")

    assert rid > 0
    assert len(llm.calls) == 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "sync task"
    assert stored[0]["fix_action"] == "reboot"


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_when_rethink_disabled(memory_store):
    llm = FakeLLM(tool_responses=[[{"role": "tool", "tool_call_id": "call_rethink", "content": "Ignored."}]], tool_names=['rethink'])
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": False},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["fix_action"] == "tried ./docs"


def test_build_context_formats_stored_reflection(memory_store):
    config = Config(llm={"api_key": "test"}, mcp_servers={})
    engine = ReflectionEngine(config, memory_store)
    engine.record_sync("resize window", "window too small", "maximize it")

    context = engine.build_context("resize window")

    assert context.startswith("Past reflections that may help:")
    assert "resize window" in context
    assert "maximize it" in context
