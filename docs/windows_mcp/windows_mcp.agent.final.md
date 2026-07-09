# Windows-MCP 技术调研报告

> **调研日期**：2026年7月9日  
> **调研对象**：[CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) v0.8.2  
> **报告性质**：开源 MCP 服务器技术评估与工具参数参考手册  
> **目标读者**：Windows 平台 AI Agent 开发者、技术架构师

---

## 1. 项目概述与安装方式

### 1.1 项目背景与定位

#### 1.1.1 核心定位：Windows 操作系统的 MCP 桥接器

Windows-MCP 是由 CursorTouch 组织维护的开源项目，其 GitHub 仓库位于 `CursorTouch/Windows-MCP`，截至 2026 年 7 月已获得 6.4k Star 和 770 个 Fork，贡献者人数达 36 人[^4^]。该项目在 Claude Desktop 扩展市场拥有超过 200 万用户[^64^]，是 Windows 平台上用户规模最为广泛的 MCP（Model Context Protocol，模型上下文协议）服务器实现之一。

从架构定位来看，Windows-MCP 充当 LLM Agent 与 Windows 操作系统之间的桥接层（bridge）。它通过暴露一组标准化的 MCP 工具接口，使任何支持 MCP 协议的客户端——包括 Claude Desktop、Claude Code、Codex CLI、Gemini CLI、Perplexity Desktop 等——能够驱动 Windows 桌面环境完成文件导航、应用控制、UI 交互、QA 测试等操作[^4^]。与同类工具相比，其核心差异化在于完全基于 Microsoft UI Automation（UIA）可访问性树进行元素定位，而非依赖计算机视觉模型对屏幕截图进行解析[^50^]。这意味着即使使用纯文本模型（如 GPT-4o-mini 或 Claude Sonnet），Agent 也能够通过元素 ID 和角色信息精确操作 UI，视觉能力仅在需要时作为辅助手段启用。

#### 1.1.2 技术特点：纯 UIA 架构与轻量级设计

Windows-MCP 的技术设计遵循三个核心原则。第一，纯 UIA 架构：项目通过 `comtypes` 库调用 Microsoft UI Automation API，直接读取 Windows 可访问性树（accessibility tree）获取 UI 元素的名称、角色、边界矩形和控件类型等信息，不依赖 OmniParser 或 UI-TARS 等需要视觉骨干模型（vision backbone）的方案[^50^]。这种设计将典型操作延迟控制在 0.2–0.5 秒之间（两次鼠标点击之间）[^4^]，且对系统资源的占用显著低于基于截图-解析的替代方案。

第二，协议与模型无关性：项目实现遵循 MCP 规范，传输层支持 stdio（默认）、SSE（Server-Sent Events）和 Streamable HTTP 三种模式[^4^]。在模型兼容性方面，任何支持工具调用的 LLM 均可驱动 Windows-MCP，视觉能力为可选而非必需。

第三，开源与轻量：项目以 MIT 许可证发布，100% 采用 Python 编写，代码总行数控制在轻量级范围内，依赖管理通过 `pyproject.toml` 和 `uv.lock` 文件严格锁定[^4^]。项目还提供了 DOM 模式（`use_dom=True`），在浏览器自动化场景中能够过滤掉浏览器壳层 UI，仅操作网页内容，目前支持 Chrome、Edge 和 Firefox[^4^]。

### 1.2 安装方式详解

#### 1.2.1 UV 包管理器安装：推荐方案及其技术原理

Windows-MCP 的官方文档明确推荐使用 Astral 公司开发的 `uv` 包管理器进行安装。`uv` 是一个用 Rust 编写的高性能 Python 包管理工具，其 `uvx` 子命令支持从 PyPI 直接运行包而无需显式安装到全局环境。项目的标准启动命令为：

```bash
uvx windows-mcp serve
```

对于需要网络访问的场景，可启用 SSE 或 Streamable HTTP 传输：

```bash
uvx windows-mcp serve --transport sse --host localhost --port 8000
```

关于开发者普遍关心的两个问题，需要明确回答。第一，**是否可以直接使用 `pip install` 安装？** 技术上可行——`windows-mcp` 包已完整发布至 PyPI[^64^]，`pip install windows-mcp` 命令可以成功执行。然而，项目使用 `uv.lock` 锁定依赖版本，且 `pyproject.toml` 中配置了 `tool.uv` 专属段（如 `constraint-dependencies` 和 `extra-build-dependencies`）[^4^]，使用 pip 安装将绕过这些锁定机制，可能引入未经验证的依赖版本。因此官方文档仅提供 UV 安装指引，`pip` 方式虽可行但不被推荐。

第二，**`python -m windows_mcp_server` 是否可行？** 不可行。项目的 Python 模块名为 `windows_mcp`（下划线分隔，无 `_server` 后缀），通过 `src/windows_mcp/__main__.py` 和 `pyproject.toml` 中的 `[project.scripts]` 段（`windows-mcp = "windows_mcp.__main__:main"`）同时支持两种调用方式：`python -m windows_mcp` 或直接执行 `windows-mcp` 命令[^4^]。推荐的入口为 `windows-mcp serve`，`python -m windows_mcp` 可作为后备方案。

下表对比了 UV 与 pip 两种安装方式的技术差异：

| 对比维度 | UV（推荐） | pip（技术上可行） |
|:---------|:-----------|:-----------------|
| 安装命令 | `uvx windows-mcp serve` | `pip install windows-mcp` |
| 依赖锁定 | 严格遵循 `uv.lock`，确定性安装 | 使用 `requirements` 解析，版本浮动 |
| 环境隔离 | `uvx` 自动创建临时虚拟环境 | 需手动管理 venv 或全局安装 |
| 启动速度 | 首次后缓存加速，毫秒级解析 | 依赖 pip 缓存机制 |
| 项目专属配置 | 支持 `tool.uv` 段（约束依赖、额外构建依赖）[^4^] | 忽略 `tool.uv` 配置 |
| 官方支持状态 | 文档明确推荐，CI/CD 流程验证 | 未在官方文档中提及 |

**分析**：从上表可以看出，UV 方案的核心优势在于确定性依赖管理和环境隔离。对于生产环境或团队标准化部署，UV 能够消除"在我机器上可以运行"的依赖漂移问题；pip 方案更适合临时测试或无法安装 UV 的受限环境，但需自行承担依赖版本冲突的风险。

#### 1.2.2 各 MCP 客户端配置方式

Windows-MCP 作为标准 MCP 服务器，可与多种客户端对接。下表汇总了主要客户端的配置方式：

| MCP 客户端 | 配置文件路径 | 配置方式 | 备注 |
|:-----------|:------------|:---------|:-----|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | JSON 配置：`command` 为 `uvx`，`args` 为 `["windows-mcp", "serve"]` | MSIX 版本需使用绝对路径[^4^] |
| Claude Code | 命令行注册 | `claude mcp add --transport stdio windows-mcp -- uvx windows-mcp serve` | WSL 环境需通过 `powershell.exe` 桥接[^61^] |
| Perplexity Desktop | Settings → Connectors → Add Connector → Advanced | 粘贴 `command` + `args` JSON | 重启后生效[^4^] |
| Gemini CLI | `%USERPROFILE%\.gemini\settings.json` | 在 `mcpServers` 键下添加配置 | 2025 年 Google 发布[^4^] |
| Codex CLI | `%USERPROFILE%\.codex\config.toml` | TOML 格式：`[mcp_servers.windows-mcp]` 段 | OpenAI 官方 CLI 工具[^4^] |
| Qwen Code | `%USERPROFILE%\.qwen\settings.json` | JSON 格式，与 Gemini CLI 类似 | 阿里巴巴通义千问团队发布[^4^] |

**分析**：上述客户端均支持 stdio 传输模式作为默认对接方式，配置结构遵循 MCP 规范的标准格式。Claude Desktop 和 Claude Code 是用户基数最大的两个入口，其中 Claude Code 的命令行注册方式（`claude mcp add`）最为简洁，且支持 `--scope user` 参数跨项目共享配置。对于从 WSL 运行 Claude Code 的用户，必须明确指定 `powershell.exe` 作为命令桥接，因为 Windows-MCP 依赖原生 Windows API，无法在 Linux 子系统内直接执行[^61^]。

#### 1.2.3 Windows 开机自启与后台服务模式

Windows-MCP 提供了原生的后台服务模式，通过以下命令安装：

```bash
windows-mcp install
```

该命令利用 Windows Scheduled Task（计划任务）机制创建一个名为 `windows-mcp-server` 的用户级任务，同时生成包装脚本 `~/.windows-mcp/start-server.cmd`，确保服务器在每次用户登录时自动启动[^4^]。日志输出分别写入 `~/.windows-mcp/server.log` 和 `~/.windows-mcp/server.error.log`，便于排障。卸载命令为 `windows-mcp uninstall`，将清理计划任务和包装脚本。该机制的设计避免了将服务器注册为系统服务的复杂性，以最小权限原则在用户会话上下文中运行，既满足了持久化需求，又降低了安全风险。

