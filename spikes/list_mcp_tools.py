"""Generic helper: list tools exposed by a stdio MCP server."""
import asyncio
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def list_tools(command: str, args: list[str]) -> None:
    params = StdioServerParameters(command=command, args=args, env=None)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Tools ({len(tools.tools)}):")
            for t in tools.tools:
                desc = (t.description or "")[:100]
                print(f"  - {t.name}: {desc}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python list_mcp_tools.py <command> [arg1 arg2 ...]")
        sys.exit(1)
    asyncio.run(list_tools(sys.argv[1], sys.argv[2:]))
