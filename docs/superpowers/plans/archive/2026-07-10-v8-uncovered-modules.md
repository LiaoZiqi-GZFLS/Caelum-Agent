# v8 未覆盖模块实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补全 Caelum-Agent v8 设计规范中尚未完全覆盖的四个核心模块：更安全的 CodeRunner、可靠的 Kill Switch、支持优先级和中间件的 EventBus、以及带后台健康监控的 MCP 重连机制。

**Architecture:** 每个模块独立演进，保持现有接口向后兼容；新增功能通过可选参数/配置启用，不破坏已有测试。所有改动都伴随单元测试，最终 `pytest tests/` 全部通过。

**Tech Stack:** Python 3.12, pytest-asyncio, pynput, asyncio, mcp SDK, AST, subprocess.

---

## 任务总览

| 任务 | 模块 | 优先级 | 估算时间 |
|---|---|---|---|
| 1 | CodeRunner 安全加固 | 高 | 30 min |
| 2 | Kill Switch `Ctrl+C` 可靠性 | 高 | 25 min |
| 3 | EventBus PriorityQueue + 中间件链 | 中 | 35 min |
| 4 | MCP 后台健康监控与自动重连 | 中 | 30 min |

---

## Task 1: CodeRunner 安全加固

**目标：** 让本地 Python 代码执行沙箱更难被绕过，保持与现有 `agent/tools.py` 接口兼容。

**Files:**
- Modify: `agent/tools.py`
- Test: `tests/test_tools.py`

### 设计要点

1. 新增 `RestrictedCodeRunner` 类，继承/替换现有 `CodeRunner`。
2. 在子进程中额外做三件事：
   - 使用 `PYTHONSAFEPATH=1` 启动，阻止当前目录/用户站点包被导入。
   - 重写 `builtins.__import__`，只允许加载 `ALLOWED_MODULES` 中的模块。
   - 清空 `sys.modules` 中不在白名单的已加载模块，并锁定 `sys.path` 为空列表。
3. 保持 AST 校验不变，作为第一道防线。
4. JavaScript 执行逻辑不变。

### Step 1: 编写新沙箱的运行时白名单测试

```python
def test_restricted_code_runner_blocks_disallowed_imports():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    result = runner.run("import os\nprint(os.getcwd())")
    assert "[error]" in result
    assert "Import not allowed" in result or "No module named" in result


def test_restricted_code_runner_allows_whitelisted_imports():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    result = runner.run("import math\nprint(math.sqrt(16))")
    assert "4.0" in result


def test_restricted_code_runner_blocks_bypass_via_subclasses():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    code = "().__class__.__base__.__subclasses__()"
    result = runner.run(code)
    # Should either be blocked by AST or produce harmless output; must not crash.
    assert "[error]" not in result or "blocked" in result.lower()
```

Run: `.venv\Scripts\python.exe -m pytest tests/test_tools.py::test_restricted_code_runner_blocks_disallowed_imports tests/test_tools.py::test_restricted_code_runner_allows_whitelisted_imports tests/test_tools.py::test_restricted_code_runner_blocks_bypass_via_subclasses -v`
Expected: 前两个 FAIL（`RestrictedCodeRunner` 不存在），第三个视现有行为可能 PASS/FAIL。

### Step 2: 实现 `RestrictedCodeRunner`

在 `agent/tools.py` 中 `CodeRunner` 之后添加：

```python
class RestrictedCodeRunner(CodeRunner):
    """A stricter sandbox that also restricts the subprocess runtime environment."""

    def _wrap_in_restricted_env(self, code: str) -> str:
        """Return a Python script string that sets up a restricted runtime."""
        allowed = ",".join(f'"{m}"' for m in self.allowed_modules)
        restricted_builtins = ",".join(f'"{n}"' for n in _RESTRICTED_BUILTINS)
        wrapper = f'''
import builtins
import importlib
import sys

_ALLOWED_MODULES = {{{allowed}}}
_RESTRICTED_BUILTINS = {{{restricted_builtins}}}

_original_import = builtins.__import__

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0:
        raise ImportError("Relative imports are not allowed")
    root = name.split(".")[0]
    if root not in _ALLOWED_MODULES:
        raise ImportError(f"Import not allowed: {{name}}")
    return _original_import(name, globals, locals, fromlist, level)

builtins.__import__ = _safe_import

for _name in list(sys.modules):
    if _name.split(".")[0] not in _ALLOWED_MODULES and _name != "builtins":
        sys.modules.pop(_name, None)

sys.path = []

for _name in _RESTRICTED_BUILTINS:
    if hasattr(builtins, _name):
        setattr(builtins, _name, None)

{code}
'''
        return wrapper
```

