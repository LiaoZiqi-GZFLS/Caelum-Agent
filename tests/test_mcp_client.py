"""Tests for MCP client data structures and tool mapping."""

import asyncio
import io
import logging
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.config import MCPConfig, MCPServerConfig
from agent.tools import build_mcp_tools
from mcp_client import MCPClient, MCPMultiplexer, ToolResult, _UpstreamNoiseFilter


def test_tool_result_dataclass():
    r = ToolResult(success=True, content="ok")
    assert r.success
    assert r.content == "ok"


def test_mcp_client_creation():
    cfg = MCPServerConfig(command="echo", args=["hello"])
    client = MCPClient("test", cfg)
    assert client.name == "test"


def test_build_mcp_tools():
    config = {
        "playwright": MCPServerConfig(command="npx", args=["-y", "@playwright/mcp@latest"]),
        "windows": MCPServerConfig(command="windows-mcp", args=["serve"]),
        "filesystem": MCPServerConfig(command="npx", args=["-y", "filesystem-mcp", "."]),
    }

    mcp = MCPMultiplexer(MCPConfig(**config))
    mcp.clients["playwright"]._tools = [
        {"name": "browser_navigate", "description": "navigate", "schema": {"type": "object"}}
    ]
    tools = build_mcp_tools(mcp)
    assert any(t["function"]["name"] == "playwright__browser_navigate" for t in tools)


@pytest.mark.asyncio
async def test_health_monitor_reconnects_on_failure():
    client = MCPClient("test", {"command": "echo", "args": [], "env": {}})
    client._connected = True
    client.session = MagicMock()
    client.ping = AsyncMock(side_effect=[True, False, True])
    client.reconnect = AsyncMock(return_value=True)

    multiplexer = MCPMultiplexer.__new__(MCPMultiplexer)
    multiplexer.clients = {"test": client}
    multiplexer.health_interval = 0.05
    multiplexer._health_task = None

    monitor = asyncio.create_task(multiplexer._health_monitor())
    await asyncio.sleep(0.15)
    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass

    client.reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_all_cancels_health_monitor():
    config = MCPConfig(
        playwright=MCPServerConfig(command="echo", args=[]),
        windows=MCPServerConfig(command="echo", args=[]),
        filesystem=MCPServerConfig(command="echo", args=[]),
    )
    multiplexer = MCPMultiplexer(config, health_interval=0.05)
    multiplexer._health_task = asyncio.create_task(multiplexer._health_monitor())
    await asyncio.sleep(0.01)
    await multiplexer.disconnect_all()
    assert multiplexer._health_task is None or multiplexer._health_task.done()


@pytest.mark.asyncio
async def test_disconnect_all_cancels_health_monitor_during_reconnect():
    config = MCPConfig(
        playwright=MCPServerConfig(command="echo", args=[]),
        windows=MCPServerConfig(command="echo", args=[]),
        filesystem=MCPServerConfig(command="echo", args=[]),
    )
    multiplexer = MCPMultiplexer(config, health_interval=0.05)
    client = multiplexer.clients["playwright"]
    client._connected = True
    client.session = MagicMock()
    client.ping = AsyncMock(return_value=False)

    reconnect_started = asyncio.Event()
    reconnect_continue = asyncio.Event()

    async def blocking_reconnect():
        reconnect_started.set()
        await reconnect_continue.wait()
        return True

    client.reconnect = AsyncMock(side_effect=blocking_reconnect)

    multiplexer._health_task = asyncio.create_task(multiplexer._health_monitor())
    await reconnect_started.wait()

    await asyncio.wait_for(multiplexer.disconnect_all(), timeout=1.0)
    assert multiplexer._health_task is None


@pytest.mark.asyncio
async def test_concurrent_calls_trigger_single_reconnect():
    cfg = MCPServerConfig(command="echo", args=["hello"])
    client = MCPClient("test", cfg)
    client._connected = True
    client.session = MagicMock()
    client.ping = AsyncMock(return_value=False)

    reconnect_event = asyncio.Event()

    async def slow_reconnect():
        reconnect_event.set()
        await asyncio.sleep(0.1)
        client._connected = True
        client.session = MagicMock()
        return True

    client.reconnect = AsyncMock(side_effect=slow_reconnect)

    call1 = asyncio.create_task(client.call("foo", {}))
    call2 = asyncio.create_task(client.call("bar", {}))

    await reconnect_event.wait()
    await asyncio.gather(call1, call2)

    client.reconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# _UpstreamNoiseFilter tests
# ---------------------------------------------------------------------------

def _make_filter() -> tuple[_UpstreamNoiseFilter, io.StringIO]:
    downstream = io.StringIO()
    filt = _UpstreamNoiseFilter(downstream)
    return filt, downstream


def test_upstream_noise_filter_suppresses_known_lines():
    filt, downstream = _make_filter()
    filt.write("Error in tree_traversal: cannot access local variable 'tree_node'\n")
    filt.write("some real error\n")
    filt.flush()

    assert downstream.getvalue() == "some real error\n"
    assert filt._suppressed == 1


def test_upstream_noise_filter_passes_real_errors():
    filt, downstream = _make_filter()
    filt.write("ValueError: bad arg\n")
    filt.flush()

    assert downstream.getvalue() == "ValueError: bad arg\n"
    assert filt._suppressed == 0


def test_upstream_noise_filter_handles_partial_lines():
    filt, downstream = _make_filter()
    filt.write("Error in tree_")
    filt.write("traversal\n")
    filt.flush()

    assert downstream.getvalue() == ""
    assert filt._suppressed == 1


def test_upstream_noise_filter_periodic_summary(caplog):
    downstream = io.StringIO()
    filt = _UpstreamNoiseFilter(downstream)
    filt._last_report = time.monotonic() - (filt.SUMMARY_INTERVAL + 1)

    with caplog.at_level(logging.INFO, logger="caelum.mcp"):
        filt.write("Error getting nodes for handle 123: tree_node\n")
        filt.flush()

    assert any(
        "Suppressed" in rec.message and "tree_node" in rec.message
        for rec in caplog.records
    )
    assert filt._suppressed == 0


def test_upstream_noise_filter_fileno_created_lazily():
    filt, _ = _make_filter()
    try:
        assert filt._write_fd is None
        fd = filt.fileno()
        assert isinstance(fd, int)
        assert filt._write_fd == fd
        # A second call reuses the same pipe; it does not spawn another thread.
        assert filt.fileno() == fd
    finally:
        filt.close()


def test_upstream_noise_filter_reads_from_pipe():
    downstream = io.StringIO()
    filt = _UpstreamNoiseFilter(downstream)
    try:
        fd = filt.fileno()
        os.write(fd, b"Error in tree_traversal: tree_node\nreal error\n")
        # The reader thread drains asynchronously; wait for the forwarded line.
        deadline = time.monotonic() + 3.0
        while "real error" not in downstream.getvalue() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert downstream.getvalue() == "real error\n"
        assert filt._suppressed == 1
    finally:
        filt.close()


@pytest.mark.asyncio
async def test_disconnect_closes_noise_filter():
    client = MCPClient("windows", MCPServerConfig(command="echo", args=[]))
    assert isinstance(client._errlog, _UpstreamNoiseFilter)
    client._errlog.fileno()  # create the pipe + reader thread
    assert client._errlog._write_fd is not None

    await client.disconnect()

    assert client._errlog._write_fd is None
    assert client._errlog._read_fd is None
