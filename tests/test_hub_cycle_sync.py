"""Fixes from the full-cycle production test (ticket 2714): the acked flag, one log line per event, WhatsApp delivery
statuses, who approved, the confirm-draft summary, and the lifecycle fields the One desktop reads from /api/v1."""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta

import pytest
from admin_helpers import PW, Client, approve_form, fast_hash, make_app
from hub_helpers import approve, build_hub, deliver, run_to_resolved
from test_hub_readonly_api import ADMIN_HOST, TOKEN, Api

from twin_agent.hub import events, slots, templates
from twin_agent.hub.admin import data, seed
from twin_agent.hub.admin.config import StaffUser
from twin_agent.hub.readonly_api import ApiGate

PHONE = "PHONE_NUMBER_ID"
LIFECYCLE = {"new", "needs_approval", "approved", "reply_sent", "reply_delivered", "reply_read", "awaiting_customer",
             "closed", "held"}


def status_payload(wamid, status, *, pid=PHONE, code=None, ts=1762858000):
    st = {"id": wamid, "status": status, "timestamp": str(ts), "recipient_id": "201000000001"}
    if code:
        st["errors"] = [{"code": code, "title": "Message undeliverable"}]
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "1", "phone_number_id": pid},
        "statuses": [st]}}]}]}


def sends(hub, kind=None):
    rows = hub.store.scan(events.SENDS_NS)
    return [r for r in rows if kind in (None, r["kind"])]


# ------------------------------------------------------------------ 1. acked

def test_acked_is_true_once_the_ack_is_accepted():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    (t,) = hub.all_tickets()
    assert t.acked is True and "7-ACK" in t.history
    assert hub.store.get("tickets", t.tid)["acked"] is True            # persisted, not just in memory
    assert [s["kind"] for s in sends(hub)] == ["ack"] and sends(hub)[0]["tid"] == t.tid


def test_acked_is_false_when_the_ack_was_skipped():
    hub = build_hub(whatsapp=_ClosedWindow())
    deliver(hub, "webhook_text.json")
    (t,) = hub.all_tickets()
    assert t.acked is False and "7-ACK(skipped)" in t.history and sends(hub) == []


class _ClosedWindow:
    sent: list = []

    def send_text(self, *a, **k):
        from twin_agent.channels import whatsapp_cloud
        raise whatsapp_cloud.WindowClosed("closed")


# ------------------------------------------------------------------ 2. one log line per event

def event_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "twin_agent.hub.events"]


def test_a_full_cycle_logs_one_line_per_event_and_no_personal_data(caplog):
    caplog.set_level(logging.INFO, logger="twin_agent.hub.events")
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    ack = sends(hub, "ack")[0]["wamid"]
    deliver(hub, "webhook_text.json")                                           # the same message again: a duplicate
    hub.handle_webhook(status_payload(ack, "delivered"))
    hub.handle_webhook(status_payload("wamid.NOT.OURS", "read", pid="999"))     # another number
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")
    approve(hub, "advance")
    approve(hub, "fix_ready")
    approve(hub, "resolve")
    lines = event_lines(caplog)
    names = {m.group(1) for line in lines if (m := re.match(r"event=(\w+)", line))}
    assert {"inbound_received", "inbound_dropped", "ticket_created", "ack_sent", "approval_requested", "approval_approved",
            "reply_sent", "delivery_status", "ticket_closed"} <= names, names
    assert all(line.startswith("event=") and "\n" not in line for line in lines)
    blob = "\n".join(lines)
    assert not re.search(r"\d{7,}", blob), blob                                 # no phone number, however it is written
    forbidden = ["201000000001", "invoice", "Mia", "export", "Hello", "wamid.", "@", "Acme"]
    for payload in ("webhook_text.json", "webhook_confirm.json"):
        for e in hub_fixture_texts(payload):
            words = re.findall(r"w+", e.lower())
            forbidden += [" ".join(p) for p in zip(words, words[1:])] + [e]          # any two words in a row of a client message
    assert not [w for w in forbidden if w.lower() in blob.lower()], blob
    assert "reason=duplicate" in blob and "by=" not in blob.split("approval_requested")[0]


