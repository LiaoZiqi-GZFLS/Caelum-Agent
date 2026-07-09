"""Tests for the AutoSkill learning module."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.memory import MemoryStore
from agent.skills import SkillLearner


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    return MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )


@pytest.fixture
def learner(memory: MemoryStore, tmp_path: Path) -> SkillLearner:
    return SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory,
    )


class FakeLLM:
    """Returns a canned skill JSON wrapped in a ChatCompletion-like object."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(
        self, messages: list[dict[str, Any]], tools: Any | None = None
    ) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f"```json\n{__import__('json').dumps(self.payload)}\n```"
                    )
                )
            ]
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
    learner: SkillLearner, memory: MemoryStore, tmp_path: Path
) -> None:
    # Seed an existing skill about Notepad.
    learned_dir = tmp_path / "skills" / "learned"
    learned_dir.mkdir(parents=True)
    original = learned_dir / "open-notepad.md"
    original.write_text("open notepad", encoding="utf-8")
    memory.sync_skills()

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
    memory: MemoryStore, tmp_path: Path
) -> None:
    payload = {
        "name": "launch-calculator",
        "description": "Open the calculator app.",
        "usage": "open calculator",
        "steps": ["Click the calculator icon."],
        "tags": "calculator,math",
        "version": "v0.1.0",
    }
    fake_llm = FakeLLM(payload)
    learner = SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory,
        llm_client=fake_llm,
    )

    result = await learner.learn("open calculator", [])

    assert fake_llm.calls
    assert result["name"] == "learned/launch-calculator"
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "Open the calculator app." in content


@pytest.mark.asyncio
async def test_learn_falls_back_when_llm_fails(
    memory: MemoryStore, tmp_path: Path
) -> None:
    class BrokenLLM:
        async def chat(
            self, messages: list[dict[str, Any]], tools: Any | None = None
        ) -> Any:
            raise RuntimeError("LLM unavailable")

    learner = SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory,
        llm_client=BrokenLLM(),
    )

    result = await learner.learn("open calculator", ["windows/Click: clicked Calculator"])

    assert Path(result["path"]).exists()
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "open-calculator" in content


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
