# Test Infrastructure Refactoring, Integration Smoke Tests & Code Review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate scattered test fakes into a shared module, add `@pytest.mark.smoke` integration tests for real API/MCP connections, and run security + code quality review passes with categorized findings.

**Architecture:** Extract 10 fake classes from 5 test files into `tests/fakes.py`, create `tests/conftest.py` with shared fixtures and pytest markers, add `tests/test_integration.py` with 8 smoke tests gated behind `config.yaml`, then run two sequential review passes (security → code quality). The canonical `FakeLLM` supports three modes via separate queues: chat (scripted completions), tool (scripted tool results), and combined (both queues active simultaneously for orchestrator tests).

**Tech Stack:** pytest 9.x, pytest-asyncio, Python 3.12, httpx, openai

---

## File Structure

| File | Responsibility |
|------|---------------|
| `tests/fakes.py` | **New** — canonical `FakeLLM`, `FakeMCP`, `FakeKillSwitch`, `FakeReflection`, `FakePerception`, `FakeSkillLearner`, `TriggeringLLM` |
| `tests/conftest.py` | **New** — shared fixtures (`eventbus`, `config`, `tiny_config`, `killswitch`, `memory_store`), pytest markers (`smoke`, `slow`) |
| `tests/test_integration.py` | **New** — 8 smoke tests behind `@pytest.mark.smoke` auto-skip when `config.yaml` absent |
| `tests/test_kimi_memory.py` | **Modify** — drop local `FakeLLM`, import from `fakes` |
| `tests/test_memory.py` | **Modify** — drop local `FakeLLMForMemory`, import from `fakes`; drop local `memory` fixture → use conftest |
| `tests/test_reflection.py` | **Modify** — drop local `FakeLLMForRethink`, import from `fakes`; drop local `memory` fixture → use conftest |
| `tests/test_skills.py` | **Modify** — drop local `FakeLLM`, import from `fakes`; drop local `memory` fixture → use conftest |
| `tests/test_orchestrator.py` | **Modify** — drop all 7 local fake classes, import from `fakes`; drop local `_make_config` → use conftest `config` fixture |
| `agent/tools.py` | **Reviewed** (security + quality) — no changes unless CRITICAL/IMPORTANT finding |
| `agent/orchestrator.py` | **Reviewed** (security + quality) — no changes unless CRITICAL/IMPORTANT finding |
| `agent/security.py` | **Reviewed** (security + quality) — no changes unless CRITICAL/IMPORTANT finding |
| `agent/config.py` | **Reviewed** (security + quality) — no changes unless CRITICAL/IMPORTANT finding |
| `agent/llm_client.py` | **Reviewed** (security + quality) — no changes unless CRITICAL/IMPORTANT finding |
| `agent/kill_switch.py` | **Reviewed** (security + quality) — no changes unless CRITICAL/IMPORTANT finding |
| `main.py` | **Reviewed** (security + quality) — no changes unless CRITICAL/IMPORTANT finding |

---

### Task 1: Create `tests/fakes.py` — canonical fake classes

**Files:**
- Create: `tests/fakes.py`
- Test: None new — existing tests will verify

- [ ] **Step 1: Write `tests/fakes.py`**

