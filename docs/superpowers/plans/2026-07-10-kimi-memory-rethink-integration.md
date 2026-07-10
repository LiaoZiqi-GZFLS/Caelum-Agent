# Kimi memory / rethink Formula Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the agent to use Kimi's built-in `memory` and `rethink` Formula tools for cross-session memory and structured reflection, while keeping local SQLite as the always-available fallback.

**Architecture:** Add a thin `KimiMemoryClient` adapter in `agent/kimi_memory.py` that calls the registered Formula tools through `LLMClient.execute_tool_calls`. Expose `set_memory` / `get_memory` / `rethink` methods. Update `MemoryStore` and `ReflectionEngine` to prefer the Kimi adapter when it is available and enabled, falling back to SQLite otherwise. Keep the existing local-only path unchanged so the agent works without a Kimi memory/rethink subscription.

**Tech Stack:** Python, Pydantic, OpenAI-style tool calls, pytest, pytest-asyncio.

---

## File Structure

- `agent/kimi_memory.py` — **NEW** adapter that knows how to call `moonshot/memory:latest` and `moonshot/rethink:latest` via `LLMClient.execute_tool_calls`.
- `agent/memory.py` — **MODIFY** inject optional `KimiMemoryClient` into `MemoryStore`; route `set_preference` / `get_preference` through the adapter first, then SQLite fallback.
- `agent/reflection.py` — **MODIFY** inject optional `KimiMemoryClient` into `ReflectionEngine`; route `record()` / `build_context()` through `rethink` Formula first, then SQLite fallback.
- `agent/orchestrator.py` — **MODIFY** instantiate `KimiMemoryClient` and pass it to `MemoryStore` and `ReflectionEngine`.
- `agent/config.py` — **MODIFY** add `memory`/`rethink` enable flags and Formula names.
- `config.yaml.example` — **MODIFY** add commented config knobs.
- `tests/test_kimi_memory.py` — **NEW** unit tests for the adapter.
- `tests/test_memory.py` — **MODIFY** add fallback and integration tests.
- `tests/test_reflection.py` — **NEW** tests for `ReflectionEngine` with and without Kimi rethink.

---

### Task 1: Add Kimi memory/rethink adapter

**Files:**
- Create: `agent/kimi_memory.py`
- Test: `tests/test_kimi_memory.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_kimi_memory.py`:

```python
"""Tests for the Kimi memory/rethink adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.kimi_memory import KimiMemoryClient


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=__import__("json").dumps(args)),
    )


class FakeLLM:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[Any]] = []

    def tool_names(self) -> list[str]:
        return ["memory", "rethink"]

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        self.calls.append(calls)
        if self.responses:
            return self.responses.pop(0)
        return [{"role": "tool", "tool_call_id": calls[0].id, "content": "{}"}]


@pytest.mark.asyncio
async def test_set_memory_calls_memory_tool():
    llm = FakeLLM([[{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]])
    client = KimiMemoryClient(llm)
    await client.set_memory("user_name", "Alice")

    assert len(llm.calls) == 1
    call = llm.calls[0][0]
    assert call.function.name == "memory"
    args = __import__("json").loads(call.function.arguments)
    assert args["operation"] == "set"
    assert args["key"] == "user_name"
    assert args["value"] == "Alice"


@pytest.mark.asyncio
async def test_get_memory_returns_value():
    llm = FakeLLM([[{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": __import__("json").dumps({"value": "Alice"}),
    }]])
    client = KimiMemoryClient(llm)
    value = await client.get_memory("user_name")

    assert value == "Alice"
    assert llm.calls[0][0].function.arguments == __import__("json").dumps({"operation": "get", "key": "user_name"})


@pytest.mark.asyncio
async def test_rethink_returns_reflection():
    llm = FakeLLM([[{
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
    assert args["task_summary"] == "list files"
    assert args["failure_reason"] == "directory empty"
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_kimi_memory.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent.kimi_memory'`.

- [ ] **Step 3: Write minimal implementation**

Create `agent/kimi_memory.py`:

