"""Reflection engine: capture and retrieve past failures/fixes."""

from __future__ import annotations

import logging
from typing import Any

from agent.config import Config
from agent.kimi_memory import KimiMemoryClient
from agent.memory import MemoryStore


logger = logging.getLogger("caelum.reflection")


class ReflectionEngine:
    def __init__(
        self,
        config: Config,
        memory: MemoryStore,
        kimi: KimiMemoryClient | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.kimi = kimi

    async def record(
        self,
        task_summary: str,
        failure_reason: str | None = None,
        fix_action: str | None = None,
    ) -> int:
        """Record a reflection, optionally routing through Kimi rethink.

        Always persists to SQLite and returns the row id.
        """
        if self.config.reflection.use_rethink and self.kimi is not None:
            try:
                context = [fix_action] if fix_action else None
                fix = await self.kimi.rethink(
                    task_summary,
                    failure_reason or "",
                    context,
                )
                return self.memory.add_reflection(task_summary, failure_reason, fix)
            except Exception as exc:  # pragma: no cover - fallback path
                logger.warning("Kimi rethink failed, falling back to SQLite: %s", exc)
        return self.memory.add_reflection(task_summary, failure_reason, fix_action)

    def record_sync(
        self,
        task_summary: str,
        failure_reason: str | None = None,
        fix_action: str | None = None,
    ) -> int:
        """Synchronous SQLite-only reflection recording."""
        return self.memory.add_reflection(task_summary, failure_reason, fix_action)

    def retrieve(self, query: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
        # The `query` parameter is accepted for API symmetry but filtering by query
        # is not yet implemented; reflections are returned ordered by recency.
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
