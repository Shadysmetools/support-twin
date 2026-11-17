"""Local ledger for helpdesk writes (tables in state.db) and the Twin Monitor write events.

Agent state stays OFF the ticket record (02h section 0 #12: any field write on a Live ticket delays auto-close).
- `hd_links`:  dedupe key (REQ id, email Message-ID, source link) -> ticket id. A key is CLAIMED before the
  Odoo call; a call that fails mid-way leaves `maybe_created`, and the next attempt searches Odoo first.
- `hd_drafts`: the exact draft each card showed, with its hash (the tap is bound to that hash).
- `hd_gates`:  stage moves into a client-emailing stage, waiting for Supervisor's tap.

`log_write` is the only way helpdesk and timesheet code record a write: CONTRACT "Changes" (MONITOR) shape, with
system "odoo", env "sme_helpdesk" (Acme' own helpdesk, not a client system), login "personal" (Principal's key).
"""
from __future__ import annotations

import hashlib
import json

from ..models import utc_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS hd_links (key TEXT PRIMARY KEY, request_id TEXT, ticket_id INTEGER, status TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS hd_drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, draft_json TEXT,
  draft_hash TEXT, status TEXT, ticket_id INTEGER, created_at TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS hd_gates (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, ticket_id INTEGER,
  stage_id INTEGER, status TEXT, approvals_json TEXT, created_at TEXT, decided_at TEXT, decided_by TEXT);
"""
DONE = ("created", "linked")
ENV, TARGET = "sme_helpdesk", "helpdesk.example.com"


def sha(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, db):
        self.db = db
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                db._exec(stmt)

    def _now(self) -> str:
        return self.db._now()

    def install_id(self) -> str:
        """A short id for this state.db. REQ numbers restart at REQ-0001 on a fresh install, so the ref written into
        a ticket ("ref REQ-0001.a1b2c3") must never match a ticket made by an older install."""
        iid = self.db.get_cursor("helpdesk_install_id")
        if not iid:
            import secrets
            iid = secrets.token_hex(3)
            self.db.set_cursor("helpdesk_install_id", iid)
        return iid

    def ref(self, request_id: str) -> str:
        return f"{request_id}.{self.install_id()}"

    # ---------------------------------------------------------------- links (dedupe)

    def find(self, keys) -> dict | None:
        for k in keys or []:
            row = self.db._one("SELECT * FROM hd_links WHERE key = ?", (k,))
            if row:
                return row
        return None

    def claim(self, key: str, request_id: str) -> bool:
        """True if this call owns the key now. False if someone already claimed or finished it."""
        cur = self.db._exec("INSERT OR IGNORE INTO hd_links (key, request_id, status, created_at, updated_at) "
                            "VALUES (?,?,?,?,?)", (key, request_id, "claimed", self._now(), self._now()))
        return cur.rowcount == 1

    def set_link(self, keys, request_id: str, ticket_id: int | None, status: str) -> None:
        for k in keys or []:
            self.db._exec("INSERT INTO hd_links (key, request_id, ticket_id, status, created_at, updated_at) "
                          "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET ticket_id = excluded.ticket_id, "
                          "status = excluded.status, updated_at = excluded.updated_at",
                          (k, request_id, ticket_id, status, self._now(), self._now()))

    def release(self, key: str) -> None:
        self.db._exec("DELETE FROM hd_links WHERE key = ? AND status IN ('claimed', 'prepared')", (key,))

    def ticket_of(self, request_id: str) -> int | None:
        row = self.db._one("SELECT ticket_id FROM hd_links WHERE request_id = ? AND status IN ('created','linked') "
                           "AND ticket_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1", (request_id,))
        return int(row["ticket_id"]) if row else None

    def all_links(self) -> list[dict]:
        return self.db._all("SELECT * FROM hd_links ORDER BY created_at")

    # ---------------------------------------------------------------- drafts and gates

    def save_draft(self, request_id: str, draft: dict) -> tuple[int, str]:
        h = sha(draft)
        cur = self.db._exec("INSERT INTO hd_drafts (request_id, draft_json, draft_hash, status, created_at) "
                            "VALUES (?,?,?,?,?)", (request_id, json.dumps(draft, ensure_ascii=False), h, "offered",
                                                   self._now()))
        return int(cur.lastrowid), h

    def draft_by_hash(self, request_id: str, h: str) -> dict | None:
        return self.db._one("SELECT * FROM hd_drafts WHERE request_id = ? AND draft_hash = ? ORDER BY id DESC "
                            "LIMIT 1", (request_id, h))

    def latest_draft(self, request_id: str) -> dict | None:
        return self.db._one("SELECT * FROM hd_drafts WHERE request_id = ? ORDER BY id DESC LIMIT 1", (request_id,))

    def decide_draft(self, draft_id: int, status: str, ticket_id: int | None = None) -> None:
        self.db._exec("UPDATE hd_drafts SET status = ?, ticket_id = ?, decided_at = ? WHERE id = ?",
                      (status, ticket_id, self._now(), draft_id))

    def add_gate(self, request_id: str, ticket_id: int, stage_id: int, approvals: list) -> int:
        cur = self.db._exec("INSERT INTO hd_gates (request_id, ticket_id, stage_id, status, approvals_json, "
                            "created_at) VALUES (?,?,?,?,?,?)", (request_id, ticket_id, stage_id, "waiting_supervisor",
                                                                 json.dumps(approvals), self._now()))
        return int(cur.lastrowid)

    def open_gate(self, request_id: str) -> dict | None:
        return self.db._one("SELECT * FROM hd_gates WHERE request_id = ? AND status = 'waiting_supervisor' ORDER BY id "
                            "DESC LIMIT 1", (request_id,))

    def close_gate(self, gate_id: int, status: str, by: str) -> None:
        self.db._exec("UPDATE hd_gates SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                      (status, self._now(), by, gate_id))


def approval(kind: str, by, at, **extra) -> dict:
    """One approval-evidence entry for a write event (CONTRACT MONITOR (c))."""
    return {"kind": kind, "by": str(by), "at": utc_iso(at) if hasattr(at, "tzinfo") else str(at), **extra}


def instruction(by, at, words: str) -> dict:
    return approval("principal_instruction", by, at, words=words, words_sha256=sha(words))


def _host(url: str | None) -> str:
    u = str(url or "").strip()
    if "//" in u:
        u = u.split("//", 1)[1]
    return u.split("/", 1)[0].split("@")[-1].strip()


def log_write(db, request_id: str | None, *, op: str, model: str, status: str, approvals: list, purpose: str,
              record_id: int | None = None, fields: list[str] | None = None, stage_to: int | None = None,
              emails_client: bool = False, reason: str = "", auto_close: bool = False,
              uid: int | None = None, url: str | None = None) -> None:
    """A Monitor write event. status: done | prepared (observe) | blocked. Blocked uses kind write_blocked.

    `uid` is the authenticated Odoo user id and `url` the actual odoo host this call went to; the Monitor
    classifies login/target from these facts (an admin uid or an off-host url raises an alert).
    """
    kind = "write_blocked" if status == "blocked" else "write"
    target = _host(url) or TARGET
    detail = {"system": "odoo", "op": op, "model": model, "env": ENV, "target": target, "status": status,
              "login": "personal", "approvals": approvals, "purpose": purpose}
    for k, v in (("record_id", record_id), ("fields", sorted(fields) if fields else None), ("stage_to", stage_to),
                 ("reason", reason[:160] if reason else None),
                 ("uid", int(uid) if isinstance(uid, int) and not isinstance(uid, bool) else None),
                 ("url", str(url) if url else None)):
        if v is not None:
            detail[k] = v
    if emails_client:
        detail["emails_client"] = True
    if auto_close:
        detail["auto_close_clock"] = True
    db.log_event(kind, request_id, actor="digital_twin", **detail)
