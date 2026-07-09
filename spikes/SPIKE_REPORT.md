# Spike Report: External Dependency Verification for Caelum-Agent v8

Date: 2026-07-09
Environment: Windows 11, Python 3.13.9 (base), Node v24.14.0, npm 11.12.1

## Summary

Most dependencies are available, but **three key package names/commands in the v8 design doc are incorrect**, and **GUI-Actor requires Python <3.13** while the current base environment is Python 3.13.9.

After reading the new Playwright and Windows-MCP research reports and re-verifying the actual packages, the recommended desktop MCP server is **`windows-mcp` (CursorTouch)** rather than the older `windows-mcp-server` distribution.

---

## 1. Kimi K2.6 API (Formula tools)

**Status:** Runtime-verified. API key loaded from `config.yaml`.

### Verified facts

- Base URL: `https://api.moonshot.cn/v1`
- Correct model name is `kimi-k2.6` (with a dot), not `kimi-k2-6` as written in v8 docs.
- Formula tools URI pattern: `moonshot/{name}:latest`
- Tool-definition endpoint: `GET /formulas/{URI}/tools`
- Tool-execution endpoint: `POST /formulas/{URI}/fibers`
- Tool results live in `context.output` for normal tools and `context.encrypted_output` for protected tools. Both can be passed back to the model as `role="tool"` content.
- `web-search` is billed separately (¥0.03/call).
- For `kimi-k2.6`, **do not pass `reasoning_effort="none"`**; the model rejects it. Valid values are `minimal`, `low`, `medium`, `high`. Omitting the parameter also works and is the safest default.

### Formula tool availability (2026-07-09)

| URI | Status | function.name(s) | Notes |
|-----|--------|------------------|-------|
| `moonshot/web-search:latest` | ✅ | `web_search` | Returns `encrypted_output`; accepts `query`, optional `classes` |
| `moonshot/fetch:latest` | ✅ | `fetch` | Returns markdown in `output`; params include `url`, `max_length`, `raw` |
| `moonshot/code_runner:latest` | ❌ | — | Returns 404 `formula not found` |
| `moonshot/quickjs:latest` | ✅ | `quickjs` | Runs JS in QuickJS sandbox |
| `moonshot/convert:latest` | ✅ | `convert` | Unit conversion |
| `moonshot/date:latest` | ✅ | `date` | Date/time operations |
| `moonshot/base64:latest` | ✅ | `base64_encode` / `base64_decode` | Two separate functions |
| `moonshot/excel:latest` | ✅ | `read_file`, `list_sheets`, ... | Plugin-style tool with nested `functions` array |
| `moonshot/memory:latest` | ✅ | `memory` | Key-value + semantic recall |
| `moonshot/rethink:latest` | ✅ | `rethink` | Thought organization |
| `moonshot/random-choice:latest` | ✅ | `random_choice` | Random selection |
| `moonshot/mew:latest` | ✅ | `mew_generator` | Entertainment tool |

### Important corrections

- `code_runner` Formula is **not available** via the Formula mechanism. The recommended replacement is to implement a custom `CodeRunner` function tool exposed through standard OpenAI-style Function Calling and execute it in a local sandbox (restricted subprocess, QuickJS, `RestrictedPython`, etc.). This gives full control over language support, timeout, network access, and file-system isolation.
- `quickjs` Formula is available for light JavaScript execution if a local sandbox is not yet implemented.
- `base64` exposes **two** functions (`base64_encode` / `base64_decode`), not one `base64` function.
- `excel` tool definition contains a nested `functions` array under `_plugin`; the client must flatten these to register them in the OpenAI `tools` array.
- The official Kimi tool guide says to use `reasoning_effort="none"` with `web-search`, but the actual `kimi-k2.6` endpoint rejects `"none"`. Use a valid level or omit the field.

### End-to-end verified flow

Ran `web_search` + `fetch` chain successfully:
1. `GET /formulas/moonshot/web-search:latest/tools` returned tool schema.
2. Chat completion with `kimi-k2.6` returned `finish_reason="tool_calls"`.
3. `POST /formulas/.../fibers` returned search results in `context.encrypted_output`.
4. Returned result as `role="tool"` message.
5. Second completion produced final answer.

Script: `spikes/kimi_formula_chain.py`

---

## 2. MCP Servers

### 2.1 Playwright MCP

**v8 design doc command:** `npx @anthropic/playwright-mcp-server`  
**Actual available package:** `@playwright/mcp` (current version 0.0.77)

`@anthropic/playwright-mcp-server` does **not** exist on npm.

Correct command:
```bash
npx -y @playwright/mcp@latest
```

#### Default core tools (23, stdio, verified at runtime)

