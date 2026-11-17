"""Issue #55: follow-ups from the PR #54 reviews. The invariant under test everywhere: AT MOST ONE automatic message
per ticket (and per sender episode). The Arabic fallback wording still needs a native review (TODO, not tested)."""
from __future__ import annotations

import copy
import threading

from twin_agent.hub import fakes
from hub_helpers import approve, build_hub, deliver, load, msg, run_to_resolved

PHONE = "201000000001"


def _autos(hub):
    return [s for s in hub.whatsapp.sent if s["automatic"]]


def _st(hub):
    return hub.latest_ticket(PHONE)


def _now_ts(hub, back=5):
    return int(hub.clock().timestamp()) - back


# ---------------------------------------------------------------- 1. missed ack (probe Q6)

def test_an_old_skipped_unbound_claim_does_not_swallow_a_later_tickets_ack():
    hub = build_hub()
    v = load("webhook_voice.json")
    hub.clock.advance(hours=26)                                      # the hub was down: this old voice note is stale
    gate, started = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        gate.wait(20)
        return {"status": "blocked", "why": "groq down"}

    hub.stt.transcribe = slow
    hub.accept(v)
    w = threading.Thread(target=lambda: hub.handle_webhook(v))
    w.start()
    assert started.wait(5)
    hub.clock.advance(seconds=70)
    hub._fallback_pass(hub.clock())                                  # window closed for the old message: claim 'skipped'
    gate.set()
    w.join(20)
    assert hub.store.get("fcclaim", PHONE)["state"] == "skipped"
    deliver(hub, msg("F2", "Invoice is broken", ts=_now_ts(hub)))    # a FRESH message, window open
    t = _st(hub)
    assert "7-ACK" in t.history and "7-ACK(skipped)" not in t.history
    assert len(_autos(hub)) == 1                                     # the fresh ticket got its own single ack


def test_an_errored_fallback_stays_maybe_sent_so_the_next_ticket_sends_no_second_ack():
    hub = build_hub()
    hub.store.put("fcclaim", PHONE, {"mid": "old", "kind": "fallback", "state": "error", "tid": 0,
                                     "at": hub.clock().isoformat()})
    deliver(hub, "webhook_text.json")
    t = _st(hub)
    assert "7-ACK(unknown)" in t.history and len(_autos(hub)) == 0
    assert any("outcome unknown" in p["text"] for p in hub.telegram.posts)      # the owner is told


