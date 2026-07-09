# Windows桌面操作助手 — 精简技术方案 v3

> **版本**: v3.0 | **性质**: 个人CLI项目 | **平台**: Windows 10/11
> **大脑**: Kimi K2.6 (Moonshot AI) | **本地模型管理**: Ollama

---

## 1. 项目定位

一个Windows命令行桌面操作Agent。通过自然语言对话，帮用户完成网页操作和Windows客户端操作。核心能力：理解屏幕、操控UI、学习用户习惯。

**不做的事**：不设计GUI界面（CLI即可）、不做企业级部署、不写CI/CD流水线、不做代码签名、不做自动更新。

---

## 2. 核心架构

单Agent + 多MCP工具模式。Agent核心是一个异步Python CLI程序，通过MCP协议调用各种工具完成操作。

```
用户输入（自然语言）
    ↓
[Agent Core] —— 对话管理 + 任务规划 + 反思学习
    ↓                    ↑
[MCP工具层] ————————————+（工具执行结果反馈）
    ├── playwright-mcp  → 浏览器操作
    ├── windows-mcp     → 桌面UI操作
    ├── mcp-filesystem  → 文件读写
    └── builtin-tools   → 截图、OCR、本地模型推理
    ↓
[模型层]
    ├── Kimi K2.6 API   → 核心推理、决策、学习
    └── Ollama本地      → UI解析、OCR、简单分类
```

架构模式采用**简化版ReAct循环**：

1. **Perceive**: 截图 + UIA控件树 + 浏览器A11y Tree
2. **Think**: Kimi K2.6根据感知结果选择工具、规划下一步
3. **Act**: 通过MCP调用具体工具执行
4. **Reflect**: 评估操作结果，更新记忆

---

## 3. Kimi K2.6 API 与 Tool Call

### 3.1 API基本信息

| 项目 | 内容 |
|:---|:---|
| 接口标准 | OpenAI兼容 |
| base_url | `https://api.moonshot.cn/v1` |
| 模型名称 | `kimi-k2.6` |
| 上下文长度 | 256K tokens |
| 多模态 | 支持图文混合输入 |
| 工具调用 | 支持 `tool_calls` / `tools` 参数 |
| 流式输出 | 支持 `stream=True` |
| 并行工具 | 支持一次调用多个工具 |

### 3.2 定价（2025年7月）

| 项目 | 价格 |
|:---|:---|
| 输入tokens（缓存命中） | ¥6.5 / 1M tokens |
| 输入tokens（缓存未命中） | ¥26 / 1M tokens |
| 输出tokens | ¥27 / 1M tokens |
| 联网搜索 | ¥0.025 / 次 |

### 3.3 Tool Call 工作机制

Kimi的tool call遵循OpenAI标准：

1. 客户端用JSON Schema定义工具（`tools`参数），通过`client.chat.completions.create()`发送
2. Kimi根据上下文决定调用哪些工具，返回`finish_reason="tool_calls"`
3. 客户端执行工具，将结果以`role="tool"`消息回传
4. Kimi根据工具结果继续推理，可能再次调用工具，直到`finish_reason="stop"`

**关键设计**：我们的Agent将**所有操作能力**（浏览器控制、桌面控制、文件操作、网络搜索）都注册为Kimi的tools。Kimi通过tool call选择具体工具，Agent负责执行并回传结果。

### 3.4 联网搜索内置能力

Kimi API**已内置联网搜索**。注册一个名为`search`的tool，Kimi在需要时会自动调用。无需额外接入Tavily/Brave等外部搜索API。

示例tool定义：

```python
{
    "type": "function",
    "function": {
        "name": "search",
        "description": "通过搜索引擎搜索互联网上的内容。当你的知识无法回答用户问题，或用户请求联网搜索时调用。",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "用户搜索的内容"}
            }
        }
    }
}
```

执行搜索后，将结果通过`role="tool"`回传，Kimi自动整合到回答中。

### 3.5 Context Caching

Kimi支持Context Caching（上下文缓存），将长上下文缓存复用，降低token成本。缓存命中时输入价格从¥26降至¥6.5/1M tokens（约75%节省）。适合多轮对话中重复的截图/控件树信息。

---

## 4. 本地模型管理 — Ollama

### 4.1 选型理由

