"""Kimi (Moonshot) LLM client with Formula and local function tool support."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import httpx
from openai import AsyncOpenAI

from agent.config import LLMConfig

logger = logging.getLogger("caelum.llm")
FunctionHandler = Callable[..., str | Coroutine[Any, Any, str]]


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)
        self.http = httpx.AsyncClient(timeout=120.0)
        self._tools: list[dict[str, Any]] = []
        self._tool_to_uri: dict[str, str] = {}
        self._local_handlers: dict[str, FunctionHandler] = {}

    async def initialize(self) -> None:
        if not self.config.enable_builtin_tools:
            return
        for uri in self.config.builtin_tools:
            if not uri:
                continue
            try:
                tools = await self._fetch_formula_tools(uri)
            except Exception as exc:
                logger.warning("Failed to load formula %s: %s", uri, exc)
                continue
            for tool in tools:
                converted = self._convert_formula_tool(tool)
                if converted is None:
                    logger.warning("Skipping malformed tool from %s: %s", uri, tool)
                    continue
                name = converted["function"]["name"]
                self._tool_to_uri[name] = uri
                self._tools.append(converted)
        logger.info("Loaded %d tool(s): %s", len(self._tools), list(self._tool_to_uri.keys()))

    @staticmethod
    def _convert_formula_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(tool, dict) and "function" in tool:
            return tool
        if isinstance(tool, dict) and "_plugin" in tool:
            plugin = tool["_plugin"]
            functions = plugin.get("functions", [])
            if functions:
                # Use the first function as the primary tool.
                first = functions[0]
                return {
                    "type": "function",
                    "function": {
                        "name": first["name"],
                        "description": first.get("description", plugin.get("description", "")),
                        "parameters": first.get("parameters", {"type": "object"}),
                    },
                }
        return None

    def register_function_tools(self, tools: list[dict[str, Any]]) -> None:
        """Register OpenAI-style function tools (e.g., MCP tools, CodeRunner)."""
        existing = {t["function"]["name"] for t in self._tools}
        for tool in tools:
            name = tool["function"]["name"]
            if name not in existing:
                self._tools.append(tool)
                existing.add(name)

    def register_local_function(
        self,
        name: str,
        handler: FunctionHandler,
        schema: dict[str, Any],
        description: str,
    ) -> None:
        """Register a locally executable function tool."""
        self._local_handlers[name] = handler
        self.register_function_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": schema,
                    },
                }
            ]
        )

    async def close(self) -> None:
        await self.http.aclose()
        await self.client.close()

    async def _fetch_formula_tools(self, uri: str) -> list[dict[str, Any]]:
        url = f"{self.config.base_url}/formulas/{uri}/tools"
        resp = await self.http.get(url, headers={"Authorization": f"Bearer {self.config.api_key}"})
        resp.raise_for_status()
        return resp.json().get("tools", [])

    async def _call_formula(self, uri: str, name: str, arguments: dict[str, Any]) -> str:
        url = f"{self.config.base_url}/formulas/{uri}/fibers"
        resp = await self.http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json={"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        )
        resp.raise_for_status()
        ctx = resp.json().get("context", {})
        return ctx.get("output") or ctx.get("encrypted_output", "")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = ...,  # type: ignore[assignment]
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if tools is ...:
            if self._tools:
                kwargs["tools"] = self._tools
        elif tools:
            kwargs["tools"] = tools
        # tools=None explicitly omits the tools key.
        if self.config.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        if response_format is not None:
            # e.g. {"type": "json_object"} — Kimi JSON Mode. Do not combine
            # with Partial Mode prefills (the API rejects the combination).
            kwargs["response_format"] = response_format
        return await self.client.chat.completions.create(**kwargs)

    async def execute_tool_calls(
        self, tool_calls: list[Any]
    ) -> list[dict[str, Any]]:
        results = []
        for call in tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments)
            uri = self._tool_to_uri.get(name)
            if uri:
                try:
                    output = await self._call_formula(uri, name, args)
                except Exception as exc:
                    output = f"[error] {exc}"
                results.append({"role": "tool", "tool_call_id": call.id, "content": str(output)})
                continue
            handler = self._local_handlers.get(name)
            if handler:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        output = await handler(**args)
                    else:
                        output = handler(**args)
                except Exception as exc:
                    output = f"[error] {exc}"
                results.append({"role": "tool", "tool_call_id": call.id, "content": str(output)})
                continue
            results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": f"[error] Tool {name} is not registered as a Formula or local tool.",
            })
        return results

    def tool_names(self) -> list[str]:
        return list(self._tool_to_uri.keys()) + list(self._local_handlers.keys())