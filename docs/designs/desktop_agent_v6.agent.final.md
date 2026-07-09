# Windows桌面操作助手 — 技术方案 v6

> **版本**: v6.0 | **性质**: 个人CLI项目 | **平台**: Windows 10/11
> **大脑**: Kimi K2.6 (Moonshot AI, 12个内置工具) | **本地模型管理**: Ollama
> **UI检测**: GUI-Actor-3B + Verifier (NeurIPS'25, Microsoft)

---

## 1. 概述与核心架构

### 1.1 定位

一个Windows命令行桌面操作Agent。用户通过自然语言下达指令，Agent自主操控浏览器和Windows桌面应用完成任务。

**双域覆盖**：浏览器（Playwright MCP accessibility tree）+ Windows桌面（Windows-MCP UIA控件树）。

**不做的事**：不设计GUI、不做CI/CD、不做打包签名、不做自动更新。

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
| **截图** | mss | Python库 |
| **技能库** | SKILL.md (OpenClaw生态) | 33,000+ skills |
| **记忆** | Kimi memory tool + 本地SQLite备份 | Kimi API层 |
| **反思** | Kimi rethink tool + 本地记录 | Kimi API层 |
| **状态机** | 自研FSM (8状态) | — |
| **事件总线** | 自研EventBus (asyncio) | — |

---

## 2. UI检测层 — GUI-Actor-3B + Verifier

### 2.1 选型说明

GUI-Actor来自微软NeurIPS'25论文《Training GUI Models to Be Generalists, Not Specialists》，基于Qwen2.5-VL系列微调。

| 模型 | 参数量 | ScreenSpot-Pro | 显存 | Ollama支持 |
|:---|:---:|:---:|:---:|:---:|
| GUI-Actor-7B | 7B | 44.6 | ~6GB | 需手动导入 |
| **GUI-Actor-3B** | **3B** | **~38**（估算） | **~3.5GB** | 需手动导入 |
| Qwen2.5-VL-7B（官方） | 7B | 27.6 | ~6GB | `ollama pull qwen2.5vl:7b` |

选择GUI-Actor-3B的原因：
1. **微软针对UI专门微调**：在大量GUI截图+控件标注数据上训练，UI理解能力远超官方Qwen2.5-VL-7B（44.6 vs 27.6）
2. **3B参数量轻量**：适合个人项目消费级显卡（~3.5GB显存），8GB显卡可常驻
3. **Verifier架构**：论文提出的Guider-Verifier范式——Guider（GUI-Actor-3B）生成候选操作，Verifier（同模型或轻量分类器）验证操作合理性，降低幻觉

### 2.2 Verifier工作模式

```
截图 ──▶ GUI-Actor-3B (Guider) ──▶ 候选操作列表 [click(x1,y1), type("text"), ...]
                                         │
                                         ▼
                              Verifier (同一模型或轻量分类器)
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
                  通过                  否决               不确定
                    │                    │                    │
                    ▼                    ▼                    ▼
               执行操作           返回Guider重试       请求用户确认
```

Verifier评估维度：目标元素是否存在、操作类型是否匹配、坐标是否在合理范围。

### 2.3 Ollama导入

GUI-Actor-3B需手动导入Ollama（非官方库模型）：

1. 从Hugging Face下载GUI-Actor-3B的GGUF格式权重（如`GUI-Actor-3B-Q4_K_M.gguf`）
2. 编写Modelfile定义对话模板和系统提示
3. `ollama create gui-actor-3b -f Modelfile`

Modelfile关键配置：

```
FROM ./GUI-Actor-3B-Q4_K_M.gguf
PARAMETER temperature 0.1
PARAMETER top_p 0.9
SYSTEM "你是一个GUI操作助手。分析屏幕截图，列出所有可交互UI元素，返回格式：[(x1,y1,x2,y2, element_type, label)]"
```

### 2.4 感知融合流程

```
屏幕截图 ──▶ RapidOCR文字识别（30ms, CPU）
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

### 3.2 定价

| 项目 | 价格 |
|:---|:---|
| 输入（缓存命中） | ¥6.5 / 1M tokens |
| 输入（缓存未命中） | ¥26 / 1M tokens |
| 输出 | ¥27 / 1M tokens |
| 联网搜索 | ¥0.025 / 次 |

### 3.3 12个内置工具

**以下工具由Kimi服务端执行，Agent只需在tools参数中注册名称即可。**

| 工具名 | 功能 | 替代了我们的什么 |
|:---|:---|:---|
| `web-search` | 实时互联网搜索 | 外部搜索API（Tavily/Brave等） |
| `memory` | 对话历史+用户偏好持久化 | Mem0（API层替代） |
| `rethink` | 整理想法、反思分析 | Reflexion的部分实现 |
| `fetch` | URL内容提取转Markdown | Jina Reader/Crawl4AI |
| `excel` | Excel/CSV分析 | 本地pandas处理 |
| `code_runner` | 安全执行Python代码 | 本地Python沙箱 |
| `convert` | 单位换算（长度/质量/货币等） | 单位转换代码 |
| `date` | 日期时间处理 | 日期处理代码 |
| `base64` | Base64编解码 | base64代码 |
| `quickjs` | 安全执行JavaScript | JS沙箱 |
| `random-choice` | 随机选择 | random代码 |
| `mew` | 娱乐工具 | — |

**使用方式**：在调用`chat.completions.create()`时通过`tools`参数注册工具名，Kimi自主决定是否调用。调用结果通过`role="tool"`回传。

### 3.4 Memory工具的具体使用

Kimi `memory` tool的数据存储在Moonshot服务端。对个人项目设计如下策略：

- **利用Kimi memory**：日常对话偏好、短期上下文由Kimi自动管理
- **本地SQLite备份**：关键偏好（如"总是用Chrome"、"文件保存到D盘"）同时在本地`data/memory.db`备份
- **SKILL.md技能库**：操作型技能（"如何在Excel创建透视表"）存为SKILL.md文件，不依赖API记忆
- **API不可用时**：完全使用本地SQLite记忆 + SKILL.md技能库运行

这种设计平衡了便利性（利用Kimi能力）和可靠性（关键数据本地备份）。

### 3.5 Rethink工具的具体使用

`rethink` tool用于整理想法和反思分析。在Agent中的使用场景：

- 操作失败后，将失败上下文传给Kimi，调用`rethink`让Kimi分析原因
- 将`rethink`的输出摘要（非原始数据）保存到本地SQLite
- 下次遇到类似场景时，将历史反思摘要注入上下文

这比Reflexion更轻量——利用了Kimi的反思能力，只保存关键结论到本地。

---

## 4. MCP工具集

### 4.1 Playwright MCP（浏览器操作）

来源：Anthropic官方 `@anthropic/playwright-mcp-server`（31.2K stars）

启动：`npx @anthropic/playwright-mcp-server`，stdio连接。

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

### 4.2 Windows-MCP（桌面操作）

来源：`wonderwhy-er/desktop-commander`（5,456 stars）

启动：`python -m windows_mcp_server`，stdio连接。

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `get_ui_tree` | 获取UIA控件树 | Read |
| `click_element` | 点击控件 | Write-safe |
| `type_text` | 输入文本 | Write-safe |
| `send_keys` | 发送快捷键 | Write-safe |
| `get_window_list` | 列出窗口 | Read |
| `focus_window` | 聚焦窗口 | Write-safe |
| `take_screenshot` | 屏幕截图 | Read |

### 4.3 Filesystem-MCP（文件操作）— Python版

来源：PyPI `filesystem-mcp`（纯Python实现）

安装：`pip install filesystem-mcp`

启动：`filesystem-mcp`（stdio模式）

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

### 4.4 执行优先级策略

确定性控制（MCP） → 半确定性（GUI-Actor-3B SoM坐标+pyautogui） → 坐标Fallback → 人工介入。

---

## 5. 学习机制

### 5.1 技能库 — SKILL.md

技能以SKILL.md格式存储在`./skills/`目录，兼容OpenClaw生态（33,000+社区skills可复用）。

AutoSkill（ECNU/上海AI Lab）提取流程：
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

操作失败后调用Kimi `rethink`工具分析原因，将关键结论保存到本地SQLite。下次类似场景注入上下文。

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

- **Layer 1**：用户输入`/stop`立即终止当前任务
- **Layer 2**：连续5次API失败或3次操作失败自动暂停，等待用户指令

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
│
├── agent/                     # Agent核心包
│   ├── __init__.py
│   ├── core.py                # 五级ReAct循环
│   ├── llm.py                 # LLM封装：Kimi API + Ollama
│   ├── perceive.py            # 感知层：截图+OCR+GUI-Actor-3B+SoM
│   ├── reflect.py             # 反思层：Kimi rethink + 本地记录
│   ├── think.py               # 推理层：任务规划+工具选择
│   ├── verify.py              # 验证层：效果检查+死循环检测
│   ├── memory.py              # 记忆管理：Kimi memory + 本地备份
│   ├── skills.py              # SKILL.md读写与检索
│   └── builtin_tools.py       # 内置工具：截图、OCR
│
├── mcp/                       # MCP客户端封装
│   ├── __init__.py
│   ├── client.py              # MCP stdio客户端基类
│   ├── playwright.py          # playwright-mcp连接
│   ├── windows.py             # windows-mcp连接
│   └── filesystem.py          # filesystem-mcp连接
│
├── eventbus/                  # 事件总线
│   ├── __init__.py
│   ├── core.py
│   └── events.py
│
├── plugins/                   # 插件目录
│   └── base.py                # Plugin ABC基类
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
pillow>=10.0             # 图像处理

# 向量存储
chromadb>=0.5            # 向量数据库

# 工具
httpx>=0.27              # 异步HTTP
pydantic>=2.0            # 配置验证
pydantic-settings>=2.0   # 配置管理
pyyaml>=6.0              # YAML配置

# 日志
loguru>=0.7              # 开发日志
```

Ollama独立安装，GUI-Actor-3B需手动导入GGUF。

---

## 10. 实施路线图

| 阶段 | 时间 | 内容 | 产出 |
|:---|:---:|:---|:---|
| 0 | 3天 | 环境：Ollama安装+GUI-Actor-3B导入+Python环境+项目脚手架 | 能运行hello world |
| 1 | 4天 | 核心：MCP连接+Kimi对话（含12个内置工具）+EventBus+FSM | 能对话，能调用MCP工具 |
| 2 | 4天 | 感知：截图+OCR+GUI-Actor-3B部署+SoM标注 | Agent能"看懂"屏幕 |
| 3 | 4天 | 执行：playwright+windows+filesystem全工具+Fallback | 能操控浏览器和桌面 |
| 4 | 3天 | 学习：Kimi memory/rethink集成+SKILL.md+本地备份 | 会学习、记偏好 |
| 5 | 2天 | 安全：操作分级+Kill Switch+审计日志 | 安全可控 |
| 6 | 持续 | 日常使用、积累skills | 越用越顺手 |

**总周期：约3周出可用版本。**

---

## 11. 关键技术决策汇总

| 决策 | 选择 | 理由 |
|:---|:---|:---|
| UI检测 | GUI-Actor-3B + Verifier | 微软NeurIPS'25，UI专用微调，3B轻量 |
| 大脑 | Kimi K2.6 API | 256K上下文+12个内置工具 |
| 搜索/fetch/excel等 | Kimi内置工具 | 无需本地实现，直接调用 |
| 记忆 | Kimi memory tool + 本地SQLite备份 | 利用API能力+关键数据本地 Fallback |
| 反思 | Kimi rethink tool + 本地记录 | 利用API能力+保存关键结论 |
| 技能库 | SKILL.md (OpenClaw生态) | 33,000+ skills，Kimi无此能力 |
| 浏览器 | Playwright MCP | A11y tree，token效率最高 |
| 桌面 | Windows-MCP | UIA控件树确定性定位 |
| 文件 | filesystem-mcp Python | 纯Python，目录白名单安全 |
| OCR | RapidOCR CPU | 30ms，零显存 |
| 本地模型管理 | Ollama | Windows一键安装 |
