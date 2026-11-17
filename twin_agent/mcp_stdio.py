"""Tiny stdio MCP server helper (newline-delimited JSON-RPC 2.0): initialize, tools/list, tools/call. No 'mcp' package.

Owner: FLOW (written by the integrator). Used by the worker's three read-only tool servers (CONTRACT section 16).
A handler gets only the tool arguments; it never sees secrets. A handler that raises becomes an error result,
never a crashed server. Logging goes nowhere: stdout is the protocol channel.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

Tool = tuple[dict, Callable[[dict], Any]]   # (tool schema {name, description, inputSchema}, handler(arguments) -> result)
DEFAULT_PROTOCOL = "2024-11-05"


def _ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call(params: dict, tools: list[Tool]) -> dict:
    name = (params or {}).get("name")
    for schema, handler in tools:
        if schema.get("name") == name:
            try:
                out = handler((params or {}).get("arguments") or {})
                failed = isinstance(out, dict) and out.get("ok") is False      # e.g. a packet that did not validate
                return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
                        "isError": bool(failed)}
            except Exception as e:                       # noqa: BLE001 - a tool error must not kill the server
                return {"content": [{"type": "text", "text": f"tool failed: {type(e).__name__}"}], "isError": True}
    return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}


def handle_message(msg: dict, server_name: str, tools: list[Tool]) -> dict | None:
    """Pure function used by serve_stdio and tests. Returns the response, or None for notifications."""
    if not isinstance(msg, dict):
        return _err(None, -32600, "invalid request")
    method, msg_id = msg.get("method"), msg.get("id")
    if "id" not in msg:                                  # notifications (e.g. notifications/initialized)
        return None
    if method == "initialize":
        version = ((msg.get("params") or {}).get("protocolVersion")) or DEFAULT_PROTOCOL
        return _ok(msg_id, {"protocolVersion": version, "capabilities": {"tools": {}},
                            "serverInfo": {"name": server_name, "version": "0.1"}})
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": [schema for schema, _ in tools]})
    if method == "tools/call":
        return _ok(msg_id, _call(msg.get("params") or {}, tools))
    return _err(msg_id, -32601, f"method not found: {method}")


def serve_stdio(server_name: str, tools: list[Tool], *, stdin=None, stdout=None) -> None:
    """Read JSON-RPC lines from stdin, answer on stdout, until EOF. Handlers never get secrets."""
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict | None = _err(None, -32700, "parse error")
        else:
            reply = handle_message(msg, server_name, tools)
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False) + "\n")
            out.flush()