### 1.3 版本与兼容性

#### 1.3.1 支持的系统与 Python 版本

Windows-MCP 对 Windows 版本的支持范围较广，覆盖 Windows 7、Windows 8/8.1、Windows 10 和 Windows 11[^4^]。这一兼容性得益于 UI Automation API 自 Windows 7 起即作为系统组件内置，无需额外安装运行时。

Python 版本方面，项目历史上要求 Python 3.13+，2026 年 7 月的最新 commit 将 `requires-python` 从 `>=3.13` 下调至 `>=3.12`[^4^]。`pyproject.toml` 中的 `tool.ruff.target-version` 仍保持 `py313` 配置，表明代码使用了 Python 3.13 的部分语法特性，但在 3.12 环境下经过兼容性验证。这一调整扩大了可部署范围，使更多尚未升级至 3.13 的企业环境能够直接采用。对于通过 PyPI 安装的用户，当前页面仍显示 "Requires: Python >=3.13"[^64^]，预计将在下一版本更新后同步修正。

WSL（Windows Subsystem for Linux）环境需要特别注意：由于 Windows-MCP 依赖原生 Windows UI Automation COM 接口，服务器进程必须在 Windows 侧执行。WSL 用户需要通过 `powershell.exe` 命令桥接调用 `uvx windows-mcp serve`，确保 MCP 服务器运行在 Windows 主机而非 Linux 容器中[^61^]。

#### 1.3.2 版本发布节奏与当前状态

Windows-MCP 的最新正式发布版本为 v0.8.2，于 2026 年 6 月 9 日发布[^4^]。截至 2026 年 7 月，项目已累计 14 个标签版本和 658 次提交，开发活动保持活跃[^4^]。版本号采用语义化版本控制（SemVer），`0.x` 的主版本号表明项目仍处于 Beta 阶段（`Development Status :: 4 - Beta`）[^64^]。

从版本迭代频率来看，项目在过去半年中保持了平均每月 1–2 个小版本的发布节奏，主要更新方向包括：新增工具（如剪贴板、进程、系统信息、通知等远程管理工具）、安全加固（如 2026 年 5 月修复的 CORS 和 DNS rebinding 漏洞 GHSA-vrxg-gm77-7q5g[^50^]）、以及浏览器 DOM 模式的完善。对于企业用户，建议在评估时关注 SECURITY.md 中的漏洞披露策略和版本支持周期。

---

## 2. 架构设计与传输方式

### 2.1 传输协议支持

#### 2.1.1 三种传输方式对比

Windows-MCP 基于 fastmcp 框架（版本 $\geq$ 3.0）[^1^] 构建，支持三种传输层（Transport Layer），分别对应不同的部署场景与通信模型。fastmcp 是 PrefectHQ 维护的 Pythonic MCP 服务器开发框架，封装了协议握手、消息序列化与传输层适配，开发者只需关注工具实现[^2^]。

**stdio** 为标准输入输出传输，是本地 MCP 客户端（如 Claude Desktop、Cursor）的默认连接方式。该模式下，MCP 服务器作为子进程由客户端启动，通过 stdin/stdout 交换 JSON-RPC 消息，不占用网络端口，适用于单机个人助手场景。

**sse**（Server-Sent Events）通过网络提供 MCP 服务，服务器以 HTTP 长连接方式单向推送事件流。该模式适合局域网内多客户端共享访问，但会话状态由连接维持，服务器重启后客户端需重新握手。

**streamable-http** 是推荐的网络传输方式，基于 HTTP 流实现双向通信。相较于 SSE，它支持 `Mcp-Session-Id` 会话状态管理，并可通过 `--stateless-http` 参数切换为无状态模式，使重连客户端在服务器重启后无需重新握手，同时支持水平扩展部署[^3^]。

三种传输方式在适用场景、安全需求与性能特征上的差异汇总于表 1。

**表 1　传输方式对比**

| 维度 | stdio（本地默认） | sse（网络访问） | streamable-http（生产环境） |
|:---|:---|:---|:---|
| 启动命令 | `serve --transport stdio` | `serve --transport sse --host HOST --port PORT` | `serve --transport streamable-http --host HOST --port PORT` |
| 通信模型 | 标准输入输出管道 | HTTP 长连接（单向事件流） | HTTP 流（双向） |
| 适用场景 | 单机 MCP 客户端直连 | 局域网多客户端共享 | 远程/分布式生产部署 |
| 会话状态 | 进程生命周期绑定 | 连接维持 | 支持有状态/无状态切换 |
| 认证方式 | 无需认证（本地进程） | `Authorization: Bearer` 或 OAuth 2.0 | `Authorization: Bearer` 或 OAuth 2.0 |
| TLS/HTTPS | 不适用 | `--ssl-certfile` + `--ssl-keyfile` | `--ssl-certfile` + `--ssl-keyfile` |
| 水平扩展 | 不支持 | 不支持 | 支持（`--stateless-http`） |

从表 1 可以看出，stdio 模式在本地开发环境中最为简便，无需网络配置即可即开即用；sse 模式填补了局域网共享的空白，但受限于连接态设计；streamable-http 则通过无状态选项与完整的 OAuth 2.0 支持，成为面向生产环境的唯一推荐方案。实际部署时，若客户端与服务器位于同一主机，stdio 的零配置优势不可替代；若需跨网络访问，则应优先选择 streamable-http，并配合 TLS 与认证机制使用。

#### 2.1.2 远程访问安全配置

当启用 sse 或 streamable-http 传输时，Windows-MCP 暴露多层安全控制。若绑定地址为非回环地址且未配置认证，服务器将拒绝启动并提示安全错误[^3^]。

**认证（Authentication）** 通过 `--auth-key` 参数启用静态 Bearer Token 验证，客户端需在请求头中携带 `Authorization: Bearer <token>`。该方式适用于预共享密钥场景，配置简单但缺乏细粒度权限控制。

**IP 白名单** 通过 `--ip-allowlist` 参数接受 CIDR 格式的 IP 段列表（如 `203.0.113.0/24,198.51.100.5`），默认拒绝私有地址与回环地址的连接请求，有效降低未授权内网扫描风险。

**TLS/HTTPS** 通过 `--ssl-certfile` 与 `--ssl-keyfile` 参数加载证书和私钥文件，对传输通道进行加密。项目支持通过 `mkcert` 工具自动生成受系统信任的本地证书，或回退至 `openssl` 生成自签名证书[^3^]。

**OAuth 2.0 + PKCE** 为需要用户级授权的场景提供标准化认证流程。服务器暴露 RFC 8414 元数据端点（`/.well-known/oauth-authorization-server`）、授权端点（`/oauth/authorize`，强制 S256 PKCE）与令牌交换端点（`/oauth/token`），动态客户端注册被显式禁用，重定向 URI 仅限回环地址。OAuth 与静态 auth-key 可并存，两者均被接受为有效的 Bearer Token[^3^]。

**CORS 控制** 默认不发送任何 CORS 响应头，浏览器依据同源策略自动阻止跨域请求。若需浏览器端 MCP 客户端访问，须通过 `--cors-origins` 显式声明允许的源地址。此外，服务器自动对回环绑定启用 Host Header 验证，防御 DNS 重绑定攻击[^3^]。

**工具级访问控制** 通过 `--tools`（白名单）与 `--exclude-tools`（黑名单）参数，可在服务器启动时精确控制暴露的工具集合。例如，禁用 PowerShell 与 Registry 工具可降低高权限操作的风险面[^3^]。上述安全参数均可通过 `~/.windows-mcp/config.toml` 配置文件持久化，CLI 参数优先级高于配置文件。

### 2.2 项目模块结构

#### 2.2.1 核心模块架构

Windows-MCP 的源码位于 `src/windows_mcp/` 目录下，采用按功能域划分的模块化设计。整体架构可分为四层：入口与配置层、核心能力层、工具定义层和基础设施层。

**入口与配置层** 包含 `__main__.py` 与 `config.py`、`paths.py`。`__main__.py` 实现基于 Click 的命令行接口（CLI），定义 `serve`、`install`、`uninstall`、`auth` 四个子命令，并通过 `_build_mcp()` 函数完成 FastMCP 服务器实例的构建与生命周期管理[^4^]。`config.py` 处理调试模式与环境变量配置，`paths.py` 负责 MSIX 沙箱环境下的路径解析。