- Navigation / tabs: `browser_navigate`, `browser_navigate_back`, `browser_tabs`
- Page perception: `browser_snapshot` (YAML accessibility tree), `browser_take_screenshot`
- Interaction: `browser_click`, `browser_type`, `browser_press_key`, `browser_fill_form`, `browser_select_option`, `browser_drag`, `browser_hover`, `browser_drop`, `browser_file_upload`
- Script / wait: `browser_evaluate`, `browser_wait_for`, `browser_run_code_unsafe`
- Dialog / console / network: `browser_handle_dialog`, `browser_console_messages`, `browser_network_requests`, `browser_network_request`
- Window: `browser_resize`, `browser_close`

#### Capability-gated tool groups (source-level, enable with `--vision` or `capabilities`)

- **Vision:** `browser_screenshot`, `browser_mouse_move_xy`, `browser_mouse_down`, `browser_mouse_up`, `browser_mouse_click`, `browser_mouse_drag`
- **Storage:** cookie / localStorage / sessionStorage tools
- **Network:** `browser_route`
- **Testing:** `browser_tracing_start`, `browser_tracing_stop`, `browser_verify`
- **Devtools:** `browser_devtools`
- **PDF:** `browser_pdf`
- **Config:** `browser_configure`

#### Accessibility snapshot format

`browser_snapshot` returns a **YAML accessibility tree**, not raw DOM. Example:

```yaml
- main
  - heading "Sign in to QASkills" [level=1] [target=e3]
  - textbox "Email" [target=e4]
  - textbox "Password" [target=e5]
  - button "Sign in" [target=e6]
```

Key points:
- Element references are `[target=eN]` strings.
- `target` parameters accept `target=eN`, role+name, ARIA attributes, or CSS selectors.
- Use `boxes=true` to include bounding boxes (`[box=x,y,w,h]`).

**Impact:** Update v8 docs and implementation to use `@playwright/mcp` and the actual tool names. The browser automation path should rely on `browser_snapshot` + `target=eN` refs.

### 2.2 Windows MCP

**v8 design doc command:** `python -m windows_mcp_server`  
**Initial PyPI finding:** `windows-mcp-server` 0.5.3 (different/unofficial distribution with Pascal-case tool names)  
**Recommended official package:** `windows-mcp` v0.8.2 from CursorTouch/Windows-MCP

Neither `python -m windows_mcp_server` nor an npm `@windows/mcp-server` package exists.

Correct commands:
```bash
# Recommended by upstream (Astral uv)
uvx windows-mcp serve

# Alternative via pip
pip install windows-mcp
windows-mcp serve
```

**Install-service / background mode:**
```bash
windows-mcp install    # auto-start on login
windows-mcp uninstall  # remove
```

#### Verified tools exposed by `windows-mcp` (19)

- **UI interaction:** `Click`, `Type`, `Scroll`, `Move`
- **Keyboard / wait:** `Shortcut`, `Wait`, `WaitFor`
- **State capture:** `Snapshot`, `Screenshot`
- **System / app:** `App`, `PowerShell`, `FileSystem`, `Process`
- **Network / helpers:** `Scrape`, `Clipboard`, `Notification`, `Registry`
- **Batch:** `MultiSelect`, `MultiEdit`

#### Perception workflow

- `Snapshot` is the full perception tool: returns interactive/scrollable UI nodes with numeric `label` IDs, optional screenshot, optional browser DOM mode (`use_dom=True`).
- `Screenshot` is fast and screenshot-only; use it when the LLM only needs a visual overview.
- `Click` / `Type` / `Scroll` / `Move` accept either `loc=[x,y]` or `label=<id>` from `Snapshot`.

#### Security notes from research

- `PowerShell` and `Registry` are high-risk tools. For normal operation, consider starting the server with `--exclude-tools "PowerShell,Registry"`.
- Network transports (`sse`, `streamable-http`) support `--auth-key`, `--ip-allowlist`, TLS, and OAuth 2.0 + PKCE.
- Stdio mode (used by Caelum-Agent) runs locally as a child process and does not need authentication.

**Impact:** Switch the project from `windows-mcp-server` to `windows-mcp`. Update tool names and tool-call schemas to the 19 names above.

### 2.3 Filesystem MCP

**v8 design doc command:** `filesystem-mcp /allowed/path`  
**Status:** Package `filesystem-mcp` does **not** exist on PyPI.

Verified working alternative:

```bash
npx -y @modelcontextprotocol/server-filesystem@latest <allowed-dir>
```

Exposes 14 tools (verified at runtime):
- `read_text_file`, `read_multiple_files`, `read_media_file`
- `write_file`, `edit_file`, `create_directory`
- `list_directory`, `list_directory_with_sizes`, `directory_tree`
- `move_file`, `search_files`, `get_file_info`
- `list_allowed_directories`
- `read_file` (deprecated alias)

