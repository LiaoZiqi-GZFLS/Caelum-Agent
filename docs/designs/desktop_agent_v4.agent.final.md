# Windows桌面操作助手 — 技术方案 v4

> **版本**: v4.0 | **性质**: 个人CLI项目 | **平台**: Windows 10/11
> **大脑**: Kimi K2.6 (Moonshot AI) | **本地模型管理**: Ollama
> **参考架构**: Cradle六模块循环 (BAAI, arXiv:2403.03186) + UFO² AgentOS (Microsoft, NAACL'25)

---

## 1. 方案概述

### 1.1 定位与能力边界

一个Windows命令行桌面操作Agent。用户通过自然语言下达指令，Agent自主操控浏览器和Windows桌面应用完成任务。核心能力：**感知屏幕 → 规划操作 → 执行控制 → 学习积累**。

**双域覆盖**：
- **浏览器域**：基于Playwright MCP的accessibility tree方案，token消耗比纯视觉方案减少82.5%（来源：Anthropic Computer Use评估报告，2024年10月）
- **桌面域**：基于Windows-MCP的UIA控件树方案，确定性控件定位+视觉Fallback

**不做的事**：不设计GUI界面（CLI够用）、不做CI/CD、不做代码签名、不做自动更新、不做企业级部署。

### 1.2 核心循环：增强型五级ReAct

参考Cradle的六模块循环（Information Gathering → Self-Reflection → Task Inference → Skill Curation → Action Planning → Memory）和UFO²的ReAct循环，精简为五级结构：

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

**五级详解**：

| 阶段 | 功能 | 输入 | 输出 | LLM调用 |
|:---|:---|:---|:---|:---:|
| **Perceive** 感知 | 多源感知融合：截图+OCR+UIA/A11y树+SoM标注 | 屏幕状态 | 结构化环境描述 | 0（本地模型） |
| **Reflect** 反思 | 评估上一步是否成功，分析失败原因，检索历史经验 | 上一步动作+结果 | 反思结论+改进建议 | 1（可选） |
| **Think** 推理 | 任务规划+工具选择+参数生成 | 感知结果+反思 | 工具调用计划 | 1（必须） |
| **Act** 执行 | 通过MCP调用具体工具 | 工具计划 | 执行结果 | 0 |
| **Verify** 验证 | 检查操作效果，更新记忆，检测死循环 | 执行结果 | 成功/失败/需重试 | 0 |

**反思触发条件**：上一步操作失败、遇到未知UI状态、连续多步在同一UI间切换（死循环检测）。正常成功路径跳过Reflect，节省token。

---

## 2. Kimi K2.6 API 能力对接

### 2.1 接口基本信息

| 项目 | 内容 | 来源 |
|:---|:---|:---|
| 接口标准 | OpenAI兼容 | Moonshot官方文档 |
| Base URL | `https://api.moonshot.cn/v1` | platform.moonshot.cn |
| 模型 | `kimi-k2-6`（多模态） | 2025年7月 |
| 上下文 | 256K tokens | 官方文档 |
| 工具调用 | `tool_calls` / `tools` 参数 | 官方文档 |
| 流式输出 | `stream=True` | 官方文档 |
| 多模态 | 图文混合输入，1024 tokens/图 | 官方文档 |
| 联网搜索 | 通过tool_calls内置 | 官方文档 |

### 2.2 定价

| 项目 | 价格 | 备注 |
|:---|:---|:---|
| 输入（缓存命中） | ¥6.5 / 1M tokens | Context Caching节省75% |
| 输入（缓存未命中） | ¥26 / 1M tokens | 首次请求 |
| 输出 | ¥27 / 1M tokens | |
| 联网搜索 | ¥0.025 / 次 | 无需额外API key |

### 2.3 Tool Calls 工作模式

Kimi遵循OpenAI标准tool call协议。核心要点：

1. 客户端将**所有操作能力**（浏览器控制、桌面控制、文件操作、联网搜索）注册为JSON Schema格式的tools
2. 调用`chat.completions.create(tools=tools)`时，Kimi自主决定调用哪些工具
3. 返回`finish_reason="tool_calls"`时，客户端执行对应工具，结果以`role="tool"`回传
4. Kimi可能连续调用多个工具，直到`finish_reason="stop"`

### 2.4 联网搜索能力

Kimi API内置联网搜索，注册`search` tool即可：

```python
{
    "type": "function",
    "function": {
        "name": "search",
        "description": "通过搜索引擎搜索互联网内容。当知识无法回答用户问题，或用户请求联网搜索时调用。",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "搜索内容"}
            }
        }
    }
}
```

搜索由Kimi后端执行（非本地浏览器），结果包含标题+URL+摘要。无需接入Tavily/Brave等第三方搜索API。

### 2.5 Context Caching

长上下文（截图base64 + 控件树）可通过Context Caching缓存复用。缓存命中时输入价格降至¥6.5/1M tokens（节省75%）。多轮对话中重复的截图信息自动命中缓存。

---

## 3. 本地模型管理 — Ollama

### 3.1 选型理由

| 方案 | 配置复杂度 | Windows支持 | OpenAI兼容 | 模型生态 |
|:---|:---:|:---:|:---:|:---|
| Ollama | 极低（一键安装） | 原生 | 是 | 极丰富（官方库1000+模型） |
| llama.cpp | 高（手动编译） | 需WSL/MSYS2 | 需额外层 | 极丰富 |
| vLLM | 中（pip安装） | 一般 | 是 | 需手动下载 |
| Transformers | 低（pip安装） | 是 | 否 | 极丰富 |

Ollama在Windows上一键安装（`OllamaSetup.exe`），提供`http://localhost:11434`的OpenAI兼容API。`ollama pull`拉取模型，`ollama list`查看已安装。对个人项目而言配置成本最低。

### 3.2 部署模型

| 模型 | 用途 | 命令 | 显存 | 常驻/按需 |
|:---|:---|:---|:---:|:---:|
| Qwen2.5-VL-7B | UI元素检测、截图理解 | `ollama pull qwen2.5vl:7b` | ~6GB | 常驻 |
| Qwen2.5-3B | 任务分类、意图路由 | `ollama pull qwen2.5:3b` | ~2.5GB | 按需 |
| granite3.2-vision | OCR备选 | `ollama pull granite3.2-vision` | ~4GB | 按需 |

推荐配置：RTX 3060 12GB 可同时常驻 Qwen2.5-VL-7B。8GB显卡需交替加载。

Ollama API完全兼容OpenAI SDK，切换远程/本地只需改`base_url`：

```python
# 本地Ollama
local_client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
# Kimi远程
kimi_client = OpenAI(base_url="https://api.moonshot.cn/v1", api_key=os.getenv("KIMI_API_KEY"))
```

---

## 4. 感知层设计

感知层负责将屏幕状态转化为结构化信息，供Kimi理解。三层感知融合：

### 4.1 多源感知融合

| 数据源 | 来源工具 | 用途 | 优先级 |
|:---|:---|:---|:---:|
| **UIA控件树** | Windows-MCP (`get_ui_tree`) | 桌面应用的确定性控件定位 | 最高 |
| **A11y树** | Playwright MCP (`browser_get_accessibility_tree`) | 浏览器页面的确定性元素定位 | 最高 |
| **视觉感知** | Ollama + Qwen2.5-VL-7B | UI元素检测、SoM标注、无法A11y时的Fallback | 中 |
| **OCR** | RapidOCR (`rapidocr-onnxruntime`) | 截图文字识别，CPU 30ms | 辅助 |

### 4.2 Set-of-Mark (SoM) 标注

参考OmniParser v2（Microsoft, arXiv:2406.12717）的SoM技术：在截图上用色块+编号标注检测到的UI元素，将连续坐标空间离散化为元素ID。这种方式让LLM通过元素编号而非原始坐标来指定操作目标，消除分辨率依赖。

实现方式：Qwen2.5-VL-7B检测截图中的元素，返回`[(x1,y1,x2,y2, label)]`列表，Agent在截图上绘制编号色块后传给Kimi。

### 4.3 感知融合流程

```
屏幕截图 ──▶ RapidOCR文字识别（30ms, CPU）
    │
    ├──▶ UIA/A11y控件树（确定性路径） ──┐
    │                                     ├──▶ 结构化环境描述 ──▶ Kimi
    └──▶ Qwen2.5-VL-7B元素检测 ──▶ SoM标注 ──┘         (感知结果)
              (本地, ~500ms)
```

---

## 5. MCP工具集

所有工具以JSON Schema注册为Kimi的tools，Kimi通过tool_calls选择。

### 5.1 playwright-mcp（浏览器操作）

来源：`@anthropic/playwright-mcp-server`（Anthropic官方，stdio MCP服务器）

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `browser_navigate` | 打开URL | Read |
| `browser_click` | 点击元素（by a11y selector） | Write-safe |
| `browser_type` | 输入文本 | Write-safe |
| `browser_select` | 下拉选择 | Write-safe |
| `browser_press_key` | 按键 | Write-safe |
| `browser_get_accessibility_tree` | 获取页面A11y树 | Read |
| `browser_screenshot` | 页面截图 | Read |
| `browser_evaluate` | 执行JS代码 | Write-risky |

启动：`npx @anthropic/playwright-mcp-server`，Agent通过MCP stdio协议连接。

### 5.2 windows-mcp（桌面UI操作）

来源：`wonderwhy-er/desktop-commander`中的windows-mcp（5,456 stars）

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `get_ui_tree` | 获取当前窗口UIA控件树 | Read |
| `click_element` | 点击控件（by UIA selector） | Write-safe |
| `type_text` | 在控件中输入文本 | Write-safe |
| `send_keys` | 发送快捷键 | Write-safe |
| `get_window_list` | 列出所有窗口 | Read |
| `focus_window` | 聚焦指定窗口 | Write-safe |
| `take_screenshot` | 屏幕截图 | Read |

启动：`python -m windows_mcp_server`，stdio连接。

**Fallback策略**：UIA定位失败时，通过SoM标注的元素坐标使用pyautogui模拟点击（坐标Fallback）。

### 5.3 filesystem-mcp（文件操作）— Python版本

来源：`filesystem-mcp`（PyPI, Python实现，MIT License）

**不使用官方的`@modelcontextprotocol/server-filesystem`（Node版本），使用纯Python的`filesystem-mcp`**：

安装：`pip install filesystem-mcp`

启动：`filesystem-mcp`（stdio模式，自动通过MCP客户端管理）

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `read_file` | 读取文件内容（支持head/tail） | Read |
| `read_multiple_files` | 批量读取 | Read |
| `list_directory` | 列出目录（含大小） | Read |
| `directory_tree` | 递归目录树 | Read |
| `search_files` | 文件搜索 | Read |
| `write_file` | 写入文件（覆盖） | Write-risky |
| `edit_file` | 选择性编辑（dryRun预览） | Write-risky |
| `create_directory` | 创建目录 | Write-safe |
| `get_file_info` | 文件元数据 | Read |

**安全设计**：启动时传入允许访问的目录白名单，超出范围的操作被拒绝。参考`mcp-workspace`（MarcusJellinghaus）的路径校验机制。

### 5.4 Builtin工具（Agent内置，非MCP）

在Agent Python代码中直接实现，不通过MCP协议：

| 工具 | 实现 | 用途 |
|:---|:---|:---|
| `screenshot` | `mss`库 | 全屏/区域截图（~10ms） |
| `ocr` | `rapidocr-onnxruntime` | 截图文字识别（~30ms, CPU） |
| `ui_parse` | Ollama + qwen2.5vl | 截图UI元素检测 |
| `search` | Kimi内置 | 联网搜索（¥0.025/次） |

---

## 6. 学习机制

三层学习，渐进式增强。

### 6.1 会话记忆 — Mem0

来源：Mem0（mem0.ai，Python库`mem0ai`）

- 每次对话自动提取关键事实（用户偏好、操作结果、环境状态）
- 跨会话时加载历史记忆，Agent记得用户之前的偏好
- 本地SQLite存储，零配置
- 语义检索，相似度>0.7的记忆自动注入上下文

### 6.2 技能库 — SKILL.md

来源：OpenClaw生态（33,000+社区skills）+ AutoSkill（ECNU/上海AI Lab）

- 用户教会的操作流程，保存为SKILL.md到`./skills/`目录
- 格式兼容OpenClaw：`name` + `description` + `tools` + `steps`
- 每次任务前用关键词+向量检索相关skill作为few-shot示例
- 技能积累越多，Agent处理已知任务越熟练

### 6.3 反思 — Reflexion

来源：Reflexion（Shinn et al., NeurIPS 2023, 15-25%成功率提升）

- 操作失败后自动反思：失败原因+改进策略
- 反思结果保存到SQLite，关键词索引
- 下次遇到类似场景时检索历史反思作为上下文提示
- 不过度设计：简单关键词匹配即可，不需要复杂向量检索

---

## 7. 项目结构

```
desktop-agent/
│
├── main.py                    # CLI入口，对话循环启动
├── config.py                  # Pydantic配置管理（API key、模型参数、路径）
├── config.yaml                # 用户配置文件（gitignore）
├── requirements.txt           # Python依赖
│
├── agent/                     # Agent核心包
│   ├── __init__.py
│   ├── core.py                # 增强型五级ReAct循环
│   ├── llm.py                 # LLM封装：Kimi API + Ollama本地 + 路由
│   ├── perceive.py            # 感知层：截图+OCR+UIA+SoM融合
│   ├── reflect.py             # 反思层：成功评估+失败分析
│   ├── think.py               # 推理层：任务规划+工具选择
│   ├── verify.py              # 验证层：效果检查+死循环检测
│   ├── memory.py              # Mem0会话记忆管理
│   ├── skills.py              # SKILL.md读写与检索
│   └── builtin_tools.py       # 内置工具：截图、OCR、UI解析
│
├── mcp/                       # MCP客户端封装
│   ├── __init__.py
│   ├── client.py              # MCP stdio客户端基类
│   ├── playwright_mcp.py      # playwright-mcp连接
│   ├── windows_mcp.py         # windows-mcp连接
│   └── filesystem_mcp.py      # filesystem-mcp连接
│
├── skills/                    # 技能库目录
│   └── (SKILL.md文件)
│
└── data/                      # 本地数据
    ├── memory.db              # SQLite（Mem0 + 反思记录）
    └── cache/                 # 截图缓存
```

---

## 8. 依赖清单

```
# LLM
openai>=1.0              # Kimi API（OpenAI兼容）
mem0ai>=0.1              # 会话记忆

# MCP
mcp>=1.0                 # MCP协议SDK

# Windows操作
pywinauto>=0.6           # UIA Fallback
pyautogui>=0.9           # 坐标Fallback
mss>=9.0                 # 高性能截图

# 视觉
rapidocr-onnxruntime     # OCR（CPU）
pillow>=10.0             # 图像处理

# 工具
httpx>=0.27              # HTTP客户端
pydantic>=2.0            # 配置验证
pydantic-settings>=2.0   # 配置管理
pyyaml>=6.0              # YAML配置

# 日志
loguru>=0.7              # 开发日志

# 文件系统MCP（Python版）
filesystem-mcp            # PyPI，纯Python实现
```

Ollama独立安装（`OllamaSetup.exe`），不在pip依赖中。

---

## 9. 实施计划

| 阶段 | 时间 | 内容 | 关键产出 |
|:---|:---:|:---|:---|
| 1 | 1周 | 脚手架+MCP连接+Kimi对话 | 能对话，调通一个MCP工具 |
| 2 | 1周 | 感知层（截图+OCR+Ollama+SoM） | Agent能"看懂"屏幕 |
| 3 | 1周 | 执行层（playwright+windows+filesystem全工具） | 能操控浏览器和桌面 |
| 4 | 1周 | 学习机制（Mem0+SKILL.md+Reflexion） | 会学习、记偏好 |
| 5 | 持续 | 日常使用、积累skills、优化 | 越用越顺手 |

**总周期：4周出可用版本。**

---

## 10. 关键技术决策

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
| 记忆 | Mem0 | mem0.ai | 语义记忆，本地SQLite |
| 技能格式 | SKILL.md | OpenClaw生态 | 33,000+社区skills可复用 |
| 反思 | Reflexion | Shinn et al., NeurIPS 2023 | 语言反馈学习，零训练成本 |
| 网络搜索 | Kimi内置 | Moonshot官方 | ¥0.025/次，无需额外API |