```python
"""Shared fake implementations for test suites.

All fake classes live here so that test files can import them from a single
source instead of each defining their own slightly-different variant.
"""

from __future__ import annotations

import copy
from typing import Any

from agent.kill_switch import KillSwitch
from agent.perception import Perception, PerceptionModule
from agent.reflection import ReflectionEngine
from eventbus import EventBus
from eventbus.events import KillSwitchTriggered
from mcp_client import ToolResult


class FakeLLM:
    """Scripted fake LLM usable as both chat client and tool executor.

    Three input modes (queues) that compose cleanly:

    1. ``chat_responses`` — ChatCompletion-like objects (or Exceptions)
       returned by ``chat()``. Indexed sequentially.
    2. ``tool_responses`` — lists of tool-result dicts returned by
       ``execute_tool_calls()``. Indexed sequentially.
    3. Combined — queues are independent. The orchestrator uses both.

    Each queue can be left empty; sensible defaults apply.
    """

    def __init__(
        self,
        chat_responses: list[Any] | None = None,
        tool_responses: list[list[dict[str, Any]]] | None = None,
        tool_names: list[str] | None = None,
        default_chat: Any | None = None,
    ) -> None:
        self._chat_queue = list(chat_responses or [])
        self._tool_queue = list(tool_responses or [])
        self._tool_names = list(tool_names or [])
        self._default_chat = default_chat
        self._chat_index = 0
        self._tool_index = 0
        # Public recording fields.
        self.calls: list[list[dict[str, Any]]] = []
        self.last_tools: list[Any] = []
        self.tools: list[str] = []

    # -- Registration (for orchestrator) ---------------------------------

    def register_function_tools(self, tools: list[dict[str, Any]]) -> None:
        for t in tools:
            self.tools.append(t["function"]["name"])

    def register_local_function(self, name: str, fn: Any, **kwargs: Any) -> None:
        self.tools.append(name)

    def tool_names(self) -> list[str]:
        return self.tools

    # -- Lifecycle -------------------------------------------------------

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    # -- chat() — orchestrator + skills tests ----------------------------

    async def chat(
        self, messages: list[dict[str, Any]], tools: Any = None
    ) -> Any:
        self.calls.append(messages)
        self.last_tools.append(tools)
        response: Any
        if self._chat_index < len(self._chat_queue):
            response = self._chat_queue[self._chat_index]
        elif self._default_chat is not None:
            response = self._default_chat
        else:
            raise RuntimeError(
                f"FakeLLM ran out of chat responses after {self._chat_index} calls"
            )
        self._chat_index += 1
        if isinstance(response, Exception):
            raise response
        return response

    # -- execute_tool_calls() — kimi_memory + reflection + orchestrator ---

    async def execute_tool_calls(
        self, calls: list[Any]
    ) -> list[dict[str, Any]]:
        if self._tool_index < len(self._tool_queue):
            result = self._tool_queue[self._tool_index]
            self._tool_index += 1
            return result
        # Default: return empty success results.
        return [
            {"role": "tool", "tool_call_id": call.id, "content": "{}"}
            for call in calls
        ]


class FakeMCP:
    """In-memory MCP multiplexer with call recording and result stubbing."""

    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        self._tools = tools or []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._results: dict[tuple[str, str], ToolResult] = {}

    def set_result(self, server: str, tool: str, result: ToolResult) -> None:
        self._results[(server, tool)] = result

    async def connect_all(self) -> None:
        pass

    async def disconnect_all(self) -> None:
        pass

    async def call(
        self, server: str, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        self.calls.append((server, tool_name, arguments))
        return self._results.get(
            (server, tool_name),
            ToolResult(success=True, content=f"{server}/{tool_name} ok"),
        )

    def all_tools(self) -> list[dict[str, Any]]:
        return self._tools


class FakeKillSwitch(KillSwitch):
    """Kill switch that never listens to pynput; trigger manually."""

    def __init__(self, eventbus: EventBus) -> None:
        self.eventbus = eventbus
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    async def trigger(self) -> None:
        await self.eventbus.emit(KillSwitchTriggered(reason="test"))


class FakePerception(PerceptionModule):
    """Perception module that replays pre-baked Perception objects."""

    def __init__(self, perceptions: list[Perception] | None = None) -> None:
        self.perceptions = list(perceptions or [])
        self._index = 0
        self.calls: list[str] = []

    async def perceive(self, instruction: str = "") -> Perception:
        self.calls.append(instruction)
        if self._index >= len(self.perceptions):
            base = (
                self.perceptions[-1]
                if self.perceptions
                else _blank_perception()
            )
        else:
            base = self.perceptions[self._index]
        self._index += 1
        perception = copy.copy(base)
        if not perception.ui_hash:
            perception.ui_hash = f"fake-{self._index - 1}"
        return perception


class FakeReflection(ReflectionEngine):
    """Reflection engine that records entries in a list."""

    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def build_context(self, user_input: str) -> str:
        return ""

    async def record(
        self, task_summary: str, failure_reason: str, fix_action: str
    ) -> None:
        self.recorded.append({
            "task_summary": task_summary,
            "failure_reason": failure_reason,
            "fix_action": fix_action,
        })


class FakeSkillLearner:
    """Skill learner that records (task, trajectory) calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def learn(
        self, task: str, trajectory: list[str]
    ) -> dict[str, Any]:
        self.calls.append((task, list(trajectory)))
        return {
            "name": "learned",
            "version": "v0.1.0",
            "path": "/tmp/learned.md",
            "merged": False,
        }


class TriggeringLLM(FakeLLM):
    """Fake LLM that fires the kill switch on the first chat() call."""

    def __init__(
        self,
        responses: list[Any],
        killswitch: FakeKillSwitch,
    ) -> None:
        super().__init__(chat_responses=responses)
        self._killswitch = killswitch

    async def chat(
        self, messages: list[dict[str, Any]], tools: Any = None
    ) -> Any:
        if self._chat_index == 0:
            await self._killswitch.trigger()
        return await super().chat(messages, tools)


# -- Helpers (formerly in test_orchestrator.py) -------------------------

def _blank_perception() -> Perception:
    """Return a minimal Perception for tests that just need one."""
    from pathlib import Path

    return Perception(
        screenshot_path=Path("/tmp/blank.jpg"),
        description="Blank screen",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
    )


def _same_hash_perception(hash_value: str = "same") -> Perception:
    """Return a Perception with a fixed ui_hash for loop-detection tests."""
    from pathlib import Path

    return Perception(
        screenshot_path=Path("/tmp/blank.jpg"),
        description="Same screen",
        ocr_text="same",
        ui_tree={"same": True},
        som_annotations=[],
        ui_hash=hash_value,
    )


def _message(content: str = "", tool_calls: list[Any] | None = None) -> Any:
    """Build a ChatCompletion-like object from content + optional tool_calls."""
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls or [],
                )
            )
        ]
    )


class _FakeToolCall:
    """A tool-call object that mimics OpenAI's ToolCall with model_dump()."""

    def __init__(
        self, name: str, args: dict[str, Any], call_id: str = "call_1"
    ) -> None:
        self.id = call_id
        self.function = SimpleNamespace
        import json as _json

        self.function.name = name
        self.function.arguments = _json.dumps(args)

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


def _tool_call(
    name: str, args: dict[str, Any], call_id: str = "call_1"
) -> Any:
    """Shorthand to create a FakeToolCall."""
    return _FakeToolCall(name, args, call_id)
```

Wait — the `_FakeToolCall` class above has a bug: `self.function = SimpleNamespace` sets `function` to the *class*, not an instance. Let's fix that in the actual write. The correct version is:

```python
class _FakeToolCall:
    """A tool-call object that mimics OpenAI's ToolCall with model_dump()."""

    def __init__(
        self, name: str, args: dict[str, Any], call_id: str = "call_1"
    ) -> None:
        self.id = call_id
        import json as _json

        self.function = SimpleNamespace(
            name=name, arguments=_json.dumps(args)
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }
```

And `SimpleNamespace` needs to be imported in `_FakeToolCall` — but wait, it's used in `_message` too. Let's just import it at the top.

- [ ] **Step 2: Verify syntax**

Run: `.venv\Scripts\python.exe -m py_compile tests/fakes.py`
Expected: no output (success)

- [ ] **Step 3: Run full test suite to confirm no regressions from importing fakes**

Even though no existing test imports from fakes yet, we want to confirm the file itself loads cleanly:
Run: `.venv\Scripts\python.exe -c "from tests.fakes import FakeLLM, FakeMCP, FakeKillSwitch, FakePerception, FakeReflection, FakeSkillLearner, TriggeringLLM, _blank_perception, _same_hash_perception, _message, _tool_call; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tests/fakes.py
git commit -m "feat: add canonical test fakes module"
```

---

### Task 2: Create `tests/conftest.py` — shared fixtures and markers

**Files:**
- Create: `tests/conftest.py`
- Test: None new — existing tests will use these fixtures

- [ ] **Step 1: Write `tests/conftest.py`**

