"""Tests for the AutoSkill learning module."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.memory import MemoryStore
from agent.security import SecurityGuard
from agent.skills import SkillLearner
from tests.fakes import FakeLLM


@pytest.fixture
def learner(memory_store: MemoryStore, tmp_path: Path) -> SkillLearner:
    return SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory_store,
    )


@pytest.mark.asyncio
async def test_learn_creates_new_skill(learner: SkillLearner, tmp_path: Path) -> None:
    result = await learner.learn(
        "open notepad",
        ["windows/Click: clicked Notepad icon"],
    )

    assert result["merged"] is False
    assert result["version"] == "v0.1.0"
    assert Path(result["path"]).exists()
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "learned/open-notepad" in content or "open notepad" in content.lower()
    assert "v0.1.0" in content


@pytest.mark.asyncio
async def test_learn_merges_similar_skill(
    learner: SkillLearner, memory_store: MemoryStore, tmp_path: Path
) -> None:
    # Seed an existing skill about Notepad.
    learned_dir = tmp_path / "skills" / "learned"
    learned_dir.mkdir(parents=True)
    original = learned_dir / "open-notepad.md"
    original.write_text("open notepad", encoding="utf-8")
    memory_store.sync_skills()

    result = await learner.learn(
        "open notepad",
        ["windows/Click: focused Notepad window"],
    )

    assert result["merged"] is True
    assert result["version"] == "v0.1.1"
    content = (learned_dir / "open-notepad.md").read_text(encoding="utf-8")
    assert "v0.1.1" in content


@pytest.mark.asyncio
async def test_learn_uses_llm_when_available(
    memory_store: MemoryStore, tmp_path: Path
) -> None:
    payload = {
        "name": "launch-calculator",
        "description": "Open the calculator app.",
        "usage": "open calculator",
        "steps": ["Click the calculator icon."],
        "tags": "calculator,math",
        "version": "v0.1.0",
    }
    chat_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    # Partial Mode: the API returns the continuation after the
                    # prefilled "{", without the leading brace.
                    content=json.dumps(payload)[1:]
                )
            )
        ]
    )
    fake_llm = FakeLLM(chat_responses=[chat_response])
    learner = SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory_store,
        llm_client=fake_llm,
    )

    result = await learner.learn("open calculator", [])

    assert fake_llm.calls
    assert result["name"] == "learned/launch-calculator"
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "Open the calculator app." in content


def test_memory_store_writes_audit_file(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit.log"
    memory = MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
        audit_log_path=audit_file,
    )
    memory.audit("read", "test", "noop", "ok")

    assert audit_file.exists()
    content = audit_file.read_text(encoding="utf-8")
    assert "read" in content
    assert "test" in content
    assert "noop" in content


def test_security_typed_confirmation_blocks_on_mismatch(monkeypatch) -> None:
    from agent.config import SecurityConfig

    calls = []

    def callback(summary: str, action: dict) -> bool:
        calls.append((summary, action))
        return True

    config = SecurityConfig(
        destructive_requires_approval=True,
        destructive_requires_typed_confirmation=True,
    )
    guard = SecurityGuard(config, confirm_callback=callback)
    action = {"server": "windows", "tool": "delete", "args": {"path": "x"}}
    monkeypatch.setattr("builtins.input", lambda _: "wrong summary")
    approval = guard.check("destructive", action)

    assert approval.allowed is False
    assert approval.reason == "human-denied"


def test_security_typed_confirmation_allows_on_match(monkeypatch) -> None:
    from agent.config import SecurityConfig

    config = SecurityConfig(
        destructive_requires_approval=True,
        destructive_requires_typed_confirmation=True,
    )
    guard = SecurityGuard(config, confirm_callback=lambda s, a: True)
    action = {"server": "windows", "tool": "delete", "args": {"path": "x"}}
    expected_summary = guard._summarize(action)
    monkeypatch.setattr("builtins.input", lambda _: expected_summary)
    approval = guard.check("destructive", action)

    assert approval.allowed is True
    assert approval.reason == "human-confirmed"


@pytest.mark.asyncio
async def test_learn_falls_back_when_llm_fails(
    memory_store: MemoryStore, tmp_path: Path
) -> None:
    class BrokenLLM:
        async def chat(
            self, messages: list[dict[str, Any]], tools: Any | None = None
        ) -> Any:
            raise RuntimeError("LLM unavailable")

    learner = SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory_store,
        llm_client=BrokenLLM(),
    )

    result = await learner.learn("open calculator", ["windows/Click: clicked Calculator"])

    assert Path(result["path"]).exists()
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "open-calculator" in content


class _PartialRecordingLLM:
    """Records chat() messages and returns a JSON continuation WITHOUT the
    leading brace, mimicking Kimi Partial Mode (the API strips prefilled text)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.messages: list[list[dict[str, Any]]] = []
        self._payload = payload

    async def chat(
        self, messages: list[dict[str, Any]], tools: Any | None = None
    ) -> Any:
        self.messages.append(messages)
        body = json.dumps(self._payload, ensure_ascii=False)[1:]  # drop leading '{'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=body))]
        )


