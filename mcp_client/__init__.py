"""MCP multi-server client with stdio transport and reconnection."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import threading
import time
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


_NOISE_PATTERNS = [
    re.compile(r"tree_node", re.IGNORECASE),
    re.compile(r"tree_traversal", re.IGNORECASE),
    re.compile(r"Error in processing window", re.IGNORECASE),
    re.compile(r"getting nodes for handle", re.IGNORECASE),
    re.compile(r"Task failed completely for handle", re.IGNORECASE),
    re.compile(r"UI services may be loading", re.IGNORECASE),
]


class _UpstreamNoiseFilter:
    """Line-buffered stderr wrapper that drops known windows-mcp upstream noise.

    windows-mcp prints a burst of ``tree_node`` / ``tree_traversal`` errors to
    its stderr on every Snapshot (an upstream bug, see
    docs/windows_mcp/upstream-tree-node-issue.md). The MCP SDK pipes that
    straight to our ``sys.stderr`` via ``errlog=``, bypassing ``logging``, so a
    ``logging.Filter`` cannot catch it. This stream is installed as the
    ``errlog`` for the windows client: matching lines are counted and dropped,
    everything else is forwarded unchanged, and a periodic summary is logged so
    the user knows the upstream issue is still present.
    """

    SUMMARY_INTERVAL = 60.0  # seconds between "suppressed N lines" summaries

    def __init__(
        self,
        downstream: Any,
        summary_logger: logging.Logger | None = None,
    ) -> None:
        self._downstream = downstream
        self._log = summary_logger or logger
        self._buf = ""
        self._suppressed = 0
        self._last_report = time.monotonic()
        self._lock = threading.Lock()
        # OS pipe backing fileno(); created lazily so unit tests (which drive
        # write()/flush() directly) pay no cost and spawn no thread.
        self._read_fd: int | None = None
        self._write_fd: int | None = None
        self._reader_thread: threading.Thread | None = None

    @staticmethod
    def _is_noise(line: str) -> bool:
        return any(p.search(line) for p in _NOISE_PATTERNS)

    def _maybe_report(self) -> None:
        now = time.monotonic()
        if self._suppressed and (now - self._last_report) >= self.SUMMARY_INTERVAL:
            n = self._suppressed
            self._suppressed = 0
            self._last_report = now
            self._log.info(
                "Suppressed %d windows-mcp upstream noise line(s) "
                "(tree_node traversal bug); see docs/windows_mcp/upstream-tree-node-issue.md",
                n,
            )

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if self._is_noise(line):
                    self._suppressed += 1
                else:
                    self._downstream.write(line + "\n")
            self._maybe_report()
        return len(s)

    def flush(self) -> None:
        # Keep any partial line buffered until the next write completes it, so
        # we never emit half a line (and never mis-judge a partial real error).
        with self._lock:
            self._maybe_report()
        self._downstream.flush()

    def fileno(self) -> int:
        """Return a real OS write fd so this object can be used as ``stderr=``.

        The MCP SDK passes ``errlog`` straight to ``subprocess.Popen(stderr=...)``
        / ``anyio.open_process(stderr=...)``, which requires a real file object
        with a ``fileno()`` (a plain Python stream fails with
        ``'X' object has no attribute 'fileno'``). We create an OS pipe on first
        use, hand the write end to the child, and drain the read end in a daemon
        thread that runs the same line filter as :meth:`write`.
        """
        start_reader = False
        with self._lock:
            if self._write_fd is None:
                r, w = os.pipe()
                self._read_fd = r
                self._write_fd = w
                start_reader = True
        if start_reader:
            t = threading.Thread(
                target=self._reader_loop,
                name="windows-stderr-filter",
                daemon=True,
            )
            self._reader_thread = t
            t.start()
        assert self._write_fd is not None
        return self._write_fd

    def _reader_loop(self) -> None:
        assert self._read_fd is not None
        while True:
            try:
                chunk = os.read(self._read_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            try:
                self.write(text)
            except Exception:
                return

    def close(self) -> None:
        """Release the backing pipe (test/shutdown hygiene); optional in prod."""
        write_fd = self._write_fd
        read_fd = self._read_fd
        self._write_fd = None
        self._read_fd = None
        # Close the write end first so the reader sees EOF and exits.
        if write_fd is not None:
            try:
                os.close(write_fd)
            except OSError:
                pass
        t = self._reader_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        if read_fd is not None:
            try:
                os.close(read_fd)
            except OSError:
                pass


class MCPClient:
    def __init__(
        self,
        name: str,
        config: MCPServerConfig,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        errlog: Any | None = None,
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
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_lock_holder: asyncio.Task | None = None
        # windows-mcp emits known upstream stderr noise (tree_node); install a
        # filtering errlog for it unless the caller supplied their own stream.
        if errlog is not None:
            self._errlog = errlog
        elif name == "windows":
            self._errlog = _UpstreamNoiseFilter(sys.stderr)
        else:
            self._errlog = sys.stderr

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

    async def _acquire_reconnect_lock(self) -> None:
        await self._reconnect_lock.acquire()
        self._reconnect_lock_holder = asyncio.current_task()

    def _release_reconnect_lock(self) -> None:
        self._reconnect_lock_holder = None
        self._reconnect_lock.release()

    async def connect(self) -> bool:
        command = self._resolve_command()
        params = StdioServerParameters(
            command=command,
            args=self.config.args,
            env=self.config.env or None,
        )
        for attempt in range(1, self.max_retries + 1):
            try:
                read, write = await self._stack.enter_async_context(
                    stdio_client(params, errlog=self._errlog)
                )
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
            except asyncio.CancelledError:
                raise
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
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        # Release the upstream-noise filter's pipe/thread if one is installed;
        # fileno() lazily recreates it on the next connect, so this is safe
        # across reconnects.
        if isinstance(self._errlog, _UpstreamNoiseFilter):
            self._errlog.close()

    async def reconnect(self) -> bool:
        acquired_here = False
        if self._reconnect_lock_holder != asyncio.current_task():
            await self._acquire_reconnect_lock()
            acquired_here = True
        try:
            if self._connected and self.session is not None:
                return True
            await self.disconnect()
            return await self.connect()
        finally:
            if acquired_here:
                self._release_reconnect_lock()

    async def ping(self) -> bool:
        if not self.session:
            return False
        try:
            await asyncio.wait_for(self.session.send_ping(), timeout=5.0)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("MCP ping failed: %s", exc, extra={"server": self.name})
            return False

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if not self.session or not await self.ping():
            logger.info("MCP session unhealthy; reconnecting", extra={"server": self.name})
            self._connected = False
            self.session = None
            await self._acquire_reconnect_lock()
            try:
                if self._connected and self.session is not None:
                    ok = True
                else:
                    ok = await self.reconnect()
            finally:
                self._release_reconnect_lock()
            if not ok:
                return ToolResult(
                    success=False,
                    content=f"[error] MCP server {self.name} is not connected",
                    raw=None,
                )
        if self.session is None:
            return ToolResult(
                success=False,
                content=f"[error] MCP server {self.name} is not connected",
                raw=None,
            )
        try:
            resp = await self.session.call_tool(tool_name, arguments=arguments)
        except asyncio.CancelledError:
            raise
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
    def __init__(
        self,
        config: MCPConfig,
        health_interval: float = 30.0,
        health_enabled: bool = True,
    ) -> None:
        self.config = config
        self.health_interval = health_interval
        self.health_enabled = health_enabled
        self.clients: dict[str, MCPClient] = {
            "playwright": MCPClient("playwright", config.playwright),
            "windows": MCPClient("windows", config.windows),
            "filesystem": MCPClient("filesystem", config.filesystem),
        }
        self._health_task: asyncio.Task | None = None

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
        if self.health_enabled:
            self._health_task = asyncio.create_task(self._health_monitor())

    async def disconnect_all(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        await asyncio.gather(
            *(c.disconnect() for c in self.clients.values()),
            return_exceptions=True,
        )

    async def _health_monitor(self, interval: float | None = None) -> None:
        interval = interval if interval is not None else self.health_interval
        try:
            while True:
                await asyncio.sleep(interval)
                for client in self.clients.values():
                    if not client._connected:
                        continue
                    try:
                        ok = await client.ping()
                    except Exception as exc:
                        logger.warning(
                            "MCP health ping for %s raised: %s",
                            client.name,
                            exc,
                            extra={"server": client.name},
                        )
                        ok = False
                    if not ok:
                        logger.warning(
                            "MCP client %s unhealthy; reconnecting",
                            client.name,
                            extra={"server": client.name},
                        )
                        try:
                            reconnected = await client.reconnect()
                        except Exception as exc:
                            logger.error(
                                "MCP client %s reconnect failed: %s",
                                client.name,
                                exc,
                                extra={"server": client.name},
                            )
                        else:
                            if not reconnected:
                                logger.error(
                                    "MCP client %s reconnect failed",
                                    client.name,
                                    extra={"server": client.name},
                                )
        except asyncio.CancelledError:
            raise

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
