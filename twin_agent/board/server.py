"""Read-only board on 127.0.0.1:8765 (red-team #22: Host check + URL token, no actions).

GET /?t=<token>            live HTML page (auto refresh)
GET /api/<part>?t=<token>  JSON: requests, timers, cards, kpis, audit, events
Anything else: 403 (bad host or token), 404 (unknown path) or 405 (not GET). There are no action endpoints.

CLI: python -I aa.py board [--port 8765] [--token T]   (a random token is made if none is given)
"""
from __future__ import annotations

import argparse
import hmac
import html
import http.server
import json
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import paths
from ..clock import Clock, SystemClock
from ..models import parse_ts
from . import queries as Q

PARTS = ("requests", "timers", "cards", "kpis", "audit", "events")


def collect(db_path: Path, logs_dir: Path, now: datetime) -> dict:
    con = Q.connect_ro(db_path)
    try:
        return {"now": now.isoformat(timespec="seconds"), "requests": Q.requests_live(con),
                "timers": Q.timers_open(con), "cards": Q.cards_recent(con), "kpis": Q.kpis(con, now),
                "audit": Q.audit_tail(logs_dir), "events": Q.events_recent(con), "watcher": _watcher(con, now)}
    finally:
        if con is not None:
            con.close()


def _watcher(con, now: datetime) -> str:
    """The Watcher's visible indicator (recording / paused / watching / off). Never raises."""
    try:
        from ..config import load_config
        from ..watcher import gate
        rows = Q._rows(con, "SELECT id, ts, kind, detail_json FROM events WHERE kind IN (?, ?)",
                       (gate.PAUSE, gate.UNPAUSE)) if con is not None else []
        try:
            cfg = load_config()
        except Exception:                                   # noqa: BLE001 - no config: show off / paused only
            cfg = None
        return gate.indicator(cfg, gate.windows_from_rows(rows, now), now)
    except Exception:                                       # noqa: BLE001
        return "unknown"


def _local(ts: str | None) -> str:
    dt = parse_ts(ts)
    return dt.strftime("%a %d %b %H:%M") if dt else ""


def _table(rows: list[dict], cols: list[str], times: tuple[str, ...] = ()) -> str:
    if not rows:
        return "<p class=empty>none</p>"
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(
        f"<td>{html.escape(_local(r.get(c)) if c in times else str(r.get(c) if r.get(c) is not None else ''))}</td>"
        for c in cols) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def render_html(data: dict, token: str, refresh_s: int = 10) -> str:
    k = data["kpis"]
    tiles = [("Open", k["open_requests"]), ("Waiting first reply", k["waiting_first_reply"]),
             ("Unanswered 48 h", k["unanswered_48h"]), ("Requests 7 d", k["requests_7d"]),
             ("First reply median 7 d (min)", k["first_reply_median_min_7d"] if k["first_reply_median_min_7d"]
              is not None else "-")]
    tiles += [(f"Sends 7 d: {s}", n) for s, n in sorted(k["sends_7d"].items())]
    tile_html = "".join(f"<div class=tile><b>{html.escape(str(v))}</b><span>{html.escape(n)}</span></div>"
                        for n, v in tiles)
    audit = [{"ts": a.get("ts", ""), "what": json.dumps({x: y for x, y in a.items() if x != "ts"},
                                                        ensure_ascii=False)[:300]} for a in data["audit"]]
    t = html.escape(token, quote=True)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{int(refresh_s)};url=/?t={t}"><title>Principal's Agent board</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px;color:#1d2433}}h2{{margin:20px 0 6px;font-size:15px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #e3e6ea;padding:4px 6px;text-align:left;
vertical-align:top}}th{{color:#5b6472;font-weight:600}}.tile{{display:inline-block;margin:0 8px 8px 0;padding:8px 12px;
border:1px solid #e3e6ea;border-radius:6px}}.tile b{{display:block;font-size:20px}}.tile span{{color:#5b6472}}
.empty{{color:#8a93a0}}</style></head><body>
<h1 style="font-size:18px">Principal's Agent: live board (read-only)</h1><p class=watcher><b>Watcher:</b> {html.escape(str(data.get("watcher", "unknown")))}</p><p class=empty>As of {html.escape(data["now"])}</p>
<div>{tile_html}</div>
<h2>Live requests</h2>{_table(data["requests"], ["id", "state", "lane", "tier", "company", "chat", "topic",
    "requester_name", "created_at", "first_reply_at", "first_reply_by"], ("created_at", "first_reply_at"))}
<h2>Timers (next due)</h2>{_table(data["timers"][:40], ["request_id", "kind", "due_at"], ("due_at",))}
<h2>Cards (latest)</h2>{_table(data["cards"], ["id", "request_id", "kind", "to_user", "created_at", "text"],
    ("created_at",))}
<h2>Audit tail (actions.jsonl)</h2>{_table(audit, ["ts", "what"], ("ts",))}
<h2>Events</h2>{_table(data["events"][:30], ["ts", "kind", "request_id", "actor", "detail_json"], ("ts",))}
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TwinAgentBoard"
    sys_version = ""

    def log_message(self, fmt, *args):          # never log URLs: they carry the token
        pass

    def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _allowed(self, query: dict) -> bool:
        srv = self.server
        want_host = f"127.0.0.1:{srv.server_address[1]}"
        given = (query.get("t") or [""])[0]
        return self.headers.get("Host", "") == want_host and hmac.compare_digest(given.encode(), srv.token.encode())

    def do_GET(self):
        parts = urlsplit(self.path)
        if not self._allowed(parse_qs(parts.query)):
            return self._send(403, "forbidden")
        srv = self.server
        path = parts.path.rstrip("/") or "/"
        if path == "/":
            data = collect(srv.db_path, srv.logs_dir, srv.clock.now())
            return self._send(200, render_html(data, srv.token, srv.refresh_s), "text/html; charset=utf-8")
        if path.startswith("/api/") and path[5:] in PARTS:
            data = collect(srv.db_path, srv.logs_dir, srv.clock.now())
            return self._send(200, json.dumps(data[path[5:]], ensure_ascii=False, default=str),
                              "application/json; charset=utf-8")
        return self._send(404, "not found")

    def _no(self):
        self._send(405, "read-only board")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no

    def do_HEAD(self):
        self._no()


def make_server(db_path: Path, *, host: str = "127.0.0.1", port: int = 8765, token: str) -> http.server.HTTPServer:
    """Bind the read-only board. Only 127.0.0.1 is allowed; a token of at least 16 chars is required."""
    if host != "127.0.0.1":
        raise ValueError("the board binds to 127.0.0.1 only")
    if not token or len(token) < 16:
        raise ValueError("token must be at least 16 characters")
    srv = http.server.ThreadingHTTPServer((host, port), _Handler)
    srv.daemon_threads = True
    srv.db_path, srv.token = Path(db_path), token
    srv.logs_dir = paths.logs_dir()
    srv.clock: Clock = SystemClock()
    srv.refresh_s = 10
    return srv


def serve(db_path: Path, token: str, port: int = 8765) -> None:
    srv = make_server(db_path, port=port, token=token)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="board", description="Serve the read-only board on 127.0.0.1.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="")
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args(argv)
    token = args.token or secrets.token_urlsafe(24)
    print(f"Board: http://127.0.0.1:{args.port}/?t={token}  (Ctrl+C to stop)")
    try:
        serve(args.db or paths.state_db(), token, args.port)
    except KeyboardInterrupt:
        pass
    return 0
