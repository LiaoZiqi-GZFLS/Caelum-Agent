"""Tests for MCP client data structures and tool mapping."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.config import MCPConfig, MCPServerConfig
from agent.tools import build_mcp_tools
from mcp_client import MCPClient, MCPMultiplexer, ToolResult


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