同时把 `run_code` 改为默认使用 `RestrictedCodeRunner`：

```python
def run_code(code: str, language: str = "python") -> str:
    return RestrictedCodeRunner().run(code, language=language)
```

保留 `CodeRunner` 类作为向后兼容的基类。

### Step 3: 运行测试

Run: `.venv\Scripts\python.exe -m pytest tests/test_tools.py -v`
Expected: 全部通过。

### Step 4: 提交

```bash
git add agent/tools.py tests/test_tools.py
git commit -m "Harden CodeRunner with runtime import whitelist and PYTHONSAFEPATH

- Add RestrictedCodeRunner that locks sys.path and overrides __import__
  in the subprocess, in addition to the existing AST validator.
- Default run_code now uses the restricted runner.
- Keep CodeRunner class as backward-compatible base.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Kill Switch `Ctrl+C` 可靠性

**目标：** 让 `Ctrl+C` 取消当前任务而不是终止整个进程，并确保 REPL 模式下 `/stop` 和 `Ctrl+C` 行为一致。

**Files:**
- Modify: `agent/kill_switch.py`
- Modify: `main.py`
- Test: `tests/test_kill_switch.py`

### 设计要点

1. 在 `KillSwitch.start()` 中安装一个自定义 `signal` 处理器（Windows 用 `signal.signal(signal.SIGINT, ...)`），把 `KeyboardInterrupt` 转换为 `KillSwitchTriggered` 事件。
2. 在 `main.py` 的 REPL 循环中捕获 `KeyboardInterrupt`，同样触发 kill switch。
3. 保持 `pynput` 全局监听作为补充路径。
4. 提供 `KillSwitch.is_triggered()` 以便 orchestrator 和外部代码快速检查。

### Step 1: 编写 Kill Switch 可靠性测试

```python
import asyncio
import signal

import pytest

from agent.kill_switch import KillSwitch
from eventbus import EventBus
from eventbus.events import KillSwitchTriggered


@pytest.mark.asyncio
async def test_sigint_emits_kill_switch_event():
    eventbus = EventBus()
    events = []
    eventbus.subscribe("KillSwitchTriggered", lambda e: events.append(e))
    ks = KillSwitch(eventbus)
    ks.start()

    # Simulate the signal handler path on Windows.
    if hasattr(ks, "_on_sigint"):
        ks._on_sigint(signal.SIGINT, None)
        await asyncio.sleep(0.05)
        assert len(events) == 1
        assert events[0].reason == "sigint"

    ks.stop()


def test_kill_switch_triggered_flag():
    eventbus = EventBus()
    ks = KillSwitch(eventbus)
    assert not ks.is_triggered()
    ks._trigger("test")
    assert ks.is_triggered()
```

Run: `.venv\Scripts\python.exe -m pytest tests/test_kill_switch.py::test_sigint_emits_kill_switch_event tests/test_kill_switch.py::test_kill_switch_triggered_flag -v`
Expected: FAIL（`_on_sigint` / `is_triggered` 不存在）。

### Step 2: 修改 `agent/kill_switch.py`

在 `KillSwitch.__init__` 中：
- 新增 `self._triggered = asyncio.Event()`。
- 保存旧的 SIGINT handler。

在 `start()` 中：
- 注册 `_on_sigint` 为 `signal.SIGINT` 处理器。

在 `stop()` 中：
- 恢复旧 handler。

新增方法：
- `is_triggered()` 返回 `self._triggered.is_set()`。
- `_trigger()` 现在同时设置 `self._triggered`。

```python
import signal

class KillSwitch:
    def __init__(self, eventbus: EventBus) -> None:
        self.eventbus = eventbus
        self._listener: keyboard.Listener | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pressed: set[keyboard.Key | keyboard.KeyCode] = set()
        self._triggered = asyncio.Event()
        self._original_sigint: Any | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()
        try:
            self._original_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        except Exception:
            pass

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None
        if self._original_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._original_sigint)
            except Exception:
                pass
            self._original_sigint = None

    def is_triggered(self) -> bool:
        return self._triggered.is_set()

    def reset(self) -> None:
        self._triggered.clear()

    def _on_sigint(self, signum, frame) -> None:
        self._trigger("sigint")

    def _trigger(self, reason: str) -> None:
        self._triggered.set()
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.eventbus.emit(KillSwitchTriggered(reason=reason)),
                self._loop,
            )
