# 基于 Agent-Controller 的桌面智能体实习项目 —— 系统设计文档

> **项目阶段**：第一阶段 · 第 1 周

> **成员**：廖子祺

---

## 1. 项目背景与目标

随着大语言模型（LLM）能力快速提升，让 AI Agent 直接操作图形界面（GUI Agent）成为重要研究方向。现有桌面自动化方案普遍存在四个核心问题：

1. **太慢**：一次简单点击可能需要多次 LLM 调用，整体任务耗时远超人工。
2. **容易错**：相似按钮、弹窗、动态加载内容经常被误判。
3. **太贵**：每一步都把整张屏幕截图喂给多模态模型，Token 消耗高。
4. **不会进步**：昨天做过的任务，今天再做一遍不会更快。

本项目基于已有的 `Agent-Controller` SDK（一个 Windows 优先的通用桌面自动化 SDK），通过 OCR、视觉元素检测、元素 ID 化文本 markup、动作记忆、分层规划、Mixture-of-Grounding 等技术，构建一个能够自动完成知乎网页、WPS 客户端、微信客户端三类复杂任务的桌面智能体。项目目标不仅是跑通三个阶段的任务，更要在速度、准确率、Token 成本、可扩展性四个维度上形成可量化的改进。

## 2. 需求分析

### 2.1 功能需求

项目需从易到难完成以下三个级别的挑战任务：

| 阶段   | 目标应用       | 任务流程                                                                                                                       |
| ------ | -------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 一阶段 | 知乎（网页）   | 登录 → 写文章 → Agent 自动识别内容并生成配图（文生图 / HTML Canvas / SVG 等）→ 发表 → 搜索文章 → 评论 → 点赞、收藏、喜欢 |
| 二阶段 | WPS（客户端）  | 打开客户端 → 新建文字文档 → 写文章（可与知乎内容相同）→ 样式格式设置（标题、字体、序号等）→ 保存 → 导出为 PDF             |
| 三阶段 | 微信（客户端） | 打开客户端 → 搜索“火眼审阅” → 选择类型为服务号的结果 → 关注 → 发送一段文字私信                                           |

### 2.2 非功能需求

| 维度       | 说明                 | 本项目目标                                                                      |
| ---------- | -------------------- | ------------------------------------------------------------------------------- |
| 速度       | 完成任务耗时（秒）   | 一阶段知乎任务总耗时控制在 3 分钟以内                                           |
| Token 消耗 | 总消耗 Token 数      | 通过文本 markup 优先和记忆复用，相比纯视觉方案降低 50% 以上                     |
| 稳定性     | N 次运行成功次数占比 | 单任务跑 5 次成功率 ≥ 80%                                                      |
| 扩展性     | 新任务支持速度       | 新增类似任务（如朋友圈点赞、知乎热点分析）可在 1 小时内完成配置或复用已有 skill |

### 2.3 约束条件

- **平台**：当前 Agent-Controller 仅在 Windows 上完整验证（UIA、DPI 缩放等），开发和测试以 Windows 为主。
- **网络**：知乎、文生图 API 等需要联网；WPS/微信为本地客户端。
- **权限**：部分操作（如启动应用、微信登录状态）依赖当前用户环境和账号权限。
- **反爬/反自动化**：知乎可能存在验证码、频率限制；微信客户端可能有操作风控。
- **成本控制**：优先使用文本 LLM，仅在必要时调用多模态视觉模型。
- **凭据安全**：知乎/微信等账号的 cookie、密码、API key 仅保存在本地 `.env` 文件中，`.env` 已加入 `.gitignore`，不进入版本控制；打包产物中仅包含 `.env.example`，避免泄漏密钥。

## 3. 总体架构

系统整体采用 Agent-Controller 原生分层架构，并在此之上补充任务编排层与评估层。

