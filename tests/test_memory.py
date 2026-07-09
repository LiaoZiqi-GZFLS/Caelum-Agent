"""Tests for local memory store."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent.kimi_memory import KimiMemoryClient
from agent.memory import MemoryStore


@pytest.fixture
def memory(tmp_path: Path):
    return MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )


def test_preference_round_trip(memory):
    memory.set_preference("theme", "dark")
    assert memory.get_preference("theme") == "dark"
    assert memory.get_preference("missing", "default") == "default"


def test_audit(memory):
    memory.audit("read", "test", "noop", "ok")
    with sqlite3.connect(memory.db_path) as conn:
        row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert row is not None
    assert row[2] == "read"


def test_reflection_round_trip(memory):
    rid = memory.add_reflection("summary", "reason", "fix")
    assert rid > 0
    reflections = memory.list_reflections()
    assert any(r["id"] == rid for r in reflections)


class FakeLLMForMemory:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[Any]] = []

    def tool_names(self) -> list[str]:
        return ["memory"]

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        self.calls.append(calls)
        if self.responses:
            return self.responses.pop(0)
        return [{"role": "tool", "tool_call_id": calls[0].id, "content": "{}"}]


@pytest.mark.asyncio
async def test_memory_store_prefers_kimi_memory(memory, tmp_path):
    llm = FakeLLMForMemory([
        [{"role": "tool", "tool_call_id": "call_memory", "content": "ok"}],
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({"results": [{"key": "user_name", "value": "Alice"}], "count": 1}),
        }],
    ])
    memory.kimi = KimiMemoryClient(llm)

    await memory.aset_preference("user_name", "Alice")
    value = await memory.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_memory_store_falls_back_to_sqlite_when_kimi_unavailable(memory, tmp_path):
    llm = FakeLLMForMemory()
    llm.tool_names = lambda: []  # memory tool not registered
    memory.kimi = KimiMemoryClient(llm)

    await memory.aset_preference("theme", "dark")
    value = await memory.aget_preference("theme")

    assert value == "dark"
    assert len(llm.calls) == 0


@pytest.mark.asyncio
async def test_aget_preference_falls_back_to_sqlite_on_semantic_recall_mismatch(memory, tmp_path):
    # Kimi recall returns a similar but different key; SQLite holds the exact key.
    llm = FakeLLMForMemory([
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({
                "results": [{"key": "user_name_alias", "value": "Bob"}],
                "count": 1,
            }),
        }],
    ])
    memory.kimi = KimiMemoryClient(llm)
    memory.set_preference("user_name", "Alice")

    value = await memory.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_aget_preference_returns_kimi_value_on_exact_key_match(memory, tmp_path):
    llm = FakeLLMForMemory([
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({
                "results": [{"key": "user_name", "value": "Alice"}],
                "count": 1,
            }),
        }],
    ])
    memory.kimi = KimiMemoryClient(llm)
    memory.set_preference("user_name", "SQLite-Value")

    value = await memory.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 1


def test_skills_sync_and_search(memory, tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "click.md").write_text("Click the element described in the instruction.")
    (skills_dir / "type.md").write_text("Type text into a focused input field.")
    memory.sync_skills()
    results = memory.search_skills("click element", top_k=1)
    assert len(results) == 1
    assert results[0]["name"] == "click"
