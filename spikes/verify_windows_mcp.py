"""Connect to the verified Windows MCP server and list its tools."""
import asyncio
from list_mcp_tools import list_tools

if __name__ == "__main__":
    asyncio.run(list_tools("windows-mcp", []))
