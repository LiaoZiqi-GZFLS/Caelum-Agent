# CLI UI Facelift Design (rich, event-driven)

- Date: 2026-07-11
- Status: Approved (brainstorming) → ready for implementation plan
- Scope: REPL + one-shot (`--task`) terminal presentation only

## Goal

Make the Caelum-Agent CLI feel like a modern coding agent (Claude-Code-like):
a startup banner, a styled input prompt, a live spinner through the
Perceive/Think/Act/Verify cycle, each tool call and its result rendered as it
happens, and the final answer rendered as markdown — without touching the ReAct
loop.

## Locked decisions (from brainstorming)

1. **Depth = light polish.** `rich` only. No `prompt_toolkit`, no token
   streaming, no persistent dashboard panel, no configurable theme.
2. **Logging in REPL = file only.** When the rich presenter is active, the
   console `StreamHandler` is not attached; logs still go to
   `data/logs/agent.log` (DEBUG included). Same for one-shot.
3. **Structure = event-subscribing presenter.** A new `CLIPresenter` subscribes
   to the existing EventBus events. The orchestrator is not modified.

## Why this is low-risk

The orchestrator already emits every event the UI needs:

| Event | Fired when | UI use |
|---|---|---|
| `UserInputReceived(text)` | task starts | (optional echo) |
| `AgentStateChanged(old, new)` | FSM transition | spinner text |
| `LLMResponseReceived(content, tool_calls)` | each LLM round | narration + queued tool lines |
| `ToolCallRequested(server, tool_name, arguments)` | before an MCP tool runs | `▶ tool(args)` line |
| `ToolCallCompleted(server, tool_name, result, success)` | after a tool runs | `✓/✗ tool — result` |
| `KillSwitchTriggered(reason)` | cancel | stop spinner |

Because presentation rides on events, the ReAct loop in
`agent/orchestrator.py` needs **zero** changes. The presenter is a pure
consumer: a rendering bug can never break a task.

## Architecture

```
main.py
  ├─ builds CLIPresenter(rich.Console)
  ├─ presenter.attach(eventbus)        # subscribe to the 6 events
  ├─ setup_logging(..., console=False) # file-only when presenter active
  ├─ REPL:  banner → Console.input("› ") → agent.run_task → Markdown(answer)
  └─ one-shot: header → agent.run_task → Markdown(answer)

orchestrator.run_task (unchanged)
  └─ emits events ─────────────────────▶ CLIPresenter renders live
```

`CLIPresenter` owns:
- one `rich.console.Console` (stdout; rich auto-detects Windows legacy console
  and non-TTY, downgrading color/Live accordingly);
- one `rich.status.Status` spinner context, created on the first LLM round of a
  task and stopped when the task returns or is cancelled;
- small pure helpers for argument/result truncation and a fixed theme.

## Rendering map

Reference rendering (colors/styles omitted in ASCII):

```
Caelum-Agent v0.1 · Kimi K2.6 · type /help for commands
› open notepad and type hello
⠋ Thinking…
  ▶ windows__Snapshot(use_ui_tree=True)
  ✓ Snapshot — 6.2KB, 14 elements
  ▶ windows__Click(label=5)
  ✓ Click — OK
⠋ Verifying…
╭─ Caelum ───────────────────────────────────────────╮
│ Done. Opened Notepad and typed "hello".            │
╰────────────────────────────────────────────────────╯
› _
```

Rules:
- `AgentStateChanged` → update spinner text only: `PLANNING`/`EXECUTING` →
  "Thinking…", `VERIFYING` → "Verifying…", `REFLECT` → "Reflecting…". No
  printed line (too noisy).
- `LLMResponseReceived`: if `content` non-empty → print it dim/italic as model
  narration. Tool-call lines come from `ToolCallRequested` / `ToolCallCompleted`
  (exactly one `▶`/`✓` per tool); `LLMResponseReceived.tool_calls` itself does
  not print anything.
- `ToolCallRequested` → print `▶ <server>__<tool>(<short-args>)`; set spinner
  to `Running <tool>…`.
- `ToolCallCompleted` → print `✓ <tool> — <first line of result>` in green on
  success, or `✗ <tool> — <reason>` in red on failure; set spinner back to
  "Thinking…".
- Task return → stop spinner; render the answer string with
  `rich.markdown.Markdown` inside a `rich.panel.Panel` titled "Caelum".
- Confirmations (`confirm_interactive`) → `rich.prompt.Confirm.ask` styled
  prompt; non-TTY keeps the existing deny-with-warning path (do **not** call
  `Confirm.ask` when stdin is not a TTY).

Truncation: argument summaries ≤ 60 chars; result first lines ≤ 120 chars. The
full result is never shown on screen; it remains in the audit log and LLM
history.

## File changes

