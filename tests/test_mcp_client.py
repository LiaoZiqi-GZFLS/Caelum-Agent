"""Tests for MCP client data structures and tool mapping."""

import pytest

from agent.config import MCPServerConfig
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
    from agent.config import MCPConfig

    mcp = MCPMultiplexer(MCPConfig(**config))
    mcp.clients["playwright"]._tools = [
        {"name": "browser_navigate", "description": "navigate", "schema": {"type": "object"}}
    ]
    tools = build_mcp_tools(mcp)
    assert any(t["function"]["name"] == "playwright__browser_navigate" for t in tools)
