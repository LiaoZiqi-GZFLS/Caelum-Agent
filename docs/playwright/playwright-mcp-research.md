# Playwright MCP Server 深度技术报告

> 研究日期: 2026-07-09
> 版本: @playwright/mcp v0.0.77 (最新)
> 源码: microsoft/playwright monorepo

---

## 1. 包名澄清

你提到的 `@anthropic/playwright-mcp-server` 实际上对应的是 **Microsoft 官方的 `@playwright/mcp`** 包。

| 包名 | 维护方 | 状态 | GitHub |
|------|--------|------|--------|
| `@playwright/mcp` | Microsoft | **活跃维护** (v0.0.77, 34.8k stars) | [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) |
| `@anthropic-ai/mcp-server-playwright` | Anthropic | 社区文档中偶有提及，实际指向微软包 | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) |

中文社区文档（如 cnblogs、掘金）中出现的 `npm install -g @anthropic-ai/playwright-mcp-server` 实际安装的仍是 Microsoft 的 `@playwright/mcp`（或其早期别名）。**真正的源码仓库是 microsoft/playwright，核心代码位于 `packages/playwright-core/src/tools/mcp/` 和 `packages/playwright-core/src/tools/backend/` 目录下。**

---

## 2. 当前版本暴露的完整 Tool 列表

源码位置：`packages/playwright-core/src/tools/backend/tools.ts`

通过 `filteredTools()` 函数过滤后，暴露给 MCP 的 Tools 按功能模块分类如下：

### Core（核心操作 - 默认启用）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_navigate` | 导航到 URL | `url: string` |
| `browser_navigate_back` | 浏览器后退 | 无 |
| `browser_navigate_forward` | 浏览器前进 | 无 |
| `browser_tabs` | Tab 管理（list/new/close/select） | `action`, `index?`, `url?` |
| `browser_snapshot` | **获取页面 accessibility snapshot** | `target?`, `filename?`, `depth?`, `boxes?` |
| `browser_click` | 点击元素 | `target: string`, `element?`, `doubleClick?`, `button?`, `modifiers?` |
| `browser_fill_form` | 批量填充表单 | `fields: Array<{name, type, value, target}>` |
| `browser_press_key` | 按键 | `key: string` |
| `browser_press_sequentially` | 逐字符输入（skillOnly） | `text: string`, `submit?: boolean` |
| `browser_evaluate` | 执行 JavaScript | `function: string`, `target?`, `filename?` |
| `browser_wait_for` | 等待条件 | `time?`, `text?`, `textGone?` |
| `browser_find` | 在 snapshot 中搜索文本 | `text?`, `regex?` |
| `browser_close` | 关闭浏览器 | 无 |
| `browser_resize` | 调整窗口大小 | `width: number`, `height: number` |
| `browser_network_requests` | 列出网络请求 | `static?: boolean`, `filter?: regex`, `filename?` |
| `browser_network_request` | 获取单个请求详情 | `index: number`, `filename?` |
| `browser_console` | 获取控制台日志 | 无 |
| `browser_file_upload` | 上传文件 | `target: string`, `files: string[]` |
| `browser_dialog_accept` | 接受对话框 | `text?: string` |
| `browser_dialog_dismiss` | 关闭对话框 | 无 |
| `browser_run_code` | 运行 Python/JavaScript 代码 | `code: string`, `language: 'python' \| 'javascript'` |

### Vision（视觉模式 - 需 `--vision` 或 capabilities 启用）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_mouse_move_xy` | 鼠标移动到坐标 | `x: number`, `y: number` |
| `browser_mouse_down` | 鼠标按下 | `button?: 'left' \| 'right' \| 'middle'` |
| `browser_mouse_up` | 鼠标释放 | `button?` |
| `browser_mouse_click` | 鼠标点击 | `button?`, `clickCount?` |
| `browser_mouse_drag` | 拖拽 | `fromX`, `fromY`, `toX`, `toY` |
| `browser_screenshot` | 截图 | `filename?`, `target?`, `fullPage?`, `clip?` |

