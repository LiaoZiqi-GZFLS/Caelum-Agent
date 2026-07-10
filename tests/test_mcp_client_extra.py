"""Extra coverage for mcp_client (connect/reconnect/call/multiplexer)."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_client as mcpmod
from agent.config import MCPConfig, MCPServerConfig
from mcp_client import MCPClient, MCPMultiplexer, ToolResult, _UpstreamNoiseFilter


# ---------------------------------------------------------------------------
# fakes for the stdio transport
# ---------------------------------------------------------------------------

class _ACM:
    """Async context manager returning a fixed object."""

    def __init__(self, obj: Any) -> None:
        self.obj = obj

    async def __aenter__(self) -> Any:
        return self.obj

    async def __aexit__(self, *a: Any) -> bool:
        return False


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=f"d-{name}", inputSchema={"type": "object"})


class FakeSession:
    def __init__(self, *, ping_ok: bool = True, call_raises: BaseException | None = None) -> None:
        self.ping_ok = ping_ok
        self.call_raises = call_raises
        self.initialized = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[_tool("alpha"), _tool("beta")])

    async def send_ping(self) -> None:
        if not self.ping_ok:
            raise RuntimeError("ping failed")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((tool_name, arguments))
        if self.call_raises is not None:
            raise self.call_raises
        return SimpleNamespace(
            content=[SimpleNamespace(text=f"ok:{tool_name}")],
            isError=False,
        )


def _patch_transport(
    monkeypatch,
    *,
    session: FakeSession | None = None,
    stdio_side_effect: Any = None,
) -> FakeSession:
    """Patch stdio_client + ClientSession so connect() never spawns a process."""
    sess = session or FakeSession()

    if stdio_side_effect is None:
        monkeypatch.setattr(
            mcpmod,
            "stdio_client",
            lambda params, errlog=None: _ACM(("read", "write")),
        )
    else:
        monkeypatch.setattr(mcpmod, "stdio_client", stdio_side_effect)

    monkeypatch.setattr(mcpmod, "ClientSession", lambda r, w: _ACM(sess))
    return sess


def _client(command: str = "echo", **kw: Any) -> MCPClient:
    cfg = MCPServerConfig(command=command, args=["--x"], env={"A": "1"})
    return MCPClient("windows", cfg, base_delay=0.01, max_delay=0.02, **kw)


# ---------------------------------------------------------------------------
# _resolve_command
# ---------------------------------------------------------------------------

def test_resolve_command_uses_which_when_found(monkeypatch):
    monkeypatch.setattr(mcpmod.shutil, "which", lambda c: "/usr/bin/" + c)
    c = _client(command="npx")
    assert c._resolve_command() == "npx"


def test_resolve_command_falls_back_to_original(monkeypatch):
    monkeypatch.setattr(mcpmod.shutil, "which", lambda c: None)
    c = _client(command="definitely-not-installed-xyz")
    # candidate in sys.executable's parent won't exist either -> original name
    assert c._resolve_command() == "definitely-not-installed-xyz"


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_success_populates_tools(monkeypatch):
    c = _client()
    monkeypatch.setattr(mcpmod.shutil, "which", lambda x: "echo")
    sess = _patch_transport(monkeypatch)

    ok = await c.connect()

    assert ok is True and c._connected is True
    assert sess.initialized is True
    assert {t["name"] for t in c.tools()} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_connect_retries_then_succeeds(monkeypatch):
    c = _client(max_retries=3)
    monkeypatch.setattr(mcpmod.shutil, "which", lambda x: "echo")

    async def no_sleep(d):
        pass

    monkeypatch.setattr(mcpmod.asyncio, "sleep", no_sleep)

    attempts = {"n": 0}

    def flaky_stdio(params, errlog=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("spawn failed")
        return _ACM(("read", "write"))

    _patch_transport(monkeypatch, stdio_side_effect=flaky_stdio)

    assert await c.connect() is True
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_connect_exhausts_retries(monkeypatch):
    c = _client(max_retries=2)
    monkeypatch.setattr(mcpmod.shutil, "which", lambda x: "echo")

    async def no_sleep(d):
        pass

    monkeypatch.setattr(mcpmod.asyncio, "sleep", no_sleep)

    def always_fail(params, errlog=None):
        raise RuntimeError("nope")

    _patch_transport(monkeypatch, stdio_side_effect=always_fail)

    assert await c.connect() is False
    assert c._connected is False


# ---------------------------------------------------------------------------
# disconnect / reconnect / ping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_resets_state(monkeypatch):
    c = _client()
    monkeypatch.setattr(mcpmod.shutil, "which", lambda x: "echo")
    _patch_transport(monkeypatch)
    await c.connect()
    assert c._connected is True

    await c.disconnect()
    assert c.session is None and c._connected is False


@pytest.mark.asyncio
async def test_reconnect_returns_true_when_already_connected(monkeypatch):
    c = _client()
    monkeypatch.setattr(mcpmod.shutil, "which", lambda x: "echo")
    _patch_transport(monkeypatch)
    await c.connect()

    called = {"connect": 0}
    orig = c.connect

    async def spy():
        called["connect"] += 1
        return await orig()

    c.connect = spy  # type: ignore[assignment]
    assert await c.reconnect() is True
    assert called["connect"] == 0  # short-circuits because already connected


@pytest.mark.asyncio
async def test_ping_no_session_is_false():
    c = _client()
    assert await c.ping() is False


@pytest.mark.asyncio
async def test_ping_exception_returns_false(monkeypatch):
    c = _client()
    c.session = FakeSession(ping_ok=False)  # type: ignore[assignment]
    assert await c.ping() is False


# ---------------------------------------------------------------------------
# call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_success_returns_text(monkeypatch):
    c = _client()
    sess = FakeSession()
    c.session = sess  # type: ignore[assignment]
    c._connected = True

    res = await c.call("alpha", {"x": 1})
    assert isinstance(res, ToolResult)
    assert res.success is True
    assert "ok:alpha" in res.content
    assert sess.calls == [("alpha", {"x": 1})]


@pytest.mark.asyncio
async def test_call_unhealthy_and_reconnect_fails(monkeypatch):
    c = _client()
    c.session = None
    c._connected = False

    async def no_reconnect() -> bool:
        return False

    c.reconnect = no_reconnect  # type: ignore[assignment]

    res = await c.call("alpha", {})
    assert res.success is False
    assert "not connected" in res.content


@pytest.mark.asyncio
async def test_call_tool_exception_returns_error(monkeypatch):
    c = _client()
    c.session = FakeSession(call_raises=RuntimeError("boom"))  # type: ignore[assignment]
    c._connected = True

    res = await c.call("alpha", {})
    assert res.success is False
    assert "boom" in res.content


# ---------------------------------------------------------------------------
# MCPMultiplexer
# ---------------------------------------------------------------------------

def _mux_cfg() -> MCPConfig:
    s = MCPServerConfig(command="echo")
    return MCPConfig(playwright=s, windows=s, filesystem=s)


def test_multiplexer_installs_noise_filter_only_on_windows():
    mux = MCPMultiplexer(_mux_cfg(), health_enabled=False)
    assert isinstance(mux.clients["windows"]._errlog, _UpstreamNoiseFilter)
    assert mux.clients["playwright"]._errlog is sys.stderr
    assert mux.clients["filesystem"]._errlog is sys.stderr


def test_multiplexer_all_tools_aggregates_with_server_tag():
    mux = MCPMultiplexer(_mux_cfg(), health_enabled=False)
    mux.clients["windows"]._tools = [{"name": "Click"}]
    mux.clients["playwright"]._tools = [{"name": "browser_click"}]
    mux.clients["filesystem"]._tools = []

    tools = mux.all_tools()
    assert {"server": "windows", "name": "Click"} in tools
    assert {"server": "playwright", "name": "browser_click"} in tools
    assert len(tools) == 2


def test_multiplexer_client_lookup():
    mux = MCPMultiplexer(_mux_cfg(), health_enabled=False)
    assert mux.client("windows") is mux.clients["windows"]


@pytest.mark.asyncio
async def test_multiplexer_call_unknown_server_raises():
    mux = MCPMultiplexer(_mux_cfg(), health_enabled=False)
    with pytest.raises(ValueError, match="Unknown MCP server"):
        await mux.call("nope", "x", {})


@pytest.mark.asyncio
async def test_multiplexer_call_routes_to_client(monkeypatch):
    mux = MCPMultiplexer(_mux_cfg(), health_enabled=False)

    async def fake_call(tool_name, arguments):
        return ToolResult(success=True, content="routed")

    mux.clients["windows"].call = fake_call  # type: ignore[assignment]
    res = await mux.call("windows", "Click", {"label": 1})
    assert res.content == "routed"


@pytest.mark.asyncio
async def test_multiplexer_connect_and_disconnect_all(monkeypatch):
    mux = MCPMultiplexer(_mux_cfg(), health_enabled=False)
    states = {name: {"connect": 0, "disconnect": 0} for name in mux.clients}

    for name, client in mux.clients.items():
        async def _ok_connect(_n=name):
            states[_n]["connect"] += 1
            return True

        async def _ok_disconnect(_n=name):
            states[_n]["disconnect"] += 1

        client.connect = _ok_connect  # type: ignore[assignment]
        client.disconnect = _ok_disconnect  # type: ignore[assignment]

    await mux.connect_all()
    await mux.disconnect_all()

    assert all(s["connect"] == 1 for s in states.values())
    assert all(s["disconnect"] == 1 for s in states.values())


@pytest.mark.asyncio
async def test_health_monitor_reconnects_unhealthy(monkeypatch):
    mux = MCPMultiplexer(_mux_cfg(), health_enabled=False)
    win = mux.clients["windows"]
    win._connected = True

    pings = {"n": 0}
    reconnects = {"n": 0}

    async def fake_ping():
        pings["n"] += 1
        return False  # always unhealthy

    async def fake_reconnect():
        reconnects["n"] += 1
        return True

    win.ping = fake_ping  # type: ignore[assignment]
    win.reconnect = fake_reconnect  # type: ignore[assignment]

    # Run one iteration of the monitor loop then cancel it.
    task = asyncio.create_task(mux._health_monitor(interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert pings["n"] >= 1
    assert reconnects["n"] >= 1
