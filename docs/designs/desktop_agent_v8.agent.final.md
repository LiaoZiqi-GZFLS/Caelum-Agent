# Windows桌面操作助手 — 技术方案 v8

> **版本**: v8.0 | **性质**: 个人CLI项目 | **平台**: Windows 10/11
> **大脑**: Kimi K2.6 (Moonshot AI, 12个内置工具)
> **UI检测**: GUI-Actor-3B + Verifier (NeurIPS'25, Microsoft) — Transformers原生推理

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
| **UI检测** | GUI-Actor-3B + Verifier — Transformers原生推理 | Microsoft, NeurIPS'25 |
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
| **MCP多Server** | asyncio多stdio连接 | mcp SDK |
| **Kill Switch** | pynput键盘监听 + asyncio取消 | — |

### 1.4 关于Ollama的说明

**v8删除Ollama**。原因：

1. GUI-Actor-3B使用微软定制的模型架构（`Qwen2_5_VLForConditionalGenerationWithPointer`），无法用Ollama加载
2. 已去掉Qwen2.5-3B路由模型（功能由Kimi 12个内置工具替代）
3. 本地模型仅剩GUI-Actor-3B一个，直接Transformers推理更简单，无需额外的模型管理进程

---

## 2. UI检测层 — GUI-Actor-3B + Verifier

### 2.1 选型说明

GUI-Actor来自微软NeurIPS'25论文《GUI-Actor: Coordinate-Free Visual Grounding for GUI Agents》，基于Qwen2.5-VL微调但使用**定制架构**。

| 模型 | 参数量 | ScreenSpot-Pro | 获取方式 |
|:---|:---:|:---:|:---|
| GUI-Actor-7B | 7B | 44.6 | HuggingFace PyTorch权重 |
| **GUI-Actor-3B** | **3B** | **~38** | **HuggingFace PyTorch权重** |

### 2.2 关键：定制模型架构

GUI-Actor-3B不是标准Qwen2.5-VL。微软做了以下定制：

| 定制点 | 标准Qwen2.5-VL | GUI-Actor-3B |
|:---|:---|:---|
| 模型类 | `Qwen2_5_VLForConditionalGeneration` | `Qwen2_5_VLForConditionalGenerationWithPointer` |
| 输出格式 | 纯文本 | 文本 + `topk_points`（候选坐标列表） |
| 推理函数 | `model.generate()` | `gui_actor.inference()` |
| Chat Template | 标准Qwen | `gui_actor.constants.chat_template` |
| 特殊Token | 标准 | `use_placeholder`机制 |

**这些定制使得GUI-Actor-3B无法转换为GGUF，也无法通过Ollama/vLLM/llama.cpp等通用推理框架加载。**

### 2.3 Transformers原生推理部署

**Step 1 — 安装依赖**

```
pip install transformers accelerate torch qwen-vl-utils
```

可选（性能优化）：
```
pip install flash-attn --no-build-isolation  # Flash Attention 2，约30%加速
```

**Step 2 — 下载权重**

```bash
pip install huggingface-cli
huggingface-cli download microsoft/GUI-Actor-3B-Qwen2.5-VL --local-dir ./models/gui-actor-3b
```

权重约6GB（safetensors格式）。

**Step 3 — 加载与推理**

```python
import torch
from transformers import AutoProcessor
from gui_actor.modeling_qwen25vl import Qwen2_5_VLForConditionalGenerationWithPointer
from gui_actor.inference import inference

# 加载
processor = AutoProcessor.from_pretrained("./models/gui-actor-3b")
tokenizer = processor.tokenizer
model = Qwen2_5_VLForConditionalGenerationWithPointer.from_pretrained(
    "./models/gui-actor-3b",
    torch_dtype=torch.bfloat16,
    device_map="cuda:0",
    attn_implementation="flash_attention_2"  # 或 "sdpa"
).eval()

# 推理
conversation = [
    {"role": "system", "content": [{"type": "text", "text": "You are a GUI agent..."}]},
    {"role": "user", "content": [
        {"type": "image", "image": screenshot_pil},
        {"type": "text", "text": instruction}
    ]}
]
pred = inference(conversation, model, tokenizer, processor, use_placeholder=True, topk=3)
px, py = pred["topk_points"][0]  # 最佳候选点 (0-1归一化坐标)
```

**显存需求**：

| 精度 | 显存 | 说明 |
|:---|:---:|:---|
| bfloat16 | ~6GB | 推荐，精度无损 |
| float16 | ~6GB | 与bf16类似 |
| int8 (bitsandbytes) | ~3.5GB | 轻微精度损失 |
| int4 (bitsandbytes) | ~2.5GB | 明显精度损失，不推荐 |

RTX 3060 12GB / RTX 4060 8GB均可运行bfloat16。

### 2.4 Verifier工作模式

```
截图 + 指令 ──▶ GUI-Actor-3B ──▶ topk=3候选坐标 [(x1,y1), (x2,y2), (x3,y3)]
                    │
                    ▼
            Verifier（同一模型二次推理）
       ┌────────┼────────┐
       ▼        ▼        ▼
      通过     否决    不确定
       │        │        │
       ▼        ▼        ▼
     执行     重试    请求用户
```

Verifier使用同一模型，但输入包含候选坐标+截图，让模型判断哪个坐标最合理。`topk=3`返回3个候选，Verifier从中选择最佳或全部否决。

### 2.5 感知融合流程

```
屏幕截图 ──▶ PIL压缩裁剪（降低token成本）
    │
    ├──▶ RapidOCR文字识别（30ms, CPU）
    │
    ├──▶ UIA/A11y控件树（确定性路径） ──┐
    │                                     ├──▶ 结构化环境描述 ──▶ Kimi
    └──▶ GUI-Actor-3B元素检测 ──▶ SoM标注 ──┘         (感知结果)
              (本地, ~300ms, ~6GB显存)
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

### 3.2 定价与截图成本

| 项目 | 价格 |
|:---|:---|
| 输入（缓存命中） | ¥6.5 / 1M tokens |
| 输入（缓存未命中） | ¥26 / 1M tokens |
| 输出 | ¥27 / 1M tokens |
| 联网搜索 | ¥0.03 / 次 |

**截图Token消耗**：

| 截图策略 | 图片tokens | 100步任务输入成本(缓存命中) |
|:---|:---:|:---:|
| 全屏原图1920x1080 | ~1024 | ¥0.67 |
| 50%缩放1280x720 | ~512 | ¥0.33 |
| **区域裁剪+压缩800x600,q60（推荐）** | **~205** | **¥0.14** |

**推荐策略**：区域裁剪+压缩（quality=60），月度中度使用（~500步）成本约¥0.7。

### 3.3 12个内置工具

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

### 3.4 Memory与Rethink的使用

- **Memory**：日常偏好由Kimi自动管理，关键偏好（"总是用Chrome"、"文件保存到D盘"）本地SQLite备份
- **Rethink**：操作失败后调用分析原因，关键结论保存本地SQLite
- **API不可用时**：完全使用本地SQLite + SKILL.md技能库

---

## 4. MCP工具集与多Server并发

### 4.1 三个MCP Server

| MCP Server | 启动命令 | 进程类型 |
|:---|:---|:---:|
| Playwright MCP | `npx @anthropic/playwright-mcp-server` | Node.js |
| Windows MCP | `python -m windows_mcp_server` | Python |
| Filesystem MCP | `filesystem-mcp /allowed/path` | Python |

使用`mcp` Python SDK的`stdio_client`在同一个asyncio event loop中管理3个独立连接。任一server断开时指数退避重连。

### 4.2 Playwright MCP（浏览器操作）

`browser_navigate` / `browser_click` / `browser_type` / `browser_select` / `browser_press_key` / `browser_get_accessibility_tree` / `browser_screenshot` / `browser_evaluate`

### 4.3 Windows-MCP（桌面操作）

`get_ui_tree` / `click_element` / `type_text` / `send_keys` / `get_window_list` / `focus_window` / `take_screenshot`

### 4.4 Filesystem-MCP（文件操作）— Python版

来源：PyPI `filesystem-mcp`（纯Python）。安装：`pip install filesystem-mcp`

`read_file` / `read_multiple_files` / `list_directory` / `directory_tree` / `search_files` / `write_file` / `edit_file`（dryRun预览） / `create_directory` / `get_file_info`

启动时传入目录白名单，超出范围的操作被拒绝。

---

## 5. 学习机制

### 5.1 技能库 — SKILL.md

AutoSkill提取流程：任务成功→操作轨迹→向量相似度检查（threshold=0.85）→merge升级或创建v0.1.0→存入`skills/learned/`→ChromaDB索引。

### 5.2 记忆 — Kimi memory + 本地SQLite备份

### 5.3 反思 — Kimi rethink + 本地记录

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

**Layer 1 — 用户主动终止**：`pynput`全局键盘监听 + asyncio任务取消
- `Ctrl+C`：取消当前操作，回到IDLE
- `/stop`：终止当前任务
- `/quit`：优雅退出

**Layer 2 — 自动熔断**：连续5次API失败暂停切换本地；连续3次操作失败请求用户指导；同一UI循环3次自动重规划。

---

## 7. 内部架构

### 7.1 EventBus

asyncio PriorityQueue + 发布订阅 + 中间件链。核心事件：生命周期/任务/动作/状态/感知/系统。

### 7.2 状态机 — 8状态

IDLE → PLANNING → EXECUTING → (WAITING_HUMAN →) REFLECT(可选) → COMPLETED/ERROR/STUCK

### 7.3 并发模型

asyncio主事件循环 + 2个线程池：

| 线程池 | 用途 | max_workers |
|:---|:---|:---:|
| 视觉推理池 | GUI-Actor-3B推理 | 2 |
| IO操作池 | 截图、文件读写、MCP通信 | 8 |

Kimi API走asyncio原生异步（httpx）。

### 7.4 数据存储

SQLite 5张表：`user_preferences` / `reflections` / `skills` / `audit_log` / `state_persistence`。ChromaDB向量检索。

---

## 8. 项目结构

```
desktop-agent/
│
├── main.py                    # CLI入口
├── config.py                  # Pydantic配置
├── config.yaml                # 用户配置（gitignore）
├── requirements.txt           # Python依赖
├── setup.py                   # 首次启动初始化
│
├── agent/                     # Agent核心
│   ├── __init__.py
│   ├── core.py                # 五级ReAct循环
│   ├── llm.py                 # Kimi API封装
│   ├── perceive.py            # 感知层（截图+OCR+GUI-Actor-3B+SoM）
│   ├── reflect.py             # 反思层
│   ├── think.py               # 推理层
│   ├── verify.py              # 验证层
│   ├── memory.py              # 记忆管理
│   ├── skills.py              # SKILL.md管理
│   ├── builtin_tools.py       # 内置工具（截图、OCR）
│   └── kill_switch.py         # Kill Switch
│
├── ui_detector/               # GUI-Actor-3B UI检测模块
│   ├── __init__.py
│   ├── model.py               # 模型加载与推理封装
│   ├── verifier.py            # Verifier验证逻辑
│   └── som.py                 # Set-of-Mark标注生成
│
├── mcp/                       # MCP客户端
│   ├── __init__.py
│   ├── client.py              # 多server并发管理
│   ├── playwright.py
│   ├── windows.py
│   └── filesystem.py
│
├── eventbus/                  # 事件总线
│   ├── __init__.py
│   ├── core.py
│   └── events.py
│
├── skills/                    # 技能库
│
└── data/                      # 本地数据
    ├── memory.db
    └── cache/
```

---

## 9. 依赖

```
# 核心
openai>=1.0              # Kimi API
mcp>=1.0                 # MCP协议SDK
filesystem-mcp           # 文件MCP（Python）

# GUI-Actor-3B（Transformers原生推理）
transformers>=4.48       # 模型加载
accelerate>=1.0          # device_map自动分配
torch>=2.0               # PyTorch
qwen-vl-utils            # Qwen-VL图像处理
bitsandbytes>=0.45       # 可选：INT8量化节省显存

# Windows操作
pywinauto>=0.6           # UIA Fallback
pyautogui>=0.9           # 坐标Fallback
mss>=9.0                 # 截图

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
pynput>=1.7              # 键盘监听（Kill Switch）

# 日志
loguru>=0.7              # 开发日志
```

**无需Ollama**。

---

## 10. 首次启动流程

运行 `python setup.py`：

| 步骤 | 操作 | 时间 |
|:---|:---|:---:|
| 1 | 检测Python 3.11+ | 10s |
| 2 | `pip install -r requirements.txt` | 2-5min |
| 3 | 提示输入Kimi API key | 10s |
| 4 | 下载GUI-Actor-3B权重（HuggingFace） | 10-30min |
| 5 | `playwright install chromium`（国内镜像加速） | 2-5min |
| 6 | 创建data/目录和SQLite | 5s |
| 7 | 冒烟测试 | 10s |

**Playwright Chromium国内加速**：
```bash
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
npx playwright install chromium
```

---

## 11. 实施路线图

| 阶段 | 时间 | 内容 | 产出 |
|:---|:---:|:---|:---|
| 0 | 2天 | 环境：依赖安装+GUI-Actor-3B权重下载+项目脚手架 | 能运行 |
| 1 | 3天 | 核心：MCP多server+Kimi对话（12内置工具）+EventBus+FSM | 能对话，能调MCP工具 |
| 2 | 3天 | 感知：截图压缩+OCR+GUI-Actor-3B推理+SoM标注 | Agent能"看懂"屏幕 |
| 3 | 3天 | 执行：全部MCP工具+Fallback+Kill Switch | 能操控浏览器和桌面 |
| 4 | 2天 | 学习：Kimi memory/rethink+SKILL.md+本地备份 | 会学习 |
| 5 | 2天 | 安全：操作分级+审计+截图成本优化 | 安全可控 |
| 6 | 持续 | 日常使用、积累skills | 越用越顺手 |

**总周期：约2.5周。**

---

## 12. 关键技术决策汇总

| 决策 | 选择 | 理由 |
|:---|:---|:---|
| UI检测 | GUI-Actor-3B + Verifier | 微软NeurIPS'25，定制Pointer架构，ScreenSpot-Pro ~38 |
| GUI-Actor-3B部署 | Transformers原生推理 | 定制模型类无法用Ollama/GGUF，直接Python加载 |
| 本地模型管理 | **无需Ollama** | 仅剩GUI-Actor-3B一个模型，直接Transformers推理 |
| 大脑 | Kimi K2.6 API | 256K上下文+12内置工具 |
| 搜索/fetch/excel等 | Kimi内置工具 | 无需本地实现 |
| 记忆 | Kimi memory + 本地SQLite备份 | 利用API+关键数据本地Fallback |
| 反思 | Kimi rethink + 本地记录 | 利用API+保存关键结论 |
| 技能库 | SKILL.md (OpenClaw生态) | 33,000+ skills |
| 浏览器 | Playwright MCP | A11y tree，token效率最高 |
| 桌面 | Windows-MCP | UIA控件树确定性定位 |
| 文件 | filesystem-mcp Python | 纯Python，目录白名单 |
| OCR | RapidOCR CPU | 30ms，零显存 |
| 截图优化 | PIL压缩裁剪(800x600,q60) | ~200tokens/图 |
| MCP多Server | asyncio stdio_client | 同event loop管理3连接 |
| Kill Switch | pynput+asyncio取消 | Ctrl+C立即停止 |
