# Test Infrastructure Refactoring, Integration Smoke Tests & Code Review

> **Status:** Design approved. Ready for implementation planning.

**Goal:** Consolidate scattered test fakes into a shared module, add real-API integration smoke tests, and conduct security + code quality reviews on the full codebase.

**Architecture:** Extract all Fake classes from 6 test files into `tests/fakes.py`, create `tests/conftest.py` with shared pytest fixtures and markers, add `tests/test_integration.py` with `@pytest.mark.smoke` gating behind `config.yaml` presence, then run two sequential review passes (security → code quality) with categorized findings.

**Tech Stack:** pytest 9.x, pytest-asyncio, Python 3.12

---

## File Structure

| File | Responsibility |
|------|---------------|
| `tests/fakes.py` | **New** — canonical `FakeLLM`, `FakeMCP`, `FakeKillSwitch`, `FakeReflection`, `FakePerception`, `FakeSkillLearner` |
| `tests/conftest.py` | **New** — shared fixtures (`eventbus`, `config`, `tiny_config`, `killswitch`, `memory_store`), pytest markers (`smoke`, `slow`) |
| `tests/test_integration.py` | **New** — `@pytest.mark.smoke` tests for Kimi API, LLMClient, MCP connections, config loading, orchestrator lifecycle |
| `tests/test_kimi_memory.py` | **Modify** — drop local `FakeLLM`, import from `fakes` |
| `tests/test_memory.py` | **Modify** — drop local `FakeLLMForMemory`, import from `fakes`; drop local `memory` fixture, use conftest |
| `tests/test_reflection.py` | **Modify** — drop local `FakeLLMForRethink`, import from `fakes`; drop local `memory` fixture, use conftest |
| `tests/test_skills.py` | **Modify** — drop local `FakeLLM`, import from `fakes`; drop local `memory` fixture, use conftest |
| `tests/test_orchestrator.py` | **Modify** — drop all local Fake classes, import from `fakes`; drop local `config` fixture, use conftest |
| `tests/test_ui_detector.py` | **Unchanged** |
| All other test files | **Unchanged** (except import adjustments where conftest fixtures replace local ones) |

---

## Section 1: Test Infrastructure Refactoring

### 1.1 `tests/fakes.py` — Canonical Fakes

A single `FakeLLM` class replaces five variants scattered across the test suite. It supports three modes that compose cleanly:

```python
class FakeLLM:
    """Scripted fake LLM usable as both chat client and tool executor.

    Three modes (need not be mutually exclusive):
    1. Chat mode: queue `chat_responses` (ChatCompletion-like objects or Exceptions)
    2. Tool mode: queue `tool_responses` (list of tool-result dicts)
    3. Combined: queue both — the orchestrator uses both chat() and execute_tool_calls()
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
        self.calls: list[list[dict[str, Any]]] = []
        self.last_tools: list[Any] = []
        self.tools: list[str] = []

    # --- chat() — orchestrator + skills tests ---
    async def chat(self, messages: list[dict[str, Any]], tools: Any = None) -> Any:
        self.calls.append(messages)
        self.last_tools.append(tools)
        response: Any
        if self._chat_index < len(self._chat_queue):
            response = self._chat_queue[self._chat_index]
        elif self._default_chat is not None:
            response = self._default_chat
        else:
            raise RuntimeError(f"FakeLLM ran out of chat responses after {self._chat_index} calls")
        self._chat_index += 1
        if isinstance(response, Exception):
            raise response
        return response

    # --- execute_tool_calls() — kimi_memory + reflection + orchestrator ---
    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        if self._tool_index < len(self._tool_queue):
            result = self._tool_queue[self._tool_index]
            self._tool_index += 1
            return result
        # Default: return empty results for each call.
        return [{"role": "tool", "tool_call_id": call.id, "content": "{}"} for call in calls]

    # --- register_* for orchestrator ---
    def register_function_tools(self, tools: list[dict[str, Any]]) -> None:
        for t in tools:
            self.tools.append(t["function"]["name"])

    def register_local_function(self, name: str, fn: Any, **kwargs: Any) -> None:
        self.tools.append(name)

    def tool_names(self) -> list[str]:
        return self.tools

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass
```

Other fake classes moved from `test_orchestrator.py`:

- `FakeMCP` — in-memory MCP multiplexer with `set_result()` and call recording
- `FakeKillSwitch` — eventbus-backed kill switch, never actually listens to pynput
- `FakeReflection` — records reflection entries in a list
- `FakePerception(PerceptionModule)` — replays pre-baked Perception objects
- `FakeSkillLearner` — records `(task, trajectory)` tuples
- `TriggeringLLM(FakeLLM)` — fires the kill switch on first `chat()` call