**Impact:** Replace `filesystem-mcp` with `@modelcontextprotocol/server-filesystem` in v8 docs and implementation.

---

## 3. GUI-Actor-3B

**Model:** `microsoft/GUI-Actor-3B-Qwen2.5-VL` on HuggingFace  
**Status:** Repository exists, file structure is complete.

Verified files:
- `config.json`
- `model-00001-of-00002.safetensors`, `model-00002-of-00002.safetensors`
- `model.safetensors.index.json`
- `tokenizer.json`, `tokenizer_config.json`, `chat_template.json`
- `preprocessor_config.json`, `generation_config.json`

**GitHub repo:** `https://github.com/microsoft/GUI-Actor`  
**Installation:** `git clone` + `pip install -e .`  
**Custom model class:** `Qwen2_5_VLForConditionalGenerationWithPointer` in `src/gui_actor/modeling_qwen25vl.py`  
**Inference entry:** `gui_actor.inference.inference`

**Critical constraint: Python version**  
`pyproject.toml` declares `requires-python = ">=3.10,<3.13"`. The README recommends Python 3.10. The current base environment is **Python 3.13.9**, which violates this requirement.

`windows-mcp` v0.8.2 requires Python `>=3.12`, so the overlapping safe version for both GUI-Actor and Windows-MCP is **Python 3.12**.

**Action needed:**
- Create a dedicated Python 3.12 virtual environment for the project.
- Full model download (~6 GB) and a single inference pass still need to be verified.

---

## 4. Other Python Dependencies

All checked PyPI packages exist and are installable:
- `windows-mcp` 0.8.2 (verified as the official package)
- `windows-mcp-server` 0.5.3 (different distribution; superseded)
- `pywinauto` 0.6.9
- `rapidocr-onnxruntime` 1.2.3
- `chromadb` 1.5.9 (already installed in base env)
- `pynput` 1.8.2
- `accelerate` 1.14.0
- `qwen-vl-utils` 0.0.14
- `mcp` module present in base env

`rapidocr-onnxruntime` is **not** installed in the base env yet.

---

## 5. Recommended corrections to v8 design doc

| v8 doc statement | Correction |
|------------------|------------|
| `npx @anthropic/playwright-mcp-server` | `npx -y @playwright/mcp@latest` |
| `python -m windows_mcp_server` | `windows-mcp serve` (official CursorTouch package) or `uvx windows-mcp serve` |
| `filesystem-mcp /allowed/path` | `npx -y @modelcontextprotocol/server-filesystem@latest /allowed/path` |
| `browser_get_accessibility_tree` | `browser_snapshot` |
| `get_ui_tree` / `click_element` (Windows MCP) | `Snapshot`, `Click`, `Type`, `Scroll`, `Move`, etc. |
| GUI-Actor runs on current Python | Requires Python 3.10–3.12; base env is 3.13.9 |
| `code_runner` Formula available | Returns 404; use `quickjs` or a local sandbox instead |
| `reasoning_effort="none"` for web-search | Not accepted by `kimi-k2.6`; omit or use `minimal`/`low`/`medium`/`high` |

---

## 6. Next steps

1. **Create a Python 3.12 virtual environment** so both GUI-Actor-3B and `windows-mcp` are supported.
2. ✅ **Kimi API verified.** Implement code execution as a custom `CodeRunner` function tool in a local sandbox, since the `code_runner` Formula is unavailable.
3. ✅ **Playwright MCP verified.** Decide which optional capabilities to enable (vision is useful for screenshot-heavy tasks).
4. ✅ **Windows MCP verified.** Switch implementation/tool schemas to the official `windows-mcp` tool names.
5. ✅ **Filesystem MCP verified.** Use `@modelcontextprotocol/server-filesystem`.
6. **Download GUI-Actor-3B weights and run one inference** to confirm local vision model works.
7. **Run a minimal end-to-end spike:** CLI → Kimi → one browser tool + one Windows tool + one filesystem tool.

---

## 7. Spike scripts used

See `spikes/` directory. Key scripts:
- `spikes/list_mcp_tools.py` — list tools of any stdio MCP server
- `spikes/verify_windows_mcp.py` — connect to `windows-mcp` and print tools
- `spikes/verify_playwright_mcp.py` — connect to `@playwright/mcp` and print tools
- `spikes/verify_filesystem_mcp.py` — connect to `@modelcontextprotocol/server-filesystem` and print tools
- `spikes/check_gui_actor_repo.py` — check HuggingFace/GitHub repo existence and file list
- `spikes/kimi_formula_chain.py` — end-to-end Kimi web-search + fetch chain
- `spikes/probe_kimi_models.py` — probe valid Kimi model names
