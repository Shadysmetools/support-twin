"""Code re-review round 4 (Y1 blocker + M1/M3/M4/M5) and security LOW-A/LOW-B: new_issue goes through the normal
retryable path, stale status approvals die when a ticket resolves, nothing clears the unknown-send marker, a message
after close_ticket asks the owner (no automatic ack), a busy sender never occupies the pool, and a reply that arrived
while a confirm send was unknown is re-offered as an advance."""
from __future__ import annotations

import threading

from twin_agent.hub import fakes
from hub_helpers import approve, build_hub, deliver, load, msg, pending, run_to_resolved
from test_hub_exits import FlakyTG, PHONE, _client_sends, _creates, _now, _st, _to_in_progress
from test_hub_webhook_security import post, serve, signed

OWNER = 1003
STATUS = "Hello Oscar, we are working on it, the ETA is Friday. — Acme Support"


def _new_issue_question(hub):
    run_to_resolved(hub)
    hub.clock.advance(hours=1)
    deliver(hub, msg("ni1", "Thanks! Also the sales report is now empty", ts=_now(hub)))
    return pending(hub, "reopen")


# ---------------------------------------------------------------- Y1: new_issue survives an adapter blip

def test_new_issue_with_odoo_down_is_retried_by_tick_and_gives_one_ticket_and_one_ack():
    hub = build_hub()
    req = _new_issue_question(hub)
    real = hub.odoo.create_ticket
    hub.odoo.create_ticket = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("odoo down"))
    out = hub.on_owner_decision(req["nonce"], OWNER, approved=False, new_issue=True)
    assert out["ok"] is True and _creates(hub) == 1 and len(hub.tickets_for(PHONE)) == 1
    assert any("processing_failed" in t for t in hub.telegram.triage)          # a human is told; the issue is not lost
    assert hub.store.get("messages", "ni1")["status"] == "failed"
    hub.odoo.create_ticket = real
    hub.clock.advance(seconds=31)
    hub.tick()
    new = _st(hub)
    assert len(hub.tickets_for(PHONE)) == 2 and _creates(hub) == 2 and hub.whatsapp.automatic_count() == 2
    assert "sales report" in new.inbound.text and new.stage == "awaiting_owner" and pending(hub, "confirm") is not None
    hub.clock.advance(hours=1)
    hub.tick()
    assert len(hub.tickets_for(PHONE)) == 2 and hub.whatsapp.automatic_count() == 2


def test_new_issue_with_telegram_down_resumes_the_half_built_ticket_without_a_second_one():
    tg = FlakyTG()
    hub = build_hub(telegram=tg)
    req = _new_issue_question(hub)
    tg.down = True
    out = hub.on_owner_decision(req["nonce"], OWNER, approved=False, new_issue=True)
    assert out["ok"] is True and hub.store.get("messages", "ni1")["status"] == "failed"
    tg.down = False
    hub.clock.advance(seconds=31)
    hub.tick()
    new = _st(hub)
    assert len(hub.tickets_for(PHONE)) == 2 and _creates(hub) == 2 and hub.whatsapp.automatic_count() == 2
    assert new.stage == "awaiting_owner" and pending(hub, "confirm") is not None and new.topic_id


# ---------------------------------------------------------------- M3 / LOW-B: stale status approvals

def test_a_status_approved_after_the_ticket_resolved_is_not_sent():
    hub = build_hub()
    _to_in_progress(hub)
    hub.on_owner_text(_st(hub).tid, OWNER, STATUS, kind="status")
    stale = pending(hub, "status")
    approve(hub, "fix_ready")
    approve(hub, "resolve")
    n = len(_client_sends(hub))
    out = approve(hub, nonce=stale["nonce"], as_tg=OWNER)
    assert out["ok"] is False and len(_client_sends(hub)) == n


def test_a_status_approved_after_close_or_reopen_is_not_sent():
    hub = build_hub()
    _to_in_progress(hub)
    tid = _st(hub).tid
    hub.on_owner_text(tid, OWNER, STATUS, kind="status")
    stale = pending(hub, "status")
    hub.close_ticket(tid, OWNER)
    n = len(_client_sends(hub))
    assert approve(hub, nonce=stale["nonce"], as_tg=OWNER)["ok"] is False and len(_client_sends(hub)) == n


# ---------------------------------------------------------------- LOW-A: no path clears the unknown-send marker

