# RequestHumanHelp 人工交接工具 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 agent 增加 `RequestHumanHelp(question, options)` 本地工具：遇到登录/验证码等需要人类的步骤时模型调用它，CLI 弹出 ↑↓ 可选菜单（最后一项永远是可直接打字的 `type something`），人类的回答作为工具结果返回模型，任务带完整 history 继续。

**Architecture:** 工具调用即暂停——orchestrator 的 async 工具 handler 阻塞在同步 callback 上（与现有 `confirm_interactive` 同模式，不占额外线程），期间状态机转 `WAITING_HUMAN`；callback 由 `main.py` 注入，默认走 `CLIPresenter.ask_choice` → `agent/choice_menu.py` 的 msvcrt 原始按键菜单；非 TTY 退化为返回 None（视为取消）。回答原样交回模型，由模型按系统提示词判断继续或收尾。

**Tech Stack:** Python 3.12、asyncio、rich（Console/Status）、msvcrt（Windows 原始按键）、pytest + pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-07-11-request-human-help-design.md`（已批准）。本计划是其逐任务落地。

---

## File map

| 文件 | 责任 | 改动 |
|---|---|---|
| `agent/choice_menu.py` | msvcrt ↑↓ 菜单（纯渲染+按键循环，可被测试注入 getch） | 新建（Task 1） |
| `agent/tools.py` | 各本地工具的 JSON Schema 集中地 | 加 `REQUEST_HUMAN_HELP_SCHEMA`（Task 2） |
| `agent/orchestrator.py` | 工具注册、handler、状态迁移、系统提示词 | 加 callback/setter/`_register_human_help`/`_request_human_help_impl`/提示词新节（Task 2） |
| `agent/cli_presenter.py` | rich presenter；菜单前停 spinner、非 TTY 退化 | 加 `ask_choice`（Task 3） |
| `main.py` | 注入 human-question callback + 无 presenter 的 legacy 编号输入 | 加 `ask_human_interactive` + 接线（Task 3） |
| `tests/test_choice_menu.py` | 菜单单元测试 | 新建（Task 1） |
| `tests/test_orchestrator.py` | 工具 handler + 暂停/恢复 + 提示词锚点 | 扩展（Task 2） |
| `tests/test_cli_presenter.py` | presenter.ask_choice 退化与 spinner | 扩展（Task 3） |
| `tests/test_main_extra.py` | callback 接线、`_ReplAgent` 兼容 | 扩展（Task 3） |

**测试命令约定（本仓库）**：所有 pytest 都通过项目 venv 运行，且在 PowerShell 中执行（Bash 会把 Windows 路径的反斜杠吃掉）：

```powershell
.\.venv\Scripts\python.exe -m pytest <paths> -q --no-cov
```

**工作树注意**：开始前确认 `git status` 干净（或仅有与本功能无关的用户残留：orchestrator.py 的未使用 `from pathlib import Path`、`zhihu_*.md` 快照——不要触碰、不要提交它们；每个 commit 只 `git add` 本任务列出的文件）。

---

### Task 1: msvcrt 选择菜单（`agent/choice_menu.py`）

**Files:**
- Create: `agent/choice_menu.py`
- Test: `tests/test_choice_menu.py`（新建）

菜单契约：渲染 `需要你的帮助：{question}` + 提示行 + 空行 + N 个选项 + 最后一条 `type something: {typed}`；选中行前缀 `❯ `；最后一行被选中时输入缓冲后显示 `█`。返回选中文本/打的字；ESC 或 Ctrl+C 返回 None。空输入回车不提交。

- [ ] **Step 1: 写失败测试 `tests/test_choice_menu.py`**

```python
"""Tests for the msvcrt up/down choice menu."""

from __future__ import annotations

import io

from rich.console import Console

from agent.choice_menu import ask_choice


def _run(keys: list[str], options: list[str], question: str = "登录好了吗？"):
    """Drive ask_choice with a scripted key sequence; return (result, rendered)."""
    it = iter(keys)

    def fake_getch() -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("menu asked for more keys than scripted")

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=120)
    result = ask_choice(question, options, console, getch=fake_getch)
    return result, buf.getvalue()


def test_enter_selects_first_option():
    result, _ = _run(["\r"], ["是，已完成登录", "否，我暂时无法完成登录"])
    assert result == "是，已完成登录"