- **Create `agent/cli_presenter.py`**
  - `CLIPresenter(console: Console | None = None)`
    - `attach(eventbus)` / `detach()` — subscribe/unsubscribe the six handlers.
    - `banner()` — print the startup banner.
    - `input(prompt="› ")` → styled input (wraps `Console.input`).
    - `print_answer(text)` → `Panel(Markdown(text))`.
    - `confirm(summary, action)` → `Confirm.ask`; falls back to deny on
      non-TTY.
    - private handlers `_on_user_input`, `_on_state`, `_on_llm`,
      `_on_tool_requested`, `_on_tool_completed`, `_on_kill`, each wrapped in
      `try/except` that logs to the `caelum.cli` file logger and never re-raises
      (a UI error must not abort a task).
  - Helpers: `_short_args(args) -> str`, `_first_line(text, n=120) -> str`,
    theme constants (colors for `▶`, `✓`, `✗`, narration, panel border).
- **Modify `main.py`**
  - After building `agent`, build `presenter = CLIPresenter()` and
    `presenter.attach(eventbus)`; detach in `finally`.
  - Print banner at REPL start (and a one-line header for one-shot).
  - Replace `input("> ")` with `presenter.input()`; replace `print(result)`
    with `presenter.print_answer(result)`.
  - Replace the body of `confirm_interactive`’s TTY branch with
    `presenter.confirm(...)`; keep the non-TTY deny path unchanged.
  - Pass `console=False` to `setup_logging` whenever the presenter is active
    (both REPL and one-shot).
- **Modify `agent/logging_config.py`**
  - Add `console: bool = True` keyword to `setup_logging`. When `False`, skip
    creating/attaching the console `StreamHandler` (file handler unchanged).
    Existing `if root.handlers: return` guard stays.
- **Modify `requirements.txt`**
  - Add `rich`. Pin: `rich>=13.7,<15` (lock the exact version during the plan
    after confirming the current stable on PyPI).

## Edge cases & compatibility

- **Non-TTY (CI, piped stdin/stdout):** rich auto-disables color and `Live`;
  the presenter degrades to plain printed lines. `confirm_interactive` keeps the
  current non-TTY deny path; do not invoke `Confirm.ask`.
- **One-shot (`--task`):** runs the same presenter (spinner + tool lines +
  markdown answer). `python main.py --task "..." | cat` must not crash and must
  not emit raw ANSI that breaks downstream (rich handles stripping when not a
  terminal).
- **Cancellation:** on `KillSwitchTriggered` and on task return, the spinner is
  stopped so the terminal is never left with a running `Live`.
- **Unicode/width:** `main.py` already reconfigures stdout/stderr to UTF-8;
  emoji/CJK width is delegated to rich.
- **Presenter exceptions:** every event handler is wrapped so a rendering error
  is logged to the file logger and swallowed; it cannot propagate into
  `run_task`.

## Testing

- **New `tests/test_cli_presenter.py`** — build a presenter with
  `Console(file=io.StringIO(), force_terminal=True, color_system="truecolor")`,
  drive it with fake events, and assert on the captured text:
  - `ToolCallRequested` → output contains the tool name and a `▶` marker.
  - `ToolCallCompleted(success=True)` → contains `✓` and the truncated result;
    `(success=False)` → contains `✗`.
  - final `print_answer` → captured text contains the answer string (Markdown
    render keeps the literal text).
  - `confirm` approve/deny via a monkeypatched `Confirm.ask` stub.
  - non-TTY confirm path returns False without prompting.
- **Extend `tests/test_main_extra.py`** — assert the banner is printed at REPL
  start and that, with the presenter active, the root logger has no
  `StreamHandler` (file handler still present).
- **Logging test** (extend existing or new `tests/test_logging_config.py`) —
  `setup_logging(console=False)` attaches no `StreamHandler`;
  `setup_logging(console=True)` attaches exactly one.

Existing tests must remain green; the orchestrator is untouched so
`tests/test_orchestrator.py` is unaffected.

## Verification (manual)

- `python main.py` → banner + `›` prompt. Type `hello` → spinner "Thinking…"
  → (model calls `CompleteTask`) → markdown answer, no tool lines. Type
  `open notepad and type 'hello'` → `▶ windows__Snapshot`, `✓`,
  `▶ windows__Click`, `✓`, spinner "Verifying…", final panel. No log lines
  appear on screen; `data/logs/agent.log` still grows.
- `python main.py --task "what is 2+2" --yes` → spinner then answer, exit 0.
- `python main.py --task "..." --yes | cat` → plain output, no ANSI crash.

## Out of scope (explicit, future work)

- `prompt_toolkit` input: history, slash-command completion, multi-line.
- Token streaming of the final answer (requires a streaming path in
  `LLMClient.chat`, which does not exist yet).
- Persistent live dashboard panel (status bar, token counters).
- Configurable theme/colors; user-toggleable verbosity.
