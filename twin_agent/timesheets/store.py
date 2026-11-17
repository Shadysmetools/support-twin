"""Timesheet entries and cards (tables in state.db). Status of an entry:
proposed -> approved (Principal's tap, or autopilot when eligible) -> written | prepared (observe) | maybe_written
proposed -> rejected; any change by /ts makes the card `superseded` and a new card is offered.
A ticket-day part is UNIQUE: a recompute updates the open (proposed) part only; later time on a decided
ticket-day becomes a new part holding only the extra minutes (nothing is counted twice).
"""
from __future__ import annotations

import json

from ..helpdesk.ledger import sha

SCHEMA = """
CREATE TABLE IF NOT EXISTS ts_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, key TEXT, minutes REAL,
  hours REAL, name TEXT, how TEXT, status TEXT, card_id INTEGER, edited INTEGER DEFAULT 0, client TEXT,
  billable INTEGER, line_id INTEGER, created_at TEXT, decided_at TEXT, part INTEGER DEFAULT 0,
  UNIQUE(day, key, part));
CREATE TABLE IF NOT EXISTS ts_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, entries_hash TEXT, status TEXT,
  edited INTEGER DEFAULT 0, autopilot INTEGER DEFAULT 0, created_at TEXT, decided_at TEXT);
"""


class TimesheetStore:
    def __init__(self, db):
        self.db = db
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                db._exec(stmt)

    def upsert(self, e, *, min_minutes: float = 8) -> None:
        """Recompute one ticket-day. Minutes already decided (approved, written, rejected) are never changed and
        never counted again: time logged later that day (after the card) becomes a new PART with only the delta."""
        from .entries import round_quarter
        key = e.key or "-"
        rows = self.db._all("SELECT * FROM ts_entries WHERE day = ? AND key = ? ORDER BY part", (e.day, key))
        open_ = [r for r in rows if r["status"] == "proposed"]
        decided_rows = [r for r in rows if r["status"] != "proposed"]
        decided = sum(float(r["minutes"] or 0) for r in decided_rows)
        booked = sum(float(r["hours"] or 0) for r in decided_rows)
        delta = round(float(e.minutes) - decided, 1)
        # Round the whole ticket-day total ONCE (never each part on its own), then book only the hours not yet
        # booked by decided parts. Otherwise 40 min (0.75 h) + 10 min (0.25 h) would bill 1.0 h for 50 min.
        new_hours = max(0.0, round(round_quarter(float(e.minutes), min_minutes) - booked, 2))
        if open_:
            r = open_[-1]
            if not r["edited"]:                            # Principal edited it: keep his values
                self.db._exec("UPDATE ts_entries SET minutes = ?, hours = ?, name = ?, how = ? WHERE id = ?",
                              (delta, new_hours, e.name, e.how, r["id"]))
            return
        if rows and delta <= 0:
            return
        part = (max(int(r["part"] or 0) for r in rows) + 1) if rows else 0
        self.db._exec("INSERT INTO ts_entries (day, key, part, minutes, hours, name, how, status, created_at) VALUES "
                      "(?,?,?,?,?,?,?,?,?)", (e.day, key, part, delta, new_hours, e.name,
                                              e.how, "proposed", self.db._now()))

    def entries(self, day: str | None = None, status: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM ts_entries WHERE 1=1", []
        if day:
            sql, args = sql + " AND day = ?", args + [day]
        if status:
            sql += f" AND status IN ({','.join('?' * len(status))})"
            args += list(status)
        return self.db._all(sql + " ORDER BY day, id", args)

    def get(self, entry_id: int) -> dict | None:
        return self.db._one("SELECT * FROM ts_entries WHERE id = ?", (entry_id,))

    def set(self, entry_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db._exec(f"UPDATE ts_entries SET {cols} WHERE id = ?", (*fields.values(), entry_id))

    @staticmethod
    def card_hash(rows: list[dict]) -> str:
        return sha([{k: r.get(k) for k in ("id", "day", "key", "hours", "name")} for r in rows])

    def new_card(self, day: str, rows: list[dict], *, edited: bool = False) -> tuple[int, str]:
        h = self.card_hash(rows)
        self.db._exec("UPDATE ts_cards SET status = 'superseded' WHERE status = 'offered'")   # one open card
        cur = self.db._exec("INSERT INTO ts_cards (day, entries_hash, status, edited, created_at) VALUES "
                            "(?,?,?,?,?)", (day, h, "offered", int(edited), self.db._now()))
        cid = int(cur.lastrowid)
        for r in rows:
            self.set(r["id"], card_id=cid)
        return cid, h

    def card_by_hash(self, h: str) -> dict | None:
        return self.db._one("SELECT * FROM ts_cards WHERE entries_hash = ? ORDER BY id DESC LIMIT 1", (h,))

    def open_card(self, day: str | None = None) -> dict | None:
        sql = "SELECT * FROM ts_cards WHERE status = 'offered'" + (" AND day = ?" if day else "") + " ORDER BY id DESC"
        return self.db._one(sql + " LIMIT 1", (day,) if day else ())

    def decide_card(self, card_id: int, status: str, *, autopilot: bool = False) -> None:
        self.db._exec("UPDATE ts_cards SET status = ?, autopilot = ?, decided_at = ? WHERE id = ?",
                      (status, int(autopilot), self.db._now(), card_id))

    def card_rows(self, card_id: int) -> list[dict]:
        return self.db._all("SELECT * FROM ts_entries WHERE card_id = ? ORDER BY id", (card_id,))

    def cards(self) -> list[dict]:
        return self.db._all("SELECT * FROM ts_cards ORDER BY id")

    def dump(self, rows: list[dict]) -> str:
        return json.dumps([{k: r.get(k) for k in ("day", "key", "hours", "name")} for r in rows], ensure_ascii=False)