**核心能力层** 由多个功能子包构成，直接对接 Windows 系统 API。`desktop/` 模块封装桌面 UI 自动化核心逻辑，管理窗口状态、屏幕尺寸与 UI 元素交互；`filesystem/` 提供文件读写、复制、移动、删除等操作；`uia/` 是对 Microsoft UI Automation（UIA）框架的 Python 封装，负责低层次的可访问性树遍历与元素属性提取；`tree/` 基于 `uia/` 构建 UI 树提取逻辑，将原始可访问性数据转换为结构化描述；`powershell/` 与 `process/` 分别提供 PowerShell 命令执行与进程管理（列出、终止）能力；`registry/` 封装 Windows 注册表的读写操作；`vdm/` 处理虚拟桌面管理；`watchdog/` 实现 UI 元素的异步监控与焦点变更回调[^5^]。

**工具定义层** 位于 `tools/` 目录，包含 11 个工具模块与 1 个集中注册文件。各模块按功能命名：`app.py`（应用启动与窗口管理）、`clipboard.py`（剪贴板读写）、`filesystem.py`（文件系统工具）、`input.py`（鼠标点击、键盘输入、等待）、`multi.py`（批量选择与编辑）、`notification.py`（系统通知）、`process.py`（进程管理）、`registry.py`（注册表操作）、`scrape.py`（网页抓取）、`shell.py`（PowerShell 执行）、`snapshot.py`（截图与桌面状态捕获）[^5^]。

**基础设施层** 位于 `infrastructure/` 目录，包含遥测（PostHog 匿名统计）、认证中间件（AuthKeyMiddleware、IPAllowlistMiddleware、OAuthOnlyMiddleware）与配置加载逻辑。`notifications/` 子包提供 Windows Toast 通知的独立实现[^5^]。

服务器生命周期通过 `lifespan` 异步上下文管理器管理：启动时依次初始化遥测分析器（`PostHogAnalytics`）、桌面自动化引擎（`Desktop`）与 UI 监控服务（`WatchDog`），并将焦点变更回调注册到 UI 树模块；关闭时依次停止监控服务与分析器[^4^]。

#### 2.2.2 工具注册与发现机制

Windows-MCP 采用 fastmcp 框架的装饰器模式注册工具。每个工具模块暴露 `register(mcp, *, get_desktop, get_analytics)` 函数，在函数内部通过 `@mcp.tool()` 装饰器将工具函数绑定到 FastMCP 实例[^6^]。以 `snapshot.py` 为例，`register()` 函数内定义了 `_state_tool` 与 `_screenshot_tool` 两个内部函数，分别通过 `@mcp.tool(name='Snapshot', ...)` 与 `@mcp.tool(name='Screenshot', ...)` 完成注册，同时叠加 `@with_analytics` 装饰器实现调用埋点[^7^]。

`tools/__init__.py` 作为集中注册入口，维护 `_MODULES` 列表，按序引用全部 11 个工具模块。`register_all()` 函数遍历该列表，依次调用各模块的 `register()` 函数，并将 `get_desktop` 与 `get_analytics` 两个零参数工厂函数传递给每个模块，以支持延迟初始化[^6^]。这种设计使得工具模块无需在导入时依赖已初始化的单例对象，服务器可在 `lifespan` 启动前完成全部工具注册，而在实际调用时才解析桌面实例与分析器实例。

```
src/windows_mcp/
├── __init__.py              # 包初始化
├── __main__.py              # CLI 入口与服务器构建
├── config.py                # 配置管理
├── paths.py                 # 路径管理（MSIX 检测）
├── desktop/                 # 桌面 UI 自动化核心
├── filesystem/              # 文件系统操作
├── infrastructure/          # 基础设施（遥测、认证中间件）
├── notifications/           # 通知系统
├── powershell/              # PowerShell 执行
├── process/                 # 进程管理
├── registry/                # 注册表操作
├── tools/                   # MCP 工具注册与定义
│   ├── __init__.py          # 集中注册（register_all）
│   ├── _snapshot_helpers.py # 截图辅助函数
│   ├── app.py               # App 工具
│   ├── clipboard.py         # 剪贴板工具
│   ├── filesystem.py        # 文件系统工具
│   ├── input.py             # 输入工具
│   ├── multi.py             # 批量操作工具
│   ├── notification.py      # 通知工具
│   ├── process.py           # 进程工具
│   ├── registry.py          # 注册表工具
│   ├── scrape.py            # 网页抓取工具
│   ├── shell.py             # PowerShell 工具
│   └── snapshot.py          # 截图/快照工具
├── tree/                    # UI 树提取
├── uia/                     # UI Automation 底层封装
├── vdm/                     # 虚拟桌面管理
└── watchdog/                # UI 元素监控
```

上述模块结构体现了"关注点分离"的设计原则：底层系统交互（`uia/`、`desktop/`）与高层工具定义（`tools/`）解耦，工具注册机制（`tools/__init__.py`）与工具实现（各 `*.py`）分离，基础设施（`infrastructure/`）与业务逻辑正交。对于需要扩展新工具的开发者，只需在 `tools/` 下新建模块并实现 `register()` 函数，再将模块导入 `_MODULES` 列表即可完成集成，无需修改服务器核心代码。

---

## 3. 工具列表与参数格式详解

Windows-MCP 共暴露 19 个工具，按功能划分为六个工具组：UI 交互、键盘与等待、状态捕获、系统与应用管理、网络与辅助，以及批量操作。每个工具均携带 MCP 注解（annotations）——`readOnlyHint`、`destructiveHint`、`idempotentHint` 与 `openWorldHint`——用于向 LLM 客户端声明工具的行为特征。其中，`readOnlyHint=True` 表示只读操作，`destructiveHint=True` 表示会修改系统状态，`idempotentHint=True` 表示幂等操作（多次执行效果相同），`openWorldHint=True` 表示操作可能影响外部世界[^1^]。本章逐一解析全部 19 个工具的参数定义、取值约束与典型使用场景，所有参数描述均源自 GitHub 仓库 `CursorTouch/Windows-MCP` 的源码[^2^]。

### 3.1 UI 交互工具组

UI 交互工具组是 Windows-MCP 的核心工具集，包含 Click、Type、Scroll 和 Move 四个工具，覆盖鼠标点击、文本输入、页面滚动和光标移动四种基本桌面操作。这四个工具共享同一套坐标定位机制——通过 `loc` 参数传入屏幕坐标 `[x, y]`，或通过 `label` 参数引用 Snapshot 工具返回的 UI 元素标签 ID。

#### 3.1.1 Click 工具

Click 工具执行鼠标点击操作，支持坐标与标签双模式定位以及多种点击行为配置。其参数定义如下表所示。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `loc` | `list[int] \| str \| None` | `None` | 屏幕坐标 `[x, y]`，或 JSON 字符串 `"[x,y]"` |
| `label` | `int \| None` | `None` | UI 元素标签 ID，由 Snapshot 工具返回 |
| `button` | `Literal["left","right","middle"]` | `"left"` | 鼠标按钮类型：`left` 用于选择/激活，`right` 用于上下文菜单，`middle` 用于中键点击 |
| `clicks` | `int` | `1` | 点击次数：`0` = 仅悬停（不点击），`1` = 单击（选择/聚焦），`2` = 双击（打开/激活） |

`loc` 与 `label` 为互斥参数，二者不可同时为空。当传入 `label` 时，工具会在内部调用 `_resolve_label` 函数将标签 ID 解析为对应的屏幕坐标[^2^]。`clicks=0` 的悬停模式不产生实际点击事件，仅将鼠标指针移动到目标位置，适用于触发悬停提示（tooltip）或验证元素可见性。`button` 参数的三个取值覆盖了 Windows 应用中的主要鼠标交互模式——左键用于常规选择，右键用于调出上下文菜单，中键则常用于浏览器标签页关闭或特定应用功能。

#### 3.1.2 Type 工具

Type 工具在指定位置输入文本，支持输入前清空、光标位置控制和回车提交等辅助功能。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `text` | `str` | 必填 | 待输入的文本内容 |
| `loc` | `list[int] \| str \| None` | `None` | 屏幕坐标 `[x, y]`，或 JSON 字符串 |
| `label` | `int \| None` | `None` | UI 元素标签 ID |
| `clear` | `bool \| str` | `False` | 输入前是否清空现有文本；字符串 `"true"` 会被强制转换为布尔值 `True` |
| `caret_position` | `Literal["start","idle","end"]` | `"idle"` | 光标位置：`start` 置于文本开头，`end` 置于末尾，`idle` 保持默认 |
| `press_enter` | `bool \| str` | `False` | 输入完成后是否按回车键提交 |

