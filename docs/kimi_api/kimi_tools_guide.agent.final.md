# Kimi 开放平台官方内置工具详解手册

> **版本**：2026年7月  
> **适用范围**：Kimi 开放平台（Moonshot AI）Formula 工具  
> **工具数量**：11 个内置工具  

---

# Kimi 开放平台官方内置工具详解

> 本文档面向需要使用 Kimi 开放平台（Moonshot AI）内置工具的开发者，系统讲解 Formula 调用框架及 `web-search`、`fetch` 两个核心官方工具的使用方法。

---

## 第一部分：通用 Formula 调用框架

### 1.1 Formula 机制概述

**Formula** 是 Kimi 开放平台提供内置工具（Built-in Tools）的核心机制。开发者通过 Formula 可以将 Kimi 大模型与外部能力（如联网搜索、网页抓取、代码执行等）无缝集成，让模型在对话过程中根据需要主动调用这些工具，实现"推理—行动—再推理"的 Agent 工作流。

每个 Formula 通过唯一的 **Formula URI** 进行标识，其格式如下：

```
moonshot/{name}:{tag}
```

| 组件 | 说明 | 示例 |
|------|------|------|
| `namespace` | 命名空间，目前仅支持 `moonshot` | `moonshot` |
| `name` | 工具名称 | `web-search`、`fetch` |
| `tag` | 版本标签，默认为 `latest` | `latest` |

例如，`moonshot/web-search:latest` 表示 Kimi 官方提供的联网搜索工具的最新版本，`moonshot/fetch:latest` 表示网页内容提取工具。

### 1.2 获取工具定义

在将 Formula 工具集成到 Chat Completions 流程之前，首先需要获取该工具的标准化定义。Kimi 开放平台提供专门的 REST 端点，返回与 OpenAI 兼容的 `function` 类型 JSON Schema。

**请求方式**：

```bash
curl ${MOONSHOT_BASE_URL}/formulas/{FORMULA_URI}/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

**请求示例**（获取 web-search 工具定义）：

```bash
curl https://api.moonshot.cn/v1/formulas/moonshot/web-search:latest/tools \
  -H "Authorization: Bearer sk-xxxxxxxxxxxxxxxxxxxx"
```

**返回格式**（标准的 OpenAI tools 格式）：

```json
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "web_search",
        "description": "Search the web for information",
        "parameters": {
          "type": "object",
          "properties": {
            "query": {
              "description": "What to search for",
              "type": "string"
            }
          },
          "required": ["query"]
        }
      }
    }
  ]
}
```

返回的 `tools` 数组可直接嵌入到 Chat Completions 请求的 `tools` 参数中使用。

### 1.3 调用工具执行

当模型在对话中决定调用某个工具时，开发者需要通过 `/formulas/{URI}/fibers` 端点实际执行该工具调用。

**请求方式**：

```bash
curl -X POST ${MOONSHOT_BASE_URL}/formulas/{FORMULA_URI}/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "{tool_function_name}",
    "arguments": "{json_encoded_params}"
  }'
```

**请求示例**：

```bash
curl -X POST https://api.moonshot.cn/v1/formulas/moonshot/web-search:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-xxxxxxxxxxxxxxxxxxxx" \
  -d '{
    "name": "web_search",
    "arguments": "{\"query\":\"Kimi K2.6 发布日期\"}"
  }'
```

**返回格式**：

```json
{
  "context": {
    "output": "...明文输出内容...",
    "encrypted_output": "...加密输出内容..."
  }
}
```

对于普通工具，执行结果位于 `context.output` 中；对于 `protected` 工具（如 `web-search`），结果位于 `context.encrypted_output` 中。加密内容可直接作为 `role="tool"` 消息返回给模型，无需解密。

### 1.4 与 Chat Completions 集成的完整流程

将 Formula 工具与 Chat Completions API 集成遵循以下标准 ReAct（Reasoning + Acting）循环：

```
┌─────────────┐    tools    ┌─────────────────┐
│  获取工具定义  │ ──────────▶ │ /formulas/.../  │
│ (/tools)    │             │     tools       │
└─────────────┘             └─────────────────┘
       │                            │
       ▼                            ▼
┌─────────────────────────────────────────────┐
│          构建 Chat Completions 请求           │
│   {model, messages, tools: [tool_def, ...]}  │
└─────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│        模型返回 finish_reason="tool_calls"    │
│   {tool_calls: [{id, function: {name, args}}]}│
└─────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│        通过 /formulas/.../fibers 执行工具      │
│   获取 context.output / context.encrypted_output│
└─────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│      将工具结果以 role="tool" 返回模型         │
│   {role: "tool", tool_call_id: "...", content} │
└─────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│        模型基于工具结果生成最终回复            │
│         finish_reason="stop"                  │
└─────────────────────────────────────────────┘
```

### 1.5 重要注意事项

| 注意事项 | 详细说明 |
|----------|----------|
| **function.name 唯一性** | 单个请求中所有工具的 `function.name` 必须唯一。若同时使用多个 Formula，需确保名称不冲突 |
| **function.name → Formula URI 映射** | 同时使用多个 formula 时，需维护 `function.name` 到 `formula_uri` 的映射关系，以便在执行 tool_call 时路由到正确的 Formula |
| **protected 工具结果加密** | `web-search` 等 protected 工具的结果在 `context.encrypted_output` 中返回，该加密内容可直接作为 tool 结果返回给模型，无需手动解密 |
| **处理全部 tool_calls** | 模型可能返回多个 `tool_calls`，必须**全部**执行并返回结果，遗漏任何一项都会导致后续请求不合法 |
| **tool_call_id 严格对齐** | 每个 `role="tool"` 消息中的 `tool_call_id` 必须与对应的 `tool_calls[].id` 一一对应，否则模型无法正确关联 |
| **限时免费执行** | Kimi 官方 Formula 工具目前处于限时免费阶段，但 `web-search` 每次搜索会额外计费（¥0.03/次） |
| **禁用 thinking 模式** | 使用 `web-search` 等 protected 工具时，必须在请求中禁用 thinking 模式（`reasoning_effort="none"` 或不传），否则工具调用会失败 |

### 1.6 完整集成代码示例

以下是一个使用 Python + OpenAI SDK + httpx 的完整 Formula Chat Client 实现，支持多 Formula 并发获取和工具调用链的自动处理：

```python
import os
import json
import asyncio
import httpx
from openai import AsyncOpenAI