def hub_fixture_texts(name):
    from hub_helpers import load
    p = load(name)
    out = []
    for e in p["entry"]:
        for c in e["changes"]:
            out += [(m.get("text") or {}).get("body", "") for m in c["value"].get("messages", [])]
            out += [(x.get("profile") or {}).get("name", "") for x in c["value"].get("contacts", [])]
    return [o for o in out if o]


def test_the_real_production_logging_setup_puts_the_event_lines_on_stderr(tmp_path, capfd):
    """No caplog and no forced levels: the way ``production serve`` really configures logging (``log_setup=True``)."""
    from dataclasses import replace
    import json
    from test_hub_production import _post, make_cfg, payload, transport
    from twin_agent.hub import production
    cfg = replace(make_cfg(), users_file=str(tmp_path / "none" / "users.json"))
    logging.getLogger("twin_agent.hub").setLevel(logging.NOTSET)
    rt, sec = production.open_runtime(cfg, dry_run=True, log_setup=True, transport=transport(cfg))
    rt.start()
    base = f"http://127.0.0.1:{rt.ports()['webhook']}"
    try:
        assert _post(base, payload(mid="wamid.REAL1"), sec.app_secret) == 200
        rt.webhook.drain()
    finally:
        rt.close()
    err = capfd.readouterr().err
    lines = [x for x in err.splitlines() if "event=" in x]
    names = [re.search(r"event=(\w+)", x).group(1) for x in lines]
    assert "inbound_received" in names and "ticket_created" in names and "ack_sent" in names, err
    assert not re.search(r"\d{7,}", " ".join(x.split(": ", 1)[1] for x in lines))
    assert "Mia" not in err and "sales quotation" not in err


def test_the_event_helper_masks_anything_that_is_not_a_plain_token():
    class Rec(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, r):
            self.lines.append(r.getMessage())

    h, lg = Rec(), logging.getLogger("twin_agent.hub.events")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        events.event("x", tid=5, who="Mia Principal", phone="201000000001", note="call 201000000001", ok="a1-b2")
    finally:
        lg.removeHandler(h)
    assert h.lines == ["event=x tid=5 who=? phone=? note=? ok=a1-b2"]
    assert events.wamid_tail("wamid.HBgL.Mj-AxQjQ5") == "AxQjQ5"


def test_a_status_for_an_unknown_message_or_another_number_stores_nothing(caplog):
    caplog.set_level(logging.INFO, logger="twin_agent.hub.events")
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    before = hub.store.scan(events.SENDS_NS)
    hub.handle_webhook(status_payload("wamid.UNKNOWN", "delivered"))
    hub.handle_webhook(status_payload(before[0]["wamid"], "read", pid="OTHER_NUMBER"))
    assert hub.store.scan(events.SENDS_NS) == before
    assert not [x for x in event_lines(caplog) if "delivery_status" in x]


# ------------------------------------------------------------------ 3. delivery statuses

def test_statuses_move_forward_never_back_and_are_linked_to_ticket_and_kind(caplog):
    caplog.set_level(logging.INFO, logger="twin_agent.hub.events")
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    (t,) = hub.all_tickets()
    ack = sends(hub, "ack")[0]["wamid"]
    hub.handle_webhook(status_payload(ack, "read", ts=1762858300))
    hub.handle_webhook(status_payload(ack, "delivered", ts=1762858200))         # arrives late: must not go back
    hub.handle_webhook(status_payload(ack, "sent", ts=1762858100))
    (row,) = sends(hub, "ack")
    assert row["status"] == "read" and row["tid"] == t.tid and row["msg_kind"] == "normal"
    assert row["at"]["read"].startswith("2025-11-11T")
    approve(hub, "confirm")
    (reply,) = sends(hub, "reply")
    assert reply["tid"] == t.tid and reply["msg_kind"] == "confirm" and reply["status"] == "sent"
    hub.handle_webhook(status_payload(reply["wamid"], "failed", code=131026))
    (reply,) = sends(hub, "reply")
    assert reply["status"] == "failed" and reply["error_code"] == 131026
    line = [x for x in event_lines(caplog) if "status=failed" in x][0]
    assert f"tid={t.tid}" in line and "kind=reply" in line and "error_code=131026" in line


