"""Shared helpers for the Watcher tests (synthetic data only)."""
from __future__ import annotations

import json

from twin_agent.db import StateDB
from twin_agent.testing.fakes import PRINCIPAL, PRINCIPAL_ID, BACKUP_ID, STAFF_A, canned_packet, make_event
from twin_agent.watcher import gate


def watcher_on(cfg: dict, **caps) -> dict:
    cfg["consent"]["d4_watcher_ref"] = "D4-test-signed"
    cfg["watcher"]["enabled"] = True
    for c in gate.CAPTURE:
        cfg["watcher"]["capture"][c] = caps.get(c, True)
    return cfg


def seed(db: StateDB, clock) -> str:
    """One request: staff asks, a draft, Principal taps + pastes shell output, Principal replies, the agent's echo."""
    q = make_event("WAVE/00001 cannot be validated, Staff A here", msg_id=1, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A")
    db.add_message(q, rid)
    db.save_packet(rid, canned_packet(rid), True, [])
    clock.advance(minutes=5)
    db.log_event("button", rid, actor=str(PRINCIPAL_ID), action="fx")
    db.log_event("instruction", rid, actor=str(PRINCIPAL_ID), text="$ odoo-bin shell output: 1 move fixed")
    db.log_event("button", rid, actor=str(BACKUP_ID), action="to")           # backup: never captured
    clock.advance(minutes=5)
    mine = make_event("Done ya Staff A, try now please", msg_id=2, sender=PRINCIPAL, ts=clock.now(), reply_to=1)
    db.add_message(mine, rid)
    echo = make_event("Agent text that went out as Principal", msg_id=3, sender=PRINCIPAL, ts=clock.now())
    db.add_message(echo, rid)
    db._exec("INSERT INTO sends (send_key, request_id, channel, kind, status, target_json, remote_id) "
             "VALUES (?,?,?,?,?,?,?)", (f"{rid}:x", rid, "telegram_group", "answer", "sent",
                                         json.dumps({"chat_id": str(q.chat_id)}), "3"))
    return rid
