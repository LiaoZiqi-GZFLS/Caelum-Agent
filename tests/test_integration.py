"""Integration smoke tests — require config.yaml with real API credentials.

All tests in this module are marked ``@pytest.mark.smoke`` and auto-skip
when ``config.yaml`` is absent.  To run::

    .venv\\Scripts\\pytest tests/test_integration.py -v -m smoke
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke


def _has_config() -> bool:
    return Path("config.yaml").exists()


requires_config = pytest.mark.skipif(
    not _has_config(), reason="config.yaml not found"
)


# ---------------------------------------------------------------------------
# Kimi API connectivity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@requires_config
async def test_smoke_kimi_chat_roundtrip():
    """A single-turn chat with a real API key returns non-empty content."""
    from agent.config import load_config
    from agent.llm_client import LLMClient

    config = load_config()
    llm = LLMClient(config.llm)
    await llm.initialize()
    try:
        completion = await llm.chat([
            {"role": "user", "content": "Say hello in exactly one word."},
        ])
        content = completion.choices[0].message.content or ""
        assert len(content.strip()) > 0
    finally:
        await llm.close()


@pytest.mark.asyncio
@requires_config
async def test_smoke_kimi_tool_calls():
    """Registering web-search and asking for a search returns tool_calls."""
    from agent.config import load_config
    from agent.llm_client import LLMClient

    config = load_config()
    llm = LLMClient(config.llm)
    await llm.initialize()
    try:
        llm.register_function_tools([{
            "type": "function",
            "function": {
                "name": "web-search",
                "description": "Search the web",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
        }])
        completion = await llm.chat([
            {"role": "user", "content": "Search for the current UTC time."},
        ])
        message = completion.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        assert len(tool_calls) > 0, "Expected at least one tool_call"
    finally:
        await llm.close()


@pytest.mark.asyncio
@requires_config
async def test_smoke_kimi_bad_key():
    """An invalid API key produces a 401 AuthenticationError."""
    import openai

    from agent.config import Config
    from agent.llm_client import LLMClient

    bad_config = Config(
        llm={
            "provider": "kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key": "sk-deadbeef",
            "model": "kimi-k2.6",
        },
        mcp_servers={},
    )
    llm = LLMClient(bad_config.llm)
    await llm.initialize()
    try:
        with pytest.raises(openai.AuthenticationError):
            await llm.chat([{"role": "user", "content": "hi"}])
    finally:
        await llm.close()


# ---------------------------------------------------------------------------
# MCP server connectivity
# ---------------------------------------------------------------------------

_requires_npx = pytest.mark.skipif(
    not any(
        Path(p).exists()
        for p in [
            r"C:\Program Files\nodejs\npx.cmd",
            r"C:\Program Files (x86)\nodejs\npx.cmd",
        ]
    ),
    reason="npx not found",
)


@pytest.mark.asyncio
@requires_config
@_requires_npx
async def test_smoke_mcp_connect_all():
    """All three MCP servers connect without error."""
    from agent.config import load_config
    from mcp_client import MCPMultiplexer

    config = load_config()
    mcp = MCPMultiplexer(config.mcp_servers)
    try:
        await mcp.connect_all()
    finally:
        await mcp.disconnect_all()


@pytest.mark.asyncio
@requires_config
@_requires_npx
async def test_smoke_mcp_each_server_lists_tools():
    """Each connected server returns at least one tool."""
    from agent.config import load_config
    from mcp_client import MCPMultiplexer

    config = load_config()
    mcp = MCPMultiplexer(config.mcp_servers)
    try:
        await mcp.connect_all()
        all_tools = mcp.all_tools()
        servers = {t["server"] for t in all_tools}
        configured = [
            name
            for name in ("playwright", "windows", "filesystem")
            if getattr(config.mcp_servers, name) is not None
        ]
        for server in configured:
            assert server in servers, f"{server} not found in tool list"
        for server in servers:
            server_tools = [t for t in all_tools if t["server"] == server]
            assert len(server_tools) > 0, f"{server} has zero tools"
    finally:
        await mcp.disconnect_all()


@pytest.mark.asyncio
@requires_config
@_requires_npx
async def test_smoke_mcp_disconnect_clean():
    """disconnect_all() runs without hanging or raising."""
    from agent.config import load_config
    from mcp_client import MCPMultiplexer

    config = load_config()
    mcp = MCPMultiplexer(config.mcp_servers)
    await mcp.connect_all()
    await mcp.disconnect_all()
    # Should not hang and should reach here without exception.


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


@requires_config
def test_smoke_config_loads():
    """load_config() reads config.yaml and validates all sections."""
    from agent.config import load_config

    config = load_config()
    assert config.llm.api_key.startswith("sk-"), "API key should start with sk-"
    assert config.llm.model == "kimi-k2.6"
    configured = [
        name
        for name in ("playwright", "windows", "filesystem")
        if getattr(config.mcp_servers, name) is not None
    ]
    assert len(configured) >= 3, "Expected at least 3 MCP servers"


# ---------------------------------------------------------------------------
# Orchestrator lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@requires_config
async def test_smoke_orchestrator_lifecycle():
    """AgentOrchestrator.initialize() → shutdown() clean cycle (no task)."""
    from agent.config import load_config
    from agent.llm_client import LLMClient
    from agent.orchestrator import AgentOrchestrator
    from eventbus import EventBus
    from mcp_client import MCPMultiplexer
    from tests.fakes import FakeKillSwitch

    config = load_config()
    eventbus = EventBus()
    llm = LLMClient(config.llm)
    mcp = MCPMultiplexer(config.mcp_servers)
    killswitch = FakeKillSwitch(eventbus)
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)

    try:
        await agent.initialize()
    finally:
        await agent.shutdown()