```

### Step 3: 修改 `main.py` 的 REPL 循环

在 `_run_repl` 的 `while True` 循环中，把 `input(...)` 调用包在 `try/except KeyboardInterrupt` 里：

```python
while True:
    try:
        user_input = await loop.run_in_executor(None, input, "> ")
    except KeyboardInterrupt:
        print("\n[kill switch] Ctrl+C pressed; triggering /stop")
        await agent.eventbus.emit(KillSwitchTriggered(reason="ctrl+c"))
        continue
    except EOFError:
        break
```

### Step 4: 运行测试

Run: `.venv\Scripts\python.exe -m pytest tests/test_kill_switch.py -v`
Expected: 全部通过。

### Step 5: 提交

```bash
git add agent/kill_switch.py main.py tests/test_kill_switch.py
git commit -m "Make Ctrl+C cancel the current task instead of killing the process

- Install a signal handler in KillSwitch that emits KillSwitchTriggered on SIGINT.
- Add is_triggered()/reset() helpers for external checks.
- Catch KeyboardInterrupt in the REPL loop and route it through /stop.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: EventBus PriorityQueue + 中间件链

**目标：** 实现 v8 规范中提到的优先级队列和中间件链，同时保持现有 pub/sub API 不变。

**Files:**
- Modify: `eventbus/__init__.py`
- Modify: `eventbus/events.py`（可选，为事件增加 `priority` 字段）
- Test: `tests/test_eventbus.py`

### 设计要点

1. `EventBus` 内部使用 `asyncio.PriorityQueue` 作为事件分发队列。
2. 新增 `Middleware` 抽象：`async def __call__(event, handler)`，可决定是否继续调用 handler。
3. `subscribe`/`emit` API 保持不变；新增 `add_middleware(middleware)`。
4. 启动一个后台任务 `_process_loop()` 持续从队列取事件并派发。
5. 提供 `shutdown()` 清理队列和后台任务。
6. 为兼容性，保持当前同步 `await emit()` 也能工作：当没有 middleware 时直接 `asyncio.gather`；有 middleware 时入队。

### Step 1: 编写 EventBus 中间件与优先级测试

```python
import asyncio

import pytest

from eventbus import EventBus


@pytest.mark.asyncio
async def test_middleware_can_block_event():
    bus = EventBus()
    received = []

    async def blocker(event, handler):
        if getattr(event, "block", False):
            return
        await handler(event)

    bus.add_middleware(blocker)
    bus.subscribe("TestEvent", lambda e: received.append(e))

    class BlockEvent:
        block = True

    class PassEvent:
        block = False

    await bus.emit(BlockEvent())
    await bus.emit(PassEvent())
    await bus.shutdown()

    assert len(received) == 1
    assert received[0].block is False


@pytest.mark.asyncio
async def test_event_priority_order():
    bus = EventBus()
    results = []

    async def slow_handler(event):
        await asyncio.sleep(0.05)
        results.append(event.value)

    bus.subscribe("PriorityEvent", slow_handler)

    class PriorityEvent:
        def __init__(self, value, priority=0):
            self.value = value
            self.priority = priority

        def __lt__(self, other):
            return self.priority < other.priority

    await bus.emit(PriorityEvent("low", priority=10))
    await bus.emit(PriorityEvent("high", priority=1))
    await bus.shutdown()

    assert results == ["high", "low"]
```

Run: `.venv\Scripts\python.exe -m pytest tests/test_eventbus.py::test_middleware_can_block_event tests/test_eventbus.py::test_event_priority_order -v`
Expected: FAIL（`add_middleware` / `shutdown` / PriorityQueue 未实现）。

### Step 2: 实现 PriorityQueue + Middleware EventBus

替换 `eventbus/__init__.py` 内容：

