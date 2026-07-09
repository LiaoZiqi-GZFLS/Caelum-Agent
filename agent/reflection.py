"""Reflection engine: capture and retrieve past failures/fixes."""

from __future__ import annotations

from typing import Any

from agent.config import Config
from agent.memory import MemoryStore


class ReflectionEngine:
    def __init__(self, config: Config, memory: MemoryStore) -> None:
        self.config = config
        self.memory = memory

    def record(
        self,
        task_summary: str,
        failure_reason: str | None = None,
        fix_action: str | None = None,
    ) -> int:
        return self.memory.add_reflection(task_summary, failure_reason, fix_action)

    def retrieve(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        return self.memory.list_reflections(limit=limit)

    def build_context(self, current_task: str) -> str:
        reflections = self.retrieve(current_task)
        if not reflections:
            return ""
        parts = ["Past reflections that may help:"]
        for r in reflections:
            parts.append(
                f"- {r['task_summary']}"
                + (f" (fix: {r['fix_action']})" if r.get("fix_action") else "")
            )
        return "\n".join(parts)