def test_down_enter_selects_second_option():
    result, _ = _run(
        ["\xe0", "P", "\r"], ["是，已完成登录", "否，我暂时无法完成登录"]
    )
    assert result == "否，我暂时无法完成登录"


def test_typing_auto_jumps_to_type_row():
    result, out = _run(
        list("帮我点跳过") + ["\r"], ["是，已完成登录", "否，我暂时无法完成登录"]
    )
    assert result == "帮我点跳过"
    assert "帮我点跳过" in out


def test_backspace_deletes_char():
    result, _ = _run(["a", "b", "\x08", "c", "\r"], ["opt1", "opt2"])
    assert result == "ac"


def test_empty_type_row_enter_is_ignored_then_esc_cancels():
    # Two options -> index 2 is the type row. Enter on the empty type row must
    # not submit; the following ESC cancels.
    result, _ = _run(["\xe0", "P", "\xe0", "P", "\r", "\x1b"], ["opt1", "opt2"])
    assert result is None


def test_esc_cancels():
    result, _ = _run(["\x1b"], ["opt1", "opt2"])
    assert result is None


def test_ctrl_c_cancels():
    result, _ = _run(["\x03"], ["opt1", "opt2"])
    assert result is None
```

- [ ] **Step 2: 运行确认失败**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_choice_menu.py -q --no-cov
```
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.choice_menu'`

- [ ] **Step 3: 实现 `agent/choice_menu.py`**

```python
"""Interactive up/down choice menu on the raw Windows console.

The agent asks the human a question (see ``RequestHumanHelp`` in
``agent/orchestrator.py``); this module renders the choices and reads the
answer. The last row is always a free-text ``type something`` entry the
user can type into directly. Only rendering + the key loop live here;
callers own TTY checks and spinner suspension. Keys are read through an
injectable ``getch`` so tests can script key sequences.
"""

from __future__ import annotations

from typing import Callable

from rich.console import Console

_TYPE_ROW_LABEL = "type something: "
_CURSOR = "█"


def _default_getch() -> str:
    import msvcrt  # Windows-only project; imported lazily for importability

    return msvcrt.getwch()


def ask_choice(
    question: str,
    options: list[str],
    console: Console,
    getch: Callable[[], str] | None = None,
) -> str | None:
    """Show an up/down menu and return the chosen text, or None on cancel.

    ``options`` are the model-provided choices; a free-text row is appended
    automatically as the last entry. Typing anywhere jumps to that row.
    Enter on an empty type row does nothing. ESC / Ctrl+C return None.
    """
    getch = getch or _default_getch
    write = console.file.write
    flush = console.file.flush

    selected = 0
    typed = ""
    n = len(options)

    def frame_lines() -> list[str]:
        lines = [
            f"需要你的帮助：{question}",
            "  (↑↓ 选择，回车确认；在最后一条直接打字，ESC 取消)",
            "",
        ]
        for i, opt in enumerate(options):
            marker = "❯ " if i == selected else "  "
            lines.append(f"{marker}{opt}")
        marker = "❯ " if selected == n else "  "
        cursor = _CURSOR if selected == n else ""
        lines.append(f"{marker}{_TYPE_ROW_LABEL}{typed}{cursor}")
        return lines

    def render(first: bool = False) -> None:
        lines = frame_lines()
        if not first:
            # Cursor sits len(lines) rows below the frame top; move back up.
            write(f"\x1b[{len(lines)}A")
        for line in lines:
            write("\x1b[2K" + line + "\n")
        flush()

    render(first=True)
    while True:
        ch = getch()
        if ch in ("\xe0", "\x00"):
            code = getch()
            if code == "H":  # up
                selected = max(0, selected - 1)
            elif code == "P":  # down
                selected = min(n, selected + 1)
            render()
            continue
        if ch == "\r":
            if selected < n:
                return options[selected]
            if typed:
                return typed
            continue  # empty type row: enter does nothing
        if ch == "\x08":
            if typed:
                typed = typed[:-1]
            render()
            continue
        if ch in ("\x1b", "\x03"):
            return None
        if ch.isprintable():
            typed += ch
            selected = n
            render()
            continue
        render()
```