### Storage（存储 - 需 capabilities 启用）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_cookie_list` | 列出 cookies | `domain?`, `path?` |
| `browser_cookie_get` | 获取指定 cookie | `name: string` |
| `browser_cookie_set` | 设置 cookie | `name`, `value`, `domain?`, `path?`, `expires?`, `httpOnly?`, `secure?`, `sameSite?` |
| `browser_cookie_delete` | 删除 cookie | `name: string` |
| `browser_cookie_clear` | 清除所有 cookies | `domain?`, `path?` |
| `browser_storage_get` | 获取 storage | `origin?: string` |
| `browser_storage_set` | 设置 storage | `origin?`, `items: Record<string, string>` |
| `browser_local_storage` | localStorage 操作 | `action: 'get' \| 'set' \| 'delete' \| 'clear'`, `key?`, `value?` |
| `browser_session_storage` | sessionStorage 操作 | `action: 'get' \| 'set' \| 'delete' \| 'clear'`, `key?`, `value?` |

### Network（网络 - 需 capabilities 启用）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_route` | 拦截/模拟网络请求 | `url: string`, `response?: { status?, headers?, body? }` |

### Testing（测试 - 需 capabilities 启用）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_tracing_start` | 开始追踪 | 无 |
| `browser_tracing_stop` | 停止追踪 | `filename?: string` |
| `browser_verify` | 验证页面状态 | `assertions: Array<{type, ...}>` |

### Devtools（开发者工具 - 需 capabilities 启用）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_devtools` | 执行 CDP 命令 | `command: string`, `params?: object` |

### PDF（PDF - 需 capabilities 启用）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_pdf` | 生成 PDF | `filename?: string`, `fullPage?: boolean` |

### Config（配置）

| Tool Name | 功能 | 关键参数 |
|-----------|------|----------|
| `browser_configure` | 浏览器配置 | `viewport?`, `userAgent?`, `locale?`, `timezone?`, `colorScheme?`, `geolocation?`, `permissions?` |

---

## 3. Accessibility Tree 返回格式

### 3.1 核心机制

Playwright MCP 的 `browser_snapshot` 工具返回的是 **结构化 YAML 格式** 的 accessibility tree，而非原始 DOM 或截图。它的底层基于 Playwright 的 `page.accessibility.snapshot()` API。

### 3.2 格式示例

```yaml
- main
  - heading "Sign in to QASkills" [level=1] [target=e3]
  - textbox "Email" [target=e4]
  - textbox "Password" [target=e5]
  - button "Sign in" [target=e6]
  - link "Create an account" [target=e7]
```

### 3.3 格式规范

每个元素节点遵循以下模式：

```
- <role> "<accessible_name>" [attr1=value1] [attr2=value2] [target=eN]
```

#### Role（角色）

来自 ARIA role 或 HTML 语义：

- **容器**：`main`, `navigation`, `complementary`, `article`, `section`, `form`, `list`, `listitem`, `group`, `tablist`, `tabpanel`, `region`, `banner`, `contentinfo`, `search`
- **文本**：`heading`, `paragraph`, `textbox`, `searchbox`, `textarea`, `static`（纯文本）
- **交互**：`button`, `link`, `checkbox`, `radio`, `combobox`, `slider`, `menuitem`, `tab`, `switch`, `menuitemcheckbox`, `menuitemradio`
- **媒体**：`img`, `video`, `audio`, `figure`
- **表格**：`table`, `row`, `cell`, `columnheader`, `rowheader`, `grid`, `gridcell`
- **其他**：`dialog`, `tooltip`, `progressbar`, `status`, `alert`, `separator`, `scrollbar`, `spinbutton`, `timer`

#### Accessible Name（可访问名称）

- 引号包裹的字符串，来源优先级：`aria-label` > `aria-labelledby` > 关联 `<label>` 文本 > `alt` 属性 > `placeholder` > 元素文本内容
- 如果元素没有 accessible name，则省略引号部分

#### 属性（方括号内）

| 属性 | 含义 | 示例 |
|------|------|------|
| `[target=eN]` | **元素引用 ID**，用于后续操作定位 | `target=e12` |
| `[level=N]` | heading 的层级 | `level=1` (对应 h1) |
| `[checked]` | checkbox/radio 被选中 | `[checked]` |
| `[disabled]` | 元素被禁用 | `[disabled]` |
| `[expanded]` | 展开状态（下拉菜单等） | `[expanded]` |
| `[selected]` | 选项被选中 | `[selected]` |
| `[pressed]` | 按钮被按下（toggle button） | `[pressed]` |
| `[box=x,y,w,h]` | 元素边界框（当 boxes=true 时） | `box=100,200,150,30` |

