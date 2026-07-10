"""Tests for local memory store."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent.kimi_memory import KimiMemoryClient
from agent.memory import MemoryStore
from tests.fakes import FakeLLM


def test_preference_round_trip(memory_store):
    memory_store.set_preference("theme", "dark")
    assert memory_store.get_preference("theme") == "dark"
    assert memory_store.get_preference("missing", "default") == "default"


def test_audit(memory_store):
    memory_store.audit("read", "test", "noop", "ok")
    with sqlite3.connect(memory_store.db_path) as conn:
        row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert row is not None
    assert row[2] == "read"


def test_reflection_round_trip(memory_store):
    rid = memory_store.add_reflection("summary", "reason", "fix")
    assert rid > 0
    reflections = memory_store.list_reflections()
    assert any(r["id"] == rid for r in reflections)


@pytest.mark.asyncio
async def test_memory_store_prefers_kimi_memory(memory_store, tmp_path):
    llm = FakeLLM(tool_responses=[
        [{"role": "tool", "tool_call_id": "call_memory", "content": "ok"}],
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({"results": [{"key": "user_name", "value": "Alice"}], "count": 1}),
        }],
    ], tool_names=["memory"])
    memory_store.kimi = KimiMemoryClient(llm)

    await memory_store.aset_preference("user_name", "Alice")
    value = await memory_store.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_memory_store_falls_back_to_sqlite_when_kimi_unavailable(memory_store, tmp_path):
    llm = FakeLLM()
    llm.tool_names = lambda: []  # memory tool not registered
    memory_store.kimi = KimiMemoryClient(llm)

    await memory_store.aset_preference("theme", "dark")
    value = await memory_store.aget_preference("theme")

    assert value == "dark"
    assert len(llm.calls) == 0


@pytest.mark.asyncio
async def test_aget_preference_falls_back_to_sqlite_on_semantic_recall_mismatch(memory_store, tmp_path):
    # Kimi recall returns a similar but different key; SQLite holds the exact key.
    llm = FakeLLM(tool_responses=[
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({
                "results": [{"key": "user_name_alias", "value": "Bob"}],
                "count": 1,
            }),
        }],
    ], tool_names=["memory"])
    memory_store.kimi = KimiMemoryClient(llm)
    memory_store.set_preference("user_name", "Alice")

    value = await memory_store.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_aget_preference_returns_kimi_value_on_exact_key_match(memory_store, tmp_path):
    llm = FakeLLM(tool_responses=[
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({
                "results": [{"key": "user_name", "value": "Alice"}],
                "count": 1,
            }),
        }],
    ], tool_names=["memory"])
    memory_store.kimi = KimiMemoryClient(llm)
    memory_store.set_preference("user_name", "SQLite-Value")

    value = await memory_store.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 1


def test_skills_sync_and_search(memory_store, tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "click.md").write_text("Click the element described in the instruction.")
    (skills_dir / "type.md").write_text("Type text into a focused input field.")
    memory_store.sync_skills()
    results = memory_store.search_skills("click element", top_k=1)
    assert len(results) == 1
    assert results[0]["name"] == "click"
