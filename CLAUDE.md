# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current repository state

A minimal implementation skeleton exists and is covered by unit tests. Key files:

- `main.py` — CLI entry point.
- `agent/` — Core modules: config, LLM client, orchestrator, state machine, perception, security, kill switch, tools, memory, reflection, skills (AutoSkill learning).
- `eventbus/` — Asyncio EventBus and event dataclasses.
- `mcp_client/` — Multi-server stdio MCP client.
- `ui_detector/` — OmniParser YOLO icon detection and SoM visualization.
- `skills/` — `SKILL.md` skill library.
- `tests/` — pytest unit tests.
- `agent/snapshot_parser.py` — Parse Windows-MCP / Playwright accessibility snapshots into `UIElement` trees.
- `agent/logging_config.py` — Structured logging to console + rotating files.
- `setup.py` — First-run setup (venv, deps, optional weight download, smoke tests).
- `requirements-dev.txt` — pytest dev dependencies.

Design documents live under `docs/designs/` (currently untracked). The authoritative technical spec is `docs/designs/desktop_agent_v8.agent.final.md`.

## Project overview

**Caelum-Agent** is a personal Windows CLI desktop-operation agent. Users give natural-language instructions and the agent autonomously controls the browser and Windows desktop applications.

- Platform: Windows 10/11
- Form: CLI-only (no GUI, no CI/CD, no packaging, no auto-updates in the initial version)
- License: BSD 3-Clause, Copyright (c) 2026 LiaoZiqi-GZFLS

## Intended tech stack

The v8 design doc specifies the following stack:

| Layer | Choice |
|-------|--------|
| LLM brain | Kimi K2.6 API (`kimi-k2.6`, base URL `https://api.moonshot.cn/v1`) |
| Browser control | Playwright MCP (`npx -y @playwright/mcp@latest`) |
| Desktop control | Windows-MCP (`windows-mcp serve`, official CursorTouch package) |
| Filesystem control | `@modelcontextprotocol/server-filesystem` (`npx -y ... <allowed-dir>`) |
| UI detection | OmniParser `icon_detect` YOLOv8 (ultralytics) + SoM annotation |
| OCR | RapidOCR (ONNXRuntime, DirectML GPU with CPU fallback) |
| Screenshots | mss + Pillow compression/cropping |
| Local memory | Kimi memory tool + local SQLite backup |
| Reflection | Kimi rethink tool + local records |
| State machine | Custom 8-state FSM |
| Event bus | Custom asyncio EventBus |
| MCP multiplexing | `mcp` Python SDK `stdio_client`, 3 concurrent stdio connections in one asyncio loop |
| Kill switch | pynput global keyboard listener + asyncio task cancellation |

Important environment constraint: `windows-mcp` v0.8.2 requires Python `>=3.12`. Use **Python 3.12** for the project virtual environment.

## Project structure

Implemented layout:

