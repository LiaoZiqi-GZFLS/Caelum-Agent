# Caelum-Agent

Caelum-Agent 是一个面向个人使用的 Windows 命令行桌面操作 Agent。给它一句自然语言指令，它就能自主控制浏览器和 Windows 桌面应用。

- **平台：** Windows 10/11
- **形态：** 纯 CLI（首版无 GUI、无 CI/CD、不打包）
- **许可证：** BSD 3-Clause，Copyright (c) 2026 LiaoZiqi-GZFLS

> English version: see [README.md](README.md).

---

## 特性

- **ReAct 闭环：** 感知（Perceive）→ 思考（Think）→ 行动（Act）→ 验证（Verify），失败时按需反思。
- **多模态感知：** 截图 + OCR + 无障碍树 + GUI-Actor-3B 的 SoM 标注。
- **浏览器自动化：** 走 Playwright MCP，无障碍树优先交互。
- **桌面自动化：** 走 Windows-MCP，UIA/A11y 优先，必要时回退到坐标/图像方式。
- **Kimi K2.6 大脑：** OpenAI 兼容 API，支持 Formula 工具与本地函数工具。
- **三个 MCP 并发：** Playwright / Windows / filesystem 在一个 asyncio 循环里同时运行。
- **安全守卫：** 四级分类（read / write_safe / write_risky / destructive），危险与破坏性操作必须人工批准。
- **急停：** `Ctrl+C`、`/stop`、`/quit` 可取消或优雅退出。
- **熔断：** 连续 API 失败、连续动作失败、同一 UI 原地打转检测。
- **AutoSkill 自学习：** 从成功轨迹自动生成 `SKILL.md` 技能文件。
- **记忆：** Kimi memory + rethink 工具，本地 SQLite 兜底。

---

## 环境要求

