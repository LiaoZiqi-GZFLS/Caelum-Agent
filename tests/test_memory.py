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


def test_skill_embedding_function_pinned_to_cpu(tmp_path: Path, monkeypatch):
    """The ChromaDB skill collection must embed on CPUExecutionProvider.

    After setup.py swaps onnxruntime for onnxruntime-directml, ChromaDB's
    default provider list puts DmlExecutionProvider first, and two
    concurrent DirectML sessions (this embedding model + RapidOCR's) crash
    onnxruntime natively with an access violation (0xc0000005; reproduced by
    scripts/repro_dml_crash.py --dml-embedding). The all-MiniLM-L6-v2 model
    is tiny, so CPU embedding costs nothing.
    """
    captured: dict[str, Any] = {}

    class FakeCollection:
        def upsert(self, **kwargs: Any) -> None: ...
        def query(self, **kwargs: Any) -> dict[str, list]:
            return {"ids": [[]], "documents": [[]]}

    class FakePersistentClient:
        def __init__(self, path: str) -> None: ...
        def get_or_create_collection(self, name, embedding_function=None, **kwargs):
            captured["name"] = name
            captured["embedding_function"] = embedding_function
            return FakeCollection()

    import chromadb

    monkeypatch.setattr(chromadb, "PersistentClient", FakePersistentClient)

    MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )

    ef = captured.get("embedding_function")
    assert ef is not None, "skill collection must pin an embedding function"
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    assert isinstance(ef, ONNXMiniLM_L6_V2)
    assert getattr(ef, "_preferred_providers", None) == ["CPUExecutionProvider"]


def test_skill_collection_recreated_on_legacy_ef_conflict(tmp_path: Path, monkeypatch):
    """Pre-fix collections persist the 'default' (DML-capable) EF; opening
    them with the pinned EF raises an EF-conflict ValueError. The store must
    drop and recreate the collection — sync_skills() repopulates it from
    skills/**/*.md right after, so nothing is lost."""
    calls = {"get": 0, "delete": 0}

    class FakeCollection:
        def upsert(self, **kwargs: Any) -> None: ...
        def query(self, **kwargs: Any) -> dict[str, list]:
            return {"ids": [[]], "documents": [[]]}

    class FakePersistentClient:
        def __init__(self, path: str) -> None: ...
        def get_or_create_collection(self, name, embedding_function=None, **kwargs):
            calls["get"] += 1
            if calls["get"] == 1:
                raise ValueError(
                    "An embedding function already exists in the collection "
                    "configuration, and a new one is provided. Embedding "
                    "function conflict: new: onnx_mini_lm_l6_v2 vs persisted: default"
                )
            return FakeCollection()

        def delete_collection(self, name) -> None:
            calls["delete"] += 1

    import chromadb

    monkeypatch.setattr(chromadb, "PersistentClient", FakePersistentClient)

    MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )

    assert calls["delete"] == 1
    assert calls["get"] == 2


def test_skill_collection_unrelated_value_error_propagates(tmp_path: Path, monkeypatch):
    """A ValueError that is NOT an embedding-function conflict must not be
    swallowed by the migration handler."""

    class FakePersistentClient:
        def __init__(self, path: str) -> None: ...
        def get_or_create_collection(self, name, embedding_function=None, **kwargs):
            raise ValueError("disk on fire")

    import chromadb

    monkeypatch.setattr(chromadb, "PersistentClient", FakePersistentClient)

    with pytest.raises(ValueError, match="disk on fire"):
        MemoryStore(
            db_path=tmp_path / "memory.db",
            skills_dir=tmp_path / "skills",
            vector_dir=tmp_path / "chroma",
        )