`caret_position` 参数在表单批量填写场景中尤为重要。当需要向多个输入框依次填入数据时，将 `caret_position` 设为 `"start"` 可确保新文本从字段起始位置开始写入，避免因光标遗留位置导致文本错位。`clear` 参数支持布尔值与字符串两种类型，这是为了兼容 Claude Desktop 等客户端的序列化行为——该类客户端会将列表参数以 JSON 字符串形式传递，工具内部通过 `_as_bool` 辅助函数完成类型统一[^2^]。

#### 3.1.3 Scroll 工具

Scroll 工具控制鼠标滚轮操作，支持垂直与水平两个滚动方向。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `loc` | `list[int] \| str \| None` | `None` | 滚动位置坐标；为 `None` 时在鼠标当前位置滚动 |
| `label` | `int \| None` | `None` | UI 元素标签 ID |
| `type` | `Literal["horizontal","vertical"]` | `"vertical"` | 滚动方向类型 |
| `direction` | `Literal["up","down","left","right"]` | `"down"` | 滚动方向：`up`/`down` 对应垂直，`left`/`right` 对应水平 |
| `wheel_times` | `int` | `1` | 滚轮滚动次数；单次滚动约移动 3–5 行 |

Scroll 工具的 `loc` 参数允许为 `None`，此时工具在当前鼠标指针位置执行滚动，无需先进行定位操作。这一设计在浏览长文档或网页时非常实用——Agent 可以在滚动之后继续基于 Snapshot 获取的新 UI 状态进行后续操作，无需每次滚动都重新指定坐标[^2^]。`wheel_times` 参数的量纲为滚轮"刻度"数，而非像素值或行数，这一抽象使得工具在不同 DPI 设置和不同应用程序中的滚动行为保持一致。

#### 3.1.4 Move 工具

Move 工具控制鼠标指针移动，并支持通过拖拽模式实现拖放操作。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `loc` | `list[int] \| str \| None` | `None` | 目标屏幕坐标 `[x, y]` |
| `label` | `int \| None` | `None` | UI 元素标签 ID |
| `drag` | `bool \| str` | `False` | 是否执行拖拽操作；`True` 时从当前位置拖拽到目标坐标 |

Move 工具在 `drag=False`（默认）时执行纯鼠标移动操作，不产生点击或拖拽事件，适用于将指针悬停到某个元素以触发其悬停状态（hover state）。当 `drag=True` 时，工具在当前鼠标位置按下左键，移动指针至目标坐标后释放，完成完整的拖放（drag-and-drop）操作[^2^]。这一机制使得 Agent 能够实现文件在不同文件夹之间的移动、窗口位置的调整以及支持拖拽排序的界面操作。

**UI 交互工具组注解汇总**

| 工具 | readOnlyHint | destructiveHint | idempotentHint | openWorldHint |
|------|-------------|-----------------|---------------|---------------|
| Click | False | **True** | False | False |
| Type | False | **True** | False | False |
| Scroll | False | False | **True** | False |
| Move | False | False | **True** | False |

Click 和 Type 被标记为 `destructiveHint=True`，因为它们会修改 UI 状态（点击按钮可能触发提交，文本输入会改变字段内容）；Scroll 和 Move 则为幂等操作，多次执行相同的滚动或移动不会改变最终结果[^2^]。

### 3.2 键盘与等待工具组

键盘与等待工具组包含 Shortcut、Wait 和 WaitFor 三个工具，分别覆盖键盘快捷键执行、固定时长暂停和条件轮询等待三类时间/输入控制需求。

#### 3.2.1 Shortcut 工具

Shortcut 工具执行键盘快捷键，支持由 `+` 号分隔的组合键语法。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `shortcut` | `str` | 必填 | 快捷键组合，如 `"ctrl+c"`、`"alt+tab"`、`"win+r"` |

该工具接受的键名遵循标准键盘命名规范。常用组合包括系统级快捷键（`win+r` 打开"运行"对话框、`win` 单独按下打开开始菜单）、应用操作快捷键（`ctrl+c` 复制、`ctrl+v` 粘贴、`ctrl+shift+esc` 打开任务管理器）以及窗口管理快捷键（`alt+tab` 切换应用、`alt+f4` 关闭窗口）[^2^]。工具内部将这些组合键转换为底层键盘事件序列，因此支持几乎所有 Windows 应用程序的快捷键响应。需要注意的是，Shortcut 工具的 `destructiveHint=True` 且 `idempotentHint=False`，因为快捷键的效果高度依赖于当前焦点窗口的上下文——相同的 `ctrl+s` 在记事本中保存文档，在浏览器中则可能保存网页。

#### 3.2.2 Wait 工具

Wait 工具执行固定时长的主动暂停。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `duration` | `int` | 必填 | 暂停时长，单位为秒 |

该工具内部直接调用 Python 的 `time.sleep(duration)` 实现等待[^2^]。其 `readOnlyHint=True` 和 `idempotentHint=True` 表明这是一个安全的只读操作——多次执行相同的 Wait 调用不会产生副作用。典型使用场景包括：等待应用程序启动完成、等待 UI 动画播放结束、等待页面内容渲染、等待对话框弹出，以及在两个快速连续操作之间插入间隔以避免竞态条件。

#### 3.2.3 WaitFor 工具

WaitFor 工具是 Wait 的条件化扩展，通过轮询 Windows 辅助功能树（accessibility tree）来检测 UI 状态变化，直至指定条件满足或超时。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `condition` | `str` | 必填 | 等待条件，可选值见下文 |
| `text` | `str \| None` | `None` | 用于匹配的文本内容 |
| `window_name` | `str \| None` | `None` | 用于匹配的窗口名称 |
| `timeout` | `float` | `10.0` | 最大等待时间，单位为秒，取值范围 $(0, 120]$ |
| `interval` | `float` | `0.25` | 轮询间隔，单位为秒，取值范围 $(0, 5]$ |
| `use_dom` | `bool \| str` | `False` | 是否使用浏览器 DOM 文本进行匹配 |

WaitFor 支持五种等待条件，各条件对 `text` 和 `window_name` 参数的要求不同：

| 条件值 | 别名 | 检测逻辑 | `text` 要求 | `window_name` 要求 |
|--------|------|----------|------------|-------------------|
| `text_exists` | `text` | 文本是否出现在任何可访问文本源中 | **必填** | 可选 |
| `active_window` | `window` | 当前活动窗口标题是否匹配 | 或 `window_name` 至少填一个 | 或 `text` 至少填一个 |
| `element_exists` | `element` | 是否存在匹配的交互元素 | 或 `window_name` 至少填一个 | 或 `text` 至少填一个 |
| `element_enabled` | `enabled` | 元素是否存在且处于启用状态 | 或 `window_name` 至少填一个 | 或 `text` 至少填一个 |
| `focused_element` | `focused` | 当前聚焦元素是否匹配 | 或 `window_name` 至少填一个 | 或 `text` 至少填一个 |

工具内部通过 `_matches_wait_condition` 函数执行条件判断[^2^]。对于 `text_exists` 条件，检测范围覆盖活动窗口标题、所有打开窗口标题、UI 交互节点的 `name`/`control_type`/`window_name` 属性及其元数据，以及浏览器 DOM 信息节点的文本内容（当 `use_dom=True` 时）。对于 `element_enabled`，逻辑与 `element_exists` 相同，但额外要求元素的元数据中包含 `enabled` 标记。`focused_element` 则要求元素元数据中包含 `has_focused` 标记。

WaitFor 的轮询机制在工具内部完成，无需 Agent 反复调用 Snapshot 进行状态检查。每次轮询调用 `desktop.get_state(use_vision=False, use_dom=use_dom_bool, use_ui_tree=True, use_annotation=False)` 获取轻量级桌面状态，通过 `time.monotonic()` 计算剩余时间，若超时则抛出 `TimeoutError`[^2^]。这种内置轮询模式显著减少了 Agent 与 MCP 服务器之间的往返通信次数，将原本可能需要数十次工具调用的等待过程压缩为单次 WaitFor 调用。

### 3.3 状态捕获工具组

状态捕获工具组包含 Screenshot 和 Snapshot 两个工具，分别提供快速截图和完整桌面状态捕获两种不同粒度的桌面感知能力。这两个工具是整个工具链的基础——UI 交互工具依赖它们来获取 `label` 参数所需的元素 ID，Agent 决策流程依赖它们来理解当前桌面状态。

#### 3.3.1 Screenshot 工具

Screenshot 工具优先保证捕获速度，跳过 UI 树提取，仅返回截图图像、光标位置和窗口摘要信息。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `use_annotation` | `bool \| str` | `False` | 是否在截图上绘制 UI 元素边界框 |
| `width_reference_line` | `int \| None` | `None` | 在截图上叠加的垂直参考网格线数量 |
| `height_reference_line` | `int \| None` | `None` | 在截图上叠加的水平参考网格线数量 |
| `display` | `list[int] \| None` | `None` | 目标显示器索引（从 0 开始），如 `[0]` 或 `[0,1]` |