Ollama在Windows上开箱即用，一行命令拉取模型，提供OpenAI兼容的本地API（`http://localhost:11434`）。比llama.cpp配置更简单，比vLLM更适合消费级显卡。

### 4.2 部署模型清单

| 模型 | 用途 | 命令 | 显存需求 |
|:---|:---|:---|:---:|
| Qwen2.5-VL-7B | UI元素检测与理解 | `ollama pull qwen2.5vl:7b` | ~6GB |
| Qwen2.5-3B | 任务路由/简单分类 | `ollama pull qwen2.5:3b` | ~2.5GB |
| granite3.2-vision | 屏幕截图OCR备选 | `ollama pull granite3.2-vision` | ~4GB |

**推荐配置**：RTX 3060 12GB 可同时常驻 Qwen2.5-VL-7B + Qwen2.5-3B。

### 4.3 调用方式

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
response = client.chat.completions.create(
    model="qwen2.5vl:7b",
    messages=[{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                    {"type": "text", "text": "列出图中所有可点击的UI元素"}]
    }]
)
```

Ollama API完全兼容OpenAI SDK，切换远程/本地模型只需改`base_url`和`model`名。

### 4.4 模型加载策略

- **常驻内存**：Qwen2.5-VL-7B（每帧UI解析都需要）
- **按需加载**：Qwen2.5-3B（任务路由等简单任务时启用）
- **未安装时Fallback**：纯Kimi API方案（所有感知走API，无本地模型也能运行）

---

## 5. MCP工具集

Agent通过MCP协议调用以下工具集。所有工具以JSON Schema形式注册到Kimi的`tools`参数中。

### 5.1 playwright-mcp（浏览器操作）

| 工具 | 功能 |
|:---|:---|
| `browser_navigate` | 打开URL |
| `browser_click` | 点击元素（by a11y selector） |
| `browser_type` | 输入文本 |
| `browser_select` | 下拉选择 |
| `browser_press_key` | 按键 |
| `browser_get_accessibility_tree` | 获取页面A11y树 |
| `browser_screenshot` | 页面截图 |
| `browser_evaluate` | 执行JS代码 |

**启动方式**：`npx @anthropic/playwright-mcp-server`，通过stdio与Agent通信。

**核心优势**：Accessibility Tree方案比纯视觉方案token消耗减少82.5%。

### 5.2 windows-mcp（桌面UI操作）

| 工具 | 功能 |
|:---|:---|
| `get_ui_tree` | 获取当前窗口UIA控件树 |
| `click_element` | 点击控件（by UIA selector） |
| `type_text` | 在控件中输入文本 |
| `send_keys` | 发送快捷键 |
| `get_window_list` | 列出所有窗口 |
| `focus_window` | 聚焦指定窗口 |
| `take_screenshot` | 屏幕截图 |

**启动方式**：`python -m windows_mcp_server`，通过stdio通信。

**Fallback**：当UIA无法定位时，通过坐标Fallback（pyautogui）执行。

### 5.3 mcp-filesystem（文件操作）

| 工具 | 功能 | 风险等级 |
|:---|:---|:---:|
| `read_text_file` | 读取文本文件 | Read |
| `read_media_file` | 读取图片/音频 | Read |
| `read_multiple_files` | 批量读取 | Read |
| `list_directory` | 列出目录 | Read |
| `directory_tree` | 递归目录树 | Read |
| `search_files` | 文件搜索 | Read |
| `write_file` | 写入文件（覆盖）| Write-risky |
| `edit_file` | 选择性编辑（diff模式）| Write-risky |
| `create_directory` | 创建目录 | Write-safe |

**启动方式**：`npx @modelcontextprotocol/server-filesystem /allowed/path`，通过stdio通信。

**权限控制**：只允许访问用户指定的目录（通过启动参数传入），不允许访问系统目录。

### 5.4 Builtin工具（Agent内置）

不通过MCP，直接在Agent Python代码中实现：

| 工具 | 实现 | 用途 |
|:---|:---|:---|
| `screenshot` | PIL+mss | 全屏/区域截图 |
| `ocr` | RapidOCR | 截图文字识别（~30ms） |
| `ui_parse` | Ollama+qwen2.5vl | 截图UI元素检测 |
| `search` | httpx+搜索引擎 | 网络搜索（被Kimi内置tool替代时直接用Kimi） |

---

## 6. 学习机制（精简版）

学习是核心差异化，但不过度设计。三层学习机制：

### 6.1 会话记忆 — Mem0

- 每次对话的上下文摘要自动保存
- 跨会话时加载历史记忆，Agent知道用户之前的偏好
- 本地SQLite存储，零配置

### 6.2 技能库 — SKILL.md

- 用户教会的操作流程，自动保存为SKILL.md文件到`./skills/`目录
- 格式兼容OpenClaw生态（33,000+社区skills可复用）
- 每次任务前检索相关skill作为few-shot示例

### 6.3 反思 — Reflexion

- 操作失败后，Agent自动反思失败原因
- 将反思结果保存到SQLite，下次遇到类似场景时参考
- 不实现复杂的向量检索，简单的关键词匹配即可

---

## 7. 项目结构

```
desktop-agent/
├── main.py                  # CLI入口，对话循环
├── agent/
│   ├── __init__.py
│   ├── core.py              # Agent核心：ReAct循环
│   ├── llm.py               # LLM封装：Kimi API + Ollama本地
│   ├── tools.py             # Builtin工具（截图/OCR/UI解析）
│   ├── memory.py            # Mem0记忆管理
│   ├── skills.py            # SKILL.md读写与检索
│   └── reflection.py        # 反思机制
├── mcp/
│   ├── __init__.py
│   ├── client.py            # MCP stdio客户端封装
│   ├── playwright.py        # playwright-mcp连接
│   ├── windows.py           # windows-mcp连接
│   └── filesystem.py        # mcp-filesystem连接
├── config.py                # 配置管理（Pydantic）
├── config.yaml              # 用户配置文件
├── requirements.txt         # 依赖
├── skills/                  # 技能库目录
│   └── example_skill.md
└── data/                    # 本地数据
    ├── memory.db            # SQLite记忆
    └── screenshots/         # 截图缓存
