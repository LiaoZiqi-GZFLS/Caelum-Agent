# Caelum-Agent

Caelum-Agent is a personal Windows CLI desktop-operation agent. Give it natural-language instructions and it autonomously controls the browser and Windows desktop applications.

- **Platform:** Windows 10/11
- **Form:** CLI-only (no GUI, no CI/CD, no packaging in the initial version)
- **License:** BSD 3-Clause, Copyright (c) 2026 LiaoZiqi-GZFLS

---

## Features

- **ReAct loop:** Perceive → Think → Act → Verify, with optional reflection on failure.
- **Multimodal perception:** screenshot + OCR + accessibility tree + GUI-Actor-3B SoM annotations.
- **Browser automation:** via Playwright MCP and accessibility-tree-first interactions.
- **Desktop automation:** via Windows-MCP and UIA/A11y-first interactions, falling back to coordinate/image methods.
- **Kimi K2.6 brain:** OpenAI-compatible API with Formula tools and local function tools.
- **Three concurrent MCP servers:** Playwright, Windows, and filesystem in one asyncio loop.
- **Security guard:** four-level classification (read / write_safe / write_risky / destructive) with mandatory human approval for risky and destructive actions.
- **Kill switch:** `Ctrl+C`, `/stop`, and `/quit` to cancel or exit gracefully.
- **Circuit breakers:** consecutive API failures, action failures, and same-UI-loop detection.
- **AutoSkill learning:** generates `SKILL.md` files from successful trajectories.
- **Memory:** Kimi memory + rethink tools with local SQLite fallback.

---

## Requirements

- Windows 10/11
- Python **3.12** (GUI-Actor-3B requires `<3.13`; `windows-mcp` requires `>=3.12`)
- Node.js with `npx` (for Playwright and filesystem MCP servers)
- Kimi API key from [Moonshot AI](https://platform.moonshot.cn/)

---

## Installation

```powershell
python setup.py
```

For non-interactive installs, pass the API key directly:

```powershell
python setup.py --api-key sk-...
```

To skip smoke tests (useful for CI or headless environments):

```powershell
python setup.py --skip-smoke-tests
```

If you are behind the Chinese firewall, set the Playwright download mirror before running setup:

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
python setup.py
```

---

## Configuration

`setup.py` copies `config.yaml.example` to `config.yaml` (gitignored). Edit `config.yaml` and add your real Kimi API key.

Key sections:

- `llm`: Kimi API key, model, optional `reasoning_effort`, and built-in Formula tools.
- `mcp_servers`: commands and arguments for Playwright, Windows, and filesystem MCP servers.
- `ui_detector`: GUI-Actor-3B model path, device, dtype, and verifier settings.
- `screenshot`: resolution, compression, and cropping strategy.
- `security`: auto-execute, confirm, and destructive-operation approval levels.
- `kill_switch`: API/action failure thresholds and same-UI-loop threshold.

Do **not** commit `config.yaml`.

---

## Usage

### Interactive REPL

```powershell
python main.py
```

Available REPL commands:

- `/help` — show commands
- `/status` — show current state, task ID, last action, failure counters, MCP health
- `/stop` — cancel the current task
- `/quit` — exit

### One-shot task mode

```powershell
python main.py --task "list files in E:/code/project"
```

In one-shot mode, risky actions normally prompt for an interactive `y/n`
approval. When stdin is not a TTY (scripts, CI, or piped input) the prompt is
replaced by a warning and the action is denied, so the task never hangs on
`input()`. To auto-approve in non-interactive scenarios:

```powershell
# Auto-approve write_risky actions (Click/Type/App/browser edits).
python main.py --task "open notepad" --yes

# Also auto-approve destructive actions, skipping typed confirmation. Use with caution.
python main.py --task "delete temp files" --yes-destructive
```

`--yes` does **not** cover destructive actions; they still require retyping the
action summary unless `--yes-destructive` is also passed.

### Disable vision/OCR for faster testing

```powershell
python main.py --no-vision --task "list files in E:/code/project"
```

---

## Development

Run all unit tests (excludes real API/MCP integration smoke tests):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q -m "not smoke"
```

Run smoke tests (requires `config.yaml` with valid credentials):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q -m smoke
```

Run the full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q -m ""
```

Quick syntax/import check:

```powershell
.\.venv\Scripts\python.exe -m py_compile agent/*.py eventbus/*.py mcp_client/*.py main.py setup.py
.\.venv\Scripts\python.exe -c "import agent; import mcp_client; import eventbus; import main; import setup"
```

End-to-end single task test:

```powershell
python spikes/e2e_single_task.py "open notepad"
```

---

## Project Structure

```
├── main.py                    # CLI entry point
├── config.yaml.example        # Configuration template
├── setup.py                   # First-run setup
├── agent/                     # Core agent modules
│   ├── config.py              # Pydantic configuration
│   ├── orchestrator.py        # ReAct loop orchestrator
│   ├── state_machine.py       # 8-state FSM
│   ├── perception.py          # Multimodal perception
│   ├── llm_client.py          # Kimi LLM client
│   ├── security.py            # Security policy guard
│   ├── kill_switch.py         # Global keyboard kill switch
│   ├── tools.py               # MCP tool mapper + CodeRunner
│   ├── snapshot_parser.py     # Accessibility tree parsers
│   ├── memory.py              # SQLite + ChromaDB store
│   ├── kimi_memory.py         # Kimi memory/rethink adapter
│   ├── reflection.py          # Reflection engine
│   ├── skills.py              # AutoSkill learning
│   └── logging_config.py      # Structured logging
├── eventbus/                  # Asyncio EventBus
├── mcp_client/                # Multi-server stdio MCP client
├── ui_detector/               # GUI-Actor-3B + verifier
├── skills/                    # SKILL.md skill library
├── tests/                     # pytest unit + smoke tests
└── spikes/                    # Spike/experiment scripts
```

---

## Architecture

### ReAct Loop

```
Perceive → Reflect (on failure/unknown UI only) → Think → Act → Verify
```

### Perception Fusion

```
Screenshot
    ├── PIL compression/crop
    ├── RapidOCR text recognition
    ├── UIA/A11y control tree ──┐
    │                              ├── Structured environment description → Kimi
    └── GUI-Actor-3B element detection → SoM annotation ──┘
```

### Concurrency

- Main loop: asyncio
- Visual inference thread pool: max 2 workers (GUI-Actor-3B)
- IO thread pool: max 8 workers (screenshots, file IO, MCP I/O)
- Kimi API calls: asyncio-native via httpx

---

## Important Constraints

- **Python 3.12 only** for the virtual environment. Do not use 3.13+ for the environment that loads GUI-Actor-3B.
- **GUI-Actor-3B must run via Transformers native inference.** It cannot be loaded through Ollama, GGUF, vLLM, or llama.cpp.
- **`moonshot/code-runner:latest` (hyphen) is the correct URI.** Formula URIs use hyphens (e.g. `web-search`, `code-runner`); registered tool names use underscores (`web_search`, `code_runner`). Code execution uses the local `RestrictedCodeRunner` sandbox by default; the Formula `code-runner` can be enabled as an alternative.
- **Browser automation uses accessibility tree first**, not pure vision.
- **Desktop automation uses UIA/A11y first**, falling back to coordinate/image methods only when needed.

---

## License

BSD 3-Clause, Copyright (c) 2026 LiaoZiqi-GZFLS.
See [LICENSE](LICENSE) for details.
