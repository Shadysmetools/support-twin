"""The DEMO hub and FICTIONAL seed data ("Demo Bakery", "Test Customer", 555-01xx numbers). All-fakes: the only hub
that can be built without production adapters (same as ``hub.tick --dry-run``). Seeding drives the REAL pipeline
through ``Hub.handle_webhook`` so the tickets, the acks and the approval requests are genuine hub output.

A demo DB carries a marker doc; ``serve`` refuses any SQLite file without it, so this can never be pointed at a real
hub state file by accident."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from ...testing.fake_odoo import FakeOdoo
from .. import fakes, judge, learning, proof_runner, routing
from ..model import Owner
from ..odoo_gateway import GuardedOdoo
from ..pipeline import Hub
from ..store import SqliteStore
from .auth import hash_password
from .config import StaffUser

DEMO_APPROVER_ID = 90001
DEMO_PARTNERS = {"15550100001": 3001, "15550100002": 3001, "15550100003": 3001}
DEMO_CONTEXT = {3001: {"source": "odoo", "name": "Demo Bakery", "modules": [], "owner_uids": []}}
MARKER_NS, MARKER_KEY = "admin_meta", "demo"
VOICE_TEXT = "Hi, my order has not arrived and I need an update on the delivery."


class DemoWhatsApp(fakes.FakeWhatsApp):
    """FakeWhatsApp that also records a simulated delivery report for each human-approved send (stands in for the
    Meta status callback, which the hub does not ingest yet). The ack is never marked: only approved replies are."""

    def __init__(self, store):
        super().__init__()
        self.store = store

    def send_text(self, to, text, *, last_inbound_at, now=None, automatic=False):
        rec = super().send_text(to, text, last_inbound_at=last_inbound_at, now=now, automatic=automatic)
        if not automatic:
            self.store.put("delivery", str(to), {"status": "delivered", "ts": rec["ts"].isoformat(), "simulated": True})
        return rec


def build_demo_hub(db_path: str) -> Hub:
    store = SqliteStore(db_path)
    agent = Owner(uid=9001, name="Demo Agent", tg_id=DEMO_APPROVER_ID, role="support")
    team = [routing.Candidate(agent, skills=("Inventory", "Sales", "Accounting", "Helpdesk"),
                              handles_tracks=("inquiry", "functional", "development"))]
    return Hub(odoo=GuardedOdoo(FakeOdoo(), writes_on=True), whatsapp=DemoWhatsApp(store),
               stt=fakes.FakeSTT({"DEMO_AUDIO_0001": VOICE_TEXT}), telegram=fakes.FakeTelegram(),
               knowledge_odoo=fakes.FakeOdooKnowledge(partners=dict(DEMO_PARTNERS), context=dict(DEMO_CONTEXT)),
               knowledge_wiki=fakes.FakeWikiKnowledge(context={}),
               assigner=routing.RuleAssigner(team), proof=proof_runner.FakeProofRunner(),
               metrics_connector=fakes.FakeMetrics(), judge=judge.FakeJudge(), learning=learning.FakeLearningLog(),
               cfg={"run_mode": "dry_run", "phone_number_id": "DEMO_PHONE_ID", "approver_tg_ids": [DEMO_APPROVER_ID],
                    "client_channel": "whatsapp"},             # the demo shows the WhatsApp approval flow
               store=store)


def is_demo_db(hub) -> bool:
    return hub.store.get(MARKER_NS, MARKER_KEY) is not None


def is_new_path(path: str) -> bool:
    """seed only ever CREATES a file: ``:memory:`` or a path that does not exist yet."""
    return path == ":memory:" or not Path(path).exists()


def has_demo_marker(path: str) -> bool:
    """Read-only check of an existing SQLite file for the demo marker, BEFORE any hub/store is opened on it (opening
    a store would switch the file to WAL, create tables, chmod it and take its lock)."""
    if path == ":memory:" or not Path(path).is_file():
        return False
    try:
        c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = c.execute("SELECT 1 FROM docs WHERE ns=? AND key=?", (MARKER_NS, MARKER_KEY)).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return False
    return row is not None


def is_empty(hub) -> bool:
    return not any(hub.store.scan(ns) for ns in ("tickets", "messages", "approvals", "holds"))


def _payload(wa_id: str, mid: str, ts: int, *, text: str | None = None, audio_id: str | None = None) -> dict:
    msg = {"from": wa_id, "id": mid, "timestamp": str(ts)}
    msg.update({"type": "text", "text": {"body": text}} if text else
               {"type": "audio", "audio": {"id": audio_id, "mime_type": "audio/ogg; codecs=opus", "voice": True}})
    return {"object": "whatsapp_business_account", "entry": [{"id": "DEMO_WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550100000", "phone_number_id": "DEMO_PHONE_ID"},
        "contacts": [{"profile": {"name": "Test Customer"}, "wa_id": wa_id}], "messages": [msg]}}]}]}


def status_payload(wamid: str, status: str = "delivered") -> dict:
    """A Meta status callback (fictional ids) for a message the demo hub sent."""
    return {"object": "whatsapp_business_account", "entry": [{"id": "DEMO_WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550100000", "phone_number_id": "DEMO_PHONE_ID"},
        "statuses": [{"id": wamid, "status": status, "timestamp": str(int(time.time()))}]}}]}]}


def seed(hub) -> dict:
    """Three tickets: A approved + sent + delivered, B (text) and C (voice note) awaiting approval. Refuses a DB that
    already holds anything."""
    if not is_empty(hub):
        raise RuntimeError("refusing to seed: this DB already has hub data (use a fresh demo file)")
    hub.store.put(MARKER_NS, MARKER_KEY, {"demo": True})
    ts = int(time.time()) - 120
    a = hub.handle_webhook(_payload("15550100001", "wamid.DEMO0001", ts, text="Hi, my invoice export is failing."))
    req = hub.store.get("tickets", a["ticket_id"])["pending_nonce"]
    hub.on_owner_decision(req, DEMO_APPROVER_ID)                      # ticket A: approved through the real path
    reply = next((s for s in hub.store.scan("sends") if s["kind"] == "reply"), None)
    if reply:                                                         # and Meta's "delivered" callback, through the real path
        hub.handle_webhook(status_payload(reply["wamid"]))
    b = hub.handle_webhook(_payload("15550100002", "wamid.DEMO0002", ts + 30, text="Hi, my order hasn't arrived."))
    c = hub.handle_webhook(_payload("15550100003", "wamid.DEMO0003", ts + 60, audio_id="DEMO_AUDIO_0001"))
    return {"tickets": [a["ticket_id"], b["ticket_id"], c["ticket_id"]]}


def write_demo_users(path: str | os.PathLike) -> str | None:
    """Create a demo users file (mode 0600, outside git) with one random password. Returns the password ONCE, or None
    when the file already exists (it is never overwritten)."""
    p = Path(path)
    if p.exists():
        return None
    password = secrets.token_urlsafe(12)
    user = StaffUser("demo-approver", "Demo Approver", DEMO_APPROVER_ID, hash_password(password))
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"users": [{"username": user.username, "display_name": user.display_name,
                              "approver_id": user.approver_id, "password_hash": user.pw_hash}]}, f, indent=2)
    return password