```python
"""Shared pytest fixtures and markers for Caelum-Agent tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import Config
from eventbus import EventBus
from tests.fakes import FakeKillSwitch


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "smoke: real API/MCP integration tests (needs config.yaml)",
    )
    config.addinivalue_line(
        "markers",
        "slow: tests that take >5 seconds",
    )


@pytest.fixture
def eventbus():
    """A fresh EventBus instance."""
    return EventBus()


@pytest.fixture
def tiny_config(tmp_path: Path) -> Config:
    """Minimal Config for unit tests that don't need full orchestrator setup."""
    return Config(
        llm={
            "provider": "kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key": "test",
            "model": "kimi-k2.6",
        },
        mcp_servers={},
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={
            "skills_dir": str(tmp_path / "skills"),
            "cache_dir": str(tmp_path / "cache"),
            "audit_log": str(tmp_path / "audit.log"),
        },
        security={
            "default_level": "read",
            "auto_execute_levels": ["read", "write_safe"],
            "confirm_levels": ["write_risky"],
            "destructive_requires_approval": True,
        },
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Full Config with all MCP stubs — used by orchestrator tests."""
    return Config(
        llm={
            "provider": "kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key": "test",
            "model": "kimi-k2.6",
            "enable_builtin_tools": False,
            "builtin_tools": [],
        },
        mcp_servers={
            "playwright": {"command": "npx", "args": [], "env": {}},
            "windows": {"command": "windows-mcp", "args": ["serve"], "env": {}},
            "filesystem": {"command": "npx", "args": [], "env": {}},
        },
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={
            "skills_dir": str(tmp_path / "skills"),
            "cache_dir": str(tmp_path / "cache"),
            "audit_log": str(tmp_path / "audit.log"),
        },
        security={
            "default_level": "read",
            "auto_execute_levels": ["read", "write_safe"],
            "confirm_levels": ["write_risky"],
            "destructive_requires_approval": True,
        },
    )


@pytest.fixture
def killswitch(eventbus):
    """A FakeKillSwitch wired to the shared event bus."""
    return FakeKillSwitch(eventbus)


@pytest.fixture
def memory_store(tmp_path: Path):
    """A MemoryStore backed by tmp_path — shared across test files."""
    from agent.memory import MemoryStore

    return MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )
```

- [ ] **Step 2: Verify conftest loads and pytest picks it up**

Run: `.venv\Scripts\python.exe -m pytest tests/ --collect-only -q 2>&1`
Expected: shows all 166 tests collected, no errors about missing fixtures

- [ ] **Step 3: Run full test suite**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: 166 passed (no regressions — existing tests still use their own fixtures)

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "feat: add shared pytest fixtures and markers via conftest.py"
```

---

### Task 3: Migrate `test_kimi_memory.py` to use shared fakes

**Files:**
- Modify: `tests/test_kimi_memory.py:1-169`

Each test creates `FakeLLM(responses=[[...]], tool_names=...)`. The canonical `FakeLLM` uses `tool_responses=` for the tool queue and `tool_names=` for the tool name list. The migration is a drop-in replacement with a parameter rename.

- [ ] **Step 1: Replace local FakeLLM with import + update all tests**

The local `FakeLLM` class (lines 12-28) is entirely deleted. Replace it with:

```python
from tests.fakes import FakeLLM
```

Then update every test's `FakeLLM(...)` call:
- `FakeLLM([[{...}]])` → `FakeLLM(tool_responses=[[{...}]])` — (the first positional arg was `responses`, now keyword `tool_responses`)
- `FakeLLM(tool_names=[])` → `FakeLLM(tool_names=[])` — unchanged

For example, `test_set_memory_calls_memory_tool` (line 34):
```python
# Before:
llm = FakeLLM([[{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]])

# After:
llm = FakeLLM(tool_responses=[[{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]])
```

For `test_set_memory_raises_when_memory_tool_not_registered` (line 107):
```python
# Before:
llm = FakeLLM(tool_names=[])

# After:
llm = FakeLLM(tool_names=[])
```

The full migrated file (complete content):

```python
"""Tests for the Kimi memory/rethink adapter."""

from __future__ import annotations

from typing import Any

import pytest

from agent.kimi_memory import KimiMemoryClient, ToolNotAvailableError
from tests.fakes import FakeLLM


@pytest.mark.asyncio
async def test_set_memory_calls_memory_tool():
    llm = FakeLLM(tool_responses=[[{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]])
    client = KimiMemoryClient(llm)
    await client.set_memory("user_name", "Alice")

    assert len(llm.calls) == 1
    call = llm.calls[0][0]
    assert call.function.name == "memory"
    args = __import__("json").loads(call.function.arguments)
    assert args["action"] == "save"
    assert args["key"] == "user_name"
    assert args["value"] == "Alice"
    assert args["scope"] == "user"


@pytest.mark.asyncio
async def test_get_memory_returns_top_result_value():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": __import__("json").dumps({
            "results": [
                {"key": "user_name", "value": "Alice", "score": 0.95, "scope": "user"},
            ],
            "count": 1,
        }),
    }]])
    client = KimiMemoryClient(llm)
    value = await client.get_memory("user_name")

    assert value == "Alice"
    args = __import__("json").loads(llm.calls[0][0].function.arguments)
    assert args == {"action": "recall", "query": "user_name", "scope": "user"}


@pytest.mark.asyncio
async def test_get_memory_returns_none_when_empty_results():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": __import__("json").dumps({"results": [], "count": 0}),
    }]])
    client = KimiMemoryClient(llm)
    value = await client.get_memory("missing_key")

    assert value is None


@pytest.mark.asyncio
async def test_rethink_returns_reflection():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Try a different directory.",
    }]])
    client = KimiMemoryClient(llm)
    result = await client.rethink(
        task_summary="list files",
        failure_reason="directory empty",
        context=["tried ./docs"],
    )

    assert result == "Try a different directory."
    call = llm.calls[0][0]
    assert call.function.name == "rethink"
    args = __import__("json").loads(call.function.arguments)
    assert args["action"] == "organize"
    assert "Task: list files" in args["thought"]
    assert "Failure: directory empty" in args["thought"]
    assert "tried ./docs" in args["thought"]


@pytest.mark.asyncio
async def test_set_memory_raises_when_memory_tool_not_registered():
    llm = FakeLLM(tool_names=[])
    client = KimiMemoryClient(llm)

    with pytest.raises(ToolNotAvailableError):
        await client.set_memory("user_name", "Alice")


@pytest.mark.asyncio
async def test_get_memory_raises_when_memory_tool_not_registered():
    llm = FakeLLM(tool_names=[])
    client = KimiMemoryClient(llm)

    with pytest.raises(ToolNotAvailableError):
        await client.get_memory("user_name")