#### 树形缩进

- 使用 **2 空格缩进** 表示父子关系
- 深度可通过 `depth` 参数限制
- 超过深度限制的子树会被折叠为 `...`

### 3.4 browser_snapshot 参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `target` | `string` | 仅捕获指定元素及其子树的 snapshot |
| `filename` | `string` | 保存到文件而非返回文本 |
| `depth` | `number` | 限制树的最大深度 |
| `boxes` | `boolean` | 是否在 snapshot 中包含 `[box=x,y,w,h]` 坐标信息（基于 `Element.getBoundingClientRect`） |

### 3.5 元素定位机制

Tool 中的 `target` 参数支持多种定位方式（由 `tab.ts` 中 `targetLocator()` 方法解析）：

1. **target=eN 引用**：从 snapshot 中获取的 `[target=eN]` 引用（最精确、最推荐）
2. **Role + Name 定位**：如 `heading "Sign in"`、`button "Submit"`
3. **ARIA 属性定位**：如 `textbox "Email"[pressed]`
4. **CSS 选择器**：如 `#email`、`.submit-btn`

---

## 4. Capability 过滤机制

不是所有 tool 默认都暴露。过滤逻辑在 `filteredTools()`（`tools.ts`）中：

```typescript
// 1. 默认暴露所有 core* capability 的 tool
tool.capability.startsWith('core')

// 2. 额外 capabilities 需通过配置显式启用
config.capabilities?.includes(tool.capability)

// 3. skillOnly=true 的 tool 不暴露给 MCP（仅给 Skill 使用）
!tool.skillOnly
```

可用的非 core capabilities：`network`, `pdf`, `storage`, `testing`, `vision`, `devtools`

---

## 5. 源码架构

```
packages/playwright-core/src/tools/
├── mcp/                          # MCP 服务端入口
│   ├── index.ts                  # createConnection() - 创建 MCP Server
│   ├── protocol.ts               # Extension 通信协议 (CDP over WebSocket, v2)
│   ├── browserFactory.ts         # 浏览器实例创建
│   ├── browserModel.ts           # 浏览器状态模型
│   ├── config.ts                 # 配置解析
│   └── program.ts                # CLI 参数处理
├── backend/                      # Tool 实现
│   ├── tools.ts                  # Tool 注册表 + filteredTools()
│   ├── tool.ts                   # Tool 类型定义
│   ├── browserBackend.ts         # MCP tool 调用路由
│   ├── context.ts                # Context 管理
│   ├── tab.ts                    # Tab 管理 + snapshot 生成
│   ├── response.ts               # Tool 响应构建
│   ├── snapshot.ts               # browser_snapshot + browser_click
│   ├── navigate.ts               # browser_navigate
│   ├── tabs.ts                   # browser_tabs
│   ├── keyboard.ts               # browser_press_key
│   ├── mouse.ts                  # mouse 系列
│   ├── form.ts                   # browser_fill_form
│   ├── evaluate.ts               # browser_evaluate
│   ├── find.ts                   # browser_find
│   ├── wait.ts                   # browser_wait_for
│   ├── network.ts                # network 系列
│   ├── cookies.ts                # cookie 系列
│   ├── screenshot.ts             # browser_screenshot
│   ├── console.ts                # browser_console
│   ├── files.ts                  # browser_file_upload
│   ├── dialogs.ts                # dialog 处理
│   ├── storage.ts                # storage 管理
│   ├── webstorage.ts             # local/sessionStorage
│   ├── pdf.ts                    # PDF 生成
│   ├── route.ts                  # 网络路由拦截
│   ├── tracing.ts                # Tracing
│   ├── devtools.ts               # CDP 命令
│   ├── verify.ts                 # 验证
│   ├── video.ts                  # 视频录制
│   ├── runCode.ts                # 代码执行
│   └── config.ts                 # browser_configure
```

---

## 6. 参考链接

- **发布仓库**: https://github.com/microsoft/playwright-mcp
- **核心源码**: https://github.com/microsoft/playwright/tree/main/packages/playwright-core/src/tools/mcp
- **Tool 源码**: https://github.com/microsoft/playwright/tree/main/packages/playwright-core/src/tools/backend
- **NPM**: https://www.npmjs.com/package/@playwright/mcp