### 1.2 `tests/conftest.py` — Shared Fixtures & Markers

```python
import pytest
from pathlib import Path

from agent.config import Config
from eventbus import EventBus
from tests.fakes import FakeKillSwitch


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: real API/MCP integration tests (needs config.yaml)")
    config.addinivalue_line("markers", "slow: tests that take >5 seconds")


@pytest.fixture
def eventbus():
    return EventBus()


@pytest.fixture
def tiny_config(tmp_path: Path) -> Config:
    """Minimal Config for unit tests that don't need full orchestrator setup."""
    return Config(
        llm={"provider": "kimi", "base_url": "https://api.moonshot.cn/v1", "api_key": "test", "model": "kimi-k2.6"},
        mcp_servers={},
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={"skills_dir": str(tmp_path / "skills"), "cache_dir": str(tmp_path / "cache"), "audit_log": str(tmp_path / "audit.log")},
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Full Config with all MCP stubs — used by orchestrator tests."""
    return Config(
        llm={"provider": "kimi", "base_url": "https://api.moonshot.cn/v1", "api_key": "test", "model": "kimi-k2.6", "enable_builtin_tools": False, "builtin_tools": []},
        mcp_servers={"playwright": {"command": "npx", "args": [], "env": {}}, "windows": {"command": "windows-mcp", "args": ["serve"], "env": {}}, "filesystem": {"command": "npx", "args": [], "env": {}}},
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={"skills_dir": str(tmp_path / "skills"), "cache_dir": str(tmp_path / "cache"), "audit_log": str(tmp_path / "audit.log")},
        security={"default_level": "read", "auto_execute_levels": ["read", "write_safe"], "confirm_levels": ["write_risky"], "destructive_requires_approval": True},
    )


@pytest.fixture
def killswitch(eventbus):
    return FakeKillSwitch(eventbus)


@pytest.fixture
def memory_store(tmp_path: Path):
    """Shared MemoryStore backed by tmp_path."""
    from agent.memory import MemoryStore
    return MemoryStore(db_path=tmp_path / "memory.db", skills_dir=tmp_path / "skills", vector_dir=tmp_path / "chroma")
```

### 1.3 Migrating Existing Test Files

Each affected test file drops its local Fake classes and duplicate fixtures, importing from `tests.fakes` and relying on `conftest.py` fixtures instead.

**`test_orchestrator.py`** is the largest migration. The local `_make_config(tmp_path)` helper is replaced by the shared `config` fixture. Helper functions `_blank_perception()`, `_same_hash_perception()`, `_message()`, `_tool_call()` remain local — they are test-specific builders, not reusable fakes.

**Backward compatibility guarantee:** All existing tests pass without modification after migration. Fake interfaces are unified but not narrowed — every method call that worked before still works.

---

## Section 2: Integration Smoke Tests

### 2.1 `tests/test_integration.py`

All tests are marked `@pytest.mark.smoke` and auto-skip if `config.yaml` is absent:

```python
import pytest
from pathlib import Path

pytestmark = pytest.mark.smoke

def _has_config() -> bool:
    return Path("config.yaml").exists()

requires_config = pytest.mark.skipif(not _has_config(), reason="config.yaml not found")
```

### 2.2 Test Catalog

| # | Test | What it verifies |
|---|------|-----------------|
| 1 | `test_smoke_kimi_chat_roundtrip` | `LLMClient.chat()` sends a single-turn message, returns non-empty content |
| 2 | `test_smoke_kimi_tool_calls` | `LLMClient.chat()` with web-search tool registered returns a `tool_calls` response |
| 3 | `test_smoke_kimi_bad_key` | Invalid API key produces a 401 `AuthenticationError` |
| 4 | `test_smoke_mcp_connect_all` | `MCPMultiplexer.connect_all()` connects 3 servers without error |
| 5 | `test_smoke_mcp_each_server_lists_tools` | Each connected server returns >0 tools from `list_tools()` |
| 6 | `test_smoke_mcp_disconnect_clean` | `disconnect_all()` runs without hanging or error |
| 7 | `test_smoke_config_loads` | `load_config()` reads the real `config.yaml` and validates all sections |
| 8 | `test_smoke_orchestrator_lifecycle` | `AgentOrchestrator.initialize()` → `shutdown()` clean cycle (no task) |