| 层级         | 模块/文件                                           | 核心职责                                         | 关键技术/组件                                                                                                    |
| ------------ | --------------------------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| 任务编排层   | `tests/benchmark/`、`examples/zhihu_task.py` 等 | 定义三阶段任务流程、成功判定、指标采集           | 任务脚本、基准测试、成功判据函数                                                                                 |
| Agent 层     | `cli.py`、`agent/tools.py`                      | 工具注册、LLM 调用、任务入口                     | smolagents`CodeAgent`、`LiteLLMModel`                                                                        |
| 规划层       | `planning/`                                       | 复杂任务拆分为子任务并协调执行                   | `Manager`、`Worker`、`Orchestrator`、`BestOfNPlanner`                                                    |
| 记忆层       | `memory/`                                         | 成功轨迹记录、recall、反思、技能提取             | `TrajectoryRecorder`、`MemoryStore`、`ReflectionEngine`、`ExperienceSkill`                               |
| Grounding 层 | `grounding/`                                      | 把 LLM 引用解析为物理像素坐标                    | 多专家路由：`Explicit`、`Text`、`UIA`、`VisionDetector`、`PlaywrightDom`、`VisionLLM`、`UI-TARS`   |
| 表示层       | `representation/`                                 | 元素 ID 分配、文本 markup 生成、引用解析         | `ElementRegistry`、`to_markup`、`ScreenRepresentation.resolve()`                                           |
| 感知层       | `perception/`                                     | 截图、OCR、检测、UIA 注入、二维码、容器轮廓      | `screenshot.py`、`ocr.py`、`detector.py`、`uia_controls.py`、`qrcode.py`、`merge.py`、`enhance.py` |
| 动作层       | `action/`                                         | 鼠标、键盘、剪贴板、启动应用、DPI 缩放、UIA 模式 | `ActionExecutor`、`CoordinateScaler`、`ClickTool`、`TypeTool`、`DragTool` 等                           |
| 技能/MCP 层  | `skills/`、`mcp/`                               | 浏览器自动化、搜索、文生图、UIA 等扩展能力       | `playwright`、`uia`、`duckduckgo`、`firecrawl`、`open_websearch`、`generate_image` skill             |

**数据流：**

| 步骤 | 执行层       | 输入/输出                                                           |
| ---- | ------------ | ------------------------------------------------------------------- |
| 1    | 感知层       | 截图 + OCR + 检测 + UIA 注入 → 带 ID 的文本 markup                 |
| 2    | Agent 层     | LLM 基于 markup 选择工具/动作                                       |
| 3    | Grounding 层 | LLM 引用（ID/文本/坐标）→ 物理像素坐标                             |
| 4    | 动作层       | 执行鼠标/键盘/UIA 操作，前后截图对比验证                            |
| 5    | 记忆层       | 成功后记录轨迹；失败时通过 checkpoint 回到安全点，由 planner replan |

**关键设计原则：**

- **文本优先**：能用 OCR/UIA 拿到的文本就不调用视觉模型。
- **确定 ID**：同一份布局尽量输出相同 ID，降低 LLM 引用歧义。
- **记忆驱动**：做过的任务沉淀为可复用路径和 skill，越做越快。
- **降级哲学**：任何子系统失败都优雅降级，不阻塞主任务。

## 4. 关键技术方案

针对实习任务评价标准中提出的“慢、错、贵、不会进步”四个问题，系统采用以下技术方案。

### 4.1 慢：分层规划 + 异步感知 + 记忆复用

**问题**：简单任务人工 30 秒，AI 要 3–5 分钟。

**方案**：

1. **分层规划（Manager/Worker）**：由 Manager LLM 把复杂任务拆成子任务，每个子任务交给 Worker Agent 执行。Worker 只关注当前一步，减少长上下文干扰；Manager 根据执行结果 replan，避免一次性长计划失效。
2. **异步感知增强**：Layer 1 同步返回基础 markup（检测 + OCR + UIA + 二维码）后立即让 LLM 决策；Layer 2 在后台线程补充容器轮廓和 UI-TARS 点击点，失败时无损回退。
3. **动作记忆复用**：成功任务序列保存到 `memory/trajectories.json`，相似任务通过 `recall` 工具直接返回历史步骤，LLM 可复用已知-good 路径，减少推理轮次。

### 4.2 错：多源感知融合 + Mixture-of-Grounding + 动作验证

**问题**：相似按钮点错，弹窗不知道关还是填。

**方案**：