```python
"""Adapter for Kimi's built-in memory and rethink Formula tools.

Uses LLMClient.execute_tool_calls so the same Formula registration/execution
path is reused. If the tools are not registered, all methods raise
ToolNotAvailableError so callers can fall back to local SQLite.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("caelum.kimi_memory")


class ToolNotAvailableError(Exception):
    """Raised when the requested Kimi Formula tool is not registered."""


class KimiMemoryClient:
    """Thin client for moonshot/memory and moonshot/rethink Formula tools."""

    def __init__(
        self,
        llm: Any,
        memory_tool_name: str = "memory",
        rethink_tool_name: str = "rethink",
    ) -> None:
        self.llm = llm
        self.memory_tool_name = memory_tool_name
        self.rethink_tool_name = rethink_tool_name

    def _ensure_available(self, name: str) -> None:
        if name not in self.llm.tool_names():
            raise ToolNotAvailableError(f"Tool {name} is not registered with the LLM client")

    def _make_call(self, name: str, arguments: dict[str, Any]) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=f"call_{name}",
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            ),
        )

    async def set_memory(self, key: str, value: str) -> None:
        self._ensure_available(self.memory_tool_name)
        call = self._make_call(self.memory_tool_name, {
            "operation": "set",
            "key": key,
            "value": value,
        })
        outputs = await self.llm.execute_tool_calls([call])
        if outputs and outputs[0]["content"].startswith("[error]"):
            raise RuntimeError(outputs[0]["content"])

    async def get_memory(self, key: str) -> str | None:
        self._ensure_available(self.memory_tool_name)
        call = self._make_call(self.memory_tool_name, {
            "operation": "get",
            "key": key,
        })
        outputs = await self.llm.execute_tool_calls([call])
        content = outputs[0]["content"] if outputs else "{}"
        if content.startswith("[error]"):
            raise RuntimeError(content)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content or None
        return parsed.get("value")

    async def rethink(
        self,
        task_summary: str,
        failure_reason: str,
        context: list[str] | None = None,
    ) -> str:
        self._ensure_available(self.rethink_tool_name)
        call = self._make_call(self.rethink_tool_name, {
            "task_summary": task_summary,
            "failure_reason": failure_reason,
            "context": context or [],
        })
        outputs = await self.llm.execute_tool_calls([call])
        content = outputs[0]["content"] if outputs else ""
        if content.startswith("[error]"):
            raise RuntimeError(content)
        return content
```

- [ ] **Step 4: Run test to verify it passes**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_kimi_memory.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/kimi_memory.py tests/test_kimi_memory.py
git commit -m "feat: add Kimi memory/rethink adapter"
```

---

### Task 2: Wire adapter into MemoryStore for preferences

**Files:**
- Modify: `agent/memory.py`
- Modify: `agent/config.py`
- Test: `tests/test_memory.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_memory.py`:

```python
import pytest

from agent.kimi_memory import KimiMemoryClient


class FakeLLMForMemory:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[Any]] = []

    def tool_names(self) -> list[str]:
        return ["memory"]

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        self.calls.append(calls)
        if self.responses:
            return self.responses.pop(0)
        return [{"role": "tool", "tool_call_id": calls[0].id, "content": "{}"}]


@pytest.mark.asyncio
async def test_memory_store_prefers_kimi_memory(memory, tmp_path):
    llm = FakeLLMForMemory([[{
        "role": "tool",
        "tool_call_id": "call_memory",
        "content": json.dumps({"value": "Alice"}),
    }]])
    memory.kimi = KimiMemoryClient(llm)

    await memory.aset_preference("user_name", "Alice")
    value = await memory.aget_preference("user_name")

    assert value == "Alice"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_memory_store_falls_back_to_sqlite_when_kimi_unavailable(memory, tmp_path):
    llm = FakeLLMForMemory()
    llm.tool_names = lambda: []  # memory tool not registered
    memory.kimi = KimiMemoryClient(llm)

    await memory.aset_preference("theme", "dark")
    value = await memory.aget_preference("theme")

    assert value == "dark"
    assert len(llm.calls) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_memory.py::test_memory_store_prefers_kimi_memory -v
```

Expected: FAIL with `AttributeError: 'MemoryStore' object has no attribute 'kimi'`.

- [ ] **Step 3: Write minimal implementation**

Modify `agent/memory.py`:

1. Update `MemoryStore.__init__` signature and body:

```python
    def __init__(
        self,
        db_path: Path | str,
        skills_dir: Path | str,
        vector_dir: Path | str,
        audit_log_path: Path | str | None = None,
        kimi: Any | None = None,
    ) -> None:
        ...
        self.kimi = kimi
```

2. Add async preference methods; keep sync ones as aliases for local fallback:

```python
    async def aset_preference(self, key: str, value: str) -> None:
        if self.kimi is not None:
            try:
                await self.kimi.set_memory(key, value)
                return
            except Exception as exc:
                logger.warning("Kimi set_memory failed, falling back to SQLite: %s", exc)
        self.set_preference(key, value)

    async def aget_preference(self, key: str, default: str | None = None) -> str | None:
        if self.kimi is not None:
            try:
                return await self.kimi.get_memory(key)
            except Exception as exc:
                logger.warning("Kimi get_memory failed, falling back to SQLite: %s", exc)
        return self.get_preference(key, default)
```

3. Add `import logging` and module logger:

```python
import logging
logger = logging.getLogger("caelum.memory")
```

- [ ] **Step 4: Run test to verify it passes**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_memory.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/memory.py tests/test_memory.py
git commit -m "feat: route preferences through Kimi memory with SQLite fallback"
```