注意：每帧 F 行逐行带 `\n` 输出，光标停在帧下方 F 行处；重绘用 `\x1b[{F}A` 回帧首、`\x1b[2K` 清行后覆写。F 恒定（选项不变）所以数学始终成立。直接写 `console.file` 绕过 rich 排版，避免 ANSI 被转义。

- [ ] **Step 4: 运行确认通过**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_choice_menu.py -q --no-cov
```
Expected: `7 passed`

- [ ] **Step 5: 提交**

```powershell
git add agent/choice_menu.py tests/test_choice_menu.py
git commit -m "feat(cli): add msvcrt up/down choice menu with inline typing"
```

---

### Task 2: orchestrator 工具 + handler + 系统提示词

**Files:**
- Modify: `agent/tools.py`（`COMPLETE_TASK_SCHEMA` 之后加 schema）
- Modify: `agent/orchestrator.py`（5 处，锚点见下）
- Test: `tests/test_orchestrator.py`（扩展）

handler 行为（spec §5.2）：清洗 options → 校验（question 非空、2≤options≤4）→ callback 为 None 返回 `[unavailable]` → 转 `WAITING_HUMAN` → 同步调 callback → `finally` 转回 `EXECUTING` → None 返回 `[cancelled]`，否则 `Human answered: {answer}`。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_orchestrator.py`，放在 `test_system_prompt_guides_complete_task` 之前）**

```python
# ---------------------------------------------------------------------------
# RequestHumanHelp (human handoff) tests
# ---------------------------------------------------------------------------

class _HumanHelpLLM(FakeLLM):
    """FakeLLM that routes RequestHumanHelp to the agent's real handler."""

    def __init__(self, agent: AgentOrchestrator, chat_responses: list[Any]) -> None:
        super().__init__(chat_responses)
        self._agent = agent

    async def execute_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in calls:
            args = json.loads(c.function.arguments)
            content = await self._agent._request_human_help_impl(**args)
            out.append({"role": "tool", "tool_call_id": c.id, "content": content})
        return out


def _wire_human_help(agent: AgentOrchestrator, llm: "_HumanHelpLLM") -> None:
    agent.llm = llm
    agent._register_human_help()  # adds "RequestHumanHelp" to llm.tool_names()


@pytest.mark.asyncio
async def test_request_human_help_pauses_and_resumes(config, eventbus, killswitch):
    scripted = [
        _message("需要登录", tool_calls=[_tool_call(
            "RequestHumanHelp",
            {"question": "是否已经手动完成登录？",
             "options": ["是，已完成登录", "否，我暂时无法完成登录"]},
        )]),
        _message("继续完成任务。"),
        _message("YES"),
        _message("热榜前三：……"),
    ]
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    _wire_human_help(agent, _HumanHelpLLM(agent, scripted))
    agent.set_human_question_callback(lambda q, o: "是，已完成登录")

    states: list[str] = []

    async def _rec(e: Any) -> None:
        if isinstance(e, AgentStateChanged):
            states.append(e.new_state)

    eventbus.subscribe("AgentStateChanged", _rec)

    result = await agent.run_task("总结知乎热榜")

    assert result == "热榜前三：……"
    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    assert any("Human answered: 是，已完成登录" in m["content"] for m in tool_msgs)
    assert "WAITING_HUMAN" in states
    # The handler restores EXECUTING after the human answers.
    assert "EXECUTING" in states[states.index("WAITING_HUMAN"):]


@pytest.mark.asyncio
async def test_request_human_help_cancel_returns_cancelled(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.set_human_question_callback(lambda q, o: None)
    await agent.state.transition("PLANNING", task_id="t1")
    await agent.state.transition("EXECUTING", task_id="t1")

    content = await agent._request_human_help_impl("q", ["a", "b"])

    assert content.startswith("[cancelled]")
    assert agent.state.current_state == "EXECUTING"  # restored after cancel


@pytest.mark.asyncio
async def test_request_human_help_without_callback_is_unavailable(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    content = await agent._request_human_help_impl("q", ["a", "b"])
    assert content.startswith("[unavailable]")


@pytest.mark.asyncio
async def test_request_human_help_rejects_bad_options(config, eventbus, killswitch):
    agent = AgentOrchestrator(
        config, eventbus, FakeLLM(), FakeMCP(), killswitch,
        perception=FakePerception([_blank_perception()]),
    )
    agent.set_human_question_callback(lambda q, o: "x")
    assert (await agent._request_human_help_impl("q", [])).startswith("[error]")
    assert (await agent._request_human_help_impl("q", ["a"])).startswith("[error]")
    assert (await agent._request_human_help_impl("", ["a", "b"])).startswith("[error]")
```