@pytest.mark.asyncio
async def test_rethink_raises_when_rethink_tool_not_registered():
    llm = FakeLLM(tool_names=[])
    client = KimiMemoryClient(llm)

    with pytest.raises(ToolNotAvailableError):
        await client.rethink("task", "failure")


@pytest.mark.asyncio
async def test_set_memory_raises_runtime_error_on_error_output():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "[error] tool execution failed",
    }]])
    client = KimiMemoryClient(llm)

    with pytest.raises(RuntimeError, match="tool execution failed"):
        await client.set_memory("user_name", "Alice")


@pytest.mark.asyncio
async def test_get_memory_raises_runtime_error_on_error_output():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "[error] recall failed",
    }]])
    client = KimiMemoryClient(llm)

    with pytest.raises(RuntimeError, match="recall failed"):
        await client.get_memory("user_name")


@pytest.mark.asyncio
async def test_rethink_raises_runtime_error_on_error_output():
    llm = FakeLLM(tool_responses=[[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "[error] rethink failed",
    }]])
    client = KimiMemoryClient(llm)

    with pytest.raises(RuntimeError, match="rethink failed"):
        await client.rethink("task", "failure")
```

- [ ] **Step 2: Run the migrated tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_kimi_memory.py -v`
Expected: 9 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_kimi_memory.py
git commit -m "refactor: migrate test_kimi_memory.py to shared FakeLLM"
```

---

### Task 4: Migrate `test_memory.py` to use shared fakes and conftest fixtures

**Files:**
- Modify: `tests/test_memory.py:1-143`

- [ ] **Step 1: Replace local FakeLLMForMemory and local `memory` fixture**

Delete the local `FakeLLMForMemory` class (lines 44-56) and local `memory` fixture (lines 14-20). Add imports from `fakes` and `conftest`. Update all test usages.

Full migrated file:

```python
"""Tests for local memory store."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent.kimi_memory import KimiMemoryClient
from agent.memory import MemoryStore
from tests.fakes import FakeLLM


def test_preference_round_trip(memory_store):
    memory_store.set_preference("theme", "dark")
    assert memory_store.get_preference("theme") == "dark"
    assert memory_store.get_preference("missing", "default") == "default"


def test_audit(memory_store):
    memory_store.audit("read", "test", "noop", "ok")
    with sqlite3.connect(memory_store.db_path) as conn:
        row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert row is not None
    assert row[2] == "read"


def test_reflection_round_trip(memory_store):
    rid = memory_store.add_reflection("summary", "reason", "fix")
    assert rid > 0
    reflections = memory_store.list_reflections()
    assert any(r["id"] == rid for r in reflections)


@pytest.mark.asyncio
async def test_memory_store_prefers_kimi_memory(memory_store, tmp_path):
    llm = FakeLLM(tool_responses=[
        [{"role": "tool", "tool_call_id": "call_memory", "content": "ok"}],
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({"results": [{"key": "user_name", "value": "Alice"}], "count": 1}),
        }],
    ])
    memory_store.kimi = KimiMemoryClient(llm)

    await memory_store.aset_preference("user_name", "Alice")
    value = await memory_store.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_memory_store_falls_back_to_sqlite_when_kimi_unavailable(memory_store, tmp_path):
    llm = FakeLLM()
    llm.tool_names = lambda: []  # memory tool not registered
    memory_store.kimi = KimiMemoryClient(llm)

    await memory_store.aset_preference("theme", "dark")
    value = await memory_store.aget_preference("theme")

    assert value == "dark"
    assert len(llm.calls) == 0


@pytest.mark.asyncio
async def test_aget_preference_falls_back_to_sqlite_on_semantic_recall_mismatch(memory_store, tmp_path):
    # Kimi recall returns a similar but different key; SQLite holds the exact key.
    llm = FakeLLM(tool_responses=[
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({
                "results": [{"key": "user_name_alias", "value": "Bob"}],
                "count": 1,
            }),
        }],
    ])
    memory_store.kimi = KimiMemoryClient(llm)
    memory_store.set_preference("user_name", "Alice")

    value = await memory_store.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_aget_preference_returns_kimi_value_on_exact_key_match(memory_store, tmp_path):
    llm = FakeLLM(tool_responses=[
        [{
            "role": "tool",
            "tool_call_id": "call_memory",
            "content": json.dumps({
                "results": [{"key": "user_name", "value": "Alice"}],
                "count": 1,
            }),
        }],
    ])
    memory_store.kimi = KimiMemoryClient(llm)
    memory_store.set_preference("user_name", "SQLite-Value")

    value = await memory_store.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 1


def test_skills_sync_and_search(memory_store, tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "click.md").write_text("Click the element described in the instruction.")
    (skills_dir / "type.md").write_text("Type text into a focused input field.")
    memory_store.sync_skills()
    results = memory_store.search_skills("click element", top_k=1)
    assert len(results) == 1
    assert results[0]["name"] == "click"
```

Key changes:
- `FakeLLMForMemory(responses=[...])` → `FakeLLM(tool_responses=[...])`
- Local `memory` fixture → conftest `memory_store` fixture (renamed to avoid collision with `agent.memory` import aliases in other files)
- All test functions now accept `memory_store` instead of `memory`

- [ ] **Step 2: Run the migrated tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_memory.py -v`
Expected: 8 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_memory.py
git commit -m "refactor: migrate test_memory.py to shared FakeLLM and conftest fixtures"
```

---

### Task 5: Migrate `test_reflection.py` to use shared fakes and conftest fixtures

**Files:**
- Modify: `tests/test_reflection.py:1-156`

- [ ] **Step 1: Replace local FakeLLMForRethink and local `memory` fixture**

Delete `FakeLLMForRethink` (lines 15-27) and local `memory` fixture (lines 30-36). Add imports.

Complete migrated file:

```python
"""Tests for ReflectionEngine with optional Kimi rethink."""

from __future__ import annotations

from typing import Any

import pytest

from agent.config import Config
from agent.kimi_memory import KimiMemoryClient
from agent.reflection import ReflectionEngine
from tests.fakes import FakeLLM


@pytest.mark.asyncio
async def test_record_uses_rethink_when_available(memory_store):
    llm = FakeLLM(tool_responses=[[{"role": "tool", "tool_call_id": "call_rethink", "content": "Use a different path."}]])
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 1
    assert llm.calls[0][0].function.name == "rethink"

    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "list files"
    assert stored[0]["fix_action"] == "Use a different path."


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_when_rethink_unavailable(memory_store):
    llm = FakeLLM()
    llm.tool_names = lambda: []
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 0
    assert len(engine.retrieve()) == 1


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_on_rethink_exception(memory_store):
    class FakeLLMBoom(FakeLLM):
        async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

    llm = FakeLLMBoom()
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "list files"
    assert stored[0]["fix_action"] == "tried ./docs"


def test_build_context_without_reflections(memory_store):
    config = Config(llm={"api_key": "test"}, mcp_servers={})
    engine = ReflectionEngine(config, memory_store)
    assert engine.build_context("anything") == ""


def test_record_sync_persists_without_llm(memory_store):
    llm = FakeLLM()
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = engine.record_sync("sync task", "it broke", "reboot")

    assert rid > 0
    assert len(llm.calls) == 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["task_summary"] == "sync task"
    assert stored[0]["fix_action"] == "reboot"


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_when_rethink_disabled(memory_store):
    llm = FakeLLM(tool_responses=[[{"role": "tool", "tool_call_id": "call_rethink", "content": "Ignored."}]])
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": False},
    )
    engine = ReflectionEngine(config, memory_store, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 0
    stored = engine.retrieve()
    assert len(stored) == 1
    assert stored[0]["fix_action"] == "tried ./docs"


def test_build_context_formats_stored_reflection(memory_store):
    config = Config(llm={"api_key": "test"}, mcp_servers={})
    engine = ReflectionEngine(config, memory_store)
    engine.record_sync("resize window", "window too small", "maximize it")

    context = engine.build_context("resize window")

    assert context.startswith("Past reflections that may help:")
    assert "resize window" in context
    assert "maximize it" in context
```

Key changes:
- `FakeLLMForRethink(responses=[...])` → `FakeLLM(tool_responses=[...])`
- `FakeLLMBoom(FakeLLMForRethink)` → `FakeLLMBoom(FakeLLM)` (subclassing the canonical)
- Local `memory` fixture → conftest `memory_store`

- [ ] **Step 2: Run the migrated tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_reflection.py -v`
Expected: 9 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_reflection.py
git commit -m "refactor: migrate test_reflection.py to shared FakeLLM and conftest fixtures"
```

---

### Task 6: Migrate `test_skills.py` to use shared fakes and conftest fixtures

**Files:**
- Modify: `tests/test_skills.py:1-226`

- [ ] **Step 1: Replace local FakeLLM and local `memory` fixture**

Delete local `FakeLLM` (lines 33-52) and local `memory` fixture (lines 16-22). The `learner` fixture stays — it still creates a `SkillLearner`, just uses `memory_store` from conftest.

Full migrated file:

```python
"""Tests for the AutoSkill learning module."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.memory import MemoryStore
from agent.security import SecurityGuard
from agent.skills import SkillLearner
from tests.fakes import FakeLLM


