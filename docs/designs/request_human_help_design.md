# RequestHumanHelp 人工交接工具 设计文档

> 状态：已批准（2026-07-11 brainstorming 确认）。实现计划由 writing-plans 产出。

## 1. 背景与问题

agent 执行长任务时会遇到**必须人类亲自操作**的步骤：应用/网站登录、扫码、CAPTCHA、短信 2FA、OS 权限弹窗。当前架构（`agent/orchestrator.py`）只有两种相关机制，都不够用：

- **动作审批** `confirm_interactive`（`main.py`）：审批 agent 自己的动作，不是"请人类代劳"。
- **失败兜底 WAITING_HUMAN**（`orchestrator.py:566-568`）：连续失败 N 次后状态转 `WAITING_HUMAN` 并**直接结束任务**；用户在 REPL 再输入会开启**全新任务**（`run_task` 重建 history），原计划上下文全部丢失。

缺口：模型不能**主动**判断"这一步需要人"，也没有**暂停当前任务 → 等人类操作 → 带完整 history 恢复**的通道。

## 2. 目标 / 非目标

**目标**
- 新增本地工具 `RequestHumanHelp(question, options)`：模型在需要人类时调用，CLI 弹出 ↑↓ 可选菜单，最后一个选项永远是 `type something`（可直接在该行打字）。
- 工具调用即暂停：菜单阻塞期间任务 history 完整保留，人类回答作为工具结果返回模型，循环继续。
- 登录场景示范：question="是否已经手动完成登录？"，options=["是，已完成登录", "否，我暂时无法完成登录"]（由模型按系统提示词生成，非硬编码）。
- 任何选项（含"否"和打字内容）都原样返回模型，由模型判断下一步（系统提示词引导：完成就继续，无法完成就结束任务说明原因）。
- 非 TTY / 管道模式优雅退化；kill switch 与取消语义清晰。

**非目标（YAGNI）**
- 失败阈值触发的被动 WAITING_HUMAN 不接本菜单（保持现状）。
- 不做等待超时、不做问答持久化、选项不做循环滚动、不支持 >4 个选项。
- 不更新 CLAUDE.md / 设计总文档（用户自维护）。

## 3. 已锁定的设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | **工具调用即暂停**，不新增 orchestrator 恢复逻辑 | `_execute_tool_calls` 本就 await 工具处理器；阻塞等人不破坏 history，恢复免费 |
| D2 | **手写 msvcrt 菜单**，不用 questionary | 用户要求"在 type something 选项后直接打字"；项目 Windows-only，`msvcrt` 稳定可用，零新依赖 |
| D3 | **"否"等所有答案原样交回模型** | 通用工具覆盖全部求助场景；模型可按上下文换路线或优雅结束 |
| D4 | `type something` 永远由 **CLI 自动追加** | 保证交互契约一致；提示词禁止模型自己提供该选项 |
| D5 | 菜单在**事件循环线程同步阻塞**（同 `confirm_interactive` 现状） | 等待人类时无需循环做其他事；Ctrl+C 由菜单 `\x03` 映射 + pynput 双路覆盖 |

## 4. 架构与数据流

```
模型输出 tool_call: RequestHumanHelp(question, options)
  → orchestrator._execute_tool_calls
    → llm.execute_tool_calls → 本地 handler _request_human_help_impl（async，llm_client.py:161-164 支持）
      → 校验参数（question 非空、2≤options≤4）
      → callback 未设置 → 返回 "[unavailable] ..."（无人可问）
      → state: EXECUTING → WAITING_HUMAN（presenter spinner: "Waiting for input…"）
      → answer = self._human_question_callback(question, options)   # 同步阻塞，同 confirm 模式
          → main.ask_human_interactive
            → presenter 存在 → CLIPresenter.ask_choice → choice_menu.ask_choice（msvcrt 菜单）
            → presenter 不存在 → 编号列表 + input() legacy fallback；非 TTY → None
      → state: WAITING_HUMAN → EXECUTING（finally 保证恢复）
      → answer is None → 工具结果 "[cancelled] The human dismissed the question without answering."
      → 否则 → 工具结果 "Human answered: {answer}"
  → 工具结果进入 history，模型继续循环；下一轮 perceive 重新观察屏幕
```

## 5. 组件规格

### 5.1 `agent/tools.py` — 新增 Schema