并在现有 `test_system_prompt_guides_complete_task` 末尾追加锚点：

```python
    # The human-handoff tool must be advertised in the system prompt.
    assert "RequestHumanHelp" in system_content
```

注意：`test_request_human_help_pauses_and_resumes` 用到 `AgentStateChanged`，而 `tests/test_orchestrator.py` 顶部**没有** eventbus 导入——必须补上这一行：

```python
from eventbus.events import AgentStateChanged
```

流程上：RequestHumanHelp 不触屏 → `_used_ui_tool` 保持 False → verify 阶段 YES 直接通过（`orchestrator.py` 的 `_verify` 对非 UI 任务信任 YES），4 条 LLM 消息恰好用完。

- [ ] **Step 2: 运行确认失败**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_orchestrator.py -q --no-cov -k "human_help or system_prompt"
```
Expected: FAIL — `AttributeError: 'AgentOrchestrator' object has no attribute '_request_human_help_impl'` / `_register_human_help`，以及 `"RequestHumanHelp" not in system_content`

- [ ] **Step 3: 实现（4 个文件编辑）**

**3a. `agent/tools.py`** — 在 `COMPLETE_TASK_SCHEMA`（第 50-64 行）之后、`CODERUNNER_SCHEMA` 之前插入：

```python
REQUEST_HUMAN_HELP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "The question shown to the human, e.g. "
                "'是否已经手动完成知乎登录？'."
            ),
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "2-4 mutually exclusive choices for the human. Do NOT include a "
                "free-text option: the CLI always appends 'type something' itself."
            ),
        },
    },
    "required": ["question", "options"],
}
```

**3b. `agent/orchestrator.py`** — 第 25 行 import 追加 schema：

```python
from agent.tools import (
    COMPLETE_TASK_SCHEMA,
    DESKTOP_INTERACT_SCHEMA,
    REQUEST_HUMAN_HELP_SCHEMA,
    register_all,
)
```

**3c. `agent/orchestrator.py`** — `__init__` 里 `self._human_confirm_callback: Any | None = None`（第 153 行）之后加一行：

```python
        self._human_question_callback: Any | None = None
```

并在 `set_human_confirmation_callback`（第 156-157 行）之后加 setter：

```python
    def set_human_question_callback(self, callback: Any) -> None:
        self._human_question_callback = callback
```

**3d. `agent/orchestrator.py`** — `initialize()` 里 `self._register_complete_task()`（第 179 行）之后加：

```python
        self._register_human_help()
```

并在 `_register_complete_task` 方法（约第 354-368 行）之后新增两个方法：

```python
    async def _request_human_help_impl(self, question: str, options: list) -> str:
        """Handler for RequestHumanHelp: ask the human and return their answer.

        The call itself is the pause: the ReAct loop blocks here (same thread
        model as confirm_interactive) with full history intact, the state
        machine shows WAITING_HUMAN, and the answer goes back to the model as
        the tool result. None from the callback means the human cancelled
        (ESC/Ctrl+C) or no human is present (non-TTY).
        """
        options = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not question or not (2 <= len(options) <= 4):
            return "[error] RequestHumanHelp requires a question and 2-4 options."
        callback = self._human_question_callback
        if callback is None:
            return (
                "[unavailable] No human is present to answer. End the task and "
                "explain what the user must do manually."
            )
        await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
        try:
            answer = callback(question, options)
        except Exception as exc:
            logger.warning("human question callback failed: %s", exc)
            answer = None
        finally:
            await self.state.transition("EXECUTING", task_id=self.task_id)
        if answer is None:
            return "[cancelled] The human dismissed the question without answering."
        return f"Human answered: {answer}"

    def _register_human_help(self) -> None:
        """Register the RequestHumanHelp local function tool with the LLM."""
        self.llm.register_local_function(
            "RequestHumanHelp",
            self._request_human_help_impl,
            schema=REQUEST_HUMAN_HELP_SCHEMA,
            description=(
                "Ask the human to perform a step you cannot do yourself (login, "
                "scan a QR code, solve a CAPTCHA, enter a 2FA code, OS permission "
                "dialog). The CLI shows your question with the given options plus "
                "a free-text option and returns the human's answer. Prefer this "
                "over retrying an action that keeps failing because it requires "
                "human involvement."
            ),
        )