def test_statuses_are_stored_on_the_200_path_and_a_repeat_logs_once(caplog):
    """The webhook stores the payload before answering 200 (``accept``); the worker then runs ``handle_webhook``."""
    caplog.set_level(logging.INFO, logger="twin_agent.hub.events")
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    ack = sends(hub, "ack")[0]["wamid"]
    p = status_payload(ack, "delivered")
    hub.accept(p)
    hub.handle_webhook(p)
    assert sends(hub, "ack")[0]["status"] == "delivered"
    assert len([x for x in event_lines(caplog) if "delivery_status" in x]) == 1


def test_a_status_callback_still_creates_no_ticket_and_is_logged_as_dropped(caplog):
    caplog.set_level(logging.INFO, logger="twin_agent.hub.events")
    hub = build_hub()
    res = hub.handle_webhook(status_payload("wamid.X", "delivered"))
    assert "ignored" in res and hub.all_tickets() == []
    assert any("inbound_dropped" in x and "reason=status_callback" in x for x in event_lines(caplog))


# ------------------------------------------------------------------ API + admin fixtures

def build_world(tmp_path, users=None):
    from twin_agent.hub.admin.app import AdminApp
    from twin_agent.hub.admin.auth import RateLimiter
    from twin_agent.hub.admin.config import AdminConfig
    from twin_agent.hub.admin.graph import FakeGraphClient
    hub = seed.build_demo_hub(":memory:")
    info = seed.seed(hub)
    users = users or {"demo-approver": StaffUser("demo-approver", "Demo Approver", seed.DEMO_APPROVER_ID, fast_hash(PW))}
    app = AdminApp(hub, AdminConfig(users=users, app_id="1", config_id="2", cookie_secure=True), FakeGraphClient(),
                   limiter=RateLimiter())
    tf = tmp_path / "t.txt"
    tf.write_text(TOKEN, encoding="utf-8")
    gate = ApiGate(app, hub, app.cfg, admin_host=ADMIN_HOST, token_file=tf, health=lambda: {"ok": True})
    return Api(gate), hub, app, info["tickets"]


def demo_status(hub, wamid, status, code=None):
    hub.handle_webhook(status_payload(wamid, status, pid="DEMO_PHONE_ID", code=code))


def item(api, tid):
    return api.get(f"/api/v1/tickets/{tid}").json["ticket"]


# ------------------------------------------------------------------ 6. the desktop contract

def test_every_ticket_has_the_lifecycle_contract_fields(tmp_path):
    api, hub, app, (a, b, c) = build_world(tmp_path)
    rows = {t["id"]: t for t in api.get("/api/v1/tickets").json["tickets"]}
    for tid in (a, b, c):
        for t in (rows[tid], item(api, tid)):
            assert {"lifecycle", "lifecycle_at", "acked", "approved_by", "last_delivery"} <= set(t)
            assert t["lifecycle"] in LIFECYCLE and isinstance(t["acked"], bool)
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+\+00:00", t["lifecycle_at"]), t["lifecycle_at"]
            assert t["stage"] and "waiting_on" in t and "has_pending_approval" in t and "status" in t   # unchanged
    assert rows[b]["lifecycle"] == rows[c]["lifecycle"] == "needs_approval" and rows[b]["approved_by"] is None
    assert rows[b]["acked"] is True and rows[b]["last_delivery"] == {"kind": "ack", "status": "sent",
                                                                     "at": rows[b]["last_delivery"]["at"]}
    assert rows[a]["lifecycle"] == "reply_delivered" and rows[a]["approved_by"] == "Demo Approver"
    assert rows[a]["last_delivery"]["kind"] == "reply" and rows[a]["last_delivery"]["status"] == "delivered"


def test_lifecycle_follows_the_reply_from_sent_to_delivered_to_read(tmp_path):
    api, hub, app, (_a, b, _c) = build_world(tmp_path)
    hub.on_owner_decision(hub.ticket(b).pending_nonce, seed.DEMO_APPROVER_ID)          # a fresh reply: status "sent"
    a = b
    reply = [s for s in sends(hub, "reply") if s["tid"] == a][0]["wamid"]
    seen = [item(api, a)["lifecycle"]]
    for st in ("delivered", "read"):
        demo_status(hub, reply, st)
        t = item(api, a)
        seen.append(t["lifecycle"])
        assert t["last_delivery"]["status"] == st and t["last_delivery"]["kind"] == "reply"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+\+00:00", t["last_delivery"]["at"])
    assert seen == ["reply_sent", "reply_delivered", "reply_read"]
    assert item(api, a)["lifecycle_at"] == item(api, a)["last_delivery"]["at"]


