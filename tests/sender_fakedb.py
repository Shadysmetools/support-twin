"""Tiny in-memory stand-in for the StateDB methods sender.py uses (CONTRACT section 8 signatures).

The sender tests use it so they do not depend on db.py being finished; test_sender also runs one
scenario against the real StateDB when CORE has shipped it.
"""
from __future__ import annotations

from datetime import datetime

from twin_agent.models import Dial, SendStatus, parse_ts, utc_iso


class FakeStateDB:
    def __init__(self, clock, dials: dict | None = None):
        self.clock = clock
        self.sends: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.cursors: dict[str, str] = {}
        self.dials: dict[str, Dial] = dict(dials or {})
        self.events: list[dict] = []
        self.kpis: list[dict] = []

    def get_request(self, request_id):
        return self.requests.get(request_id)

    def update_request(self, request_id, **fields):
        self.requests.setdefault(request_id, {}).update(fields)

    def issue_approval(self, request_id, card_id, action, allowed_user_id, draft_hash, expires_at):
        nonce = f"n{len(self.approvals) + 1:010d}"
        self.approvals[nonce] = {"nonce": nonce, "request_id": request_id, "card_id": card_id, "action": action,
                                 "draft_hash": draft_hash, "allowed_user_id": allowed_user_id,
                                 "expires_at": utc_iso(expires_at), "used_at": None, "used_by": None}
        return nonce

    def use_approval(self, nonce, user_id, action, request_id):
        row = self.approvals.get(nonce)
        if (not row or row["used_at"] or row["action"] != action or row["request_id"] != request_id
                or row["allowed_user_id"] != user_id or parse_ts(row["expires_at"]) < self.clock.now()):
            return None
        row["used_at"], row["used_by"] = utc_iso(self.clock.now()), user_id
        return dict(row)

    def claim_send(self, msg, trigger):
        old = self.sends.get(msg.send_key)
        if old and old["status"] in ("pending", "sent", "maybe_sent"):
            return False
        self.sends[msg.send_key] = {"send_key": msg.send_key, "request_id": msg.request_id,
                                    "channel": str(msg.channel), "chat_id": str(msg.chat_id), "kind": str(msg.kind),
                                    "trigger": str(trigger), "status": "pending",
                                    "created_at": utc_iso(self.clock.now()), "remote_id": None}
        return True

    def finish_send(self, send_key, status, **fields):
        self.sends[send_key].update(status=str(SendStatus(status)), **fields)

    def sends_since(self, since: datetime, *, chat_id=None, request_id=None, statuses=("sent",)):
        return [dict(r) for r in self.sends.values()
                if r["status"] in statuses and r["created_at"] >= utc_iso(since)
                and (chat_id is None or r["chat_id"] == str(chat_id))
                and (request_id is None or r["request_id"] == request_id)]

    def log_event(self, kind, request_id=None, actor="system", **detail):
        self.events.append({"kind": kind, "request_id": request_id, "actor": actor, **detail})

    def add_kpi(self, name, value, *, lane=None, request_id=None, **detail):
        self.kpis.append({"name": name, "value": value, "lane": lane, "request_id": request_id, **detail})

    def get_cursor(self, source):
        return self.cursors.get(source)

    def set_cursor(self, source, value):
        self.cursors[source] = value

    def get_dial(self, lane):
        return self.dials.get(str(lane), Dial.observe)

    def set_dial(self, lane, dial, by):
        self.dials[str(lane)] = Dial(dial)

    def get_approval(self, nonce):
        row = self.approvals.get(nonce)
        return dict(row) if row else None