```
desktop-agent/
├── main.py                    # CLI entry point
├── config.yaml                # User config (gitignored)
├── config.yaml.example        # Configuration template
├── requirements.txt           # Python dependencies
├── requirements-dev.txt       # pytest dev dependencies
├── setup.py                   # First-run initialization
├── agent/                     # Agent core
│   ├── __init__.py
│   ├── config.py              # Pydantic configuration
│   ├── llm_client.py          # Kimi LLM client (Formula + local function tools)
│   ├── orchestrator.py        # ReAct loop orchestrator
│   ├── state_machine.py       # FSM
│   ├── perception.py          # Multimodal perception
│   ├── security.py            # Security policy guard
│   ├── kill_switch.py         # Global keyboard kill switch
│   ├── tools.py               # MCP tool mapper + CodeRunner
│   ├── snapshot_parser.py     # Accessibility tree parsers
│   ├── logging_config.py      # Structured logging
│   ├── memory.py              # SQLite + ChromaDB store
│   ├── kimi_memory.py         # Kimi memory Formula tool client
│   ├── reflection.py          # Reflection engine
│   ├── skills.py              # AutoSkill learning (SKILL.md generation/merge)
│   ├── file_reader.py         # ReadDocument: binary docs via Kimi Files API (file-extract)
│   ├── media.py               # ViewMedia: image/video upload with native ms:// rendering
│   ├── content_writer.py      # DraftContent: writer subagent tool (Partial Mode prefill)
│   ├── task_list.py           # Model-managed task list tool for long-task coherence
│   ├── preview_points.py      # PreviewPoints: numbered markers for raw coordinate guesses
│   ├── history_archive.py     # Flight recorder: per-task JSONL history archive
│   ├── choice_menu.py         # msvcrt keyboard choice menu (RequestHumanHelp)
│   ├── self_window.py         # Own console window hide/show (SelfWindow)
│   ├── focus_guard.py         # Foreground focus watchdog (FocusGuard)
│   └── cli_presenter.py       # CLI output presenter
├── ui_detector/               # OmniParser YOLO detection + SoM visualization
│   ├── __init__.py
│   ├── yolo_detector.py       # YoloDetector: ultralytics icon_detect wrapper
│   ├── fusion.py              # fuse_annotations: OCR+YOLO box merge/dedup
│   └── visualizer.py          # visualize_som: numbered boxes on screenshots
├── mcp_client/                # MCP multi-server stdio client
│   ├── __init__.py
├── eventbus/                  # Asyncio EventBus and event definitions
│   ├── __init__.py
│   └── events.py
├── skills/                    # SKILL.md skill library (auto-learned skills go in skills/learned/)
├── tests/                     # pytest unit tests
└── data/                      # Local data (memory.db, cache/, archives/)
```

## Configuration

Run `python setup.py` to create `config.yaml` from the example and optionally inject your Kimi API key interactively. `config.yaml` is gitignored; never commit secrets.

```powershell
python setup.py
```

For non-interactive installs, pass the key directly:

```powershell
python setup.py --api-key sk-...
```

To skip smoke tests (useful for CI or headless environments):

```powershell
python setup.py --skip-smoke-tests
```

Key sections in `config.yaml`:
- `llm`: Kimi API key, model (`kimi-k2.6`), optional `reasoning_effort`, and which Formula tools to register.
  - Do **not** set `reasoning_effort="none"` for `kimi-k2.6`; omit it or use `minimal/low/medium/high`.
  - `moonshot/code-runner:latest` (hyphen) is the correct URI and is available; the registered tool name is `code_runner` (underscore). The local `RestrictedCodeRunner` remains the default code execution backend; enable the Formula `code-runner` as an alternative if you prefer Kimi-side execution.
  - `enable_file_extract` / `enable_media_upload`: toggle the ReadDocument / ViewMedia tools (both default true).
- `mcp_servers`: commands and arguments for Playwright, Windows, and filesystem MCP servers.
- `yolo`: OmniParser YOLO model path, device, confidence, image size, auto-compensation.
- `screenshot`: resolution, compression, and cropping strategy.
- `security`: auto-execute, confirm, and destructive-operation approval levels.

## Local function tools

Beyond MCP tools, the orchestrator registers these local tools on the LLM client (`agent/`):