class _DeliveredThenTimeout(fakes.FakeWhatsApp):
    """Meta accepted the first send, but the HTTP read timed out: the client HAS the message, the hub saw an error."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def send_text(self, *a, **k):
        r = super().send_text(*a, **k)
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("read timeout after delivery")
        return r


def test_delivered_then_timeout_fallback_then_a_fresh_ticket_sends_exactly_one_automatic_message():
    hub = build_hub(whatsapp=_DeliveredThenTimeout())
    gate, started = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        gate.wait(20)
        return {"status": "blocked", "why": "groq down"}

    hub.stt.transcribe = slow
    v = load("webhook_voice.json")
    hub.accept(v)
    w = threading.Thread(target=lambda: hub.handle_webhook(v))
    w.start()
    assert started.wait(5)
    hub.clock.advance(seconds=70)
    hub._fallback_pass(hub.clock())
    assert hub.store.get("fcclaim", PHONE)["state"] == "error"
    gate.set()
    w.join(20)
    deliver(hub, msg("F2", "Invoice is broken", ts=_now_ts(hub)))
    assert len(_autos(hub)) == 1                                     # the one that was delivered; none added
    assert "7-ACK(unknown)" in _st(hub).history


def test_an_errored_fallback_does_not_let_a_second_fallback_go_out_for_a_later_message():
    hub = build_hub()
    hub.store.put("fcclaim", PHONE, {"mid": "old", "kind": "fallback", "state": "error", "tid": 0,
                                     "at": hub.clock().isoformat()})
    hub.accept(load("webhook_text.json"))
    hub.clock.advance(seconds=70)
    assert hub._fallback_pass(hub.clock()) == 0 and len(_autos(hub)) == 0


# ---------------------------------------------------------------- 2. false "outcome unknown" after a crash before save

def test_a_sent_claim_of_this_ticket_records_the_ack_not_unknown():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    st = _st(hub)
    st.history.remove("7-ACK")                                       # crash after the send, before the save
    hub._save(st)
    n_before = len(hub.whatsapp.sent)
    hub._ack(hub.ticket(st.tid), [])
    after = hub.ticket(st.tid)
    assert "7-ACK" in after.history and "7-ACK(unknown)" not in after.history
    assert len(hub.whatsapp.sent) == n_before                        # never re-sent
    assert not any("outcome unknown" in p["text"] for p in hub.telegram.posts)


# ---------------------------------------------------------------- 3. the silent "skipped" branch

def test_another_open_episode_owning_the_claim_tells_the_owner():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    a = _st(hub)
    b = copy.deepcopy(a)
    b.tid, b.source_message_id = a.tid + 1, "other-mid"
    b.history = [h for h in b.history if not h.startswith("7-")]
    b.topic_id = b.thread_ref = ""
    hub._ack_refused(b, PHONE)
    assert "7-ACK(skipped)" in b.history
    assert any("another" in t and str(b.tid) in t for t in hub.telegram.triage)


# ---------------------------------------------------------------- 4. a claim whose ticket no longer exists

def test_a_claim_bound_to_a_missing_ticket_is_kept_inside_the_window_and_triaged():
    hub = build_hub()
    hub.store.put("fcclaim", PHONE, {"mid": "gone", "kind": "normal", "state": "sent", "tid": 424242,
                                     "at": hub.clock().isoformat()})
    deliver(hub, "webhook_text.json")
    assert len(_autos(hub)) == 0                                     # fail closed: the claim is NOT released
    assert any("424242" in t and "no longer exists" in t for t in hub.telegram.triage)
    assert hub._episode_open(PHONE)
    hub.clock.advance(hours=25)                                      # the 24h window is over: it cannot block forever
    assert not hub._episode_open(PHONE)


# ---------------------------------------------------------------- 5. failed / sent fallback is reported

class _FailingWA(fakes.FakeWhatsApp):
    def send_text(self, *a, **k):
        raise ConnectionError("wa down")


def test_a_failed_fallback_is_reported_to_triage():
    hub = build_hub(whatsapp=_FailingWA())
    hub.accept(load("webhook_text.json"))
    hub.clock.advance(seconds=70)
    hub._fallback_pass(hub.clock())
    assert any("fallback" in t.lower() and "failed" in t.lower() for t in hub.telegram.triage)
    assert not any("wamid" in t for t in hub.telegram.triage)       # a wamid encodes the sender's phone number


def test_a_sent_fallback_leaves_an_odoo_note_when_the_ticket_binds():
    hub = build_hub()
    p = load("webhook_text.json")
    hub.accept(p)
    hub.clock.advance(seconds=70)
    assert hub._fallback_pass(hub.clock()) == 1
    hub.retry_failed()
    t = _st(hub)
    assert "7-ACK(fallback)" in t.history and len(_autos(hub)) == 1
    notes = [w["kwargs"].get("body") or "" for w in hub.odoo.t.writes if w["method"] == "message_post"]
    assert any("Fallback auto-ack sent" in n for n in notes)


# ---------------------------------------------------------------- 6. _clean_name

def test_the_app_log_never_carries_a_raw_wamid(caplog):
    import logging
    hub = build_hub(whatsapp=_FailingWA())
    hub.accept(load("webhook_text.json"))
    hub.clock.advance(seconds=70)
    hub.stt.transcribe = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    with caplog.at_level(logging.DEBUG):
        hub._fallback_pass(hub.clock())
        hub.handle_webhook(load("webhook_voice.json"))
    assert "wamid" not in caplog.text and "ERROR" in caplog.text


# ---------------------------------------------------------------- 7. routing also checks the partner

def test_a_number_with_no_partner_never_attaches_to_the_old_clients_ticket():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    a = _st(hub)
    hub.k_odoo.partners.pop(PHONE)                                   # the number lost its partner in Odoo
    deliver(hub, msg("np", "I am the new owner of this number", ts=_now_ts(hub)))
    assert hub.ticket(a.tid).replies == [] and len(hub.tickets_for(PHONE)) == 1
    assert any(h["reason"] == "unknown_sender" for h in hub.holds())
    assert not any(r["kind"] == "advance" for r in hub.telegram.approvals)


def test_a_reassigned_number_does_not_attach_to_the_old_partners_open_ticket():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    a = _st(hub)
    hub.k_odoo.partners[PHONE] = 3002                                # the number now belongs to another partner
    deliver(hub, msg("n2", "Payroll is broken", ts=_now_ts(hub)))
    tickets = hub.tickets_for(PHONE)
    assert len(tickets) == 2 and tickets[-1].partner_id == 3002 and tickets[-1].previous_ticket_id == 0
    assert hub.ticket(a.tid).replies == []                           # nothing of partner B leaked onto A's ticket
    assert all(sum(1 for h in t.history if h in ("7-ACK", "7-ACK(fallback)")) <= 1 for t in tickets)


# ---------------------------------------------------------------- 8. tick cost

def test_the_fallback_pass_does_not_look_up_the_same_stuck_message_every_minute():
    hub = build_hub()
    calls = []
    real = hub.k_odoo.partner_for
    hub.k_odoo.partner_for = lambda wa: (calls.append(wa), real(wa))[1]
    p = msg("stuck", "hello", ts=_now_ts(hub), wa_id="201999999999")  # an unknown sender: never a fallback candidate
    hub.accept(p)
    for _ in range(6):
        hub.clock.advance(seconds=70)
        hub._fallback_pass(hub.clock())
    assert len(calls) <= 2


def test_the_early_out_never_delays_the_first_fallback():
    hub = build_hub()
    hub.accept(load("webhook_text.json"))
    hub.clock.advance(seconds=30)
    assert hub._fallback_pass(hub.clock()) == 0                      # too early: nothing, and nothing cached as 'no'
    hub.clock.advance(seconds=35)
    assert hub._fallback_pass(hub.clock()) == 1                      # due at 65s: sent on that very pass


# ---------------------------------------------------------------- 9. old links

def test_a_very_old_previous_ticket_is_not_linked():
    hub = build_hub()
    run_to_resolved(hub)
    first = _st(hub)
    hub.clock.advance(days=100)                                      # follow_up_max_days default 90
    deliver(hub, msg("o1", "New problem: payroll is broken", ts=_now_ts(hub)))
    new = _st(hub)
    assert new.tid != first.tid and new.previous_ticket_id == 0


def test_follow_up_max_days_is_configurable():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "follow_up_max_days": 200})
    run_to_resolved(hub)
    first = _st(hub)
    hub.clock.advance(days=100)
    deliver(hub, msg("o2", "New problem: payroll is broken", ts=_now_ts(hub)))
    assert _st(hub).previous_ticket_id == first.tid


# ---------------------------------------------------------------- round 3: _clean_name on ANY tld

# ---------------------------------------------------------------- round 3: an aged-out claim never re-acks an old message

def _aged_claim(hub, **fields):
    doc = {"mid": "first", "kind": "normal", "state": "sent", "tid": 424242, "at": hub.clock().isoformat()}
    doc.update(fields)
    hub.store.put("fcclaim", PHONE, doc)


def test_a_message_stuck_since_inside_the_window_never_gets_a_fallback_after_the_claim_ages_out():
    for fields in ({}, {"kind": "fallback", "tid": 0}):               # a missing-ticket claim, and an unbound one
        hub = build_hub()
        _aged_claim(hub, **fields)
        hub.clock.advance(hours=23)
        hub.accept(msg("stuck", "Invoice is broken", ts=_now_ts(hub, 0)))     # inside the window, stays queued
        hub.clock.advance(hours=1, minutes=7)                         # now past the 24h age-out
        assert hub._fallback_pass(hub.clock()) == 0 and len(_autos(hub)) == 0


def test_a_stuck_message_processed_after_the_age_out_gets_no_ack_either():
    hub = build_hub()
    _aged_claim(hub, kind="fallback", tid=0)
    hub.clock.advance(hours=23)
    hub.accept(msg("stuck", "Invoice is broken", ts=_now_ts(hub, 0)))
    hub.clock.advance(hours=2)
    hub.retry_failed()
    assert len(_autos(hub)) == 0 and _st(hub) is not None


def test_only_a_new_message_after_the_age_out_opens_a_new_first_contact_with_its_own_ack():
    hub = build_hub()
    _aged_claim(hub, kind="fallback", tid=0)
    hub.clock.advance(hours=25)
    deliver(hub, msg("fresh", "Invoice is broken", ts=_now_ts(hub)))
    assert len(_autos(hub)) == 1 and "7-ACK" in _st(hub).history


# ---------------------------------------------------------------- round 4: token-boundary bypasses of _clean_name

