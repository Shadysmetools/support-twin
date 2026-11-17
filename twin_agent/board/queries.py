"""Read-only queries for the board. Opens state.db with mode=ro; a missing db or table gives empty lists.

Every text value goes through redact() before it leaves this module.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from ..clock import elapsed, utc
from ..models import TERMINAL, parse_ts, utc_iso
from ..redact import redact

_TERMINAL = tuple(str(s) for s in TERMINAL)


def connect_ro(db_path: Path) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.exists():
        return None
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql: str, args=()) -> list[dict]:
    if con is None:
        return []
    try:
        return [{k: (redact(v) if isinstance(v, str) else v) for k, v in dict(r).items()}
                for r in con.execute(sql, args)]
    except sqlite3.Error:
        return []


def requests_live(con) -> list[dict]:
    marks = ",".join("?" * len(_TERMINAL))
    return _rows(con, f"""SELECT id, state, lane, tier, company, channel, chat, topic, requester_name, title,
        created_at, updated_at, first_reply_at, first_reply_by, taken_over, ticket_url
        FROM requests WHERE state NOT IN ({marks}) ORDER BY created_at""", _TERMINAL)


def timers_open(con, limit: int = 100) -> list[dict]:
    return _rows(con, """SELECT id, request_id, kind, due_at FROM timers
        WHERE fired_at IS NULL AND cancelled_at IS NULL ORDER BY due_at LIMIT ?""", (limit,))


def cards_recent(con, limit: int = 30) -> list[dict]:
    return _rows(con, """SELECT id, request_id, kind, to_user, text, silent, created_at, sent_at
        FROM cards ORDER BY id DESC LIMIT ?""", (limit,))


def events_recent(con, limit: int = 50) -> list[dict]:
    return _rows(con, "SELECT id, ts, kind, request_id, actor, detail_json FROM events ORDER BY id DESC LIMIT ?",
                 (limit,))


def _minutes(a: str | None, b: str | None) -> float | None:
    ta, tb = parse_ts(a), parse_ts(b)
    return elapsed(ta, tb).total_seconds() / 60 if ta and tb else None


def kpis(con, now: datetime) -> dict:
    """Live KPI numbers (05 9.4), computed from the tables; plus the latest stored kpis rows."""
    since = utc(now) - timedelta(days=7)
    reqs = _rows(con, "SELECT id, state, created_at, first_reply_at FROM requests")
    recent = [r for r in reqs if utc(parse_ts(r["created_at"]) or now) >= since]
    waits = [m for r in recent if (m := _minutes(r["created_at"], r["first_reply_at"])) is not None]
    unanswered = [r for r in reqs if not r["first_reply_at"] and r["state"] not in _TERMINAL
                  and utc(parse_ts(r["created_at"]) or now) <= utc(now) - timedelta(hours=48)]
    sends = _rows(con, "SELECT status, COUNT(*) AS n FROM sends WHERE created_at >= ? GROUP BY status",
                  (utc_iso(since),))
    stored = _rows(con, """SELECT k.name, k.lane, k.value, k.day FROM kpis k JOIN
        (SELECT name, IFNULL(lane, '') AS l, MAX(id) AS mid FROM kpis GROUP BY name, IFNULL(lane, '')) x
        ON k.id = x.mid ORDER BY k.name""")
    return {
        "open_requests": sum(1 for r in reqs if r["state"] not in _TERMINAL),
        "requests_7d": len(recent),
        "first_reply_median_min_7d": round(statistics.median(waits), 1) if waits else None,
        "waiting_first_reply": sum(1 for r in reqs if not r["first_reply_at"] and r["state"] not in _TERMINAL),
        "unanswered_48h": len(unanswered),
        "sends_7d": {r["status"]: r["n"] for r in sends},
        "stored": stored,
    }


def audit_tail(logs_dir: Path, n: int = 30) -> list[dict]:
    """Last n lines of actions.jsonl, redacted. Never raises."""
    p = Path(logs_dir) / "actions.jsonl"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            rec.pop("prev_hash", None)
            rec.pop("hash", None)
            out.append(json.loads(redact(json.dumps(rec, ensure_ascii=False))))
        except (ValueError, TypeError):
            out.append({"raw": redact(line)})
    return out
