"""The worker's tool servers: JSON-RPC helper, submit_packet, tg_history (integrator)."""
from __future__ import annotations

import io
import json

from twin_agent.channels.mcp_tg_history import tg_history_tool
from twin_agent.db import StateDB
from twin_agent.mcp_stdio import handle_message, serve_stdio
from twin_agent.testing import fakes
from twin_agent.worker.mcp_submit_packet import TOOL_SCHEMA, submit_packet

ECHO = ({"name": "echo", "inputSchema": {"type": "object"}}, lambda a: {"got": a.get("x")})
BOOM = ({"name": "boom", "inputSchema": {"type": "object"}}, lambda a: 1 / 0)


def test_initialize_list_call_and_errors():
    init = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}}, "srv", [ECHO])
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, "srv", [ECHO]) is None
    tools = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "srv", [ECHO])
    assert [t["name"] for t in tools["result"]["tools"]] == ["echo"]
    call = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "echo", "arguments": {"x": 5}}}, "srv", [ECHO])
    assert json.loads(call["result"]["content"][0]["text"]) == {"got": 5}
    assert call["result"]["isError"] is False
    unknown = handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"}, "srv", [ECHO])
    assert unknown["error"]["code"] == -32601


def test_a_crashing_tool_is_an_error_result_not_a_dead_server():
    out = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "boom"}},
                         "srv", [BOOM])
    assert out["result"]["isError"] is True
    assert "ZeroDivisionError" in out["result"]["content"][0]["text"]


def test_serve_stdio_over_pipes():
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), "not json",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]
    out = io.StringIO()
    serve_stdio("srv", [ECHO], stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert len(replies) == 2 and replies[1]["error"]["code"] == -32700


def test_submit_packet_writes_only_a_valid_packet(tmp_path):
    bad = submit_packet({"packet": {"request_id": "REQ-0001"}}, workdir=tmp_path, request_id="REQ-0001")
    assert bad["ok"] is False and bad["errors"]
    assert not (tmp_path / "packet.json").exists()
    wrong = submit_packet({"packet": fakes.canned_packet("REQ-0002")}, workdir=tmp_path, request_id="REQ-0001")
    assert wrong["ok"] is False
    good = submit_packet({"packet": fakes.canned_packet("REQ-0001")}, workdir=tmp_path, request_id="REQ-0001")
    assert good["ok"] is True
    assert json.loads((tmp_path / "packet.json").read_text(encoding="utf-8"))["request_id"] == "REQ-0001"
    assert TOOL_SCHEMA["name"] == "submit_packet"


def test_tg_history_reads_allow_listed_chats_only(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "data" / "state.db", clock, cfg)
    ok_ev = fakes.make_event("WAVE/00001 stuck", msg_id=10)
    bad_ev = fakes.make_event("secret chat", msg_id=11, chat_id=-100999, topic_id=None)
    db.add_message(ok_ev, "REQ-0001")
    db.add_message(bad_ev, "REQ-0001")
    db.close()
    state_db = tmp_home / "data" / "state.db"
    out = tg_history_tool({"request_id": "REQ-0001"}, state_db=state_db, cfg=cfg)
    assert len(out["messages"]) == 1 and out["dropped_not_allow_listed"] == 1
    assert "untrusted" in out["messages"][0]["text"]
    other = tg_history_tool({"request_id": "REQ-0002"}, state_db=state_db, cfg=cfg, only_request="REQ-0001")
    assert other["messages"] == [] and "own request" in other["error"]
    by_chat = tg_history_tool({"chat_id": -100999}, state_db=state_db, cfg=cfg)
    assert by_chat["messages"] == []