class FormulaChatClient:
    """Kimi Formula 工具集成客户端

    支持自动获取 Formula 工具定义、执行工具调用链、
    处理 protected 工具加密输出。
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.openai = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.httpx = httpx.AsyncClient(timeout=60)

    async def get_tools(self, formula_uri: str) -> list:
        """获取指定 Formula 的工具定义列表

        Args:
            formula_uri: Formula URI, e.g. "moonshot/web-search:latest"

        Returns:
            OpenAI-compatible tools 列表
        """
        resp = await self.httpx.get(
            f"{self.base_url}/formulas/{formula_uri}/tools",
            headers={"Authorization": f"Bearer {self.openai.api_key}"}
        )
        resp.raise_for_status()
        return resp.json()["tools"]

    async def call_tool(self, formula_uri: str, name: str, arguments: dict) -> str:
        """执行工具调用

        Args:
            formula_uri: Formula URI
            name: 工具函数名
            arguments: 参数字典

        Returns:
            工具执行结果字符串（自动兼容 output 和 encrypted_output）
        """
        resp = await self.httpx.post(
            f"{self.base_url}/formulas/{formula_uri}/fibers",
            headers={
                "Authorization": f"Bearer {self.openai.api_key}",
                "Content-Type": "application/json"
            },
            json={"name": name, "arguments": json.dumps(arguments)}
        )
        resp.raise_for_status()
        result = resp.json()
        ctx = result.get("context", {})
        # 兼容普通工具和 protected 工具的加密输出
        return ctx.get("output") or ctx.get("encrypted_output", "")

    async def handle_response(self, response, messages, all_tools, tool_to_uri):
        """递归处理模型响应，自动执行工具调用链

        Args:
            response: ChatCompletion 响应对象
            messages: 对话消息历史列表
            all_tools: 所有可用的工具定义
            tool_to_uri: function.name -> formula_uri 的映射字典
        """
        message = response.choices[0].message

        # 将 assistant 消息（含 tool_calls）追加到历史
        msg_dict = {
            "role": "assistant",
            "content": message.content or "",
        }
        if message.tool_calls:
            msg_dict["tool_calls"] = [
                tc.model_dump() for tc in message.tool_calls
            ]
        messages.append(msg_dict)

        # 没有 tool_calls，直接输出最终回复
        if not message.tool_calls:
            print(f"\nAI: {message.content}")
            return

        # 逐一执行每个 tool_call
        for call in message.tool_calls:
            func_name = call.function.name
            args = json.loads(call.function.arguments)
            print(f"\n调用工具: {func_name}({json.dumps(args, ensure_ascii=False)})")

            uri = tool_to_uri.get(func_name)
            if not uri:
                raise ValueError(f"未知的工具函数: {func_name}")

            result = await self.call_tool(uri, func_name, args)
            print(f"工具返回: {result[:200]}...")

            # 将工具执行结果以 role="tool" 返回
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,  # 必须严格对齐
                "content": result
            })

        # 再次请求模型，传入工具结果
        next_resp = await self.openai.chat.completions.create(
            model="kimi-k2.6",
            messages=messages,
            tools=all_tools
        )
        await self.handle_response(next_resp, messages, all_tools, tool_to_uri)

    async def chat(self, user_message: str, formulas: list[str]):
        """启动一次支持 Formula 工具的对话

        Args:
            user_message: 用户输入
            formulas: 要启用的 Formula URI 列表
        """
        # 1. 获取所有 Formula 的工具定义
        all_tools = []
        tool_to_uri = {}
        for uri in formulas:
            tools = await self.get_tools(uri)
            for tool in tools:
                tool_name = tool["function"]["name"]
                tool_to_uri[tool_name] = uri
            all_tools.extend(tools)

        print(f"已加载 {len(all_tools)} 个工具: {list(tool_to_uri.keys())}")

        # 2. 构建初始对话
        messages = [{"role": "user", "content": user_message}]

        # 3. 首次请求模型
        response = await self.openai.chat.completions.create(
            model="kimi-k2.6",
            messages=messages,
            tools=all_tools
        )

        # 4. 递归处理响应（自动执行工具调用链）
        await self.handle_response(response, messages, all_tools, tool_to_uri)


# ========== 使用示例 ==========
async def main():
    client = FormulaChatClient(
        base_url="https://api.moonshot.cn/v1",
        api_key=os.environ["MOONSHOT_API_KEY"]
    )
    await client.chat(
        user_message="今天有什么科技新闻？",
        formulas=["moonshot/web-search:latest", "moonshot/fetch:latest"]
    )


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 第二部分：web-search 工具详解

### 2.1 功能描述

`web-search` 是 Kimi 开放平台提供的**实时联网搜索工具**，允许模型在对话过程中主动检索互联网上的最新信息。与模型训练数据的静态知识不同，`web-search` 能够获取实时新闻、最新事件、当前价格、赛事结果等时效性信息，有效突破大模型的知识截止日期限制。

### 2.2 Formula URI

```
moonshot/web-search:latest
```

### 2.3 工具定义（JSON Schema）

通过 `GET /formulas/moonshot/web-search:latest/tools` 获取到的工具定义如下：

```json
{
  "type": "function",
  "function": {
    "name": "web_search",
    "description": "Search the web for information",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "description": "What to search for",
          "type": "string"
        }
      },
      "required": ["query"]
    }
  }
}
```

**参数说明**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | `string` | 是 | 搜索查询语句，支持自然语言描述。模型会根据对话上下文自动生成合适的查询 |

### 2.4 关键特性

| 特性 | 说明 |
|------|------|
| **Protected 工具** | `web-search` 属于 protected 工具，执行结果在 `context.encrypted_output` 中返回。加密内容由 Kimi 平台自动处理，开发者只需原样传回即可 |
| **独立计费** | 每次调用 `web_search` 额外收取 ¥0.03（不含 tokens 费用）。搜索结果通常消耗 5,000–10,000 tokens |
| **禁用 thinking 模式** | 使用 `web-search` 时必须在请求中禁用 reasoning/thinking 模式（`reasoning_effort="none"`），否则工具调用会报错 |
| **查询智能生成** | 模型会根据用户问题和对话上下文自动构造搜索 query，无需人工干预 |

### 2.5 与旧版 `$web_search` 内置函数的对比

| 对比项 | 旧版 `$web_search` | 新版 `web-search` Formula |
|--------|-------------------|--------------------------|
| 工具类型 | `type: "builtin_function"` | `type: "function"` |
| 函数名称 | `name: "$web_search"` | `name: "web_search"` |
| 集成方式 | 内置函数，直接在 Chat API 中启用 | 通过 Formula 机制独立获取和执行 |
| 组合能力 | 不可与其他内置工具组合 | 可与其他 Formula 工具（如 `fetch`）自由组合 |
| 执行控制 | 由平台自动代理执行 | 开发者显式调用 `/fibers` 执行，完全可控 |
| 灵活性 | 低，黑盒式调用 | 高，可在工具执行前后插入自定义逻辑 |

**建议**：新项目直接使用 Formula 方式的 `web-search`，旧版 `$web_search` 已处于维护模式。

### 2.6 完整调用代码示例

以下示例展示如何在 Chat Completions 流程中集成 `web-search`：

```python
import os
import json
import httpx
from openai import AsyncOpenAI


async def web_search_chat():
    """使用 web-search Formula 进行联网搜索对话"""

    base_url = "https://api.moonshot.cn/v1"
    api_key = os.environ["MOONSHOT_API_KEY"]
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    http = httpx.AsyncClient(timeout=60)

    # 步骤 1: 获取 web-search 工具定义
    resp = await http.get(
        f"{base_url}/formulas/moonshot/web-search:latest/tools",
        headers={"Authorization": f"Bearer {api_key}"}
    )
    tools = resp.json()["tools"]
    print(f"获取到工具定义: {tools[0]['function']['name']}")

    # 步骤 2: 发起对话（必须禁用 thinking 模式）
    messages = [
        {"role": "user", "content": "帮我搜索 Kimi K2.6 模型的最新发布信息"}
    ]

    response = await client.chat.completions.create(
        model="kimi-k2.6",
        messages=messages,
        tools=tools,
        # 注意：使用 web-search 时必须禁用 thinking 模式
        reasoning_effort="none"
    )

    message = response.choices[0].message

    # 步骤 3: 检查模型是否请求调用工具
    if message.tool_calls:
        # 将 assistant 的 tool_calls 消息追加到历史
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [tc.model_dump() for tc in message.tool_calls]
        })

        # 执行每个 tool_call
        for call in message.tool_calls:
            func_name = call.function.name
            args = json.loads(call.function.arguments)
            print(f"执行工具: {func_name}(query='{args['query']}')")

            # 通过 /fibers 端点执行搜索
            exec_resp = await http.post(
                f"{base_url}/formulas/moonshot/web-search:latest/fibers",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "name": func_name,
                    "arguments": json.dumps(args)
                }
            )
            result = exec_resp.json()

            # Protected 工具结果在 encrypted_output 中
            output = result["context"].get("encrypted_output") \
                     or result["context"].get("output", "")

            # 将结果以 role="tool" 返回模型
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output
            })

        # 步骤 4: 再次请求模型，传入搜索结果
        final_resp = await client.chat.completions.create(
            model="kimi-k2.6",
            messages=messages,
            tools=tools,
            reasoning_effort="none"
        )

        final_msg = final_resp.choices[0].message
        print(f"\nAI: {final_msg.content}")
        return final_msg.content
    else:
        # 模型直接回复，未使用工具
        print(f"\nAI: {message.content}")
        return message.content


# 运行
# asyncio.run(web_search_chat())
```

### 2.7 使用场景

| 场景 | 示例 |
|------|------|
| **实时信息查询** | "今天北京的天气怎么样？" "现在美元兑人民币汇率是多少？" |
| **新闻检索** | "最近一周 AI 领域有什么重要新闻？" |
| **事实核查** | "验证这个说法：'Python 3.14 将在 2025 年发布'" |
| **时效性知识** | "2024 年诺贝尔文学奖得主是谁？" |
| **产品/服务调研** | "对比 Kimi、GPT-4、Claude 3 的价格和功能" |

### 2.8 注意事项

1. **Token 消耗**：搜索结果通常占用 5,000–10,000 tokens，大量使用会显著增加 API 费用。建议仅在必要时启用，或通过系统提示限制模型的搜索频率
2. **计费说明**：每次 `web_search` 调用固定收费 ¥0.03（不含 tokens 费用），与搜索结果页数无关
3. **Thinking 模式冲突**：`web-search` 与模型的 thinking/reasoning 模式不兼容，请求中必须设置 `reasoning_effort="none"`
4. **Query 质量**：模型自动生成的搜索 query 通常效果良好，但复杂场景下可通过系统提示引导模型优化 query 构造策略
5. **结果加密**：`encrypted_output` 的加密内容可直接返回模型，切勿尝试解密或修改

---

## 第三部分：fetch 工具详解

### 3.1 功能描述

`fetch` 是 Kimi 开放平台提供的 **URL 内容提取工具**，能够将指定网页的内容抓取并转换为结构化的 Markdown 格式。与 `web-search` 配合使用时，可先搜索获取相关网页链接，再通过 `fetch` 提取详细内容，实现"搜索 + 精读"的完整信息获取流程。

### 3.2 Formula URI

```
moonshot/fetch:latest
```

### 3.3 工具定义（JSON Schema）

```json
{
  "type": "function",
  "function": {
    "name": "fetch",
    "description": "Fetch the content of a URL and convert it to markdown",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {
          "description": "The URL to fetch",
          "type": "string"
        }
      },
      "required": ["url"]
    }
  }
}
```

**参数说明**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | `string` | 是 | 需要抓取的网页完整 URL，需包含协议前缀（`http://` 或 `https://`） |

### 3.4 完整调用代码示例

以下示例展示 `fetch` 工具的独立调用及与 Chat Completions 的集成：

```python
import os
import json
import httpx
from openai import AsyncOpenAI


async def fetch_demo():
    """fetch 工具独立调用示例"""

    base_url = "https://api.moonshot.cn/v1"
    api_key = os.environ["MOONSHOT_API_KEY"]
    http = httpx.AsyncClient(timeout=60)

    # 直接调用 fetch 工具
    resp = await http.post(
        f"{base_url}/formulas/moonshot/fetch:latest/fibers",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json={
            "name": "fetch",
            "arguments": json.dumps({
                "url": "https://platform.moonshot.cn/docs/intro"
            })
        }
    )
    result = resp.json()
    markdown_content = result["context"].get("output", "")
    print(f"获取内容长度: {len(markdown_content)} 字符")
    print(markdown_content[:1000])


async def fetch_with_chat():
    """fetch 工具与 Chat Completions 集成示例"""

    base_url = "https://api.moonshot.cn/v1"
    api_key = os.environ["MOONSHOT_API_KEY"]
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    http = httpx.AsyncClient(timeout=60)

    # 获取 fetch 工具定义
    resp = await http.get(
        f"{base_url}/formulas/moonshot/fetch:latest/tools",
        headers={"Authorization": f"Bearer {api_key}"}
    )
    fetch_tools = resp.json()["tools"]

    # 对话：让模型抓取并总结网页
    messages = [
        {"role": "user", "content": "请抓取 https://platform.moonshot.cn/docs 并总结主要内容"}
    ]

    response = await client.chat.completions.create(
        model="kimi-k2.6",
        messages=messages,
        tools=fetch_tools
    )

    message = response.choices[0].message

    if message.tool_calls:
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [tc.model_dump() for tc in message.tool_calls]
        })

        for call in message.tool_calls:
            args = json.loads(call.function.arguments)
            print(f"抓取 URL: {args['url']}")

            exec_resp = await http.post(
                f"{base_url}/formulas/moonshot/fetch:latest/fibers",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "name": call.function.name,
                    "arguments": json.dumps(args)
                }
            )
            result = exec_resp.json()
            content = result["context"].get("output", "")

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": content
            })

        # 获取最终总结
        final = await client.chat.completions.create(
            model="kimi-k2.6",
            messages=messages,
            tools=fetch_tools
        )
        print(f"\nAI 总结: {final.choices[0].message.content}")


# 运行
# asyncio.run(fetch_demo())
# asyncio.run(fetch_with_chat())
```

### 3.5 返回值说明

`fetch` 工具执行成功后，返回内容位于 `context.output` 中，格式为 **Markdown 纯文本**，包含以下特征：

- 保留原文的标题层级（`#`、`##` 等）
- 保留段落、列表、代码块等 Markdown 结构
- 移除 CSS、JavaScript 等无关元素
- 图片保留为 Markdown 图片链接格式 `![alt](url)`
- 超链接保留为 Markdown 链接格式 `[text](url)`

**示例返回值**（截取）：

```markdown
# Kimi 开放平台文档

## 快速开始

Kimi 开放平台提供了一系列 API，帮助开发者将 Kimi 大语言模型集成到自己的应用中。

### 获取 API Key

1. 登录 [Kimi 开放平台](https://platform.moonshot.cn)
2. 进入「账户设置」页面
3. 点击「新建 API Key」

### 安装 SDK

```bash
pip install openai
```
```

### 3.6 与 web-search 的协同使用

`fetch` 与 `web-search` 是天然的最佳拍档，典型协同流程如下：

```python
async def search_and_fetch(client, http, api_key):
    """搜索 + 抓取的协同示例"""

    base_url = "https://api.moonshot.cn/v1"

    # 同时获取两个工具的定义
    ws_resp = await http.get(
        f"{base_url}/formulas/moonshot/web-search:latest/tools",
        headers={"Authorization": f"Bearer {api_key}"}
    )
    f_resp = await http.get(
        f"{base_url}/formulas/moonshot/fetch:latest/tools",
        headers={"Authorization": f"Bearer {api_key}"}
    )

    all_tools = ws_resp.json()["tools"] + f_resp.json()["tools"]
    tool_to_uri = {
        "web_search": "moonshot/web-search:latest",
        "fetch": "moonshot/fetch:latest"
    }

    messages = [{"role": "user", "content": "搜索 Kimi K2.6 的发布信息，然后抓取官方文档详细阅读"}]

    # 模型会自动决定：先调用 web_search 搜索，再调用 fetch 抓取具体页面
    response = await client.chat.completions.create(
        model="kimi-k2.6",
        messages=messages,
        tools=all_tools,
        reasoning_effort="none"  # 使用 web-search 必须禁用 thinking
    )

    # ... 处理 tool_calls（同前文 FormulaChatClient 的处理逻辑）
```

### 3.7 使用场景

| 场景 | 说明 |
|------|------|
| **网页内容提取** | 将任意网页转为 Markdown，便于后续文本分析或存储 |
| **文章摘要** | 抓取长文后让模型生成摘要 |
| **数据抓取** | 提取结构化页面中的表格、列表等数据 |
| **竞品调研** | 批量抓取竞品页面内容进行分析 |
| **文档同步** | 将外部文档抓取后同步到知识库 |

### 3.8 注意事项

1. **反爬机制**：部分网站配置了反爬虫策略（如 Cloudflare 验证、User-Agent 检测、Rate Limiting），`fetch` 工具可能无法成功抓取。对于这类网站，建议引导用户手动提供内容
2. **Token 消耗**：大页面（如包含大量代码或表格的文档）转换后的 Markdown 可能非常长，消耗的 tokens 较多。可在抓取前预估页面大小，必要时分页处理
3. **动态内容限制**：`fetch` 工具抓取的是静态 HTML，对于需要 JavaScript 渲染的单页应用（SPA）或动态加载内容，可能无法获取完整内容
4. **URL 格式**：传入的 URL 必须包含协议前缀（`http://` 或 `https://`），不支持相对路径
5. **配合使用效果更佳**：单独使用 `fetch` 需要预先知道目标 URL，与 `web-search` 配合可实现"发现—抓取—分析"的完整链路

---

## 附录：快速参考卡

| 工具 | Formula URI | 函数名 | 结果位置 | 额外计费 |
|------|-------------|--------|----------|----------|
| 联网搜索 | `moonshot/web-search:latest` | `web_search` | `context.encrypted_output` | ¥0.03/次 |
| 网页抓取 | `moonshot/fetch:latest` | `fetch` | `context.output` | 免费 |

| 通用端点 | URL 模板 |
|----------|----------|
| 获取工具定义 | `GET {BASE_URL}/formulas/{URI}/tools` |
| 执行工具 | `POST {BASE_URL}/formulas/{URI}/fibers` |

| 关键限制 | 说明 |
|----------|------|
| function.name 唯一性 | 同请求内不可重复 |
| tool_call_id 对齐 | `role="tool"` 消息的 `tool_call_id` 必须与 `tool_calls[].id` 一致 |
| 全量返回 | 必须处理所有 `tool_calls`，不可遗漏 |
| web-search 需禁用 thinking | `reasoning_effort="none"` |

# 第二节 代码执行类工具详解

> **本章工具清单**：`code_runner`（Python 代码执行）、`quickjs`（JavaScript 代码执行）
>
> **适用场景**：数学计算、数据分析、算法验证、JSON 处理、自动化脚本等需要程序化解决问题的场景

---

## 1. code_runner —— Python 代码执行工具

### 1.1 功能描述

`code_runner` 是 Kimi 开放平台提供的 **Python 代码执行工具**，允许大语言模型在安全的沙箱环境中运行 Python 代码并获取执行结果。该工具解决了 LLM 在复杂计算、数据分析、算法验证等场景中"只能推理、无法执行"的局限，使模型能够：**编写代码 → 执行验证 → 基于结果继续推理**，形成完整的"思考-执行-反馈"闭环。

通过 code_runner，模型可以执行数学运算、处理结构化数据、调用 Python 标准库与常用第三方库（如 NumPy、pandas 等），将 Python 丰富的生态能力融入对话流程。

### 1.2 Formula URI

```
moonshot/code_runner:latest
```

### 1.3 工具定义注册

通过 OpenAI SDK 获取并注册工具定义：

```python
import os
import httpx

async def get_code_runner_tool():
    http = httpx.AsyncClient()
    formula_uri = "moonshot/code_runner:latest"

    resp = await http.get(
        f"https://api.moonshot.cn/v1/formulas/{formula_uri}/tools",
        headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
    )
    tool_def = resp.json()["tools"][0]
    return tool_def
```

**返回的工具定义结构**（示例，以实际返回为准）：

```json
{
  "type": "function",
  "function": {
    "name": "code_runner",
    "description": "Execute Python code in a sandboxed environment and return the output.",
    "parameters": {
      "type": "object",
      "properties": {
        "code": {
          "type": "string",
          "description": "The Python code to execute."
        }
      },
      "required": ["code"]
    }
  }
}
```

### 1.4 参数说明

| 参数名 | 类型 | 必填 | 描述 |
|:---:|:---:|:---:|:---|
| `code` | `string` | 是 | 待执行的 Python 源代码。可包含多行代码、函数定义、import 语句等。代码将在沙箱环境中以独立进程运行，标准输出（stdout）和标准错误（stderr）的内容将被捕获并返回。 |

> **参数来源说明**：上表参数基于 Kimi 开放平台工具设计惯例推断，实际参数名和约束以 `/formulas/moonshot/code_runner:latest/tools` 接口返回的 JSON Schema 为准。

### 1.5 核心特性

| 特性 | 说明 |
|:---|:---|
| **安全沙箱执行** | 代码在隔离的容器化环境中运行，与宿主机系统隔离，防止恶意代码对平台造成损害。每次执行均为独立进程，执行结束后环境即销毁。 |
| **标准库支持** | 完整支持 Python 标准库，包括 `math`、`random`、`datetime`、`json`、`re`、`itertools`、`collections`、`statistics` 等常用模块。 |
| **第三方库支持** | 预装了常用科学计算和数据处理库，如 **NumPy**（数值计算）、**pandas**（数据分析）、**matplotlib**（绘图）等，可直接 import 使用。 |
| **输出捕获** | 自动捕获代码的标准输出（`print` 输出）和标准错误（异常信息），以字符串形式返回给模型。 |
| **执行超时保护** | 设有执行时间上限（通常为数十秒），超时将被强制终止，防止资源占用。 |
| **内存限制** | 沙箱环境对内存使用设有上限，防止代码消耗过多资源。 |

### 1.6 完整调用代码示例

以下示例展示从获取工具定义到最终获取模型回复的完整流程，包括素数判断的数学计算场景：

```python
import os
import json
import httpx
from openai import AsyncOpenAI


async def use_code_runner():
    # 初始化客户端
    client = AsyncOpenAI(
        api_key=os.environ["MOONSHOT_API_KEY"],
        base_url="https://api.moonshot.cn/v1"
    )
    http = httpx.AsyncClient()

    formula_uri = "moonshot/code_runner:latest"

    # 步骤 1：获取 code_runner 的工具定义
    resp = await http.get(
        f"https://api.moonshot.cn/v1/formulas/{formula_uri}/tools",
        headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
    )
    tool_def = resp.json()["tools"][0]

    # 步骤 2：发起 Chat Completions 请求，携带工具定义
    messages = [
        {"role": "system", "content": "你是一个数学助手，擅长使用 Python 工具解决计算问题。"},
        {"role": "user", "content": "计算 3214567 是不是素数？请详细说明判断过程。"}
    ]

    completion = await client.chat.completions.create(
        model="kimi-k2-0711-preview",
        messages=messages,
        tools=[tool_def]
    )

    # 步骤 3：检查模型是否请求调用工具
    choice = completion.choices[0]

    if choice.finish_reason == "tool_calls":
        # 记录模型请求工具调用的消息
        messages.append(choice.message)

        for tc in choice.message.tool_calls:
            # 解析模型传递的参数
            args = json.loads(tc.function.arguments)
            code_to_run = args.get("code", "")

            print(f"[模型生成代码]\n{code_to_run}\n")

            # 步骤 4：通过 fibers 接口执行工具
            exec_resp = await http.post(
                f"https://api.moonshot.cn/v1/formulas/{formula_uri}/fibers",
                headers={
                    "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}",
                    "Content-Type": "application/json"
                },
                json={
                    "name": tc.function.name,
                    "arguments": json.dumps(args)
                }
            )
            result = exec_resp.json()

            # 提取执行输出
            output = result.get("context", {}).get("output", "")
            print(f"[执行输出]\n{output}\n")

            # 步骤 5：将执行结果以 role="tool" 返回给模型
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(output)
            })

        # 步骤 6：携带工具执行结果，再次请求模型生成最终回复
        final = await client.chat.completions.create(
            model="kimi-k2-0711-preview",
            messages=messages,
            tools=[tool_def]
        )

        print(f"[最终回复]\n{final.choices[0].message.content}")
        return final.choices[0].message.content

    else:
        # 模型未请求工具，直接返回回复内容
        print(f"[直接回复]\n{choice.message.content}")
        return choice.message.content


# 运行入口
if __name__ == "__main__":
    import asyncio
    asyncio.run(use_code_runner())
```

**示例中模型可能生成的代码**：

```python
def is_prime(n):
    if n < 2:
        return False
    if n in (2, 3):
        return True
    if n % 2 == 0:
        return False
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False, i
        i += 2
    return True, None

n = 3214567
result, factor = is_prime(n)
if result:
    print(f"{n} 是素数")
else:
    print(f"{n} 不是素数，最小因子为 {factor}")
```

### 1.7 使用场景

| 场景 | 示例描述 |
|:---|:---|
| **数学计算与验证** | 判断大数素性、求解方程、计算积分、矩阵运算、统计量计算等需要精确数值结果的场景。 |
| **数据分析与处理** | 使用 pandas 处理 CSV/JSON 数据、数据清洗、分组聚合、生成描述性统计信息。 |
| **算法原型测试** | 快速验证算法逻辑正确性，如排序算法、搜索算法、动态规划等，观察实际运行结果。 |
| **文件格式转换** | 读取一种格式数据（如 JSON），转换为另一种格式（如 CSV），或进行编码/解码操作。 |
| **科学计算** | 使用 NumPy 进行数值模拟、使用 matplotlib 生成数据可视化图表（支持 base64 输出嵌入对话）。 |
| **自动化脚本** | 批量处理文本、正则匹配提取信息、日期格式转换、URL 编解码等常见编程任务。 |

### 1.8 注意事项

| 注意事项 | 详细说明 |
|:---|:---|
| **执行时间限制** | 沙箱环境设有执行超时（通常为 30~60 秒），复杂计算应控制算法时间复杂度，避免无限循环或长时间阻塞操作。 |
| **网络访问受限** | 沙箱内通常禁止或限制出站网络连接，代码中不应依赖下载外部资源、调用外部 API 等网络操作。 |
| **敏感操作禁止** | 文件系统写操作受到严格限制，禁止删除系统文件、访问宿主机敏感路径等危险操作。仅允许在指定工作目录内进行安全的文件读写。 |
| **结果需验证** | 模型生成的代码可能存在逻辑错误，建议在实际生产应用中对关键计算结果进行复核验证，不可直接用于金融、医疗等高精度要求的场景。 |
| **第三方库限制** | 虽然预装了常用库，但并非 PyPI 全部库都可用。如需特定第三方库，建议先验证可用性或使用标准库实现替代方案。 |
| **状态不保留** | 每次 code_runner 调用均为独立的沙箱实例，前一次调用中定义的变量、导入的模块、创建的文件在后续调用中不可见。若需保持状态，应将上下文通过参数传递。 |
| **错误处理** | 代码抛出异常时，异常信息（traceback）将返回在输出中。模型可根据错误信息修正代码后重新执行，形成自我调试循环。 |

---

## 2. quickjs —— JavaScript 代码执行工具

### 2.1 功能描述

`quickjs` 是 Kimi 开放平台提供的 **JavaScript 代码执行工具**，基于 [QuickJS](https://bellard.org/quickjs/) 引擎实现。QuickJS 是由 Fabrice Bellard 开发的高性能 JavaScript 引擎，以轻量、启动快、支持 ES2020+ 语法著称。

该工具使模型能够在安全沙箱中运行 JavaScript 代码，适合前端逻辑验证、JSON 数据处理、正则表达式测试、轻量级算法实现等场景。作为 code_runner 的补充，quickjs 为偏好 JavaScript 语法的开发者或处理 JS 生态相关任务提供了原生支持。

### 2.2 Formula URI

```
moonshot/quickjs:latest
```

### 2.3 工具定义注册

```python
import os
import httpx

async def get_quickjs_tool():
    http = httpx.AsyncClient()
    formula_uri = "moonshot/quickjs:latest"

    resp = await http.get(
        f"https://api.moonshot.cn/v1/formulas/{formula_uri}/tools",
        headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
    )
    tool_def = resp.json()["tools"][0]
    return tool_def
```

**返回的工具定义结构**（示例，以实际返回为准）：

```json
{
  "type": "function",
  "function": {
    "name": "quickjs",
    "description": "Execute JavaScript code using the QuickJS engine in a sandboxed environment.",
    "parameters": {
      "type": "object",
      "properties": {
        "code": {
          "type": "string",
          "description": "The JavaScript code to execute."
        }
      },
      "required": ["code"]
    }
  }
}
```

### 2.4 参数说明

| 参数名 | 类型 | 必填 | 描述 |
|:---:|:---:|:---:|:---|
| `code` | `string` | 是 | 待执行的 JavaScript 源代码。支持 ES2020+ 语法，包括箭头函数、解构赋值、模板字符串、async/await、可选链操作符（`?.`）等现代特性。代码在 QuickJS 沙箱中运行，`console.log` 的输出将被捕获并返回。 |

> **参数来源说明**：上表参数基于 Kimi 开放平台工具设计惯例推断，实际参数名和约束以 `/formulas/moonshot/quickjs:latest/tools` 接口返回的 JSON Schema 为准。

### 2.5 核心特性

| 特性 | 说明 |
|:---|:---|
| **QuickJS 引擎** | 采用 QuickJS 引擎，具有启动速度快、内存占用低的特点，适合短脚本快速执行场景。 |
| **ES2020+ 语法支持** | 完整支持现代 JavaScript 语法特性，包括 Class、Promise、async/await、展开运算符、解构赋值、可选链（`?.`）、空值合并（`??`）等。 |
| **安全沙箱执行** | 与 code_runner 相同，代码在隔离环境中运行，禁止访问底层系统资源，确保平台安全性。 |
| **输出捕获** | 捕获 `console.log`、`console.error`、`console.warn` 等标准输出，以字符串形式返回。 |
| **轻量高效** | 相比完整 Node.js 运行时，QuickJS 更加轻量，冷启动时间极短，适合高频短脚本调用。 |
| **内置标准对象** | 支持 JavaScript 内置对象，如 `Math`、`JSON`、`Date`、`Array`、`Object`、`Map`、`Set`、`Proxy`、`Reflect` 等。 |

### 2.6 完整调用代码示例

以下示例展示使用 quickjs 进行 JSON 数据处理和正则表达式验证的完整流程：

```python
import os
import json
import httpx
from openai import AsyncOpenAI


async def use_quickjs():
    # 初始化客户端
    client = AsyncOpenAI(
        api_key=os.environ["MOONSHOT_API_KEY"],
        base_url="https://api.moonshot.cn/v1"
    )
    http = httpx.AsyncClient()

    formula_uri = "moonshot/quickjs:latest"

    # 步骤 1：获取 quickjs 的工具定义
    resp = await http.get(
        f"https://api.moonshot.cn/v1/formulas/{formula_uri}/tools",
        headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
    )
    tool_def = resp.json()["tools"][0]

    # 步骤 2：发起 Chat Completions 请求
    messages = [
        {"role": "system", "content": "你是一个数据助手，擅长使用 JavaScript 工具处理 JSON 和文本数据。"},
        {"role": "user", "content": """请将以下 JSON 数组按 age 字段降序排序，并提取所有 email 字段验证格式是否正确：