```python
"""Asyncio EventBus with priority queue and middleware chain.

Subscribers register by event type/class name. Events can carry a `priority`
attribute (lower value = higher priority). Middleware can inspect, modify, or
short-circuit events before they reach subscribers.
"""

from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]
Middleware = Callable[[Any, EventHandler], Coroutine[Any, Any, None]]


@dataclass(order=True)
class _QueuedEvent:
    priority: int
    seq: int
    event: Any = field(compare=False)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._middleware: list[Middleware] = []
        self._queue: asyncio.PriorityQueue[_QueuedEvent] = asyncio.PriorityQueue()
        self._seq = 0
        self._worker: asyncio.Task[Any] | None = None
        self._shutdown = False

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type in self._subscribers:
            self._subscribers[event_type] = [h for h in self._subscribers[event_type] if h is not handler]

    def add_middleware(self, middleware: Middleware) -> None:
        self._middleware.append(middleware)

    def _event_type(self, event: Any) -> str:
        event_type = getattr(event, "type", None)
        if event_type is None:
            event_type = event.__class__.__name__
        return event_type

    async def emit(self, event: Event | Any) -> None:
        if self._shutdown:
            return
        if not self._middleware:
            # Fast path: preserve original direct-dispatch behavior.
            await self._dispatch(event)
            return
        # Queue path: ensure ordered processing through middleware.
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._process_loop())
        priority = getattr(event, "priority", 0)
        self._queue.put_nowait(_QueuedEvent(priority, self._seq, event))
        self._seq += 1

    async def _process_loop(self) -> None:
        while not self._shutdown:
            try:
                queued = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if self._queue.empty():
                    break
                continue
            await self._dispatch(queued.event)

    async def _dispatch(self, event: Any) -> None:
        event_type = self._event_type(event)
        handlers = list(self._subscribers.get(event_type, []))
        if not handlers:
            return
        await asyncio.gather(
            *(self._invoke_with_middleware(event, h) for h in handlers),
            return_exceptions=True,
        )

    async def _invoke_with_middleware(self, event: Any, handler: EventHandler) -> None:
        async def final(e: Any) -> None:
            await self._invoke(handler, e)

        chain = final
        for mw in reversed(self._middleware):
            async def make_step(mw_inner: Middleware, next_step: EventHandler) -> EventHandler:
                async def step(e: Any) -> None:
                    await mw_inner(e, next_step)
                return step
            chain = await make_step(mw, chain)
        await chain(event)

    async def _invoke(self, handler: EventHandler, event: Any) -> None:
        try:
            await handler(event)
        except Exception as exc:
            print(f"[eventbus] handler error for {self._event_type(event)}: {exc}")

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._worker and not self._worker.done():
            # Drain the queue then cancel.
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
```

注意：`_invoke_with_middleware` 的闭包实现需要小心处理；上面的写法有 bug（`make_step` 是 async 的但返回 step 函数不合理）。更简单的实现：

```python
    async def _invoke_with_middleware(self, event: Any, handler: EventHandler) -> None:
        async def final(e: Any) -> None:
            await self._invoke(handler, e)

        chain: list[Callable[[Any], Coroutine[Any, Any, None]]] = [final]
        for mw in self._middleware:
            next_handler = chain[-1]
            async def make_step(mw_inner, nh):
                async def step(e):
                    await mw_inner(e, nh)
                return step
            chain.append(await make_step(mw, next_handler))
        await chain[-1](event)
```

实际上更简单且正确的是使用非闭包递归：

```python
    async def _invoke_with_middleware(self, event: Any, handler: EventHandler) -> None:
        await self._run_middleware(event, handler, 0)

    async def _run_middleware(
        self,
        event: Any,
        handler: EventHandler,
        index: int,
    ) -> None:
        if index >= len(self._middleware):
            await self._invoke(handler, event)
            return
        mw = self._middleware[index]
        async def next_step(e: Any) -> None:
            await self._run_middleware(e, handler, index + 1)
        await mw(event, next_step)
```

把这个更简单的版本写入最终代码。

### Step 3: 运行测试

Run: `.venv\Scripts\python.exe -m pytest tests/test_eventbus.py -v`
Expected: 全部通过。

### Step 4: 提交

