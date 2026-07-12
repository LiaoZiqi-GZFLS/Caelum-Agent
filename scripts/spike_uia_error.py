"""Spike: reproduce the UIA_E_ELEMENTNOTAVAILABLE warning from windows-mcp.

Connects the windows MCP server exactly like production but with RAW stderr
(no noise filter), then calls Snapshot several times. The full warning names
the offending window/handle, which tells us whether the dying element is
ours (console), a specific app, or random desktop churn.

Run: .venv\\Scripts\\python.exe scripts/spike_uia_error.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from agent.config import load_config
from mcp_client import MCPClient


async def main() -> int:
    cfg = load_config()
    assert cfg.mcp_servers.windows is not None
    # Raw stderr on purpose: we want the FULL upstream warning, not the
    # filtered version.
    client = MCPClient("windows", cfg.mcp_servers.windows, errlog=sys.stderr)
    ok = await client.connect()
    print(f"\n=== connected: {ok}, tools: {len(client.tools())} ===", flush=True)
    for i in range(4):
        print(f"\n=== Snapshot #{i + 1} ===", flush=True)
        result = await client.call("Snapshot", {})
        print(f"success={result.success}, content={len(result.content)} chars", flush=True)
        await asyncio.sleep(1.0)
    await client.disconnect()
    print("\n=== done ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