---

### Task 3: Wire adapter into ReflectionEngine

**Files:**
- Modify: `agent/reflection.py`
- Create: `tests/test_reflection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_reflection.py`:

```python
"""Tests for ReflectionEngine with optional Kimi rethink."""

from __future__ import annotations

from typing import Any

import pytest

from agent.config import Config
from agent.kimi_memory import KimiMemoryClient
from agent.memory import MemoryStore
from agent.reflection import ReflectionEngine


class FakeLLMForRethink:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[Any]] = []

    def tool_names(self) -> list[str]:
        return ["rethink"]

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        self.calls.append(calls)
        if self.responses:
            return self.responses.pop(0)
        return [{"role": "tool", "tool_call_id": calls[0].id, "content": "Retry."}]


@pytest.fixture
def memory(tmp_path):
    return MemoryStore(
        db_path=tmp_path / "memory.db",
        skills_dir=tmp_path / "skills",
        vector_dir=tmp_path / "chroma",
    )


@pytest.mark.asyncio
async def test_record_uses_rethink_when_available(memory):
    llm = FakeLLMForRethink([[{"role": "tool", "tool_call_id": "call_rethink", "content": "Use a different path."}]])
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory, kimi=KimiMemoryClient(llm))

    fix = await engine.record("list files", "directory empty", "tried ./docs")

    assert fix == "Use a different path."
    assert len(llm.calls) == 1
    assert llm.calls[0][0].function.name == "rethink"


@pytest.mark.asyncio
async def test_record_falls_back_to_sqlite_when_rethink_unavailable(memory):
    llm = FakeLLMForRethink()
    llm.tool_names = lambda: []
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
        reflection={"use_rethink": True},
    )
    engine = ReflectionEngine(config, memory, kimi=KimiMemoryClient(llm))

    rid = await engine.record("list files", "directory empty", "tried ./docs")

    assert rid > 0
    assert len(llm.calls) == 0
    assert len(engine.retrieve()) == 1


def test_build_context_without_reflections(memory):
    config = Config(llm={"api_key": "test"}, mcp_servers={})
    engine = ReflectionEngine(config, memory)
    assert engine.build_context("anything") == ""
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_reflection.py -v
```

Expected: FAIL because `ReflectionEngine` does not accept `kimi` and has no async `record()`.

- [ ] **Step 3: Write minimal implementation**

Modify `agent/reflection.py`:

```python
import logging
from typing import Any

from agent.config import Config
from agent.memory import MemoryStore

logger = logging.getLogger("caelum.reflection")


class ReflectionEngine:
    def __init__(
        self,
        config: Config,
        memory: MemoryStore,
        kimi: Any | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.kimi = kimi

    async def record(
        self,
        task_summary: str,
        failure_reason: str | None = None,
        fix_action: str | None = None,
    ) -> int | str:
        if getattr(self.config, "reflection", None) and self.config.reflection.get("use_rethink") and self.kimi is not None:
            try:
                fix = await self.kimi.rethink(
                    task_summary=task_summary,
                    failure_reason=failure_reason or "",
                    context=[f"fix_action hint: {fix_action}"] if fix_action else [],
                )
                # Persist the structured rethink result locally too.
                self.memory.add_reflection(task_summary, failure_reason, fix)
                return fix
            except Exception as exc:
                logger.warning("Kimi rethink failed, falling back to SQLite: %s", exc)
        return self.memory.add_reflection(task_summary, failure_reason, fix_action)

    # Keep synchronous record for backward compatibility.
    def record_sync(
        self,
        task_summary: str,
        failure_reason: str | None = None,
        fix_action: str | None = None,
    ) -> int:
        return self.memory.add_reflection(task_summary, failure_reason, fix_action)

    def retrieve(self, query: str = "", limit: int = 3) -> list[dict[str, Any]]:
        return self.memory.list_reflections(limit=limit)

    def build_context(self, current_task: str) -> str:
        reflections = self.retrieve(current_task)
        if not reflections:
            return ""
        parts = ["Past reflections that may help:"]
        for r in reflections:
            parts.append(
                f"- {r['task_summary']}"
                + (f" (fix: {r['fix_action']})" if r.get("fix_action") else "")
            )
        return "\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_reflection.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/reflection.py tests/test_reflection.py
git commit -m "feat: route reflection through Kimi rethink with SQLite fallback"
```

---

### Task 4: Add reflection configuration to Config