```

**3e. `agent/orchestrator.py`** — `system_content`（约第 500-520 行）：在 `"Use DesktopInteract(label=N, ...) when you can see a SoM marker instead.\n\n"` 与 `"## Finishing a turn\n"` 两个字符串字面量之间插入新节：

```python
            "## Asking the human for help\n"
            "If a step needs a human — login, scanning a QR code, CAPTCHA, SMS/2FA "
            "codes, OS permission dialogs — call RequestHumanHelp(question, options) "
            "instead of retrying the failing action. Make the question specific (name "
            "the site or app) and give 2-4 options; the CLI always adds a free-text "
            "'type something' option, so never include one yourself.\n"
            "Reading the answer: if the human completed the step, look at the screen "
            "again and continue the original plan. If they could not complete it or "
            "the answer is unclear, stop and finish with a normal text answer that "
            "explains where the task is blocked and what the user must do manually.\n\n"
```

- [ ] **Step 4: 运行确认通过**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_orchestrator.py -q --no-cov
```
Expected: 全部通过（含原有 61 个 + 新增 4 个 + 锚点扩展）

- [ ] **Step 5: 提交**

```powershell
git add agent/tools.py agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): add RequestHumanHelp tool with WAITING_HUMAN pause"
```

---

### Task 3: presenter 菜单出口 + main 接线

**Files:**
- Modify: `agent/cli_presenter.py`（加 `ask_choice`）
- Modify: `main.py`（加 `ask_human_interactive` + 一行接线）
- Test: `tests/test_cli_presenter.py`、`tests/test_main_extra.py`（扩展）

- [ ] **Step 1: 写失败测试**

`tests/test_cli_presenter.py` 末尾追加：

```python
def test_ask_choice_non_tty_returns_none(monkeypatch):
    presenter, _ = _make_presenter()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("menu must not run on a non-TTY")

    monkeypatch.setattr("agent.choice_menu.ask_choice", boom)
    assert presenter.ask_choice("q", ["a", "b"]) is None


def test_ask_choice_suspends_spinner(monkeypatch):
    presenter, _ = _make_presenter()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    seen = {}

    def fake_menu(question, options, console):
        seen["status_during_menu"] = presenter._status
        return "是，已完成登录"

    monkeypatch.setattr("agent.choice_menu.ask_choice", fake_menu)
    presenter._start_status("Thinking…")
    assert presenter.ask_choice("q", ["a", "b"]) == "是，已完成登录"
    assert seen["status_during_menu"] is None  # spinner suspended while asking
    assert presenter._status is not None  # restored afterwards
    presenter._stop_status()
```

`tests/test_main_extra.py`：

① `_ReplAgent` 增加方法（紧挨现有 `set_human_confirmation_callback`，否则 `main()` 会 AttributeError）：

```python
    def set_human_question_callback(self, cb) -> None:
        self.help_cb = cb
```

② 文件末尾追加：

```python
# ---------------------------------------------------------------------------
# ask_human_interactive
# ---------------------------------------------------------------------------

def test_ask_human_interactive_non_tty_returns_none(monkeypatch):
    monkeypatch.setattr(main, "_presenter", None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main.ask_human_interactive("q", ["a", "b"]) is None


def test_ask_human_interactive_delegates_to_presenter(monkeypatch):
    seen = {}

    def fake_ask_choice(q, o):
        seen["call"] = (q, o)
        return "是"

    monkeypatch.setattr(main, "_presenter", SimpleNamespace(ask_choice=fake_ask_choice))
    assert main.ask_human_interactive("q", ["a", "b"]) == "是"
    assert seen["call"] == ("q", ["a", "b"])


def test_ask_human_interactive_legacy_number_choice(monkeypatch, capsys):
    monkeypatch.setattr(main, "_presenter", None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    assert main.ask_human_interactive("q", ["a", "b"]) == "b"


def test_ask_human_interactive_legacy_free_text(monkeypatch, capsys):
    monkeypatch.setattr(main, "_presenter", None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "帮我点跳过")
    assert main.ask_human_interactive("q", ["a", "b"]) == "帮我点跳过"
```