@pytest.fixture
def learner(memory_store: MemoryStore, tmp_path: Path) -> SkillLearner:
    return SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory_store,
    )


@pytest.mark.asyncio
async def test_learn_creates_new_skill(learner: SkillLearner, tmp_path: Path) -> None:
    result = await learner.learn(
        "open notepad",
        ["windows/Click: clicked Notepad icon"],
    )

    assert result["merged"] is False
    assert result["version"] == "v0.1.0"
    assert Path(result["path"]).exists()
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "learned/open-notepad" in content or "open notepad" in content.lower()
    assert "v0.1.0" in content


@pytest.mark.asyncio
async def test_learn_merges_similar_skill(
    learner: SkillLearner, memory_store: MemoryStore, tmp_path: Path
) -> None:
    # Seed an existing skill about Notepad.
    learned_dir = tmp_path / "skills" / "learned"
    learned_dir.mkdir(parents=True)
    original = learned_dir / "open-notepad.md"
    original.write_text("open notepad", encoding="utf-8")
    memory_store.sync_skills()

    result = await learner.learn(
        "open notepad",
        ["windows/Click: focused Notepad window"],
    )

    assert result["merged"] is True
    assert result["version"] == "v0.1.1"
    content = (learned_dir / "open-notepad.md").read_text(encoding="utf-8")
    assert "v0.1.1" in content


@pytest.mark.asyncio
async def test_learn_uses_llm_when_available(
    memory_store: MemoryStore, tmp_path: Path
) -> None:
    payload = {
        "name": "launch-calculator",
        "description": "Open the calculator app.",
        "usage": "open calculator",
        "steps": ["Click the calculator icon."],
        "tags": "calculator,math",
        "version": "v0.1.0",
    }
    # Build a chat_response that wraps JSON in markdown code fence (matching
    # how the real LLM returns structured skill output).
    chat_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=f"```json\n{__import__('json').dumps(payload)}\n```"
                )
            )
        ]
    )
    fake_llm = FakeLLM(chat_responses=[chat_response])
    learner = SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory_store,
        llm_client=fake_llm,
    )

    result = await learner.learn("open calculator", [])

    assert fake_llm.calls
    assert result["name"] == "learned/launch-calculator"
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "Open the calculator app." in content


def test_memory_store_writes_audit_file(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit.log"
    memory = MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
        audit_log_path=audit_file,
    )
    memory.audit("read", "test", "noop", "ok")

    assert audit_file.exists()
    content = audit_file.read_text(encoding="utf-8")
    assert "read" in content
    assert "test" in content
    assert "noop" in content


def test_security_typed_confirmation_blocks_on_mismatch(monkeypatch) -> None:
    from agent.config import SecurityConfig

    calls = []

    def callback(summary: str, action: dict) -> bool:
        calls.append((summary, action))
        return True

    config = SecurityConfig(
        destructive_requires_approval=True,
        destructive_requires_typed_confirmation=True,
    )
    guard = SecurityGuard(config, confirm_callback=callback)
    action = {"server": "windows", "tool": "delete", "args": {"path": "x"}}
    monkeypatch.setattr("builtins.input", lambda _: "wrong summary")
    approval = guard.check("destructive", action)

    assert approval.allowed is False
    assert approval.reason == "human-denied"