1. **五源感知融合**：视觉检测 + OCR + UIA 控件注入 + 二维码 + 容器轮廓，多源结果经 IoU 去重合并，UIA 控件置信度 0.99 优先胜出。
2. **UIA 状态注入**：对 checkbox、radio、input、slider 等读取 UIA 模式，输出 `[checked]`、`[value=...]` 等状态标记，让 LLM 判断当前界面状态。
3. **Mixture-of-Grounding**：目标引用解析不再使用固定优先级，而是让 Explicit / Text / UIA / VisionDetector / PlaywrightDom / VisionLLM / UI-TARS 多位专家竞争，按置信度短路，低于阈值再升级到视觉模型。
4. **动作生效验证**：Click/Type 后做前后截图对比，向 LLM 报告 `changed/unchanged/unverified`，连续 unchanged 达到阈值提示使用 checkpoint restore。

### 4.3 贵：文本 Markup 优先 + 视觉模型按需触发

**问题**：每步都把截图喂给多模态模型，Token 消耗高。

**方案**：

1. **文本 markup 作为默认输入**：每次只把 `perceive_screen()` 生成的文本 markup 喂给 LLM，markup 中已包含元素 ID、文本、类型、坐标、状态。
2. **视觉模型仅 fallback**：`look` 工具仅在 markup 不足以描述内容时由 LLM 主动调用，例如识别复杂图片、理解弹窗含义。
3. **Grounding 视觉专家降级**：默认关闭 UI-TARS / VisionLLM grounding；当文本/UIA 专家无法命中目标时才升级到视觉模型。
4. **记忆减少重复推理**：同一任务第二次执行时，历史轨迹直接给 LLM 参考，显著降低每步决策 Token。

### 4.4 不会进步：轨迹记录 + 反思 + 技能提取

**问题**：昨天做过的事今天不会更快。

**方案**：

1. **轨迹记录**：每次成功任务记录完整步骤序列到 `memory/trajectories.json`，包含任务描述、动作、坐标、结果。
2. **反思引擎**：任务结束后调用 Reflection LLM 总结成功/失败原因、经验教训、标签。
3. **技能提取**：对成功轨迹按标签聚类，达到阈值后抽象为 `ExperienceSkill` 保存到 `memory/experience_skills.json`；失败教训保存为 `AntiPattern`。
4. **召回策略**：`recall` 工具按任务相似度 + 标签重叠 + 信息论打分返回历史轨迹、可复用技能、避坑提示。

## 5. 模块详细设计

### 5.1 感知层（perception/）

**职责**：把屏幕像素转换为带 ID 的结构化元素列表。

**核心入口**：`perception/__init__.py::get_screen_representation()`

**流程**：

1. `screenshot.py` 截取屏幕，默认隐藏自身终端窗口。
2. `window.py` 获取前台窗口，作为 OCR 裁剪和上下文来源。
3. `detector.py` 根据 `DETECTOR_BACKEND` 加载检测器：OpenCV（默认零依赖）、OmniParser（需本地权重）、mock（仅用于测试/基线，不用于真实任务）。
4. `ocr.py` 根据 `OCR_BACKEND` 选择引擎，auto 顺序为 GPU PaddleOCR → CPU EasyOCR → Tesseract → CPU PaddleOCR；PaddleOCR 通过 `scripts/paddleocr_worker.py` 子进程隔离运行。
5. `uia_controls.py` 注入 Windows UIA 原生控件（按钮、滚动条、标题栏按钮、复选框、单选框、输入框等）。
6. `qrcode.py` 检测二维码并作为 `type=qrcode` 元素注入。
7. `merge.py` 对多源结果做 IoU 去重，保留带文本或高置信度元素，并做图标-标签绑定。
8. `enhance.py` 在后台线程补充 OpenCV 容器轮廓和 UI-TARS 点击点（Layer 2）。

**关键配置**：

- `PERCEPTION_OCR_ACTIVE_WINDOW_ONLY=true`：只 OCR 前台窗口，降低 CPU 耗时。
- `PERCEPTION_ASYNC_ENHANCE_ENABLED=true`：开启异步增强。
- `PERCEPTION_UIA_STATE=true`：读取 UIA 控件状态。

### 5.2 表示层（representation/）

**职责**：为元素分配确定性 ID，并生成 LLM 可读的文本 markup。

**核心类/函数**：