_SKILL_PAYLOAD = {
    "name": "demo-skill",
    "description": "A demo skill.",
    "usage": "run demo",
    "steps": ["Step one.", "Step two."],
    "tags": "demo",
    "version": "v0.1.0",
}


@pytest.mark.asyncio
async def test_generate_with_llm_uses_partial_mode(
    memory_store: MemoryStore, tmp_path: Path
) -> None:
    """Skill generation prefills the assistant turn with '{' (Kimi Partial
    Mode) and parses the brace-less continuation the API returns."""
    llm = _PartialRecordingLLM(_SKILL_PAYLOAD)
    learner = SkillLearner(
        skills_dir=tmp_path / "skills", memory=memory_store, llm_client=llm
    )

    skill = await learner._generate_with_llm("do thing", ["trace step"])

    assert skill["name"] == "learned/demo-skill"
    assert skill["steps"] == ["Step one.", "Step two."]
    last = llm.messages[0][-1]
    assert last["role"] == "assistant"
    assert last["partial"] is True
    assert last["content"] == "{"


@pytest.mark.asyncio
async def test_merge_with_llm_uses_partial_mode(
    memory_store: MemoryStore, tmp_path: Path
) -> None:
    """Skill merging uses the same Partial Mode prefill and parsing."""
    llm = _PartialRecordingLLM({**_SKILL_PAYLOAD, "version": "v0.1.1"})
    learner = SkillLearner(
        skills_dir=tmp_path / "skills", memory=memory_store, llm_client=llm
    )
    existing = dict(_SKILL_PAYLOAD)

    merged = await learner._merge_with_llm(existing, "do thing", ["trace step"])

    assert merged["version"] == "v0.1.1"
    last = llm.messages[0][-1]
    assert last["role"] == "assistant"
    assert last["partial"] is True
    assert last["content"] == "{"


def test_bump_version() -> None:
    assert SkillLearner._bump_version("v1.2.3") == "v1.2.4"
    assert SkillLearner._bump_version("0.1.0") == "v0.1.1"
    assert SkillLearner._bump_version("not-a-version") == "v0.1.1"


def test_slugify() -> None:
    assert SkillLearner._slugify("Open Notepad") == "open-notepad"
    assert SkillLearner._slugify("Click  the  button!!!") == "click-the-button"
    assert SkillLearner._slugify("") == "learned-skill"


def test_parse_skill_round_trip(tmp_path: Path, learner: SkillLearner) -> None:
    skill = {
        "name": "demo-skill",
        "description": "A demo skill.",
        "usage": "run demo",
        "steps": ["Step one.", "Step two."],
        "tags": "demo",
        "version": "v0.2.0",
    }
    path = learner._write_skill(skill)
    parsed = learner._parse_skill(path.read_text(encoding="utf-8"))

    assert parsed["name"] == "demo-skill"
    assert parsed["steps"] == ["Step one.", "Step two."]
    assert parsed["version"] == "v0.2.0"
    assert parsed["tags"] == "demo"
