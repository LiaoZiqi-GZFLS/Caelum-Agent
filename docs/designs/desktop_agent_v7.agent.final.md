# Windows桌面操作助手 — 技术方案 v7

> **版本**: v7.0 | **性质**: 个人CLI项目 | **平台**: Windows 10/11
> **大脑**: Kimi K2.6 (Moonshot AI, 12个内置工具) | **本地模型管理**: Ollama
> **UI检测**: GUI-Actor-3B + Verifier (NeurIPS'25, Microsoft)

---

## 1. 概述与核心架构

### 1.1 定位

一个Windows命令行桌面操作Agent。用户通过自然语言下达指令，Agent自主操控浏览器和Windows桌面应用完成任务。

**双域覆盖**：浏览器（Playwright MCP accessibility tree）+ Windows桌面（Windows-MCP UIA控件树）。

**不做的事**：不设计GUI、不做CI/CD、不做打包签名、不做自动更新。插件系统保留设计但初始版本不实现。

### 1.2 核心循环：五级ReAct

```
Perceive（感知）→ Reflect（反思，可选）→ Think（推理）→ Act（执行）→ Verify（验证）
```

Reflect仅在操作失败/死循环/未知UI时触发，正常路径跳过，节省token。

### 1.3 技术栈全景

| 层级 | 选型 | 来源 |
|:---|:---|:---|
| **大脑** | Kimi K2.6 API | Moonshot AI |
| **Kimi内置工具** | web-search / memory / rethink / fetch / excel / code_runner / convert / date / base64 / quickjs / random-choice / mew | Kimi开放平台 |
| **本地模型管理** | Ollama | ollama.com |
| **UI检测** | GUI-Actor-3B + Verifier | Microsoft, NeurIPS'25 |
| **浏览器控制** | Playwright MCP | Anthropic, 31.2K stars |
| **桌面控制** | Windows-MCP | wonderwhy-er, 5,456 stars |
| **文件操作** | filesystem-mcp (Python) | PyPI |
| **OCR** | RapidOCR (ONNXRuntime) | RapidAI, CPU |
| **截图** | mss + PIL压缩裁剪 | Python库 |
| **技能库** | SKILL.md (OpenClaw生态) | 33,000+ skills |
| **记忆** | Kimi memory tool + 本地SQLite备份 | Kimi API层 |
| **反思** | Kimi rethink tool + 本地记录 | Kimi API层 |
| **状态机** | 自研FSM (8状态) | — |
| **事件总线** | 自研EventBus (asyncio) | — |
| **MCP多Server管理** | asyncio多stdio连接 | mcp SDK |
| **Kill Switch** | pynput全局监听 + asyncio取消 | — |

---

## 2. UI检测层 — GUI-Actor-3B + Verifier

### 2.1 选型说明

GUI-Actor来自微软NeurIPS'25论文，基于Qwen2.5-VL系列微调。

| 模型 | 参数量 | ScreenSpot-Pro | 显存(Q4_K_M) | 获取方式 |
|:---|:---:|:---:|:---:|:---|
| GUI-Actor-7B | 7B | 44.6 | ~4.5GB | HuggingFace手动下载+转GGUF |
| **GUI-Actor-3B** | **3B** | **~38** | **~2.5GB** | **HuggingFace手动下载+转GGUF** |
| Qwen2.5-VL-7B(官方) | 7B | 27.6 | ~4.5GB | `ollama pull qwen2.5vl:7b` |

选择GUI-Actor-3B：微软UI专用微调（远超官方Qwen2.5-VL），3B轻量适合消费级显卡，Verifier架构降低幻觉。

### 2.2 Verifier工作模式

```
截图 ──▶ GUI-Actor-3B(Guider) ──▶ 候选操作 [click(x1,y1), type("text")...]
                    │
                    ▼
            Verifier验证
       ┌────────┼────────┐
       ▼        ▼        ▼
      通过     否决    不确定
       │        │        │
       ▼        ▼        ▼
     执行     重试    请求用户
```

Verifier评估：目标元素是否存在、操作类型是否匹配、坐标是否合理。

### 2.3 Ollama导入步骤

GUI-Actor-3B需从HuggingFace下载PyTorch权重，转为GGUF后导入Ollama。完整步骤：

**Step 1 — 下载原始权重**

```bash
pip install huggingface-hub
huggingface-cli download microsoft/GUI-Actor-3B --local-dir ./gui-actor-3b
```

**Step 2 — 安装llama.cpp并转换GGUF**

```bash
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp
pip install -r requirements.txt

# 转换为GGUF（Q4_K_M量化，推荐平衡精度与体积）
python convert_hf_to_gguf.py ./gui-actor-3b --outfile gui-actor-3b-q4_k_m.gguf --outtype q4_k_m

# 如需更高精度可选Q5_K_M或Q8_0
# python convert_hf_to_gguf.py ./gui-actor-3b --outfile gui-actor-3b-q5_k_m.gguf --outtype q5_k_m
```

量化选项对比：

| 量化类型 | 体积(3B) | 精度损失 | 推荐场景 |
|:---|:---:|:---|:---|
| Q4_K_M | ~2GB | 轻微 | 8GB显卡，首选 |
| Q5_K_M | ~2.4GB | 极小 | 12GB显卡，追求精度 |
| Q8_0 | ~3.5GB | 几乎无损 | 16GB显卡，最佳效果 |
| F16 | ~6GB | 无损 | 开发调试 |

**Step 3 — 编写Modelfile**

```dockerfile
FROM ./gui-actor-3b-q4_k_m.gguf

PARAMETER temperature 0.1
PARAMETER top_p 0.9
PARAMETER num_ctx 8192

# Qwen2.5-VL的chat template
TEMPLATE """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ range .Messages }}<|im_start|>{{ .Role }}
{{ .Content }}<|im_end|>
{{ end }}<|im_start|>assistant
"""

PARAMETER stop "<|im_end|>"
PARAMETER stop "<|im_start|>"

# Vision-specific: tell Ollama this is a multimodal model
# (Ollama 0.5+自动检测multimodal GGUF)

SYSTEM "You are a GUI analysis assistant. Analyze the screenshot and list all interactive UI elements. Return format: [(x1, y1, x2, y2, element_type, label)]. Be precise with coordinates."
```

**Step 4 — 创建并运行**

```bash
ollama create gui-actor-3b -f Modelfile
ollama run gui-actor-3b

# 测试API
curl http://localhost:11434/api/generate -d '{
  "model": "gui-actor-3b",
  "prompt": "List all interactive elements",
  "images": ["<base64_encoded_screenshot>"]
}'
```

**常见问题**：
- Ollama 0.5+才原生支持multimodal GGUF，旧版需升级
- 如果输出乱码，检查TEMPLATE是否匹配Qwen2.5-VL的chat template格式
- `convert_hf_to_gguf.py`可能需要添加`--clip-model-is-vision`或类似参数处理vision tower

### 2.4 感知融合流程

```
屏幕截图 ──▶ PIL压缩裁剪（降低token成本）
    │
    ├──▶ RapidOCR文字识别（30ms, CPU）
    │
    ├──▶ UIA/A11y控件树（确定性路径） ──┐
    │                                     ├──▶ 结构化环境描述 ──▶ Kimi
    └──▶ GUI-Actor-3B元素检测 ──▶ SoM标注 ──┘         (感知结果)
              (本地, ~300ms)
```

---

## 3. Kimi K2.6 API 与 12个内置工具

### 3.1 接口信息

| 项目 | 内容 |
|:---|:---|
| Base URL | `https://api.moonshot.cn/v1` |
| 模型 | `kimi-k2-6` |
| 上下文 | 256K tokens |
| 工具调用 | `tool_calls` / `tools` |
| 多模态 | 图文混合输入，1024 tokens/图 |
| 流式输出 | `stream=True` |

### 3.2 定价与成本估算

| 项目 | 价格 |
|:---|:---|
| 输入（缓存命中） | ¥6.5 / 1M tokens (~$0.16/M) |
| 输入（缓存未命中） | ¥26 / 1M tokens (~$0.95/M) |
| 输出 | ¥27 / 1M tokens (~$4.00/M) |
| 联网搜索 | ¥0.03 / 次 (~$0.004/次) |

**截图传Kimi的token消耗**：

| 截图分辨率 | 原始大小 | Base64编码 | 图片tokens |
|:---|:---:|:---:|:---:|
| 1920x1080 全屏 | ~2MB | ~2.7MB | ~1024 |
| 1280x720 (50%缩放) | ~0.9MB | ~1.2MB | ~512 |
| 800x600 区域裁剪 | ~0.5MB | ~0.7MB | ~341 |
| 640x480 (压缩 quality=60) | ~0.15MB | ~0.2MB | ~205 |

**成本估算**（以100步任务为例，每步传1张截图）：

| 策略 | 每步截图tokens | 100步输入tokens | 输入成本(缓存命中) | 输入成本(缓存未命中) |
|:---|:---:|:---:|:---:|:---:|
| 全屏原图 | 1024 | ~102K | ¥0.67 | ¥2.65 |
| 50%缩放 | 512 | ~51K | ¥0.33 | ¥1.33 |
| 区域裁剪+压缩 | 205 | ~21K | ¥0.14 | ¥0.55 |

**推荐策略**：区域裁剪+压缩（quality=60），将单图token控制在200-300，月度中度使用（~500步）成本约¥0.7-2.8。

**Context Caching**：系统提示词和工具定义每次不变，自动命中缓存（¥6.5/M vs ¥26/M，节省75%）。截图部分不缓存（每步不同），但控件树文本描述可缓存。

### 3.3 截图压缩与裁剪策略

```python
from PIL import Image
import io

def optimize_screenshot(raw_bytes: bytes, max_size: tuple = (800, 600), quality: int = 60) -> str:
    """截图压缩裁剪，返回base64编码的JPEG"""
    img = Image.open(io.BytesIO(raw_bytes))
    # 缩放
    img.thumbnail(max_size, Image.LANCZOS)
    # 转JPEG压缩
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='JPEG', quality=quality)
    import base64
    return base64.b64encode(buf.getvalue()).decode()
```

**自适应策略**：
- 首次感知：全屏50%缩放（快速全局理解）
- 定位操作后：目标元素区域裁剪（精准局部理解）
- 验证步骤：仅传变化区域（diff截图，最小token）

### 3.4 12个内置工具

以下工具由Kimi服务端执行，Agent在`tools`参数中注册名称即可。

| 工具名 | 功能 | 替代了我们的什么 |
|:---|:---|:---|
| `web-search` | 实时互联网搜索 | 外部搜索API |
| `memory` | 对话历史+用户偏好持久化 | Mem0（API层替代） |
| `rethink` | 整理想法、反思分析 | Reflexion的部分实现 |
| `fetch` | URL内容提取转Markdown | Jina Reader/Crawl4AI |
| `excel` | Excel/CSV分析 | 本地pandas处理 |
| `code_runner` | 安全执行Python代码 | 本地Python沙箱 |
| `convert` | 单位换算 | 单位转换代码 |
| `date` | 日期时间处理 | 日期处理代码 |
| `base64` | Base64编解码 | base64代码 |
| `quickjs` | 安全执行JavaScript | JS沙箱 |
| `random-choice` | 随机选择 | random代码 |
| `mew` | 娱乐工具 | — |

### 3.5 Memory工具的具体使用

Kimi `memory` tool数据存储在Moonshot服务端。设计策略：

- **利用Kimi memory**：日常对话偏好、短期上下文由Kimi自动管理
- **本地SQLite备份**：关键偏好（如"总是用Chrome"、"文件保存到D盘"）同时在`data/memory.db`备份
- **SKILL.md技能库**：操作型技能存为SKILL.md文件，不依赖API
- **API不可用时**：完全使用本地SQLite记忆 + SKILL.md技能库运行

### 3.6 Rethink工具的具体使用

操作失败后调用Kimi `rethink`工具分析原因，将关键结论保存到本地SQLite。下次类似场景时注入上下文。

---

## 4. MCP工具集与多Server并发管理

### 4.1 三个MCP Server同时运行

Agent需要同时维护3个stdio子进程：

| MCP Server | 启动命令 | 进程类型 |
|:---|:---|:---:|
| Playwright MCP | `npx @anthropic/playwright-mcp-server` | Node.js |
| Windows MCP | `python -m windows_mcp_server` | Python |
| Filesystem MCP | `filesystem-mcp /allowed/path` | Python |

**并发连接方案**：使用`mcp` Python SDK的`stdio_client`，在同一个asyncio event loop中管理3个独立连接。每个server有自己的`ClientSession`，通过各自的`call_tool`调用工具。

```python
# 核心设计：每个MCP server一个asyncio Task
# 3个Task并发运行，通过各自的ClientSession调用工具
# 工具选择由Kimi的tool_calls决定，Agent负责路由到对应server
```

**启动顺序**：
1. 同时启动3个stdio子进程（asyncio.create_subprocess_exec）
2. 逐个初始化ClientSession（session.initialize()）
3. 收集所有tools注册到Kimi的tools参数
4. 任一server断开时自动重连（指数退避）

**工具命名空间**：3个server可能有同名工具（如都有`read_file`），通过前缀区分：`playwright_read_file` / `windows_read_file` / `fs_read_file`。实际实现中各server工具名天然不同。

### 4.2 Playwright MCP（浏览器操作）

来源：Anthropic官方 `@anthropic/playwright-mcp-server`（31.2K stars）

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `browser_navigate` | 打开URL | Read |
| `browser_click` | 点击元素（by a11y selector） | Write-safe |
| `browser_type` | 输入文本 | Write-safe |
| `browser_select` | 下拉选择 | Write-safe |
| `browser_press_key` | 按键 | Write-safe |
| `browser_get_accessibility_tree` | 获取A11y树 | Read |
| `browser_screenshot` | 页面截图 | Read |
| `browser_evaluate` | 执行JS | Write-risky |

### 4.3 Windows-MCP（桌面操作）

来源：`wonderwhy-er/desktop-commander`（5,456 stars）

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `get_ui_tree` | 获取UIA控件树 | Read |
| `click_element` | 点击控件 | Write-safe |
| `type_text` | 输入文本 | Write-safe |
| `send_keys` | 发送快捷键 | Write-safe |
| `get_window_list` | 列出窗口 | Read |
| `focus_window` | 聚焦窗口 | Write-safe |
| `take_screenshot` | 屏幕截图 | Read |

### 4.4 Filesystem-MCP（文件操作）— Python版

来源：PyPI `filesystem-mcp`（纯Python实现）

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `read_file` | 读取文件 | Read |
| `read_multiple_files` | 批量读取 | Read |
| `list_directory` | 列出目录 | Read |
| `directory_tree` | 递归目录树 | Read |
| `search_files` | 文件搜索 | Read |
| `write_file` | 写入文件 | Write-risky |
| `edit_file` | 选择性编辑（dryRun预览） | Write-risky |
| `create_directory` | 创建目录 | Write-safe |
| `get_file_info` | 文件元数据 | Read |

**安全控制**：启动时传入目录白名单，超出范围的操作被拒绝。

### 4.5 执行优先级策略

确定性控制（MCP） → 半确定性（GUI-Actor-3B SoM坐标+pyautogui） → 坐标Fallback → 人工介入。

---

## 5. 学习机制

### 5.1 技能库 — SKILL.md

AutoSkill提取流程：
1. 任务执行成功后，操作轨迹传入AutoSkill
2. 向量相似度检查（threshold=0.85），存在则merge升级版本，不存在则创建v0.1.0
3. 存入`skills/learned/`，embedding索引到本地ChromaDB

### 5.2 记忆 — Kimi memory + 本地备份

| 数据类型 | 存储位置 | 说明 |
|:---|:---|:---|
| 日常对话偏好 | Kimi memory tool | API自动管理 |
| 关键偏好备份 | 本地SQLite | API不可用时Fallback |
| 操作型技能 | SKILL.md文件 | 不依赖API |
| 反思摘要 | 本地SQLite | rethink输出摘要 |

### 5.3 反思 — Kimi rethink + 本地记录

操作失败后调用Kimi `rethink`工具分析原因，关键结论保存到本地SQLite。下次类似场景注入上下文。

---

## 6. 安全与权限控制

### 6.1 操作分级

| 级别 | 策略 | 示例 |
|:---|:---|:---|
| Read | 自动执行 | 文件读取、A11y树获取 |
| Write-safe | 自动执行+审计 | 截图缓存、临时文件 |
| Write-risky | 需确认 | 文件修改、配置变更 |
| Destructive | 强制人工审批 | 数据删除、权限变更 |

### 6.2 Kill Switch

**Layer 1 — 用户主动终止**：

实现方式：`pynput`全局键盘监听 + asyncio任务取消。

```python
# 设计要点：
# 1. 主对话循环阻塞在等Kimi API返回
# 2. 需要单独的键盘监听线程检测Ctrl+C或自定义快捷键
# 3. 检测到终止信号后，通过asyncio.CancelledError取消当前task
# 4. 清理状态：关闭MCP连接、保存当前进度、回到IDLE状态
```

- 用户按 `Ctrl+C`：取消当前操作，回到IDLE
- 用户输入 `/stop`：CLI命令，终止当前任务
- 用户输入 `/quit`：优雅退出，保存所有状态

**Layer 2 — 自动熔断**：
- 连续5次API失败 → 自动暂停，切换本地模型
- 连续3次操作失败 → 暂停任务，请求用户指导
- 同一UI状态循环超过3次 → 自动触发Reflect重规划

---

## 7. 内部架构

### 7.1 EventBus

自研EventBus：asyncio PriorityQueue + 发布订阅 + 中间件链。

核心事件类型：

| 事件域 | 类型 | 优先级 |
|:---|:---|:---:|
| 生命周期 | `SESSION_STARTED` / `SESSION_ENDED` | CRITICAL |
| 任务 | `TASK_RECEIVED` / `TASK_COMPLETED` / `TASK_FAILED` | HIGH |
| 动作 | `ACTION_EXECUTED` / `ACTION_FAILED` | NORMAL |
| 状态 | `AGENT_STATE_CHANGED` | HIGH |
| 感知 | `SCREEN_CAPTURED` / `UI_PARSED` | NORMAL |
| 系统 | `ERROR_OCCURRED` | HIGH |

### 7.2 状态机 — 8状态

```
                    ┌──────────┐
         ┌─────────▶│   IDLE   │◀────────┐
         │          └────┬─────┘         │
         │               │ 收到任务       │ 重置
         │               ▼                │
         │          ┌──────────┐         │
         │          │ PLANNING │         │
         │          └────┬─────┘         │
         │               │ 规划完成       │
         │               ▼                │
    人工输入      ┌──────────┐      全部完成
◀───────────────│EXECUTING │────────▶┐
         │       └────┬─────┘        │
         │            │ 需人工确认     │
         │            ▼               │
         │       ┌──────────┐        │
         └───────│WAITING   │        │
                 │_HUMAN    │        │
                 └────┬─────┘        │
                      │ 人工响应       │
                      ▼               │
                 ┌──────────┐        │
         ┌───────│ REFLECT  │        │
         │       │ (可选)    │        │
         │       └────┬─────┘        │
         │            │              │
    卡住/失败        重试            成功
         │            │              │
         ▼            ▼              ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐
    │   STUCK  │  │  ERROR   │  │ COMPLETED│
    └──────────┘  └──────────┘  └──────────┘
```

### 7.3 并发模型

asyncio主事件循环 + 2个线程池：

| 线程池 | 用途 | max_workers |
|:---|:---|:---:|
| 视觉推理池 | GUI-Actor-3B推理 | 2 |
| IO操作池 | 截图、文件读写、MCP通信 | 8 |

Kimi API调用走asyncio原生异步（httpx），不占用线程池。

### 7.4 数据存储

SQLite表：

| 表名 | 用途 |
|:---|:---|
| `user_preferences` | 关键偏好备份 |
| `reflections` | 反思摘要（FTS5搜索） |
| `skills` | 技能元数据 |
| `audit_log` | 操作审计 |
| `state_persistence` | 状态机恢复 |

ChromaDB：技能embedding + 记忆embedding的向量检索。

---

## 8. 项目结构

```
desktop-agent/
│
├── main.py                    # CLI入口，对话循环
├── config.py                  # Pydantic配置管理
├── config.yaml                # 用户配置文件（gitignore）
├── requirements.txt           # Python依赖
├── setup.py                   # 首次启动初始化脚本
│
├── agent/                     # Agent核心包
│   ├── __init__.py
│   ├── core.py                # 五级ReAct循环
│   ├── llm.py                 # LLM封装：Kimi API + Ollama
│   ├── perceive.py            # 感知层：截图压缩+OCR+GUI-Actor-3B+SoM
│   ├── reflect.py             # 反思层：Kimi rethink + 本地记录
│   ├── think.py               # 推理层：任务规划+工具选择
│   ├── verify.py              # 验证层：效果检查+死循环检测
│   ├── memory.py              # 记忆管理：Kimi memory + 本地备份
│   ├── skills.py              # SKILL.md读写与检索
│   ├── builtin_tools.py       # 内置工具：截图压缩、OCR
│   └── kill_switch.py         # Kill Switch：键盘监听+任务取消
│
├── mcp/                       # MCP客户端封装
│   ├── __init__.py
│   ├── client.py              # MCP stdio客户端：多server并发管理
│   ├── playwright.py          # playwright-mcp连接
│   ├── windows.py             # windows-mcp连接
│   └── filesystem.py          # filesystem-mcp连接
│
├── eventbus/                  # 事件总线
│   ├── __init__.py
│   ├── core.py
│   └── events.py
│
├── skills/                    # 技能库目录
│   └── (SKILL.md文件)
│
└── data/                      # 本地数据
    ├── memory.db              # SQLite
    └── cache/                 # 截图缓存
```

---

## 9. 依赖

```
# LLM
openai>=1.0              # Kimi API（OpenAI兼容）

# MCP
mcp>=1.0                 # MCP协议SDK
filesystem-mcp           # 文件操作MCP（Python版）

# Windows操作
pywinauto>=0.6           # UIA Fallback
pyautogui>=0.9           # 坐标Fallback
mss>=9.0                 # 高性能截图

# 视觉
rapidocr-onnxruntime     # OCR（CPU）
pillow>=10.0             # 截图压缩裁剪

# 向量存储
chromadb>=0.5            # 向量数据库

# 工具
httpx>=0.27              # 异步HTTP
pydantic>=2.0            # 配置验证
pydantic-settings>=2.0   # 配置管理
pyyaml>=6.0              # YAML配置
pynput>=1.7              # 全局键盘监听（Kill Switch）

# 日志
loguru>=0.7              # 开发日志
```

Ollama独立安装，GUI-Actor-3B需手动导入GGUF。

---

## 10. 首次启动流程

用户clone项目后，运行 `python setup.py` 自动完成初始化：

| 步骤 | 操作 | 预计时间 |
|:---|:---|:---:|
| 1 | 检测Python 3.11+，创建venv | 10s |
| 2 | `pip install -r requirements.txt` | 1-2min |
| 3 | 提示输入Kimi API key，写入`config.yaml` | 10s |
| 4 | 检测Ollama，未安装则提示下载`OllamaSetup.exe` | 2-5min |
| 5 | 检测GUI-Actor-3B，未安装则提示HuggingFace下载+转GGUF命令 | 10-30min |
| 6 | `playwright install chromium`（设置国内镜像加速） | 2-5min |
| 7 | 创建`data/`目录和SQLite数据库 | 5s |
| 8 | 运行冒烟测试：截图→OCR→Kimi对话 | 10s |

**Playwright Chromium安装常见问题**：

国内网络环境下`npx playwright install chromium`容易卡住。解决方案：

```bash
# 方案1：设置国内镜像
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
npx playwright install chromium

# 方案2：降级到稳定版本
npm install @anthropic/playwright-mcp-server@latest
# 然后手动下载对应Chromium版本到%USERPROFILE%\AppData\Local\ms-playwright\
```

---

## 11. 实施路线图

| 阶段 | 时间 | 内容 | 产出 |
|:---|:---:|:---|:---|
| 0 | 2天 | 环境：Ollama+GUI-Actor-3B+Python环境+项目脚手架 | 能运行hello world |
| 1 | 3天 | 核心：MCP多server连接+Kimi对话（12内置工具）+EventBus+FSM | 能对话，能调用MCP工具 |
| 2 | 3天 | 感知：截图压缩+OCR+GUI-Actor-3B部署+SoM标注 | Agent能"看懂"屏幕 |
| 3 | 3天 | 执行：playwright+windows+filesystem全工具+Fallback+Kill Switch | 能操控浏览器和桌面，能紧急停止 |
| 4 | 2天 | 学习：Kimi memory/rethink+SKILL.md+本地备份 | 会学习、记偏好 |
| 5 | 2天 | 安全：操作分级+审计日志+截图成本优化 | 安全可控、成本可预期 |
| 6 | 持续 | 日常使用、积累skills | 越用越顺手 |

**总周期：约2.5周出可用版本。**

---

## 12. 关键技术决策汇总

| 决策 | 选择 | 理由 |
|:---|:---|:---|
| UI检测 | GUI-Actor-3B + Verifier | 微软NeurIPS'25，UI专用微调，3B轻量 |
| 大脑 | Kimi K2.6 API | 256K上下文+12个内置工具 |
| 搜索/fetch/excel等 | Kimi内置工具 | 无需本地实现 |
| 记忆 | Kimi memory tool + 本地SQLite备份 | 利用API能力+关键数据本地Fallback |
| 反思 | Kimi rethink tool + 本地记录 | 利用API能力+保存关键结论 |
| 技能库 | SKILL.md (OpenClaw生态) | 33,000+ skills，Kimi无此能力 |
| 浏览器 | Playwright MCP | A11y tree，token效率最高 |
| 桌面 | Windows-MCP | UIA控件树确定性定位 |
| 文件 | filesystem-mcp Python | 纯Python，目录白名单安全 |
| OCR | RapidOCR CPU | 30ms，零显存 |
| 截图优化 | PIL压缩裁剪(800x600,quality=60) | 单图~200tokens，成本可控 |
| 本地模型管理 | Ollama + 手动GGUF导入 | Windows一键安装，OpenAI兼容 |
| MCP多Server | asyncio stdio_client多连接 | 同event loop管理3个server |
| Kill Switch | pynput键盘监听+asyncio取消 | Ctrl+C立即停止，状态可恢复 |
| 插件系统 | 保留设计，Phase 7实现 | 初始版本不必须 |
