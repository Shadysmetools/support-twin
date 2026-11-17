"""The Twin Monitor's OWN SQLite (Sysadmin's device). The only file this package writes rows to.

Ingest is idempotent: every record has a stable `key` (twin + source row), so re-reading a bundle, or two bundles
that overlap, adds nothing twice. Request snapshots are upserted (the latest view wins). Alerts are deduped by key,
so an alert is raised once and delivered once.
It refuses to open Principal's state.db (or any file named state.db): the monitor never writes to the twin's state.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, twin TEXT, bundle TEXT, cat TEXT, kind TEXT,
  request_id TEXT, ts TEXT, data_json TEXT);
CREATE TABLE IF NOT EXISTS requests (twin TEXT, request_id TEXT, ts TEXT, data_json TEXT,
  PRIMARY KEY (twin, request_id));
CREATE TABLE IF NOT EXISTS bundles (id TEXT PRIMARY KEY, twin TEXT, made_at TEXT, consent_hash TEXT, d4 INTEGER,
  records INTEGER);
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, ts TEXT, severity TEXT, kind TEXT, request_id TEXT,
  text TEXT, evidence TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS dev (key TEXT PRIMARY KEY, kind TEXT, ts TEXT, data_json TEXT);
CREATE INDEX IF NOT EXISTS rec_req ON records(request_id);
CREATE INDEX IF NOT EXISTS rec_ts ON records(ts);
"""


class RefusedPath(Exception):
    pass


def default_path() -> Path:
    return paths.data_dir() / "monitor" / "monitor.db"


class MonitorStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or default_path())
        if self.path.name.lower() == "state.db" or self.path.resolve() == paths.state_db().resolve():
            raise RefusedPath("the monitor never opens the twin's state.db for writing")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def all(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    # ------------------------------------------------------------------ ingest
    def ingest(self, head: dict, recs: list[dict]) -> list[dict]:
        """Store one bundle. Returns the records that were NEW (alerts look at these only)."""
        twin = str(head.get("twin") or "principal")
        new: list[dict] = []
        with self.conn:
            c = head.get("consent") or {}
            self.conn.execute("INSERT OR IGNORE INTO bundles VALUES (?,?,?,?,?,?)",
                              (head.get("id"), twin, head.get("made_at"), c.get("hash"), int(bool(c.get("d4"))),
                               len(recs)))
            for r in recs:
                if r.get("cat") == "request":
                    self.conn.execute("INSERT INTO requests VALUES (?,?,?,?) ON CONFLICT(twin, request_id) DO "
                                      "UPDATE SET ts = excluded.ts, data_json = excluded.data_json",
                                      (twin, r.get("request_id"), r.get("ts"), json.dumps(r, ensure_ascii=False)))
                    continue
                cur = self.conn.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?)",
                                        (r["key"], twin, head.get("id"), r.get("cat"), r.get("kind"),
                                         r.get("request_id"), r.get("ts"), json.dumps(r, ensure_ascii=False)))
                if cur.rowcount:
                    new.append(r)
        return new

    def add_dev(self, rows: list[dict]) -> list[dict]:
        new = []
        with self.conn:
            for r in rows:
                cur = self.conn.execute("INSERT OR IGNORE INTO dev VALUES (?,?,?,?)",
                                        (r["key"], r.get("kind"), r.get("ts"), json.dumps(r, ensure_ascii=False)))
                if cur.rowcount:
                    new.append(r)
        return new

    # ------------------------------------------------------------------ reads
    def records(self, *, cat: str | None = None, kind: str | None = None, request_id: str | None = None,
                since: str | None = None, until: str | None = None) -> list[dict]:
        sql, args = "SELECT data_json FROM records WHERE 1=1", []
        for col, val in (("cat", cat), ("kind", kind), ("request_id", request_id)):
            if val is not None:
                sql, args = sql + f" AND {col} = ?", args + [val]
        if since:
            sql, args = sql + " AND ts >= ?", args + [since]
        if until:
            sql, args = sql + " AND ts < ?", args + [until]
        return [json.loads(r["data_json"]) for r in self.all(sql + " ORDER BY ts, key", args)]

    def requests(self) -> list[dict]:
        return [json.loads(r["data_json"]) for r in self.all("SELECT data_json FROM requests ORDER BY request_id")]

    def dev_rows(self, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT data_json FROM dev", []
        if kind:
            sql, args = sql + " WHERE kind = ?", [kind]
        return [json.loads(r["data_json"]) for r in self.all(sql + " ORDER BY ts", args)]

    def bundles(self) -> list[dict]:
        return self.all("SELECT * FROM bundles ORDER BY made_at, id")

    # ------------------------------------------------------------------ alerts
    def raise_alert(self, a: dict) -> bool:
        """Store an alert once. True if it is new."""
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?,?,NULL)",
                                    (a["key"], a.get("ts"), a["severity"], a["kind"], a.get("request_id"), a["text"],
                                     ";".join(a.get("evidence") or [])))
        return bool(cur.rowcount)

    def unsent_alerts(self) -> list[dict]:
        return self.all("SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY ts, key")

    def mark_sent(self, key: str, at: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE alerts SET sent_at = ? WHERE key = ?", (at, key))

    def alerts(self, since: str | None = None, until: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM alerts WHERE 1=1", []
        if since:
            sql, args = sql + " AND ts >= ?", args + [since]
        if until:
            sql, args = sql + " AND ts < ?", args + [until]
        return self.all(sql + " ORDER BY ts, key", args)
