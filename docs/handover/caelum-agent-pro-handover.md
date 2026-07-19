# Caelum-Agent-Pro 交接文档

> **版本**：2026-07-13
> **来源项目**：Caelum-Agent（`E:\code\project\Caelum-Agent`，主分支 `main`）
> **目标项目**：Caelum-Agent-Pro（基于 Microsoft UFO2 二次开发，融入 Agent-S2 规划机制，适配 Kimi K3 API）
> **阅读对象**：接手下一阶段开发的人员（含 AI agent）

---

## 目录

1. [项目背景与目标](#1-项目背景与目标)
2. [现状盘点：Caelum-Agent 做了什么](#2-现状盘点caelum-agent-做了什么)
3. [UFO2 架构深度解析](#3-ufo2-架构深度解析)
4. [Agent-S2 规划机制解析](#4-agent-s2-规划机制解析)
5. [三项目对比：差距与迁移路径](#5-三项目对比差距与迁移路径)
6. [Caelum-Agent-Pro 融合架构设计](#6-caelum-agent-pro-融合架构设计)
7. [Kimi K3 适配方案](#7-kimi-k26-适配方案)
8. [实现路线图](#8-实现路线图)
9. [关键风险与注意事项](#9-关键风险与注意事项)
10. [附录：参考资源与映射表](#10-附录参考资源与映射表)

---

## 1. 项目背景与目标

### 1.1 Caelum-Agent 当前定位

Caelum-Agent 是一个**从零构建的**个人 Windows CLI 桌面操作 agent，6000+ 行 Python，681 个单元测试。核心特色：

- **自研 ReAct 循环**：Perceive → Reflect(on failure) → Think → Act → Verify，五阶段
- **多源感知融合**：UIA 无障碍树 + OCR 文本 + YOLO 图标检测 + Florence-2 图标描述 → SoM 标注
- **双 MCP 通道**：Playwright MCP（浏览器）+ Windows MCP（桌面），stdin/stdout 通信
- **Kimi K3 独家适配**：Formula 内置工具注册/执行、Files API 三种 purpose、Partial Mode、JSON Mode、熔断器等全部对齐
- **自学习链路**：SkillLearner（成功轨迹→SKILL.md）+ 中断清算（LearningSettler 判定完成度）

### 1.2 为什么基于 UFO2 重构

| 维度 | Caelum-Agent 现状 | UFO2 提供 |
|------|-------------------|-----------|
| 架构 | 单一 AgentOrchestrator（1800+ 行） | 多 agent 分层：HostAgent + AppAgent + 策略层，天然可拆 |
| 规划 | ReAct 即时反应，无显式规划阶段 | HostAgent 子任务分解 + AppAgent 4 阶段处理管线 |
| 跨应用 | 通过 MCP 工具名区分，无应用级抽象 | AppAgent 为每个应用实例化专用执行器，带应用知识 |
| Windows 集成 | MCP 间接访问 UIA、截图等 | Win32/WinCOM/UIA 原生调用 + MCP 命令层 |
| 内存/知识 | SQLite + ChromaDB + Kimi memory 公式 | Blackboard 黑板共享内存 + RAG 知识检索 + 执行历史 |

**目标不是重写 UFO2，而是站在它的肩膀上**：继承它的架构骨架、Windows 原生集成能力、多 agent 编排模型，再把 Caelum-Agent 的三个差异化能力融合进去——Kimi API 全栈适配、感知融合管线（OCR + YOLO + Florence-2）、自学习体系。

### 1.3 为什么要融入 Agent-S2 的规划机制

Agent-S2 的**主动层次规划（PHP）**是目前 GUI agent 领域最强的规划范式：

- **双层时间尺度**：Manager 按子目标规划（分钟级），Worker 按原子动作执行（秒级）
- **主动重规划**：每完成一个子目标就基于最新观察更新剩余计划（而非失败时才重规划）
- **图上推理**：规划被编码为 DAG，允许并行子任务、条件分支

UFO2 的 HostAgent 做任务分解但缺乏显式的计划更新机制；Agent-S2 的 PHP 正好可以补上。融合方案见 §6.1。

---

## 2. 现状盘点：Caelum-Agent 做了什么

### 2.1 项目文件清单（关键模块）

```
desktop-agent/
├── main.py                          # CLI 入口：交互/非交互模式、MCP 启动、风险批准
├── config.yaml.example              # 完整配置模板（Kimi + MCP + 视觉 + 安全）
├── setup.py                         # 首次安装：venv/pip/权重下载/Chromium/smoke test
├── agent/                           # -- 核心 agent 层 --
│   ├── orchestrator.py              # ★ ReAct 主循环（1800+ 行，需拆解）
│   ├── llm_client.py                # Kimi: chat + Formula 注册/执行 + 本地工具分派
│   ├── config.py                    # Pydantic 配置全量：LLM/MCP/视觉/安全/记忆/反思
│   ├── state_machine.py             # 8 状态 FSM（IDLE→PLANNING→EXECUTING→VERIFYING→...）
│   ├── perception.py                # ★ 感知管线：截图→OCR→UIA→YOLO→Florence-2→融合→SoM
│   ├── snapshot_parser.py           # Windows-MCP / Playwright a11y 树→UIElement
│   ├── tools.py                     # RestrictedCodeRunner + MCP 工具映射
│   ├── reflection.py                # 反思引擎：Kimi rethink 优先→SQLite 回落
│   ├── memory.py                    # SQLite + ChromaDB 双存储
│   ├── skills.py                    # SkillLearner: 轨迹→SKILL.md 自动学习+合并
│   ├── pending_learning.py          # LearningSettler: 中断轨迹跨启动清算
│   ├── kimi_memory.py               # Kimi memory/rethink 公式的程序化适配
│   ├── file_reader.py               # ReadDocument: Files API file-extract + sha256 缓存
│   ├── media.py                     # ViewMedia: 图片/视频 ms:// 上传 + 压缩管线
│   ├── content_writer.py            # DraftContent: 写作子代理 + Partial Mode
│   ├── image_gen.py                 # GenerateImage: SVG→PNG→自评闭环（JSON Mode）
│   ├── task_list.py                 # 模型管理的任务列表
│   ├── cli_presenter.py             # Rich terminal 事件消费者
│   ├── kill_switch.py               # pynput 全局监听 Ctrl+C
│   ├── focus_guard.py               # 前台窗口聚焦看门狗
│   ├── self_window.py               # 自有控制台窗口显隐控制
│   ├── history_archive.py           # JSONL 飞行记录（脱敏+去 base64）
│   ├── choice_menu.py               # msvcrt 键盘选择菜单（RequestHumanHelp）
│   └── preview_points.py            # 坐标猜测预览标记
├── ui_detector/                     # -- 视觉检测层 --
│   ├── yolo_detector.py             # YOLOv8 icon_detect（OmniParser），~50ms/帧 GPU
│   ├── icon_captioner.py            # Florence-2 图标描述，批量+batch decode
│   ├── fusion.py                    # OCR+图标框 IoU 融合/去重，生成统一 SoM 标记
│   └── visualizer.py               # 红框编号标注渲染
├── mcp_client/__init__.py           # MCP 多服务器 stdio 客户端+重连+噪声过滤
├── eventbus/                        # asyncio EventBus + 事件 dataclasses
├── skills/                          # SKILL.md 技能库（learned/ 子目录自动生成）
├── tests/                           # pytest 681 个测试（含 fakes.py 共享假体）
└── docs/                            # 设计文档+Kimi API 手册（本目录）
```

### 2.2 已攻克的关键技术点

| 技术点 | 方案 | 文件 |
|--------|------|------|
| **Kimi Chat Completions** | OpenAI SDK 兼容端点，tools 三态语义（省略/列表/None），Kimi 拒连续同角色 message 的合并约束 | `agent/llm_client.py:124-146`, `agent/orchestrator.py:1514-1550` |
| **Kimi Formula 注册** | GET `/formulas/{uri}/tools` 拉定义，`_convert_formula_tool` 兼容两种返回格式；URI 连字符↔工具名下划线映射 | `agent/llm_client.py:28-67` |
| **Kimi Formula 执行** | POST `/formulas/{uri}/fibers`，`encrypted_output` 兜底；memory/rethink 通过伪造 ToolCall 程序化调用 | `agent/llm_client.py:110-122`, `agent/kimi_memory.py` |
| **Kimi Files API** | 三种 purpose 三套生命周期：file-extract（即删+sha256 缓存）/image（40K 降采样）/video（15fps1080p ffmpeg 重编码）；双 sweep（启动+任务结束） | `agent/file_reader.py`, `agent/media.py` |
| **Kimi Partial Mode** | `{"role":"assistant","content":prefill,"partial":true}` + 响应不含 prefill 需手动拼接；与 response_format 互斥 | `agent/content_writer.py:86-98` |
| **Kimi JSON Mode** | `response_format={"type":"json_object"}`，GenerateImage 自评用；LearningSettler 的反面教材是容错解析 | `agent/image_gen.py:135-154`, `agent/pending_learning.py:40-58` |
| **熔断器** | 连续 5 次任意异常→WAITING_HUMAN+退出；成功清零；重试 WARNING 日志带 N/5 计数；与动作熔断独立 | `agent/orchestrator.py:1029-1062` |
| **感知融合** | OCR+YOLO→IoU 15% 合并 / 5% 去重→Florence-2 描述裸图标→SoM 标注；DPI 逆归一化；ZoomRegion 区域重感知+坐标平移 | `agent/perception.py`, `ui_detector/fusion.py`, `ui_detector/icon_captioner.py` |
| **定位降级链** | UIA label→SoM label→NearbyLabels 三角定位→ZoomRegion→UpgradeVision/CaptureWindow→PreviewPoints 坐标猜测 | `agent/orchestrator.py:1203-1240` |
| **中断学习清算** | 熔断/kill switch 退出时轨迹入 pending_learning 表；下次启动 LLM 判定完成度→成功 skill 或失败 reflection；3 次判定失败兜底删除 | `agent/pending_learning.py` |
| **MCP 噪声过滤** | `_UpstreamNoiseFilter` 抑制 windows-mcp `tree_node` 上游 bug 的 stderr 噪声，60s 汇总一次 →文件日志 | `mcp_client/__init__.py` |
| **视窗控制** | SelfWindow 隐藏自有控制台免入截图；FocusGuard 前台窗口聚焦看门狗（AttachThreadInput 配方） | `agent/self_window.py`, `agent/focus_guard.py` |
| **模型工具分派** | execute_tool_calls 的分派顺序：Formula→本地 handler→MCP 转发（按 server__tool 命名截胡），单工具失败不打断整批 | `agent/llm_client.py:148-179`, `agent/tools.py` |

### 2.3 已暴露的问题与教训

| 问题 | 教训 | 对 Pro 的启示 |
|------|------|--------------|
| `agent/orchestrator.py` 1800+ 行 | 单文件容纳 ReAct 循环 + 工具接线 + 感知调用 + 消息格式化 + 熔断器 + 学习调度，难以维护 | 必须按 UFO2 的策略层/命令层/状态层拆分 |
| LLM 重试无间隔 | 外层 `TransientAPIError→continue` 在 429 限流下火上浇油 | 加退避（参照 UFO2 AppAgent 的 3 次重试 + 指数退避） |
| 感知每轮都跑全套 | DPI 归一化+OCR+YOLO+Florence-2 每轮串行，无感知缓存 | UFO2 的 4 阶段管线天然支持条件跳过；Agent-S2 的观察差分也可参考 |
| 无显式规划阶段 | ReAct 即兴反应容易偏航，Verify 是事后补救 | Agent-S2 PHP 的事前规划+主动重规划是根本解决方案 |
| Kimi API 的 model 名拼写容错 | `kimi-k2-6→kimi-k3` 这种坑已踩过 | 配置校验沿用 |
| windows-mcp 上游 tree_node bug | 写好了噪声过滤方案和上游 issue 草稿 | UFO2 原生调 UIA，不受此影响 |

---

## 3. UFO2 架构深度解析

> 来源：[microsoft/UFO](https://github.com/microsoft/UFO)，论文 arXiv:2504.14603（April 2025）

### 3.1 三层架构

```
┌─────────────────────────────────────────────────────────┐
│  Level 1: State Layer (FSM)                             │
│  - Agent lifecycle: init → run → finish/pause/resume    │
│  - 状态机管理 agent 生老病死；状态变更通过 transition()  │
├─────────────────────────────────────────────────────────┤
│  Level 2: Strategy Layer (Processor → Strategy)         │
│  - Processor = Phase orchestrator                       │
│  - Strategy = 可插拔的处理逻辑：Screenshot / LLM /       │
│    Action / Memory，每轮按策略链顺序执行                  │
│  - HostAgent: TaskDecompositionStrategy                 │
│  - AppAgent:  ComposedStrategy（4 阶段管线）             │
├─────────────────────────────────────────────────────────┤
│  Level 3: Command Layer (MCP)                           │
│  - 原子系统操作：Click, Type, Screenshot, Snapshot...    │
│  - 统一 GUI-API 动作编排（Puppeteer 选路）               │
└─────────────────────────────────────────────────────────┘
```

### 3.2 HostAgent —— 桌面编排器

**责任**：

- **任务分解**：将用户指令分解为子任务序列（自然语言列表）
- **应用选择**：决定每个子任务需要哪个 Windows 应用
- **AppAgent 管理**：为每个子任务创建/重用 AppAgent，传入子任务描述
- **跨应用协调**：管理应用间数据传递（剪贴板、文件路径等）
- **用户交互**：需要确认时向用户提问

**关键组件**：

| 组件 | 说明 |
|------|------|
| `TaskDecompositionStrategy` | LLM 驱动的任务分解为子任务列表 |
| `AppSelectionStrategy` | 为每个子任务匹配合适的应用 |
| `HostAgentProcessor` | 编排 HostAgent 策略链 |
| `Blackboard` | 全局共享内存，所有 agent 可见 |
| `Session` | 跨多轮对话的持久会话管理 |

### 3.3 AppAgent —— 应用执行器

**责任**：

- 在单个应用中执行一个子任务
- ReAct 风格循环：Observe → Think → Act
- 统一 GUI-API 动作编排（可通过 API 完成的绝不用多步 GUI）

**7 状态 FSM**：

```
CONTINUE ──→ FINISH
    │            ↑
    ├──→ ERROR   │（下一轮感知后继续）
    ├──→ FAIL    │
    ├──→ PENDING │（等人输入）
    ├──→ CONFIRM │（等人确认）
    └──→ SCREENSHOT（重新截图标注）
```

**4 阶段处理管线（每轮执行）**：

```
Phase 1: DATA_COLLECTION  → ComposedStrategy(ScreenshotCapture + ControlInfo)
          │                   UIA 控件树 + OmniParser 视觉检测 → IoU 融合
          ▼
Phase 2: LLM_INTERACTION  → Prompt 构建（含 Blackboard 上下文 + RAG 知识 + 控件信息 + 截图）
          │                   LLM 调用 → JSON 解析（重试 3 次）
          ▼
Phase 3: ACTION_EXECUTION → Puppeteer 选路：API 优先｜GUI 回退
          │                   通过 MCP 命令执行（fail_fast=False）
          ▼
Phase 4: MEMORY_UPDATE    → 更新 Agent 内存 + 共享 Blackboard
                             （fail_fast=False，失败不阻塞）
```

### 3.4 Blackboard —— 共享内存

所有 agent 可见的全局命名空间：

| 字段 | 内容 |
|------|------|
| `questions` | Agent 提问 + 用户回答记录 |
| `requests` | 历史用户请求 |
| `trajectories` | 逐步动作/决策记录 |
| `screenshots` | 值得保留的关键截图 |

`blackboard_to_prompt()` 方法将 Blackboard 内容转换为 LLM prompt 上下文，实现松耦合的 agent 间通信。

### 3.5 混合控制检测

UFO2 的感知方案（两路 + 融合，与 Caelum-Agent 的 OCR+YOLO 融合思路一致但检测源不同）：

```
UIA 控件树 ──→ 结构化控件信息（名称/类型/位置/层级）
                                   │
                          IoU 重叠 → 去重融合 → 统一控件图
                                   │
OmniParser ──→ 视觉检测（非标准控件的像素级定位）
```

### 3.6 Puppeteer —— 统一 GUI-API 动作编排

每个动作在两个层面可选：

- **API 层**：Win32/WinCOM 直接调用（如 `Workbook.SaveAs()` 替代 5 步点击菜单）
- **GUI 层**：通过 MCP 工具模拟键鼠操作

选路逻辑：API 可用 → API；否则 → GUI。实测效果：o1 模型下减少 58.5% 步骤。

### 3.7 知识/记忆体系

| 来源 | 用途 |
|------|------|
| RAG 检索（应用文档） | 为 AppAgent 注入应用领域知识 |
| Web 搜索 | 实时获取未知软件的操作方法 |
| 执行历史 | Sentence Transformers 相似度检索，复用成功经验 |
| 用户反馈 | 融合进 Blackboard 影响后续规划 |

### 3.8 PiP 虚拟桌面

通过 Windows RDP loopback 创建隔离的虚拟桌面：

- Agent 和用户可**同时操作**而互不干扰
- 安全 IPC：Windows named pipes（会话级凭证 + 加密）
- 对用户体验无影响（代理操作在独立会话里进行）

### 3.9 代码结构（关键路径）

```
microsoft/UFO/
├── ufo/
│   ├── agents/
│   │   ├── host_agent.py          # HostAgent + HostAgentProcessor
│   │   ├── app_agent.py           # AppAgent + AppAgentProcessor
│   │   ├── processors/
│   │   │   ├── app_agent_processor.py   # 4 阶段主循环
│   │   │   ├── host_agent_processor.py # 任务分解+调度
│   │   │   └── ...                     # 其他处理器
│   │   ├── memory/
│   │   │   ├── blackboard.py      # 共享内存黑板
│   │   │   ├── knowledge_base.py  # RAG 知识库
│   │   │   └── episodic_memory.py # 情节记忆
│   │   └── states/                # FSM 状态实现
│   ├── commands/                  # MCP 命令层
│   ├── config/                    # 配置系统
│   └── utils/                     # 工具函数
├── documents/docs/                # 完整文档
└── mcp_servers/                   # 应用专用 MCP 服务器
```

---

## 4. Agent-S2 规划机制解析

> 来源：[simular-ai/Agent-S](https://github.com/simular-ai/Agent-S)，论文 arXiv:2504.00906（April 2025）

### 4.1 架构：Manager-Worker 层次化设计

```
                      ┌──────────────────┐
                      │    用户指令       │
                      └────────┬─────────┘
                               ▼
           ┌──────────────────────────────────┐
           │          Manager (Planner)        │
           │  - 知识检索（本地知识库+网络搜索） │
           │  - 经验检索（相似任务的情节记忆）  │
           │  - LLM 融合生成计划              │
           │  - 计划→DAG→拓扑排序→Node 队列   │
           │  - [PHP] 完成子目标后主动重规划   │
           └──────────────┬───────────────────┘
                          ▼
              ┌───────────────────────┐
              │   Node 队列（有序）    │
              │  Node(name, info)     │
              └───────────┬───────────┘
                          ▼
           ┌──────────────────────────────────┐
           │          Worker (Executor)        │
           │  - 系统提示加载当前子目标         │
           │  - 反思上一个动作的效果           │
           │  - LLM 生成带接地信息的动作计划    │
           │  - MoG 路由到接地专家→坐标→PyAutoGUI│
           │  - 轨迹截断（max_trajectory_length=8）│
           └──────────────────────────────────┘
```

### 4.2 主动层次规划（PHP）—— 核心创新

PHP 与传统被动规划的区别：

| | 被动规划（ReAct） | 主动层次规划（PHP） |
|---|---|---|
| 重规划时机 | 失败后 | **每个子目标完成后** |
| 计划更新方式 | 替换失败步骤 | 基于最新观察**重新评估全部剩余计划** |
| 上下文保持 | 仅当前步骤 | 携带之前子目标的上下文，减少噪声敏感 |
| 适应能力 | 差（新信息不被利用） | 强（动态融入新观察） |

**PHP 的工作流程**：

```
初始指令 →
  Manager 生成初始 DAG → topo_sort → [g₀, g₁, ..., gₙ]
  Worker 执行 g₀ →
  Manager 收到完成信号 + 新观察 →
  Manager 重评估：g₀ 已完成的前提下，[g₁, ..., gₙ] 还需要调整吗？
  可能的结果：
    - 计划不变，继续 g₁
    - g₁ 的描述基于新观察更新（contextual refinement）
    - g₂ 因为 g₀ 的中间结果变得不必要，删除
    - 发现需要新的子目标 g'，插入
```

### 4.3 混合接地（MoG）—— 动作→坐标

Worker 生成的接地动作经 MoG 路由到正确的专家：

| 专家 | 输入 | 输出 | 适用场景 |
|------|------|------|----------|
| **Visual**（UI-TARS-72B-DPO） | 自然语言元素描述 + 截图 | (x, y) 像素坐标 | 图标、按钮、非文字 UI |
| **Textual**（Tesseract OCR） | 文字片段 | word-level bbox坐标 | 选中特定文本、点击文字链接 |
| **Structural**（UNO bridge） | 表格操作语义 | LibreOffice/Excel API 调用 | 电子表格数据操作 |

Worker 作为**门控机制**，根据动作类型路由到对应专家。

### 4.4 知识体系（知识库 + 情节记忆）

```
知识库（Knowledge Base）:
  ├── 应用手册 → RAG 检索（BGE-M3 嵌入 + FAISS）
  ├── 网络搜索 → Perplexica / LLM web search
  └── 任务级经验 → 情节记忆（episodic memory），相似任务检索

知识融合流程：
  query = 用户指令 → 嵌入检索(知识库+记忆) → LLM 融合 → 计划
```

### 4.5 反思系统

Worker 启动一个独立的 `reflection_agent`，每步后分析：

- 上一个动作是否达到了预期？
- 当前轨迹是否有问题？
- 反思内容注入下一步 generator 提示，但**不进入消息历史**（避免历史膨胀）

### 4.6 观测/动作空间（OSWorld 环境下的实现）

动作空间定义为一组 `@agent_action` 装饰的方法，生成 PyAutoGUI 代码：

| 动作 | 接地方式 | 生成的代码 |
|------|---------|-----------|
| `click(element_desc, n, button)` | Visual | `pyautogui.click(x, y, clicks=n)` |
| `type(element_desc, text, overwrite)` | Visual+Text | `pyautogui.click(x, y); pyautogui.write(text)` |
| `scroll(element_desc, clicks, shift)` | Visual | `pyautogui.moveTo(x, y); pyautogui.vscroll(clicks)` |
| `drag_and_drop(start, end)` | Visual×2 | `pyautogui.moveTo(x1, y1); pyautogui.dragTo(x2, y2)` |
| `hotkey(keys)` | 无 | `pyautogui.hotkey('ctrl', 'c')` |
| `highlight_text_span(phrase)` | Text(start+end) | OCR→bbox→drag |
| `open(app_name)` | 无 | `pyautogui.hotkey('win'); pyautogui.write(name)` |
| `switch_applications(app)` | 平台特化 | Linux:wmctrl / macOS:AppleScript |

### 4.7 Agent-S2 性能数据

| Benchmark | Agent-S2 | UI-TARS | Claude 3.7 | 提升 |
|-----------|----------|---------|------------|------|
| OSWorld 15-step | 27.0% | 22.7% | — | +18.9% |
| OSWorld 50-step | 34.5% | — | 26.0% | +32.7% |
| WindowsAgentArena | 29.8% | — | (NAVI 19.5%) | +52.8% |
| AndroidWorld | 54.3% | 46.8% | — | +16.5% |

**关键洞察**：这些数字是在**纯截图驱动、无 UIA 接入**的条件下取得的——这在 Windows 上意味着还有大量提升空间（UIA 可以提供精确控件坐标，不需要接地模型猜测）。

---

## 5. 三项目对比：差距与迁移路径

### 5.1 架构维度对比

| 维度 | Caelum-Agent | UFO2 | Agent-S2 | Pro 目标 |
|------|-------------|------|----------|----------|
| **Agent 模型** | 单一 ReAct agent | HostAgent + N×AppAgent | Manager + Worker | **HostAgent + AppAgent + PHP** |
| **规划方式** | 无显式规划 | HostAgent 任务分解 | PHP 主动层次规划 | **融合：HostAgent 分解 + PHP 主动重评估** |
| **感知** | OCR+YOLO+Florence-2 融合 | UIA+OmniParser 融合 | 纯截图+Visual Grounding LLM | **UIA+感知融合（继承 UIA 精度 + 已有融合管线）** |
| **动作** | MCP 工具（playwright/windows/fs） | GUI(MCP)+API(Win32/WinCOM) | PyAutoGUI（跨平台） | **API 优先→MCP 回退→PyAutoGUI 兜底** |
| **接地** | SoM 标注→label 匹配→坐标解析 | UIA label→坐标 | Visual/Textual/Structural 3 专家 | **UIA 直接接地→SoM 回退→Visual 专家兜底** |
| **内存** | SQLite+ChromaDB+Kimi Formula | Blackboard+RAG+Episodic | KnowledgeBase+Episodic | **Blackboard+RAG+Episodic+Kimi memory 公式** |
| **LLM 后端** | Kimi K3 深度适配 | GPT-4o/o1 通用 | 可插拔（OpenAI/Anthropic/HF TGI） | **Kimi K3 保持深度适配** |
| **虚拟桌面** | 无 | PiP RDP loopback | 无 | **可选集成 PiP** |
| **代码风格** | 自研，从零写起 | 框架化，策略模式+配置驱动 | 研究代码，直白 | **框架化，参考 UFO2 风格** |

### 5.2 可复用资产评估

| 资产 | 可直接迁移 | 需改造 | 废弃 |
|------|-----------|--------|------|
| `agent/llm_client.py`（Kimi 客户端） | ✅ Formula 注册/执行、Files API、三态 tools | 改为 UFO2 的 LLM 调用接口 | — |
| `agent/perception.py`（感知管线） | — | 移植 OCR+YOLO+Florence-2 融合逻辑到 UFO2 CONTROL_COLLECTION 策略 | DPI 归一化（UFO2 有自己的缩放） |
| `ui_detector/`（视觉检测） | ✅ fusion.py/visualizer.py 可整体保留 | yolo_detector/icon_captioner 接口适配 | — |
| `agent/file_reader.py`（ReadDocument） | ✅ Files API file-extract 可直接作为 AppAgent 的一个策略 | — | — |
| `agent/media.py`（ViewMedia） | ✅ 图片/视频压缩+上传 | — | — |
| `agent/pending_learning.py`（中断清算） | — | 移植到 UFO2 的 session 生命周期 | — |
| `agent/skills.py`（技能学习） | — | 与 UFO2 的 episodic_memory 合并 | — |
| `mcp_client/`（MCP 客户端） | ❌ UFO2 有自己的 MCP 基础架构 | 噪声过滤逻辑可移植 | 连接管理、重连逻辑 |
| `agent/memory.py`（SQLite+ChromaDB） | — | ChromaDB 嵌入向量搜索可复用到知识库 | SQLite schema 不兼容 UFO2 |
| `agent/orchestrator.py`（主循环） | ❌ | 被 UFO2 HostAgent+AppAgent 替代 | 熔断器逻辑提取为通用模块 |
| `agent/state_machine.py` | — | 适配 UFO2 的 FSM 系统 | — |
| `tests/` | — | 按新模块重写 | — |
| `agent/choice_menu.py` | ✅ msvcrt 键盘菜单可直接迁移 | — | — |
| `agent/self_window.py` / `focus_guard.py` | ✅ 窗口管理直接迁移 | — | — |
| `agent/history_archive.py` | ✅ JSONL 归档直接迁移 | — | — |

### 5.3 决定性差异：为什么基于 UFO2 而不是继续自研

1. **Windows 原生集成**：UFO2 直接调 Win32/WinCOM/UIA，不走 MCP 代理，理论上更快更可靠
2. **多 agent 架构成熟**：HostAgent+AppAgent 的分工已在 6000+ GitHub star 的项目中验证过，不需要从零设计
3. **策略模式**：Processing Strategy 的模式高度可扩展——感知/LLM/动作/记忆四阶段每阶段都可以换实现，这正是融入我们的差异化能力的最佳位置
4. **社区生态**：UFO3 Galaxy（2025 年 11 月）的跨设备编排已发布路线图，基座本身在持续演进

---

## 6. Caelum-Agent-Pro 融合架构设计

### 6.1 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│                    Caelum-Agent-Pro                           │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐    │
│  │                   HostAgent                           │    │
│  │  ┌─────────────┐  ┌──────────┐  ┌─────────────────┐  │    │
│  │  │Task Decomp   │  │ PHP       │  │App Selection    │  │    │
│  │  │(UFO2原生)    │  │Module     │  │(UFO2原生)       │  │    │
│  │  └──────┬───────┘  │(Agent-S2) │  └────────┬────────┘  │    │
│  │         │           │           │           │           │    │
│  │         ▼           ▼           ▼           ▼           │    │
│  │  ┌──────────────────────────────────────────────────┐   │    │
│  │  │        PHP 注入点：每子目标完成后调 Manager       │   │    │
│  │  │        重评估剩余计划，生成/更新/删除子目标       │   │    │
│  │  └──────────────────────────────────────────────────┘   │    │
│  └──────────────────────────────────────────────────────┘    │
│                          │                                    │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │                Blackboard（共享内存）                 │    │
│  │  questions | requests | trajectories | screenshots    │    │
│  │  + learning_queue（中断轨迹清算队列）                 │    │
│  │  + kimi_memory（云端记忆同步）                        │    │
│  └──────────────────────────────────────────────────────┘    │
│                          │                                    │
│           ┌──────────────┼──────────────┐                     │
│           ▼              ▼              ▼                     │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐             │
│  │ AppAgent   │  │ AppAgent   │  │ AppAgent   │   ...       │
│  │ (Chrome)   │  │ (Excel)    │  │ (WeChat)   │             │
│  │            │  │            │  │            │             │
│  │ 4 阶段管线 │  │ 4 阶段管线 │  │ 4 阶段管线 │             │
│  │ +融合感知  │  │ +API 优先  │  │ +纯视觉    │             │
│  └────────────┘  └────────────┘  └────────────┘             │
│                          │                                    │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │           Level 3: Command Layer (MCP)                │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐           │    │
│  │  │ GUI Cmds │  │API Cmds  │  │Win32/WinCOM│          │    │
│  │  │(Click/   │  │(Excel:   │  │(原生调用)  │          │    │
│  │  │ Type/...)│  │ SaveAs…) │  │           │           │    │
│  │  └──────────┘  └──────────┘  └──────────┘           │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

### 6.2 PHP 融入 HostAgent 的方案

**切入位置**：UFO2 的 `HostAgentProcessor` 在每个子任务完成后触发下一个子任务时。在这个点上插入 PHP 评估。

```python
# 伪代码：HostAgent 的增强执行循环
class HostAgentProcessorWithPHP(HostAgentProcessor):
    async def execute_subtasks(self, subtasks: list[SubTask]):
        remaining = subtasks[:]
        completed = []

        for subtask in remaining:
            # --- 原 UFO2 逻辑 ---
            app_agent = self.get_or_create_app_agent(subtask.app)
            result = await app_agent.execute(subtask)

            # --- PHP 注入点 ---
            if result.is_complete():
                completed.append(subtask)
                remaining.remove(subtask)

                # 主动重评估剩余计划
                remaining = await self._php_refine_plan(
                    original_instruction=self.user_instruction,
                    completed=completed,
                    remaining=remaining,
                    latest_observation=result.observation,
                )

        return completed

    async def _php_refine_plan(self, original_instruction, completed,
                                remaining, latest_observation):
        """基于 Agent-S2 PHP 精神：每完成一个子目标后重评估"""
        prompt = self._build_php_prompt(
            original_instruction,
            [s.description for s in completed],
            [s.description for s in remaining],
            latest_observation,
        )
        # 用 Kimi K3 调用，return 更新的 remaining 列表
        # 可能的输出：保持不变 / 修改描述 / 删除某子目标 / 插入新子目标
        response = await self.llm.chat(prompt, tools=None,
                                        response_format={"type": "json_object"})
        plan = json.loads(response.choices[0].message.content)
        return self._apply_plan_patch(remaining, plan)
```

PHP 的 LLM prompt 模板：

```
You are reviewing a plan for a Windows desktop automation task.

Original instruction: {original_instruction}

Already completed:
{completed_list}

Remaining subgoals:
{remaining_list}

Latest observation: {observation}

Decide: given what was just completed and the current state, should any of the
remaining subgoals be modified, removed, or should new subgoals be added?

Return JSON: {{"changes": [{{"action": "keep"|"modify"|"delete"|"insert",
"index": N, "new_description": "..."(if modify/insert)}}]}}
```

### 6.3 感知融合：用 Caelum-Agent 管线替换 OmniParser 单一方案

UFO2 原生的 `AppControlInfoStrategy` 调用 `UIA + OmniParser → IoU 融合`。Pro 版替换为 Caelum-Agent 的增强感知策略：

```python
# Pro 的增强感知策略
class EnhancedControlDetectionStrategy(BaseStrategy):
    """融合 Caelum-Agent 的 OCR + YOLO + Florence-2 感知管线"""
    def execute(self, context):
        # 1. Windows UIA 树采集（原生，不是 MCP）
        uia_controls = self._capture_uia_tree()

        # 2. 截图 + 压缩（继承 Caelum-Agent 的 mss + Pillow 管线）
        screenshot = self._capture_and_compress()

        # 3. OCR（RapidOCR + DirectML GPU，继承 Caelum-Agent）
        ocr_boxes = self._run_ocr(screenshot)

        # 4. YOLO 图标检测（继承 Caelum-Agent，~50ms GPU）
        icon_boxes = self._yolo_detect(screenshot)

        # 5. IoU 三路融合（继承 fusion.py，OCR + YOLO + UIA）
        markers = fuse_uia_ocr_yolo(uia_controls, ocr_boxes, icon_boxes,
                                     iou_merge=0.15, iou_dedup=0.05)

        # 6. Florence-2 裸图标描述（继承 Caelum-Agent）
        bare_icons = [m for m in markers if m["text"] == "icon"]
        captioned = self._caption_icons(screenshot, bare_icons)

        # 7. SoM 可视化（继承 visualizer.py）
        return ControlInfo(
            markers=markers,    # 每个 marker 有 label + text + bbox + icon 标志
            annotated_path=self._visualize_som(screenshot, markers),
            uia_tree=self._build_uia_tree(uia_controls),
        )
```

**相比 UFO2 原生的改进**：

| 方面 | UFO2 原生 | Pro 增强 |
|------|-----------|----------|
| 检测源 | UIA + OmniParser YOLO | UIA + OCR + YOLO + Florence-2 |
| 图标语义 | 无（YOLO 只检测"有图标"） | Florence-2 生成 "放大镜" / "红色关闭按钮" |
| OCR 引擎 | — | RapidOCR + DirectML GPU（5.5x 加速） |
| 标注质量 | 控件名或空 | 融合结果带 OCR 文本+图标描述+控件类型 |

### 6.4 接地降级链：三层接地方案

UFO2 原生用 UIA label 接地，Agent-S2 用 Visual Grounding LLM。Pro 结合两者，形成三层降级：

```
Level 1: UIA 直接接地（精度最高、零 LLM 开销）
  └─ UIA element → bounding rect → 坐标 = (left+w/2, top+h/2)
     失败原因：非标准控件、空 name、Qt/Electron 应用 → 降级到 Level 2

Level 2: SoM 标注接地（继承 Caelum-Agent）
  └─ 模型根据标注描述选择 label=N → DesktopInteract 解析坐标
     失败原因：标注遗漏、模型选错 → 降级到 Level 3

Level 3: Visual Grounding Expert（Agent-S2 MoG 的 Visual 专家）
  └─ 自然语言描述 → Visual Grounding LLM → (x, y)
     成本最高（一次 LLM 调用），但兜底能力最强
```

### 6.5 动作编排：三优先级 Puppeteer

```python
class ProPuppeteer(Puppeteer):
    async def execute_action(self, action: Action, context: Context):
        # Priority 1: Win32/WinCOM API（零 GUI 操作）
        if api_handler := self._find_api_handler(action, context.app):
            return await api_handler.execute(action)

        # Priority 2: MCP GUI 命令（保留现有 MCP 基础设施）
        if mcp_tool := self._find_mcp_tool(action):
            return await self.mcp.call(mcp_tool.server, mcp_tool.name, action.args)

        # Priority 3: PyAutoGUI（Agent-S2 的兜底方案）
        return await self._execute_pyautogui(action)
```

### 6.6 知识体系：四源融合

```
┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐
│ 应用文档 RAG │  │ 网络搜索     │  │ 情节记忆     │  │ Kimi Memory  │
│ (UFO2原生)   │  │ (Kimi        │  │ (UFO2原生+   │  │ (Caelum-     │
│ BGE/Sentence  │  │  web-search  │  │  Agent-S2)   │  │ Agent 迁移)  │
│ Transformers │  │  Formula)    │  │ Sentence TF   │  │ 云端偏好同步  │
└──────┬───────┘  └──────┬──────┘  └──────┬───────┘  └──────┬───────┘
       │                │                │                  │
       └────────────────┴───────┬────────┴──────────────────┘
                                ▼
                    ┌─────────────────────┐
                    │  Knowledge Fusion   │
                    │  (LLM 融合多源知识)  │
                    └──────────┬──────────┘
                               ▼
                    注入 AppAgent system prompt
```

### 6.7 自学习体系：学习链路迁移

Caelum-Agent 的学习链路整体迁移到 UFO2 框架中：

| 原 Caelum-Agent 组件 | Pro 中的位置 | 实现方式 |
|----------------------|-------------|----------|
| `SkillLearner` | AppAgent Phase 4 (Memory Update) 触发的后处理 | 成功轨迹→SkillGenerator 子代理→`skills/learned/*.md` |
| `LearningSettler` | HostAgent session 初始化时运行 | 同原逻辑，查 Blackboard 的 `learning_queue` |
| `pending_learning` 表 | Blackboard `learning_queue` 字段 | 中断时写入，启动时清算 |
| `reflection.record()` | AppAgent Phase 4 的 fail 分支 | 失败轨迹写 Episodic Memory（负样本）+ 更新 Skills |

---

## 7. Kimi K3 适配方案

### 7.1 从 OpenAI 到 Kimi 的接口适配层

UFO2 源码使用 `openai` SDK 调 GPT-4o/o1。需要最小侵入的适配层：

```python
# agent/adapters/kimi_adapter.py
class KimiLLMAdapter:
    """把 Kimi LLMClient 的语义适配为 UFO2 期望的 LLM 接口"""

    def __init__(self, config: LLMConfig):
        self._client = LLMClient(config)  # 复用 Caelum-Agent 的完整客户端

    async def initialize(self):
        await self._client.initialize()   # 启动 Formula 注册

    async def chat(self, messages, tools=None, response_format=None, **kwargs):
        # tools 三态：None 不带 tools，省略带全部，列表传列表
        # Kimi 拒连续同角色——这里只做薄封装，调用方需要自己保证 message 序列合规
        return await self._client.chat(messages, tools, response_format)

    async def execute_tool_calls(self, tool_calls):
        return await self._client.execute_tool_calls(tool_calls)

    def register_tool(self, name, handler, schema, description):
        self._client.register_local_function(name, handler, schema, description)

    # 暴露 Kimi 特有能力给 UFO2 策略层
    @property
    def kimi_http(self):
        return self._client.http  # Formula + Files API 共用

    @property
    def tool_names(self):
        return self._client.tool_names()
```

### 7.2 UFO2 各阶段如何适配 Kimi

| UFO2 组件 | Kimi 注意事项 |
|-----------|--------------|
| **Task Decomposition** | `response_format={"type":"json_object"}` + system prompt 约束子任务格式 |
| **AppAgent LLM Phase** | 复用 Caelum-Agent 的消息合并策略：多个跟进信息（截图+感知+SoM）合并为一条 user 消息 |
| **PHP Refinement** | `tools=None` 防止误触发工具，`response_format={"type":"json_object"}` 确保结构化输出 |
| **Web Search** | 用 Kimi `web_search` Formula 替代 UFO2 的搜索方案，零额外配置 |
| **Document Reading** | 用 ReadDocument（`agent/file_reader.py`）的 file-extract 替代 UFO2 的基础文件读取 |
| **Memory** | `kimi_memory.set_memory/get_memory` 提供云端持久记忆，与 Blackboard 事件记忆互补 |
| **Reflection** | `kimi_memory.rethink(thought, action="organize")` 整理反思建议 |

### 7.3 Partial Mode 的适用场景

```
AppAgent 内部适用 Partial Mode 的场景：
1. AppAgent 的 action JSON 固定前缀 {"action": → 强制模型只填参数
2. PHP 评估的输出 {"changes": → 减少幻觉
3. SkillLearner 的 SKILL.md 生成 → # Skill: {name}\n\n## Description\n\n → 续写
```

### 7.4 熔断器在 UFO2 框架中的位置

熔断器不适合放在单个 AppAgent 层面（一个应用挂了不应拖垮全局）。正确位置：

- **Per-AppAgent**：每个 AppAgent 维护独立 `consecutive_api_failures`。
- **Per-HostAgent**：当所有活跃 AppAgent 都熔断 → HostAgent 请求人工介入。
- PHP 重规划可主动规避已熔断的应用（"Chrome 不可用，能否用 Edge？").

---

## 8. 实现路线图

### Phase 1：基座搭建（预计 2-3 周）

- [ ] 克隆 `microsoft/UFO` 为 `Caelum-Agent-Pro` 的初始代码基座
- [ ] 安装 Kimi 适配层（`agent/adapters/kimi_adapter.py`），替换 UFO2 的默认 LLM 调用
- [ ] 验证：用 `kimi-k3` 跑通 UFO2 自带的 demo（Notepad 打字、文件管理器操作）
- [ ] 迁移 Caelum-Agent 的 `config.yaml.example` 到 Pro 的配置系统
- [ ] 写第一个集成测试：`test_kimi_host_agent_decomposes_task`

### Phase 2：感知融合（预计 2-3 周）

- [ ] 移植 `ui_detector/` 全部模块（`yolo_detector.py`, `icon_captioner.py`, `fusion.py`, `visualizer.py`）
- [ ] 移植 `agent/perception.py` 的 OCR + DPI 归一化逻辑
- [ ] 实现 `EnhancedControlDetectionStrategy`（§6.3）替换 UFO2 原生的 `AppControlInfoStrategy`
- [ ] 验证：对比 Ursa 原生感知 vs Pro 增强感知的标注数量和质量
- [ ] 移植 `agent/file_reader.py` 和 `agent/media.py` 为 AppAgent 的 FileStrategy / MediaStrategy

### Phase 3：PHP 规划融合（预计 1-2 周）

- [ ] 在 `HostAgentProcessor` 中注入 PHP 重评估回调
- [ ] 实现 PHP prompt 模板 + JSON 解析
- [ ] 测试：多步任务中模拟中间结果变化，验证计划动态更新
- [ ] 实现计划 DAG 的有向无环图约束校验

### Phase 4：三层接地 + 动作编排（预计 1-2 周）

- [ ] 实现三层接地降级链（§6.4）：UIA→SoM→Visual Grounding
- [ ] 实现 ProPuppeteer 三优先级动作编排（§6.5）
- [ ] 对接 Agent-S2 的 Visual Grounding 专家（UI-TARS 或 Kimi 多模态能力）
- [ ] 测试：定位器在非标准 Qt/Electron 应用上的表现

### Phase 5：自学习 + 知识体系（预计 1-2 周）

- [ ] 迁移 `agent/skills.py` → Pro 的 SkillGenerator
- [ ] 迁移 `agent/pending_learning.py` → Blackboard learning_queue + HostAgent 初始化清算
- [ ] 迁移 `agent/reflection.py` → Episodic Memory 的失败样本记录
- [ ] 验证：完整任务的自动学习→下次同类型任务成功复用的闭环

### Phase 6：窗口管理 + 测试覆盖（预计 1 周）

- [ ] 迁移 `agent/self_window.py` 和 `agent/focus_guard.py`
- [ ] 迁移 `agent/history_archive.py` 和 `agent/choice_menu.py`
- [ ] 写 Pro 的系统测试（跨应用任务集成测试）
- [ ] 验证 681 个 Caelum-Agent 测试中可复用的子集在新框架下通过

---

## 9. 关键风险与注意事项

### 9.1 技术风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| UFO2 的 UIA 直接调用与 windows-mcp 冲突 | 中 | 高 | 先用 MCP 通道验证，再逐步替换为原生 UIA |
| Kimi K3 在 UFO2 的 JSON 结构化输出上不如 GPT-4o | 中 | 中 | 加容错解析（`_parse_verdict` 风格）；关键路径用 `response_format` 强制 JSON |
| UFO2 代码量巨大（30000+ 行），改动面广 | 高 | 中 | 严格遵循策略模式，只在 Strategy 层写新代码，不动核心框架 |
| PyAutoGUI 在 Windows 上的行为与 Linux(Agent-S2 测试环境) 不同 | 中 | 中 | 用 MCP 命令替代 PyAutoGUI 默认实现，PyAutoGUI 仅为兜底 |
| 多个 LLM 调用并行时的 API key 限流 | 低 | 中 | 按 AppAgent 隔离计数器 + 熔断器，全局信号量控制并发数 |

### 9.2 架构风险

| 风险 | 缓解措施 |
|------|---------|
| PHP 每子目标后都调 LLM，大幅增加 token 消耗 | PHP 评估用轻量 prompt（~500 tokens） + 可配置开关（跳过简单任务） |
| Manager-Worker + HostAgent-AppAgent 两套层级概念叠加混乱 | 明确分工：HostAgent=Manager 角色（管计划+分配），AppAgent=Worker 组（管执行） |
| Blackboard 成为单点瓶颈（所有 agent 共享） | 按应用隔离 Blackboard namespace；无共享需求时不写 |
| UFO2 的上游更新（UFO3 Galaxy）与 Pro 的改动冲突 | 所有 Pro 新增代码放在独立命名空间 `pro/`，不改动 UFO2 核心文件；定期 rebase |

### 9.3 从 Caelum-Agent 继承的教训

1. **不要把所有逻辑塞一个文件**：Caelum-Agent 的 `orchestrator.py` 1800+ 行是反面教材。Pro 必须保持每个 Strategy/Module < 500 行。
2. **熔断器要分层**：Caelum-Agent 的熔断器是全或无的（一个 LLM 调用失败拖垮整个任务）。Pro 改为 per-AppAgent 熔断 + PHP 重规划绕开。
3. **重试必须有间隔**：Caelum-Agent 外层 LLM 重试无间隔的问题已在 §2.3 记录，Pro 须在重试逻辑中加入指数退避。
4. **document 先行**：Caelum-Agent 后来补的 Kimi API 文档证明了"先写文档再实现"的价值——Pro 从第一天就维护 `docs/pro/`。
5. **TDD 不能丢**：Caelum-Agent 的 681 个测试是其最可靠的资产。Pro 的每个新模块必须有对应测试。

---

## 10. 附录：参考资源与映射表

### 10.1 参考资源

| 资源 | 链接 |
|------|------|
| **Caelum-Agent 代码库** | `E:\code\project\Caelum-Agent` |
| **Caelum-Agent Kimi API 手册** | `docs/kimi_api/kimi_api_usage.md` |
| **Caelum-Agent 核心设计文档** | `docs/designs/desktop_agent_v8.agent.final.md` |
| **UFO2 GitHub** | https://github.com/microsoft/UFO |
| **UFO2 论文** | arXiv:2504.14603 |
| **UFO2 文档** | https://microsoft.github.io/UFO/ |
| **Agent-S2 GitHub** | https://github.com/simular-ai/Agent-S |
| **Agent-S2 论文** | arXiv:2504.00906 |
| **Agent-S2 技术博客** | https://www.simular.ai/articles/agent-s2 |
| **Kimi 开放平台** | https://platform.moonshot.cn |
| **OmniParser 权重镜像** | https://github.com/LiaoZiqi-GZFLS/omniparser-weights |
| **windows-mcp 上游 issue 草稿** | `docs/windows_mcp/upstream-tree-node-issue.md` |

### 10.2 组件映射速查表

| Caelum-Agent 组件 | UFO2 对应 | Agent-S2 对应 | Pro 方案 |
|-------------------|-----------|---------------|----------|
| `orchestrator.py` ReAct 循环 | `AppAgentProcessor` 4 阶段管线 | Worker 执行循环 | 保持 UFO2 模式，增强 Phase 2（LLM）和 Phase 3（Action） |
| `perception.py` | `AppControlInfoStrategy` | 无（纯截图+VLM） | EnhancedControlDetectionStrategy（UIA+OCR+YOLO+Florence-2） |
| `state_machine.py` | `states/` FSM | Worker 内部状态 | UFO2 7 状态 FSM |
| `llm_client.py` | UFO2 的 LLM 调用封装 | 可插拔 LLM 引擎 | KimiLLMAdapter（封装 LLMClient） |
| `tools.py` | `commands/` MCP 层 | PyAutoGUI 接地 | ProPuppeteer（API→MCP→PyAutoGUI） |
| `memory.py` + `kimi_memory.py` | Blackboard + knowledge_base | KnowledgeBase + Episodic | Blackboard + RAG + Kimi 云端 |
| `reflection.py` | AppAgent Phase 4 | reflection_agent | Episodic Memory 写失败样本 + Kimi rethink |
| `skills.py` | episodic_memory | KnowledgeBase（narrative） | SkillGenerator（LLM 从轨迹生成 SKILL.md） |
| `pending_learning.py` | —（无对应） | —（无对应） | Blackboard learning_queue + HostAgent 启动清算 |
| `file_reader.py` | —（基础文件读取） | — | ReadDocument FileStrategy（Files API file-extract） |
| `media.py` | — | — | ViewMedia MediaStrategy（上传+ms://） |
| `content_writer.py` | — | — | DraftContent WritingStrategy（Partial Mode 子代理） |
| `image_gen.py` | — | — | GenerateImage Strategy（SVG→PNG→自评） |
| `cli_presenter.py` | — | — | 直接迁移（Rich terminal 事件展示） |
| `kill_switch.py` | — | — | 直接迁移（pynput 全局监听） |
| `self_window.py` / `focus_guard.py` | — | — | 直接迁移（窗口显隐+聚焦） |
| `history_archive.py` | — | — | 直接迁移（JSONL 脱敏归档） |
| `mcp_client/__init__.py` | UFO2 自有 MCP 基础 | — | 复用 UFO2 MCP，移植噪声过滤 |
| `choice_menu.py` | — | — | 直接迁移（msvcrt 键盘选择） |
| `ui_detector/` | OmniParser 视觉检测 | Visual/Text Grounding | 整体保留（YOLO+Florence-2+融合+可视化） |

### 10.3 关键命名约定（Pro 新增代码）

```
Caelum-Agent-Pro/
├── pro/                              # ★ Pro 差异化代码隔离在此目录
│   ├── adapters/
│   │   └── kimi_adapter.py           # Kimi LLM 适配层
│   ├── strategies/
│   │   ├── perception/
│   │   │   └── enhanced_detection.py # 增强感知策略（UIA+OCR+YOLO+Florence-2）
│   │   ├── grounding/
│   │   │   └── three_tier.py         # 三层接地降级链
│   │   ├── action/
│   │   │   └── pro_puppeteer.py      # 三优先级动作编排
│   │   ├── planning/
│   │   │   └── php_refinement.py     # PHP 主动重评估
│   │   ├── learning/
│   │   │   ├── skill_generator.py    # SkillLearner 迁移
│   │   │   └── settlement.py         # LearningSettler 迁移
│   │   └── tools/
│   │       ├── file_reader.py        # ReadDocument
│   │       ├── media.py              # ViewMedia
│   │       ├── content_writer.py     # DraftContent
│   │       └── image_gen.py          # GenerateImage
│   ├── utils/
│   │   ├── self_window.py            # 自有窗口管理
│   │   ├── focus_guard.py            # 前台聚焦看门狗
│   │   ├── choice_menu.py            # 键盘选择菜单
│   │   └── history_archive.py        # 飞行记录归档
│   └── config/
│       └── kimi_config.py            # Kimi 专属配置扩展
├── ufo/                              # UFO2 主框架（尽量不动）
├── tests/
│   └── pro/                          # Pro 新增测试
└── docs/
    └── pro/                          # Pro 设计文档
```

---

*本文档随 Caelum-Agent-Pro 开发迭代更新。与代码冲突时以代码为准。*
