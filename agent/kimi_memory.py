"""Adapter for Kimi's built-in memory and rethink Formula tools.

Uses LLMClient.execute_tool_calls so the same Formula registration/execution
path is reused. If the tools are not registered, all methods raise
ToolNotAvailableError so callers can fall back to local SQLite.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("caelum.kimi_memory")


class ToolNotAvailableError(Exception):
    """Raised when the requested Kimi Formula tool is not registered."""


class KimiMemoryClient:
    """Thin client for moonshot/memory and moonshot/rethink Formula tools."""

    def __init__(
        self,
        llm: Any,
        memory_tool_name: str = "memory",
        rethink_tool_name: str = "rethink",
    ) -> None:
        self.llm = llm
        self.memory_tool_name = memory_tool_name
        self.rethink_tool_name = rethink_tool_name

    def _ensure_available(self, name: str) -> None:
        if name not in self.llm.tool_names():
            raise ToolNotAvailableError(f"Tool {name} is not registered with the LLM client")

    def _make_call(self, name: str, arguments: dict[str, Any]) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=f"call_{name}",
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            ),
        )

    async def set_memory(self, key: str, value: str) -> None:
        self._ensure_available(self.memory_tool_name)
        call = self._make_call(self.memory_tool_name, {
            "action": "save",
            "key": key,
            "value": value,
            "scope": "user",
        })
        outputs = await self.llm.execute_tool_calls([call])
        if outputs and outputs[0]["content"].startswith("[error]"):
            raise RuntimeError(outputs[0]["content"])

    async def get_memory(self, query: str) -> str | None:
        """Recall a memory entry by query string."""
        self._ensure_available(self.memory_tool_name)
        call = self._make_call(self.memory_tool_name, {
            "action": "recall",
            "query": query,
            "scope": "user",
        })
        outputs = await self.llm.execute_tool_calls([call])
        content = outputs[0]["content"] if outputs else "{}"
        if content.startswith("[error]"):
            raise RuntimeError(content)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content or None
        results = parsed.get("results") or []
        if results:
            return results[0].get("value")
        return None

    async def get_memory_exact(self, key: str) -> str | None:
        """Recall a memory entry and return its value only if the key matches exactly."""
        self._ensure_available(self.memory_tool_name)
        call = self._make_call(self.memory_tool_name, {
            "action": "recall",
            "query": key,
            "scope": "user",
        })
        outputs = await self.llm.execute_tool_calls([call])
        content = outputs[0]["content"] if outputs else "{}"
        if content.startswith("[error]"):
            raise RuntimeError(content)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        results = parsed.get("results") or []
        for result in results:
            if result.get("key") == key:
                return result.get("value")
        return None

    async def rethink(
        self,
        task_summary: str,
        failure_reason: str,
        context: list[str] | None = None,
    ) -> str:
        self._ensure_available(self.rethink_tool_name)
        thought_parts = [f"Task: {task_summary}", f"Failure: {failure_reason}"]
        if context:
            thought_parts.append("Context:\n" + "\n".join(context))
        thought = "\n".join(thought_parts)
        call = self._make_call(self.rethink_tool_name, {
            "thought": thought,
            "action": "organize",
        })
        outputs = await self.llm.execute_tool_calls([call])
        content = outputs[0]["content"] if outputs else ""
        if content.startswith("[error]"):
            raise RuntimeError(content)
        return content
