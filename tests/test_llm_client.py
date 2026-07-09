"""Tests for LLM client local function tool execution."""

from types import SimpleNamespace
from typing import Any

import pytest

from agent.config import LLMConfig
from agent.llm_client import LLMClient


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=__import__("json").dumps(args)),
    )


@pytest.fixture
def llm():
    return LLMClient(LLMConfig(api_key="test", enable_builtin_tools=False))


@pytest.mark.asyncio
async def test_local_function_execution(llm):
    llm.register_local_function(
        "Greet",
        lambda name: f"Hello, {name}!",
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
        description="Greet someone.",
    )
    calls = [_tool_call("Greet", {"name": "Alice"})]
    results = await llm.execute_tool_calls(calls)
    assert len(results) == 1
    assert results[0]["content"] == "Hello, Alice!"
    assert results[0]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(llm):
    calls = [_tool_call("MissingTool", {})]
    results = await llm.execute_tool_calls(calls)
    assert results[0]["content"].startswith("[error]")
