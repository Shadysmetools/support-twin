"""state.db: schema and data access (CONTRACT section 8).

One SQLite file in WAL mode. Times are stored as UTC ISO text (models.utc_iso) and read back as Cairo
(models.parse_ts). Rows come back as plain dicts. The listener is the only writer; the board reads it.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from .clock import Clock
from .models import (OVERLAY, TERMINAL, Card, Dial, Event, IllegalTransition, Lane, OutboundMessage,
                     SendStatus, SendTrigger, State, can_transition, utc_iso)
from .redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, state TEXT NOT NULL, prev_state TEXT,
  lane TEXT, tier TEXT, company TEXT, channel TEXT NOT NULL,
  chat_id TEXT, chat TEXT, topic_id INTEGER, topic TEXT, root_msg_id TEXT,
  requester_id TEXT, requester_name TEXT, title TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, burst_closed_at TEXT,
  first_reply_at TEXT, first_reply_by TEXT,
  taken_over INTEGER DEFAULT 0, no_holding INTEGER DEFAULT 0, fully_blocked INTEGER DEFAULT 0,
  session_id TEXT, worker_runs INTEGER DEFAULT 0, check_calls INTEGER DEFAULT 0,
  injection_hit INTEGER DEFAULT 0, found_by_sweep INTEGER DEFAULT 0,
  ticket_id TEXT, ticket_url TEXT, promised_update_date TEXT,
  reopened_from TEXT, closed_at TEXT, close_reason TEXT,
  agent_minutes REAL DEFAULT 0, principal_minutes REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
  event_id TEXT PRIMARY KEY, request_id TEXT, channel TEXT, chat_id TEXT, topic_id INTEGER, msg_id TEXT,
  sender_id TEXT, sender_name TEXT, is_principal INTEGER, is_bot INTEGER, ts TEXT, text TEXT, media_json TEXT,
  reply_to TEXT, edited_at TEXT
);
CREATE TABLE IF NOT EXISTS timers (
  id INTEGER PRIMARY KEY, request_id TEXT, kind TEXT NOT NULL, due_at TEXT NOT NULL,
  fired_at TEXT, cancelled_at TEXT, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY, request_id TEXT, kind TEXT, to_user TEXT, text TEXT, buttons_json TEXT,
  silent INTEGER, bot_msg_id INTEGER, created_at TEXT, sent_at TEXT, expires_at TEXT, coalesced_into INTEGER
);
CREATE TABLE IF NOT EXISTS approvals (
  nonce TEXT PRIMARY KEY, request_id TEXT, card_id INTEGER, action TEXT, draft_hash TEXT,
  allowed_user_id INTEGER, issued_at TEXT, expires_at TEXT, used_at TEXT, used_by INTEGER
);
CREATE TABLE IF NOT EXISTS sends (
  send_key TEXT PRIMARY KEY, request_id TEXT, channel TEXT, kind TEXT, lane TEXT, trigger TEXT,
  target_json TEXT, text_hash TEXT, status TEXT, created_at TEXT, sent_at TEXT, remote_id TEXT,
  from_id_ok INTEGER, guard_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, request_id TEXT, actor TEXT, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS kpis (
  id INTEGER PRIMARY KEY, day TEXT, lane TEXT, name TEXT, value REAL, request_id TEXT, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS packets (
  id INTEGER PRIMARY KEY, request_id TEXT, run_no INTEGER, packet_json TEXT, verified INTEGER,
  verdicts_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS dials (lane TEXT PRIMARY KEY, dial TEXT, changed_at TEXT, changed_by TEXT, cooloff_until TEXT);
CREATE INDEX IF NOT EXISTS ix_timers_due ON timers(fired_at, cancelled_at, due_at);
CREATE INDEX IF NOT EXISTS ix_messages_req ON messages(request_id);
CREATE INDEX IF NOT EXISTS ix_sends_req ON sends(request_id);
CREATE INDEX IF NOT EXISTS ix_events_req ON events(request_id);
"""

REQUEST_COLS = frozenset("""state prev_state lane tier company channel chat_id chat topic_id topic root_msg_id
requester_id requester_name title created_at updated_at burst_closed_at first_reply_at first_reply_by taken_over
no_holding fully_blocked session_id worker_runs check_calls injection_hit found_by_sweep ticket_id ticket_url
promised_update_date reopened_from closed_at close_reason agent_minutes principal_minutes""".split())
SEND_COLS = frozenset("""request_id channel kind lane trigger target_json text_hash status created_at sent_at
remote_id from_id_ok guard_json error""".split())
LIVE_SEND_STATUSES = ("pending", "sent", "maybe_sent")   # a key in one of these states is burnt

