"""Tests for the Kimi memory/rethink adapter."""

from __future__ import annotations

from typing import Any

import pytest

from agent.kimi_memory import KimiMemoryClient, ToolNotAvailableError
from tests.fakes import FakeLLM


@pytest.mark.asyncio
async def test_set_memory_calls_memory_tool():
    llm = FakeLLM(tool_responses=[[{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]], tool_names=["memory", "rethink"])
    client = KimiMemoryClient(llm)
    await client.set_memory("user_name", "Alice")

    assert len(llm.calls) == 1
    call = llm.calls[0][0]
    assert call.function.name == "memory"
    args = __import__("json").loads(call.function.arguments)
    assert args["action"] == "save"
    assert args["key"] == "user_name"
    assert args["value"] == "Alice"
    assert args["scope"] == "user"


@pytest.mark.asyncio
async def test_get_memory_returns_top_result_value():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": __import__("json").dumps({
            "results": [
                {"key": "user_name", "value": "Alice", "score": 0.95, "scope": "user"},
            ],
            "count": 1,
        }),
    }]], tool_names=["memory", "rethink"])
    client = KimiMemoryClient(llm)
    value = await client.get_memory("user_name")

    assert value == "Alice"
    args = __import__("json").loads(llm.calls[0][0].function.arguments)
    assert args == {"action": "recall", "query": "user_name", "scope": "user"}


@pytest.mark.asyncio
async def test_get_memory_returns_none_when_empty_results():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": __import__("json").dumps({"results": [], "count": 0}),
    }]], tool_names=["memory", "rethink"])
    client = KimiMemoryClient(llm)
    value = await client.get_memory("missing_key")

    assert value is None


@pytest.mark.asyncio
async def test_rethink_returns_reflection():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Try a different directory.",
    }]], tool_names=['memory', 'rethink'])
    client = KimiMemoryClient(llm)
    result = await client.rethink(
        task_summary="list files",
        failure_reason="directory empty",
        context=["tried ./docs"],
    )

    assert result == "Try a different directory."
    call = llm.calls[0][0]
    assert call.function.name == "rethink"
    args = __import__("json").loads(call.function.arguments)
    assert args["action"] == "organize"
    assert "Task: list files" in args["thought"]
    assert "Failure: directory empty" in args["thought"]
    assert "tried ./docs" in args["thought"]


@pytest.mark.asyncio
async def test_set_memory_raises_when_memory_tool_not_registered():
    llm = FakeLLM(tool_names=[])
    client = KimiMemoryClient(llm)

    with pytest.raises(ToolNotAvailableError):
        await client.set_memory("user_name", "Alice")


@pytest.mark.asyncio
async def test_get_memory_raises_when_memory_tool_not_registered():
    llm = FakeLLM(tool_names=[])
    client = KimiMemoryClient(llm)

    with pytest.raises(ToolNotAvailableError):
        await client.get_memory("user_name")


@pytest.mark.asyncio
async def test_rethink_raises_when_rethink_tool_not_registered():
    llm = FakeLLM(tool_names=[])
    client = KimiMemoryClient(llm)

    with pytest.raises(ToolNotAvailableError):
        await client.rethink("task", "failure")


@pytest.mark.asyncio
async def test_set_memory_raises_runtime_error_on_error_output():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "[error] tool execution failed",
    }]], tool_names=['memory', 'rethink'])
    client = KimiMemoryClient(llm)

    with pytest.raises(RuntimeError, match="tool execution failed"):
        await client.set_memory("user_name", "Alice")


@pytest.mark.asyncio
async def test_get_memory_raises_runtime_error_on_error_output():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "[error] recall failed",
    }]], tool_names=['memory', 'rethink'])
    client = KimiMemoryClient(llm)

    with pytest.raises(RuntimeError, match="recall failed"):
        await client.get_memory("user_name")


@pytest.mark.asyncio
async def test_rethink_raises_runtime_error_on_error_output():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "[error] rethink failed",
    }]], tool_names=['memory', 'rethink'])
    client = KimiMemoryClient(llm)

    with pytest.raises(RuntimeError, match="rethink failed"):
        await client.rethink("task", "failure")