```python
REQUEST_HUMAN_HELP_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The question shown to the human, e.g. '是否已经手动完成知乎登录？'.",
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

### 5.2 `agent/orchestrator.py`

- `__init__`：新增 `self._human_question_callback: Any | None = None`；在 `_register_complete_task()`（`:178`）之后调用 `_register_human_help()`。
- `set_human_question_callback(self, callback)` —— 仿 `set_human_confirmation_callback`（`:155`）。
- `_register_human_help()` —— 仿 `_register_complete_task()`（`:354`）：

```python
self.llm.register_local_function(
    "RequestHumanHelp",
    self._request_human_help_impl,
    schema=REQUEST_HUMAN_HELP_SCHEMA,
    description=(
        "Ask the human to perform a step you cannot do yourself (login, scan a "
        "QR code, solve a CAPTCHA, enter a 2FA code, OS permission dialog). The "
        "CLI shows your question with the given options plus a free-text option "
        "and returns the human's answer. Prefer this over retrying an action "
        "that keeps failing because it requires human involvement."
    ),
)
```

- handler（async；`llm_client.execute_tool_calls` 会 `await handler(**args)`）：

```python
async def _request_human_help_impl(self, question: str, options: list) -> str:
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
```

注意：同步调用 callback（决策 D5）；`finally` 保证状态恢复；callback 抛错按取消处理并记日志（`llm_client` 外层还有 `[error]` 兜底）。

### 5.3 `agent/choice_menu.py`（新文件）—— 菜单本体

```python
def ask_choice(
    question: str,
    options: list[str],
    console: Console,
    getch: Callable[[], str] | None = None,   # 默认 msvcrt.getwch；测试注入
) -> str | None:
    """↑↓ 选择菜单；最后一条永远是 type something 且可直接打字。
    返回选中的选项文本 / 打的字；ESC 或 Ctrl+C 返回 None。"""
```

**渲染帧**（写入 `console.file`，raw ANSI；与批准的原型一致）：

```
需要你的帮助：{question}
  (↑↓ 选择，回车确认；在最后一条直接打字，ESC 取消)

  是，已完成登录
  否，我暂时无法完成登录
