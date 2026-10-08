"""Minimal MCP server over stdio: initialize, ping, tools/list, tools/call.

Why not the SDK: `import mcp` runs the package __init__, which pulls in the
client, HTTP, auth and telemetry stacks (about 550 modules) before any server
code runs. On Windows, with every file open scanned, that alone took 5-30s and
pushed coord past Claude Code's 30s MCP connect timeout. This module needs only
the stdlib and pydantic, which the tool input models already use.

Speaks the initialize-handshake protocol revisions, one request at a time.
No batches (dropped in 2025-06-18); notifications are accepted and ignored. `server/discover` (the
stateless 2026 revision) answers Method not found, as the SDK server did, and
clients fall back to `initialize`. The wire output matches what MCPServer
produced for these tools: same schemas, same error texts.
"""

from __future__ import annotations

import inspect
import json
import sys
import traceback
import typing
from typing import Any, Callable

from pydantic import BaseModel, create_model

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class _Tool:
    def __init__(self, name: str, fn: Callable[..., Any], annotations: dict[str, Any] | None):
        self.name = name
        self.fn = fn
        hints = typing.get_type_hints(fn)
        fields = {p: (hints.get(p, Any), ...) for p in inspect.signature(fn).parameters}
        self.args_model: type[BaseModel] = create_model(f"{fn.__name__}Arguments", **fields)
        self.output_model: type[BaseModel] = create_model(f"{fn.__name__}Output", result=(str, ...))
        self.annotations = annotations

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.fn.__doc__ or "",
            "inputSchema": self.args_model.model_json_schema(by_alias=True),
            "outputSchema": self.output_model.model_json_schema(),
        }
        if self.annotations:
            out["annotations"] = self.annotations
        return out

    def parse_args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # Some clients (Claude Desktop) send a sub-model as a JSON string.
        data = dict(arguments)
        for key, value in arguments.items():
            if key in self.args_model.model_fields and isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(parsed, (str, int, float)):
                    data[key] = parsed
        model = self.args_model.model_validate(data)
        return {k: getattr(model, k) for k in self.args_model.model_fields}


class Server:
    def __init__(self, name: str, *, instructions: str | None = None, version: str | None = None):
        self.name = name
        self.instructions = instructions
        self.version = version
        self._tools: dict[str, _Tool] = {}

    def tool(self, name: str, annotations: dict[str, Any] | None = None):
        def deco(fn):
            self._tools[name] = _Tool(name, fn, annotations)
            return fn
        return deco

    def list_tools(self) -> list[dict[str, Any]]:
        return [t.describe() for t in self._tools.values()]

    # ---------------------------------------------------------------- dispatch

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
        info = {"name": self.name}
        if self.version:
            info["version"] = self.version
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {
                "experimental": {},
                "prompts": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "tools": {"listChanged": False},
            },
            "serverInfo": info,
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self._tools.get(name)  # type: ignore[arg-type]
        if tool is None:
            return _tool_error(f"Unknown tool: {name}")
        try:
            kwargs = tool.parse_args(params.get("arguments") or {})
            text = tool.fn(**kwargs)
            if inspect.iscoroutine(text):
                import asyncio  # not at startup: ~30 modules nobody needs to connect
                text = asyncio.run(text)
        except Exception as e:
            return _tool_error(f"Error executing tool {name}: {e}")
        text = str(text)
        return {"content": [{"type": "text", "text": text}],
                "structuredContent": {"result": text}, "isError": False}

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """One JSON-RPC message in, one response (or None for a notification) out."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            rid = msg.get("id") if isinstance(msg, dict) else None
            if isinstance(msg, dict) and "method" not in msg:
                return None  # a response to something we never send; ignore
            return _error(rid, INVALID_REQUEST, "Invalid request")
        method, rid = msg["method"], msg.get("id")
        if "id" not in msg:
            return None  # notifications/initialized, cancelled, progress: nothing to do
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                return _result(rid, self._initialize(params))
            if method == "ping":
                return _result(rid, {})
            if method == "tools/list":
                return _result(rid, {"tools": self.list_tools()})
            if method == "tools/call":
                return _result(rid, self._call_tool(params))
            if method == "resources/list":
                return _result(rid, {"resources": []})
            if method == "resources/templates/list":
                return _result(rid, {"resourceTemplates": []})
            if method == "prompts/list":
                return _result(rid, {"prompts": []})
            return _error(rid, METHOD_NOT_FOUND, "Method not found", method)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return _error(rid, INTERNAL_ERROR, str(e))

    def run(self) -> None:
        """Newline-delimited JSON-RPC on stdin/stdout until stdin closes."""
        stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError as e:
                replies: list[Any] = [_error(None, PARSE_ERROR, f"Parse error: {e}")]
            else:
                replies = [r for r in [self.handle(msg)] if r is not None]
            for reply in replies:
                stdout.write(json.dumps(reply, ensure_ascii=False, default=str).encode("utf-8") + b"\n")
                stdout.flush()


def _result(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}