Screenshot 工具的 `use_annotation` 默认关闭，这是其与 Snapshot 工具的关键差异之一。当需要向 LLM 提供干净的截图画面（如用于视觉推理）时，保持 `use_annotation=False` 可避免边界框干扰[^2^]。`display` 参数支持多显示器配置，通过零基索引引用 Windows 当前活动的显示器，省略该参数时捕获整个虚拟桌面（跨所有显示器的完整桌面区域）。工具内部默认启用截图后端（`use_vision=True`），且固定跳过 UI 树提取（`use_ui_tree=False`）以换取速度优势。

工具支持三种截图后端，通过环境变量 `WINDOWS_MCP_SCREENSHOT_BACKEND` 控制：`auto`（默认，按 `dxcam` → `mss` → `pillow` 顺序尝试）、`dxcam`、`mss` 和 `pillow`。在高分屏（1440p、4K）上，可通过 `WINDOWS_MCP_SCREENSHOT_SCALE` 环境变量设置缩放因子（取值范围 $[0.1, 1.0]$）来控制截图文件大小，避免超过 Claude Desktop 约 1 MB 的工具结果限制[^1^]。

#### 3.3.2 Snapshot 工具

Snapshot 工具提供完整的桌面状态捕获，是 UI 自动化流程中的核心感知工具。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `use_vision` | `bool \| str` | `False` | 是否在结果中包含截图 |
| `use_dom` | `bool \| str` | `False` | 是否提取浏览器 DOM 内容而非浏览器 UI |
| `use_annotation` | `bool \| str` | `True` | 是否在截图上绘制元素边界框和标签 |
| `use_ui_tree` | `bool \| str` | `True` | 是否提取交互元素和可滚动区域 |
| `width_reference_line` | `int \| None` | `None` | 垂直参考网格线数量 |
| `height_reference_line` | `int \| None` | `None` | 水平参考网格线数量 |
| `display` | `list[int] \| None` | `None` | 目标显示器索引 |

Snapshot 工具的参数设计体现了功能与性能之间的权衡。`use_ui_tree=True`（默认）时，工具通过 Windows UIA（UI Automation）接口提取两类节点：交互节点（interactive nodes，如按钮、文本框、链接、菜单项）和可滚动节点（scrollable nodes），每个节点附带唯一标签 ID、屏幕坐标和控制类型信息[^2^]。这些标签 ID 是 Click、Type 等工具 `label` 参数的数据来源。`use_dom=True` 时，工具针对浏览器内容提取 DOM 信息节点（dom informative nodes），将网页元素而非浏览器 UI 外壳暴露给 Agent，该模式支持 Chrome、Edge 和 Firefox（Firefox 通过 IAccessible2 回退实现）。

`use_annotation=True`（默认）在截图上以彩色矩形框标注检测到的 UI 元素并叠加标签编号，便于 LLM 通过视觉方式定位目标元素。当需要获得干净的截图（如用于视觉质量判断或向用户展示）时，可设置 `use_annotation=False`。`width_reference_line` 和 `height_reference_line` 参数在截图上叠加等距参考网格线，辅助 LLM 进行空间推理和坐标估算[^2^]。

**Screenshot 与 Snapshot 功能对比**

| 维度 | Screenshot | Snapshot |
|------|-----------|----------|
| 首要目标 | 速度 | 完整性 |
| 截图 | 始终包含 | 由 `use_vision` 控制 |
| UI 树提取 | 跳过 | 由 `use_ui_tree` 控制（默认开启） |
| 元素标签 | 无 | 有（交互节点和滚动节点） |
| 浏览器 DOM | 不支持 | 由 `use_dom` 控制 |
| 边界框标注 | 默认关闭 | 默认开启 |
| 典型调用顺序 | 首选手，快速获取视觉上下文 | 需要元素 ID 时调用 |
| 视觉反馈闪光 | 有（可禁用） | 有（可禁用） |

两个工具捕获完成后均会触发一次橙色-红色发光边框的视觉反馈（flash），该效果在截图完成后渲染于透明的始终置顶窗口上，因此不会出现在截图画面中。通过设置环境变量 `WINDOWS_MCP_DISABLE_FLASH=1` 可禁用此效果[^1^]。

在 Agent 工作流中，建议的调用模式是：首先调用 Screenshot 快速获取视觉上下文，若 LLM 需要与特定 UI 元素交互（如"点击保存按钮"），再调用 Snapshot 获取元素标签。这种分层策略平衡了速度与功能，避免了每次操作前都执行完整的 UI 树提取带来的延迟开销。

### 3.4 系统与应用管理工具组

系统与应用管理工具组包含 App、PowerShell、FileSystem 和 Process 四个工具，覆盖应用生命周期管理、命令行执行、文件系统操作和进程管理四类系统级功能。

#### 3.4.1 App 工具

App 工具管理应用程序的启动、窗口调整和焦点切换。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | `Literal["launch","resize","switch"]` | `"launch"` | 操作模式：启动应用、调整窗口、切换焦点 |
| `name` | `str \| None` | `None` | 应用名称或窗口标题（支持模糊匹配） |
| `window_loc` | `list[int] \| None` | `None` | 窗口位置 `[x, y]`（仅 resize 模式） |
| `window_size` | `list[int] \| None` | `None` | 窗口尺寸 `[width, height]`（仅 resize 模式） |

三种模式的工作方式各有不同。`launch` 模式从开始菜单搜索并启动指定名称的应用程序，`name` 参数支持模糊匹配，无需完整的应用路径[^2^]。`resize` 模式调整指定窗口（或当 `name` 为空时调整活动窗口）的位置和大小，通过 `window_loc` 和 `window_size` 参数精确控制。`switch` 模式将指定窗口带到前台并赋予焦点。该工具的 `destructiveHint=True`，因为启动应用或切换窗口会改变桌面状态。

需要注意的是，App 工具依赖 Windows 的英文界面来识别开始菜单中的应用名称。当系统语言设置为非英文时，`launch` 模式可能无法正常工作，此时可通过 PowerShell 工具以命令行方式启动应用作为替代方案[^1^]。

#### 3.4.2 PowerShell 工具

PowerShell 工具提供完整的 PowerShell 命令执行能力，是系统管理和自动化任务的核心工具。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `command` | `str` | 必填 | 要执行的 PowerShell 命令 |
| `timeout` | `int` | `30` | 命令执行超时时间，单位为秒 |

工具内部通过 `PowerShellExecutor.execute_command` 方法执行命令，返回命令输出和状态码[^2^]。由于 PowerShell 具有完整的系统访问权限，该工具的 `openWorldHint=True` 和 `destructiveHint=True`，表明它不仅修改本地系统状态，还可能通过网络访问影响外部世界。命令执行环境继承 MCP 服务器进程的权限上下文，因此可以执行文件管理（`Get-ChildItem`、`Copy-Item`）、进程控制（`Get-Process`、`Stop-Process`）、网络操作（`Invoke-WebRequest`、`Test-NetConnection`）以及 Windows 管理接口（WMI）查询等操作。

`timeout` 参数默认 30 秒，对于可能耗时较长的操作（如大型文件复制、网络下载）应适当增大。工具返回的结果包含命令的文本输出（标准输出和标准错误合并）和数字状态码，便于 Agent 判断命令执行是否成功。

#### 3.4.3 FileSystem 工具

FileSystem 工具提供八种文件系统操作模式，是 Agent 与本地文件系统交互的主要接口。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | `Literal["read","write","copy","move","delete","list","search","info"]` | 必填 | 操作模式 |
| `path` | `str` | 必填 | 目标文件或目录路径 |
| `destination` | `str \| None` | `None` | 目标路径（copy/move 模式必填） |
| `content` | `str \| None` | `None` | 写入内容（write 模式必填） |
| `pattern` | `str \| None` | `None` | 搜索模式（search/list 模式可选） |
| `recursive` | `bool \| str` | `False` | 是否递归操作 |
| `append` | `bool \| str` | `False` | 是否追加写入（write 模式） |
| `overwrite` | `bool \| str` | `False` | 是否覆盖目标（copy/move 模式） |
| `offset` | `int \| None` | `None` | 起始行偏移量（read 模式） |
| `limit` | `int \| None` | `None` | 最大读取行数（read 模式） |
| `encoding` | `str` | `"utf-8"` | 文件编码 |
| `show_hidden` | `bool \| str` | `False` | 是否显示隐藏文件（list 模式） |

路径解析采用 Desktop 相对路径策略：当传入的路径不是绝对路径时，工具自动将其解析为相对于用户桌面目录（`user_desktop_dir()`）的路径[^2^]。这一设计简化了常见文件操作的书写——`path="report.txt"` 即指向桌面上的 report.txt 文件。如需访问其他位置，应传入完整绝对路径。