```

---

## 8. 依赖清单

```
# 核心
openai>=1.0           # Kimi API（OpenAI兼容）
mcp>=1.0              # MCP协议SDK

# Windows操作
pywinauto>=0.6        # UIA控件树Fallback
pyautogui>=0.9        # 坐标模拟Fallback
mss>=9.0              # 高性能截图

# 视觉
rapidocr-onnxruntime  # OCR（CPU，~30ms）
pillow>=10.0          # 图像处理

# 记忆
mem0ai>=0.1           # 语义记忆
chromadb>=0.5         # 向量存储

# 工具
httpx>=0.27           # 异步HTTP（搜索/API调用）
pydantic>=2.0         # 配置验证
pydantic-settings>=2.0 # 配置管理
pyyaml>=6.0           # YAML配置

# 日志
loguru>=0.7           # 开发日志
```

Ollama独立安装，不在pip依赖中。

---

## 9. 实施计划

| 阶段 | 时间 | 内容 | 产出 |
|:---|:---:|:---|:---|
| 1 | 1周 | 项目脚手架 + MCP连接 + Kimi API对话 | 能对话，能调用一个MCP工具 |
| 2 | 1周 | 感知层（截图+OCR+Ollama UI解析） | Agent能看到屏幕 |
| 3 | 1周 | 执行层（playwright + windows-mcp全工具） | 能操作浏览器和桌面 |
| 4 | 1周 | 文件操作（mcp-filesystem）+ 学习机制（Mem0 + skill + reflection） | 能读写文件，会学习 |
| 5 | 持续 | 日常使用优化，积累skills | 越用越顺手 |

**总周期：4周出可用版本，之后持续迭代。**

---

## 10. 关键设计决策

| 决策 | 选择 | 理由 |
|:---|:---|:---|
| 界面 | CLI | 个人项目，CLI够用，GUI后期再说 |
| 模型管理 | Ollama | Windows一键安装，OpenAI兼容API |
| 网络搜索 | Kimi内置 | 无需额外API key，¥0.025/次 |
| 文件操作 | mcp-filesystem | MCP标准工具，权限可控 |
| 记忆 | Mem0 + SQLite | 零配置，本地存储 |
| 技能格式 | SKILL.md | 兼容OpenClaw生态 |
| 打包 | 无 | Python脚本直接运行，不打包 |
| CI/CD | 无 | 个人项目手动管理 |