- Windows 10/11
- Python **3.12**（GUI-Actor-3B 要求 `<3.13`；`windows-mcp` 要求 `>=3.12`）
- 带 `npx` 的 Node.js（用于 Playwright 与 filesystem MCP 服务）
- 来自 [Moonshot AI](https://platform.moonshot.cn/) 的 Kimi API key

---

## 安装

```powershell
python setup.py
```

非交互安装可直接传入 API key：

```powershell
python setup.py --api-key sk-...
```

跳过 smoke 测试（适用于 CI 或无显示环境）：

```powershell
python setup.py --skip-smoke-tests
```

如果处于国内网络环境，运行 setup 前先设置 Playwright 下载镜像：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
python setup.py
```

### 本地模型权重（GUI-Actor-3B，可选）

视觉定位需要 GUI-Actor-3B 权重，默认放在 `./models/gui-actor-3b/`（约 7.6GB）。任选一种方式下载：

```powershell
# 国内推荐：GitHub Release 镜像
python setup.py --download-weights --weights-source github

# 或 HuggingFace（走 hf-mirror）
python setup.py --download-weights --weights-source huggingface

# 或 huggingface-cli 直接拉取
huggingface-cli download microsoft/GUI-Actor-3B-Qwen2.5-VL --local-dir ./models/gui-actor-3b
```

---

## 配置

`setup.py` 会把 `config.yaml.example` 复制为 `config.yaml`（已 gitignore）。编辑 `config.yaml`，填入你的真实 Kimi API key。

主要配置段：

- `llm`：Kimi API key、模型、可选的 `reasoning_effort`、内置 Formula 工具。
- `mcp_servers`：Playwright / Windows / filesystem MCP 服务的命令与参数。
- `ui_detector`：GUI-Actor-3B 模型路径、device、dtype、verifier 设置。
- `screenshot`：分辨率、压缩、裁剪策略。
- `security`：auto-execute / confirm / destructive 各级审批策略。
- `kill_switch`：API/动作失败阈值与同一 UI 循环阈值。

**切勿**提交 `config.yaml`。

---

## 使用

### 交互式 REPL

```powershell
python main.py
```

可用的 REPL 命令：

- `/help` — 显示命令列表
- `/status` — 显示当前状态、任务 ID、上一步动作、失败计数、MCP 健康度
- `/stop` — 取消当前任务
- `/quit` — 退出

### 单次任务模式

```powershell
python main.py --task "list files in E:/code/project"
```

在单次模式下，危险动作通常会弹出交互式 `y/n` 确认。当 stdin 不是 TTY（脚本、CI 或管道输入）时，提示会被替换为一条警告并**拒绝**该动作，因此任务永远不会卡在 `input()` 上。非交互场景下要自动批准：

```powershell
# 自动批准 write_risky 动作（Click/Type/App/浏览器编辑）。
python main.py --task "open notepad" --yes

# 同时自动批准 destructive 动作，跳过逐字确认。请谨慎使用。
python main.py --task "delete temp files" --yes-destructive
```

`--yes` **不**涵盖破坏性动作；除非同时传入 `--yes-destructive`，否则破坏性动作仍需逐字输入动作摘要来确认。

### 关闭视觉/OCR 以加快测试

```powershell
python main.py --no-vision --task "list files in E:/code/project"
```

---

## 开发

运行全部单元测试（排除真实 API/MCP 集成的 smoke 测试）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q -m "not smoke"
```

运行 smoke 测试（需要带有效凭据的 `config.yaml`）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q -m smoke
```

运行完整套件：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q -m ""
```

快速语法/导入检查：

```powershell
.\.venv\Scripts\python.exe -m py_compile agent/*.py eventbus/*.py mcp_client/*.py main.py setup.py
.\.venv\Scripts\python.exe -c "import agent; import mcp_client; import eventbus; import main; import setup"
```

端到端单任务测试：

```powershell
python spikes/e2e_single_task.py "open notepad"
```

---

## 项目结构

```
├── main.py                    # CLI 入口
├── config.yaml.example        # 配置模板
├── setup.py                   # 首次初始化
├── agent/                     # Agent 核心模块
│   ├── config.py              # Pydantic 配置
│   ├── orchestrator.py        # ReAct 闭环编排
│   ├── state_machine.py       # 8 状态 FSM
│   ├── perception.py          # 多模态感知
│   ├── llm_client.py          # Kimi LLM 客户端
│   ├── security.py            # 安全策略守卫
│   ├── kill_switch.py         # 全局键盘急停
│   ├── tools.py               # MCP 工具映射 + CodeRunner
│   ├── snapshot_parser.py     # 无障碍树解析器
│   ├── memory.py              # SQLite + ChromaDB 存储
│   ├── kimi_memory.py         # Kimi memory/rethink 适配
│   ├── reflection.py          # 反思引擎
│   ├── skills.py              # AutoSkill 自学习
│   └── logging_config.py      # 结构化日志
├── eventbus/                  # asyncio EventBus
├── mcp_client/                # 多服务 stdio MCP 客户端
├── ui_detector/               # GUI-Actor-3B + verifier
├── skills/                    # SKILL.md 技能库
├── tests/                     # pytest 单元 + smoke 测试
└── spikes/                    # Spike/实验脚本
```

---

## 架构

### ReAct 闭环

```
Perceive → Reflect（仅在失败/未知 UI 时）→ Think → Act → Verify
```

### 感知融合

```
截图
    ├── PIL 压缩/裁剪
    ├── RapidOCR 文字识别
    ├── UIA/A11y 控件树 ──┐
    │                       ├── 结构化环境描述 → Kimi
    └── GUI-Actor-3B 元素检测 → SoM 标注 ──┘
```

### 并发模型

- 主循环：asyncio
- 视觉推理线程池：最多 2 个 worker（GUI-Actor-3B）
- IO 线程池：最多 8 个 worker（截图、文件 IO、MCP I/O）
- Kimi API 调用：经 httpx 原生 asyncio

---

## 重要约束

- 虚拟环境**只能用 Python 3.12**。加载 GUI-Actor-3B 的环境不要用 3.13+。
- **GUI-Actor-3B 必须走 Transformers 原生推理**，不能通过 Ollama、GGUF、vLLM 或 llama.cpp 加载。
- **`moonshot/code-runner:latest`（连字符）才是正确的 URI。** Formula URI 用连字符（如 `web-search`、`code-runner`）；注册到本地的工具名用下划线（`web_search`、`code_runner`）。代码执行默认用本地 `RestrictedCodeRunner` 沙箱，Formula 的 `code-runner` 可作为替代启用。
- **浏览器自动化无障碍树优先**，而非纯视觉。
- **桌面自动化 UIA/A11y 优先**，仅在需要时回退到坐标/图像方式。

---

## 许可证

BSD 3-Clause，Copyright (c) 2026 LiaoZiqi-GZFLS。
详见 [LICENSE](LICENSE)。