- `representation/registry.py::ElementRegistry`：按从上到下、从左到右排序后分配类型前缀 ID（`btn_1`、`input_1`、`txt_1` 等）。
- `representation/formatter.py::to_markup`：把元素列表格式化为文本 markup，支持按 `parent_id` 缩进分组。
- `representation/__init__.py::ScreenRepresentation.resolve()`：解析 LLM 引用（ID → 坐标 → 文本 → 子串）。

**输出示例**：

```text
[btn_1] button "发布" at (120,300,60,28)
[input_1] input "标题" at (100,200,400,32)
[chk_1] checkbox "记住我" [unchecked] at (50,350,80,20)
```

### 5.3 Grounding 层（grounding/）

**职责**：把 LLM 的元素引用解析为物理像素坐标。

**核心组件**：

- `grounding/experts.py`：定义 7 位专家。
  - Explicit：解析 ID / 坐标，置信度 1.0。
  - Text：按文本匹配，置信度 0.9 / 0.6。
  - UIA：读取 UIA 注入控件，置信度 0.95 / 0.7。
  - VisionDetector：基于 icon/image caption 文本，置信度 0.7 / 0.5。
  - PlaywrightDom：浏览器 DOM 元素，置信度 0.85。
  - VisionLLM：通用多模态定位，置信度 0.7。
  - UI-TARS：本地 UI-TARS-1.5 视觉 grounding，置信度 0.85。
- `grounding/router.py::GroundingRouter`：按 applicability 排序，先尝试非视觉专家，首个 ≥ `grounding_confidence_threshold` 直接返回；否则升级视觉专家。
- `grounding/experts.py::ground_target()`：动作工具统一调用此函数解析目标。

### 5.4 动作层（action/）

**职责**：把解析后的坐标/控件转换为真实鼠标/键盘/UIA 操作。

**核心组件**：

- `action/executor.py::ActionExecutor`：封装 pyautogui / pyperclip / UIA 模式。
- `action/scaling.py::CoordinateScaler`：物理像素 → 逻辑坐标，支持 per-monitor DPI。
- `action/uia_actions.py`：对 UIA 控件优先使用 Invoke/Toggle/Value/RangeValue 模式，失败再回退鼠标。
- 动作工具：`ClickTool`、`TypeTool`、`PressTool`、`DragTool`、`ScrollTool`、`LongPressTool`。

**动作验证**：`agent/verify.py::classify_change/describe_change` 对 Click/Type 做前后截图对比。

### 5.5 记忆与反思层（memory/）

**职责**：记录成功路径，支持 recall，并通过反思沉淀技能和避坑模式。

**核心组件**：

- `memory/runner.py::run_task`：设置 `TrajectoryRecorder`，任务成功时持久化。
- `memory/store.py::MemoryStore` / `memory/experience.py::ExperienceStore`：JSON 文件存储。
- `memory/recall.py` + `memory/its_retrieval.py`：按 `recent` / `similarity` / `info_theoretic` 策略召回。
- `memory/reflection.py::ReflectionEngine`：任务结束后生成反思和标签。
- `memory/skill_extractor.py::ExperienceSkillExtractor`：聚类成功轨迹，提取 `ExperienceSkill`。

### 5.6 规划层（planning/）

**职责**：把复杂任务拆分为子任务并协调执行。

**核心组件**：

- `planning/manager.py::Manager`：LLM 规划角色，输出 JSON 子任务数组。
- `planning/worker.py::Worker`：每个子任务以 `agent.run(reset=True)` 执行，保持进程级 recorder/stack。
- `planning/orchestrator.py::Orchestrator`：plan → execute → replan 循环，终止条件为 DONE / 失败上限 / 子任务上限。
- `planning/behavior.py::BestOfNPlanner` + `planning/judge.py::Judge`：首次规划采样 N 个候选，listwise 评审选最优（默认关闭）。

### 5.7 MCP 技能层（mcp/ + skills/）

**职责**：通过 MCP 协议扩展 Agent 能力。

**内置 MCP Server**：

- `playwright`：浏览器自动化。
- `uia`：Windows 原生无障碍树。
- `context7`：库文档检索。
- `duckduckgo`：免 key 搜索。
- `firecrawl`：高质量抓取（需 key，默认关）。
- `open_websearch`：多引擎搜索（需 Node/npx，默认关）。

