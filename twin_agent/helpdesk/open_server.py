"""The twin's local "Open as a session in Claude Code" endpoint (Principal 2026-11-12).

The routed-ticket email links to http://127.0.0.1:<port>/open/<ticket id>, because Gmail and Odoo's sanitizer may
drop a custom `claude://` href. This server answers with a small page that opens the Claude Code deep link
`claude://code/new?q=<prompt>&folder=<hub>` (and shows it as a button too).

Safety:
- It binds 127.0.0.1 only and refuses any other Host header (no DNS rebinding).
- It only opens tickets the twin itself routed (a `ticket_routed` event).
- It reads state.db read-only, and the prompt carries no secret: the request, the analysis and the rules.
"""
from __future__ import annotations

import html
import http.server
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from . import analyst, ticket_mail

_PATH = re.compile(r"^/open/(\d{1,9})/?$")


def _ro(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def build_link(db_path, tid: int) -> str | None:
    """The claude:// deep link for a ticket the twin routed, or None."""
    con = _ro(db_path)
    try:
        ev = con.execute("SELECT request_id, detail_json FROM events WHERE kind = 'ticket_routed' "
                         "AND detail_json LIKE ? ORDER BY id DESC LIMIT 1", (f'%"ticket": {int(tid)},%',)).fetchone()
        if not ev:
            return None
        d = json.loads(ev["detail_json"] or "{}")
        if int(d.get("ticket") or 0) != int(tid):
            return None
        rid = ev["request_id"]
        req = con.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        pk = con.execute("SELECT packet_json FROM packets WHERE request_id = ? ORDER BY run_no DESC LIMIT 1",
                         (rid,)).fetchone()
        msgs = con.execute("SELECT text FROM messages WHERE request_id = ? AND NOT COALESCE(is_principal, 0) "
                           "ORDER BY ts, event_id", (rid,)).fetchall()
    finally:
        con.close()
    track = d.get("track") if d.get("track") in analyst.TRACKS else "functional"
    typ = d.get("type") if d.get("type") in analyst.TYPES[track] else analyst.TYPES[track][0]
    level = str(d.get("level") or "RED")
    r = analyst.Routing(track=track, type=typ, confidence=float(d.get("confidence") or 0), level=level,
                        unsure=level != "GREEN", reasons=list(d.get("reasons") or []))
    packet = json.loads(pk["packet_json"]) if pk and pk["packet_json"] else {}
    message = " / ".join(str(m["text"]).strip() for m in msgs if (m["text"] or "").strip()) or "(photo only)"
    prompt = ticket_mail.session_prompt(int(tid), rid, dict(req) if req else {"id": rid}, r, packet, message)
    return ticket_mail.claude_link(prompt)


def _page(link: str) -> bytes:
    a = html.escape(link, quote=True)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Open in Claude Code</title></head>'
            f'<body style="font-family:sans-serif;padding:24px">'
            f'<p>Opening Claude Code with this ticket. If nothing happens, press the button.</p>'
            f'<p><a href="{a}" style="font-size:18px">Open as a session in Claude Code</a></p>'
            f'<script>window.location.href = {json.dumps(link)};</script></body></html>').encode("utf-8")


def make_server(db_path, *, host: str = "127.0.0.1", port: int = 8766) -> http.server.ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the open-session server binds 127.0.0.1 only")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):                      # never log paths or prompts
            return

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            hostname = (self.headers.get("Host") or "").split(":")[0].lower()
            if hostname not in ("127.0.0.1", "localhost"):
                return self._send(403, b"forbidden", "text/plain")
            m = _PATH.match(self.path.split("?", 1)[0])
            if not m:
                return self._send(404, b"not found", "text/plain")
            try:
                link = build_link(db_path, int(m.group(1)))
            except Exception:                           # noqa: BLE001 - a broken read is a 404, never a trace
                link = None
            if not link:
                return self._send(404, b"This ticket was not routed by Digital Twin.", "text/plain")
            return self._send(200, _page(link))

    return http.server.ThreadingHTTPServer((host, int(port)), Handler)


def start(db_path, port: int) -> SimpleNamespace:
    """Start in a daemon thread. Returns a handle with .server (None when the port is taken)."""
    import threading
    try:
        srv = make_server(db_path, port=port)
    except OSError:
        return SimpleNamespace(server=None)
    threading.Thread(target=srv.serve_forever, name="open-session-server", daemon=True).start()
    return SimpleNamespace(server=srv)