def test_no_approval_path_can_send_while_a_send_outcome_is_unknown():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    tid = _st(hub).tid
    hub.on_owner_text(tid, OWNER, STATUS, kind="status")
    stale = pending(hub, "status")
    st = _st(hub)
    st.send_unknown = "confirm"                                      # an earlier send ended with an unknown result
    hub._save(st)
    n = len(_client_sends(hub))
    out = approve(hub, nonce=stale["nonce"], as_tg=OWNER)
    assert out["ok"] is False and out["reason"] == "send_outcome_unknown" and len(_client_sends(hub)) == n
    assert _st(hub).send_unknown == "confirm"                        # only confirm_unknown_send clears it


# ---------------------------------------------------------------- M5: "thanks" after close_ticket asks the owner

def test_thanks_after_close_ticket_asks_the_owner_and_opens_no_ticket_or_ack():
    hub = build_hub()
    deliver(hub, msg("i1", "How do I print a delivery slip in Inventory?", ts=1762857600))
    approve(hub, "confirm")
    deliver(hub, msg("i2", "yes", ts=_now(hub)))
    approve(hub, "advance")
    tid = _st(hub).tid
    hub.on_owner_text(tid, OWNER, "Hello Oscar, open the order and press Print. — Acme Support", kind="reply")
    approve(hub, "reply")
    hub.close_ticket(tid, OWNER)
    hub.clock.advance(hours=3)
    res = deliver(hub, msg("th1", "Thanks a lot!", ts=_now(hub)))
    assert res.get("reopen_asked") is True and len(hub.tickets_for(PHONE)) == 1 and hub.whatsapp.automatic_count() == 1
    assert pending(hub, "reopen") is not None
    approve(hub, "reopen", approved=False)                           # keep closed -> archived to triage
    assert any("kept closed" in t.lower() for t in hub.telegram.triage)
    deliver(hub, msg("th2", "New problem: payroll is broken", ts=_now(hub)))
    assert len(hub.tickets_for(PHONE)) == 2 and hub.whatsapp.automatic_count() == 2


# ---------------------------------------------------------------- M4: one in-flight job per sender

def test_a_busy_sender_never_occupies_more_than_one_worker():
    gate = threading.Event()
    hub = build_hub()
    real = hub.handle_webhook

    def slow_for_a(payload):
        if payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"] == "201000000001":
            gate.wait(10)
        return real(payload)

    hub.handle_webhook = slow_for_a
    with serve(hub, workers=2) as (srv, base):
        for i in range(4):                                           # 4 follow-ups from customer A while A is busy
            p = msg(f"a{i}", f"any update {i}?", ts=1762857600 + i)
            body, h = signed(p)
            assert post(base, body, h)[0] == 200
        other = load("webhook_text.json")
        other["entry"][0]["changes"][0]["value"]["messages"][0].update({"id": "b1", "from": "201000000002"})
        body, h = signed(other)
        assert post(base, body, h)[0] == 200
        for _ in range(100):
            if hub.tickets_for("201000000002"):
                break
            threading.Event().wait(0.05)
        assert hub.tickets_for("201000000002"), "customer B waited behind customer A's queued messages"
        gate.set()
        srv.drain()
    assert hub.store.get("messages", "a3")["status"] in ("done", "held")          # A's messages still ran, in order


# ---------------------------------------------------------------- M1: a reply during an unknown confirm send

def test_reply_during_an_unknown_confirm_send_is_re_offered_as_an_advance():
    class Flaky(fakes.FakeWhatsApp):
        n = 0

        def send_text(self, to, text, **kw):
            r = super().send_text(to, text, **kw)
            if not kw.get("automatic") and Flaky.n == 0:
                Flaky.n += 1
                raise TimeoutError("read timeout after the send was accepted")
            return r

    hub = build_hub(whatsapp=Flaky())
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    deliver(hub, msg("y1", "Yes exactly, that is the problem", ts=_now(hub)))
    assert pending(hub, "advance") is None                           # blocked while the outcome is unknown
    out = hub.confirm_unknown_send(_st(hub).tid, OWNER, sent=True)
    assert out["ok"] is True
    st = _st(hub)
    assert st.stage == "awaiting_owner_advance"
    assert pending(hub, "advance")["draft"] == "Yes exactly, that is the problem"
    assert len(_client_sends(hub)) == 1