[
    {"name": "Alice", "age": 25, "email": "alice@example.com"},
    {"name": "Bob", "age": 30, "email": "bob@invalid"},
    {"name": "Carol", "age": 22, "email": "carol@test.org"}
]"""}
    ]

    completion = await client.chat.completions.create(
        model="kimi-k2-0711-preview",
        messages=messages,
        tools=[tool_def]
    )

    # 步骤 3：处理 tool_calls
    choice = completion.choices[0]

    if choice.finish_reason == "tool_calls":
        messages.append(choice.message)

        for tc in choice.message.tool_calls:
            args = json.loads(tc.function.arguments)
            code_to_run = args.get("code", "")

            print(f"[模型生成代码]\n{code_to_run}\n")

            # 步骤 4：执行工具
            exec_resp = await http.post(
                f"https://api.moonshot.cn/v1/formulas/{formula_uri}/fibers",
                headers={
                    "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}",
                    "Content-Type": "application/json"
                },
                json={
                    "name": tc.function.name,
                    "arguments": json.dumps(args)
                }
            )
            result = exec_resp.json()
            output = result.get("context", {}).get("output", "")

            print(f"[执行输出]\n{output}\n")

            # 步骤 5：返回结果给模型
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(output)
            })

        # 步骤 6：获取最终回复
        final = await client.chat.completions.create(
            model="kimi-k2-0711-preview",
            messages=messages,
            tools=[tool_def]
        )

        print(f"[最终回复]\n{final.choices[0].message.content}")
        return final.choices[0].message.content

    else:
        print(f"[直接回复]\n{choice.message.content}")
        return choice.message.content


# 运行入口
if __name__ == "__main__":
    import asyncio
    asyncio.run(use_quickjs())
```

**示例中模型可能生成的代码**：

```javascript
const data = [
    {name: "Alice", age: 25, email: "alice@example.com"},
    {name: "Bob", age: 30, email: "bob@invalid"},
    {name: "Carol", age: 22, email: "carol@test.org"}
];

// 按 age 降序排序
const sorted = [...data].sort((a, b) => b.age - a.age);
console.log("按 age 降序排序结果：");
console.log(JSON.stringify(sorted, null, 2));

// 验证 email 格式
const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
console.log("\nEmail 格式验证：");
data.forEach(item => {
    const isValid = emailRegex.test(item.email);
    console.log(`${item.name}: ${item.email} -> ${isValid ? "有效" : "无效"}`);
});
```

### 2.7 使用场景

| 场景 | 示例描述 |
|:---|:---|
| **JavaScript 逻辑验证** | 验证前端代码逻辑、测试函数行为、确认 ES6+ 语法特性执行结果，帮助开发者快速检验代码片段。 |
| **JSON 数据处理** | 使用 JavaScript 的 `JSON.parse`/`JSON.stringify` 和数组方法（`map`、`filter`、`reduce`、`sort`）处理 JSON 数据，进行格式转换、字段提取、数据聚合。 |
| **正则表达式测试** | 利用 JavaScript 正则引擎测试和验证正则表达式的匹配结果，包括复杂的多行匹配、捕获组提取等。 |
| **简单算法实现** | 实现轻量级算法，如数组去重、字符串处理、日期计算、简单排序等，利用 JS 灵活的语法快速求解。 |
| **前端数据模拟** | 生成模拟数据、计算数据聚合结果、验证数据转换逻辑，辅助前端开发调试。 |
| **与 Python 协作** | 在涉及 JavaScript 特定语法或前端代码生成的场景中，作为 code_runner 的互补工具使用。 |

### 2.8 注意事项

| 注意事项 | 详细说明 |
|:---|:---|
| **不支持 DOM/BOM API** | quickjs 为纯 JavaScript 引擎，不包含浏览器环境。`document`、`window`、`navigator`、`fetch`、`localStorage` 等浏览器 API 不可用。 |
| **不支持 Node.js 内置模块** | `fs`、`path`、`http`、`crypto` 等 Node.js 模块不可用。仅能使用 JavaScript 语言内置对象和标准库（`Math`、`JSON`、`Date` 等）。 |
| **网络访问受限** | 沙箱环境限制网络请求，代码中不可使用 `fetch`、`XMLHttpRequest` 或创建网络连接。 |
| **执行时间限制** | 设有执行超时保护（通常为数秒至数十秒），复杂计算应优化算法效率，避免超时。 |
| **与 code_runner 的差异** | quickjs 更轻量、启动更快，适合短脚本和 JS 特有任务；code_runner 生态更丰富（pandas、numpy 等），适合数据科学和复杂计算。根据任务类型选择合适工具。 |
| **console 输出捕获** | 与 Python 的 `print` 不同，quickjs 通过 `console.log` 输出内容。确保代码中使用 `console.log()` 而非浏览器特有的 `alert()` 等 API。 |
| **模块系统限制** | QuickJS 支持 ES Module 语法（`import`/`export`），但无法从 npm 加载外部包。所有代码应以内联方式实现，不依赖外部模块。 |

---

## 3. 工具对比与选型指南

| 对比维度 | code_runner | quickjs |
|:---:|:---|:---|
| **执行语言** | Python 3.x | JavaScript (ES2020+) |
| **底层引擎** | Python 解释器 | QuickJS 引擎 |
| **Formula URI** | `moonshot/code_runner:latest` | `moonshot/quickjs:latest` |
| **参数** | `code`（Python 代码） | `code`（JavaScript 代码） |
| **标准库支持** | Python 完整标准库 | JavaScript 内置对象（Math、JSON、Date 等） |
| **第三方库** | NumPy、pandas、matplotlib 等 | 不支持外部 npm 包 |
| **启动速度** | 中等（需加载 Python 运行时） | 极快（QuickJS 轻量设计） |
| **适用任务** | 数据分析、科学计算、复杂算法 | JSON 处理、正则测试、前端逻辑验证 |
| **DOM/BOM** | 不适用 | 不支持 |
| **网络访问** | 受限 | 受限 |
| **执行超时** | 约 30~60 秒 | 约数秒至数十秒 |
| **输出方式** | `print()` → stdout 捕获 | `console.log()` → 输出捕获 |
| **内存限制** | 有 | 有 |

### 选型建议

- **数据分析、科学计算、复杂数学运算** → 选择 `code_runner`，利用 Python 丰富的生态和强大的数值计算能力。
- **JSON 处理、正则验证、前端代码测试、轻量级脚本** → 选择 `quickjs`，享受更快的启动速度和 JavaScript 灵活的语法特性。
- **任务不明确时** → 优先使用 `code_runner`，其第三方库支持使其覆盖范围更广；若模型自行选择了 quickjs，也应尊重其判断。

---

## 4. 代码执行类工具最佳实践

### 4.1 安全性实践

```python
# 在将用户输入传递给 code 参数前，进行基本的输入校验
def sanitize_code_input(user_input: str) -> str:
    """
    简单的安全检查：禁止明显的危险操作。
    生产环境中应使用更严格的沙箱策略或代码签名机制。
    """
    dangerous_patterns = [
        "__import__('os')",
        "subprocess",
        "eval(",
        "exec(",
    ]
    for pattern in dangerous_patterns:
        if pattern in user_input:
            raise ValueError(f"检测到潜在危险代码模式: {pattern}")
    return user_input
```

### 4.2 多工具共存时的 name 映射

当同时使用多个 Formula 工具时，需维护 `function.name` → `formula_uri` 的映射关系：

```python
# 多工具 name 映射管理
FORMULA_REGISTRY = {
    "code_runner": "moonshot/code_runner:latest",
    "quickjs": "moonshot/quickjs:latest",
    "web_search": "moonshot/web-search:latest",
    "browser": "moonshot/browser:latest",
}

async def execute_tool(tool_call):
    """根据 tool_call 中的 function.name 路由到对应的 Formula URI"""
    tool_name = tool_call.function.name
    formula_uri = FORMULA_REGISTRY.get(tool_name)

    if not formula_uri:
        raise ValueError(f"未知工具: {tool_name}")

    resp = await http.post(
        f"https://api.moonshot.cn/v1/formulas/{formula_uri}/fibers",
        headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}",
                 "Content-Type": "application/json"},
        json={
            "name": tool_name,
            "arguments": tool_call.function.arguments
        }
    )
    return resp.json()
```

### 4.3 错误处理与重试

```python
async def execute_with_retry(http, formula_uri, tool_name, arguments, max_retries=2):
    """带重试机制的工具执行"""
    for attempt in range(max_retries + 1):
        try:
            resp = await http.post(
                f"https://api.moonshot.cn/v1/formulas/{formula_uri}/fibers",
                headers={
                    "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}",
                    "Content-Type": "application/json"
                },
                json={"name": tool_name, "arguments": arguments},
                timeout=60.0
            )
            result = resp.json()

            # 检查是否有错误
            if "error" in result:
                raise Exception(f"工具执行错误: {result['error']}")

            return result

        except Exception as e:
            if attempt == max_retries:
                raise
            print(f"第 {attempt + 1} 次执行失败，正在重试: {e}")
            await asyncio.sleep(1)
```

### 4.4 状态管理注意

代码执行工具为**无状态设计**，每次调用都是独立的沙箱实例：

```python
# ❌ 错误示例：试图在两次调用间保持状态
first_call = {"code": "x = 100\nprint(x)"}
# 第二次调用无法访问变量 x
second_call = {"code": "print(x + 1)"}  # NameError: x is not defined

# ✅ 正确做法：将状态作为代码的一部分传递
combined_code = """
x = 100
print(f"初始值: {x}")
# 继续处理
result = x + 1
print(f"结果: {result}")
"""
```

---

> **文档版本**：v1.0
> **更新日期**：2025年7月
> **适用范围**：Kimi 开放平台 Formula 工具集

# 第三节 数据转换类工具

> 本章介绍 Kimi 开放平台提供的三个数据转换类内置工具：`convert`（单位转换）、`date`（日期时间处理）和 `base64`（Base64 编解码）。这些工具专注于解决开发过程中常见的数据格式转换与计算问题，可直接通过 Formula 机制接入 Chat Completions 流程。

---

## 3.1 convert — 单位转换工具

### 3.1.1 功能描述

`convert` 是一款通用单位转换工具，支持长度、质量、体积、温度、面积、时间、能量、压力、速度和货币共 **10 大类**单位的相互换算。无论是科学计算中的物理量转换，还是日常应用中的货币汇率换算，该工具均能提供精确的计算结果。货币汇率数据基于实时市场数据更新，适用于对时效性要求较高的金融场景。

### 3.1.2 Formula URI

```
moonshot/convert:latest
```

### 3.1.3 工具定义注册

通过以下 API 请求获取 `convert` 工具的 JSON Schema 定义：

```bash
curl ${MOONSHOT_BASE_URL}/formulas/moonshot/convert:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

返回的工具定义遵循 OpenAI Function Calling 规范，包含工具名称、描述及参数结构。获取后将其追加至 `chat.completions` 请求的 `tools` 数组中即可启用该工具。

### 3.1.4 参数说明

| 参数 | 类型 | 必填 | 说明 |
|:---|:---|:---|:---|
| `value` | number | 是 | 待转换的数值 |
| `from` | string | 是 | 源单位标识符 |
| `to` | string | 是 | 目标单位标识符 |
| `category` | string | 否 | 转换类别，用于限定单位范围 |

**支持的转换类别与单位示例**：

| 类别（category） | 示例单位（from/to） |
|:---|:---|
| `length`（长度） | `meter`, `kilometer`, `mile`, `foot`, `inch`, `centimeter`, `yard` |
| `mass`（质量） | `kilogram`, `gram`, `pound`, `ounce`, `ton`, `milligram` |
| `volume`（体积） | `liter`, `milliliter`, `gallon`, `cup`, `cubic_meter` |
| `temperature`（温度） | `celsius`, `fahrenheit`, `kelvin` |
| `area`（面积） | `square_meter`, `hectare`, `acre`, `square_kilometer`, `square_foot` |
| `time`（时间） | `second`, `minute`, `hour`, `day`, `week`, `month`, `year` |
| `energy`（能量） | `joule`, `calorie`, `kwh`, `btu` |
| `pressure`（压力） | `pascal`, `bar`, `psi`, `atm` |
| `speed`（速度） | `mps`（米/秒）, `kph`（公里/小时）, `mph`（英里/小时）, `knot`（节） |
| `currency`（货币） | `CNY`, `USD`, `EUR`, `JPY`, `GBP`（实时汇率） |

### 3.1.5 完整调用代码示例

#### 方式一：直接调用 fibers API

```bash
# 将 100 公里转换为英里
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/convert:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "convert",
    "arguments": "{\"value\": 100, \"from\": \"kilometer\", \"to\": \"mile\", \"category\": \"length\"}"
  }'
```

**预期返回**：

```json
{
  "result": 62.1371,
  "from": "kilometer",
  "to": "mile",
  "category": "length"
}
```

#### 方式二：Chat Completions 集成

```python
import os
import json
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["MOONSHOT_BASE_URL"],
    api_key=os.environ["MOONSHOT_API_KEY"]
)

# Step 1: 获取 convert 工具定义
import urllib.request
req = urllib.request.Request(
    f"{os.environ['MOONSHOT_BASE_URL']}/formulas/moonshot/convert:latest/tools",
    headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
)
with urllib.request.urlopen(req) as resp:
    convert_tools = json.loads(resp.read())

# Step 2: 发起对话请求
messages = [
    {"role": "system", "content": "你是一个单位换算助手。"},
    {"role": "user", "content": "北京到天津的距离大约是120公里，请帮我转换成英里。"}
]

response = client.chat.completions.create(
    model="moonshot-v1-128k",
    messages=messages,
    tools=convert_tools  # 注册 convert 工具
)

# Step 3: 处理 tool_calls
if response.choices[0].finish_reason == "tool_calls":
    tool_call = response.choices[0].message.tool_calls[0]
    tool_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)

    # Step 4: 执行工具调用
    import urllib.request
    fiber_req = urllib.request.Request(
        f"{os.environ['MOONSHOT_BASE_URL']}/formulas/moonshot/convert:latest/fibers",
        data=json.dumps({
            "name": tool_name,
            "arguments": json.dumps(arguments)
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"
        },
        method="POST"
    )
    with urllib.request.urlopen(fiber_req) as resp:
        tool_result = json.loads(resp.read())

    # Step 5: 将结果返回模型
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [tool_call.model_dump()]
    })
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps(tool_result)
    })

    final_response = client.chat.completions.create(
        model="moonshot-v1-128k",
        messages=messages,
        tools=convert_tools
    )
    print(final_response.choices[0].message.content)
```

#### 方式三：货币转换示例

```bash
# 将 1000 人民币转换为美元
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/convert:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "convert",
    "arguments": "{\"value\": 1000, \"from\": \"CNY\", \"to\": \"USD\", \"category\": \"currency\"}"
  }'
```

### 3.1.6 返回值说明

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `result` | number | 转换后的数值 |
| `from` | string | 源单位 |
| `to` | string | 目标单位 |
| `category` | string | 转换类别 |

### 3.1.7 使用场景

- **科学计算辅助**：物理实验数据中的单位统一换算，如将速度从 m/s 转换为 km/h
- **跨境电商应用**：商品价格的多货币展示，利用实时汇率自动更新定价
- **健康生活应用**：体重、身高的多单位显示（公斤/磅、厘米/英寸），满足国际化用户需求
- **旅行出行助手**：距离、速度、温度的本地化单位转换，如将天气预报从摄氏度转为华氏度
- **工程计算**：面积、压力、能量等工程单位之间的快速换算

### 3.1.8 注意事项

1. **货币汇率时效性**：货币类别（`currency`）的汇率数据基于实时市场数据，可能存在分钟级延迟，不建议用于高频交易等对精度要求极高的金融场景。
2. **温度转换特殊处理**：温度单位的换算涉及线性偏移（如摄氏度与华氏度的转换公式为 `F = C × 9/5 + 32`），不同于其他类别的比例转换，工具内部已自动处理。
3. **单位标识符大小写**：除货币代码（`CNY`, `USD` 等）为大写外，其他类别单位建议使用小写字母。
4. **非法单位处理**：传入工具不支持的单位标识符时，fibers API 将返回错误信息，建议在应用层做好异常捕获。
5. **多工具并发**：若同一请求中注册多个 Formula 工具，需维护 `function.name` 到 `formula_uri` 的映射关系，确保正确路由 tool_calls。

---

## 3.2 date — 日期时间处理工具

### 3.2.1 功能描述

`date` 是一款功能全面的日期时间处理工具，支持日期格式化、时间解析、日期算术运算（加减计算）、时区转换以及日期间隔计算等核心操作。该工具基于标准日期时间库实现，能够正确处理闰年、时区偏移（包括夏令时）等复杂场景，适用于全球化应用中的时间数据处理需求。

### 3.2.2 Formula URI

```
moonshot/date:latest
```

### 3.2.3 工具定义注册

```bash
curl ${MOONSHOT_BASE_URL}/formulas/moonshot/date:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

### 3.2.4 参数说明

| 参数 | 类型 | 必填 | 说明 |
|:---|:---|:---|:---|
| `action` | string | 是 | 操作类型：`format` / `parse` / `add` / `diff` / `convert_timezone` |
| `date` | string | 否 | 输入日期时间字符串，默认使用当前时间 |
| `format` | string | 否 | 日期格式化模板（如 `YYYY-MM-DD HH:mm:ss`） |
| `amount` | number | 否 | 日期加减的数值（`add` 操作时必填） |
| `unit` | string | 否 | 时间单位：`year` / `month` / `day` / `hour` / `minute` / `second` |
| `from_timezone` | string | 否 | 源时区标识符（如 `UTC`, `Asia/Shanghai`） |
| `to_timezone` | string | 否 | 目标时区标识符 |
| `date2` | string | 否 | 第二个日期（`diff` 操作时用于计算间隔） |

**action 操作类型详解**：

| 操作类型 | 功能描述 | 必填参数 |
|:---|:---|:---|
| `format` | 将日期按指定格式输出 | `date`, `format` |
| `parse` | 解析日期字符串为结构化数据 | `date` |
| `add` | 对日期进行加减运算 | `date`, `amount`, `unit` |
| `diff` | 计算两个日期之间的差值 | `date`, `date2`, `unit` |
| `convert_timezone` | 转换日期到目标时区 | `date`, `from_timezone`, `to_timezone` |

**常用时区标识符**：

| 时区 | 标识符 |
|:---|:---|
| 北京时间 | `Asia/Shanghai` |
| 东京时间 | `Asia/Tokyo` |
| 纽约时间 | `America/New_York` |
| 伦敦时间 | `Europe/London` |
| 协调世界时 | `UTC` |
| 太平洋时间 | `America/Los_Angeles` |

### 3.2.5 完整调用代码示例

#### 方式一：直接调用 fibers API

```bash
# 示例1：日期格式化
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/date:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "date",
    "arguments": "{\"action\": \"format\", \"date\": \"2024-12-25T10:30:00Z\", \"format\": \"YYYY年MM月DD日 HH:mm\"}"
  }'
```

**预期返回**：

```json
{
  "result": "2024年12月25日 10:30",
  "action": "format"
}
```

```bash
# 示例2：时区转换
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/date:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "date",
    "arguments": "{\"action\": \"convert_timezone\", \"date\": \"2024-06-15T12:00:00Z\", \"from_timezone\": \"UTC\", \"to_timezone\": \"Asia/Shanghai\"}"
  }'
```

```bash
# 示例3：日期加法运算
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/date:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "date",
    "arguments": "{\"action\": \"add\", \"date\": \"2024-01-01\", \"amount\": 30, \"unit\": \"day\"}"
  }'
```

```bash
# 示例4：计算日期间隔
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/date:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "date",
    "arguments": "{\"action\": \"diff\", \"date\": \"2024-01-01\", \"date2\": \"2024-12-31\", \"unit\": \"day\"}"
  }'
```

#### 方式二：Chat Completions 集成

```python
import os
import json
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["MOONSHOT_BASE_URL"],
    api_key=os.environ["MOONSHOT_API_KEY"]
)

# Step 1: 获取 date 工具定义
import urllib.request
req = urllib.request.Request(
    f"{os.environ['MOONSHOT_BASE_URL']}/formulas/moonshot/date:latest/tools",
    headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
)
with urllib.request.urlopen(req) as resp:
    date_tools = json.loads(resp.read())

# Step 2: 发起对话请求
messages = [
    {"role": "system", "content": "你是一个日期时间处理助手。"},
    {"role": "user", "content": "现在是北京时间，我想知道100天后是哪一天？"}
]

response = client.chat.completions.create(
    model="moonshot-v1-128k",
    messages=messages,
    tools=date_tools
)

# Step 3: 处理 tool_calls 并执行
if response.choices[0].finish_reason == "tool_calls":
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [tc.model_dump() for tc in response.choices[0].message.tool_calls]
    })

    for tool_call in response.choices[0].message.tool_calls:
        arguments = json.loads(tool_call.function.arguments)

        fiber_req = urllib.request.Request(
            f"{os.environ['MOONSHOT_BASE_URL']}/formulas/moonshot/date:latest/fibers",
            data=json.dumps({
                "name": tool_call.function.name,
                "arguments": json.dumps(arguments)
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"
            },
            method="POST"
        )
        with urllib.request.urlopen(fiber_req) as resp:
            tool_result = json.loads(resp.read())

        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(tool_result)
        })

    # Step 4: 获取最终回复
    final_response = client.chat.completions.create(
        model="moonshot-v1-128k",
        messages=messages,
        tools=date_tools
    )
    print(final_response.choices[0].message.content)
```

### 3.2.6 返回值说明

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `result` | string / number | 操作结果，格式化后的字符串或计算后的数值 |
| `action` | string | 执行的操作类型 |
| `timestamp` | number | 日期对应的时间戳（部分操作返回） |
| `year` / `month` / `day` | number | 日期的年月日分量（`parse` 操作时返回） |

### 3.2.7 使用场景

- **日程管理应用**：计算会议提醒时间、项目截止日期，如"3个工作日后是几号"
- **国际化时区转换**：为全球用户提供本地化的时间显示，如将 UTC 时间转换为用户所在时区
- **财务账期计算**：计算账单周期、还款日期、利息计算天数等
- **物流跟踪系统**：计算预计送达时间、运输时长，以及不同时区下的时间同步
- **数据格式化**：将 ISO 8601 标准时间格式转换为适合展示的本地化日期字符串

### 3.2.8 注意事项

1. **日期格式兼容性**：输入的 `date` 参数支持 ISO 8601 格式（如 `2024-12-25T10:30:00Z`）及常见简写格式（如 `2024-12-25`），建议使用标准格式以确保解析正确。
2. **时区标识符规范**：时区参数使用 IANA 时区数据库标识符（如 `Asia/Shanghai`），不支持 GMT+8 这类偏移量写法。
3. **夏令时处理**：`convert_timezone` 操作会自动处理目标时区的夏令时（DST）规则，无需手动调整。
4. **月份边界处理**：日期加法涉及月份时，工具会自动处理不同月份天数差异（如 1月31日 + 1个月 = 2月28/29日）。
5. **空日期默认行为**：若 `date` 参数留空，工具默认使用当前系统时间作为输入。

---

## 3.3 base64 — Base64 编解码工具

### 3.3.1 功能描述

`base64` 是标准的 Base64 编码与解码工具，支持将任意文本数据编码为 Base64 字符串，或将 Base64 字符串解码还原为原始文本。Base64 是一种基于 64 个可打印字符（A-Z、a-z、0-9、+、/）表示二进制数据的编码方式，广泛用于数据传输、URL 参数编码、图片内嵌、JWT Token 构建等场景。该工具严格遵循 RFC 4648 标准实现。

### 3.3.2 Formula URI

```
moonshot/base64:latest
```

### 3.3.3 工具定义注册

```bash
curl ${MOONSHOT_BASE_URL}/formulas/moonshot/base64:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

### 3.3.4 参数说明

| 参数 | 类型 | 必填 | 说明 |
|:---|:---|:---|:---|
| `action` | string | 是 | 操作类型：`encode`（编码）或 `decode`（解码） |
| `data` | string | 是 | 待处理的原始数据（编码时）或 Base64 字符串（解码时） |

### 3.3.5 完整调用代码示例

#### 方式一：直接调用 fibers API

```bash
# 示例1：Base64 编码
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/base64:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "base64",
    "arguments": "{\"action\": \"encode\", \"data\": \"Hello, Kimi!\"}"
  }'
```

**预期返回**：

```json
{
  "result": "SGVsbG8sIEtpbWkh",
  "action": "encode"
}
```

```bash
# 示例2：Base64 解码
curl -X POST ${MOONSHOT_BASE_URL}/formulas/moonshot/base64:latest/fibers \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{
    "name": "base64",
    "arguments": "{\"action\": \"decode\", \"data\": \"SGVsbG8sIEtpbWkh\"}"
  }'
```

**预期返回**：

```json
{
  "result": "Hello, Kimi!",
  "action": "decode"
}
```

#### 方式二：Chat Completions 集成

```python
import os
import json
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["MOONSHOT_BASE_URL"],
    api_key=os.environ["MOONSHOT_API_KEY"]
)

# Step 1: 获取 base64 工具定义
import urllib.request
req = urllib.request.Request(
    f"{os.environ['MOONSHOT_BASE_URL']}/formulas/moonshot/base64:latest/tools",
    headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
)
with urllib.request.urlopen(req) as resp:
    base64_tools = json.loads(resp.read())

# Step 2: 发起对话请求
messages = [
    {"role": "system", "content": "你是一个数据编码助手，擅长 Base64 编解码操作。"},
    {"role": "user", "content": "请将这段文字进行 Base64 编码：'Kimi 开放平台'"}
]

response = client.chat.completions.create(
    model="moonshot-v1-128k",
    messages=messages,
    tools=base64_tools
)

# Step 3: 处理 tool_calls
if response.choices[0].finish_reason == "tool_calls":
    tool_call = response.choices[0].message.tool_calls[0]
    arguments = json.loads(tool_call.function.arguments)

    # Step 4: 执行工具调用
    fiber_req = urllib.request.Request(
        f"{os.environ['MOONSHOT_BASE_URL']}/formulas/moonshot/base64:latest/fibers",
        data=json.dumps({
            "name": tool_call.function.name,
            "arguments": json.dumps(arguments)
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"
        },
        method="POST"
    )
    with urllib.request.urlopen(fiber_req) as resp:
        tool_result = json.loads(resp.read())

    # Step 5: 将结果返回模型
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [tool_call.model_dump()]
    })
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps(tool_result)
    })

    final_response = client.chat.completions.create(
        model="moonshot-v1-128k",
        messages=messages,
        tools=base64_tools
    )
    print(final_response.choices[0].message.content)
```

#### 方式三：结合文件读取进行图片 Base64 编码

```python
import os
import json
import base64

# 读取本地图片文件并进行 Base64 编码（客户端预处理）
with open("./image.png", "rb") as f:
    image_data = f.read()
    image_base64 = base64.b64encode(image_data).decode("utf-8")

# 将编码后的图片数据传递给模型进行多模态分析
messages = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "请分析这张图片的内容。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
        ]
    }
]
```

### 3.3.6 返回值说明

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `result` | string | 编码后的 Base64 字符串，或解码后的原始文本 |
| `action` | string | 执行的操作类型（`encode` 或 `decode`） |

### 3.3.7 使用场景

- **数据传输编码**：在 JSON 或 URL 中传输二进制数据时，将数据编码为 Base64 以避免特殊字符干扰
- **图片内嵌（Data URI）**：将图片文件编码为 Base64 字符串，直接嵌入 HTML/CSS 中，减少 HTTP 请求次数
- **JWT Token 构建**：JSON Web Token 的 Header 和 Payload 部分使用 Base64URL 编码
- **API 认证信息处理**：对 Basic Auth 的用户名密码组合进行 Base64 编码
- **配置文件加密存储**：对敏感配置信息进行简单的 Base64 混淆存储（注意：Base64 不是加密算法，仅提供编码转换）

### 3.3.8 注意事项

1. **数据体积膨胀**：Base64 编码会使数据体积增加约 **33%**（每 3 字节二进制数据编码为 4 字节文本），在传输大文件时需注意带宽开销。
2. **解码有效性校验**：解码操作要求输入必须是有效的 Base64 字符串。若输入包含非法字符或填充符（`=`）位置不正确，工具将返回错误信息。建议应用层对输入进行预校验。
3. **与 URL 安全编码的区别**：标准 Base64 使用 `+` 和 `/` 字符，在 URL 和文件名中需替换为 `-` 和 `_`（Base64URL 编码）。当前工具遵循 RFC 4648 标准 Base64 编码，如需 URL 安全版本，可在编码结果中手动替换字符。
4. **Unicode 文本处理**：编码包含多字节字符（如中文）的 Unicode 文本时，工具内部会正确处理 UTF-8 字节序列，无需预处理。
5. **非加密机制**：Base64 仅提供编码转换，不提供任何加密或安全性保障，切勿用于敏感信息的加密保护。

---

> **本章小结**：`convert`、`date` 和 `base64` 三个工具分别覆盖单位换算、日期时间处理和 Base64 编解码三大常见数据转换场景。它们均通过统一的 Formula 机制接入，遵循相同的 `tools` 注册 + `fibers` 执行模式。在实际应用中，可根据业务需求灵活组合使用，例如通过 `convert` 完成货币换算后，利用 `date` 计算账期，再经由 `base64` 编码传输凭证数据。

# Kimi 开放平台官方内置工具详解（四）：Excel 与 Memory

> **适用范围**：Kimi 开放平台 Formula 机制内置工具  
> **更新日期**：2025 年 7 月  
> **公式版本**：`moonshot/excel:latest`、`moonshot/memory:latest`

---

## 一、Excel 文件分析工具（`moonshot/excel:latest`）

### 1.1 功能描述

Excel 工具是 Kimi 开放平台提供的电子表格数据分析工具，支持对 `.xlsx`（Excel 工作簿）和 `.csv`（逗号分隔值）两种主流表格格式进行读取、解析、统计分析与汇总。该工具能够自动识别表头结构、推断数据类型，并支持多工作表（multi-sheet）场景下的数据操作，适用于财务报表分析、销售数据统计、数据清洗与转换等多种业务场景。

开发者可通过 Formula 机制将该工具注册至 Chat Completions 流程中，由模型根据用户请求自动触发，实现对话式数据分析体验。

### 1.2 Formula URI

| 属性 | 值 |
|:---|:---|
| Formula URI | `moonshot/excel:latest` |
| 命名空间 | `moonshot` |
| 工具名称 | `excel` |
| 版本标签 | `latest` |

### 1.3 获取工具定义

通过以下 HTTP 请求获取工具的标准 OpenAI function 定义：

```bash
curl https://api.moonshot.cn/v1/formulas/moonshot/excel:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

返回结果为标准的 `type: function` JSON Schema 格式，可直接追加至 Chat Completions 请求的 `tools` 参数中。

### 1.4 参数说明

> **注意**：以下参数基于工具功能描述推断得出，实际参数名和类型请以 `/formulas/{URI}/tools` 返回的 JSON Schema 为准。

| 参数名 | 类型 | 必填 | 描述 |
|:---|:---|:---|:---|
| `action` | `string` | 是 | 操作类型，可选值：`read`（读取数据）、`analyze`（统计分析）、`summarize`（汇总描述） |
| `file_url` | `string` | 条件必填 | 已上传文件的 URL 或文件 ID。`action` 为数据类操作时必须提供 |
| `sheet_name` | `string` | 否 | 目标工作表名称，适用于多工作簿场景；不指定时默认读取第一个工作表 |
| `range` | `string` | 否 | 数据范围，支持 Excel 风格表示（如 `"A1:D50"`）或行列索引；不指定时读取整个工作表 |
| `query` | `string` | 否 | 自然语言分析指令（如"计算各产品线的平均销售额"），仅在 `action=analyze` 时有效 |

### 1.5 核心特性

- **双格式支持**：原生支持 `.xlsx`（含多工作表）和 `.csv`（单表）两种格式
- **智能表头识别**：自动检测第一行是否为表头，并据此进行列名映射
- **数据类型推断**：自动将文本列、数值列、日期列分类，便于后续统计计算
- **多工作表读取**：支持指定 `sheet_name` 参数定位特定工作表
- **统计分析能力**：支持求和、均值、计数、最大/最小值等聚合运算
- **大文件分块**：支持分块读取大文件，避免内存溢出（行为由后端自动管理）

### 1.6 完整调用代码示例

以下示例展示完整的"文件上传 → 工具注册 → 对话分析 → 工具执行 → 结果返回"流程：

```python
import os
import json
import httpx
from openai import OpenAI
from pathlib import Path

# 初始化客户端
client = OpenAI(
    api_key=os.environ["MOONSHOT_API_KEY"],
    base_url="https://api.moonshot.cn/v1"
)
http = httpx.Client()

FORMULA_URI = "moonshot/excel:latest"
BASE_URL = "https://api.moonshot.cn/v1"

# ==========================================
# 步骤 1：上传 Excel 文件
# ==========================================
file_obj = client.files.create(
    file=Path("data.xlsx"),
    purpose="file-extract"
)
# 获取文件文本内容（可选，用于增强模型上下文）
file_content = client.files.content(file_id=file_obj.id).text

# ==========================================
# 步骤 2：获取 excel 工具定义
# ==========================================
resp = http.get(
    f"{BASE_URL}/formulas/{FORMULA_URI}/tools",
    headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
)
resp.raise_for_status()
tools_data = resp.json()
# 取第一个 function 定义作为工具注册
tool_def = tools_data["tools"][0]

# ==========================================
# 步骤 3：发起 Chat Completions 请求
# ==========================================
messages = [
    {
        "role": "system",
        "content": "你是专业的数据分析助手，擅长从 Excel 和 CSV 文件中提取洞察。"
    },
    {
        "role": "user",
        "content": f"请分析以下 Excel 数据，给出汇总统计和关键发现：\n{file_content}"
    }
]

completion = client.chat.completions.create(
    model="kimi-k2.6",
    messages=messages,
    tools=[tool_def]
)

# ==========================================
# 步骤 4：处理 tool_calls 并执行工具
# ==========================================
choice = completion.choices[0]

if choice.finish_reason == "tool_calls":
    # 将模型的 tool_calls 请求追加到消息列表
    messages.append({
        "role": "assistant",
        "content": choice.message.content or "",
        "tool_calls": [tc.model_dump() for tc in choice.message.tool_calls]
    })

    for tc in choice.message.tool_calls:
        # 解析模型生成的参数
        args = json.loads(tc.function.arguments)

        # 执行 excel 工具
        exec_resp = http.post(
            f"{BASE_URL}/formulas/{FORMULA_URI}/fibers",
            headers={
                "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}",
                "Content-Type": "application/json"
            },
            json={
                "name": tc.function.name,
                "arguments": json.dumps(args)
            }
        )
        exec_resp.raise_for_status()
        result = exec_resp.json()

        # 提取工具输出（普通工具在 context.output 中）
        output = result.get("context", {}).get("output", "")

        # 将工具执行结果以 role="tool" 消息返回
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": str(output)
        })

    # ==========================================
    # 步骤 5：将工具结果返回模型生成最终回复
    # ==========================================
    final = client.chat.completions.create(
        model="kimi-k2.6",
        messages=messages,
        tools=[tool_def]
    )
    print(final.choices[0].message.content)
else:
    # 模型直接回复，未触发工具
    print(choice.message.content)
```

### 1.7 返回值结构

工具执行后，`/fibers` 接口返回的 JSON 结构示例如下：

```json
{
  "context": {
    "output": {
      "summary": {
        "row_count": 1000,
        "column_count": 8,
        "columns": ["日期", "产品", "销售额", "数量", "地区", "客户", "利润", "利润率"],
        "data_types": {
          "日期": "datetime",
          "产品": "string",
          "销售额": "number",
          "数量": "number",
          "地区": "string",
          "客户": "string",
          "利润": "number",
          "利润率": "number"
        }
      },
      "statistics": {
        "销售额": { "sum": 1250000, "mean": 1250, "max": 8500, "min": 50 },
        "利润": { "sum": 320000, "mean": 320, "max": 2200, "min": -100 }
      },
      "top_products": [
        {"产品": "产品A", "总销售额": 350000},
        {"产品": "产品B", "总销售额": 280000}
      ]
    }
  }
}
```

> 注意：受保护（protected）工具的加密输出存储在 `context.encrypted_output` 中，需要按特殊流程解密。Excel 工具为普通工具，结果直接位于 `context.output`。

### 1.8 使用场景

| 场景 | 描述 | 示例 prompt |
|:---|:---|:---|
| **财务报表分析** | 读取利润表、资产负债表，进行比率分析 | "分析这份利润表，计算毛利率和净利率的变化趋势" |
| **销售数据统计** | 对销售数据进行分组聚合，输出统计报告 | "按地区和月份统计销售额，找出增长最快的区域" |
| **CSV 数据清洗** | 检测缺失值、异常值，给出清洗建议 | "检查这份 CSV 数据的缺失值比例和异常值" |
| **表格数据问答** | 针对表格内容进行自然语言问答 | "第三季度哪个产品的销量最高？" |

### 1.9 注意事项

1. **文件预处理**：Excel 文件需先通过 `client.files.create()` 上传并获取 `file_id` 或文件内容。工具本身不直接接受本地文件路径。
2. **公式解析限制**：包含复杂嵌套公式（如跨工作表引用、VBA 宏）的单元格可能无法完全解析，工具通常读取公式计算后的值。
3. **大文件策略**：超大文件（如数十万行以上）建议先通过 `range` 参数限定数据范围，或在上传前进行数据预处理（如筛选、采样）。
4. **函数名唯一性**：在单个 Chat Completions 请求中，`function.name` 必须唯一。若同时注册多个 Formula 工具，需自行维护 `function.name → formula_uri` 的映射关系。
5. **多 tool_calls 处理**：模型可能一次返回多个 `tool_calls`，必须全部执行并将结果一一对应返回，`tool_call_id` 需严格对齐。
6. **执行费用**：官方内置工具在限时免费期内执行不收取额外费用，具体以平台公告为准。

---

## 二、Memory 记忆存储与检索工具（`moonshot/memory:latest`）

### 2.1 功能描述

Memory 工具是 Kimi 开放平台提供的对话记忆存储与检索系统，支持将对话历史、用户偏好、个性化设置等数据以键值对（key-value）形式进行持久化存储，并通过语义检索（semantic retrieval）在后续对话中召回相关记忆。该工具使 AI 助手能够"记住"用户的偏好和上下文，实现跨会话的个性化交互体验。

记忆系统支持多种操作模式（保存、检索、列举、删除）和多级别作用域（会话级、用户级、全局级），满足从临时上下文保持到长期知识积累的多样化需求。

### 2.2 Formula URI

| 属性 | 值 |
|:---|:---|
| Formula URI | `moonshot/memory:latest` |
| 命名空间 | `moonshot` |
| 工具名称 | `memory` |
| 版本标签 | `latest` |

### 2.3 获取工具定义

```bash
curl https://api.moonshot.cn/v1/formulas/moonshot/memory:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

### 2.4 参数说明

> **注意**：以下参数基于工具功能描述推断得出，实际参数名和类型请以 `/formulas/{URI}/tools` 返回的 JSON Schema 为准。

| 参数名 | 类型 | 必填 | 描述 |
|:---|:---|:---|:---|
| `action` | `string` | 是 | 操作类型，可选值：`save`（保存记忆）、`recall`（检索记忆）、`list`（列举记忆）、`delete`（删除记忆） |
| `key` | `string` | 条件必填 | 记忆键名，`action=save` 或 `delete` 时必填，用于唯一标识一条记忆 |
| `value` | `string` | 条件必填 | 记忆内容，`action=save` 时必填，存储的具体信息 |
| `query` | `string` | 条件必填 | 检索查询语句，`action=recall` 时必填，支持自然语言描述 |
| `scope` | `string` | 否 | 记忆作用域，可选值：`session`（当前会话，默认）、`user`（用户级，跨会话）、`global`（全局共享） |

#### 各 action 参数组合

| action | 必填参数 | 可选参数 | 行为描述 |
|:---|:---|:---|:---|
| `save` | `key`, `value` | `scope` | 将 `value` 以 `key` 为键存入记忆系统 |
| `recall` | `query` | `scope` | 根据 `query` 语义检索匹配的记忆 |
| `list` | 无 | `scope` | 列举指定作用域下的所有记忆键名 |
| `delete` | `key` | `scope` | 删除指定键的记忆条目 |

### 2.5 核心特性

- **持久化存储**：记忆数据在服务端持久化保存，不受单次对话上下文窗口限制
- **键值对存储**：支持结构化的 key-value 存储模式，便于精确读写
- **语义检索**：`recall` 操作支持基于自然语言的语义匹配，无需精确键名即可召回相关记忆
- **多级作用域**：`session` 级记忆仅当前会话可见，`user` 级记忆跨会话保留，`global` 级记忆所有会话共享
- **记忆管理**：支持列出和删除操作，方便开发者对记忆进行维护和清理

### 2.6 完整调用代码示例

以下示例展示"保存用户偏好 → 后续对话中检索记忆"的完整流程：

```python
import os
import json
import httpx
from openai import OpenAI

# 初始化
client = OpenAI(
    api_key=os.environ["MOONSHOT_API_KEY"],
    base_url="https://api.moonshot.cn/v1"
)
http = httpx.Client()

FORMULA_URI = "moonshot/memory:latest"
BASE_URL = "https://api.moonshot.cn/v1"

# ==========================================
# 步骤 1：获取 memory 工具定义
# ==========================================
resp = http.get(
    f"{BASE_URL}/formulas/{FORMULA_URI}/tools",
    headers={"Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}"}
)
resp.raise_for_status()
tool_def = resp.json()["tools"][0]

# ==========================================
# 场景 A：保存用户偏好记忆
# ==========================================

save_messages = [
    {
        "role": "system",
        "content": "你是用户的个人助理。当用户表达偏好时，请使用 memory 工具将其保存。"
    },
    {
        "role": "user",
        "content": "记住，我喜欢简洁的回答，不喜欢太长的解释。还有，我偏好用 Python 编程。"
    }
]

# 第一轮：模型可能决定保存记忆
completion1 = client.chat.completions.create(
    model="kimi-k2.6",
    messages=save_messages,
    tools=[tool_def]
)

choice1 = completion1.choices[0]

if choice1.finish_reason == "tool_calls":
    save_messages.append({
        "role": "assistant",
        "content": choice1.message.content or "",
        "tool_calls": [tc.model_dump() for tc in choice1.message.tool_calls]
    })

    for tc in choice1.message.tool_calls:
        args = json.loads(tc.function.arguments)

        # 执行 memory save
        exec_resp = http.post(
            f"{BASE_URL}/formulas/{FORMULA_URI}/fibers",
            headers={
                "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}",
                "Content-Type": "application/json"
            },
            json={
                "name": tc.function.name,
                "arguments": json.dumps(args)
            }
        )
        exec_resp.raise_for_status()
        result = exec_resp.json()
        output = result.get("context", {}).get("output", "")

        save_messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": str(output)
        })

    # 获取模型对保存操作的确认回复
    final1 = client.chat.completions.create(
        model="kimi-k2.6",
        messages=save_messages,
        tools=[tool_def]
    )
    print("保存结果:", final1.choices[0].message.content)

# ==========================================
# 场景 B：在新对话中检索记忆
# ==========================================

recall_messages = [
    {
        "role": "system",
        "content": "你是用户的个人助理。在回答前，请先通过 memory 工具检索用户的偏好。"
    },
    {
        "role": "user",
        "content": "帮我写一段读取文件的 Python 代码。"
    }
]

# 第一轮：模型检索用户偏好
completion2 = client.chat.completions.create(
    model="kimi-k2.6",
    messages=recall_messages,
    tools=[tool_def]
)

choice2 = completion2.choices[0]

if choice2.finish_reason == "tool_calls":
    recall_messages.append({
        "role": "assistant",
        "content": choice2.message.content or "",
        "tool_calls": [tc.model_dump() for tc in choice2.message.tool_calls]
    })

    for tc in choice2.message.tool_calls:
        args = json.loads(tc.function.arguments)

        # 执行 memory recall
        exec_resp = http.post(
            f"{BASE_URL}/formulas/{FORMULA_URI}/fibers",
            headers={
                "Authorization": f"Bearer {os.environ['MOONSHOT_API_KEY']}",
                "Content-Type": "application/json"
            },
            json={
                "name": tc.function.name,
                "arguments": json.dumps(args)
            }
        )
        exec_resp.raise_for_status()
        result = exec_resp.json()
        output = result.get("context", {}).get("output", "")

        recall_messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": str(output)
        })

    # 模型结合检索到的偏好生成最终回答
    final2 = client.chat.completions.create(
        model="kimi-k2.6",
        messages=recall_messages,
        tools=[tool_def]
    )
    print("\n最终回答:", final2.choices[0].message.content)
```

#### 直接调用 fibers 接口示例（无需 Chat Completions）

```python
import os
import json
import httpx

http = httpx.Client()
BASE_URL = "https://api.moonshot.cn/v1"
FORMULA_URI = "moonshot/memory:latest"
api_key = os.environ["MOONSHOT_API_KEY"]

# 保存记忆
resp_save = http.post(
    f"{BASE_URL}/formulas/{FORMULA_URI}/fibers",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "name": "memory",
        "arguments": json.dumps({
            "action": "save",
            "key": "user_preference_style",
            "value": "偏好简洁回答，使用 Python",
            "scope": "user"
        })
    }
)
print("保存响应:", resp_save.json())

# 检索记忆
resp_recall = http.post(
    f"{BASE_URL}/formulas/{FORMULA_URI}/fibers",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "name": "memory",
        "arguments": json.dumps({
            "action": "recall",
            "query": "用户的编程偏好是什么",
            "scope": "user"
        })
    }
)
print("检索响应:", resp_recall.json())

# 列举记忆
resp_list = http.post(
    f"{BASE_URL}/formulas/{FORMULA_URI}/fibers",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "name": "memory",
        "arguments": json.dumps({
            "action": "list",
            "scope": "user"
        })
    }
)
print("列表响应:", resp_list.json())

# 删除记忆
resp_delete = http.post(
    f"{BASE_URL}/formulas/{FORMULA_URI}/fibers",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "name": "memory",
        "arguments": json.dumps({
            "action": "delete",
            "key": "user_preference_style",
            "scope": "user"
        })
    }
)
print("删除响应:", resp_delete.json())
```

### 2.7 返回值结构

工具执行后，`/fibers` 接口针对不同 action 返回的结构如下：

**save 返回值**：

```json
{
  "context": {
    "output": {
      "success": true,
      "key": "user_preference_style",
      "message": "Memory saved successfully."
    }
  }
}
```

**recall 返回值**：

```json
{
  "context": {
    "output": {
      "results": [
        {
          "key": "user_preference_style",
          "value": "偏好简洁回答，使用 Python",
          "score": 0.95,
          "scope": "user",
          "created_at": "2025-07-01T10:30:00Z"
        }
      ],
      "count": 1
    }
  }
}
```

**list 返回值**：

```json
{
  "context": {
    "output": {
      "memories": [
        {"key": "user_preference_style", "scope": "user", "created_at": "2025-07-01T10:30:00Z"},
        {"key": "favorite_language", "scope": "user", "created_at": "2025-07-01T11:00:00Z"}
      ],
      "count": 2
    }
  }
}
```

**delete 返回值**：

```json
{
  "context": {
    "output": {
      "success": true,
      "key": "user_preference_style",
      "message": "Memory deleted successfully."
    }
  }
}
```

### 2.8 使用场景

| 场景 | 描述 | 推荐 scope |
|:---|:---|:---|
| **用户偏好记忆** | 记录用户的表达习惯、格式偏好（如"我喜欢简洁的回答"） | `user` |
| **跨会话上下文保持** | 在多轮对话中保持关键上下文信息，突破单次对话窗口限制 | `user` |
| **个性化设置存储** | 保存用户的行业背景、专业领域、常用工具等静态信息 | `user` |
| **临时任务状态** | 当前复杂任务中的中间状态、待办事项等 | `session` |
| **长期知识积累** | 从对话中提取的知识点、最佳实践等可供后续复用的信息 | `user` |

### 2.9 注意事项

1. **敏感信息安全**：记忆系统不适合存储密码、API 密钥、个人隐私等敏感信息。建议使用专业的密钥管理服务处理敏感数据。
2. **存储上限**：记忆系统存在存储容量上限（具体限制以平台文档为准），建议定期清理过期或不再需要的记忆条目。
3. **作用域选择策略**：
   - `session` 级：适用于临时状态，会话结束后自动清理，无长期存储压力
   - `user` 级：适用于用户偏好和长期上下文，是大多数场景的首选
   - `global` 级：谨慎使用，全局记忆对所有会话可见，可能产生信息干扰
4. **键名管理**：建议使用有命名空间前缀的键名（如 `user_pref_xxx`、`task_xxx`）避免键名冲突。
5. **语义检索精度**：`recall` 操作基于语义相似度匹配，检索结果按相关度分数排序。对于需要精确读取的场景，建议配合 `list` 操作获取确切键名后再进行定向读取。
6. **记忆生命周期**：建议为记忆条目设置合理的 TTL（生命周期）策略，可通过业务逻辑层定期调用 `delete` 清理过期记忆。
7. **多 tool_calls 对齐**：与 Excel 工具相同，当模型返回多个 `tool_calls` 时，`tool_call_id` 必须一一对应返回。
8. **函数名映射**：同时使用多个 Formula 工具时，必须在代码中维护 `function.name → formula_uri` 的映射，确保工具调用路由正确。

---

## 三、两个工具的对比总结

| 维度 | `moonshot/excel:latest` | `moonshot/memory:latest` |
|:---|:---|:---|
| **核心能力** | 电子表格读取与统计分析 | 键值对存储与语义检索 |
| **输入形式** | 文件 URL / 文件内容 | 键值对 / 自然语言查询 |
| **输出形式** | 结构化数据分析结果 | 记忆内容 / 操作状态 |
| **数据持久化** | 无（基于上传文件临时分析） | 有（服务端持久化存储） |
| **典型 action** | `read`, `analyze`, `summarize` | `save`, `recall`, `list`, `delete` |
| **使用场景** | 报表分析、数据统计、数据清洗 | 偏好记忆、上下文保持、知识积累 |
| **scope 参数** | 无（作用于指定文件范围） | `session` / `user` / `global` |
| **模型触发条件** | 用户请求涉及表格数据分析时 | 用户表达偏好或需要上下文回忆时 |

---

## 四、通用集成要点

### 4.1 多 Formula 工具同时使用

当需要同时注册 Excel 和 Memory 等多个 Formula 工具时，注意维护函数名到 URI 的映射：

```python
# 维护 function.name → formula_uri 映射
FORMULA_REGISTRY = {
    "excel": "moonshot/excel:latest",
    "memory": "moonshot/memory:latest",
    # 其他工具...
}

# 收集所有工具定义
all_tools = []
for name, uri in FORMULA_REGISTRY.items():
    resp = http.get(f"{BASE_URL}/formulas/{uri}/tools", ...)
    tool = resp.json()["tools"][0]
    all_tools.append(tool)

# 路由 tool_call 到正确的 formula
for tc in tool_calls:
    formula_uri = FORMULA_REGISTRY.get(tc.function.name)
    if formula_uri:
        # 执行对应的 formula
        ...
```

### 4.2 错误处理建议

```python
try:
    exec_resp = http.post(
        f"{BASE_URL}/formulas/{formula_uri}/fibers",
        headers={...},
        json={"name": tc.function.name, "arguments": json.dumps(args)}
    )
    exec_resp.raise_for_status()
    result = exec_resp.json()
except httpx.HTTPStatusError as e:
    # HTTP 错误处理
    error_content = {"error": f"Tool execution failed: {e.response.status_code}"}
    messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(error_content)})
except Exception as e:
    # 通用错误处理
    messages.append({"role": "tool", "tool_call_id": tc.id, "content": f"Error: {str(e)}"})
```

---

> **文档说明**：本文档中标注为"推断"的参数基于工具功能描述推断得出，实际参数名、类型和行为请以 Kimi 开放平台官方 API 返回的 JSON Schema 为准。建议开发者在集成前通过 `/formulas/{URI}/tools` 接口获取最新的工具定义。

# 第5节 辅助类工具详解

> 本文档覆盖 Kimi 开放平台官方内置的三个辅助类工具：`rethink`（智能整理想法）、`random-choice`（随机选择）和 `mew`（趣味猫叫）。这些工具虽不涉及外部数据查询，但在提升模型思考质量、增强交互趣味性和辅助决策方面具有独特价值。

---

## 5.1 rethink — 智能整理想法工具

### 5.1.1 功能描述

`rethink` 是一款面向模型自身的**元认知（metacognition）辅助工具**，其核心作用是将模型的内部思考过程外部化、结构化。当模型面对复杂推理任务、多步骤分析或需要整合多方观点时，可通过调用 `rethink` 工具对当前思路进行整理、归纳、扩展或批判性审视，从而提升最终回答的逻辑严密性与表达清晰度。

从机制上看，`rethink` 类似于为模型提供一个"草稿纸"——模型将当前碎片化的思考写入 `thought` 参数，并指定期望的整理方式（`action`），工具返回结构化后的思考结果，供模型在后续生成中参考。这种**思维链（Chain-of-Thought）的外部化**机制，有效弥补了纯内部推理过程中可能出现的逻辑跳跃或遗漏问题。

### 5.1.2 Formula URI

```
moonshot/rethink:latest
```

### 5.1.3 工具定义获取

```bash
curl ${MOONSHOT_BASE_URL}/formulas/moonshot/rethink:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

返回的工具定义示例（OpenAI Function Calling 格式）：

```json
{
  "type": "function",
  "function": {
    "name": "rethink",
    "description": "智能整理想法工具，帮助模型整理思路、梳理逻辑、优化表达。当模型面对复杂问题时，可以通过 rethink 工具来整理想法，提高回答质量。",
    "parameters": {
      "type": "object",
      "properties": {
        "thought": {
          "type": "string",
          "description": "需要整理的想法或思路，可以是当前的分析片段、多种观点的集合或初步结论"
        },
        "action": {
          "type": "string",
          "description": "整理方式",
          "enum": ["organize", "summarize", "expand", "critique"],
          "default": "organize"
        }
      },
      "required": ["thought"]
    }
  }
}
```

### 5.1.4 参数说明

| 参数 | 类型 | 必填 | 说明 |
|:---:|:---:|:---:|:---|
| `thought` | string | 是 | 需要整理的想法或思路。可以是当前的分析片段、多种观点的集合、初步结论，或任何需要结构化处理的原始思考内容 |
| `action` | string | 否 | 整理方式，可选值及含义见下表。默认值为 `organize` |

**`action` 枚举值详解**：

| 取值 | 含义 | 适用场景 |
|:---:|:---|:---|
| `organize` | 结构化组织 | 将零散思路按逻辑层级重新排列，建立清晰的因果/并列/递进关系 |
| `summarize` | 归纳总结 | 从大量分析内容中提取核心要点，压缩为精炼的结论性表述 |
| `expand` | 扩展深化 | 对某一论点进行多角度展开，补充论据、推演细节、挖掘隐含前提 |
| `critique` | 批判审视 | 以对立视角审视当前思路，识别逻辑漏洞、偏见盲点或论证薄弱环节 |

### 5.1.5 返回值

工具执行成功后，返回结构化整理后的文本内容。返回格式为字符串（string），具体结构取决于 `action` 类型：

- `organize`：分层次、带编号的结构化文本，通常包含主题句 + 分项论述
- `summarize`：精炼的段落或要点列表，保留核心论点，剔除冗余细节
- `expand`：围绕原始思路展开的详细论述，可能包含多个子论点和支撑论据
- `critique`：以"问题-分析-建议"三段式呈现的逻辑审查报告

### 5.1.6 完整调用代码示例

以下示例演示如何在 Chat Completions 流程中集成 `rethink` 工具，实现模型自动触发的思考整理：

```python
import os
import json
import asyncio
import httpx
from openai import AsyncOpenAI

MOONSHOT_BASE_URL = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY")

async def call_rethink(thought: str, action: str = "organize") -> str:
    """直接调用 rethink 工具（POST /formulas/{URI}/fibers）"""
    async with httpx.AsyncClient(
        base_url=MOONSHOT_BASE_URL,
        headers={"Authorization": f"Bearer {MOONSHOT_API_KEY}"},
        timeout=30.0
    ) as client:
        resp = await client.post(
            "/formulas/moonshot/rethink:latest/fibers",
            json={
                "name": "rethink",
                "arguments": json.dumps({
                    "thought": thought,
                    "action": action
                }, ensure_ascii=False)
            }
        )
        fiber = resp.json()
        if fiber.get("status") == "succeeded":
            return fiber["context"].get("output", "")
        return f"Error: {fiber.get('error', 'Unknown error')}"


async def chat_with_rethink():
    """Chat Completions 集成：让模型自主决定是否使用 rethink 工具"""
    openai_client = AsyncOpenAI(base_url=MOONSHOT_BASE_URL, api_key=MOONSHOT_API_KEY)

    # 1. 获取 rethink 工具定义
    async with httpx.AsyncClient(
        base_url=MOONSHOT_BASE_URL,
        headers={"Authorization": f"Bearer {MOONSHOT_API_KEY}"},
        timeout=30.0
    ) as http_client:
        tools_resp = await http_client.get("/formulas/moonshot/rethink:latest/tools")
        rethink_tools = tools_resp.json().get("tools", [])

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个善于深度思考的智能助手。当面对复杂分析任务时，"
                "你可以调用 rethink 工具来整理和优化你的思考过程，"
                "从而为用户提供更严谨、更有逻辑的回答。"
            )
        },
        {
            "role": "user",
            "content": (
                "请分析人工智能技术在医疗诊断领域的应用，"
                "包括机遇、挑战和未来趋势。这是一个复杂话题，"
                "建议你在回答前先整理想法。"
            )
        }
    ]

    # 2. 首轮请求：模型可能返回 tool_calls 要求调用 rethink
    response = await openai_client.chat.completions.create(
        model="kimi-k2.6",
        messages=messages,
        tools=rethink_tools
    )

    message = response.choices[0].message

    # 3. 处理 tool_calls
    if message.tool_calls:
        # 将模型的 tool_calls 请求追加到消息历史
        messages.append(message)

        for call in message.tool_calls:
            func_name = call.function.name
            args = json.loads(call.function.arguments)
            print(f"[Tool Call] {func_name}({json.dumps(args, ensure_ascii=False)})")

            # 4. 执行工具调用
            result = await call_rethink(args["thought"], args.get("action", "organize"))
            print(f"[Tool Result] {result[:200]}...")

            # 5. 将工具结果作为 role="tool" 消息返回
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result
            })

        # 6. 二次请求：模型基于整理后的思考生成最终回答
        final_response = await openai_client.chat.completions.create(
            model="kimi-k2.6",
            messages=messages,
            tools=rethink_tools
        )
        final_message = final_response.choices[0].message
        print(f"\n[Final Answer]\n{final_message.content}")
        return final_message.content
    else:
        # 模型直接回答了
        print(f"\n[Direct Answer]\n{message.content}")
        return message.content


if __name__ == "__main__":
    asyncio.run(chat_with_rethink())
```

### 5.1.7 使用场景

| 场景 | 说明 | 推荐 action |
|:---:|:---|:---:|
| **复杂问题分析前预热** | 在正式回答多维度问题前，先将已知信息和初步判断进行结构化组织，确保后续论述覆盖全面 | `organize` |
| **多角度观点归纳** | 用户提供了多个观点或立场，需要整合为一致的论述框架 | `organize` / `summarize` |
| **论证逻辑梳理** | 构建复杂论证链时，检查因果关系是否成立、前提假设是否合理 | `critique` |
| **创意发散与收敛** | 头脑风暴阶段生成大量想法后，筛选核心创意并深化价值主张 | `expand` / `summarize` |
| **长对话上下文压缩** | 在多轮长对话后，压缩历史信息为关键要点，避免上下文溢出 | `summarize` |
| **对立观点审查** | 在输出结论性判断前，主动审视反方论据以增强回答的客观性 | `critique` |

### 5.1.8 注意事项

1. **响应时间增加**：`rethink` 涉及一次额外的工具调用往返（模型 → API → 工具 → API → 模型），整体响应时间会增加 1~3 秒（取决于网络延迟和输入长度）。在对延迟敏感的场景中，应权衡思考深度与响应速度。

2. **适用场景筛选**：简单问答（如"1+1等于几"、"什么是Python"）无需使用 `rethink`，强行调用反而增加不必要的开销。建议在 system prompt 中引导模型仅在面临多步骤推理、多方观点整合或高复杂度分析时自主触发。

3. **输入质量决定输出质量**：`rethink` 的效果高度依赖 `thought` 参数的充实程度。过于简略的输入（如"帮我整理一下"）会导致工具返回泛泛而谈的结果。最佳实践是在调用时提供尽可能完整的原始思考内容。

4. **与模型内置思考能力的协同**：Kimi K2.6 等模型本身具备 `enable_thinking` 模式（输出 `reasoning_content`），`rethink` 工具可作为其外部化补充——将内部推理中不便展开或需要结构化的部分交由工具处理，实现"内部快速思考 + 外部深度整理"的双层认知架构。

5. **避免递归调用**：在极少数情况下，模型可能在收到 `rethink` 结果后再次调用 `rethink` 进行"二次整理"。可通过在 system prompt 中限制调用次数或在业务侧检测重复调用来避免无限递归。

---

## 5.2 random-choice — 随机选择工具

### 5.2.1 功能描述

`random-choice` 是一款轻量级随机选择工具，用于从给定的选项列表中按照均匀分布随机选取一个或多个元素。该工具为模型提供了可量化的随机决策能力，适用于推荐、抽奖、分组、决策辅助等需要引入随机性的业务场景。

工具的核心特性包括：
- **均匀随机分布**：每个选项被选中的概率均等
- **可控选取数量**：支持单次选取 1 个或多个元素
- **重复开关**：可选择是否允许同一选项被多次选中
- **确定性输出**：返回明文结果，无需解密

### 5.2.2 Formula URI

```
moonshot/random-choice:latest
```

### 5.2.3 工具定义获取

```bash
curl ${MOONSHOT_BASE_URL}/formulas/moonshot/random-choice:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

返回的工具定义示例：

```json
{
  "type": "function",
  "function": {
    "name": "random_choice",
    "description": "随机选择工具，从给定选项中随机选择一个或多个。使用伪随机数生成器，不适合高安全性场景。",
    "parameters": {
      "type": "object",
      "properties": {
        "options": {
          "type": "array",
          "items": { "type": "string" },
          "description": "选项列表，每个元素为一个可选字符串",
          "minItems": 1
        },
        "count": {
          "type": "integer",
          "description": "选择数量，默认为 1",
          "minimum": 1,
          "default": 1
        },
        "allow_duplicates": {
          "type": "boolean",
          "description": "是否允许重复选择，默认为 false。当 count 大于 options 长度时必须设为 true",
          "default": false
        }
      },
      "required": ["options"]
    }
  }
}
```

> **命名映射说明**：通过 API 获取的工具定义中，`function.name` 可能为 `random_choice`（下划线连接）。在调用 `/fibers` 时，`name` 字段需与工具定义中的 `function.name` 严格一致。

### 5.2.4 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|:---:|:---:|:---:|:---:|:---|
| `options` | array of string | 是 | — | 候选选项列表，至少包含 1 个元素。每个元素为描述性字符串 |
| `count` | integer | 否 | `1` | 需选择的选项数量。必须 ≥ 1。当 `allow_duplicates=false` 时，`count` 不得超过 `options` 长度 |
| `allow_duplicates` | boolean | 否 | `false` | 是否允许同一选项被重复选中。设为 `true` 时，`count` 可大于 `options` 长度 |

### 5.2.5 返回值

工具返回 JSON 格式的结果，典型结构如下：

```json
{
  "selected": ["选项B"],
  "total_options": 4,
  "count": 1,
  "allow_duplicates": false
}
```

当 `count > 1` 且 `allow_duplicates=false` 时：

```json
{
  "selected": ["选项A", "选项D", "选项C"],
  "total_options": 4,
  "count": 3,
  "allow_duplicates": false
}
```

当 `allow_duplicates=true` 时，返回的 `selected` 数组中可能出现重复值：

```json
{
  "selected": ["选项A", "选项A", "选项B"],
  "total_options": 3,
  "count": 3,
  "allow_duplicates": true
}
```

### 5.2.6 完整调用代码示例

**示例一：直接调用（独立使用）**

```python
import os
import json
import httpx

MOONSHOT_BASE_URL = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY")

async def random_choice(
    options: list[str],
    count: int = 1,
    allow_duplicates: bool = False
) -> dict:
    """直接调用 random-choice 工具"""
    async with httpx.AsyncClient(
        base_url=MOONSHOT_BASE_URL,
        headers={"Authorization": f"Bearer {MOONSHOT_API_KEY}"},
        timeout=30.0
    ) as client:
        resp = await client.post(
            "/formulas/moonshot/random-choice:latest/fibers",
            json={
                "name": "random_choice",
                "arguments": json.dumps({
                    "options": options,
                    "count": count,
                    "allow_duplicates": allow_duplicates
                }, ensure_ascii=False)
            }
        )
        fiber = resp.json()
        if fiber.get("status") == "succeeded":
            return json.loads(fiber["context"]["output"])
        raise RuntimeError(f"Tool failed: {fiber}")


# 使用示例
async def demo():
    # 场景1：午餐推荐
    lunch = await random_choice(
        options=["日式拉面", "轻食沙拉", "川菜小炒", "粤式茶餐厅"],
        count=1
    )
    print(f"今日午餐推荐：{lunch['selected'][0]}")

    # 场景2：抽奖（抽取3名获奖者，不重复）
    winners = await random_choice(
        options=["Alice", "Bob", "Carol", "David", "Eve", "Frank"],
        count=3,
        allow_duplicates=False
    )
    print(f"中奖名单：{winners['selected']}")

    # 场景3：允许重复的随机抽样（如掷骰子模拟）
    dice = await random_choice(
        options=["1点", "2点", "3点", "4点", "5点", "6点"],
        count=10,
        allow_duplicates=True
    )
    print(f"10次掷骰子结果：{dice['selected']}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(demo())
```

**示例二：Chat Completions 集成（让模型自主随机选择）**

```python
import os
import json
import asyncio
import httpx
from openai import AsyncOpenAI

async def chat_random_choice():
    """在对话中让模型自主使用 random-choice 工具"""
    moonshot_base_url = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
    api_key = os.getenv("MOONSHOT_API_KEY")

    openai_client = AsyncOpenAI(base_url=moonshot_base_url, api_key=api_key)

    # 获取工具定义
    async with httpx.AsyncClient(
        base_url=moonshot_base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0
    ) as http_client:
        tools_resp = await http_client.get(
            "/formulas/moonshot/random-choice:latest/tools"
        )
        choice_tools = tools_resp.json().get("tools", [])

    messages = [
        {
            "role": "system",
            "content": "你是一个乐于助人的助手。当用户需要随机选择、抽奖或决策辅助时，你可以调用 random_choice 工具来提供公平的结果。"
        },
        {"role": "user", "content": "我周末想去玩，在'爬山'、'看电影'、'逛博物馆'、'宅家打游戏'里帮我随机选两个吧。"}
    ]

    # 首次请求
    response = await openai_client.chat.completions.create(
        model="kimi-k2.6",
        messages=messages,
        tools=choice_tools
    )

    message = response.choices[0].message
    if message.tool_calls:
        messages.append(message)

        for call in message.tool_calls:
            args = json.loads(call.function.arguments)

            # 执行工具
            resp = await http_client.post(
                "/formulas/moonshot/random-choice:latest/fibers",
                json={
                    "name": call.function.name,
                    "arguments": call.function.arguments
                }
            )
            fiber = resp.json()
            result = fiber["context"]["output"] if fiber["status"] == "succeeded" else "{}"

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result
            })

        # 获取最终回答
        final = await openai_client.chat.completions.create(
            model="kimi-k2.6",
            messages=messages,
            tools=choice_tools
        )
        print(final.choices[0].message.content)
    else:
        print(message.content)


if __name__ == "__main__":
    asyncio.run(chat_random_choice())
```

### 5.2.7 使用场景

| 场景 | 说明 |
|:---|:---|
| **随机推荐** | 为用户从候选列表中推荐一个或多个选项（如电影推荐、餐厅选择、旅游目的地） |
| **抽奖/抽签** | 在活动运营中随机抽取中奖者，支持一次性抽取多名且不重复 |
| **A/B 测试分组** | 将用户或样本随机分配到不同实验组，支持控制分组数量 |
| **决策辅助** | 当用户面临选择困难时，提供公平的随机决策支持 |
| **问卷抽样** | 从大量问题中随机选取指定数量组成问卷 |
| **游戏化交互** | 在对话中引入随机元素增强趣味性（如随机挑战、随机话题） |

### 5.2.8 注意事项

1. **伪随机数安全性**：`random-choice` 使用标准伪随机数生成器（PRNG），其随机性不适合用于高安全性场景，如密码学密钥生成、金融抽奖、博彩等。此类场景应使用经过密码学安全认证的随机数生成器（CSPRNG）。

2. **count 与 allow_duplicates 的约束关系**：当 `allow_duplicates=false`（默认）时，`count` 的值不得超过 `options` 的长度，否则工具将返回错误。如需从少量选项中大量抽样，必须显式设置 `allow_duplicates=true`。

3. **选项描述质量**：`options` 中的字符串应简洁明确，避免语义重复或歧义。模型在构造参数时会根据用户输入生成选项列表，良好的选项描述有助于提升最终结果的可用性。

4. **结果不可复现**：每次调用 `random-choice` 都会产生新的随机结果，同一组参数多次调用的返回值通常不同。如需可复现的随机序列，需在应用层自行实现种子控制机制。

---

## 5.3 mew — 趣味猫叫工具

### 5.3.1 功能描述

`mew` 是一款纯趣味性功能工具，用于随机生成猫叫声（如"喵~"、"咪呜~"等）及配套的祝福语。该工具的设计初衷是为对话注入轻松愉快的氛围，在用户完成一项任务、取得成就或需要情绪缓解时，提供温暖的趣味互动。

与其他功能性工具不同，`mew` 不承载任何信息处理或决策辅助职能，其价值完全体现在**情感连接与用户体验**层面。在适当的时机触发一次 `mew` 调用，可以有效拉近 AI 与用户之间的心理距离，让技术产品更具人情味。

### 5.3.2 Formula URI

```
moonshot/mew:latest
```

### 5.3.3 工具定义获取

```bash
curl ${MOONSHOT_BASE_URL}/formulas/moonshot/mew:latest/tools \
  -H "Authorization: Bearer $MOONSHOT_API_KEY"
```

返回的工具定义示例：

```json
{
  "type": "function",
  "function": {
    "name": "mew",
    "description": "随机产生猫的叫声和祝福的工具。可以生成不同风格的猫叫声，并可选附带祝福语。",
    "parameters": {
      "type": "object",
      "properties": {
        "style": {
          "type": "string",
          "description": "猫叫风格",
          "enum": ["cute", "energetic", "lazy"],
          "default": "cute"
        },
        "include_blessing": {
          "type": "boolean",
          "description": "是否包含祝福，默认为 true",
          "default": true
        }
      }
    }
  }
}
```

### 5.3.4 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|:---:|:---:|:---:|:---:|:---|
| `style` | string | 否 | `"cute"` | 猫叫风格。可选值：`cute`（可爱，柔和短促）、`energetic`（活力，欢快跳跃）、`lazy`（慵懒，拖长尾音） |
| `include_blessing` | boolean | 否 | `true` | 是否在猫叫后附带一句祝福语。设为 `false` 时仅返回纯猫叫声 |

### 5.3.5 返回值

工具返回字符串（string），格式为猫叫声 + 祝福语（当 `include_blessing=true` 时）。示例：

| style | include_blessing | 可能返回 |
|:---:|:---:|:---|
| `cute` | `true` | "喵~ 🐱 愿你今天充满阳光和好心情！" |
| `energetic` | `true` | "喵喵喵！✨ 祝你元气满满，万事顺意！" |
| `lazy` | `true` | "喵呜~~~ 😺 愿你拥有悠闲惬意的一天~" |
| `cute` | `false` | "喵~ 🐾" |

### 5.3.6 完整调用代码示例

```python
import os
import json
import asyncio
import httpx
from openai import AsyncOpenAI

MOONSHOT_BASE_URL = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY")


async def call_mew(style: str = "cute", include_blessing: bool = True) -> str:
    """直接调用 mew 工具"""
    async with httpx.AsyncClient(
        base_url=MOONSHOT_BASE_URL,
        headers={"Authorization": f"Bearer {MOONSHOT_API_KEY}"},
        timeout=30.0
    ) as client:
        resp = await client.post(
            "/formulas/moonshot/mew:latest/fibers",
            json={
                "name": "mew",
                "arguments": json.dumps({
                    "style": style,
                    "include_blessing": include_blessing
                }, ensure_ascii=False)
            }
        )
        fiber = resp.json()
        if fiber.get("status") == "succeeded":
            return fiber["context"].get("output", "喵~")
        return "喵~（工具调用出了点小问题）"


# Chat Completions 集成示例
async def chat_with_mew():
    """在对话中自动触发 mew 工具的场景"""
    openai_client = AsyncOpenAI(base_url=MOONSHOT_BASE_URL, api_key=MOONSHOT_API_KEY)

    async with httpx.AsyncClient(
        base_url=MOONSHOT_BASE_URL,
        headers={"Authorization": f"Bearer {MOONSHOT_API_KEY}"},
        timeout=30.0
    ) as http_client:
        # 获取工具定义
        tools_resp = await http_client.get("/formulas/moonshot/mew:latest/tools")
        mew_tools = tools_resp.json().get("tools", [])

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个友善的助手。当用户达成目标、完成任务或表达开心情绪时，"
                "你可以调用 mew 工具送上一句可爱的猫叫祝福，让对话更温暖。"
                "注意：只在合适的轻松时刻使用，不要过于频繁。"
            )
        },
        {"role": "user", "content": "我终于把代码写完了，好开心！"}
    ]

    response = await openai_client.chat.completions.create(
        model="kimi-k2.6",
        messages=messages,
        tools=mew_tools
    )

    message = response.choices[0].message
    if message.tool_calls:
        messages.append(message)

        for call in message.tool_calls:
            args = json.loads(call.function.arguments)
            result = await call_mew(
                style=args.get("style", "cute"),
                include_blessing=args.get("include_blessing", True)
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result
            })

        final = await openai_client.chat.completions.create(
            model="kimi-k2.6",
            messages=messages,
            tools=mew_tools
        )
        print(final.choices[0].message.content)
    else:
        print(message.content)


if __name__ == "__main__":
    asyncio.run(chat_with_mew())
```

### 5.3.7 使用场景

| 场景 | 触发时机 |
|:---|:---|
| **任务完成庆祝** | 用户成功完成一项任务（写完代码、通过考试、完成健身目标） |
| **情绪缓解** | 用户表达疲惫、压力或低落情绪时，提供轻松的温暖互动 |
| **破冰开场** | 与新用户首次对话时，以趣味方式建立友好第一印象 |
| **节日祝福** | 在特殊日期（生日、新年等）为用户送上独特祝福 |
| **儿童交互** | 面向儿童用户的对话场景中增加趣味元素 |

### 5.3.8 注意事项

1. **场景适配性**：`mew` 是纯粹的趣味性工具，不适合严肃场景（如客服投诉处理、法律咨询、医疗问诊等）。在正式或紧急对话中调用 `mew` 可能会降低产品的专业可信度。

2. **使用频率控制**：过度频繁地使用 `mew` 会导致用户产生审美疲劳甚至反感。建议在 system prompt 中明确限制调用频率（如"每轮对话最多调用一次"或"仅在用户表达积极情绪时调用"）。

3. **文化敏感性**：猫叫声在不同文化中的情感联想可能存在差异。虽然"猫"在全球范围内普遍被视为正面形象，但在面向特定文化背景的用户群体时，仍需关注反馈并适时调整。

4. **返回值处理**：`mew` 的返回值是明文字符串（非加密），可直接展示给用户。由于返回内容带有随机性，不建议在需要固定输出的自动化流程中依赖 `mew` 的具体返回值。

---

## 5.4 三个辅助类工具的对比总结

| 维度 | rethink | random-choice | mew |
|:---:|:---:|:---:|:---:|
| **核心定位** | 认知增强工具 | 决策辅助工具 | 情感互动工具 |
| **功能本质** | 结构化思考 | 随机抽样 | 趣味内容生成 |
| **输入复杂度** | 高（需完整思考内容） | 中（需选项列表） | 低（仅需风格偏好） |
| **返回值类型** | 结构化文本 | JSON（含 selected 数组） | 纯文本字符串 |
| **响应延迟** | 中（~1-3s） | 低（~100-300ms） | 低（~100-300ms） |
| **适用场景数** | 广泛（复杂分析场景） | 中等（需随机决策场景） | 有限（轻松互动场景） |
| **安全性要求** | 无特殊要求 | 不适合高安全场景 | 无特殊要求 |
| **与模型协同** | 深度协同（Thinking 模式补充） | 独立调用为主 | 轻度协同（情感点缀） |

---

> **文档信息**
> - 版本：v1.0
> - 适用平台：Kimi 开放平台（Moonshot AI）
> - 最后更新：2025 年 1 月
> - Formula 版本：`moonshot/rethink:latest`、`moonshot/random-choice:latest`、`moonshot/mew:latest`