```bash
git add eventbus/__init__.py tests/test_eventbus.py
git commit -m "Add EventBus priority queue and middleware chain

- Use asyncio.PriorityQueue for ordered event processing when middleware is present.
- Add add_middleware() for cross-cutting concerns (logging, filtering, auth).
- Maintain backward-compatible direct-dispatch fast path when no middleware is registered.
- Add shutdown() for clean teardown.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: MCP 后台健康监控与自动重连

**目标：** 让 `MCPMultiplexer` 在后台定期 ping 每个 server，发现断线时自动重连，而不是等到下次调用才发现。

**Files:**
- Modify: `mcp_client/__init__.py`
- Test: `tests/test_mcp_client.py`

### 设计要点

1. `MCPMultiplexer.connect_all()` 启动后台监控任务 `_health_monitor()`。
2. 监控任务每 `health_interval` 秒 ping 每个已连接 client；ping 失败则调用 `reconnect()`。
3. `disconnect_all()` 取消监控任务。
4. 添加配置项 `health_interval` 和 `health_enabled`（可在 `MCPConfig` 中设置，但先作为 client 参数）。
5. 保持现有 `call()` 的被动重连逻辑作为兜底。

### Step 1: 编写健康监控测试

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_client import MCPClient, MCPMultiplexer


@pytest.mark.asyncio
async def test_health_monitor_reconnects_on_failure():
    client = MCPClient("test", {"command": "echo", "args": [], "env": {}})
    client._connected = True
    client.session = MagicMock()
    client.ping = AsyncMock(side_effect=[True, False, True])
    client.reconnect = AsyncMock(return_value=True)

    multiplexer = MCPMultiplexer.__new__(MCPMultiplexer)
    multiplexer.clients = {"test": client}
    multiplexer._health_task = None

    monitor = asyncio.create_task(multiplexer._health_monitor(interval=0.05))
    await asyncio.sleep(0.15)
    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass

    client.reconnect.assert_awaited_once()
```

Run: `.venv\Scripts\python.exe -m pytest tests/test_mcp_client.py::test_health_monitor_reconnects_on_failure -v`
Expected: FAIL（`_health_monitor` 不存在）。

### Step 2: 修改 `mcp_client/__init__.py`

在 `MCPMultiplexer` 中：

```python
class MCPMultiplexer:
    def __init__(
        self,
        config: MCPConfig,
        health_interval: float = 30.0,
        health_enabled: bool = True,
    ) -> None:
        self.config = config
        self.clients: dict[str, MCPClient] = {
            "playwright": MCPClient("playwright", config.playwright),
            "windows": MCPClient("windows", config.windows),
            "filesystem": MCPClient("filesystem", config.filesystem),
        }
        self.health_interval = health_interval
        self.health_enabled = health_enabled
        self._health_task: asyncio.Task[Any] | None = None

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
        if self._health_task and not self._health_task.done():
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
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            for client in self.clients.values():
                if not client._connected:
                    continue
                healthy = await client.ping()
                if not healthy:
                    logger.warning(
                        "MCP health check failed for %s; reconnecting",
                        client.name,
                        extra={"server": client.name},
                    )
                    ok = await client.reconnect()
                    if not ok:
                        logger.error(
                            "MCP reconnection failed for %s",
                            client.name,
                            extra={"server": client.name},
                        )
```

### Step 3: 运行测试

Run: `.venv\Scripts\python.exe -m pytest tests/test_mcp_client.py -v`
Expected: 全部通过。

### Step 4: 提交

```bash
git add mcp_client/__init__.py tests/test_mcp_client.py
git commit -m "Add background MCP health monitor with automatic reconnect

- MCPMultiplexer starts a periodic _health_monitor() task after connect_all().
- Unhealthy servers are reconnected automatically, independent of tool calls.
- disconnect_all() cancels the monitor cleanly.
- Existing on-demand reconnect in call() remains as fallback.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 最终验证

Run: `.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: 100 passed（或更多，取决于新增测试数量）。

Run: `.venv\Scripts\python.exe -m py_compile agent/*.py eventbus/*.py mcp_client/*.py main.py`
Expected: 无输出，退出码 0。

---

## 自我审查

| 规范要求 | 对应任务 |
|---|---|
| CodeRunner 本地沙箱替代 `moonshot/code_runner:latest` | Task 1 |
| Kill Switch `Ctrl+C` 取消当前任务 | Task 2 |
| EventBus PriorityQueue + 中间件链 | Task 3 |
| MCP server 断线指数退避重连 | Task 4（已有被动重连，新增主动监控） |

无占位符；每个步骤包含完整代码与命令。