def test_a_failed_delivery_is_reported_as_failed(tmp_path):
    api, hub, app, (a, *_) = build_world(tmp_path)
    reply = [s for s in sends(hub, "reply") if s["tid"] == a][0]["wamid"]
    demo_status(hub, reply, "failed", code=131047)
    t = item(api, a)
    assert t["last_delivery"]["status"] == "failed" and t["lifecycle"] == "held"      # a person must act: never "reply_sent"


def test_a_ticket_closed_in_the_last_24h_stays_in_the_list_as_closed(tmp_path):
    api, hub, app, (a, b, c) = build_world(tmp_path)
    for tid, age in ((a, timedelta(hours=2)), (b, timedelta(hours=30))):
        st = hub.ticket(tid)
        st.stage, st.resolved_at = "resolved", (hub.clock() - age).isoformat()
        hub._save(st)
    rows = {t["id"]: t for t in api.get("/api/v1/tickets").json["tickets"]}
    assert a in rows and rows[a]["lifecycle"] == "closed" and rows[a]["stage"] == "resolved"
    assert rows[a]["lifecycle_at"].startswith(str(hub.clock().year))
    assert b not in rows and c in rows                                      # 30h ago: gone from the open list
    allrows = {t["id"]: t for t in api.get("/api/v1/tickets?status=all").json["tickets"]}
    assert allrows[b]["lifecycle"] == "closed"
    assert item(api, a)["lifecycle"] == "closed"


def test_an_internal_owner_tap_is_not_needs_approval(tmp_path):
    """Same rule as ``waiting_on``: only a client-facing draft is an approval. Mark-confirmed and fix-ready are owner steps."""
    import time
    api, hub, app, (a, *_) = build_world(tmp_path)
    hub.handle_webhook(seed._payload("15550100001", "wamid.DEMO9", int(time.time()), text="Yes, that is correct."))
    t = item(api, a)
    assert t["stage"] == "awaiting_owner_advance" and t["waiting_on"] == "owner" and t["has_pending_approval"]
    assert t["lifecycle"] == "approved"                                 # the confirmation was approved earlier
    hub.on_owner_decision(hub.ticket(a).pending_nonce, seed.DEMO_APPROVER_ID)           # the owner marks it confirmed
    t = item(api, a)
    assert t["stage"] == "in_progress" and t["waiting_on"] == "owner" and t["lifecycle"] == "approved"


def test_the_ack_branch_after_a_crash_between_send_and_save_still_marks_acked():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    (t,) = hub.all_tickets()
    t.acked, t.history = False, [h for h in t.history if not h.startswith("7-ACK")]
    hub._save(t)
    hub._ack(hub.ticket(t.tid), [])                                     # the claim says "sent" for this ticket
    assert hub.ticket(t.tid).acked is True and len(sends(hub, "ack")) == 1


def test_a_ticket_with_an_open_hold_is_held(tmp_path):
    api, hub, app, (a, b, c) = build_world(tmp_path)
    hub._hold("processing_failed", ticket_id=b, detail="x")
    assert item(api, b)["lifecycle"] == "held" and item(api, c)["lifecycle"] == "needs_approval"


def test_an_old_ticket_with_the_ack_in_its_history_reports_acked(tmp_path):
    api, hub, app, (a, b, c) = build_world(tmp_path)
    st = hub.ticket(b)
    st.acked = False                                        # a ticket stored before the flag existed (like ticket 2714)
    hub._save(st)
    assert "7-ACK" in st.history and item(api, b)["acked"] is True