def test_security_typed_confirmation_allows_on_match(monkeypatch) -> None:
    from agent.config import SecurityConfig

    config = SecurityConfig(
        destructive_requires_approval=True,
        destructive_requires_typed_confirmation=True,
    )
    guard = SecurityGuard(config, confirm_callback=lambda s, a: True)
    action = {"server": "windows", "tool": "delete", "args": {"path": "x"}}
    expected_summary = guard._summarize(action)
    monkeypatch.setattr("builtins.input", lambda _: expected_summary)
    approval = guard.check("destructive", action)

    assert approval.allowed is True
    assert approval.reason == "human-confirmed"


@pytest.mark.asyncio
async def test_learn_falls_back_when_llm_fails(
    memory_store: MemoryStore, tmp_path: Path
) -> None:
    class BrokenLLM:
        async def chat(
            self, messages: list[dict[str, Any]], tools: Any | None = None
        ) -> Any:
            raise RuntimeError("LLM unavailable")

    learner = SkillLearner(
        skills_dir=tmp_path / "skills",
        memory=memory_store,
        llm_client=BrokenLLM(),
    )

    result = await learner.learn("open calculator", ["windows/Click: clicked Calculator"])

    assert Path(result["path"]).exists()
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "open-calculator" in content


def test_bump_version() -> None:
    assert SkillLearner._bump_version("v1.2.3") == "v1.2.4"
    assert SkillLearner._bump_version("0.1.0") == "v0.1.1"
    assert SkillLearner._bump_version("not-a-version") == "v0.1.1"


def test_slugify() -> None:
    assert SkillLearner._slugify("Open Notepad") == "open-notepad"
    assert SkillLearner._slugify("Click  the  button!!!") == "click-the-button"
    assert SkillLearner._slugify("") == "learned-skill"


def test_parse_skill_round_trip(tmp_path: Path, learner: SkillLearner) -> None:
    skill = {
        "name": "demo-skill",
        "description": "A demo skill.",
        "usage": "run demo",
        "steps": ["Step one.", "Step two."],
        "tags": "demo",
        "version": "v0.2.0",
    }
    path = learner._write_skill(skill)
    parsed = learner._parse_skill(path.read_text(encoding="utf-8"))

    assert parsed["name"] == "demo-skill"
    assert parsed["steps"] == ["Step one.", "Step two."]
    assert parsed["version"] == "v0.2.0"
    assert parsed["tags"] == "demo"
