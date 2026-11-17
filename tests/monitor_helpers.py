"""Shared helpers for the Twin Monitor tests (synthetic data only)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from twin_agent.db import StateDB
from twin_agent.testing.fakes import PRINCIPAL, PRINCIPAL_ID, BACKUP_ID, STAFF_A, canned_packet, make_event

SALT = b"test-salt"


def monitor_on(cfg: dict) -> dict:
    cfg["consent"]["d4_watcher_ref"] = "D4-test-signed"
    cfg.setdefault("monitor", {})["enabled"] = True
    return cfg


def seed(db: StateDB, clock) -> str:
    """One request: staff asks, a draft, Principal edits it, a would-send, a blocked send, Principal replies."""
    q = make_event("WAVE/00001 cannot be validated, Staff A here", msg_id=1, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane="L3_t1_answer")
    db.add_message(q, rid)
    db.log_event("request.new", rid, actor="router", event_id=q.event_id, channel="telegram_group")
    db.save_packet(rid, canned_packet(rid), True, [{"check": "judge_correctness", "passed": True}])
    clock.advance(minutes=3)
    db.log_event("send", rid, actor="sender", send_key=f"{rid}:holding-1", out_kind="holding",
                 trigger="holding_floor", status="would_send", reason="observe", blocked_by=["send_disabled"],
                 text_sha256="a" * 64)
    db._exec("INSERT INTO sends (send_key, request_id, channel, kind, trigger, status, created_at) VALUES "
             "(?,?,?,?,?,?,?)", (f"{rid}:holding-1", rid, "telegram_group", "holding", "holding_floor",
                                 "would_send", db._now()))
    db.log_event("button", rid, actor=str(PRINCIPAL_ID), action="ed")
    db.log_event("callback", rid, actor=str(PRINCIPAL_ID), action="ed")
    db.log_event("instruction", rid, actor=str(PRINCIPAL_ID), text="say it shorter please, Staff A")
    db.log_event("send", rid, actor="sender", send_key=f"{rid}:draft-1", out_kind="answer", trigger="principal_edit",
                 status="blocked", reason="guards", blocked_by=["personal_data", "dial"])
    clock.advance(minutes=5)
    mine = make_event("Totally different words from me now", msg_id=2, sender=PRINCIPAL, ts=clock.now(), reply_to=1)
    db.add_message(mine, rid)
    db.update_request(rid, first_reply_at=clock.now(), first_reply_by="principal")
    db.log_event("principal_replied", rid, actor="principal", event_id=mine.event_id)
    db.log_event("stalled", rid, actor="timers")
    db.log_event("callback", rid, actor=str(BACKUP_ID), action="to")
    return rid


def add_write(db: StateDB, rid: str | None, **w) -> None:
    db.log_event(w.pop("kind", "write"), rid, actor="agent", **w)


def files_hash(db_path: Path) -> dict:
    out = {}
    for p in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    return out


def bundle_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def records(path: Path) -> list[dict]:
    return [json.loads(x) for x in bundle_text(path).splitlines()[1:] if x.strip()]