八种模式的功能分配如下：`read` 读取文本文件内容，支持通过 `offset` 和 `limit` 参数实现分页读取，避免一次性加载过大文件导致上下文溢出；`write` 创建或覆盖文件，`append=True` 时追加内容；`copy` 和 `move` 分别实现复制和移动操作，支持 `overwrite` 参数控制是否覆盖已有目标；`delete` 删除文件或目录，`recursive=True` 时递归删除非空目录；`list` 列出目录内容，支持 `pattern` 过滤和 `show_hidden` 参数；`search` 在目录树中搜索匹配 glob 模式的文件；`info` 返回文件或目录的元数据（大小、创建时间、修改时间、类型）。

#### 3.4.4 Process 工具

Process 工具提供进程列表查看和终止功能，相当于程序化访问 Windows 任务管理器。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | `Literal["list","kill"]` | 必填 | 操作模式 |
| `name` | `str \| None` | `None` | 进程名称（list 模式过滤，kill 模式匹配） |
| `pid` | `int \| None` | `None` | 进程 ID（kill 模式） |
| `sort_by` | `Literal["memory","cpu","name"]` | `"memory"` | 排序方式（list 模式） |
| `limit` | `int` | `20` | 返回结果数量上限（list 模式） |
| `force` | `bool \| str` | `False` | 是否强制终止（kill 模式） |

`list` 模式返回系统中运行的进程列表，每条记录包含 PID、进程名、内存占用和 CPU 使用率，默认按内存使用量降序排列[^2^]。`name` 参数支持模糊匹配过滤，可用于查找特定应用的所有进程实例。`kill` 模式通过 `pid`（精确匹配）或 `name`（模糊匹配）终止进程，`force=True` 时执行强制终止（等效于 `taskkill /F`），否则发送正常的终止信号。在终止进程时，优先使用 `pid` 而非 `name`，因为模糊匹配可能意外终止不相关的进程。

### 3.5 网络与辅助工具组

网络与辅助工具组包含 Scrape、Clipboard、Notification 和 Registry 四个工具，覆盖网页内容获取、剪贴板操作、系统通知和注册表管理四类辅助功能。

#### 3.5.1 Scrape 工具

Scrape 工具获取网页内容，支持两种工作模式：直接 HTTP 请求和浏览器 DOM 提取。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `url` | `str` | 必填 | 目标网页 URL |
| `query` | `str \| None` | `None` | 内容聚焦查询，引导 LLM 提取特定信息 |
| `use_dom` | `bool \| str` | `False` | 是否从活动浏览器标签页的 DOM 提取内容 |
| `use_sampling` | `bool \| str` | `True` | 是否使用 LLM 采样处理原始内容 |

`use_dom=False`（默认）时，工具执行轻量级 HTTP GET 请求获取页面内容，返回经过 LLM 处理的清洁文本摘要，自动去除导航菜单、Cookie 横幅、广告和页脚链接等冗余内容[^2^]。`use_dom=True` 时，工具从当前活动浏览器标签页提取 DOM 内容——这要求目标 URL 已在浏览器中打开。该模式返回 DOM 信息节点的文本内容，并在输出头部和尾部分别附加滚动状态提示（如 "Reached top" / "Scroll down to see more"），帮助 Agent 判断是否需要执行 Scroll 操作来浏览更多内容。

Scrape 工具内置 SSRF（Server-Side Request Forgery，服务器端请求伪造）保护机制，阻止以下类型的请求：私有 IP 地址（如 10.0.0.0/8、172.16.0.0/12、192.168.0.0/16）、回环地址（127.0.0.0/8）、链路本地地址（169.254.0.0/16）、URL 中嵌入的凭证（如 `http://user:pass@example.com`），以及非 HTTP/HTTPS 协议的 URL[^1^]。这一安全机制防止 Agent 被诱导访问内部网络资源或本地服务。

`use_sampling=True`（默认）时，工具通过 MCP 客户端的 `ctx.sample()` 方法将原始页面内容发送给 LLM 进行提炼和结构化，最大令牌数限制为 2048。若客户端不支持采样功能，工具自动回退至返回原始内容[^2^]。`query` 参数在采样时作为聚焦提示，引导 LLM 提取特定主题的信息。

#### 3.5.2 Clipboard 工具

Clipboard 工具读写 Windows 系统剪贴板内容。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | `Literal["get","set"]` | 必填 | 操作模式：`get` 读取，`set` 写入 |
| `text` | `str \| None` | `None` | 要写入的文本内容（set 模式必填） |

`get` 模式通过 `win32clipboard` 库访问剪贴板，优先读取 Unicode 文本格式（`CF_UNICODETEXT`）。若剪贴板中包含非文本数据（如图片、文件列表），工具返回提示信息告知无法读取[^2^]。`set` 模式先清空剪贴板，再写入指定的 Unicode 文本。工具在操作完成后立即关闭剪贴板句柄，避免长时间占用导致其他应用无法访问剪贴板。

Clipboard 工具在数据传递场景中非常有用。例如，Agent 可以先用 Scrape 工具获取网页内容，再通过 Clipboard 工具将其放入剪贴板，最后在目标应用中通过 Shortcut 工具执行 `ctrl+v` 粘贴，实现跨应用的数据流转。

#### 3.5.3 Notification 工具

Notification 工具发送 Windows Toast 通知。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `title` | `str` | 必填 | 通知标题 |
| `message` | `str` | 必填 | 通知正文内容 |
| `app_id` | `str` | 必填 | 发送通知的应用程序用户模型 ID（AUMID） |

该工具通过 Windows 通知 API 发送 Toast 通知，需要有效的 AUMID（Application User Model ID）来指定通知的发送方身份[^2^]。AUMID 是 Windows 用于标识应用程序的通知注册身份，常见的有效值包括系统应用的 AUMID（如 `Windows.SystemToast.WindowsActionCenter`）或已安装 UWP 应用的 AUMID。若传入无效的 `app_id`，通知可能无法显示。

Notification 工具在长时间运行的自动化任务中具有实用价值——Agent 可以在任务完成、需要人工确认或发生异常时向用户发送桌面通知，实现人机协作。

#### 3.5.4 Registry 工具

Registry 工具读写 Windows 注册表，支持四种操作模式。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | `Literal["get","set","delete","list"]` | 必填 | 操作模式 |
| `path` | `str` | 必填 | 注册表键路径，使用 PowerShell 格式 |
| `name` | `str \| None` | `None` | 值名称（get/set 模式必填） |
| `value` | `str \| None` | `None` | 写入值内容（set 模式必填） |
| `type` | `RegistryType` | `"String"` | 注册表值类型 |

`path` 参数使用 PowerShell 路径格式，如 `"HKCU:\\Software\\MyApp"` 或 `"HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"`[^2^]。工具内部通过 PowerShell 命令与注册表交互，因此路径格式与 `regedit` 的传统格式不同——根键使用缩写形式（`HKCU`、`HKLM`、`HKCR`、`HKU`、`HKCC`），组件之间用反斜杠分隔。

四种模式的功能如下：`get` 读取指定键下某个值名称的数据内容；`set` 创建或更新值，`name` 和 `value` 均为必填参数；`delete` 删除指定值（`name` 有值时）或整个键（`name` 为空时）；`list` 枚举指定键下的所有子键和值名称。`type` 参数指定注册表值的数据类型，可选值包括 `String`（REG_SZ，默认）、`ExpandString`（REG_EXPAND_SZ）、`Binary`（REG_BINARY）、`DWord`（REG_DWORD）、`QWord`（REG_QWORD）、`MultiString`（REG_MULTI_SZ）。

Registry 工具被标记为 `destructiveHint=True`，因为修改注册表可能影响系统稳定性。建议在使用 `set` 和 `delete` 模式前先用 `get` 或 `list` 模式确认目标路径和内容，避免误操作。

### 3.6 批量操作工具组

批量操作工具组包含 MultiSelect 和 MultiEdit 两个工具，通过单次调用实现多个 UI 元素的协同操作，减少 Agent 的工具调用次数和上下文占用。

#### 3.6.1 MultiSelect 工具

MultiSelect 工具批量选择多个 UI 元素（文件、文件夹、复选框等），支持基于坐标或标签的批量定位。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `locs` | `list[list[int]] \| str \| None` | `None` | 坐标列表 `[[x1,y1],[x2,y2],...]`，或 JSON 字符串 |
| `labels` | `list[int] \| str \| None` | `None` | 标签 ID 列表 `[label1, label2, ...]` |
| `press_ctrl` | `bool \| str` | `True` | 选择时是否按住 Ctrl 键（多选模式） |