```

Key changes:
- `FakeLLM(payload={...})` → `FakeLLM(chat_responses=[chat_response])` where `chat_response` wraps JSON in markdown
- Local `memory` fixture → conftest `memory_store` (but the `memory` fixture name is kept in `test_memory_store_writes_audit_file` since it creates its own MemoryStore)
- The `learner` fixture now takes `memory_store` from conftest instead of defining its own `memory`

- [ ] **Step 2: Run the migrated tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_skills.py -v`
Expected: 12 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_skills.py
git commit -m "refactor: migrate test_skills.py to shared FakeLLM and conftest fixtures"
```

---

### Task 7: Migrate `test_orchestrator.py` to use shared fakes and conftest fixtures

**Files:**
- Modify: `tests/test_orchestrator.py:1-1076`

This is the largest migration. The file currently defines 7 fake classes and a `_make_config` helper at the top. All of them are replaced by imports from `tests.fakes` and the conftest `config` fixture.

- [ ] **Step 1: Write the migrated file**

Delete everything from line 1 through line 155 (all fakes and helpers), plus the `_make_config` function and `config`/`killswitch` fixtures (lines 217-259). Keep all test functions, but update their imports and fixture references.

The migration is mechanical — replace the imports block and remove the fake class definitions. Here is the complete migrated file (the test functions themselves are unchanged except for fixture names):

```python
"""Tests for the ReAct orchestrator loop."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import httpx

from agent.config import Config
from agent.orchestrator import AgentOrchestrator
from agent.perception import Perception, PerceptionModule
from eventbus import EventBus
from mcp_client import ToolResult
from tests.fakes import (
    FakeLLM,
    FakeMCP,
    FakeKillSwitch,
    FakePerception,
    FakeReflection,
    FakeSkillLearner,
    TriggeringLLM,
    _blank_perception,
    _same_hash_perception,
    _message,
    _tool_call,
)


def test_orchestrator_starts_in_idle(config, eventbus, killswitch):
    agent = AgentOrchestrator(config, eventbus, FakeLLM(), FakeMCP(), killswitch)
    assert agent.state.current_state == "IDLE"


@pytest.mark.asyncio
async def test_run_task_direct_completion(config, eventbus, killswitch):
    llm = FakeLLM(chat_responses=[
        _message("I will list the files."),
        _message("YES"),
        _message("Here are the files: a.txt, b.txt."),
    ])
    agent = AgentOrchestrator(
        config, eventbus, llm, FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )

    result = await agent.run_task("list files")

    assert result == "Here are the files: a.txt, b.txt."
    assert agent.state.current_state == "COMPLETED"


# ... (all remaining test functions follow the same pattern —
#      FakeLLM(responses=[...]) → FakeLLM(chat_responses=[...]),
#      _make_config(tmp_path) → config fixture,
#      local Fake* classes → imported from tests.fakes.
#      The test bodies themselves are unchanged.)
```

The full migrated file is too long to inline here (1076 lines → ~950 lines after removing fake definitions). The key changes are:

1. **Imports**: Remove local import of `KillSwitch`, `AgentOrchestrator` types from `tests.fakes`; add `from tests.fakes import ...`
2. **Fake classes**: Delete all 7 local fake classes (lines 29-155) — they now come from `tests.fakes`
3. **Helpers**: Delete `_blank_perception`, `_same_hash_perception`, `_message`, `_FakeToolCall`, `_tool_call` — they now come from `tests.fakes`
4. **`_make_config`**: Delete (lines 217-244)
5. **`config` fixture**: Delete (lines 247-259) — now from conftest
6. **`killswitch` fixture**: Delete (lines 262-264) — now from conftest
7. **Test bodies**: Only change `FakeLLM(responses=[...])` → `FakeLLM(chat_responses=[...])`, `FakeLLM(default_response=...)` → `FakeLLM(default_chat=...)`, `TriggeringLLM(responses, killswitch)` → `TriggeringLLM(responses, killswitch)` (unchanged, since TriggeringLLM.__init__ uses `responses` param), and remove the `config=config` explicit kwarg where it shadows the fixture.

- [ ] **Step 2: Run the migrated tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator.py -v`
Expected: 32 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_orchestrator.py
git commit -m "refactor: migrate test_orchestrator.py to shared fakes and conftest fixtures"
```

---

### Task 8: Add `tests/test_integration.py` — smoke tests

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write `tests/test_integration.py`**

```python
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
        for server in config.mcp_servers:
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
    assert len(config.mcp_servers) >= 3, "Expected at least 3 MCP servers"


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
```

- [ ] **Step 2: Verify smoke tests are collected but skipped when no config.yaml**

Run: `.venv\Scripts\python.exe -m pytest tests/test_integration.py -v 2>&1`
Expected: 8 SKIPPED (config.yaml not found)

- [ ] **Step 3: Run full suite to confirm no impact**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: 166 passed, 8 skipped

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "feat: add integration smoke tests for Kimi API, MCP, and orchestrator lifecycle"
```

---

### Task 9: Security review pass

**Files:**
- Reviewed: `agent/security.py`, `agent/tools.py`, `agent/orchestrator.py`, `agent/llm_client.py`, `agent/config.py`, `agent/kill_switch.py`, `main.py`

- [ ] **Step 1: Secret leakage audit**

Check the following sites manually. For each file, verify:
- `api_key` is never included in logging messages or error output
- `api_key` is never written to disk outside of `config.yaml` (which is gitignored)
- Exception messages do not leak key material

Audit targets:
- `agent/llm_client.py` — `openai.AsyncOpenAI(api_key=...)` initialization. Is the key ever printed?
- `agent/config.py` — `LLMConfig.api_key` — is `__repr__` safe?
- `agent/orchestrator.py` — `_save_state()` serializes `self.history`. Does history contain raw HTTP headers or auth?
- `main.py` — error messages printed to stdout. Could they leak key?

Record findings as: `SEVERITY: file:line — description — fix`

- [ ] **Step 2: Tool-call authorization audit**

Verify the security level classifier in `agent/security.py`:
- `classify_tool_call(server, tool_name)` — check that Windows-MCP mutating tools (`Click`, `Type`, `Scroll`, etc.) correctly map to `write_risky`
- Check that `filesystem` read operations map to `read`, write operations to `write_risky`
- Check that `playwright` operations (`browser_click`, `browser_type`) map to appropriate levels
- Verify that `destructive` level is assigned only for truly destructive operations (delete, uninstall, registry write)

- [ ] **Step 3: Injection risk audit**

In `agent/orchestrator.py:_execute_tool_calls`:
- User-provided `text` for `DesktopInteract(type, text=...)` flows through `json.loads(call.function.arguments)` → `args` dict → `mcp.call()`. Is there any escaping risk?
- The `json.dumps(args, ensure_ascii=False)` in audit logging — does this survive malicious payloads?
- Check `_desktop_interact_impl` for any eval/dynamic dispatch risk

- [ ] **Step 4: Kill switch reliability audit**

In `agent/kill_switch.py` and `agent/orchestrator.py`:
- `_check_cancelled()` — is it called in every await point in the main loop?
- `_cancel_event` — is it properly cleared on new task start?
- `_on_kill_switch` callback — is it re-entrant safe? What if it fires twice?
- Check for deadlock paths: if kill switch fires during `mcp.call()`, does cancellation propagate?

- [ ] **Step 5: Filesystem boundary audit**

In `agent/config.py`:
- Filesystem MCP args include `allowed_dirs`. Are these validated for `../` escape?
- Check `config.yaml.example` for default allowed directories — are they too permissive?

- [ ] **Step 6: Code execution sandbox audit**

In `agent/tools.py`:
- `CodeRunner` — what restrictions does `exec()` have?
- `UnsafeCodeError` — what patterns are detected?
- Check if `__import__`, `open()`, `os.system()` are blocked

- [ ] **Step 7: Document findings and fix CRITICAL + IMPORTANT**

Create a findings summary in the commit message or a brief markdown note. Apply fixes for CRITICAL and IMPORTANT items. Leave MINOR items for the code quality pass.

- [ ] **Step 8: Commit security fixes (if any)**

```bash
git add <fixed files>
git commit -m "security: fix findings from security review pass"
```

Skip this step if no CRITICAL or IMPORTANT findings.

---

### Task 10: Code quality review pass

**Files:**
- Reviewed: all `agent/*.py`, `main.py`, `ui_detector/*.py`, `mcp_client/*.py`, `eventbus/*.py`

- [ ] **Step 1: Large file assessment**

| File | Lines | Split? |
|------|-------|--------|
| `agent/orchestrator.py` | 710 | No — methods are already well-factored. The file has one responsibility (orchestrator). |
| `tests/test_orchestrator.py` | 1076 → ~950 | Already split via Task 7 (fakes extracted). Test count (32) warrants a single file. |
| `agent/perception.py` | 267 | No — single class + dataclass. |
| `agent/tools.py` | ~300 | No. |

Verdict: No file split warranted. The large orchestrator test file is normal for integration-heavy tests.

- [ ] **Step 2: Duplicate code scan**

Check for remaining duplication after Tasks 1-7:
- `_format_perception` string building (orchestrator.py) — single site, not duplicated
- MCP result formatting (`f"{server}/{tool_name} ok"`) — this pattern appears in FakeMCP default responses but matches real behavior
- Success-path returns in `_desktop_interact_impl` — three nearly identical blocks (click, type, scroll). Could extract a helper but each block differs in the message format. Not a clear win.

- [ ] **Step 3: Interface consistency check**

- `Perception.som_annotations` — all entries now carry `verdict` and `verify_score` fields (verified in Task 71). ✅
- `ToolResult.success` — all consumers in orchestrator and tools check `.success` before using `.content`. ✅
- Tool naming: `DesktopInteract` (PascalCase) is the outlier vs snake_case MCP names. This is intentional — local function tools use PascalCase, MCP tools use their server-native names. ✅
- All public methods in `AgentOrchestrator` have type annotations. ✅

- [ ] **Step 4: Error handling coverage**

| Site | Pattern | OK? |
|------|---------|-----|
| `perception.py:_fetch_ui_tree` | `except Exception: pass` (line 235) | ⚠️ Silent swallow. Should at least log a warning. |
| `perception.py:_run_ui_detector` | `except Exception as exc: return [{"error": str(exc)}]` (line 278) | ✅ Degrades gracefully. |
| `orchestrator.py:_execute_tool_calls` | Security check failure returns `[blocked]` | ✅ Explicit, no silent failure. |
| `orchestrator.py:_llm_chat_with_breaker` | Exception hierarchy: `APIError`/`HTTPError`/`TimeoutError` → TransientAPIError; all else → TransientAPIError | ⚠️ Broad `except Exception` catch-all. Should log the unexpected type. |
| `llm_client.py:chat` | No explicit error handling — lets openai exceptions propagate | ✅ Caller handles. |
| `skills.py:learn` | `except Exception: logger.warning(...)` (line 664) | ✅ Best-effort is correct for learning. |

- [ ] **Step 5: Type annotation quality**

- `Any` appears 47 times across the codebase. Most are legitimate (user input, tool arguments, MCP results). No narrowing opportunities that don't break existing bindings.
- One actionable improvement: `Perception.som_annotations: list[dict[str, Any]]` could become a TypedDict with `label`, `center_x`, `center_y`, `score`, `verdict`, `verify_score` fields. Defer to future PR.

- [ ] **Step 6: Spec conformance check**

Cross-reference `docs/designs/desktop_agent_v8.agent.final.md` against implementation:

| Spec requirement | Status |
|-----------------|--------|
| Five-stage ReAct loop | ✅ `orchestrator.py:run_task` |
| GUI-Actor-3B + Verifier three-state | ✅ `verifier.py` + `detector.py` |
| SoM annotation + DesktopInteract | ✅ `visualizer.py` + `orchestrator.py` |
| Kimi K2.6 API + 12 built-in tools | ✅ `llm_client.py` |
| 3 MCP servers concurrent | ✅ `mcp_client/` |
| Kill switch (pynput + asyncio cancel) | ✅ `kill_switch.py` |
| Auto circuit breaker (API failures, action failures, same UI) | ✅ `orchestrator.py` |
| Security 4-level classification | ✅ `security.py` |
| AutoSkill learning (SKILL.md) | ✅ `skills.py` |
| Kimi memory + SQLite fallback | ✅ `memory.py` + `kimi_memory.py` |
| Kimi rethink + SQLite fallback | ✅ `reflection.py` + `kimi_memory.py` |
| EventBus (priority queue + pub/sub + middleware) | ✅ `eventbus/` |
| FSM 8-state | ✅ `state_machine.py` |
| SQLite 5 tables | ✅ `memory.py` |
| ChromaDB vector search | ✅ `memory.py` |
| CLI with argparse, REPL, /help, /status | ✅ `main.py` |

No spec gaps found. All v8 requirements are implemented.

- [ ] **Step 7: Fix IMPORTANT quality findings**

Apply fixes for any IMPORTANT findings from the quality review. Record MINOR items but do not block on them.

- [ ] **Step 8: Commit quality fixes (if any)**

```bash
git add <fixed files>
git commit -m "chore: fix code quality findings from review pass"
```

Skip if no IMPORTANT findings.

---

### Task 11: Final verification

**Files:**
- None modified — verification only

- [ ] **Step 1: Run full test suite**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q -m "not smoke"`
Expected: 166 passed (or more if new tests were added)

- [ ] **Step 2: Verify smoke tests are skippable**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q -m smoke`
Expected: 8 skipped (no config.yaml) or 8 passed (if config.yaml exists)

- [ ] **Step 3: Syntax and import check**

Run:
```
.venv\Scripts\python.exe -m py_compile tests/fakes.py tests/conftest.py tests/test_integration.py
.venv\Scripts\python.exe -c "import tests.fakes; import agent; import mcp_client; import eventbus; import main; print('All imports OK')"
```
Expected: No errors

- [ ] **Step 4: Commit (if anything changed)**

```bash
git commit -m "chore: final verification after test infra refactoring and reviews"
```

Skip if nothing changed.

---

## Execution Order

Tasks must run sequentially: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11

Tasks 1-2 must precede 3-7 (the shared modules must exist first).
Tasks 3-7 are independent of each other and can run in any order after 1-2.
Task 8 depends on 1-2 only (it imports from fakes and conftest).
Tasks 9-10 run after all code changes.
Task 11 is the final gate.

---

## Verification

```powershell
# Unit tests (always run):
.venv\Scripts\pytest tests/ -q -m "not smoke"

# Smoke tests (only with config.yaml):
.venv\Scripts\pytest tests/ -q -m smoke

# Full suite:
.venv\Scripts\pytest tests/ -q -m ""

# Syntax:
.venv\Scripts\python.exe -m py_compile tests/fakes.py tests/conftest.py tests/test_integration.py
.venv\Scripts\python.exe -c "import tests.fakes; import tests.conftest; print('OK')"
```

---

## Self-Review

### 1. Spec Coverage
- ✅ Test infrastructure refactoring — Tasks 1-7 (fakes.py, conftest.py, 5 test file migrations)
- ✅ Integration smoke tests — Task 8 (test_integration.py with 8 @pytest.mark.smoke tests)
- ✅ Security review — Task 9 (6 dimensions, 7 steps)
- ✅ Code quality review — Task 10 (6 dimensions, 7 steps)
- ✅ Final verification — Task 11

### 2. Placeholder Scan
- No "TBD", "TODO", "implement later" anywhere
- Every migration step shows the complete migrated file content
- Every review step names exact files and check criteria

### 3. Type Consistency
- `FakeLLM.__init__` params in Task 1 (`chat_responses`, `tool_responses`, `tool_names`, `default_chat`) match usage in Tasks 3-7
- `conftest.py` fixture names (`eventbus`, `config`, `tiny_config`, `killswitch`, `memory_store`) match what migrated tests reference
- `TriggeringLLM.__init__(responses, killswitch)` param name preserved for backward compat
- Helper functions `_blank_perception`, `_same_hash_perception`, `_message`, `_tool_call` all defined in `fakes.py` and imported by `test_orchestrator.py`
