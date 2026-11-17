"""PR #54 review + security findings: the automatic-message rule is per SENDER episode (a first-contact claim bound to
the ticket), links only join tickets of the SAME partner, each tick pass is isolated, a failed fallback is unknown (not
silently suppressing the normal path), and the tick never waits on a busy sender / never inverts a sender's order."""
from __future__ import annotations

import threading
import time

from twin_agent.hub import fakes, templates
from twin_agent.hub import pipeline as pipeline_mod
from hub_helpers import approve, build_hub, deliver, load, msg, pending, run_to_resolved
from test_hub_followups import _acks, _gated_analysis, _st

PHONE = "201000000001"


def _burst(hub, n):
    for i in range(n):
        hub.accept(msg(f"burst{i}", f"message {i}?", ts=1762857600 + 5 + i))


# ---------------------------------------------------------------- P1: a burst gets ONE automatic message

def test_burst_while_the_first_message_is_slow_gives_exactly_one_automatic_message(monkeypatch):
    hub = build_hub()
    gate, started = _gated_analysis(monkeypatch)
    t = threading.Thread(target=lambda: deliver(hub, "webhook_text.json"))
    t.start()
    assert started.wait(5)
    _burst(hub, 2)                                                  # messages 2 and 3 queue up behind message 1
    hub.clock.advance(seconds=70)
    hub.tick()
    hub.clock.advance(seconds=60)
    hub.tick()
    assert len(_acks(hub)) == 1                                     # one fallback, not one per message
    gate.set()
    t.join(10)
    hub.retry_failed()
    assert len(_acks(hub)) == 1 and len(hub.tickets_for(PHONE)) == 1
    assert "7-ACK(fallback)" in _st(hub).history and len(_st(hub).replies) == 2


def test_message_one_held_after_the_fallback_then_message_two_opens_the_ticket_with_one_ack_total():
    class DownSTT(fakes.FakeSTT):
        def transcribe(self, a, **k):
            return {"status": "blocked", "why": "groq down"}

    hub = build_hub(stt=DownSTT())
    hub.accept(load("webhook_voice.json"))                          # message 1: a voice note that cannot be transcribed
    hub.accept(msg("two", "Invoice is broken", ts=1762857700))
    hub.clock.advance(seconds=70)
    hub.tick()
    assert [h["reason"] for h in hub.holds()] == ["stt_unavailable"]
    hub.clock.advance(seconds=70)
    hub.tick()
    assert len(_acks(hub)) == 1                                     # not a second fallback for message 2
    st = _st(hub)
    assert st is not None and "7-ACK(fallback)" in st.history       # message 2's ticket binds to the open claim
    assert not any("ticket #" in a["text"].lower() for a in _acks(hub))


def test_new_issue_with_two_messages_and_an_odoo_hiccup_gives_exactly_one_new_automatic_message():
    hub = build_hub()
    run_to_resolved(hub)
    base = len(_acks(hub))
    now = lambda: int(hub.clock().timestamp()) - 10                 # noqa: E731
    deliver(hub, msg("a1", "Thanks!", ts=now()))
    deliver(hub, msg("a2", "Also the sales report is empty", ts=now()))
    req = pending(hub, "reopen")
    real = hub.odoo.create_ticket
    hub.odoo.create_ticket = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("odoo down"))
    hub.on_owner_decision(req["nonce"], 1003, approved=False, new_issue=True)
    hub.clock.advance(seconds=61)
    hub.tick()
    hub.odoo.create_ticket = real
    hub.clock.advance(seconds=31)
    hub.tick()
    hub.clock.advance(minutes=10)
    hub.tick()
    assert len(hub.tickets_for(PHONE)) == 2 and len(_acks(hub)) == base + 1


def test_a_later_ticket_after_resolve_still_gets_its_own_ack():
    hub = build_hub()
    run_to_resolved(hub)
    deliver(hub, msg("k1", "Thanks!", ts=int(hub.clock().timestamp()) - 10))
    approve(hub, "reopen", approved=False)
    deliver(hub, msg("k2", "New problem: payroll is broken", ts=int(hub.clock().timestamp()) - 10))
    assert len(_acks(hub)) == 2 and "ticket #" in _acks(hub)[1]["text"].lower()


# ---------------------------------------------------------------- security M2: link only the same partner