`locs` 和 `labels` 为互斥参数，二者不可同时为空。当传入 `labels` 时，工具通过 `desktop.get_coordinates_from_labels()` 方法批量解析标签为坐标[^2^]，这比逐个解析的效率更高。`press_ctrl=True`（默认）时，工具在点击每个元素的同时保持 Ctrl 键按下，实现不连续多选（等效于用户按住 Ctrl 键点击多个文件）。`press_ctrl=False` 时执行独立的多次单击，适用于需要逐个点击但不保持多选状态的场景。

MultiSelect 的批量标签解析机制是其性能优势的关键。相比于 Agent 循环调用 Click 工具逐个选择元素，MultiSelect 通过单次批量解析和底层批量操作将多次往返压缩为一次工具调用，显著降低了高延迟 MCP 传输模式（如 SSE/HTTP）下的总执行时间。

#### 3.6.2 MultiEdit 工具

MultiEdit 工具批量向多个输入字段填入文本，适用于表单快速填写场景。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `locs` | `list[list] \| str \| None` | `None` | 坐标+文本条目列表 `[[x1,y1,text1],[x2,y2,text2],...]` |
| `labels` | `list[list] \| str \| None` | `None` | 标签+文本条目列表 `[[label1,text1],[label2,text2],...]` |

`locs` 参数接受三元组列表，每个条目包含 x 坐标、y 坐标和待输入文本。`labels` 参数接受二元组列表，每个条目包含标签 ID 和待输入文本——工具先批量解析标签为坐标，再合成完整的三元组数据[^2^]。

工具内部通过 `desktop.multi_edit(locs)` 方法执行批量写入。与 Agent 依次调用 Type 工具逐个填写表单字段相比，MultiEdit 的优势在于：首先，批量标签解析减少了坐标查询开销；其次，单次工具调用减少了 MCP 协议往返次数；最后，底层实现可以优化文本输入的批处理顺序，避免焦点切换带来的额外延迟。在处理包含 5 个以上输入字段的表单时，MultiEdit 通常比循环调用 Type 工具快 2–3 倍。

MultiEdit 工具在填写注册表单、配置页面、数据录入界面等包含多个文本字段的场景中尤为高效。Agent 可以先调用 Snapshot 获取所有输入字段的标签，然后构建 `labels` 参数进行一次性批量填写，最后用 Shortcut 工具执行 `ctrl+s` 保存。

---

## 4. 安全机制与权限控制

Windows-MCP 作为直接操控操作系统核心功能的 MCP 服务器，其安全架构设计遵循"纵深防御"（Defense in Depth）原则，在网络、认证、工具三个层面构建分层防护体系。本章从安全防护体系与配置管理两个维度，评估其安全机制的完备性与可运维性。

### 4.1 多层安全防护体系

#### 4.1.1 网络层安全

网络层安全是阻止未授权访问的第一道屏障。Windows-MCP 提供三项关键的网络层防护机制。

**IP 白名单（IP Allowlist）** 通过 `--ip-allowlist` 参数或 `WINDOWS_MCP_IP_ALLOWLIST` 环境变量实现，支持以 CIDR（无类别域间路由，Classless Inter-Domain Routing）表示法指定允许的客户端 IP 段，例如 `--ip-allowlist "203.0.113.0/24,198.51.100.5"`。默认情况下，系统会自动阻止来自私有 IP 地址段（如 10.0.0.0/8、172.16.0.0/12、192.168.0.0/16）以及回环地址（127.0.0.0/8）的连接请求，这一默认拒绝策略有效降低了内网横向渗透的风险。

**CORS 来源控制** 采用"默认拒绝"的设计哲学：系统默认不发送任何 CORS（跨域资源共享，Cross-Origin Resource Sharing）响应头，浏览器将依据同源策略（Same-Origin Policy）阻止跨域请求。当需要支持基于浏览器的 MCP 客户端时，管理员必须通过 `--cors-origins` 参数显式声明可信来源，如 `--cors-origins "https://my-client.example.com"`，仅被列入白名单的来源才会收到 `Access-Control-Allow-Origin` 响应头。

**DNS Rebinding 防护** 通过 Host-header validation 自动启用，无需额外配置。该机制验证请求中的 Host 头是否与服务器绑定的地址一致，防止攻击者利用 DNS 重绑定技术绕过本地主机的同源策略限制，进而对 localhost 上的 MCP 服务端点发起未授权调用。

此外，**SSRF（服务器端请求伪造，Server-Side Request Forgery）防护** 内置于 `Scrape` 工具中，自动拦截针对私有 IP、回环地址、链路本地地址（link-local）以及包含用户凭证的 URL 和非 HTTP 协议的请求，阻断通过网页抓取功能发起的内网探测行为。

#### 4.1.2 认证层安全

Windows-MCP 支持两种认证机制，且允许共存运行，满足不同部署场景的需求。

**Bearer Token 模式** 是最直接的认证方式，通过 `--auth-key "your_token"` 启用后，所有 HTTP 请求必须在请求头中携带 `Authorization: Bearer your_token`。该模式适用于可信内部网络或单用户开发环境，配置简单、开销低，但密钥的集中管理需要配套的安全措施。

**OAuth 2.0 + PKCE（Proof Key for Code Exchange）模式** 面向多用户或公网部署场景，遵循 RFC 8414 和 RFC 7636 标准。服务端暴露四个标准端点：`/.well-known/oauth-authorization-server`（服务器元数据发现）、`/oauth/authorize`（授权码 + PKCE，强制使用 S256 挑战方法）、`/oauth/token`（令牌交换，需要客户端密钥）以及 `/oauth/register`（动态客户端注册，已显式禁用）。动态客户端注册的禁用意味着客户端必须预先配置（pre-provisioned），有效防止了恶意客户端的自动注册；重定向 URI（Redirect URI）被严格限制为回环地址（loopback `http(s)`），避免了授权码被截获的风险。两种认证模式可同时启用，系统均将其识别为有效的 Bearer 令牌，这一设计提升了与不同 MCP 客户端的兼容性。

#### 4.1.3 工具层安全

工具层安全聚焦于"即便网络层和认证层被突破，仍须限制攻击者可调用的操作范围"这一最小权限原则（Principle of Least Privilege）。

Windows-MCP 默认启用全部 19 个工具，但提供两种精确控制手段：`--tools` 白名单参数仅启用列出的工具，`--exclude-tools` 黑名单参数禁用指定工具。两种机制可叠加使用，形成"白名单优先、黑名单兜底"的双重过滤策略。

从风险评估角度，`PowerShell` 与 `Registry` 两个工具需要特别关注。`PowerShell` 工具允许执行任意 PowerShell 命令，具有完整的系统命令行访问权限，可执行文件读写、进程管理、网络操作甚至下载执行远程脚本；`Registry` 工具允许对 Windows 注册表进行读取、写入、删除和枚举操作，直接影响系统配置和软件行为。这两个工具的启用相当于向 MCP 客户端开放系统级管理员权限，建议在非必要场景下通过 `--exclude-tools "PowerShell,Registry"` 显式禁用。相比之下，`Screenshot`、`Click`、`Type` 等 UI 交互工具虽存在被滥用的可能，但影响范围局限于用户界面层面，风险相对可控。

### 4.2 配置管理

#### 4.2.1 配置文件结构与参数覆盖优先级

Windows-MCP 采用 TOML（Tom's Obvious, Minimal Language）格式的配置文件，默认路径为 `~/.windows-mcp/config.toml`，可通过 `--config /path/to/config.toml` 参数指定自定义路径。配置文件按功能划分为三个逻辑段：`[server]` 段定义传输协议、监听地址、端口、认证密钥和 TLS 证书路径；`[security]` 段包含 IP 白名单、CORS 来源和 OAuth 客户端凭证；`[tools]` 段控制工具的启用与排除。

配置参数的生效遵循明确的优先级顺序：**CLI 命令行参数始终覆盖配置文件中的对应值**。这意味着运维人员可以在基础配置文件之上，通过临时追加 CLI 参数实现快速调试或紧急配置变更，无需修改持久化配置文件。例如，配置文件中设置了 `auth_key = "dev-key"`，但启动时传入 `--auth-key "prod-key"`，则以生产环境密钥为准。证书文件路径（`ssl_certfile`、`ssl_keyfile`）的解析以 `~/.windows-mcp/` 目录为基准，简化了相对路径的使用。

#### 4.2.2 环境变量速查表

除配置文件和 CLI 参数外，Windows-MCP 支持通过环境变量进行配置，所有环境变量均以 `WINDOWS_MCP_` 为前缀以实现命名空间隔离。下表汇总全部可配置的环境变量及其默认值与说明。

