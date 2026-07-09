# Windows桌面操作助手 — 技术方案 v5

> **版本**: v5.0 | **性质**: 个人CLI项目 | **平台**: Windows 10/11
> **大脑**: Kimi K2.6 (Moonshot AI) | **本地模型管理**: Ollama
> **参考架构**: Cradle六模块循环 (BAAI, arXiv:2403.03186) + UFO² AgentOS (Microsoft, NAACL'25)

---

## 1. 概述与核心架构

### 1.1 定位与能力边界

一个Windows命令行桌面操作Agent。用户通过自然语言下达指令，Agent自主操控浏览器和Windows桌面应用完成任务。核心能力：**感知屏幕 → 规划操作 → 执行控制 → 学习积累**。

**双域覆盖**：
- **浏览器域**：Playwright MCP accessibility tree方案，token消耗比纯视觉方案减少82.5%（Anthropic Computer Use评估报告，2024年10月）
- **桌面域**：Windows-MCP UIA控件树方案，确定性控件定位 + 视觉Fallback

**不做的事**：不设计GUI界面（CLI够用）、不做CI/CD流水线、不做代码签名、不做自动更新、不做企业级部署。

### 1.2 核心循环：增强型五级ReAct

参考Cradle六模块循环（Information Gathering → Self-Reflection → Task Inference → Skill Curation → Action Planning → Memory）和UFO² ReAct循环，精简为五级：

```
┌──────────────────────────────────────────────────────┐
│                 增强型五级ReAct循环                     │
├──────────────────────────────────────────────────────┤
│                                                      │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐     │
│   │ Perceive │───▶│  Think   │───▶│   Act    │     │
│   │  (感知)   │    │  (推理)  │    │  (执行)  │     │
│   └──────────┘    └────┬─────┘    └──────────┘     │
│         ▲              │              │             │
│         │         ┌────┴────┐         │             │
│         │         │ Reflect │         │             │
│         │         │ (反思)   │         │             │
│         │         └────┬────┘         │             │
│         │              │              │             │
│    ┌────┴───────────────┴──────────────┘             │
│    │                  Verify                        │
│    │                 (验证)                          │
│    └─────────────────────────────────────────────────┘
│
│   每步最多2次LLM调用：Reflect（可选）+ Think          │
│   相比Cradle的5次/步，token成本降低60%               │
└──────────────────────────────────────────────────────┘
```

| 阶段 | 功能 | 输入 | 输出 | LLM调用 |
|:---|:---|:---|:---|:---:|
| **Perceive** 感知 | 多源感知融合：截图+OCR+UIA/A11y树+SoM标注 | 屏幕状态 | 结构化环境描述 | 0（本地模型） |
| **Reflect** 反思 | 评估上一步是否成功，分析失败原因，检索历史经验 | 上一步动作+结果 | 反思结论+改进建议 | 1（可选触发） |
| **Think** 推理 | 任务规划+工具选择+参数生成 | 感知结果+反思 | 工具调用计划 | 1（必须） |
| **Act** 执行 | 通过MCP调用具体工具 | 工具计划 | 执行结果 | 0 |
| **Verify** 验证 | 检查操作效果，更新记忆，检测死循环 | 执行结果 | 成功/失败/需重试 | 0 |

Reflect为**可选触发**——仅在操作失败、遇到未知UI状态、连续多步在同一UI间切换（死循环检测）时调用。正常成功路径跳过Reflect，比Cradle原始设计减少60% token成本。

### 1.3 技术栈全景

| 层级 | 组件 | 选型 | 来源 |
|:---|:---|:---|:---|
| **大脑** | 核心推理 | Kimi K2.6 API | Moonshot AI |
| **本地模型** | UI解析+路由 | Ollama (Qwen2.5-VL-7B + Qwen2.5-3B) | ollama.com |
| **浏览器控制** | MCP工具 | Playwright MCP (Anthropic官方) | 31.2K stars |
| **桌面控制** | MCP工具 | Windows-MCP (wonderwhy-er) | 5,456 stars |
| **文件操作** | MCP工具 | filesystem-mcp Python版 (PyPI) | Python实现 |
| **视觉感知** | UI检测 | Qwen2.5-VL-7B via Ollama | 阿里通义 |
| **OCR** | 文字识别 | RapidOCR (ONNXRuntime) | CPU 30ms |
| **SoM标注** | 元素编号 | 自研（基于Qwen2.5-VL-7B输出） | OmniParser v2参考 |
| **记忆** | 语义记忆 | Mem0 + SQLite本地 | mem0.ai |
| **技能** | 技能库 | SKILL.md (OpenClaw生态) | 33,000+社区skills |
| **反思** | 自我改进 | Reflexion | NeurIPS 2023 |
| **搜索** | 联网搜索 | Kimi API内置 | ¥0.025/次 |
| **内部通信** | 事件总线 | 自研EventBus (asyncio) | 优先级队列+Pub/Sub |
| **状态管理** | 状态机 | 自研FSM (11状态) | UFO²参考 |
| **并发** | 异步执行 | asyncio主循环+3线程池 | Cradle参考 |
| **插件** | 扩展系统 | Plugin ABC + PluginManager | 动态加载+热重载 |
| **数据存储** | 持久化 | SQLite + ChromaDB + 文件系统 | 三元组合 |

---

## 2. 感知层设计

### 2.1 多源感知融合

| 数据源 | 来源工具 | 用途 | 优先级 |
|:---|:---|:---|:---:|
| **UIA控件树** | Windows-MCP (`get_ui_tree`) | 桌面应用的确定性控件定位 | 最高 |
| **A11y树** | Playwright MCP (`browser_get_accessibility_tree`) | 浏览器页面的确定性元素定位 | 最高 |
| **视觉感知** | Ollama + Qwen2.5-VL-7B | UI元素检测、SoM标注、A11y失败时Fallback | 中 |
| **OCR** | RapidOCR (`rapidocr-onnxruntime`) | 截图文字识别，CPU 30ms | 辅助 |

### 2.2 Set-of-Mark (SoM) 标注

参考OmniParser v2（Microsoft, arXiv:2406.12717）：在截图上用色块+编号标注检测到的UI元素，将连续坐标空间离散化为元素ID。Qwen2.5-VL-7B检测截图中的元素，返回`[(x1,y1,x2,y2, label)]`列表，Agent在截图上绘制编号色块后传给Kimi。这种方式让LLM通过元素编号而非原始坐标指定操作目标，消除分辨率依赖。

### 2.3 感知融合流程

```
屏幕截图 ──▶ RapidOCR文字识别（30ms, CPU）
    │
    ├──▶ UIA/A11y控件树（确定性路径） ──┐
    │                                     ├──▶ 结构化环境描述 ──▶ Kimi
    └──▶ Qwen2.5-VL-7B元素检测 ──▶ SoM标注 ──┘         (感知结果)
              (本地, ~500ms)
```

---

## 3. 推理层设计

### 3.1 增强型ReAct循环

Perceive → Reflect（可选）→ Think → Act → Verify。Reflect在以下条件下触发：
- 上一步操作返回错误/异常
- 连续3步在同一UI状态间切换（死循环检测）
- 遇到完全未知的UI状态（无匹配控件树路径）

### 3.2 错误恢复机制

| 层级 | 触发条件 | 恢复策略 |
|:---|:---|:---|
| **工具级** | MCP工具调用失败 | 指数退避重试3次（1s→2s→4s） |
| **动作级** | 操作后验证失败 | Reflect分析原因→重试或切换工具 |
| **任务级** | 连续3次动作失败 | 暂停任务，请求用户指导 |
| **会话级** | Kimi API不可用 | 切换本地Qwen2.5-3B降级运行 |

**断路器模式**：连续5次API失败触发断路器，60s冷却期后自动恢复。冷却期内所有推理走本地模型。

### 3.3 里程碑检查

每N步检查一次任务进度，评估状态：ON_TRACK（正常）/ STUCK（卡住）/ COMPLETED（完成）/ DEVIATING（偏离）。STUCK状态自动触发Reflect重新规划。

---

## 4. 执行层设计

### 4.1 MCP工具集

**Playwright MCP**（浏览器操作，Anthropic官方，31.2K stars）：
`browser_navigate` / `browser_click` / `browser_type` / `browser_select` / `browser_press_key` / `browser_get_accessibility_tree` / `browser_screenshot` / `browser_evaluate`

**Windows-MCP**（桌面操作，5,456 stars）：
`get_ui_tree` / `click_element` / `type_text` / `send_keys` / `get_window_list` / `focus_window` / `take_screenshot`

**filesystem-mcp Python版**（文件操作，PyPI `filesystem-mcp`，纯Python）：
`read_file` / `read_multiple_files` / `list_directory` / `directory_tree` / `search_files` / `write_file` / `edit_file`（支持dryRun预览） / `create_directory` / `get_file_info`

不使用官方`@modelcontextprotocol/server-filesystem`（Node版），用Python版`filesystem-mcp`保持技术栈统一。

### 4.2 执行优先级策略

确定性控制（MCP） → 半确定性（SoM坐标+pyautogui） → 坐标Fallback（纯坐标模拟） → 人工介入。控件树覆盖率作为Agent健康度指标。

### 4.3 Builtin工具（Agent内置）

| 工具 | 实现 | 用途 |
|:---|:---|:---|
| `screenshot` | `mss`库 | 全屏/区域截图（~10ms） |
| `ocr` | `rapidocr-onnxruntime` | 截图文字识别（~30ms, CPU） |
| `ui_parse` | Ollama + qwen2.5vl | 截图UI元素检测 |
| `search` | Kimi内置tool_call | 联网搜索（¥0.025/次） |

---

## 5. 学习层设计 — 核心差异化

### 5.1 技能学习与生成

三类技能学习方案对比：

| 维度 | AutoSkill | Cradle | openclaw-rpa |
|:---|:---|:---|:---|
| 触发机制 | 用户反馈/重复检测 | UI探索/悬停发现 | 录屏指令（#RPA） |
| 技能格式 | SKILL.md（生态兼容） | Python函数 | 独立Python脚本 |
| 版本管理 | 语义版本（v0.1.0→v0.2.0） | 无 | 无 |
| LLM依赖度 | 中（仅提取阶段） | 高（每步合成） | 低（仅合成阶段） |
| 回放成本 | 零（SKILL.md为提示词） | 中（需LLM解析） | 零（直接执行脚本） |
| 生态兼容性 | OpenClaw 33,000+ skills | 需转换 | 需转换 |
| 推荐度 | ★★★★★ | ★★★ | ★★★★ |

本方案采用**分层技能学习架构**：以AutoSkill + SKILL.md作为技能库主干，以openclaw-rpa录制脚本作为高频任务加速层，以Cradle探索模式作为新应用UI发现时的补充机制。

技能学习的数据流：执行层判定任务成功后，学习管道接收操作轨迹（Action Trace），AutoSkill的`extract_skill`从轨迹中提取通用模式。通过向量相似度（threshold=0.85）检查是否已有相似技能；存在则merge升级版本号，不存在则创建v0.1.0入库。技能文件存于`skills/learned/`目录，embedding通过ChromaDB索引支持语义检索。

### 5.2 记忆系统

四层记忆协同工作：

| 记忆类型 | 实现 | 内容 | 持久化 |
|:---|:---|:---|:---|
| **语义记忆** | Mem0 | 用户偏好、环境知识 | SQLite本地 |
| **程序性记忆** | SKILL.md + ChromaDB | 可复用技能库 | 文件系统+向量索引 |
| **情景记忆** | 操作轨迹JSON | 具体操作序列与结果 | 文件系统 |
| **反思记忆** | Reflexion + SQLite FTS5 | 失败教训与改进建议 | SQLite |

Mem0在LoCoMo基准上达到67.13%准确率，p95延迟0.200秒，是当前生产级Agent记忆系统中延迟-准确率权衡最优选项。相比LangMem（58.10%准确率，59.82秒延迟），Mem0更适合交互式桌面场景。

### 5.3 用户偏好学习

| 信号类型 | 权重 | 示例 | 学习机制 |
|:---|:---:|:---|:---|
| 显式纠正 | 0.90 | "不要用这个浏览器，换Chrome" | 立即写入Mem0 |
| 重复请求模式 | 0.80 | 连续5次选择相同的导出格式 | 模式检测+记忆存储 |
| 正面反馈 | 0.70 | "很好，记住这个方式" | 强化现有偏好权重 |
| 隐式行为模式 | 0.60 | 经常在上午9点打开邮件客户端 | 时序分析+关联存储 |

### 5.4 反思 — Reflexion

来源：Shinn et al., NeurIPS 2023（15-25%成功率提升）。每次操作失败后，Agent生成结构化反思文本（根因分析+改进建议+适用场景标签）。反思数据持久化于SQLite，FTS5全文搜索支持跨会话检索。例如"删除任何文件前必须先展示文件列表让用户确认"的反思将在后续涉及文件删除的任务中自动召回。

---

## 6. 大小模型协作架构

### 6.1 Kimi K2.6 API

| 项目 | 内容 |
|:---|:---|
| 接口标准 | OpenAI兼容 |
| Base URL | `https://api.moonshot.cn/v1` |
| 模型 | `kimi-k2-6`（多模态） |
| 上下文 | 256K tokens |
| 工具调用 | `tool_calls` / `tools` 参数 |
| 多模态 | 图文混合输入，1024 tokens/图 |
| 联网搜索 | 内置，通过tool_call注册`search` tool |

| 定价 | 价格 |
|:---|:---|
| 输入（缓存命中） | ¥6.5 / 1M tokens |
| 输入（缓存未命中） | ¥26 / 1M tokens |
| 输出 | ¥27 / 1M tokens |
| 联网搜索 | ¥0.025 / 次 |

Context Caching：长上下文（截图base64 + 控件树）可缓存复用，缓存命中时输入价格降75%。

### 6.2 Ollama本地模型

| 模型 | 用途 | 命令 | 显存 | 常驻/按需 |
|:---|:---|:---|:---:|:---:|
| Qwen2.5-VL-7B | UI元素检测、截图理解 | `ollama pull qwen2.5vl:7b` | ~6GB | 常驻 |
| Qwen2.5-3B | 任务分类、意图路由 | `ollama pull qwen2.5:3b` | ~2.5GB | 按需 |

Ollama提供OpenAI兼容API（`http://localhost:11434/v1`），切换远程/本地只需改`base_url`。

### 6.3 模型路由策略

| 任务类型 | 路由目标 | 说明 |
|:---|:---|:---|
| 复杂决策、多步规划、技能提取 | Kimi K2.6 | API调用，~1-2s延迟 |
| UI解析、元素检测 | Ollama Qwen2.5-VL-7B | 本地，~500ms |
| 任务分类、简单问答 | Ollama Qwen2.5-3B | 本地，~200ms |
| OCR文字识别 | RapidOCR CPU | 本地，~30ms |
| API不可用时 | Ollama Qwen2.5-3B | 降级运行 |

Fallback层级：Kimi正常 → API延迟>10s切换本地 → API不可用完全本地 → GPU不可用CPU推理。

---

## 7. 安全与权限控制

### 7.1 操作分级确认机制

| 操作级别 | 执行策略 | 审计要求 | 典型示例 | 风险后果 |
|:---|:---|:---|:---|:---|
| **Read** | 自动执行 | 记录审计日志 | 文件读取、数据库查询、状态检查 | 信息泄露 |
| **Write-safe** | 自动执行 | 完整审计记录 | 日志写入、缓存更新、临时文件 | 存储膨胀 |
| **Write-risky** | 需用户确认 | 确认前记录意图、确认后记录执行 | 文件修改、配置变更、数据更新 | 数据损坏 |
| **Destructive** | 强制人工审批 | 双重确认+完整操作链记录 | 数据删除、权限变更 | 不可逆数据丢失 |

78%的生产MCP集成实际上只需要读访问（Anthropic数据），默认只读策略不会显著限制功能覆盖，但能大幅降低安全风险。Meta"二法则"：若Agent同时满足"访问敏感数据"、"暴露于不可信内容"和"能够与外部通信"三个条件，则不应完全自主运行。

filesystem-mcp通过启动时传入的目录白名单控制文件访问范围，超出范围的操作被拒绝。

### 7.2 Kill Switch

 Kill Switch架构包含两个层级：Layer 1基于短期会话令牌实现Agent级精确关闭；Layer 2断路器，连续分析Agent行为并在检测到过多API请求、异常token消耗、循环中的重复操作时触发。提供throttle→pause→full stop三级升级路径。

### 7.3 已知安全事件

| 事件名称 | 时间 | 攻击向量 | 影响 | 根本原因 |
|:---|:---|:---|:---|:---|
| Pocket OS数据库删除 | 2026年4月 | Agent自主执行DROP权限 | 整个公司数据库被删除 | 无人类确认+过度授权 |
| ZombAIs攻击 | 2025年 | 恶意网页隐藏提示注入 | 主机沦为僵尸机 | 未使用沙箱隔离 |
| 金融机构AI欺诈 | 2024年 | 邮件嵌入隐藏指令 | $230万欺诈性电汇 | 文件访问权限缺乏沙箱保护 |
| OpenClaw沙箱逃逸 | 2026年4月 | 时序缺陷权限提升 | CVSS 9.6 | CVE-2026-44112 |

---

## 8. 网络搜索与外部工具

### 8.1 联网搜索

Kimi API已内置联网搜索能力。注册`search` tool后，Kimi在需要时自动调用，结果包含标题+URL+摘要。无需额外接入Tavily/Brave等第三方搜索API。

当Kimi判断需要实时信息时（如"查一下今天的汇率再填表"），自动触发内置搜索，将结果整合到回复中。

### 8.2 外部工具生态

通过MCP协议可扩展的外部工具：

| 工具 | 用途 | 集成方式 |
|:---|:---|:---|
| 天气查询 | 任务辅助（"下雨天记得带伞"） | MCP tool + 天气API |
| 汇率转换 | 财务操作辅助 | MCP tool + 汇率API |
| 翻译 | 多语言界面处理 | MCP tool + DeepL API |
| 文件转换 | PDF/Word/Excel互转 | MCP tool + pandoc |
| 邮件发送 | 任务结果通知 | MCP tool + SMTP |

工具注册采用动态发现机制：每个MCP server启动时自动注册tools到Kimi的tool列表，Agent通过tool_calls统一调用。

---

## 9. GUI界面与交互设计（预留）

当前为纯CLI应用，通过命令行交互。未来如需GUI，推荐技术栈：

| 组件 | 推荐方案 | 理由 |
|:---|:---|:---|
| 系统托盘 | `pystray` | 轻量，~10MB内存 |
| 全局快捷键 | `pynput` | 支持监听+控制 |
| 对话面板 | `pywebview` + FastAPI | 系统WebView渲染，~30MB |
| 语音输入 | `faster-whisper` | 本地实时，base模型~150MB |
| 语音输出 | `edge-tts` | 免费，Edge浏览器语音 |

CLI交互模式：启动后进入对话循环，用户输入自然语言指令，Agent执行后返回结果。支持`/pause`暂停、`/resume`恢复、`/settings`配置、`/quit`退出。

---

## 10. 内部架构细化

### 10.1 数据流设计 — EventBus

自研EventBus，融合三种机制：
- **优先级队列**：`asyncio.PriorityQueue`，五级优先级（CRITICAL/HIGH/NORMAL/LOW/BACKGROUND），同优先级FIFO
- **发布-订阅**：`defaultdict(list)`注册表，按event_type路由
- **中间件链**：洋葱模型，预置日志/指标/持久化三类中间件

核心事件类型：

| 事件域 | 事件类型 | 优先级 |
|:---|:---|:---:|
| 生命周期 | `SESSION_STARTED` / `SESSION_ENDED` | CRITICAL |
| 任务 | `TASK_RECEIVED` / `TASK_PLANNED` / `TASK_COMPLETED` / `TASK_FAILED` | HIGH |
| 动作 | `ACTION_EXECUTED` / `ACTION_FAILED` / `ACTION_RETRIED` | NORMAL |
| Agent状态 | `AGENT_STATE_CHANGED` | HIGH |
| 感知 | `SCREEN_CAPTURED` / `UI_PARSED` / `OCR_COMPLETED` | NORMAL |
| LLM | `LLM_REQUEST_STARTED` / `LLM_REQUEST_COMPLETED` | NORMAL |
| 系统 | `ERROR_OCCURRED` / `HEARTBEAT` | HIGH / LOW |

每个Event携带`correlation_id`与`parent_id`，支持全链路追踪。

### 10.2 状态机设计

11个Agent状态：

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

状态持久化：每步操作后将当前状态写入SQLite，崩溃后可从最后状态恢复。

### 10.3 并发模型

asyncio主事件循环 + 3个线程池：

| 线程池 | 用途 | max_workers | 隔离原因 |
|:---|:---|:---:|:---|
| 视觉推理池 | Qwen2.5-VL-7B推理 | 2 | 模型推理阻塞事件循环 |
| LLM推理池 | Kimi API调用 | 4 | HTTP IO阻塞 |
| IO操作池 | 截图、文件读写 | 8 | 磁盘IO阻塞 |

并行感知流水线：截图 + OCR + UI解析三个操作并行执行，全部完成后才进入Think阶段。

### 10.4 错误处理与重试

| 层级 | 策略 | 参数 |
|:---|:---|:---|
| 工具级 | 指数退避重试 | 3次，1s→2s→4s |
| 动作级 | Reflect分析→重试或切换工具 | 最多2次重试 |
| 任务级 | 暂停→请求用户指导 | 连续3次动作失败 |
| 会话级 | 切换本地模型降级 | API连续5次失败 |

断路器：failure_threshold=5，recovery_timeout=60s。

### 10.5 插件系统

| 组件 | 设计 |
|:---|:---|
| **Plugin ABC** | 抽象基类，定义`name`/`version`/`description`/`tools`/`on_load`/`on_unload` |
| **PluginManager** | 目录扫描（`plugins/`）、importlib动态加载、5秒间隔热重载、依赖检查 |
| **安全沙箱** | 插件通过EventBus通信，不能直接访问Agent内部状态；文件访问受filesystem-mcp白名单约束 |
| **生命周期** | load → init → register_tools → run → stop → unregister → unload |

示例插件：计算器（数学计算）、翻译器（文本翻译）、邮件发送（SMTP通知）。

### 10.6 数据存储层

SQLite 11张表：

| 表名 | 用途 |
|:---|:---|
| `config` | 键值配置 |
| `audit_log` | 操作审计日志 |
| `reflections` | 反思记录（FTS5全文搜索） |
| `skills` | 技能元数据 |
| `episodes` | 情景记忆（操作轨迹） |
| `user_preferences` | 用户偏好 |
| `sessions` | 会话记录 |
| `tasks` | 任务记录 |
| `actions` | 动作记录 |
| `errors` | 错误记录 |
| `state_persistence` | 状态机持久化 |

ChromaDB：向量存储，collection-per-user，存储技能embedding和记忆embedding。

文件系统：`skills/`（SKILL.md文件）、`memories/episodes/`（操作轨迹JSON）、`data/screenshots/`（截图缓存）。

---

## 11. 项目结构与依赖

### 11.1 目录结构

```
desktop-agent/
│
├── main.py                    # CLI入口，对话循环启动
├── config.py                  # Pydantic配置管理
├── config.yaml                # 用户配置文件（gitignore）
├── requirements.txt           # Python依赖
│
├── agent/                     # Agent核心包
│   ├── __init__.py
│   ├── core.py                # 五级ReAct循环
│   ├── llm.py                 # LLM封装：Kimi API + Ollama + 路由
│   ├── perceive.py            # 感知层：截图+OCR+UIA+SoM融合
│   ├── reflect.py             # 反思层：成功评估+失败分析
│   ├── think.py               # 推理层：任务规划+工具选择
│   ├── verify.py              # 验证层：效果检查+死循环检测
│   ├── memory.py              # Mem0会话记忆管理
│   ├── skills.py              # SKILL.md读写与检索
│   ├── builtin_tools.py       # 内置工具：截图、OCR、UI解析
│   └── state_machine.py       # FSM状态机
│
├── mcp/                       # MCP客户端封装
│   ├── __init__.py
│   ├── client.py              # MCP stdio客户端基类
│   ├── playwright_mcp.py      # playwright-mcp连接
│   ├── windows_mcp.py         # windows-mcp连接
│   └── filesystem_mcp.py      # filesystem-mcp连接
│
├── eventbus/                  # 事件总线
│   ├── __init__.py
│   ├── core.py                # EventBus核心
│   ├── events.py              # 事件类型定义
│   └── middleware.py          # 中间件链
│
├── plugins/                   # 插件目录
│   ├── __init__.py
│   └── base.py                # Plugin ABC基类
│
├── storage/                   # 数据存储层
│   ├── __init__.py
│   ├── sqlite.py              # SQLite管理
│   ├── chroma.py              # ChromaDB向量存储
│   └── filesystem.py          # 文件系统操作
│
├── skills/                    # 技能库目录
│   └── (SKILL.md文件)
│
└── data/                      # 本地数据
    ├── memory.db              # SQLite
    └── cache/                 # 截图缓存
```

### 11.2 依赖

```
# LLM
openai>=1.0              # Kimi API（OpenAI兼容）
mem0ai>=0.1              # 会话记忆

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
httpx>=0.27              # HTTP客户端
pydantic>=2.0            # 配置验证
pydantic-settings>=2.0   # 配置管理
pyyaml>=6.0              # YAML配置

# 日志
loguru>=0.7              # 开发日志
```

Ollama独立安装（`OllamaSetup.exe`），不在pip依赖中。

---

## 12. 实施路线图

| 阶段 | 时间 | 内容 | 关键产出 |
|:---|:---:|:---|:---|
| **Phase 0** | 1周 | 环境搭建：Ollama安装+模型拉取+Python环境+项目脚手架 | 能运行hello world |
| **Phase 1** | 2周 | 核心Agent：MCP连接+Kimi对话+EventBus+FSM+Builtin工具 | 能对话，能调用一个MCP工具 |
| **Phase 2** | 2周 | 感知层：截图+OCR+Ollama UI解析+SoM标注 | Agent能"看懂"屏幕 |
| **Phase 3** | 2周 | 执行层：playwright+windows+filesystem全工具+Fallback | 能操控浏览器和桌面 |
| **Phase 4** | 1周 | 学习机制：Mem0+SKILL.md+Reflexion | 会学习、记偏好 |
| **Phase 5** | 1周 | 内部架构：并发模型+错误处理+插件系统+数据存储 | 稳定运行、可扩展 |
| **Phase 6** | 1周 | 安全加固：操作分级+Kill Switch+审计日志 | 安全可控 |
| **Phase 7** | 持续 | 日常使用、积累skills、优化 | 越用越顺手 |

**总周期：10周出可用版本，之后持续迭代。**

---

## 13. 关键技术决策汇总

| 决策 | 选择 | 参考来源 | 理由 |
|:---|:---|:---|:---|
| 核心循环 | 五级增强ReAct | Cradle (BAAI) + UFO² (Microsoft) | 反思+验证保障可靠性，比Cradle少3次LLM调用 |
| 浏览器控制 | Playwright MCP | Anthropic官方 (31.2K stars) | A11y tree方案token效率最高 |
| 桌面控制 | Windows-MCP | wonderwhy-er (5,456 stars) | UIA控件树确定性定位 |
| 文件操作 | filesystem-mcp Python | PyPI社区版 | 纯Python，与项目同栈 |
| 主模型 | Kimi K2.6 API | Moonshot AI | 256K上下文+tool_calls+联网搜索 |
| 本地模型 | Ollama | ollama.com | Windows一键安装，OpenAI兼容 |
| UI解析 | Qwen2.5-VL-7B | 阿里通义 | Ollama官方支持，ScreenSpot-V2 89.5% |
| OCR | RapidOCR | RapidAI (ONNXRuntime) | CPU 30ms，零显存 |
| 记忆 | Mem0 + SQLite | mem0.ai | LoCoMo 67.13%，p95延迟0.2s |
| 技能格式 | SKILL.md | OpenClaw生态 | 33,000+社区skills可复用 |
| 反思 | Reflexion | Shinn et al., NeurIPS 2023 | 语言反馈学习，15-25%提升 |
| 搜索 | Kimi内置 | Moonshot官方 | ¥0.025/次，无需额外API |
| 事件总线 | 自研EventBus | asyncio PriorityQueue | 优先级队列+Pub/Sub+中间件链 |
| 状态机 | 自研FSM (11状态) | UFO²参考 | 完整生命周期覆盖+崩溃恢复 |
| 并发 | asyncio+3线程池 | Cradle参考 | 主循环非阻塞，推理隔离 |
| 插件 | ABC+动态加载 | 自研 | importlib+热重载+安全沙箱 |
| 数据存储 | SQLite+ChromaDB+文件系统 | 三元组合 | 关系数据+向量检索+文件资产 |
