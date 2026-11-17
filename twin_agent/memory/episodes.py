"""Episodic memory: an append-only timeline of what happened per request.

The "remember specific events" layer. Events already live in state.db / the audit log; here we store a short,
redacted summary per episode so the twin can recall "what happened on REQ-xxxx / this ticket / this client".
Append-only: there is no update or delete API.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import paths
from ..redact import redact
from .store import MemoryDB


def append(mem: MemoryDB, *, request_ref: str = "", kind: str = "", summary: str = "",
           ticket_ref: str = "", client: str = "", ts: str | None = None, src: str | None = None) -> int:
    """Add one episode. Summary is redacted. ts defaults to now (UTC ISO). `src` (a source event id) makes the
    write idempotent, so re-summarising state.db does not duplicate episodes. Returns row id (0 if a dupe)."""
    cur = mem.conn.execute(
        "INSERT OR IGNORE INTO episodes (request_ref, ticket_ref, client, ts, kind, summary, src) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (redact(request_ref or ""), redact(ticket_ref or ""), redact((client or "").strip()),
         ts or mem.now(), redact(kind or ""), redact(summary or ""), src))
    return cur.lastrowid if cur.rowcount else 0


def from_state_db(mem: MemoryDB, state_path: Path | None = None) -> int:
    """Summarise the state.db `events` table (already redacted by db.log_event) into episodes, one per event,
    idempotent on the event id. This is the "events already in state.db -> episodes" layer. Returns the count."""
    p = Path(state_path or paths.state_db())
    if not p.exists():
        return 0
    con = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT id, ts, kind, request_id, actor, detail_json FROM events ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    n = 0
    for r in rows:
        detail = (r["detail_json"] or "").strip()
        summary = f"{r['actor'] or 'system'}: {r['kind']}" + (f" {detail}" if detail and detail != "{}" else "")
        n += 1 if append(mem, request_ref=r["request_id"] or "", kind=r["kind"] or "", summary=summary[:400],
                          ts=r["ts"], src=f"event:{r['id']}") else 0
    return n


def timeline(conn: sqlite3.Connection, *, request_ref: str | None = None, ticket_ref: str | None = None,
             client: str | None = None, since: str | None = None, until: str | None = None,
             k: int = 200) -> list[dict]:
    """Episodes matching the filters, oldest first (ts is UTC ISO, so text comparison sorts correctly)."""
    sql = "SELECT * FROM episodes WHERE 1=1"
    args: list = []
    for col, val in (("request_ref", request_ref), ("ticket_ref", ticket_ref), ("client", client)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    if since is not None:
        sql += " AND ts >= ?"
        args.append(since)
    if until is not None:
        sql += " AND ts <= ?"
        args.append(until)
    sql += " ORDER BY ts, id LIMIT ?"
    args.append(max(1, min(int(k), 1000)))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
