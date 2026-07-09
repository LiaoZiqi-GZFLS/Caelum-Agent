"""MCP multi-server client with stdio transport and reconnection."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if TYPE_CHECKING:
    from agent.config import MCPConfig, MCPServerConfig
else:
    # Runtime import to avoid a circular dependency with agent/__init__.py.
    from agent.config import MCPConfig, MCPServerConfig

logger = logging.getLogger("caelum.mcp")


@dataclass(frozen=True)
class ToolResult:
    success: bool
    content: str
    raw: Any | None = None


class MCPClient:
    def __init__(
        self,
        name: str,
        config: MCPServerConfig,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> None:
        self.name = name
        self.config = config
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []
        self._connected = False
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def _resolve_command(self) -> str:
        if shutil.which(self.config.command):
            return self.config.command
        # Try resolving against the current Python environment's Scripts/bin dir.
        scripts_dir = Path(sys.executable).parent
        candidate = scripts_dir / self.config.command
        if sys.platform == "win32":
            candidate = candidate.with_suffix(".exe")
        if candidate.exists():
            return str(candidate)
        return self.config.command

    async def connect(self) -> bool:
        command = self._resolve_command()
        params = StdioServerParameters(
            command=command,
            args=self.config.args,
            env=self.config.env or None,
        )
        for attempt in range(1, self.max_retries + 1):
            try:
                read, write = await self._stack.enter_async_context(stdio_client(params))
                self.session = await self._stack.enter_async_context(
                    ClientSession(read, write)
                )
                await self.session.initialize()
                tools = await self.session.list_tools()
                self._tools = [
                    {"name": t.name, "description": t.description, "schema": t.inputSchema}
                    for t in tools.tools
                ]
                self._connected = True
                logger.info(
                    "MCP server connected",
                    extra={"server": self.name, "tools": len(self._tools)},
                )
                return True
            except Exception as exc:
                delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
                logger.warning(
                    "MCP connection attempt %d/%d failed: %s (retry in %.1fs)",
                    attempt,
                    self.max_retries,
                    exc,
                    delay,
                    extra={"server": self.name},
                )
                await self.disconnect()
                if attempt < self.max_retries:
                    await asyncio.sleep(delay)
        self._connected = False
        logger.error("MCP server failed to connect", extra={"server": self.name})
        return False

    async def disconnect(self) -> None:
        self.session = None
        self._connected = False
        stack = self._stack
        self._stack = AsyncExitStack()
        try:
            await stack.aclose()
        except Exception:
            pass

    async def reconnect(self) -> bool:
        await self.disconnect()
        return await self.connect()

    async def ping(self) -> bool:
        if not self.session:
            return False
        try:
            await asyncio.wait_for(self.session.send_ping(), timeout=5.0)
            return True
        except Exception as exc:
            logger.debug("MCP ping failed: %s", exc, extra={"server": self.name})
            return False

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if not self.session or not await self.ping():
            logger.info("MCP session unhealthy; reconnecting", extra={"server": self.name})
            ok = await self.reconnect()
            if not ok:
                return ToolResult(
                    success=False,
                    content=f"[error] MCP server {self.name} is not connected",
                    raw=None,
                )
        try:
            resp = await self.session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:
            logger.warning(
                "MCP tool call failed: %s", exc, extra={"server": self.name, "tool": tool_name}
            )
            return ToolResult(success=False, content=f"[error] {exc}", raw=None)
        texts = [c.text for c in resp.content if getattr(c, "text", None)]
        content = "\n".join(texts) if texts else ""
        return ToolResult(success=not resp.isError, content=content, raw=resp)

    def tools(self) -> list[dict[str, Any]]:
        return self._tools


class MCPMultiplexer:
    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self.clients: dict[str, MCPClient] = {
            "playwright": MCPClient("playwright", config.playwright),
            "windows": MCPClient("windows", config.windows),
            "filesystem": MCPClient("filesystem", config.filesystem),
        }

    async def connect_all(self) -> None:
        results = await asyncio.gather(
            *(c.connect() for c in self.clients.values()),
            return_exceptions=True,
        )
        for client, result in zip(self.clients.values(), results):
            if isinstance(result, Exception):
                logger.error(
                    "MCP client %s connect raised exception: %s",
                    client.name,
                    result,
                    extra={"server": client.name},
                )

    async def disconnect_all(self) -> None:
        await asyncio.gather(
            *(c.disconnect() for c in self.clients.values()),
            return_exceptions=True,
        )

    async def call(self, server: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        client = self.clients.get(server)
        if not client:
            raise ValueError(f"Unknown MCP server: {server}")
        return await client.call(tool_name, arguments)

    def all_tools(self) -> list[dict[str, Any]]:
        tools = []
        for name, client in self.clients.items():
            for t in client.tools():
                tools.append({"server": name, **t})
        return tools

    def client(self, server: str) -> MCPClient:
        return self.clients[server]
