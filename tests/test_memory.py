"""Tests for local memory store."""

import sqlite3
from pathlib import Path

import pytest

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


def test_skills_sync_and_search(memory, tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "click.md").write_text("Click the element described in the instruction.")
    (skills_dir / "type.md").write_text("Type text into a focused input field.")
    memory.sync_skills()
    results = memory.search_skills("click element", top_k=1)
    assert len(results) == 1
    assert results[0]["name"] == "click"
