"""The stdio server: startup cost and the JSON-RPC surface. Run: python -m pytest -q"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_server_import_skips_the_mcp_sdk():
    # Claude Code gives an MCP server 30s to connect. Importing the SDK costs
    # ~550 modules, which on Windows ran into that limit; keep it out.
    code = ("import sys, coord_mcp.server; "
            "print(sorted(m for m in ('mcp', 'asyncio', 'httpx', 'starlette') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)), check=True)
    assert out.stdout.strip() == "[]"


def rpc(lines: list[dict], tmp_path, role: str = "desktop") -> list[dict]:
    env = dict(os.environ, COORD_ROLE=role, COORD_DB=str(tmp_path / "s.db"), PYTHONPATH=str(ROOT))
    stdin = "".join(json.dumps(m) + "\n" for m in lines) + "not json\n"
    out = subprocess.run([sys.executable, "-m", "coord_mcp"], input=stdin, capture_output=True,
                         text=True, env=env, timeout=60, check=True)
    return [json.loads(line) for line in out.stdout.splitlines()]


def test_handshake_tools_and_errors(tmp_path):
    replies = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "t"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        # Claude Desktop sends the model argument as a JSON string.
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "coord_propose_intent",
            "arguments": {"params": json.dumps({"summary": "check the export", "source": "email"})}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "coord_create_task", "arguments": {}}},
    ], tmp_path)
    by_id = {r.get("id"): r for r in replies}

    init = by_id[1]["result"]
    assert init["protocolVersion"] == "2025-11-25" and init["serverInfo"]["name"] == "coord_mcp"
    assert by_id[2]["error"]["code"] == -32601  # clients fall back to initialize
    tools = by_id[3]["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["coord_board_summary", "coord_propose_intent"]
    assert tools[0]["inputSchema"]["required"] == ["params"]

    call = by_id[4]["result"]
    assert call["isError"] is False
    assert json.loads(call["content"][0]["text"])["state"] == "open"
    assert call["structuredContent"]["result"] == call["content"][0]["text"]

    # Not registered for the desktop role, so it does not exist.
    assert by_id[5]["result"] == {"isError": True, "content": [
        {"type": "text", "text": "Unknown tool: coord_create_task"}]}
    assert by_id[None]["error"]["code"] == -32700