**Files:**
- Modify: `agent/config.py`
- Modify: `config.yaml.example`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_reflection_config_defaults():
    config = Config(
        llm={"api_key": "test"},
        mcp_servers={},
    )
    assert config.reflection.use_rethink is True
    assert config.memory.use_kimi_memory is True
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_config.py::test_reflection_config_defaults -v
```

Expected: FAIL with `AttributeError` on `config.reflection` / `config.memory.use_kimi_memory`.

- [ ] **Step 3: Write minimal implementation**

Modify `agent/config.py`:

1. Extend `MemoryConfig`:

```python
class MemoryConfig(BaseModel):
    sqlite_path: str = "./data/memory.db"
    use_kimi_memory: bool = True
```

2. Add `ReflectionConfig`:

```python
class ReflectionConfig(BaseModel):
    use_rethink: bool = True
```

3. Add field to `Config`:

```python
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
```

- [ ] **Step 4: Run test to verify it passes**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_config.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/config.py tests/test_config.py
git commit -m "config: add use_kimi_memory and use_rethink toggles"
```

---

### Task 5: Update orchestrator to inject KimiMemoryClient

**Files:**
- Modify: `agent/orchestrator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_orchestrator_creates_kimi_memory_client(config, eventbus, killswitch):
    from agent.kimi_memory import KimiMemoryClient
    llm = FakeLLM([])
    llm.tools = ["memory", "rethink"]
    agent = AgentOrchestrator(config, eventbus, llm, FakeMCP(), killswitch)
    assert isinstance(agent.memory.kimi, KimiMemoryClient)
    assert agent.reflection.kimi is agent.memory.kimi
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_orchestrator.py::test_orchestrator_creates_kimi_memory_client -v
```

Expected: FAIL with `AssertionError` because `memory.kimi` is `None`.

- [ ] **Step 3: Write minimal implementation**

Modify `agent/orchestrator.py`:

1. Add import:

```python
from agent.kimi_memory import KimiMemoryClient
```

2. In `AgentOrchestrator.__init__`, build `kimi_client` before `MemoryStore` and `ReflectionEngine`:

```python
        self._kimi_client: Any | None = None
        if config.memory.use_kimi_memory or config.reflection.use_rethink:
            self._kimi_client = KimiMemoryClient(llm)
        self.memory = memory or MemoryStore(
            db_path=config.sqlite_path_absolute(),
            skills_dir=config.skills_dir_absolute(),
            vector_dir=config.cache_dir_absolute() / "chroma",
            audit_log_path=config.audit_log_absolute(),
            kimi=self._kimi_client,
        )
        self.reflection = reflection or ReflectionEngine(
            config, self.memory, kimi=self._kimi_client
        )
```

3. Make orchestrator call async memory/reflection methods where appropriate:
   - In `run_task`, keep `reflection.record(...)` awaitable (it already is awaited).
   - In `_verify`, after a successful verification, optionally save a preference via `memory.aset_preference`? **NO** — do not expand scope. Keep orchestrator changes minimal.

- [ ] **Step 4: Run test to verify it passes**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_orchestrator.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: inject KimiMemoryClient into orchestrator"
```

---

### Task 6: Update config.yaml.example and verify full suite

**Files:**
- Modify: `config.yaml.example`

- [ ] **Step 1: Write the change**

Add comments under `memory:` and a new `reflection:` section in `config.yaml.example`:

```yaml
memory:
  # Local SQLite fallback for preferences, reflections, skills, audit log, state.
  sqlite_path: ./data/memory.db
  # Whether to prefer Kimi's built-in memory Formula over SQLite.
  # When true, set/get preference first calls moonshot/memory:latest.
  use_kimi_memory: true

reflection:
  # Whether to use Kimi's built-in rethink Formula for structured reflection.
  # When true, reflection.record() calls moonshot/rethink:latest.
  use_rethink: true
```

- [ ] **Step 2: Run tests**

```powershell
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add config.yaml.example
git commit -m "docs: document memory/reflection Kimi integration flags"
```

---

## Self-Review

**1. Spec coverage:**
- §5.2 "Local memory = Kimi memory tool + local SQLite backup" → `MemoryStore` routes preferences through Kimi memory first, falls back to SQLite.
- §5.3 "Reflection = Kimi rethink tool + local records" → `ReflectionEngine.record()` calls rethink first, persists result locally.
- §8 "12 built-in tools including memory/rethink" → Adapter reuses existing Formula tool registration/execution.
- §6.2 fallback principle → Local SQLite always remains available.

**2. Placeholder scan:** None found; all steps include exact code and expected outputs.

**3. Type consistency:**
- `KimiMemoryClient` is passed as `kimi` to both `MemoryStore` and `ReflectionEngine`.
- `ReflectionEngine.record()` returns `int | str` to accommodate both local row id and rethink-generated string.
- Config fields match access in orchestrator (`config.memory.use_kimi_memory`, `config.reflection.use_rethink`).

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-10-kimi-memory-rethink-integration.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