def test_the_api_stays_read_only_and_exposes_no_message_id(tmp_path):
    api, hub, app, (a, *_) = build_world(tmp_path)
    before = repr([hub.store.scan(ns) for ns in ("tickets", "approvals", events.SENDS_NS, "odoo_audit")])
    for p in ("/api/v1/tickets", f"/api/v1/tickets/{a}", "/api/v1/approvals/pending"):
        r = api.get(p)
        assert "wamid" not in r.raw.lower() and "1555010000" not in r.raw
    assert repr([hub.store.scan(ns) for ns in ("tickets", "approvals", events.SENDS_NS, "odoo_audit")]) == before


# ------------------------------------------------------------------ 3b. admin page

def test_the_admin_ticket_page_shows_delivery_statuses(tmp_path):
    api, hub, app, (a, *_) = build_world(tmp_path)
    reply = [s for s in sends(hub, "reply") if s["tid"] == a][0]["wamid"]
    demo_status(hub, reply, "delivered")
    demo_status(hub, reply, "read")
    c = Client(app)
    assert c.login().status == 303
    body = c.get(f"/tickets/{a}").body
    assert "reply read" in body and "ack sent" in body
    assert "Delivery status: delivered (reply)" in body and "Delivery status: read (reply)" in body
    assert "reply read" in c.get("/tickets").body


# ------------------------------------------------------------------ 4. approver attribution

def two_users():
    return {"one": StaffUser("one", "One Desktop", seed.DEMO_APPROVER_ID, fast_hash(PW)),
            "sysadmin": StaffUser("sysadmin", "Sysadmin", seed.DEMO_APPROVER_ID, fast_hash(PW))}      # the SAME approver_id, like prod


def test_the_approval_names_the_logged_in_user_not_the_first_user_with_that_id(tmp_path):
    api, hub, app, (a, b, c) = build_world(tmp_path, two_users())
    cl = Client(app)
    assert cl.login("one").status == 303
    assert cl.post(f"/tickets/{b}/approve", approve_form(cl, hub, b)).status in (302, 303)
    doc = [d for d in hub.store.scan("approvals") if d["ticket_id"] == b and d.get("approved")][0]
    assert doc["used_by_user"] == "one" and doc["used_by_name"] == "One Desktop"
    t = item(api, b)
    assert t["approved_by"] == "One Desktop"
    labels = [e["label"] for e in t["timeline"]]
    assert "Approved by One Desktop" in labels and not any("Sysadmin" in x for x in labels)
    page = cl.get(f"/tickets/{b}").body
    assert "Approved by One Desktop" in page and "Approved by Sysadmin" not in page


def test_an_approval_made_before_the_name_was_stamped_is_read_from_the_audit_row(tmp_path):
    api, hub, app, (a, b, c) = build_world(tmp_path, two_users())
    doc = [d for d in hub.store.scan("approvals") if d["ticket_id"] == a and d.get("approved")][0]
    assert "used_by_name" not in doc                                           # ticket A was approved by the seed (Telegram id)
    from twin_agent.hub.store import audit_append
    from datetime import datetime
    when = datetime.fromisoformat(doc["used_at"]) + timedelta(seconds=1)
    audit_append(hub.store, {"role": "approver", "model": "approval", "method": "approve", "ticket_id": a,
                             "hub_user": "one", "access_email": "", "outcome": "ok"}, when)
    assert item(api, a)["approved_by"] == "One Desktop"


def test_an_id_shared_by_several_users_never_names_one_of_them(tmp_path):
    api, hub, app, (a, *_) = build_world(tmp_path, two_users())
    assert item(api, a)["approved_by"] == "Staff approver"                      # no stamp, no audit row: do not guess "Sysadmin"


# ------------------------------------------------------------------ login by work email

def email_app(**over):
    app, hub, _ = make_app(**over)
    app.access_users = {"Demo.Person@example.com": "demo-approver"}
    return app


def test_the_login_page_hints_that_the_work_email_works():
    body = Client(email_app()).get("/login").body
    assert "Your username (e.g. sysadmin) or your work email" in body


def test_the_work_email_logs_in_as_the_mapped_user_case_insensitively():
    for typed in ("demo.person@example.com", " DEMO.PERSON@EXAMPLE.COM "):
        c = Client(email_app())
        assert c.login(typed).status == 303
        assert c.get("/tickets").status == 200