| Tool | Module | Purpose |
|------|--------|---------|
| `CodeRunner` | `tools.py` | Sandboxed local Python; JavaScript only with `--yes`/`--yes-all` |
| `DesktopInteract` | `orchestrator.py` | Vision-based interaction for any app: pass `label=<marker number>` from the SoM-annotated screenshot (numbered red boxes from fused OCR text + YOLO icon boxes; each perception lists every marker's number and content — OCR text / icon flag); clicks/types at that marker's center, resolved against the LAST perception — no fresh detection per click. Only valid when the perception shows an annotated image; use NearbyLabels/ZoomRegion/PreviewPoints when no marker fits |
| `PreviewPoints` | `orchestrator.py` + `preview_points.py` | Last-resort locator: 1-3 guessed coordinates (screenshot space) drawn as numbered markers on a clean screenshot copy and shown back to the model, which adjusts then clicks via `windows__Click(loc=...)`; replace semantics per call |
| `UpgradeVision` | `orchestrator.py` + `perception.py` | Switch screenshots to the ORIGINAL (full native) resolution for the rest of the task when the model can't read small text; injects a fresh full-res perception immediately; reset per task |
| `ZoomRegion` | `orchestrator.py` + `perception.py` | Re-perceive a native-resolution crop centered on a `label` or `loc` (sizes small/medium/large ≈ 480/960/1680 native px): fresh OCR + YOLO + dual images for that region. Coordinates auto-translated via the region origin (screen = origin + coord × area / image), so DesktopInteract/loc keep working; the next perception round resets to full screen |
| `NearbyLabels` | `orchestrator.py` | Pure-geometry helper: list the k nearest SoM annotations to a given label or loc (screenshot-space distance, nearest first), so the model can triangulate a point near a known marker without another detection pass |
| `CompleteTask` | `orchestrator.py` | Model-decided fast path: finish without verification |
| `RequestHumanHelp` | `orchestrator.py` + `choice_menu.py` | Interactive TTY question with selectable options |
| `UpdateTaskList` | `task_list.py` | Model-managed pending/in_progress/completed task list; self-clears when done |
| `ReadDocument` | `file_reader.py` | Binary docs (PDF/DOCX/PPTX/EPUB/XLSX) via Kimi Files API `file-extract`, paginated, sha256-cached; returns a `doc:<sha8>` ref |
| `DraftContent` | `content_writer.py` | Writer subagent for long-form content (persona + Partial Mode prefill), writes `data/cache/drafts/*.md`; accepts a `doc_ref` to write from a document without loading it into main context |
| `ViewMedia` | `media.py` | Local images/videos uploaded with `purpose=image`/`video` and rendered natively via `ms://` refs. Images >4K downscaled to 3840x2160; videos re-encoded to 15fps/1080p (ffmpeg from PATH, falling back to the bundled `imageio-ffmpeg` binary); source files >300MB rejected up front, 100MB cap after compression |
| `SelfWindow` | `self_window.py` | Hide/show/minimize/status for the agent's OWN console window (`GetConsoleWindow` + `ShowWindow`) so it stays out of screenshots and the UIA tree during desktop operation; auto-restored at task end, before RequestHumanHelp, and via atexit |
| `FocusGuard` | `focus_guard.py` | In-process asyncio watchdog (no subprocess) that pins the foreground to a target window, polling ~0.4s and re-focusing on drift via the AttachThreadInput recipe (plain `SetForegroundWindow` is blocked when a fullscreen game holds focus — see `scripts/spike_focus_guard.py`); stopped automatically at task end |
| `GenerateImage` | `image_gen.py` | Image-generation subagent: LLM writes SVG → CairoSVG renders PNG → uploaded for LLM visual self-review against the requirement → revises with feedback, max 5 rounds; returns `data/cache/generated/*.png` path (registered only when media upload is enabled; CairoSVG needs the native cairo library) |
| `CaptureWindow` | `window_capture.py` | Capture a window by title via `PrintWindow(PW_RENDERFULLCONTENT)` and show it to the model — works for occluded windows, Qt/DirectComposition apps with no UIA tree, and display-affinity filtered windows that mss misses (registered only when media upload is enabled) |

Kimi Files API notes: uploaded files are kept by the platform **indefinitely** (no TTL; 1000-file/10GB quota). `file-extract` uploads are deleted right after extraction (best-effort) and cached locally by sha256; `image`/`video` uploads must outlive the task that references them. All three purposes are swept at startup **and** after each task ends (fire-and-forget, never raises).

History archive: every `run_task` writes an append-only flight-recorder file `data/archives/<timestamp>-<taskid>.jsonl` (metadata line + sanitized messages; base64 screenshots stripped, sensitive tool args redacted). It is never read back by the agent — post-hoc review only.

## Development commands

Run the agent (after `python setup.py` and editing `config.yaml`):

```powershell
python main.py
```

Quick syntax/import check using the project venv:

```powershell
.\.venv\Scripts\python.exe -m py_compile agent/*.py eventbus/*.py mcp_client/*.py main.py setup.py
.\.venv\Scripts\python.exe -c "import agent; import mcp_client; import eventbus; import main; import setup"
```

Run the test suite (parallel via pytest-xdist, coverage on by default):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

Useful variants: `-n0` runs serially (required for pdb/breakpoint); `-m "not smoke"` skips the real API/MCP smoke tests in `tests/test_integration.py` (~45s of the suite); `--no-cov` skips coverage measurement.

Run first-time setup:

```powershell
python setup.py
```

## Planned commands

These commands are documented in the v8 spec.

### First-time setup

```powershell
python setup.py
```

The setup script is intended to:
1. Check Python 3.12 (`windows-mcp` requires `>=3.12`)
2. Create `.venv/` (prefer `uv`, fall back to stdlib `venv`)
3. `pip install -r requirements.txt` (with `--ignore-requires-python` for `windows-mcp`)
4. Copy `config.yaml.example` to `config.yaml` if missing
5. Create `data/` and SQLite schema
6. Optionally download the OmniParser YOLO weights (`--download-weights`)
7. Install Playwright Chromium (`npx playwright install chromium`)
8. Run smoke tests (Kimi API, all MCP servers, YOLO load + one inference)

For Playwright Chromium download behind the Chinese firewall:

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
python setup.py
```

To download the YOLO vision weights from the GitHub Release mirror (recommended for China):

```powershell
python setup.py --download-weights
```

The mirror is maintained at `LiaoZiqi-GZFLS/omniparser-weights` (release asset `icon_detect.zip`, ~40MB). The download is idempotent (skipped when a plausible `model.pt` already exists) and best-effort (a failure only disables vision SoM; UIA automation still works).

### Running the agent

```powershell
python main.py
```

One-shot (non-interactive) mode:

```powershell
python main.py --task "open notepad" --yes
```

In `--task` mode the confirmation callback denies the action (with a warning)
when stdin is not a TTY instead of blocking on `input()`. Pass `--yes`/`-y` to
auto-approve `write_risky` actions, or `--yes-all` to also auto-approve
destructive actions (implies `--yes`; use with caution). `--yes` does not cover
destructive actions on its own.

At startup `main()` calls `agent.set_interactive(sys.stdin.isatty())`. The
system prompt then tells the model either that a human is at the keyboard (so
`RequestHumanHelp` is worth calling) or that the run is non-interactive (so it
must not call `RequestHumanHelp` and should finish with manual instructions
when blocked).

### YOLO vision weights

```powershell
python setup.py --download-weights
```

Downloads `icon_detect.zip` from the `LiaoZiqi-GZFLS/omniparser-weights` GitHub Release into `models/omniparser/icon_detect/` (`model.pt` + `model.yaml` + `train_args.yaml`). The zip layout (nested folder or root-level files) is detected automatically.

## Core architecture

### ReAct loop

The agent runs a five-stage loop:

```
Perceive → Reflect (on failure/unknown UI only) → Think → Act → Verify
```

Reflect is skipped on the normal path to save tokens.

### Perception fusion

```
Screenshot
    │
    ├──▶ PIL compression/crop
    ├──▶ RapidOCR text + boxes ──────────┐
    ├──▶ UIA/A11y control tree ──┐       ├──▶ fusion (IoU merge/dedup) → SoM annotation ──┐
    │                              ├──▶ Structured environment description → Kimi          │
    └──▶ YOLO icon detection (UIA-less screens) ──────────────────────────────────────────┘
```

OCR input is **inverse-DPI normalized**: `_run_ocr` reads the Windows display scale of the primary monitor (`shcore.GetScaleFactorForMonitor`, works despite our DPI-unaware process) and resizes the screenshot by 1/scale so text sits at its 100% size for RapidOCR — at 100% the original image is used untouched. The result is floored at the 1080p box (`_OCR_MAX_SIZE`), so extreme scaling never yields a smaller image than plain capping would. **The model-facing screenshot uses the same normalization** (`_compress` calls the same `_ocr_resize_ratio`; there are no size config knobs) — `UpgradeVision` flips `original_resolution` and sends the full original image instead. OCR runs on the **GPU via DirectML** when `ocr.use_dml` is on (default) and `onnxruntime-directml` is installed — `setup.py` swaps it in post-install on Windows (rapidocr-onnxruntime's dependency always lands the CPU build first); rapidocr falls back to CPU with a warning when the DML provider is missing. Measured ~5.5x faster warm (4.3s → 0.8s on a 2560×1440 screenshot, RTX 4090 Laptop); spike: `scripts/spike_ocr_dml.py`.

**ChromaDB embeddings must stay on CPU.** After the onnxruntime-directml swap, ChromaDB's default provider list puts `DmlExecutionProvider` first too — and two concurrent DirectML sessions (RapidOCR during perception + ChromaDB ONNX embedding during background skill learning) break the DML device: either a native access violation in `onnxruntime_pybind11_state.pyd` (0xc0000005, observed crashing the agent mid-task) or `DXGI_ERROR_DEVICE_HUNG` (887A0006). `MemoryStore` therefore pins the skill collection to `ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])` (`agent/memory.py`) — the model is tiny, so CPU costs nothing. Repro/verification: `scripts/repro_dml_crash.py --dml-embedding` (crashes, old behavior) vs. default (survives 120s, fixed).

Vision (YOLO SoM) runs as automatic compensation (`yolo.auto_compensate`, default true): `perceive()` runs one detection pass when the UI tree comes back empty but OCR found text (UIA-less apps such as WeChat/Qt/Electron), so the model gets clickable icon markers without having to discover `DesktopInteract` itself. `ZoomRegion` always runs YOLO on its crop.

**SoM fusion** (`ui_detector/fusion.py`): OCR text boxes (`_run_ocr_detailed` returns text + pixel boxes + scores + OCR-input size) and YOLO icon boxes are reconciled into ONE marker list every round, processed in score-descending order: **IoU > 15% merges** into a union box carrying both contents (joined OCR text + `icon` flag, higher score survives); **IoU > 5% dedups** (lower-score box dropped); below 5% both live on. OCR boxes are normalized from OCR-input space into the model-visible space inside the fusion — the two spaces differ only under `UpgradeVision` (compress skips resizing while OCR still normalizes). Each fused marker is `{label, center_x, center_y, bbox, score, text, icon}`; the perception description lists every marker's number and content (`[1] "搜索" icon @(0.730,0.405)`, capped at 100 entries) so the model picks labels by content instead of guessing from the image alone.

YOLO (OmniParser `icon_detect` YOLOv8, ~40MB, ultralytics) runs full-frame icon detection on the compressed screenshot: the model loads lazily on the first detection (~200ms) and measures ~50ms/frame on GPU with an automatic one-time CPU fallback. Raw detections (score-sorted, NOT deduped — reconciliation happens in the fusion) feed `fuse_annotations`; the fused markers are drawn by `visualize_som` as red boxes with numbers, and when markers exist the model receives **dual images** — the clean screenshot first, the annotated copy second. `DesktopInteract(label=N)` resolves the label against the LAST perception's annotations (no fresh detection per click).

Coordinate contract: the model only ever sees the **compressed** screenshot (inverse-DPI normalized, same as OCR input; the ORIGINAL image after `UpgradeVision`) and is told by the perception description to give `loc` coordinates in that image's space. The orchestrator rescales them to native screen pixels at execution time (`_rescale_loc_args`: `screen = image_origin + loc * screen_size / screenshot_size`, where `image_origin` is (0,0) for full-screen views and the crop's top-left corner for ZoomRegion views) — the model never does scaling math. Skipped when `screenshot.crop_to_active_window` is on (image is then window-relative).

Locator degradation chain (cheapest/most reliable first): UIA label (`windows__Snapshot` + `Click`) → YOLO SoM + `DesktopInteract(label=N)` → `NearbyLabels` triangulation → `ZoomRegion` re-perception → `UpgradeVision`/`CaptureWindow` → `PreviewPoints` coordinate guessing.

### MCP server concurrency

Three MCP servers run as separate stdio processes managed in a single asyncio event loop. Corrected package names after spike verification:

| Server | Launch command |
|--------|----------------|
| Playwright MCP | `npx -y @playwright/mcp@latest` |
| Windows MCP | `windows-mcp serve` (official `windows-mcp` package; `uvx windows-mcp serve` also works) |
| Filesystem MCP | `npx -y @modelcontextprotocol/server-filesystem@latest <allowed-dir>` |

Each connection reconnects with exponential backoff on disconnect.

**Note:** The v8 design doc used outdated/incorrect package names (`@anthropic/playwright-mcp-server`, `python -m windows_mcp_server`, `filesystem-mcp`). Always use the corrected commands above.

#### Playwright MCP usage

- Default stdio launch exposes 23 core tools. Key tools: `browser_navigate`, `browser_click`, `browser_type`, `browser_fill_form`, `browser_evaluate`, `browser_wait_for`, `browser_take_screenshot`, and `browser_snapshot`.
- `browser_snapshot` returns a **YAML accessibility tree** with element refs like `[target=e3]`. Pass those refs back to interaction tools in the `target` parameter.
- Optional capabilities (vision, storage, network, testing, devtools, PDF, config) expose extra tools such as `browser_screenshot`, cookie/storage tools, `browser_route`, etc. Enable them with the server’s `--vision` / `--capabilities` flags if needed.

#### Windows-MCP usage

- The official package exposes 19 tools: `Snapshot`, `Screenshot`, `Click`, `Type`, `Scroll`, `Move`, `Shortcut`, `Wait`, `WaitFor`, `App`, `PowerShell`, `FileSystem`, `Process`, `Scrape`, `Clipboard`, `Notification`, `Registry`, `MultiSelect`, `MultiEdit`.
- Use `Snapshot` when you need element `label` IDs for `Click`/`Type`/`Scroll`/`Move`. Use `Screenshot` for a fast visual-only capture.
- `Snapshot` supports `use_ui_tree`, `use_vision`, `use_dom`, and `use_annotation`. Browser DOM mode (`use_dom=True`) filters browser chrome and works in Chrome, Edge, and Firefox. Note: `use_vision=True` only embeds the screenshot image (cursor highlight, optional grid) in the response — windows-mcp 0.8.2 has **no** vision-based element detection (no OmniParser); vision grounding is our own YOLO/SoM path.
- For safety, consider excluding `PowerShell` and `Registry` via `--exclude-tools "PowerShell,Registry"` unless the task explicitly requires them.
- windows-mcp 0.8.2 has an upstream bug (`UnboundLocalError: tree_node` in `tree/service.py` when an interactive element has an empty name) that drops a window's subtree and floods stderr. `setup.py` applies an idempotent patch to the installed file after dependency installation (skipped if already fixed or the layout changed); label-expiry and stale-snapshot defenses live in `agent/tools.py` / `agent/orchestrator.py` — labels are rebuilt by every Snapshot (and perception re-snapshots every round), so on a "Label N out of range" failure the orchestrator auto-fetches a fresh Snapshot and appends it (truncated to 6KB) to the error, letting the model retry with current labels in the same round. Root-cause writeup: `docs/windows_mcp/upstream-tree-node-issue.md`.
- Subprocess stderr from the windows server goes through `_UpstreamNoiseFilter` (`mcp_client/__init__.py`), installed as the client's `errlog` via a real OS pipe (the MCP SDK passes `errlog` to `Popen(stderr=...)`, which requires `fileno()`). It drops tree_node noise lines (traceback-aware, chained blocks judged independently) and whole fastmcp tool-error records (`Error calling tool` / `Invalid arguments for tool` header + indented rich traceback) — those errors are already returned as tool results, so the stderr copy is pure noise. A periodic summary (`caelum.mcp` INFO, ≤1/60s) reports what was suppressed; everything else passes through unmodified.

### Concurrency model

- Main loop: asyncio
- IO thread pool: max 8 workers (screenshots, OCR, YOLO detection, file IO, MCP I/O)
- Kimi API calls: asyncio-native via httpx

### State machine

States: `IDLE → PLANNING → EXECUTING → VERIFYING → (WAITING_HUMAN →) REFLECT → COMPLETED/ERROR/STUCK`.

### Security levels

| Level | Policy | Examples |
|-------|--------|----------|
| Read | Auto | file read, A11y tree |
| Write-safe | Auto + audit | screenshot cache, temp files |
| Write-risky | Confirm | file modify, config change |
| Destructive | Mandatory human approval | data deletion, permission change |

### Kill switch

- `Ctrl+C`: cancel current operation, return to IDLE
- `/stop`: abort current task
- `/quit`: graceful exit

Auto circuit breaker: pause and switch to local mode after 5 consecutive API failures; ask for guidance after 3 consecutive action failures; re-plan after 3 loops on the same UI.

## Data storage

Planned SQLite tables: `user_preferences`, `reflections`, `skills`, `audit_log`, `state_persistence`. ChromaDB for vector search.

### AutoSkill learning

`agent/skills.py` (`SkillLearner`) generates new `SKILL.md` files from successful task trajectories. After each completed task, the orchestrator calls `SkillLearner.learn(task, action_traces)`. The learner searches existing skills by vector similarity; if the best match exceeds the configured cosine-similarity threshold (default 0.85), it merges the new trace and bumps the patch version. Otherwise it creates a new skill under `skills/learned/<name>.md`. LLM generation is used when a client is available; a deterministic template is used as fallback. `MemoryStore.sync_skills()` recursively indexes all `**/*.md` files under `skills/`.

## Key configuration

The user-editable config is `config.yaml` (gitignored). `config.py` validates it with Pydantic.

## Important design decisions

- No Ollama: the only local model is the OmniParser YOLOv8 icon detector, loaded via ultralytics.
- Browser automation uses the accessibility tree first (Playwright MCP), not pure vision.
- Desktop automation uses UIA/A11y first (Windows-MCP), falling back to coordinate/image methods only when needed.
- Kimi's built-in tools (`web-search`, `memory`, `rethink`, `fetch`, `excel`, `convert`, `date`, `base64`, `quickjs`, `random-choice`, `mew`) replace local implementations for search, memory, reflection, fetch, Excel/CSV analysis, and light code execution.
- `moonshot/code-runner:latest` (hyphen) is the correct URI and is available; the registered tool name is `code_runner` (underscore). For code execution, the agent uses a custom `RestrictedCodeRunner` function tool exposed via OpenAI-style Function Calling, running user/model-generated Python inside a local sandbox (subprocess + AST validation + import whitelist). The Formula `code-runner` can be enabled as an alternative. This is more flexible and safer than relying solely on a Formula tool.
- Skills are stored as `SKILL.md` files compatible with the OpenClaw/Claude Code skill format.

## Where to find context

- Latest design spec: `docs/designs/desktop_agent_v8.agent.final.md`
- Background research: `docs/designs/browser_desktop_research.agent.final/browser_desktop_research.agent.final.md`
- Playwright MCP research: `docs/playwright/playwright-mcp-research.md`
- Windows-MCP research: `docs/windows_mcp/windows_mcp.agent.final.md`
- Kimi Formula tools guide: `docs/kimi_api/kimi_tools_guide.agent.final.md`
- License: `LICENSE`
