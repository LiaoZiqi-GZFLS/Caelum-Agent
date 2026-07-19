# Caelum-Agent Kimi API 使用手册

> **版本**：2026-07-13
> **范围**：本项目对 Kimi（Moonshot AI）开放平台 API 的全部使用方式——Chat Completions、Formula 内置工具、Files API、Partial Mode、JSON Mode，以及与之配套的容错与配额管理实现。
> **相关文档**：Formula 工具本身的官方说明见 `docs/kimi_api/kimi_tools_guide.agent.final.md`；本文档聚焦**本项目如何用**，不重复官方手册内容。

---

## 目录

1. [架构定位与连接配置](#1-架构定位与连接配置)
2. [Chat Completions：主会话](#2-chat-completions主会话)
3. [多模态输入：base64 与 ms:// 双通道](#3-多模态输入base64-与-ms-双通道)
4. [Formula 内置工具框架](#4-formula-内置工具框架)
5. [Files API：三种 purpose 与生命周期](#5-files-api三种-purpose-与生命周期)
6. [Partial Mode：部分预填充](#6-partial-mode部分预填充)
7. [JSON Mode](#7-json-mode)
8. [本地函数工具（Function Calling）](#8-本地函数工具function-calling)
9. [子代理调用模式](#9-子代理调用模式)
10. [容错：重试、熔断与降级](#10-容错重试熔断与降级)
11. [配额与隐私管理](#11-配额与隐私管理)
12. [配置参考](#12-配置参考)
13. [附录：端点与源码索引](#13-附录端点与源码索引)

---

## 1. 架构定位与连接配置

Kimi K3 是项目的**唯一 LLM 大脑**（设计决策：无 Ollama；本地模型只有 OmniParser YOLOv8 图标检测器和 Florence-2 图标描述器，均为视觉感知用，不参与推理）。

所有 Kimi API 访问集中在 `agent/llm_client.py` 的 `LLMClient`：

```python
# agent/llm_client.py:19-26
class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)
        self.http = httpx.AsyncClient(timeout=120.0)   # Formula / Files API 直连
        self._tools: list[dict[str, Any]] = []
        self._tool_to_uri: dict[str, str] = {}
        self._local_handlers: dict[str, FunctionHandler] = {}
```

两个 HTTP 通道并存：

| 通道 | 客户端 | 用途 |
|------|--------|------|
| OpenAI 兼容端点 | `openai.AsyncOpenAI` | `/chat/completions`（主会话、子代理、判定） |
| REST 直连 | `httpx.AsyncClient`（120s 超时） | Formula `/formulas/...`、Files `/files` |

连接配置（`agent/config.py:15-37`，`config.yaml` 的 `llm:` 段）：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `base_url` | `https://api.moonshot.cn/v1` | OpenAI 兼容根路径；Formula/Files 端点也挂在它下面 |
| `model` | `kimi-k3` | K3 旗舰模型，2.8T 参数，1M 上下文，原生视觉 |
| `reasoning_effort` | `None`（不传） | K3 仅支持 `"max"`——始终开启思考模式 |
| `enable_builtin_tools` | `true` | 是否在启动时拉取 Formula 工具定义 |
| `builtin_tools` | 12 个 URI 列表 | 见 §4 |
| `enable_file_extract` | `true` | 注册 ReadDocument（§5.2） |
| `enable_media_upload` | `true` | 注册 ViewMedia / GenerateImage / CaptureWindow（§5.3） |

`api_key` 只存于 `config.yaml`（gitignored），`Field(repr=False)` 防止日志泄露。

---

## 2. Chat Completions：主会话

### 2.1 `chat()` 方法

```python
# agent/llm_client.py:124-146
async def chat(self, messages, tools=..., response_format=None) -> Any:
    kwargs = {"model": self.config.model, "messages": messages}
    if tools is ...:                    # 默认：带全部已注册工具
        if self._tools:
            kwargs["tools"] = self._tools
    elif tools:                         # 显式列表：照传
        kwargs["tools"] = tools
    # tools=None → 不带 tools 键（子代理/判定调用专用）
    if self.config.reasoning_effort is not None:
        kwargs["reasoning_effort"] = self.config.reasoning_effort
    if response_format is not None:
        kwargs["response_format"] = response_format   # JSON Mode，见 §7
    return await self.client.chat.completions.create(**kwargs)
```

`tools` 参数的三态语义是全项目约定：

| 传值 | 行为 | 使用场景 |
|------|------|----------|
| 省略（`...`） | 自动附带全部已注册工具 | 主 ReAct 循环 |
| 显式列表 | 只带指定工具 | （预留） |
| `None` | **完全不带 tools 键** | DraftContent、GenerateImage、LearningSettler 判定等子代理——防止模型在纯生成任务里意外触发工具 |

### 2.2 消息序列约束

**Kimi 拒绝连续同角色消息（HTTP 400）**。orchestrator 为此做了两处合并：

- 工具结果之后的跟进信息（ViewMedia 的 ms:// 媒体、UpgradeVision 新感知、ZoomRegion 区域视图）全部合并进**一条** user 消息（`agent/orchestrator.py:1514-1550`）；
- `_reflect()` 已经把 assistant 消息追加进 history，主循环不再重复追加（`agent/orchestrator.py:1444-1446` 的注释记录了这条历史 bug）。

### 2.3 主循环的会话形态

ReAct 五阶段（Perceive → Reflect → Think → Act → Verify）里，Think/Act 阶段每轮携带：

1. 完整 `messages` 历史（system prompt + 用户指令 + 交替的 assistant/tool/user 轮次）；
2. 感知结果：环境描述文本 + 截图（§3.1）；
3. 全部已注册工具（MCP 工具 + Formula 工具 + 本地函数工具）。

Verify 阶段是独立的一轮 chat 调用（模型回答 YES/NO 判定任务是否完成），通过后还有一轮 final-answer 调用生成给用户的最终回复。

---

## 3. 多模态输入：base64 与 ms:// 双通道

项目有两条把图片/视频送进模型视野的通道，按来源选用：

### 3.1 base64 data URL —— 屏幕感知

每轮感知的截图以 OpenAI `image_url` content part 内联：

```python
# agent/orchestrator.py:976-983
b64 = base64.b64encode(image_bytes).decode("utf-8")
content.append({
    "type": "image_url",
    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
})
```

- 截图经 PIL 压缩/逆 DPI 归一化（`agent/perception.py`），与 OCR 输入同尺寸；
- 有 SoM 标注时发**双图**：干净截图在前、红框编号标注图在后；
- `UpgradeVision` 后改发原始分辨率图；`ZoomRegion` 发区域裁剪的双图。

**代价**：base64 体积大、每轮重复上传。因此历史归档（`agent/history_archive.py`）会把 base64 替换为占位符（原图保留在 `data/cache/`），归档仅供事后审查，不回读。

### 3.2 ms:// 引用 —— 本地媒体文件

ViewMedia / CaptureWindow / GenerateImage 走的是 Files API 上传 + `ms://<file-id>` 引用（§5.3）。orchestrator 用标记契约把工具结果里的引用提升为真正的媒体 content part：

```python
# agent/media.py:50-55 —— 标记契约
MEDIA_REF_RE = re.compile(r"\[media_ref\] (image|video) (ms://\S+)")

# agent/orchestrator.py:1683-1684 —— 工具结果中的标记被提升
for kind, url in parse_media_refs(str(output.get("content", ""))):
    key = "video_url" if kind == "video" else "image_url"
```

提升后的媒体 part 合并进下一轮 user 消息（受 §2.2 约束），模型看到的就是原生渲染的图片/视频，而不是字节或路径文本。

**两条通道的分工**：屏幕截图生命周期只有一轮对话、量大、容忍压缩 → base64；本地媒体文件可能很大（视频）、需要原生理解 → 上传 + ms://。

---

## 4. Formula 内置工具框架

Formula 是 Kimi 官方内置工具的调用机制。本项目的实现在 `agent/llm_client.py`。

### 4.1 启动时注册

```python
# agent/llm_client.py:28-47
async def initialize(self) -> None:
    if not self.config.enable_builtin_tools:
        return
    for uri in self.config.builtin_tools:
        tools = await self._fetch_formula_tools(uri)   # 单个失败只 warning，不中断
        for tool in tools:
            converted = self._convert_formula_tool(tool)
            ...
            self._tool_to_uri[name] = uri
            self._tools.append(converted)
```

**拉取工具定义**：`GET {base_url}/formulas/{uri}/tools`，返回 OpenAI 兼容的 tools JSON（或 `_plugin` 包裹格式，`_convert_formula_tool` 兼容两者，取 `_plugin.functions[0]`）。

### 4.2 执行：fibers 端点

模型发出的 tool_call 按名字查 `_tool_to_uri`，命中 Formula 的走直连调用：

```python
# agent/llm_client.py:110-122
async def _call_formula(self, uri, name, arguments) -> str:
    url = f"{self.config.base_url}/formulas/{uri}/fibers"
    resp = await self.http.post(url, headers={...},
        json={"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)})
    ctx = resp.json().get("context", {})
    return ctx.get("output") or ctx.get("encrypted_output", "")
```

### 4.3 命名约定与已注册工具

**URI 用连字符，工具名用下划线**：`moonshot/code-runner:latest` 注册为 `code_runner`。`config.yaml.example` 默认注册 12 个：

| Formula URI | 工具名 | 项目内用途 |
|-------------|--------|-----------|
| `moonshot/web-search:latest` | `web_search` | 联网搜索（替代本地搜索实现） |
| `moonshot/fetch:latest` | `fetch` | 网页抓取 |
| `moonshot/quickjs:latest` | `quickjs` | 轻量 JS 执行 |
| `moonshot/code-runner:latest` | `code_runner` | Kimi 侧代码执行（**备选**；默认后端是本地 RestrictedCodeRunner） |
| `moonshot/convert:latest` | `convert` | 格式转换 |
| `moonshot/date:latest` | `date` | 日期时间 |
| `moonshot/base64:latest` | `base64` | 编解码 |
| `moonshot/excel:latest` | `excel` | Excel/CSV 分析 |
| `moonshot/memory:latest` | `memory` | 云端记忆（§4.4） |
| `moonshot/rethink:latest` | `rethink` | 反思整理（§4.4） |
| `moonshot/random-choice:latest` | `random_choice` | 随机选择 |
| `moonshot/mew:latest` | `mew` | — |

### 4.4 memory / rethink 的程序化适配

`memory` 和 `rethink` 不只给模型自主调用，项目代码也直接复用同一条 Formula 执行路径（`agent/kimi_memory.py`）：

```python
class KimiMemoryClient:
    async def set_memory(self, key, value):   # action=save, scope=user
    async def get_memory(self, query):         # action=recall，取首条结果
    async def get_memory_exact(self, key):     # action=recall，仅当 key 精确匹配
    async def rethink(self, task_summary, failure_reason, context=None):
        # thought="Task: ...\nFailure: ...\nContext: ...", action=organize
```

它伪造一个 tool_call 对象（`_make_call`）喂给 `LLMClient.execute_tool_calls`，因此**不引入任何新的 HTTP 路径**。工具未注册时抛 `ToolNotAvailableError`，调用方降级到本地 SQLite：

- `MemoryStore.aset_preference/aget_preference`（`agent/memory.py:137-149`）：云端记忆优先，失败回落本地 `user_preferences` 表；
- `ReflectionEngine.record`（`agent/reflection.py:37-48`）：`use_rethink` 开且工具可用时让 Kimi rethink 整理修复建议，失败回落纯 SQLite 记录。

### 4.5 工具分派优先级

`execute_tool_calls`（`agent/llm_client.py:148-179`）的分派顺序：

1. **Formula 工具**（名字在 `_tool_to_uri`）→ fibers 端点；
2. **本地函数工具**（名字在 `_local_handlers`）→ 直接调用 handler（支持同步/协程）；
3. 都不命中 → 返回 `[error] Tool X is not registered...` 作为 tool 结果（不抛异常，让模型自行换策略）。

单个工具失败格式化为 `[error] ...` 字符串返回，不打断整批 tool_calls。

---

## 5. Files API：三种 purpose 与生命周期

### 5.1 平台特性（必须记住）

- **上传文件无 TTL**：平台永久保留，需主动删除；
- **配额**：1000 文件 / 10GB；
- 端点：`POST /files`（multipart 上传）、`GET /files/{id}/content`（取内容）、`GET /files`（列全部）、`DELETE /files/{id}`；
- `purpose` 决定文件用途与模型可见性：`file-extract` / `image` / `video`。

三种 purpose 对应三种完全不同的生命周期策略：

| purpose | 删除时机 | 本地缓存 | 调用方 |
|---------|---------|---------|--------|
| `file-extract` | 提取完文本**立即删**（best-effort） | sha256 缓存提取文本，永久 | ReadDocument |
| `image` / `video` | **任务结束**才删（对话历史引用期间必须存活） | 压缩产物按 sha256 缓存 | ViewMedia / GenerateImage / CaptureWindow |

### 5.2 file-extract：ReadDocument（`agent/file_reader.py`）

用途：解析本地栈读不了的二进制文档（PDF/DOC/DOCX/PPT/PPTX/EPUB/MOBI/XLS/XLSX——`ALLOWED_EXTENSIONS` 白名单）。文本/代码/日志/CSV **故意排除**，走 filesystem MCP 本地读（零上传、零成本）。

流程：

```
extract(path)
  ├─ sha256(文件) → cache_dir/<sha256>.txt 命中？→ 直接返回（零上传）
  └─ 未命中：
       POST /files (purpose=file-extract)  → file_id
       GET  /files/{file_id}/content       → 提取文本
       DELETE /files/{file_id}             ← finally 里 best-effort
       写本地缓存 → 返回文本
```

工具层的两个关键设计：

- **字符分页**：handler 默认返回 8000 字符/页（上限 16000），尾部附 `[truncated] Call ReadDocument again with offset=N`，防止 200 页 PDF 一次撑爆上下文；
- **`doc:<sha8>` 引用**：返回头里带 `ref doc:xxxxxxxx`，DraftContent 可凭此引用直接从本地缓存取全文，主上下文始终不见正文（§9.1）。

### 5.3 image / video：ViewMedia（`agent/media.py`）

用途：让模型**原生看到**本地图片/视频（而非描述文本）。

上传前的强制约束：

| 约束 | 值 | 实现 |
|------|-----|------|
| 源文件预检 | >300MB 直接拒绝 | 避免在 ffmpeg/Pillow 上浪费分钟级时间 |
| 上传硬上限 | 100MB（API 单文件配额） | 压缩后仍超限才报错 |
| 图片降采样 | >4K（3840×2160）缩进 4K 框 | Pillow `thumbnail`，JPEG q88，sha256 缓存 |
| 视频重编码 | 15fps / ≤1080p / H.264+AAC | ffmpeg（PATH 优先，回退 `imageio-ffmpeg` 自带二进制），sha256 缓存 |

上传成功返回 `(kind, "ms://<file-id>")`，经 §3.2 的标记契约为模型所见。

**生命周期纪律**：ms:// 文件在引用它的对话历史存活期间**绝不能删**（模型后续轮次还要回看），所以 `media.py` 上传后从不主动删，统一靠 sweep（§11.1）。

### 5.4 相关工具注册条件

`enable_media_upload=false` 时 ViewMedia、GenerateImage、CaptureWindow 三个工具**都不注册**（后两者依赖 MediaUploader），本地媒体完全离线。`enable_file_extract=false` 时不注册 ReadDocument。

---

## 6. Partial Mode：部分预填充

Kimi 特有能力：在 messages 末尾放一条带 `"partial": true` 的 assistant 消息，模型会**续写**这段文本而不是新开一轮生成。

```python
# agent/content_writer.py:86-92
if prefill:
    messages.append(
        {"role": "assistant", "content": prefill, "partial": True}
    )
completion = await llm.chat(messages, tools=None)
body = (completion.choices[0].message.content or "").strip()
text = prefill + body   # ← 响应不含 prefill，必须自己拼回
```

特性与坑：

1. **响应不包含 prefill**：`content` 只有续写部分，拼接是调用方的责任；
2. **不能与 `response_format` 同用**：API 拒绝这个组合（`llm_client.py:142-145` 注释）；
3. 典型用途：固定标题/称呼开头、续写已有文本、强制输出格式骨架。

项目内唯一使用者是 DraftContent（§9.1）的 `prefill` 参数。

---

## 7. JSON Mode

```python
completion = await llm.chat(
    messages, tools=None,
    response_format={"type": "json_object"},   # 强制输出合法 JSON
)
```

- 强制模型输出可解析的 JSON 对象；
- 项目内用于 GenerateImage 的视觉自评（`agent/image_gen.py:135-147`，输出 `{"ok": bool, "issues": str}`）；
- 调用方仍需防御性解析：`json.loads` 失败时按拒绝处理并把原文当反馈（`image_gen.py:149-154`）——JSON Mode 保证语法合法，不保证字段语义；
- **与 Partial Mode 互斥**（§6）。

LearningSettler 的完成度判定（`agent/pending_learning.py`）**没有用** JSON Mode，而是纯文本约定 + `_parse_verdict` 容错解析（去代码围栏、截取首个 `{...}` 段、校验 `completed` 键），因为判定消息里混有 reasoning 文本时 JSON Mode 反而不必要——这是两种风格的并存示例：纯结构化输出用 JSON Mode，带推理的判定用容错解析。

---

## 8. 本地函数工具（Function Calling）

除 Formula 外，项目以 OpenAI 标准 Function Calling 注册本地可执行工具：

```python
# agent/llm_client.py:78-98
def register_local_function(self, name, handler, schema, description):
    self._local_handlers[name] = handler
    self.register_function_tools([{
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }])
```

- handler 可为同步函数或协程（`execute_tool_calls` 用 `asyncio.iscoroutinefunction` 分派）；
- 异常一律捕获为 `[error] ...` 工具结果，不打断 ReAct 循环；
- MCP 工具经 `register_function_tools` 批量注册（无本地 handler，由 orchestrator 的 `_execute_tool_calls` 按 `server__tool` 命名截胡转发给 MCP multiplexer）。

当前注册的本地工具（orchestrator 构造时完成，`agent/orchestrator.py:305-363`）：

| 工具 | 模块 | 与 Kimi API 的关系 |
|------|------|-------------------|
| `CodeRunner` | `tools.py` | 纯本地沙箱，**不调 API**（默认代码执行后端） |
| `DesktopInteract` / `ZoomRegion` / `NearbyLabels` / `PreviewPoints` / `UpgradeVision` | `orchestrator.py` | 纯本地视觉交互 |
| `CompleteTask` / `UpdateTaskList` / `RequestHumanHelp` | `orchestrator.py` / `task_list.py` | 纯本地 |
| `ReadDocument` | `file_reader.py` | Files API file-extract（§5.2） |
| `ViewMedia` | `media.py` | Files API image/video（§5.3） |
| `DraftContent` | `content_writer.py` | chat 子代理 + Partial Mode（§9.1） |
| `GenerateImage` | `image_gen.py` | chat 子代理 + JSON Mode + 上传自评（§9.2） |
| `CaptureWindow` | `window_capture.py` | 本地截图 + ms:// 上传 |
| `SelfWindow` / `FocusGuard` | `self_window.py` / `focus_guard.py` | 纯本地窗口管理 |

---

## 9. 子代理调用模式

项目反复使用"独立消息上下文 + `tools=None`"的子代理模式，让生成类任务不污染主 ReAct 上下文。

### 9.1 DraftContent：写作子代理（`agent/content_writer.py`）

```
主循环 tool_call: DraftContent(task, persona, prefill?, max_chars?, doc_ref?)
  └─ 全新 messages: [system=persona, system=参考文档(可选), user=task, assistant(prefill, partial)?]
       └─ llm.chat(tools=None) → 写 data/cache/drafts/<slug>-<hash8>.md
            └─ 返回: 路径 + 字数 + 200 字预览（全文不进主上下文）
```

- **persona 隔离**：写作者的 system prompt 不混入 UI 操作员的 system prompt；
- **`doc_ref` 链路**：`doc:<sha8>` → `FileExtractor.read_by_ref` 从本地缓存取全文（上限 60000 字符注入子代理），主上下文零负担；
- Kimi file-chat 惯例：文档内容作为**独立 system 消息**插在 persona 和 task 之间；
- 结果里附 PowerShell 剪贴板提示（`Get-Content ... | Set-Clipboard`），避免模型把全文打回编辑器。

### 9.2 GenerateImage：生成-自评闭环（`agent/image_gen.py`）

```
requirement
  └─ 循环 ≤ 5 轮：
       1. llm.chat(tools=None) → 提取 <svg>...</svg>
       2. CairoSVG 渲染 PNG（按内容哈希命名，自动去重）
       3. MediaUploader 上传 PNG → ms://
       4. llm.chat(image_url=ms://..., response_format=json_object) → {"ok", "issues"}
       5. ok → 返回；否则 issues 拼进下一轮 user 消息重试
```

- 评审轮是**带图 + JSON Mode** 的独立单轮调用（不复用生成上下文）；
- 5 轮预算耗尽如实上报 `NOT verified`，不隐瞒失败；
- 无 SVG / 渲染失败也算一轮，反馈文本直接告诉模型修什么。

### 9.3 LearningSettler：中断任务清算判定（`agent/pending_learning.py`）

启动时后台对每条中断记录做一次 `llm.chat(tools=None)` 判定：

- system 消息要求只回 `{"completed": true|false, "summary", "lesson"}`；
- user 消息含指令、中断原因（kill switch / API 熔断）、截断到 4000 字符的轨迹；
- `_parse_verdict` 容错解析（§7 末）；判定失败跨启动重试，3 次封顶后兜底反思删除。

### 9.4 SkillLearner：技能生成（`agent/skills.py`）

任务成功后（以及清算判定为"已完成"时）用 LLM 从轨迹生成/合并 `SKILL.md`；LLM 不可用时降级为确定性模板，保证学习链路永不中断。

---

## 10. 容错：重试、熔断与降级

### 10.1 三层重试机制

| 层 | 机制 | 可见性 |
|----|------|--------|
| openai SDK 内部 | 默认 `max_retries=2`，指数退避（~0.5s→~1s，带抖动），仅对 408/409/429/5xx/连接错误/超时，尊重 `Retry-After` | SDK INFO 日志行 |
| orchestrator 外层 | `TransientAPIError` → 记反思 → `continue` **立即重试，无间隔** | WARNING 日志 `LLM call failed (N/5); retrying: ...` |
| 熔断 | 连续 5 次（`kill_switch.api_failure_threshold`）→ `WAITING_HUMAN` + 返回熔断消息 | spinner 变 "Waiting for input…" + 最终答案 panel |

实现要点（`agent/orchestrator.py:1029-1062`）：

- 计数器 `consecutive_api_failures`：**任何异常类型都计数**（`openai.APIError`/`httpx.HTTPError`/`asyncio.TimeoutError` 是显式分支，其余 Exception 走兜底分支同样计数）；一次成功立即清零；
- 计数随状态持久化（`agent/orchestrator.py:424-447`），跨任务恢复不丢；
- 未达阈值的失败抛 `TransientAPIError` 让循环继续，第 5 次才抛 `APIBreakerTripped`；
- 熔断退出前把已有轨迹写入 `pending_learning` 表（§9.3 的清算来源）。

### 10.2 与动作熔断的区别

工具/动作执行失败走**另一条线**：`consecutive_action_failures` ≥ `action_failure_threshold`（默认 3）→ "waiting for human guidance"，不计入 API 熔断。连续 2 次 UI 动作失败还会向模型注入"换定位策略，别再重试"的升级提示（`agent/orchestrator.py:1580-1589`）。

### 10.3 子代理的容错风格

子代理调用**不经过熔断器**（直接 `llm.chat`）：失败就记 warning + 降级返回（DraftContent 返回 `[error]`，settler 跨启动重试，reflection 回落 SQLite，skills 回落模板）。子代理是增强链路，绝不能拖垮主循环。

---

## 11. 配额与隐私管理

### 11.1 远端清扫（sweep）

因为 Files API 无 TTL（§5.1），两处定时清扫，均 fire-and-forget、绝不抛异常：

```python
# agent/orchestrator.py:328-338（启动时）与 :1114-1122（任务结束时）
for sweeper in (self.file_extractor, self.media_uploader):
    if sweeper is None:
        continue
    sweep_task = asyncio.create_task(sweeper.sweep_remote())
```

- `FileExtractor.sweep_remote`：列 `/files`，删全部 `purpose=file-extract`（提取文本已 sha256 缓存，远端是废物）；
- `MediaUploader.sweep_remote`：删全部 `purpose=image/video`（ms:// 引用只活一个任务）。

### 11.2 隐私边界

| 数据 | 去向 | 控制开关 |
|------|------|---------|
| 屏幕截图（含 OCR 文本） | 每轮 chat 内联 base64 | 无（核心功能） |
| 二进制文档内容 | file-extract 上传（提取后即删） | `enable_file_extract: false` |
| 本地图片/视频 | image/video 上传（任务结束清扫） | `enable_media_upload: false` |
| 用户偏好记忆 | Kimi memory 云端（scope=user） | `memory.use_kimi_memory: false` |
| 反思内容 | Kimi rethink | `reflection.use_rethink: false` |

### 11.3 历史归档脱敏

`agent/history_archive.py` 每个任务写一份 JSONL 飞行记录（`data/archives/<timestamp>-<taskid>.jsonl`）：base64 截图替换为占位符，敏感工具参数按键名脱敏。归档**永不回读**，仅供事后审查。

---

## 12. 配置参考

```yaml
# config.yaml（gitignored）—— llm 段全量
llm:
  provider: kimi
  base_url: https://api.moonshot.cn/v1
  api_key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  model: kimi-k3
  # reasoning_effort: max     # K3 仅支持 max，始终开启思考
  enable_builtin_tools: true
  enable_file_extract: true       # ReadDocument（§5.2）
  enable_media_upload: true       # ViewMedia/GenerateImage/CaptureWindow（§5.3）
  builtin_tools:
    - moonshot/web-search:latest
    - moonshot/fetch:latest
    - moonshot/quickjs:latest
    - moonshot/code-runner:latest   # 备选；默认用本地 RestrictedCodeRunner
    - moonshot/convert:latest
    - moonshot/date:latest
    - moonshot/base64:latest
    - moonshot/excel:latest
    - moonshot/memory:latest
    - moonshot/rethink:latest
    - moonshot/random-choice:latest
    - moonshot/mew:latest

memory:
  use_kimi_memory: true     # 偏好记忆走 Kimi memory Formula（§4.4）

reflection:
  use_rethink: true         # 反思修复建议走 Kimi rethink Formula（§4.4）

kill_switch:
  api_failure_threshold: 5      # API 熔断阈值（§10.1）
  action_failure_threshold: 3   # 动作熔断阈值（§10.2）
```

---

## 13. 附录：端点与源码索引

### 端点清单

| 方法 | 路径 | 用途 | 调用处 |
|------|------|------|--------|
| POST | `/chat/completions` | 主会话/子代理/判定（OpenAI SDK） | `llm_client.py:146` |
| GET | `/formulas/{uri}/tools` | 拉取 Formula 工具定义 | `llm_client.py:104-108` |
| POST | `/formulas/{uri}/fibers` | 执行 Formula 工具 | `llm_client.py:110-122` |
| POST | `/files` | 上传文件（multipart，purpose 字段） | `file_reader.py:164-174`、`media.py:190-200` |
| GET | `/files/{id}/content` | 取 file-extract 提取文本 | `file_reader.py:176-180` |
| GET | `/files` | 列全部文件（sweep 用） | `file_reader.py:139`、`media.py:211` |
| DELETE | `/files/{id}` | 删除文件 | 两个 sweep + extract 后清理 |

### 源码文件索引

| 文件 | 职责 |
|------|------|
| `agent/llm_client.py` | LLMClient：chat、Formula 注册/执行、本地工具分派 |
| `agent/kimi_memory.py` | memory/rethink Formula 的程序化适配 |
| `agent/file_reader.py` | Files API file-extract + ReadDocument 工具 |
| `agent/media.py` | Files API image/video + ViewMedia 工具 + ms:// 标记契约 |
| `agent/content_writer.py` | DraftContent 子代理 + Partial Mode |
| `agent/image_gen.py` | GenerateImage 子代理 + JSON Mode 视觉自评 |
| `agent/pending_learning.py` | LearningSettler 判定调用 |
| `agent/skills.py` | SkillLearner 技能生成调用 |
| `agent/reflection.py` | rethink 反思集成 |
| `agent/memory.py` | 偏好记忆的云端/本地双写 |
| `agent/orchestrator.py` | 熔断器、ms:// 提升、消息合并、工具注册接线、sweep 调度 |
| `agent/history_archive.py` | 归档脱敏 |
| `agent/config.py` | LLMConfig / MemoryConfig / ReflectionConfig / KillSwitchConfig |

---

*本文档随实现演进；与代码冲突时以代码为准（行号引用基于 2026-07-13 的 main 分支）。*