**工作流 skills**：`finishing_branch`、`requesting_code_review`、`test_driven_development`、`writing_plans`、`subagent_driven_development`。

**文生图扩展**：知乎配图可通过两种方式生成：

1. **外部文生图 API**：在 `.env` 中配置 `IMAGE_API_KEY`（或复用已有 API），由 `CodeTool` 调用 HTTP 接口生成图片并保存到本地；再由 `ClickTool` 点击知乎编辑器的“上传图片”按钮完成插入。
2. **程序化生成**：由 LLM 生成 SVG 或 HTML Canvas 代码，`WriteFileTool` 保存为 `.svg`/`.png`，再上传。

为便于复用，可将配图逻辑封装为 `generate_image` skill，参数包含 `prompt`、`style`、`output_path`，成功执行后沉淀到 `memory/experience_skills.json`。

### 5.8 Agent 工具层（agent/ + cli.py）

**职责**：把底层能力封装为 LLM 可调用的工具。

**常备工具**：`Recall`、`Screenshot`、`Click`、`Type`、`Press`、`Drag`、`Scroll`、`LongPress`、`Look`、`Ground`、`LaunchApp`、`Checkpoint`、`Restore`、`Code`、`Shell`、`ReadFile`、`WriteFile`、`RunTests`、`Skill`、`Init`、`Compact`、`Context`。

**可选工具组**（由 `.env` 开关控制）：输入工具组、窗口/进程工具组、系统工具组、文件系统工具组、Scrape 工具组、PowerShell 工具组。

**工具选型说明**：

- `LaunchAppTool`：通过应用名称/路径启动或激活一个本地程序（如 `LaunchAppTool("wps")`），是最简启动方式。
- `AppTool`（窗口工具组）：除启动外，还可列出窗口、切换前台窗口、调整窗口大小/位置；当需要明确控制窗口状态时使用。
- 知乎网页优先用 `playwright` skill；本地客户端优先用 `LaunchAppTool` 启动，再用 `ClickTool`/`TypeTool` 操作。

**入口**：`python -m agent_controller.cli "<task>"` 单次执行；`chat` 子命令进入交互 REPL。

## 6. 三阶段任务技术映射

### 6.1 一阶段：知乎网页

| 步骤                   | 使用技术/工具                                                                                         | 关键配置                                        |
| ---------------------- | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| 打开浏览器并导航到知乎 | `LaunchAppTool` 启动浏览器；或 `playwright` skill 直接打开页面                                    | `MCP_PLAYWRIGHT_ENABLED=true`                 |
| 登录                   | `playwright` skill 填写账号密码；或 `ClickTool`+`TypeTool` 操作网页输入框                       | 需提前登录保存 cookie，或使用扫码登录降低失败率 |
| 进入写文章页面         | `playwright` skill `goto` + `ClickTool` 点击“写文章”按钮                                      | 优先使用 PlaywrightDom grounding                |
| 写文章                 | `TypeTool` 写入标题和正文；内容由 LLM 根据任务提示生成                                              | 使用 input/textarea 的 ID 或文本引用            |
| 生成配图               | `CodeTool` 调用文生图 API 或生成 SVG/Canvas HTML；`WriteFileTool` 保存图片；再 `ClickTool` 上传 | 文生图 API key 通过`.env` 注入                |
| 发表文章               | `ClickTool` 点击“发布”/“提交”按钮；通过动作验证确认页面跳转                                     | `VERIFY_ACTIONS=true`                         |
| 搜索文章               | `playwright` skill 或 `ClickTool`+`TypeTool` 在搜索框输入标题关键词                             | 使用文本 grounding                              |
| 评论/点赞/收藏/喜欢    | `ClickTool` 点击对应按钮；UIA/Playwright 确认元素状态                                               | 多源感知避免点错相似按钮                        |

**关键优化点**：

- 知乎是网页应用，优先走 `playwright` skill，减少视觉检测依赖，速度快且稳定。
- 登录状态通过浏览器 profile 或 cookie 持久化，避免每次重新登录。
- 配图生成可复用为 `generate_image` skill，沉淀到经验库。

### 6.2 二阶段：WPS 客户端

