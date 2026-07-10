# Caelum-Agent

Caelum-Agent is a personal Windows CLI desktop-operation agent. Give it natural-language instructions and it autonomously controls the browser and Windows desktop applications.

- **Platform:** Windows 10/11
- **Form:** CLI-only (no GUI, no CI/CD, no packaging in the initial version)
- **License:** BSD 3-Clause, Copyright (c) 2026 LiaoZiqi-GZFLS

---

## 中文简介

**Caelum-Agent** 是一个面向个人使用的 Windows 命令行桌面操作 Agent。你用自然语言下指令，它就能自动控制浏览器和 Windows 桌面应用——看图、理解界面、点击、输入、读写文件，全程自主完成。

- **平台：** Windows 10/11
- **形态：** 纯 CLI（首版无 GUI、无 CI/CD、不打包、不自动更新）
- **许可证：** BSD 3-Clause，Copyright (c) 2026 LiaoZiqi-GZFLS

### 核心特性

- **ReAct 闭环：** 感知（Perceive）→ 思考（Think）→ 行动（Act）→ 验证（Verify），失败时按需反思。
- **多模态感知：** 截图 + RapidOCR 文字识别 + UIA/A11y 无障碍树 + GUI-Actor-3B 元素检测（SoM 标注），四路融合成结构化环境描述交给大模型。
- **浏览器自动化：** 走 Playwright MCP，**无障碍树优先**，而非纯视觉。
- **桌面自动化：** 走 Windows-MCP，**UIA/A11y 优先**，必要时才回退到坐标/图像方式。
- **Kimi K2.6 大脑：** OpenAI 兼容 API，支持 Formula 工具与本地函数工具。
- **三个 MCP 并发：** Playwright / Windows / filesystem 在一个 asyncio 事件循环里同时跑，断线指数退避重连。
- **安全分级：** read / write_safe / write_risky / destructive 四级，危险与破坏性操作必须人工确认。
- **急停：** `Ctrl+C` / `/stop` / `/quit` 随时取消或优雅退出。
- **熔断：** 连续 API 失败、连续动作失败、同一 UI 原地打转都会被检测并处理。
- **AutoSkill 自学习：** 从成功任务轨迹自动生成 `SKILL.md` 技能。
- **记忆：** Kimi memory + rethink 工具，本地 SQLite 兜底。

### 技术栈

| 层 | 选型 |
|---|---|
| 大模型 | Kimi K2.6（`kimi-k2.6`，`https://api.moonshot.cn/v1`） |
| 浏览器控制 | Playwright MCP（`npx -y @playwright/mcp@latest`） |
| 桌面控制 | Windows-MCP（`windows-mcp serve`） |
| 文件系统 | `@modelcontextprotocol/server-filesystem` |
| UI 检测 | GUI-Actor-3B + Verifier（Transformers 原生推理） |
| OCR | RapidOCR（ONNXRuntime，CPU） |
| 本地记忆 | Kimi memory 工具 + SQLite 备份 |
| 状态机 / 事件总线 | 自研 8 状态 FSM + asyncio EventBus |

> 环境必须用 **Python 3.12**：GUI-Actor-3B 要求 `<3.13`，`windows-mcp` 要求 `>=3.12`。
> GUI-Actor-3B 是自定义架构，**不能**走 Ollama / GGUF / vLLM / llama.cpp，只能 Transformers 原生推理。

### 快速开始

```powershell
# 1) 首次初始化（建 venv、装依赖、生成 config.yaml、可选下载权重、跑 smoke）
python setup.py

# 2) 编辑 config.yaml，填入你的 Kimi API key（config.yaml 已被 gitignore，切勿提交）

# 3a) 交互式 REPL（推荐第一次，能看到每一步 感知→思考→行动→验证）
python main.py

# 3b) 单次任务（非交互），--yes 自动批准 write_risky 动作
python main.py --task "open notepad and type 'hello'" --yes

# 3c) 先关视觉跑通 LLM+MCP 链路（不加载 ~7.6GB 模型，启动快、省显存）
python main.py --no-vision --task "list the files on my desktop" --yes
```

建议顺序：先用 `--no-vision` 跑一次确认 Kimi + 三个 MCP 链路通，再去掉它加载 GUI-Actor 跑视觉定位。危险动作默认要在终端敲 `y` 确认；`--yes` **不**覆盖破坏性操作（需 `--yes-destructive` 才会，慎用）。

### 本地模型权重

GUI-Actor-3B 权重默认放在 `./models/gui-actor-3b/`（两个 safetensors 分片，约 7.6GB）。重新下载：

```powershell
# 国内推荐 GitHub Release 镜像
python setup.py --download-weights --weights-source github
# 或 HuggingFace（hf-mirror）
python setup.py --download-weights --weights-source huggingface
# 或 huggingface-cli 直拉
huggingface-cli download microsoft/GUI-Actor-3B-Qwen2.5-VL --local-dir ./models/gui-actor-3b
```

### 当前进度

按 v8 设计的所有模块均已落地并有测试：`agent/` 13 个核心模块 + `eventbus` + `mcp_client` + `ui_detector` + `main.py` + `setup.py`。仓库内无 TODO / `NotImplementedError` / 空 `pass` 桩；单元测试 **290 passed**、smoke（真实 API+MCP）**8 passed**、覆盖率约 **89%**（已排除 vendored 的 GUI-Actor 源码）。

定位仍是**可跑的 MVP 骨架**：真实任务级端到端（如“打开记事本输入 hello”的完整 ReAct）和 GUI-Actor 真实推理尚未做生产级打磨；`reflection` / `kimi_memory` 是有意的薄封装（设计上交给 Kimi 内置 `rethink` / `memory` 工具）。

详细设计见 [`docs/designs/desktop_agent_v8.agent.final.md`](docs/designs/desktop_agent_v8.agent.final.md)。

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
