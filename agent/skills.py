"""Skill learning: generate and maintain SKILL.md files from task traces.

The learner follows the v8 AutoSkill flow:

1. Capture a successful task trajectory.
2. Search existing skills by vector similarity.
3. If a similar skill exists (distance <= 1 - threshold), merge/upgrade it.
4. Otherwise create a new SKILL.md under ``skills/learned/``.
5. Sync the updated skill library into the vector store.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agent.memory import MemoryStore


logger = logging.getLogger("caelum.skills")
DEFAULT_SIMILARITY_THRESHOLD = 0.85


class SkillLearner:
    """Generate, merge, and persist SKILL.md skills from successful tasks.

    Args:
        skills_dir: Root directory containing SKILL.md files. Learned skills are
            written to ``<skills_dir>/learned/``.
        memory: MemoryStore used to search existing skills by vector similarity.
        llm_client: Optional LLM client for generating/merging skill content.
        similarity_threshold: Cosine-similarity threshold above which a skill is
            considered similar enough to merge (default 0.85).
    """

    def __init__(
        self,
        skills_dir: Path | str,
        memory: MemoryStore,
        llm_client: Any | None = None,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self.skills_dir = Path(skills_dir)
        self.learned_dir = self.skills_dir / "learned"
        self.memory = memory
        self.llm_client = llm_client
        self.similarity_threshold = similarity_threshold

    def set_learned_dir(self, path: Path | str) -> None:
        self.learned_dir = Path(path)

    async def learn(self, task: str, trajectory: list[str]) -> dict[str, Any]:
        """Learn a skill from a completed task.

        Returns metadata including the skill path, version, and whether it was
        merged with an existing skill.
        """
        existing = self._find_similar(task)
        if existing is not None:
            return await self._merge_skill(existing, task, trajectory)
        return await self._create_skill(task, trajectory)

    def _find_similar(self, task: str) -> dict[str, Any] | None:
        """Return the closest existing skill if it meets the similarity threshold."""
        candidates = self.memory.search_skills(task, top_k=5)
        best: dict[str, Any] | None = None
        best_distance: float | None = None
        for candidate in candidates:
            distance = candidate.get("distance")
            if distance is None:
                continue
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best = candidate
        if best_distance is None:
            return None
        # Cosine similarity = 1 - cosine distance.
        if (1.0 - best_distance) >= self.similarity_threshold:
            return best
        return None

    async def _create_skill(
        self, task: str, trajectory: list[str]
    ) -> dict[str, Any]:
        """Generate a new SKILL.md from the task and trajectory."""
        skill = await self._generate_content(task, trajectory)
        self.learned_dir.mkdir(parents=True, exist_ok=True)
        path = self._write_skill(skill)
        self.memory.sync_skills()
        return {
            "name": skill["name"],
            "version": skill["version"],
            "path": str(path),
            "merged": False,
        }

    async def _merge_skill(
        self,
        existing: dict[str, Any],
        task: str,
        trajectory: list[str],
    ) -> dict[str, Any]:
        """Merge the new trajectory into an existing skill and bump its version."""
        name = existing["name"]
        skill_path = self._skill_path(name)
        existing_content = skill_path.read_text(encoding="utf-8")
        existing_skill = self._parse_skill(existing_content)
        # Ensure the parsed skill keeps its filesystem identity.
        existing_skill["name"] = name

        merged = await self._merge_content(existing_skill, task, trajectory)
        path = self._write_skill(merged)
        self.memory.sync_skills()
        return {
            "name": merged["name"],
            "version": merged["version"],
            "path": str(path),
            "merged": True,
        }

    async def _generate_content(
        self, task: str, trajectory: list[str]
    ) -> dict[str, Any]:
        """Generate structured skill content.

        Uses the LLM if available; otherwise falls back to a deterministic
        template so the agent can still record simple reusable procedures.
        """
        if self.llm_client is not None:
            try:
                return await self._generate_with_llm(task, trajectory)
            except Exception as exc:
                logger.warning("LLM skill generation failed; using fallback: %s", exc)
        return self._fallback_skill(task, trajectory)

    async def _generate_with_llm(
        self, task: str, trajectory: list[str]
    ) -> dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a skill authoring assistant. Convert a successful "
                    "task trace into a concise SKILL.md in JSON. Output only JSON."
                ),
            },
            {
                "role": "user",
                "content": self._skill_prompt(task, trajectory),
            },
        ]
        completion = await self.llm_client.chat(messages, tools=None)
        text = completion.choices[0].message.content or ""
        # Strip markdown fences if the model wraps the JSON.
        text = text.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1]).strip()
        data = json.loads(text)
        return self._normalize_skill(data)

    async def _merge_content(
        self,
        existing: dict[str, Any],
        task: str,
        trajectory: list[str],
    ) -> dict[str, Any]:
        """Combine an existing skill with a new task instance."""
        if self.llm_client is not None:
            try:
                return await self._merge_with_llm(existing, task, trajectory)
            except Exception as exc:
                logger.warning("LLM skill merge failed; using fallback: %s", exc)
        return self._fallback_merge(existing, task, trajectory)

    async def _merge_with_llm(
        self,
        existing: dict[str, Any],
        task: str,
        trajectory: list[str],
    ) -> dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": (
                    "Merge an existing SKILL.md with a new successful task trace. "
                    "Preserve the best steps, remove duplicates, and bump the patch "
                    "version. Output only JSON matching the skill schema."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Existing skill:\n{self._render_skill(existing)}\n\n"
                    f"New task: {task}\n"
                    f"New trace:\n" + "\n".join(f"- {s}" for s in trajectory)
                ),
            },
        ]
        completion = await self.llm_client.chat(messages, tools=None)
        text = completion.choices[0].message.content or ""
        text = text.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1]).strip()
        data = json.loads(text)
        return self._normalize_skill(data)

    def _fallback_skill(self, task: str, trajectory: list[str]) -> dict[str, Any]:
        name = self._slugify(task)
        steps = [self._step_from_trace(t) for t in trajectory if t.strip()]
        if not steps:
            steps = ["Identify the target element or window.", "Perform the requested action.", "Verify the outcome."]
        return {
            "name": f"learned/{name}",
            "description": f"Auto-learned skill for: {task}",
            "usage": task,
            "steps": steps,
            "tags": "auto, learned",
            "version": "v0.1.0",
        }

    def _fallback_merge(
        self,
        existing: dict[str, Any],
        task: str,
        trajectory: list[str],
    ) -> dict[str, Any]:
        merged = dict(existing)
        merged["description"] = existing.get("description", "") + f"\nAlso covers: {task}."
        existing_steps = existing.get("steps", [])
        new_steps = [self._step_from_trace(t) for t in trajectory if t.strip()]
        for step in new_steps:
            if step not in existing_steps:
                existing_steps.append(step)
        merged["steps"] = existing_steps
        merged["version"] = self._bump_version(existing.get("version", "v0.1.0"))
        return merged

    @staticmethod
    def _skill_prompt(task: str, trajectory: list[str]) -> str:
        trace_text = "\n".join(f"- {t}" for t in trajectory) or "- No action trace recorded."
        return (
            f"Task: {task}\n"
            f"Successful trace:\n{trace_text}\n\n"
            "Return JSON with keys: name (kebab-case), description, usage (example prompt), "
            "steps (list of strings), tags (comma-separated string), version (default v0.1.0)."
        )

    @staticmethod
    def _step_from_trace(trace: str) -> str:
        """Convert a terse action summary into a human-readable step."""
        # Remove common prefixes like "ok" or "done" and strip brackets.
        step = re.sub(r"^\[[^\]]+\]\s*", "", trace)
        step = step.strip(" .")
        if not step:
            return trace
        return step[0].upper() + step[1:]

    def _write_skill(self, skill: dict[str, Any]) -> Path:
        path = self._skill_path(skill["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render_skill(skill), encoding="utf-8")
        return path

    def _skill_path(self, name: str) -> Path:
        return self.skills_dir / f"{name}.md"

    @staticmethod
    def _render_skill(skill: dict[str, Any]) -> str:
        lines = [
            f"# Skill: {skill['name']}",
            "",
            "## Description",
            "",
            skill.get("description", ""),
            "",
            "## Usage",
            "",
            "```",
            skill.get("usage", ""),
            "```",
            "",
            "## Steps",
            "",
        ]
        for idx, step in enumerate(skill.get("steps", []), start=1):
            lines.append(f"{idx}. {step}")
        lines.extend([
            "",
            "## Tags",
            "",
            skill.get("tags", ""),
            "",
            "## Version",
            "",
            skill.get("version", "v0.1.0"),
            "",
        ])
        return "\n".join(lines)

    @staticmethod
    def _parse_skill(content: str) -> dict[str, Any]:
        """Parse a minimal SKILL.md back into a dict for merging."""
        skill: dict[str, Any] = {
            "name": "learned-skill",
            "description": "",
            "usage": "",
            "steps": [],
            "tags": "",
            "version": "v0.1.0",
        }
        lines = content.splitlines()
        section: str | None = None
        buffer: list[str] = []

        def flush() -> None:
            if section is None:
                return
            text = "\n".join(buffer).strip()
            if section == "steps":
                skill["steps"] = [
                    re.sub(r"^\d+\.\s*", "", line).strip()
                    for line in text.splitlines()
                    if line.strip()
                ]
            else:
                skill[section] = text
            buffer.clear()

        for line in lines:
            if line.startswith("# Skill:"):
                skill["name"] = line[len("# Skill:"):].strip()
                continue
            if line.startswith("## "):
                flush()
                section = line[3:].strip().lower()
                continue
            buffer.append(line)
        flush()
        return skill

    @staticmethod
    def _normalize_skill(data: dict[str, Any]) -> dict[str, Any]:
        steps = data.get("steps", [])
        if isinstance(steps, str):
            steps = [s.strip() for s in steps.splitlines() if s.strip()]
        name = data.get("name", "learned-skill")
        if "/" not in name:
            name = f"learned/{SkillLearner._slugify(name)}"
        return {
            "name": name,
            "description": data.get("description", ""),
            "usage": data.get("usage", ""),
            "steps": steps,
            "tags": data.get("tags", ""),
            "version": data.get("version", "v0.1.0"),
        }

    @staticmethod
    def _slugify(text: str) -> str:
        """Create a filesystem-safe kebab-case slug."""
        text = re.sub(r"[^\w\s-]", "", text).strip().lower()
        text = re.sub(r"[-\s]+", "-", text)
        return text[:64] or "learned-skill"

    @staticmethod
    def _bump_version(version: str) -> str:
        """Bump the patch component of a vX.Y.Z version string."""
        match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
        if not match:
            return "v0.1.1"
        major, minor, patch = match.groups()
        return f"v{major}.{minor}.{int(patch) + 1}"