def test_an_unknown_email_fails_exactly_like_a_wrong_password():
    app = email_app()
    bad_pw = Client(app).login("demo-approver", "nope")
    unknown = Client(app).login("nobody@example.com", PW)
    near = Client(app).login("demo.person@example.com.evil.example.net", PW)         # exact match only
    assert bad_pw.status == unknown.status == near.status == 403
    msg = lambda r: re.search(r'class="flash bad">([^<]*)<', r.body).group(1)       # noqa: E731
    assert msg(bad_pw) == msg(unknown) == msg(near) == "Wrong username or password."


def test_a_real_and_a_fake_username_get_the_identical_answer_behind_access():
    """Behind Cloudflare Access the identity check must not tell a real username from a made-up one (no enumeration)."""
    app, _hub, _ = make_app()
    app.access_users = {"a@example.com": "demo-approver"}
    env = {"aa.access_email": "a@example.com"}
    real_other = Client(app, env=env).login("outsider", PW)          # a real user that is not this identity's user
    fake = Client(app, env=env).login("ghost", PW)
    assert real_other.status == fake.status == 403
    assert real_other.body.replace("outsider", "") == fake.body.replace("ghost", "") or         re.findall(r'class="flash bad">([^<]*)<', real_other.body) == re.findall(r'class="flash bad">([^<]*)<', fake.body)
    assert Client(app, env=env).login("demo-approver", PW).status == 303      # the mapped user still gets in


def test_the_login_rate_limit_still_applies_to_the_email():
    from twin_agent.hub.admin.auth import RateLimiter
    app = email_app(limiter=RateLimiter(max_fails=3))
    for _ in range(3):
        Client(app, ip="10.0.0.9").login("demo.person@example.com", "bad")
    assert Client(app, ip="10.0.0.9").login("demo.person@example.com", PW).status == 429
    assert Client(app, ip="10.0.0.9").login("demo-approver", PW).status == 429      # same user, same lock


# ------------------------------------------------------------------ 5. confirm-draft summary

ISSUE = "TEST: Hi, my invoice export to PDF shows the wrong total for discounted customers"


def slot(draft):
    return re.search(r"seeing (.+?) on Accounting. Is that correct", draft).group(1)


def test_the_live_message_becomes_a_short_paraphrase_not_a_paste():
    d = templates.confirm("en", name="Mia", issue=ISSUE, module="Accounting", outcome="", n=3)
    s = slot(d)
    assert s == "an issue with your invoice export to PDF"
    assert "TEST" not in d and "Hi," not in d and "my invoice" not in d and len(s) <= 140


@pytest.mark.parametrize("text,want", [
    ("Re: [URGENT] We are unable to close the month", "an issue where you are unable to close the month"),
    ("Hello, I can't post a vendor bill. Thanks", "an issue where you can't post a vendor bill"),
    ("FW: Subject: our POS crashes when I print", "an issue where your POS crashes when you print"),
    ("How do I print the invoice?", ""),                                         # a question: no statement to restate
    ("Please fix the export", ""),
    ("TEST:", ""),
    ("", ""),
])
def test_issue_phrase_cases(text, want):
    assert slots.issue_phrase(text) == want


def test_a_question_or_empty_message_falls_back_to_a_plain_phrase():
    d = templates.confirm("en", name="Mia", issue="How do I print the invoice?", module="Accounting", outcome="", n=3)
    assert slot(d) == "a problem"
    d = templates.confirm("en", name="Mia", issue="", module="Accounting", outcome="", n=3)
    assert slot(d) == "a problem"


def test_a_very_long_message_is_cut_on_a_whole_word():
    s = slots.issue_phrase("my " + "export " * 60 + "fails", 140)
    assert len(s) <= 140 and s.startswith("an issue where your export") and not s.endswith(("…", "export…"))


def test_the_hub_draft_for_the_live_message_has_no_label_or_greeting():
    hub = build_hub()
    from hub_helpers import msg
    p = msg("wamid.LIVE1", ISSUE, ts=1762858300, wa_id="201000000001")
    hub.handle_webhook(p)
    (t,) = hub.all_tickets()
    draft = hub._confirm_draft(t)
    assert "TEST" not in draft and "Hi," not in draft and "you're seeing an issue with your invoice export" in draft
