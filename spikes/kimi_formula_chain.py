"""End-to-end Kimi Formula tool chain: web-search + fetch.

Run: python spikes/kimi_formula_chain.py
"""
import asyncio
import json
import os

import httpx
import yaml
from openai import AsyncOpenAI

BASE_URL = "https://api.moonshot.cn/v1"


def load_config():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def fetch_tool_defs(http: httpx.AsyncClient, api_key: str, uri: str):
    resp = await http.get(
        f"{BASE_URL}/formulas/{uri}/tools",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    return resp.json()["tools"]


async def call_formula(http: httpx.AsyncClient, api_key: str, uri: str, name: str, arguments: dict):
    resp = await http.post(
        f"{BASE_URL}/formulas/{uri}/fibers",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    )
    resp.raise_for_status()
    result = resp.json()
    ctx = result.get("context", {})
    return ctx.get("output") or ctx.get("encrypted_output", "")


async def main():
    cfg = load_config()
    api_key = cfg["llm"]["api_key"]
    model = cfg["llm"]["model"]

    # Correct known model-name typo from older v8 docs.
    if model == "kimi-k2-6":
        model = "kimi-k2.6"

    client = AsyncOpenAI(api_key=api_key, base_url=BASE_URL)

    async with httpx.AsyncClient(timeout=60) as http:
        web_search_tools = await fetch_tool_defs(http, api_key, "moonshot/web-search:latest")
        fetch_tools = await fetch_tool_defs(http, api_key, "moonshot/fetch:latest")
        all_tools = web_search_tools + fetch_tools
        tool_to_uri = {
            "web_search": "moonshot/web-search:latest",
            "fetch": "moonshot/fetch:latest",
        }

        messages = [
            {
                "role": "user",
                "content": "Search for Kimi k2.6 latest release info, then fetch the official docs page.",
            }
        ]

        # First turn: model decides tool calls
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=all_tools,
        )
        msg = response.choices[0].message
        print("finish_reason:", response.choices[0].finish_reason)
        print("tool_calls:", [tc.function.name for tc in msg.tool_calls])

        # Append assistant message with tool_calls
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        # Execute every tool_call and append results
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            uri = tool_to_uri[name]
            print(f"Calling {name}({args})")
            output = await call_formula(http, api_key, uri, name, args)
            print(f"Result length: {len(str(output))}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(output)})

        # Second turn: model produces final answer
        final = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=all_tools,
        )
        print("FINAL:", final.choices[0].message.content[:500])


if __name__ == "__main__":
    asyncio.run(main())