| 环境变量 | 默认值 | 说明 |
|:---|:---|:---|
| `WINDOWS_MCP_SCREENSHOT_SCALE` | `1.0` | 截图缩放因子，范围 0.1–1.0，高分辨率显示器上建议设为 0.5 以控制图片大小 |
| `WINDOWS_MCP_SCREENSHOT_BACKEND` | `auto` | 截图后端，可选 `auto`、`dxcam`、`mss`、`pillow`，按性能降序自动回退 |
| `WINDOWS_MCP_PROFILE_SNAPSHOT` | 禁用 | 设为 `1`/`true`/`yes`/`on` 启用截图/快照的性能分析日志 |
| `WINDOWS_MCP_DISABLE_FLASH` | 禁用 | 设为 `1`/`true`/`yes`/`on` 禁用截图后的橙色边框闪烁提示 |
| `WINDOWS_MCP_AUTH_KEY` | 无 | Bearer 令牌，替代 `--auth-key` CLI 参数 |
| `WINDOWS_MCP_IP_ALLOWLIST` | 无 | 允许的 IP/CIDR 列表（逗号分隔），替代 `--ip-allowlist` |
| `WINDOWS_MCP_CORS_ORIGINS` | 无 | 跨域来源白名单（逗号分隔），替代 `--cors-origins` |
| `WINDOWS_MCP_TOOLS` | 全部启用 | 显式启用的工具列表（逗号分隔），替代 `--tools` |
| `WINDOWS_MCP_EXCLUDE_TOOLS` | 无 | 禁用的工具列表（逗号分隔），替代 `--exclude-tools` |
| `WINDOWS_MCP_SSL_CERTFILE` | 无 | TLS 证书文件路径（.pem），需与 `SSL_KEYFILE` 配对使用 |
| `WINDOWS_MCP_SSL_KEYFILE` | 无 | TLS 私钥文件路径（.pem），需与 `SSL_CERTFILE` 配对使用 |
| `WINDOWS_MCP_OAUTH_CLIENT_ID` | 无 | OAuth 客户端 ID，需与 `OAUTH_CLIENT_SECRET` 配对使用 |
| `WINDOWS_MCP_OAUTH_CLIENT_SECRET` | 无 | OAuth 客户端密钥，需与 `OAUTH_CLIENT_ID` 配对使用 |
| `WINDOWS_MCP_STATELESS_HTTP` | `false` | 设为 `true` 启用 streamable-http 无状态模式，适用于横向扩展部署 |
| `ANONYMIZED_TELEMETRY` | `true` | 设为 `false` 禁用匿名遥测数据上报 |
| `POSTHOG_API_KEY` | 项目默认值 | PostHog 项目写入密钥，设为空字符串可跳过初始化 |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | PostHog 服务主机地址，支持自托管实例 |
| `WINDOWS_MCP_DEBUG` | `false` | 设为 `true` 启用调试模式，日志级别提升为 DEBUG |

上表中的环境变量覆盖了安全配置、功能调优和运维诊断三大类需求。在实际部署中，建议将 `WINDOWS_MCP_AUTH_KEY`、`WINDOWS_MCP_OAUTH_CLIENT_ID` 等敏感凭证通过环境变量注入而非硬编码于配置文件，以利用容器编排平台或 CI/CD 系统的密钥管理机制，实现凭证的轮转与隔离。`WINDOWS_MCP_SCREENSHOT_SCALE` 和 `WINDOWS_MCP_SCREENSHOT_BACKEND` 则属于性能调优类变量，在高分辨率（1440p、4K）显示器上，将缩放因子从默认值 1.0 调整为 0.5 可将截图文件大小缩减约 75%，有效避免超过 MCP 客户端的工具结果大小限制（如 Claude Desktop 的 1 MB 上限）。

---

## 5. 应用场景与总结评估

### 5.1 典型应用场景

综合前文对 Windows-MCP 架构、工具集与安全机制的分析，该服务器在以下三类场景中展现明确实用价值。

**AI 驱动的桌面自动化**是其最直接的应用方向。借助纯 UIA（UI Automation，用户界面自动化）架构，AI Agent 可通过自然语言指令完成文件整理（按类型归档下载文件）、应用操作（Excel 数据填充、Outlook 邮件发送）及跨应用数据流转。Snapshot 工具返回的带标签 UI 元素树使 Agent 以语义方式引用控件，无需坐标定位，任务可复现性显著提升。实测延迟 0.2–0.5 秒[^4^]，满足非实时批处理需求。

**浏览器自动化**得益于 DOM 模式（`use_dom=True`）的独特设计。启用后 State 工具过滤浏览器外壳 UI，仅返回网页内容结构，支持 Chrome、Edge 与 Firefox[^4^]。适用于信息抓取、表单批量填写及网页端 QA 测试。与视觉方案相比，DOM 模式不受渲染样式变化影响，元素定位更稳定。

**系统管理与 QA 测试**利用其对操作系统级功能的深度访问。Process 工具枚举并管理进程，Registry 工具读写注册表，App 工具启闭应用程序。结合 Screenshot 与 State 工具进行 UI 状态断言，可构建覆盖安装向导验证、功能回归测试的自动化流水线。PowerShell 与 Registry 属高风险工具，建议通过 `--exclude-tools` 在普通场景中禁用[^4^]。

### 5.2 与其他方案对比

下表从架构、平台、视觉依赖、许可和延迟五个维度进行横向比较。

| 评估维度 | Windows-MCP | Anthropic computer-use-mcp | desktop-touch-mcp | Cradle | UI-TARS |
|:---|:---|:---|:---|:---|:---|
| **GitHub Stars** | 6.4k[^4^] | ~1.2k[^83^] | 0[^66^] | ~1.5k[^90^] | ~29.5k[^65^] |
| **底层架构** | UIA（辅助功能树） | 截图 + 辅助树 | Rust UIA + CDP | 纯视觉 | 纯视觉（VLM） |
| **支持平台** | Windows 7/8/10/11 | Linux（Docker 为主） | Windows | Windows | 跨平台 |
| **视觉模型依赖** | 无（可选） | 必需 | 部分 | 必需 | 必需 |
| **GPU 要求** | 无 | 无 | 无 | 中高 | 高（7B 需 16GB+ VRAM）[^67^] |
| **开源许可** | MIT | 闭源（API 服务） | MIT | Apache 2.0 | Apache 2.0 |
| **典型延迟** | 0.2–0.5 秒[^4^] | 高（API 往返） | ~2–106 ms[^78^] | 高（视觉推理） | 中（本地推理） |
| **浏览器支持** | DOM 模式（三浏览器） | 无原生支持 | CDP（Chrome） | 无 | 有 |
| **LLM 兼容性** | 任意 LLM | 仅 Claude | 任意 LLM | 需多模态 LLM | 需视觉-语言模型 |

Windows-MCP 的核心差异化优势体现在三方面：**纯 UIA 无视觉依赖**使其无需 GPU 即可运行，适用于无法部署视觉模型的企业办公环境；**亚秒级延迟**优于需 API 往返或视觉推理的方案；**DOM 模式浏览器支持**填补了同类 Windows 自动化工具在 Web 内容提取方面的空白。UI-TARS 虽凭 ~29.5k Stars 和字节跳动背书拥有更高社区知名度[^65^]，但其纯视觉架构对硬件要求构成部署门槛；desktop-touch-mcp 采用 Rust 引擎实现了更低 UIA 延迟（加权加速比约 82 倍[^78^]），但社区采用度尚处早期，长期稳定性待验证。

### 5.3 总结与建议

**适用场景判断。** 对于以 Windows 桌面为主的 AI Agent 开发项目，Windows-MCP 是当前生态中综合权衡较优的选择。其 MIT 开源许可、任意 LLM 兼容性、6.4k Stars 社区规模及 200 万+ Claude Desktop 用户验证[^4^]，构成技术决策者所需的成熟度信号。特别推荐以下团队优先考虑：需同时覆盖桌面与浏览器自动化的 RPA（Robotic Process Automation，机器人流程自动化）替代团队、无 GPU 资源的中小企业、以及将 AI Agent 集成到现有 Windows IT 基础设施的企业开发者。

**局限性说明。** 采用前需评估以下约束：App 工具依赖 UI 元素语言属性，建议系统语言设为英文[^4^]；段落内特定文本选择精度受限，当前依赖 a11y tree（accessibility tree，无障碍树）定位，富文本精确选区支持正在改进；Type 工具不适合 IDE 逐字符编程输入，设计目标为整块文本批量输入；由于 UIA 无法捕获 DirectX / Vulkan 渲染画面，该服务器**不适用于视频游戏自动化场景**[^4^]。

综上，Windows-MCP 凭借 UIA 架构的轻量性、低延迟交互性能和 DOM 模式的浏览器增强能力，在 Windows 桌面 AI Agent 领域建立了明确技术定位。技术团队可依据自身硬件条件、延迟要求和浏览器自动化需求，结合上述局限性做出采纳决策。

---