def test_a_reassigned_number_does_not_link_to_another_clients_ticket():
    hub = build_hub()
    run_to_resolved(hub)
    hub.k_odoo.partners[PHONE] = 3002                               # the number now belongs to a different partner
    hub.clock.advance(days=8)
    deliver(hub, msg("p2", "New problem: payroll is broken", ts=int(hub.clock().timestamp()) - 10))
    new = _st(hub)
    assert new.partner_id == 3002 and new.previous_ticket_id == 0
    desc = [w["vals"]["description"] for w in hub.odoo.t.writes if w["method"] == "create"][1]
    assert "Follows up" not in desc
    assert "linked to" not in [t["brief"] for t in hub.telegram.topics.values()][-1]


# ---------------------------------------------------------------- P5: one failing pass does not stop the tick

def test_a_failing_fallback_pass_does_not_stop_retry_and_expiry():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 1})
    deliver(hub, "webhook_text.json")                               # a confirm that will expire
    hub.accept(msg("later", "hello?", ts=1762857700, wa_id="201000000002"))
    hub.clock.advance(hours=2)
    hub.k_odoo.partner_for = lambda wa: (_ for _ in ()).throw(ConnectionError("odoo down"))
    out = hub.tick()                                                # must not raise
    assert out["expired"] == 1
    assert any("tick pass" in t for t in hub.telegram.triage)


# ---------------------------------------------------------------- P7: a failed fallback is UNKNOWN, a human is told

def test_a_failed_fallback_send_is_unknown_never_silently_suppressing_and_never_two():
    class Broken(fakes.FakeWhatsApp):
        def send_text(self, *a, **k):
            if not hasattr(self, "tried"):
                self.tried = True
                raise ConnectionError("5xx before Meta accepted it")
            return super().send_text(*a, **k)

    hub = build_hub(whatsapp=Broken())
    hub.accept(load("webhook_text.json"))
    hub.clock.advance(seconds=61)
    hub.tick()                                                      # the fallback attempt raises
    assert _acks(hub) == []
    hub.retry_failed()                                              # the normal path now runs
    st = _st(hub)
    assert "7-ACK(unknown)" in st.history and _acks(hub) == []      # the unknown-send rule: never two
    assert any("outcome unknown" in p["text"].lower() for p in hub.telegram.posts)


# ---------------------------------------------------------------- claim 3: the tick never waits on a busy sender

def test_tick_does_not_block_behind_an_in_flight_sender(monkeypatch):
    hub = build_hub()
    gate, started = _gated_analysis(monkeypatch)
    t = threading.Thread(target=lambda: deliver(hub, "webhook_text.json"))
    t.start()
    assert started.wait(5)
    hub.accept(msg("second", "ok thanks", ts=1762857700))           # the same sender's NEXT message, queued
    done = threading.Event()
    tk = threading.Thread(target=lambda: (hub.tick(), done.set()))
    tk.start()
    try:
        assert done.wait(2), "tick waited on the sender's lock"
    finally:
        gate.set()
        t.join(10)
        tk.join(10)


def test_after_a_restart_the_oldest_unprocessed_message_opens_the_ticket():
    hub = build_hub()
    hub.accept(msg("m1", "Invoice is broken", ts=1762857600))
    hub.accept(msg("m2", "ok thanks", ts=1762857700))
    hub.store.update("messages", "m1", lambda d: {**d, "status": "processing"})   # m1 was in flight when the process died
    hub.tick()
    assert hub.tickets_for(PHONE) == []                             # m2 must NOT jump the queue
    hub.clock.advance(seconds=301)                                  # m1 is now stale
    hub.tick()
    st = _st(hub)
    assert st.source_message_id == "m1" and "Invoice" in st.inbound.text
    assert [r["message_id"] for r in st.replies] == ["m2"]


# ---------------------------------------------------------------- suggestions closed cheaply

def test_a_legacy_attempting_marker_is_never_re_sent():
    hub = build_hub()
    real = hub._ack
    hub._ack = lambda *a, **k: None
    deliver(hub, "webhook_text.json")
    hub._ack = real
    st = _st(hub)
    st.history.append("7-ACK(attempting)")                          # written by the pre-#54 code, then a crash
    hub._save(st)
    hub._ack(hub.ticket(st.tid), [])
    assert _acks(hub) == [] and "7-ACK(unknown)" in hub.ticket(st.tid).history


def test_the_fallback_name_is_capped_and_cleaned():
    text = templates.fallback_ack("en", name="Oscar " + "x" * 200 + " https://evil.example/a\nline2")
    assert "evil.example" not in text and "\n" not in text and len(text) < 220
