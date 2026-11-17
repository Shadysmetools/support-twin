"""MCP server 'submit_packet': validates the worker's packet and writes work/REQ/packet.json. The worker's only output.

Owner: FLOW (written by the integrator). The packet is checked with schemas.validate_packet. A bad packet is
refused with the list of problems so the worker can fix it and submit again. `verified`, the approver and any
script in the packet are ignored later anyway (recomputed in code), so this server only checks the shape.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..schemas import validate_packet

TOOL_SCHEMA = {
    "name": "submit_packet",
    "description": "Submit your ONE packet for this request (see packet.schema.json). If it is refused, fix the "
                   "listed errors and submit again. After an ok answer, stop.",
    "inputSchema": {"type": "object", "properties": {"packet": {"type": "object"}}, "required": ["packet"]},
}


def submit_packet(arguments: dict, *, workdir: Path, request_id: str) -> dict:
    """Validate with schemas.validate_packet; on success write packet.json and return {"ok": true}; else the errors."""
    args = arguments or {}
    packet = args.get("packet", args)                # tolerate a packet sent without the wrapper
    if isinstance(packet, str):                      # some clients send the object as a JSON string
        try:
            packet = json.loads(packet)
        except ValueError:
            return {"ok": False, "errors": ["packet is not valid JSON"]}
    errors = validate_packet(packet, request_id)
    if errors:
        return {"ok": False, "errors": errors[:20]}
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    tmp = workdir / "packet.json.tmp"
    tmp.write_text(json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, workdir / "packet.json")
    return {"ok": True, "message": "packet saved; you are done, stop now"}


def main(argv: list[str] | None = None) -> int:
    """--workdir <path> --request-id REQ-xxxx; runs mcp_stdio.serve_stdio."""
    ap = argparse.ArgumentParser(prog="mcp-submit-packet")
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--request-id", required=True)
    a = ap.parse_args(argv)
    from ..mcp_stdio import serve_stdio
    serve_stdio("submit_packet",
                [(TOOL_SCHEMA, lambda args: submit_packet(args, workdir=a.workdir, request_id=a.request_id))])
    return 0