**Runtime expectation:** ~10s wall clock. Tests 1-3 require a valid Kimi API key. Tests 4-6 require `npx` and `windows-mcp` available on PATH.

### 2.3 Running Smoke Tests

```powershell
# Default (skip smoke tests):
.venv\Scripts\pytest tests/ -q

# Smoke tests only:
.venv\Scripts\pytest tests/ -q -m smoke

# Everything including smoke:
.venv\Scripts\pytest tests/ -q -m ""
```

---

## Section 3: Security Review

### 3.1 Dimensions & Checkpoints

| Dimension | Check | Files |
|-----------|-------|-------|
| **Secret leakage** | API key never logged, printed, or written to disk; key masked in error messages | `llm_client.py`, `config.py`, `orchestrator.py`, `main.py` |
| **Tool-call authorization** | Security levels correctly assigned: `Click`→`write_risky` (needs confirm), `read_file`→`read` (auto); destructive tools blocked without approval | `security.py`, `tools.py` |
| **Injection via tool arguments** | User text passed to MCP calls via `json.dumps` — check for unescaped control characters or argument smuggling | `orchestrator.py:_execute_tool_calls` |
| **Kill switch reliability** | `Ctrl+C`/`/stop` works regardless of FSM state; no deadlock paths; `_cancel_event` checked in all await points | `kill_switch.py`, `orchestrator.py` |
| **Filesystem boundaries** | Filesystem MCP restricted to configured directories; no `../` escape; `allowed_dirs` validated at startup | `config.py` |
| **Code execution sandbox** | `CodeRunner` — `exec()` has time/memory limits? Can it access filesystem? Is it restricted to a temp directory? | `tools.py` |

### 3.2 Deliverable

A markdown report listing each finding with: severity (CRITICAL / IMPORTANT / MINOR), file path, line number, description, and fix recommendation. CRITICAL and IMPORTANT findings require approval before fixing.

---

## Section 4: Code Quality Review

### 4.1 Dimensions

| Dimension | What to check |
|-----------|---------------|
| **Large files** | `orchestrator.py` (710 lines), `test_orchestrator.py` (1076 lines) — can they be split without breaking invariants? |
| **Duplicate code** | Already addressed by Section 1 (5 FakeLLMs consolidated). Check for remaining duplication in `_build_description`, orchestrator success-path returns, MCP result formatting |
| **Interface consistency** | All `Perception.som_annotations` entries carry `verdict` field? All `ToolResult` consumers check `.success`? Tool name conventions consistent (PascalCase `DesktopInteract` vs snake_case MCP names)? |
| **Error handling coverage** | Compare `try/except` sites against actual error patterns — are there silent `pass` blocks masking real errors? Do fallback paths degrade gracefully? |
| **Type annotations** | `Any` usage patterns — where can `Any` be narrowed without breaking existing bindings? Are public method signatures fully annotated? |
| **Spec conformance** | Cross-reference `docs/designs/desktop_agent_v8.agent.final.md`: all mandated components present? All required behaviors implemented? Any v8 requirements that were dropped or deferred? |

### 4.2 Deliverable

Same format as security review: categorized findings with severity, file, line, description, fix recommendation. CRITICAL and IMPORTANT require approval; MINOR can be batch-fixed.

---

## Execution Order

1. **Test infrastructure refactoring** — `fakes.py` + `conftest.py` + migrate all test files
2. **Integration smoke tests** — `test_integration.py` with `@pytest.mark.smoke`
3. **Security review** → fix CRITICAL + IMPORTANT findings
4. **Code quality review** → fix CRITICAL + IMPORTANT findings
5. **Final `pytest tests/ -q`** — all 166+ tests pass, 0 regressions

Steps 1–2 are independent. Step 3 must precede Step 4 (security fixes may change behavior).

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
- ✅ Test infrastructure refactoring — Section 1
- ✅ Integration smoke tests — Section 2
- ✅ Security review — Section 3 (6 dimensions, deliverable format specified)
- ✅ Code quality review — Section 4 (6 dimensions, deliverable format specified)

### 2. Placeholder Scan
- No TBD, TODO, "implement later" found
- All code blocks are concrete
- All file paths are exact

### 3. Type Consistency
- `FakeLLM` interface is fully specified — `chat()`, `execute_tool_calls()`, `register_*()`, `tool_names()` — and matches all 5 existing call sites
- `conftest.py` fixture names match what test files currently import/use (`eventbus`, `config`, `killswitch`, `memory_store`)
- Smoke test names use `test_smoke_` prefix for discoverability