- [ ] **Step 2: 运行确认失败**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_presenter.py tests/test_main_extra.py -q --no-cov
```
Expected: FAIL — `AttributeError: 'CLIPresenter' object has no attribute 'ask_choice'` / `module 'main' has no attribute 'ask_human_interactive'`；且 REPL 相关测试会因 `_ReplAgent` 缺方法而失败（在补 ① 之前先观察也行；若先补了 ①，则只剩前两类失败）

- [ ] **Step 3: 实现**

**3a. `agent/cli_presenter.py`** — 顶部 import 区加（必须按模块导入，运行期查属性，测试才能 monkeypatch）：

```python
from agent import choice_menu
```

在 `mcp_status` 方法之后、`# -- status helpers` 之前加：

```python
    def ask_choice(self, question: str, options: list[str]) -> str | None:
        """Ask the human a multiple-choice question via the raw console menu.

        Returns the chosen/typed text, or None on cancel / non-TTY. Suspends
        the spinner exactly like confirm() so the menu owns the terminal.
        """
        self.console.print()  # separate the menu from tool output
        if not sys.stdin.isatty():
            self.console.print(
                "[dim]Non-interactive: cannot ask the human; skipping.[/]"
            )
            return None
        was_running = self._status is not None
        self._stop_status()
        try:
            return choice_menu.ask_choice(question, options, self.console)
        finally:
            if was_running:
                self._start_status("Thinking…")
```

**3b. `main.py`** — 在 `confirm_interactive`（结束于 `return answer in {"y", "yes"}`）之后插入：

```python
def ask_human_interactive(question: str, options: list[str]) -> str | None:
    """Human-question callback for RequestHumanHelp.

    Delegates to the presenter's up/down menu when one is installed;
    otherwise falls back to a numbered input() prompt. In a non-TTY it
    prints a warning and returns None (treated as "no answer").
    """
    if _presenter is not None:
        return _presenter.ask_choice(question, options)
    print(f"\n[human help] {question}")
    if not sys.stdin.isatty():
        print(
            "[warning] Non-interactive mode: cannot ask the human; "
            "treating the question as unanswered."
        )
        return None
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    print(f"  {len(options) + 1}. type something")
    try:
        answer = input("Choose a number or type a reply: ").strip()
    except EOFError:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    return answer or None
```

**3c. `main.py`** — `main()` 里 `agent.set_human_confirmation_callback(confirm_interactive)`（约第 224 行）之后加一行：

```python
        agent.set_human_question_callback(ask_human_interactive)
```

- [ ] **Step 4: 运行确认通过**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_presenter.py tests/test_main_extra.py -q --no-cov
```
Expected: 全部通过

- [ ] **Step 5: 提交**

```powershell
git add agent/cli_presenter.py main.py tests/test_cli_presenter.py tests/test_main_extra.py
git commit -m "feat(cli): wire human-question callback through presenter and main"
```

---

## 最终验证

- [ ] **全套回归**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q --no-cov -m "not smoke"
```
Expected: 全部通过（基线 327 + 本计划新增约 15 个），`8 deselected`

- [ ] **手动验收（终端实测，不可跳过）**

1. `python main.py` → 输入 `用浏览器打开知乎并总结热榜前三` → 遇到登录墙时模型应调用 RequestHumanHelp：
   - 菜单渲染：标题 `需要你的帮助：是否已经手动完成登录？`（或模型生成的等价措辞）+ 两个选项 + `type something`
   - ↑↓ 移动高亮；在任意位置直接打字（含**中文输入法**）→ 自动跳到最后一行并回显；退格删字；空输入回车无反应
   - 手动在浏览器完成登录后选 `是，已完成登录` → agent 重新观察屏幕并继续任务
2. 重跑任务，菜单出现后按 ESC → 任务不应崩溃，模型收尾
3. 菜单出现后按 Ctrl+C → 任务中止回到 IDLE（kill switch）
4. 管道退化：`"x" | python main.py --task "用浏览器打开知乎" --yes` → 不出现菜单、不阻塞，任务带说明结束

## 范围外（与 spec §9 一致）

被动 WAITING_HUMAN（失败阈值）接菜单、等待超时、问答持久化、选项循环滚动、>4 选项、更新 CLAUDE.md。