| 步骤         | 使用技术/工具                                                                                   | 关键配置                              |
| ------------ | ----------------------------------------------------------------------------------------------- | ------------------------------------- |
| 打开 WPS     | `LaunchAppTool("wps")` 或 `AppTool`                                                         | `WINDOW_TOOLS_ENABLED=true`         |
| 新建文字文档 | `ClickTool` 点击“新建”→“文字”；或 UIA 直接调用菜单                                       | `PERCEPTION_UIA_STATE=true`         |
| 写入文章     | `TypeTool` 输入内容；`PressTool` 换行/分段                                                  | 使用前台窗口 OCR + UIA 识别编辑区     |
| 设置样式     | `ClickTool` 选中标题/段落；`ClickTool` 点击工具栏样式按钮；或通过 UIA ValuePattern 设置属性 | 工具栏按钮通过文本或图标-标签绑定识别 |
| 保存         | `PressTool("ctrl+s")` 或 `ClickTool` 点击保存                                               | 动作验证确认保存对话框关闭            |
| 导出 PDF     | `ClickTool` 进入“文件”→“输出为 PDF”；确认导出路径                                        | `CodeTool` 可校验 PDF 是否生成      |

**关键优化点**：

- WPS 是本地客户端，主要依赖 OCR + UIA 控件注入；开启 `PERCEPTION_UIA_FULL_TREE` 可获得更完整控件树。
- 样式设置步骤易错，建议先用 checkpoint 保存安全点，失败时回退重做。
- 导出 PDF 后用 `CodeTool` 检查文件存在性，作为任务成功判据。

### 6.3 三阶段：微信客户端

| 步骤             | 使用技术/工具                                                         | 关键配置                      |
| ---------------- | --------------------------------------------------------------------- | ----------------------------- |
| 打开微信         | `LaunchAppTool("wechat")` 或 `AppTool`                            | 微信需已登录                  |
| 搜索“火眼审阅” | `ClickTool` 点击搜索框；`TypeTool` 输入；OCR 识别搜索结果         | 使用文本 grounding 选择匹配项 |
| 选择服务号结果   | `ClickTool` 点击类型为“服务号”的结果项                            | UIA 读取控件类型辅助判断      |
| 关注             | `ClickTool` 点击“关注”按钮                                        | 动作验证确认按钮状态变化      |
| 发送私信         | `ClickTool` 进入聊天窗口；`TypeTool` 输入文字；`ClickTool` 发送 | 发送后 OCR 校验聊天内容       |

**关键优化点**：

- 微信客户端对自动化较敏感，操作间隔和坐标要自然；可配置 `pyautogui.PAUSE` 适当加大延迟。
- 搜索框和结果列表通过 UIA + OCR 双重确认，避免误点公众号/小程序。
- 关注状态通过 UIA Toggle/Invoke 模式或截图对比验证。

## 7. 评估体系

系统从四个维度建立可量化的评估体系，并配套自动化测试脚本。

### 7.1 评估指标

| 指标       | 定义                                                                                          | 目标值                                  | 测量方式                             |
| ---------- | --------------------------------------------------------------------------------------------- | --------------------------------------- | ------------------------------------ |
| 速度       | 从任务开始到成功的总耗时（秒）                                                                | 知乎 ≤ 180s；WPS ≤ 240s；微信 ≤ 120s | 脚本记录`time.perf_counter()` 差值 |
| Token 消耗 | 单次任务中 LLM 调用消耗的总 Token 数                                                          | 相比纯视觉 baseline 降低 ≥ 50%         | 统计 litellm 回调或 Agent 日志       |
| 稳定性     | 同一任务连续运行 N 次的成功率                                                                 | ≥ 80%（N=5）                           | 跑 5 次，统计成功次数                |
| 扩展性     | 新增相似任务的支持耗时：基于现有 skill/工具组合，修改 prompt 或新增一条任务描述即可运行的时间 | ≤ 1 小时                               | 记录从接收新任务到成功运行所需时间   |

### 7.2 成功判定标准

每个阶段定义明确的成功判据，由自动化脚本或人工复核确认：

- **知乎**：文章标题可在搜索中查到，且存在目标互动（评论/点赞/收藏/喜欢）。
  - 自动化判定：`playwright` skill 检查搜索结果页是否包含目标标题；再检查文章页是否出现目标互动记录。
