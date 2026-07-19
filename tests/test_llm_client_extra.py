"""Extra coverage for agent.llm_client (Kimi LLMClient internals)."""

from __future__ import annotations

import types
from typing import Any

import httpx
import pytest

from agent.config import LLMConfig
from agent.llm_client import LLMClient
from tests.fakes import _tool_call


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, json_data: Any = None, status: int = 200) -> None:
        self._json = json_data
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")

    def json(self) -> Any:
        return self._json


class FakeHTTP:
    def __init__(self, get_resp: FakeResp | None = None, post_resp: FakeResp | None = None) -> None:
        self.get_resp = get_resp or FakeResp({})
        self.post_resp = post_resp or FakeResp({})
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def get(self, url: str, **kw: Any) -> FakeResp:
        self.gets.append((url, kw))
        return self.get_resp

    async def post(self, url: str, **kw: Any) -> FakeResp:
        self.posts.append((url, kw))
        return self.post_resp

    async def aclose(self) -> None:
        self.closed = True


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"ok": True}


class FakeOpenAI:
    def __init__(self) -> None:
        self.chat = types.SimpleNamespace(completions=FakeCompletions())
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _client(**cfg: Any) -> LLMClient:
    base = {"api_key": "test", "enable_builtin_tools": False}
    base.update(cfg)
    return LLMClient(LLMConfig(**base))


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": f"d-{name}", "parameters": {"type": "object"}},
    }


# ---------------------------------------------------------------------------
# initialize / convert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_disabled_noop():
    c = _client(enable_builtin_tools=False)
    await c.initialize()
    assert c._tools == []


@pytest.mark.asyncio
async def test_initialize_loads_and_converts(monkeypatch):
    c = _client(enable_builtin_tools=True, builtin_tools=["uri1", "uri2"])

    async def fake_fetch(uri: str) -> list[dict[str, Any]]:
        if uri == "uri1":
            return [_tool("web-search")]
        return [
            {"_plugin": {"description": "p", "functions": [{"name": "code_runner", "description": "c"}]}},
            {"bad": "tool"},  # malformed -> skipped
        ]

    monkeypatch.setattr(c, "_fetch_formula_tools", fake_fetch)
    await c.initialize()

    assert c._tool_to_uri["web-search"] == "uri1"
    assert c._tool_to_uri["code_runner"] == "uri2"
    assert "bad" not in c._tool_to_uri
    assert len(c._tools) == 2


@pytest.mark.asyncio
async def test_initialize_fetch_failure_continues(monkeypatch):
    c = _client(enable_builtin_tools=True, builtin_tools=["bad", "good"])

    async def fake_fetch(uri: str) -> list[dict[str, Any]]:
        if uri == "bad":
            raise RuntimeError("network down")
        return [_tool("from-good")]

    monkeypatch.setattr(c, "_fetch_formula_tools", fake_fetch)
    await c.initialize()

    assert list(c._tool_to_uri.keys()) == ["from-good"]
    assert c._tool_to_uri["from-good"] == "good"


def test_convert_formula_tool_variants():
    assert LLMClient._convert_formula_tool(_tool("x"))["function"]["name"] == "x"
    plugin = {"_plugin": {"functions": [{"name": "p1", "description": "d"}]}}
    assert LLMClient._convert_formula_tool(plugin)["function"]["name"] == "p1"
    assert LLMClient._convert_formula_tool({"unknown": 1}) is None


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_register_function_tools_dedup():
    c = _client()
    c.register_function_tools([_tool("a"), _tool("a"), _tool("b")])
    names = [t["function"]["name"] for t in c._tools]
    assert names == ["a", "b"]


def test_register_local_function():
    c = _client()

    def handler(x: int) -> str:
        return str(x)

    c.register_local_function("foo", handler, {"type": "object"}, "desc")
    assert c._local_handlers["foo"] is handler
    assert any(t["function"]["name"] == "foo" for t in c._tools)


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_uses_registered_tools_by_default():
    c = _client()
    c._tools = [_tool("registered")]
    c.client = FakeOpenAI()

    await c.chat(messages=[{"role": "user", "content": "hi"}])

    kwargs = c.client.chat.completions.calls[0]
    assert kwargs["tools"] == c._tools
    assert kwargs["model"] == "kimi-k3"
    assert "reasoning_effort" not in kwargs  # None -> omitted


@pytest.mark.asyncio
async def test_chat_tools_none_omits_tools_key():
    c = _client()
    c._tools = [_tool("registered")]
    c.client = FakeOpenAI()

    await c.chat(messages=[], tools=None)
    assert "tools" not in c.client.chat.completions.calls[0]


@pytest.mark.asyncio
async def test_chat_explicit_tools_override():
    c = _client()
    c._tools = [_tool("registered")]
    c.client = FakeOpenAI()

    explicit = [_tool("explicit")]
    await c.chat(messages=[], tools=explicit)
    assert c.client.chat.completions.calls[0]["tools"] == explicit


