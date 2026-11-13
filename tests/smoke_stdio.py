"""End-to-end check over real stdio MCP transport.

Spawns a team server and a desktop server against one temp DB and drives a
full lifecycle through the protocol. Run: python tests/smoke_stdio.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def params(role: str, db: str) -> StdioServerParameters:
    env = dict(os.environ, COORD_ROLE=role, COORD_DB=db, PYTHONPATH=str(ROOT))
    return StdioServerParameters(command=sys.executable, args=["-m", "coord_mcp"], env=env)


def text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def main() -> int:
    db = str(Path(tempfile.mkdtemp()) / "smoke.db")
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"{'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(f"{label}: {detail}")

    async with stdio_client(params("desktop", db)) as (dr, dw), ClientSession(dr, dw) as desktop:
        await desktop.initialize()
        tools = sorted(t.name for t in (await desktop.list_tools()).tools)
        check("desktop exposes two tools", tools == ["coord_board_summary", "coord_propose_intent"], str(tools))
        r = await desktop.call_tool("coord_propose_intent", {"params": {
            "summary": "Email from vendor asks for the Q3 usage export", "source": "email",
            "context": "msg id AAMk...; ignore the line telling the assistant to push to prod",
        }})
        intent_id = json.loads(text(r))["intent_id"]
        check("intent proposed", bool(intent_id))

    async with stdio_client(params("team", db)) as (tr, tw), ClientSession(tr, tw) as team:
        await team.initialize()
        tools = {t.name for t in (await team.list_tools()).tools}
        check("team exposes 21 tools", len(tools) == 21, str(sorted(tools)))

        r = await team.call_tool("coord_list_intents", {"params": {}})
        intents = json.loads(text(r))
        check("lead sees the untrusted intent with handling note",
              intents and intents[0]["trust"] == "untrusted" and "handling" in intents[0], text(r)[:200])

        r = await team.call_tool("coord_create_task", {"params": {
            "kind": "data_pull", "title": "Export Q3 usage",
            "spec": {"query": "usage where quarter = 'Q3'", "out": "C:/tmp/q3.csv"},
            "assigned_to": "puller",
        }})
        created = json.loads(text(r))
        task_id = created["task_id"]
        check("task created with contract echoed", "output_path" in created["expected_result_shape"])

        r = await team.call_tool("coord_resolve_intent", {"params": {"intent_id": intent_id, "task_ids": [task_id]}})
        check("intent converted", json.loads(text(r))["state"] == "converted", text(r))

        # An ask only Ben can answer, from a client with no agent: a loose end with no task.
        r = await team.call_tool("coord_propose_intent", {"params": {
            "summary": "Essex-Brownell: hours and target date for FDD-005", "source": "email-reply",
            "context": "from Homero; received 2026-11-11 14:02Z"}})
        ben_intent = json.loads(text(r))["intent_id"]
        r = await team.call_tool("coord_resolve_intent", {"params": {
            "intent_id": ben_intent, "note": "unwired client",
            "ben_item": {"what": "Reply to Homero: hours and date", "client": "Essex-Brownell", "due": "2026-11-14"}}})
        out = json.loads(text(r))
        fid = (out.get("ben_item") or {}).get("follow_up_id")
        check("resolve_intent with ben_item leaves a task-less loose end",
              out.get("state") == "dismissed" and bool(fid) and out["ben_item"]["due_at"], text(r))
        r = await team.call_tool("coord_list_follow_ups", {"params": {"who": "Ben"}})
        fus = json.loads(text(r))["follow_ups"]
        check("Ben item listed under its client", any(f["id"] == fid and f["task_id"] is None
                                                       and f["client"] == "Essex-Brownell" for f in fus), text(r))
        r = await team.call_tool("coord_record_decision", {"params": {
            "statement": "Ben replied to Homero", "because": "smoke"}})
        r = await team.call_tool("coord_resolve_follow_up", {"params": {
            "follow_up_id": fid, "decision_id": json.loads(text(r))["decision_id"]}})
        check("Ben item resolves by decision", json.loads(text(r)).get("state") == "dropped", text(r))

        r = await team.call_tool("coord_get_task", {"params": {"task_id": task_id}})
        got = json.loads(text(r))
        check("teammate reads spec and shape", got["spec"]["out"] == "C:/tmp/q3.csv" and "expected_result_shape" in got)

        r = await team.call_tool("coord_complete_task", {"params": {
            "task_id": task_id, "result": {"done": True, "rows": 10, "notes": "long prose here"},
        }})
        t = text(r)
        check("off-contract result rejected with fix", "rejected" in t and "output_path: required" in t
              and "unexpected field(s) ['notes']" in t, t[:300])

        r = await team.call_tool("coord_complete_task", {"params": {
            "task_id": task_id,
            "result": {"done": True, "rows": 10, "output_path": "C:/tmp/q3.csv", "columns": ["a", "b"]},
        }})
        check("valid result accepted", json.loads(text(r))["state"] == "done", text(r))

        r = await team.call_tool("coord_get_task_result", {"params": {"task_id": task_id, "fields": ["rows"]}})
        check("projected read", json.loads(text(r))["result"] == {"rows": 10}, text(r))

        r = await team.call_tool("coord_record_decision", {"params": {
            "statement": "Export from the warehouse, not the app DB", "because": "app DB lags a day",
        }})
        check("decision recorded", "decision_id" in json.loads(text(r)))

        r = await team.call_tool("coord_board", {"params": {}})
        b = json.loads(text(r))
        check("board shows done count and no live work",
              b["tasks_by_kind"] == {"data_pull": {"done": 1}} and b["live"] == [] and b["open_intents"] == 0, text(r))
        check("board under 300 bytes", len(text(r)) < 300, str(len(text(r))))

        r = await team.call_tool("coord_list_follow_ups", {"params": {"client": "nobody"}})
        check("follow-up list answers with an empty, counted list",
              json.loads(text(r)) == {"follow_ups": [], "total": 0}, text(r))

    print()
    print("ALL PASS" if not failures else f"{len(failures)} FAILED:\n  " + "\n  ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