KILL_CURSOR = "kill_switch"             # "1" = /stop is on (the STOP file is the other half)
TG_BLOCKED_CURSOR = "tg_send_blocked"   # "1" = PEER_FLOOD / spam notice seen: no Telegram sends at all


def _val(v: Any) -> Any:
    """Turn Python values into something SQLite stores (times as UTC ISO, enums as text, dicts as JSON)."""
    if isinstance(v, datetime):
        return utc_iso(v)
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, bool):
        return int(v)
    if is_dataclass(v) and not isinstance(v, type):
        return json.dumps(asdict(v), default=str, ensure_ascii=False)
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str, ensure_ascii=False)
    return v


def _id_text(v: Any) -> str | None:
    return None if v is None else str(v)


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class StateDB:
    def __init__(self, path: Path, clock: Clock, cfg: dict | None = None):
        """Open, WAL mode, create the schema if missing. cfg (optional) feeds close rules and dial defaults."""
        self.path, self.clock, self.cfg = Path(path), clock, cfg
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.on_state_change: Callable[[str, str, str], None] | None = None   # listener publishes state.changed

    # ---------------------------------------------------------------- basics

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _now(self) -> str:
        return utc_iso(self.clock.now())

    def _all(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def _one(self, sql: str, args: Iterable = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(args))

    # ---------------------------------------------------------------- requests

    def next_request_id(self) -> str:
        row = self._one("SELECT MAX(CAST(SUBSTR(id, 5) AS INTEGER)) AS n FROM requests")
        return f"REQ-{(row['n'] or 0) + 1:04d}"

    def create_request(self, ev: Event, **fields) -> str:
        with self._lock:
            rid = self.next_request_id()
            now = self._now()
            row = {
                "state": State.NEW.value, "channel": ev.channel, "chat_id": _id_text(ev.chat_id), "chat": ev.chat,
                "topic_id": ev.topic_id, "topic": ev.topic, "root_msg_id": _id_text(ev.msg_id),
                "requester_id": str(ev.sender.tg_id or ev.sender.email or ""), "requester_name": ev.sender.name,
                "title": (ev.text or "").strip().splitlines()[0][:80] if (ev.text or "").strip() else "(photo)",
                "company": ev.company_hint, "created_at": utc_iso(ev.ts) or now, "updated_at": now,
                "found_by_sweep": ev.found_by_sweep,
            }
            row.update({k: v for k, v in fields.items() if k in REQUEST_COLS})
            cols = ["id"] + list(row)
            self._exec(f"INSERT INTO requests ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       [rid] + [_val(v) for v in row.values()])
        self.log_event("request.new", rid, actor="router", event_id=ev.event_id, channel=str(ev.channel))
        return rid

    def get_request(self, request_id: str) -> dict | None:
        return self._one("SELECT * FROM requests WHERE id = ?", (request_id,))

    def open_requests(self) -> list[dict]:
        terminal = [s.value for s in TERMINAL]
        return self._all(f"SELECT * FROM requests WHERE state NOT IN ({','.join('?' * len(terminal))}) ORDER BY id",
                         terminal)

    def requests_in(self, states: Iterable[State]) -> list[dict]:
        states = [State(s).value for s in states]
        return self._all(f"SELECT * FROM requests WHERE state IN ({','.join('?' * len(states))}) ORDER BY id",
                         states)

    def update_request(self, request_id: str, **fields) -> None:
        bad = set(fields) - REQUEST_COLS
        if bad:
            raise KeyError(f"unknown request columns: {sorted(bad)}")
        fields.setdefault("updated_at", self.clock.now())
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE requests SET {sets} WHERE id = ?", [_val(v) for v in fields.values()] + [request_id])

    def transition(self, request_id: str, dst: State, *, actor: str, reason: str = "") -> None:
        """Move a request to dst, or raise IllegalTransition. Writes an events row (kind 'state')."""
        dst = State(dst)
        with self._lock:
            req = self.get_request(request_id)
            if req is None:
                raise IllegalTransition(f"{request_id}: no such request")
            src = State(req["state"])
            prev = State(req["prev_state"]) if req["prev_state"] else None
            if not can_transition(src, dst, prev):
                raise IllegalTransition(f"{request_id}: {src} -> {dst} not allowed")
            if dst == State.CLOSED_REREAD_PROVEN and not (self._cfg_get("close_rules", "close_silent_quickfix")
                                                          or self._cfg_get("close_rules", "reread_proven_accepted")):
                raise IllegalTransition(f"{request_id}: CLOSED_REREAD_PROVEN is off (close_rules)")
            if src in TERMINAL and dst == State.REOPENED and not self.within_reopen_window(req):
                raise IllegalTransition(f"{request_id}: closed too long ago to reopen")
            fields: dict[str, Any] = {"state": dst}
            if dst in OVERLAY and src not in OVERLAY:
                fields["prev_state"] = src            # remember where to return to
            elif dst not in OVERLAY:
                fields["prev_state"] = None
            if dst in TERMINAL:
                fields.update(closed_at=self.clock.now(), close_reason=reason or None)
            self.update_request(request_id, **fields)
        self.log_event("state", request_id, actor=actor, src=str(src), dst=str(dst), reason=reason)
        if self.on_state_change:
            self.on_state_change(request_id, str(src), str(dst))

    def within_reopen_window(self, req: dict) -> bool:
        from .clock import elapsed
        from .models import parse_ts
        closed = parse_ts(req.get("closed_at"))
        days = int(self._cfg_get("timers", "reopen_window", "days") or 14)
        return closed is None or elapsed(closed, self.clock.now()) <= timedelta(days=days)

    def _cfg_get(self, *keys: str) -> Any:
        node: Any = self.cfg or {}
        for k in keys:
            node = node.get(k) if isinstance(node, dict) else None
        return node

    # ---------------------------------------------------------------- messages

    def add_message(self, ev: Event, request_id: str | None) -> bool:
        """Store a message. False if the event_id is already stored (an edit updates the text instead)."""
        with self._lock:
            old = self._one("SELECT event_id FROM messages WHERE event_id = ?", (ev.event_id,))
            if old:
                if ev.edited:
                    self._exec("UPDATE messages SET text = ?, media_json = ?, edited_at = ? WHERE event_id = ?",
                               (ev.text, _val(list(ev.media)), self._now(), ev.event_id))
                return False
            self._exec(
                "INSERT INTO messages (event_id, request_id, channel, chat_id, topic_id, msg_id, sender_id, "
                "sender_name, is_principal, is_bot, ts, text, media_json, reply_to, edited_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.event_id, request_id, _val(ev.channel), _id_text(ev.chat_id), ev.topic_id, _id_text(ev.msg_id),
                 str(ev.sender.tg_id or ev.sender.email or ""), ev.sender.name, int(ev.sender.is_principal),
                 int(ev.sender.is_bot), utc_iso(ev.ts), ev.text, _val(list(ev.media)), _id_text(ev.reply_to),
                 self._now() if ev.edited else None))
            return True

    def get_message(self, event_id: str) -> dict | None:
        return self._one("SELECT * FROM messages WHERE event_id = ?", (event_id,))

    def set_message_request(self, event_id: str, request_id: str) -> None:
        self._exec("UPDATE messages SET request_id = ? WHERE event_id = ?", (request_id, event_id))

    def thread(self, request_id: str) -> list[dict]:
        return self._all("SELECT * FROM messages WHERE request_id = ? ORDER BY ts, event_id", (request_id,))

    # ---------------------------------------------------------------- timers

    def add_timer(self, request_id: str | None, kind: str, due_at: datetime, payload: dict | None = None) -> int:
        cur = self._exec("INSERT INTO timers (request_id, kind, due_at, payload_json) VALUES (?,?,?,?)",
                         (request_id, _val(kind), utc_iso(due_at), json.dumps(payload or {}, default=str)))
        return int(cur.lastrowid)

    def due_timers(self, now: datetime) -> list[dict]:
        return self._all("SELECT * FROM timers WHERE fired_at IS NULL AND cancelled_at IS NULL AND due_at <= ? "
                         "ORDER BY due_at, id", (utc_iso(now),))

    def mark_timer(self, timer_id: int, *, fired: bool = False, cancelled: bool = False) -> None:
        if fired:
            self._exec("UPDATE timers SET fired_at = ? WHERE id = ?", (self._now(), timer_id))
        if cancelled:
            self._exec("UPDATE timers SET cancelled_at = ? WHERE id = ?", (self._now(), timer_id))

    def future_timers(self, request_id: str) -> list[dict]:
        """Unfired, uncancelled timers of a request (overdue ones included: they still have to fire)."""
        return self._all("SELECT * FROM timers WHERE request_id = ? AND fired_at IS NULL AND cancelled_at IS NULL "
                         "ORDER BY due_at, id", (request_id,))

    def pending_timers(self, *, global_only: bool = False) -> list[dict]:
        where = " AND request_id IS NULL" if global_only else ""
        return self._all("SELECT * FROM timers WHERE fired_at IS NULL AND cancelled_at IS NULL" + where +
                         " ORDER BY due_at, id")

    def timers_of(self, request_id: str, kind: str) -> list[dict]:
        return self._all("SELECT * FROM timers WHERE request_id = ? AND kind = ? ORDER BY id", (request_id, kind))

    def cancel_timers(self, request_id: str | None, kinds: Iterable[str] | None = None) -> int:
        """Cancel pending timers of a request (all kinds if kinds is None). Returns how many."""
        sql = "UPDATE timers SET cancelled_at = ? WHERE fired_at IS NULL AND cancelled_at IS NULL"
        args: list[Any] = [self._now()]
        if request_id is None:
            sql += " AND request_id IS NULL"
        else:
            sql += " AND request_id = ?"
            args.append(request_id)
        if kinds is not None:
            kinds = [_val(k) for k in kinds]
            if not kinds:
                return 0
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        return self._exec(sql, args).rowcount

    def set_timer_due(self, timer_id: int, due_at: datetime) -> None:
        self._exec("UPDATE timers SET due_at = ? WHERE id = ?", (utc_iso(due_at), timer_id))

    # ---------------------------------------------------------------- cards and approvals

    def add_card(self, card: Card) -> int:
        buttons = [[{"label": b.label, "action": b.action} for b in row] for row in card.buttons]
        cur = self._exec(
            "INSERT INTO cards (request_id, kind, to_user, text, buttons_json, silent, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (card.request_id, _val(card.kind), card.to, redact(card.text), json.dumps(buttons), int(card.silent),
             self._now(), utc_iso(card.expires_at)))
        card.card_id = int(cur.lastrowid)
        return card.card_id

    def mark_card(self, card_id: int, *, bot_msg_id: int | None = None, coalesced_into: int | None = None) -> None:
        if bot_msg_id is not None:
            self._exec("UPDATE cards SET bot_msg_id = ?, sent_at = ? WHERE id = ?", (bot_msg_id, self._now(), card_id))
        if coalesced_into is not None:
            self._exec("UPDATE cards SET coalesced_into = ? WHERE id = ?", (coalesced_into, card_id))

    def cards_sent_since(self, since: datetime) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM cards WHERE sent_at >= ?", (utc_iso(since),))
        return int(row["n"])

    def issue_approval(self, request_id: str, card_id: int, action: str, allowed_user_id: int, draft_hash: str,
                       expires_at: datetime) -> str:
        nonce = secrets.token_urlsafe(8)
        self._exec("INSERT INTO approvals (nonce, request_id, card_id, action, draft_hash, allowed_user_id, "
                   "issued_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                   (nonce, request_id, card_id, action, draft_hash, int(allowed_user_id), self._now(),
                    utc_iso(expires_at)))
        return nonce

    def use_approval(self, nonce: str, user_id: int, action: str, request_id: str) -> dict | None:
        """Atomic one-time use. None if the nonce is unknown, used, expired, or for another user/action/request."""
        with self._lock:
            now = self._now()
            cur = self._exec("UPDATE approvals SET used_at = ?, used_by = ? WHERE nonce = ? AND used_at IS NULL "
                             "AND expires_at > ? AND allowed_user_id = ? AND action = ? AND request_id = ?",
                             (now, int(user_id), nonce, now, int(user_id), action, request_id))
            if cur.rowcount != 1:
                return None
            return self._one("SELECT * FROM approvals WHERE nonce = ?", (nonce,))

    def get_approval(self, nonce: str) -> dict | None:
        """Read-only look at an approvals row (the sender checks a nonce the bot already consumed)."""
        return self._one("SELECT * FROM approvals WHERE nonce = ?", (nonce,))

    # ---------------------------------------------------------------- sends

    def claim_send(self, msg: OutboundMessage, trigger: SendTrigger) -> bool:
        """Store the send key as 'pending' right before the network call. False if the key is already burnt."""
        with self._lock:
            old = self._one("SELECT status FROM sends WHERE send_key = ?", (msg.send_key,))
            if old and old["status"] in LIVE_SEND_STATUSES:
                return False
            target = {"chat_id": _id_text(msg.chat_id), "topic_id": msg.topic_id, "reply_to": _id_text(msg.reply_to),
                      "to": list(msg.to), "cc": list(msg.cc), "subject": msg.subject, "thread_id": msg.thread_id}
            self._exec(
                "INSERT OR REPLACE INTO sends (send_key, request_id, channel, kind, lane, trigger, target_json, "
                "text_hash, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (msg.send_key, msg.request_id, _val(msg.channel), _val(msg.kind), _val(msg.lane), _val(trigger),
                 json.dumps(target), text_hash(msg.text), "pending", self._now()))
            return True

    def finish_send(self, send_key: str, status: SendStatus, **fields) -> None:
        """Record the outcome. Upserts, so 'blocked' / 'would_send' rows can be written without claim_send.

        Extra fields: any sends column, or msg=OutboundMessage (fills target, hash, lane, kind).
        """
        msg: OutboundMessage | None = fields.pop("msg", None)
        row: dict[str, Any] = {}
        if msg is not None:
            row.update(request_id=msg.request_id, channel=msg.channel, kind=msg.kind, lane=msg.lane,
                       text_hash=text_hash(msg.text),
                       target_json={"chat_id": _id_text(msg.chat_id), "topic_id": msg.topic_id,
                                    "reply_to": _id_text(msg.reply_to), "to": list(msg.to), "cc": list(msg.cc),
                                    "subject": msg.subject, "thread_id": msg.thread_id})
        if "guard" in fields:
            fields["guard_json"] = fields.pop("guard")
        bad = set(fields) - SEND_COLS
        if bad:
            raise KeyError(f"unknown sends columns: {sorted(bad)}")
        row.update(fields)
        row["status"] = SendStatus(status).value
        if row["status"] == "sent":
            row.setdefault("sent_at", self.clock.now())
        if "error" in row and row["error"]:
            row["error"] = redact(str(row["error"]))[:2000]
        with self._lock:
            if self._one("SELECT 1 AS x FROM sends WHERE send_key = ?", (send_key,)):
                sets = ", ".join(f"{k} = ?" for k in row)
                self._exec(f"UPDATE sends SET {sets} WHERE send_key = ?", [_val(v) for v in row.values()] + [send_key])
            else:
                row.setdefault("request_id", send_key.split(":", 1)[0])
                row.setdefault("created_at", self.clock.now())
                cols = ["send_key"] + list(row)
                self._exec(f"INSERT INTO sends ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           [send_key] + [_val(v) for v in row.values()])

    def send_by_remote(self, chat_id, remote_id) -> dict | None:
        """Our own sent message (by the id Telegram gave it), so its echo is not taken as Principal typing."""
        return self._one("SELECT * FROM sends WHERE remote_id = ? AND json_extract(target_json, '$.chat_id') = ? "
                         "AND status IN ('sent', 'maybe_sent', 'pending')", (str(remote_id), str(chat_id)))

    def get_send(self, send_key: str) -> dict | None:
        return self._one("SELECT * FROM sends WHERE send_key = ?", (send_key,))

    def sends_since(self, since: datetime, *, chat_id=None, request_id=None, statuses=("sent",)) -> list[dict]:
        sql = "SELECT * FROM sends WHERE COALESCE(sent_at, created_at) >= ?"
        args: list[Any] = [utc_iso(since)]
        if statuses:
            statuses = [SendStatus(s).value if s != "pending" else s for s in statuses]
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if request_id is not None:
            sql += " AND request_id = ?"
            args.append(request_id)
        if chat_id is not None:
            sql += " AND json_extract(target_json, '$.chat_id') = ?"
            args.append(str(chat_id))
        return self._all(sql + " ORDER BY COALESCE(sent_at, created_at)", args)

    # ---------------------------------------------------------------- log, kpis, packets

    def log_event(self, kind: str, request_id: str | None = None, actor: str = "system", **detail) -> None:
        body = redact(json.dumps(detail, default=str, ensure_ascii=False)) if detail else "{}"
        self._exec("INSERT INTO events (ts, kind, request_id, actor, detail_json) VALUES (?,?,?,?,?)",
                   (self._now(), kind, request_id, actor, body))

    def events(self, request_id: str | None = None, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if request_id is not None:
            sql, args = sql + " AND request_id = ?", args + [request_id]
        if kind is not None:
            sql, args = sql + " AND kind = ?", args + [kind]
        return self._all(sql + " ORDER BY id", args)

    def add_kpi(self, name: str, value: float, *, lane: str | None = None, request_id: str | None = None,
                **detail) -> None:
        self._exec("INSERT INTO kpis (day, lane, name, value, request_id, detail_json) VALUES (?,?,?,?,?,?)",
                   (self.clock.now().date().isoformat(), _val(lane), name, float(value), request_id,
                    json.dumps(detail, default=str)))

    def kpi_sum(self, name: str, *, day: str | None = None, request_id: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(value), 0) AS s FROM kpis WHERE name = ?", [name]
        if day is not None:
            sql, args = sql + " AND day = ?", args + [day]
        if request_id is not None:
            sql, args = sql + " AND request_id = ?", args + [request_id]
        return float(self._one(sql, args)["s"])

    def save_packet(self, request_id: str, packet: dict, verified: bool, verdicts: list[dict]) -> int:
        with self._lock:
            n = self._one("SELECT COUNT(*) AS n FROM packets WHERE request_id = ?", (request_id,))["n"]
            self._exec("INSERT INTO packets (request_id, run_no, packet_json, verified, verdicts_json, created_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (request_id, n + 1, json.dumps(packet, ensure_ascii=False, default=str), int(verified),
                        json.dumps(verdicts, default=str), self._now()))
            return n + 1

    def update_packet_checks(self, request_id: str, run_no: int, verified: bool, verdicts: list[dict]) -> None:
        """Store the checks' result on the packet row the spawner saved (one row per worker run)."""
        self._exec("UPDATE packets SET verified = ?, verdicts_json = ? WHERE request_id = ? AND run_no = ?",
                   (int(verified), json.dumps(verdicts, default=str), request_id, int(run_no)))

    def latest_packet(self, request_id: str) -> dict | None:
        return self._one("SELECT * FROM packets WHERE request_id = ? ORDER BY run_no DESC LIMIT 1", (request_id,))

    # ---------------------------------------------------------------- cursors, flags, dials

    def get_cursor(self, source: str) -> str | None:
        row = self._one("SELECT value FROM cursors WHERE source = ?", (source,))
        return row["value"] if row else None

    def set_cursor(self, source: str, value: str) -> None:
        self._exec("INSERT INTO cursors (source, value, updated_at) VALUES (?,?,?) ON CONFLICT(source) DO UPDATE "
                   "SET value = excluded.value, updated_at = excluded.updated_at", (source, str(value), self._now()))

    def kill_on(self) -> bool:
        return self.get_cursor(KILL_CURSOR) == "1"

    def set_kill(self, on: bool, by: str) -> None:
        self.set_cursor(KILL_CURSOR, "1" if on else "0")
        self.log_event("kill", actor=by, on=on)

    def get_dial(self, lane: Lane) -> Dial:
        """The live dial: a dials row if Principal changed it, else the config value, else off."""
        row = self._one("SELECT dial FROM dials WHERE lane = ?", (str(Lane(lane)),))
        if row and row["dial"]:
            return Dial(row["dial"])
        if self.cfg is not None:
            from .config import lane_dial
            return lane_dial(self.cfg, lane)
        return Dial.off

    def set_dial(self, lane: Lane, dial: Dial, by: str) -> None:
        lane, dial = Lane(lane), Dial(dial)
        old = self.get_dial(lane)
        self._exec("INSERT INTO dials (lane, dial, changed_at, changed_by) VALUES (?,?,?,?) ON CONFLICT(lane) DO "
                   "UPDATE SET dial = excluded.dial, changed_at = excluded.changed_at, changed_by = excluded.changed_by",
                   (lane.value, dial.value, self._now(), by))
        self.log_event("dial", actor=by, lane=lane.value, old=old.value, new=dial.value)