@pytest.mark.asyncio
async def test_chat_includes_reasoning_effort_when_set():
    c = _client(reasoning_effort="max")
    c.client = FakeOpenAI()

    await c.chat(messages=[])
    assert c.client.chat.completions.calls[0]["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_chat_passes_response_format_when_set():
    c = _client()
    c.client = FakeOpenAI()

    await c.chat(messages=[], response_format={"type": "json_object"})
    assert c.client.chat.completions.calls[0]["response_format"] == {
        "type": "json_object"
    }


@pytest.mark.asyncio
async def test_chat_omits_response_format_by_default():
    c = _client()
    c.client = FakeOpenAI()

    await c.chat(messages=[])
    assert "response_format" not in c.client.chat.completions.calls[0]


# ---------------------------------------------------------------------------
# execute_tool_calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_tool_calls_formula(monkeypatch):
    c = _client()
    c._tool_to_uri["web-search"] = "moonshot/web-search:latest"

    async def ok(uri, name, args):
        return "RESULT"

    monkeypatch.setattr(c, "_call_formula", ok)

    res = await c.execute_tool_calls([_tool_call("web-search", {"q": "x"}, call_id="1")])
    assert res == [{"role": "tool", "tool_call_id": "1", "content": "RESULT"}]


@pytest.mark.asyncio
async def test_execute_tool_calls_formula_error(monkeypatch):
    c = _client()
    c._tool_to_uri["web-search"] = "uri"

    async def boom(uri, name, args):
        raise RuntimeError("quota")

    monkeypatch.setattr(c, "_call_formula", boom)
    res = await c.execute_tool_calls([_tool_call("web-search", {})])
    assert res[0]["content"].startswith("[error]")
    assert "quota" in res[0]["content"]


@pytest.mark.asyncio
async def test_execute_tool_calls_local_sync_and_async():
    c = _client()
    c.register_local_function("sync_tool", lambda x: f"S{x}", {}, "d")

    async def async_handler(x: str) -> str:
        return f"A{x}"

    c.register_local_function("async_tool", async_handler, {}, "d")

    res = await c.execute_tool_calls(
        [
            _tool_call("sync_tool", {"x": 1}, call_id="1"),
            _tool_call("async_tool", {"x": 2}, call_id="2"),
        ]
    )
    assert [r["content"] for r in res] == ["S1", "A2"]


@pytest.mark.asyncio
async def test_execute_tool_calls_local_error():
    c = _client()

    def handler(**kw):
        raise ValueError("bad")

    c.register_local_function("fragile", handler, {}, "d")
    res = await c.execute_tool_calls([_tool_call("fragile", {})])
    assert res[0]["content"].startswith("[error]")
    assert "bad" in res[0]["content"]


@pytest.mark.asyncio
async def test_execute_tool_calls_unregistered():
    c = _client()
    res = await c.execute_tool_calls([_tool_call("nope", {})])
    assert "not registered" in res[0]["content"]


# ---------------------------------------------------------------------------
# _call_formula / _fetch_formula_tools / close / tool_names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_formula_posts_and_returns_output():
    c = _client()
    c.http = FakeHTTP(post_resp=FakeResp({"context": {"output": "42"}}))

    out = await c._call_formula("moonshot/code-runner:latest", "code_runner", {"x": 1})

    assert out == "42"
    url, kw = c.http.posts[0]
    assert url.endswith("/formulas/moonshot/code-runner:latest/fibers")
    assert kw["json"]["name"] == "code_runner"


@pytest.mark.asyncio
async def test_call_formula_falls_back_to_encrypted_output():
    c = _client()
    c.http = FakeHTTP(post_resp=FakeResp({"context": {"encrypted_output": "enc"}}))
    assert await c._call_formula("u", "n", {}) == "enc"


@pytest.mark.asyncio
async def test_fetch_formula_tools_gets():
    c = _client()
    c.http = FakeHTTP(get_resp=FakeResp({"tools": [{"a": 1}, {"b": 2}]}))

    tools = await c._fetch_formula_tools("u")
    assert tools == [{"a": 1}, {"b": 2}]
    assert c.http.gets[0][0].endswith("/formulas/u/tools")


@pytest.mark.asyncio
async def test_close_closes_http_and_client():
    c = _client()
    http = FakeHTTP()
    openai = FakeOpenAI()
    c.http = http
    c.client = openai

    await c.close()
    assert http.closed is True
    assert openai.closed is True


def test_tool_names_combines_formula_and_local():
    c = _client()
    c._tool_to_uri = {"f1": "u1", "f2": "u2"}
    c._local_handlers = {"l1": lambda: None}
    assert sorted(c.tool_names()) == ["f1", "f2", "l1"]