- **WPS**：指定路径存在导出的 PDF 文件，且内容包含目标文字。
  - 自动化判定：`CodeTool` 用 `os.path.exists` 检查 PDF 路径，用 `pypdf` 或 `pdfplumber` 读取文本并 assert 包含目标内容。
- **微信**：服务号“火眼审阅”出现在关注列表，且发送的私信在聊天窗口可见。
  - 自动化判定：OCR 识别关注列表或聊天窗口文本；对于风控敏感场景，允许人工复核作为辅助判据。

### 7.3 评估脚本

在 `tests/benchmark/` 下建立基准测试：

```python
# tests/benchmark/test_zhihu_baseline.py
import time

def test_zhihu_article_flow():
    start = time.perf_counter()
    # 调用 agent_controller.cli 或 run_task 执行知乎任务
    success = run_zhihu_task()
    elapsed = time.perf_counter() - start
    assert success
    assert elapsed < 180
```

评估脚本同时记录：

- 总耗时
- LLM 调用次数与 Token 数
- 失败步骤与失败原因
- 是否触发记忆复用 / checkpoint restore

## 8. 开发计划与里程碑

| 周次    | 阶段   | 主要工作                                   | 里程碑                                        |
| ------- | ------ | ------------------------------------------ | --------------------------------------------- |
| 第 1 周 | 准备   | 组队选题、技术预研、环境搭建、基线测试     | 完成《系统设计文档》，跑通`pytest -q`       |
| 第 2 周 | 一阶段 | 知乎登录、写文章、配图、发表、搜索、互动   | 知乎任务成功率 ≥ 60%，Token/耗时基线建立     |
| 第 3 周 | 二阶段 | WPS 打开、编辑、样式、保存、导出 PDF       | WPS 任务跑通，成功率 ≥ 60%                   |
| 第 4 周 | 三阶段 | 微信搜索、关注、私信                       | 微信任务跑通，成功率 ≥ 60%                   |
| 第 5 周 | 优化   | 记忆复用、稳定性提升、扩展性测试、文档整理 | 三阶段平均成功率 ≥ 80%，可以完成新增扩展任务 |

## 9. 风险与应对

| 风险                | 影响                 | 应对措施                                                   |
| ------------------- | -------------------- | ---------------------------------------------------------- |
| 知乎反爬/验证码     | 登录或发文失败       | 使用已登录浏览器 profile；降低操作频率；必要时人工介入登录 |
| 微信客户端风控      | 账号限制或登录失效   | 使用测试账号；控制操作频率；避免频繁加关注/发消息          |
| WPS 界面更新        | 按钮位置/文本变化    | 优先使用 UIA 控件而非固定坐标；建立回归测试                |
| OCR 识别率低        | 文本元素漏检导致点错 | 开启 PERCEPTION_UIA_STATE；必要时升级到视觉模型            |
| LLM API 不稳定/超支 | 任务中断或成本过高   | 设置 LLM_TIMEOUT；使用便宜的文本模型做默认；视觉模型按需   |
| 跨平台限制          | macOS/Linux 无法复现 | 实习阶段以 Windows 为主，记录平台依赖                      |

## 10. 环境配置与快速开始

```bash
# 1. 克隆仓库并进入目录
git clone <repo-url> && cd Agent-Controller

# 2. 可编辑安装（开发依赖）
pip install -e ".[dev]"

# 3. 复制环境变量模板并填写
#    至少需要 LLM_MODEL 和 LLM_API_KEY
cp .env.example .env

# 4. 运行测试
pytest -q

# 5. 运行一次简单任务验证环境
python -m agent_controller.cli "打开计算器，计算 1 + 1"
```

**关键 `.env` 配置**：

```bash
LLM_MODEL=deepseek/deepseek-chat
LLM_API_KEY=sk-xxxxxxxx
LLM_BASE_URL=

VERIFY_ACTIONS=true
MEMORY_ENABLED=true
RECOVERY_ENABLED=true
PLANNING_ENABLED=true
GROUNDING_ENABLED=true

MCP_PLAYWRIGHT_ENABLED=true
MCP_UIA_ENABLED=true
MCP_DUCKDUCKGO_ENABLED=true
```
