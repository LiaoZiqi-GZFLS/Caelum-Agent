"""Connect to the verified filesystem MCP server and list its tools.

Usage: python verify_filesystem_mcp.py <allowed-dir>
"""
import asyncio
import sys
from list_mcp_tools import list_tools

if __name__ == "__main__":
    allowed_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    asyncio.run(list_tools("npx", ["-y", "@modelcontextprotocol/server-filesystem@latest", allowed_dir]))
