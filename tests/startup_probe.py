"""Time how long `python -m coord_mcp` takes to answer `initialize`.

Claude Code gives an MCP server 30s to connect. This splits that time into bare
interpreter startup (`python -c pass`) and the rest, so a slow connect can be
pinned on the interpreter (Store Python, Defender) or on our imports.

Run with the interpreter the .mcp.json files launch, e.g.
    C:\\development\\agents\\agent-tracking\\.venv\\Scripts\\python.exe tests\\startup_probe.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def bare() -> float:
    t = time.perf_counter()
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    return time.perf_counter() - t


def initialize(db: str) -> float:
    env = dict(os.environ, COORD_DB=db, PYTHONPATH=str(ROOT))
    t = time.perf_counter()
    p = subprocess.Popen([sys.executable, "-m", "coord_mcp"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, env=env)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "probe"}}}).encode() + b"\n")
    p.stdin.flush()
    reply = json.loads(p.stdout.readline())
    elapsed = time.perf_counter() - t
    p.stdin.close()
    p.wait(10)
    assert reply["result"]["serverInfo"]["name"] == "coord_mcp", reply
    return elapsed


def main() -> None:
    db = str(Path(tempfile.mkdtemp()) / "probe.db")
    print(f"{sys.executable}  ({sys.version.split()[0]})")
    for i in range(RUNS):
        b, init = bare(), initialize(db)
        print(f"run {i + 1}: python -c pass {b:6.2f}s   initialize {init:6.2f}s   (ours {init - b:5.2f}s)")


if __name__ == "__main__":
    main()