❯ type something: {typed}█
```

- 选中行前缀 `❯ `，其余 `  `；type 行固定标签 `type something: ` + 输入缓冲；仅当 type 行被选中时在缓冲末尾追加 `█`。
- 首帧逐行输出（每行带 `\n`）；之后每次按键：`\x1b[{F}A`（F=帧行数）回到帧首，逐行 `\x1b[2K` + 内容 + `\n` 重绘。提交/取消后补一个换行，帧保留在屏幕上。
- 不调 rich 排版：直接 `console.file.write` + `flush()`，避免 markup/换行干扰。此时 spinner 已被调用方停掉（§5.4），无并发写。

**按键循环**（`getch()` 返回宽字符 str）：

| 输入 | 行为 |
|---|---|
| `\xe0`/`\x00` 后 `H` | selected = max(0, selected-1) |
| `\xe0`/`\x00` 后 `P` | selected = min(len(options), selected+1)（len(options) 即 type 行） |
| `\xe0`/`\x00` 后其他 | 忽略（左右键） |
| 可打印字符 | `typed += ch` 且 selected 跳到 type 行（决策：任何位置直接打字都自动落到 type 行） |
| `\x08`（退格） | `typed = typed[:-1]`（空则不动） |
| `\r`（回车） | 选中普通选项 → 返回该选项文本；选中 type 行且 typed 非空 → 返回 typed；**type 行空缓冲回车忽略不提交** |
| `\x1b`（ESC）/ `\x03`（Ctrl+C） | 返回 None（取消） |
| 其他 | 忽略并重绘 |

每次状态变化后重绘一次。

### 5.4 `agent/cli_presenter.py` —— `CLIPresenter.ask_choice`

```python
def ask_choice(self, question: str, options: list[str]) -> str | None:
    self.console.print()  # 与工具输出分隔
    if not sys.stdin.isatty():
        self.console.print("[dim]Non-interactive: cannot ask the human; skipping.[/]")
        return None
    was_running = self._status is not None
    self._stop_status()          # 复用 confirm() 的 spinner 挂起模式
    try:
        return choice_menu.ask_choice(question, options, self.console)
    finally:
        if was_running:
            self._start_status("Thinking…")
```

（`from agent import choice_menu`；spinner 停启同线程，与 confirm 一致。）

### 5.5 `main.py` —— 接线与 legacy fallback

```python
def ask_human_interactive(question: str, options: list[str]) -> str | None:
    if _presenter is not None:
        return _presenter.ask_choice(question, options)
    print(f"\n[human help] {question}")
    if not sys.stdin.isatty():
        print("[warning] Non-interactive: cannot ask the human; treating as unanswered.")
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

`main()` 中在 `set_human_confirmation_callback(...)`（`:224`）之后加一行 `agent.set_human_question_callback(ask_human_interactive)`。

### 5.6 系统提示词（`orchestrator.py` 的 `system_content`，插在 `## Working with desktop tools` 与 `## Finishing a turn` 之间）

```
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

## 6. 行为矩阵

| 场景 | 行为 |
|---|---|
| REPL + TTY，选"是，已完成登录" | 工具结果 `Human answered: 是，已完成登录`；模型继续，下一轮重新 perceive |
| REPL + TTY，选"否…"/打字 | 文本原样返回；提示词引导模型结束任务并说明卡点 |
| REPL + TTY，ESC / Ctrl+C | 工具结果 `[cancelled] ...`；Ctrl+C 同时被 pynput 记录，循环下一轮 `_check_cancelled()` 中止任务 |
| 菜单期间 spinner | 调用前停、返回后恢复（"Thinking…"） |
| `--task` + TTY | 同 REPL（presenter 存在） |
| `--task` + 管道（非 TTY） | `ask_choice` 直接返回 None → `[cancelled]`；无 presenter 时 legacy 路径返回 None；任务由模型结束 |
| callback 未设置 | `[unavailable] ...`，提示模型收尾 |
| 模型传非法参数（options 空/超 4/含空串） | 清洗后校验失败 → `[error] RequestHumanHelp requires a question and 2-4 options.` |
| 模型把 "type something" 自己写进 options | 不额外去重（提示词禁止；最多菜单出现两条，可接受） |

## 7. 测试计划

**`tests/test_choice_menu.py`（新）** —— fake `getch` 喂按键序列，console 写 StringIO：

- `test_enter_selects_first_option`：`["\r"]` → options[0]
- `test_down_enter_selects_second_option`：`["\xe0","P","\r"]` → options[1]
- `test_typing_auto_jumps_to_type_row`：`list("帮我点跳过") + ["\r"]` → "帮我点跳过"，且输出帧含该文本
- `test_backspace_deletes_char`：`["a","b","\x08","c","\r"]` → "ac"
- `test_empty_type_row_enter_is_ignored`：移到 type 行 → `"\r"`（忽略）→ `"\x1b"` → None
- `test_esc_cancels` / `test_ctrl_c_cancels`：`["\x1b"]` / `["\x03"]` → None

**`tests/test_orchestrator.py`（扩展）** —— 仿 `_CompletingLLM` 加 `_HumanHelpLLM`，`execute_tool_calls` 路由到 `agent._request_human_help_impl(**args)`：

- `test_request_human_help_pauses_and_resumes`：LLM 脚本 [tool_call(RequestHumanHelp), "继续", "YES", "final"]；callback 返回 "是，已完成登录"；断言 history 工具消息含 `Human answered: 是，已完成登录`、result=="final"、事件序列出现 WAITING_HUMAN→EXECUTING
- `test_request_human_help_cancel_returns_cancelled`：callback 返回 None → 工具内容 `[cancelled]`
- `test_request_human_help_without_callback_is_unavailable`：不设 callback → `[unavailable]`
- `test_request_human_help_rejects_bad_options`：options=[] → `[error]`
- 系统提示词锚点：现有 `test_system_prompt_guides_complete_task` 增加 `assert "RequestHumanHelp" in system_content`

**`tests/test_cli_presenter.py`（扩展）**

- `test_ask_choice_non_tty_returns_none`：isatty=False → None 且不调用菜单
- `test_ask_choice_suspends_spinner`：isatty=True + monkeypatch `choice_menu.ask_choice` 记录 `presenter._status` → 期间为 None、返回后恢复

**`tests/test_main_extra.py`（扩展）**

- `_ReplAgent` 增加 `set_human_question_callback`（否则 `main()` AttributeError）
- `test_ask_human_interactive_non_tty_returns_none`：`_presenter=None` + isatty=False → None
- `test_ask_human_interactive_delegates_to_presenter`：monkeypatch `main._presenter` 为 stub → 透传 question/options

**手动验收**（不计入 pytest）：

1. `python main.py` → 任务"用浏览器打开知乎并总结热榜前三" → 登录墙触发菜单：实测 ↑↓、直接打字（含中文输入法）、退格、空输入回车无反应、ESC 取消、Ctrl+C 中止
2. 手动登录后选"是，已完成登录" → agent 重新观察并继续总结
3. 管道模式：`"x" | python main.py --task "…"` → 不出现菜单，任务带说明结束

## 8. 文件清单

| 文件 | 改动 |
|---|---|
| `agent/choice_menu.py` | 新建：msvcrt 菜单 |
| `agent/tools.py` | 新增 `REQUEST_HUMAN_HELP_SCHEMA` |
| `agent/orchestrator.py` | callback 字段/setter、`_register_human_help`、`_request_human_help_impl`、系统提示词新节 |
| `agent/cli_presenter.py` | `CLIPresenter.ask_choice` |
| `main.py` | `ask_human_interactive` + 接线 |
| `tests/test_choice_menu.py` | 新建 |
| `tests/test_orchestrator.py` / `tests/test_cli_presenter.py` / `tests/test_main_extra.py` | 扩展 |

## 9. 范围外

- 被动 WAITING_HUMAN（失败阈值）接菜单：未来迭代。
- 等待超时自动取消、问答历史持久化、选项循环滚动、>4 选项。
- 多人类/远程审批（手机推送等）。
