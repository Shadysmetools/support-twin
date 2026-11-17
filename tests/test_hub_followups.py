"""Follow-ups approved by Sysadmin: (1) the configurable 60s FALLBACK ack (exclusive with the normal ack, enforced in the
state, driven by the in-process tick) and a ticket number in the CONFIRM template; (2) new tickets that follow a closed
one are LINKED to it (previous_ticket_id, 'Follows up #N' in Odoo, 'linked to #N' in the Telegram brief)."""
from __future__ import annotations

import threading

import pytest

from twin_agent.hub import fakes, templates
from twin_agent.hub import pipeline as pipeline_mod
from hub_helpers import approve, build_hub, deliver, load, msg, pending, run_to_resolved

PHONE = "201000000001"


def _st(hub):
    return hub.latest_ticket(PHONE)


def _acks(hub):
    return [s for s in hub.whatsapp.sent if s["automatic"]]


def _gated_analysis(monkeypatch):
    """Make ANALYSE slow: it blocks until the returned event is set."""
    gate, started = threading.Event(), threading.Event()
    real = pipeline_mod.analyse_mod.analyse

    def slow(*a, **k):
        started.set()
        gate.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(pipeline_mod.analyse_mod, "analyse", slow)
    return gate, started


def _run_first_contact_in_thread(hub, payload="webhook_text.json"):
    t = threading.Thread(target=lambda: deliver(hub, payload))
    t.start()
    return t


# ---------------------------------------------------------------- 1. the fallback ack

def test_slow_analysis_gets_the_fallback_ack_at_60s_and_then_no_normal_ack(monkeypatch):
    hub = build_hub()
    gate, started = _gated_analysis(monkeypatch)
    t = _run_first_contact_in_thread(hub)
    assert started.wait(5)
    assert hub.tick()["fallback_acks"] == 0 and _acks(hub) == []              # not yet 60s
    hub.clock.advance(seconds=61)
    assert hub.tick()["fallback_acks"] == 1
    assert len(_acks(hub)) == 1 and "#" not in _acks(hub)[0]["text"]           # no ticket number in the fallback
    assert "Acme Support" in _acks(hub)[0]["text"]
    gate.set()
    t.join(10)
    assert len(_acks(hub)) == 1                                                # the normal ack never sends
    st = _st(hub)
    assert "7-ACK(fallback)" in st.history and st.stage == "awaiting_owner"
    assert f"#{st.tid}" in pending(hub, "confirm")["draft"]                    # the CONFIRM carries the ticket number
    hub.clock.advance(minutes=5)
    hub.tick()
    assert len(_acks(hub)) == 1


def test_fast_path_sends_the_normal_ack_and_never_a_fallback():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    assert len(_acks(hub)) == 1 and "ticket #" in _acks(hub)[0]["text"].lower()
    hub.clock.advance(minutes=10)
    assert hub.tick()["fallback_acks"] == 0 and len(_acks(hub)) == 1


def test_fallback_when_odoo_is_down_then_recovery_adds_no_second_ack():
    hub = build_hub()
    real = hub.odoo.create_ticket
    hub.odoo.create_ticket = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("odoo down"))
    deliver(hub, "webhook_text.json")
    assert _acks(hub) == []
    hub.clock.advance(seconds=61)
    assert hub.tick()["fallback_acks"] == 1 and len(_acks(hub)) == 1
    hub.odoo.create_ticket = real
    hub.clock.advance(minutes=3)
    hub.tick()                                                                  # the retry creates the ticket
    assert _st(hub) is not None and len(_acks(hub)) == 1                        # still exactly one automatic message
    assert "7-ACK(fallback)" in _st(hub).history


@pytest.mark.parametrize("trial", range(15))
def test_a_race_between_the_fallback_and_the_normal_ack_sends_exactly_one(trial):
    hub = build_hub()
    real_ack = hub._ack
    hub._ack = lambda *a, **k: None                                             # ticket created, ack NOT yet attempted
    deliver(hub, "webhook_text.json")
    hub._ack = real_ack
    state = _st(hub)
    hub.clock.advance(seconds=60)                                               # exactly at the threshold
    barrier = threading.Barrier(2)

    def normal():
        barrier.wait()
        hub._ack(hub.ticket(state.tid), [])

    def fallback():
        barrier.wait()
        hub.tick()

    ts = [threading.Thread(target=normal), threading.Thread(target=fallback)]
    [x.start() for x in ts]
    [x.join(10) for x in ts]
    assert len(_acks(hub)) == 1


class _Crash(BaseException):
    pass


def test_crash_after_the_fallback_attempt_never_gives_two_acks():
    class Dies(fakes.FakeWhatsApp):
        n = 0

        def send_text(self, to, text, **kw):
            r = super().send_text(to, text, **kw)
            if Dies.n == 0:
                Dies.n += 1
                raise _Crash()                                                  # died after Meta accepted the fallback
            return r

    hub = build_hub(whatsapp=Dies())
    hub.accept(load("webhook_text.json"))
    hub.clock.advance(seconds=61)
    with pytest.raises(_Crash):
        hub.tick()
    assert len(_acks(hub)) == 1
    hub.retry_failed()                                                          # the pipeline now runs: normal ack suppressed
    hub.clock.advance(minutes=5)
    hub.tick()
    assert len(_acks(hub)) == 1 and _st(hub) is not None


@pytest.mark.parametrize("off", [0, None])
def test_disabled_keeps_the_old_behaviour(monkeypatch, off):
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "fallback_ack_seconds": off})
    gate, started = _gated_analysis(monkeypatch)
    t = _run_first_contact_in_thread(hub)
    assert started.wait(5)
    hub.clock.advance(minutes=4)                          # past 60s, still inside the 5-minute in-flight window
    assert hub.tick()["fallback_acks"] == 0 and _acks(hub) == []
    gate.set()
    t.join(10)
    assert len(_acks(hub)) == 1 and "ticket #" in _acks(hub)[0]["text"].lower()   # the normal ack, as before


def test_fallback_threshold_is_configurable(monkeypatch):
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "fallback_ack_seconds": 10})
    gate, started = _gated_analysis(monkeypatch)
    t = _run_first_contact_in_thread(hub)
    assert started.wait(5)
    hub.clock.advance(seconds=11)
    assert hub.tick()["fallback_acks"] == 1
    gate.set()
    t.join(10)


def test_fallback_respects_the_24h_window_and_skips_unknown_senders(monkeypatch):
    hub = build_hub()
    hub.accept(load("webhook_text.json"))
    hub.clock.advance(hours=25)                                                 # the window is already closed
    assert hub.tick()["fallback_acks"] == 0
    hub2 = build_hub(knowledge_odoo=fakes.FakeOdooKnowledge(partners={}))
    hub2.accept(load("webhook_text.json"))
    hub2.clock.advance(seconds=120)
    assert hub2.tick()["fallback_acks"] == 0 and _acks(hub2) == []              # strangers get nothing automatic


def test_fallback_templates_have_no_ticket_number_and_no_english_in_arabic():
    en, ar = templates.fallback_ack("en", name="Oscar"), templates.fallback_ack("ar", name="عمر")
    assert "#" not in en and "Acme Support" in en and "specialist" in en
    assert "#" not in ar and "دعم Acme" in ar
    import re
    assert [w for w in re.findall(r"[A-Za-z]{2,}", ar) if w != "Acme"] == []


def test_confirm_template_has_a_ticket_number_slot():
    for lang, name in (("en", "Oscar"), ("ar", "عمر")):
        with_n = templates.confirm(lang, name=name, issue="x", module="Inventory", outcome="y", n=4711)
        assert "#4711" in with_n
        assert "#" not in templates.confirm(lang, name=name, issue="x", module="Inventory", outcome="y")


# ---------------------------------------------------------------- 2. linked new tickets

def _creates_vals(hub):
    return [w["vals"] for w in hub.odoo.t.writes if w["method"] == "create"]


def test_a_new_ticket_after_keep_closed_is_linked_to_the_previous_one():
    hub = build_hub()
    run_to_resolved(hub)
    first = _st(hub)
    assert first.previous_ticket_id == 0 and "Follows up" not in _creates_vals(hub)[0]["description"]
    deliver(hub, msg("k1", "Thanks!", ts=int(hub.clock().timestamp()) - 10))
    approve(hub, "reopen", approved=False)                                      # keep closed
    deliver(hub, msg("k2", "New problem: payroll is broken", ts=int(hub.clock().timestamp()) - 10))
    new = _st(hub)
    assert new.tid != first.tid and new.previous_ticket_id == first.tid
    assert f"Follows up #{first.tid}" in _creates_vals(hub)[1]["description"]
    brief = [t["brief"] for t in hub.telegram.topics.values()][-1]
    assert f"linked to #{first.tid}" in brief


def test_a_new_ticket_after_reopen_days_is_linked():
    hub = build_hub()
    run_to_resolved(hub)
    first = _st(hub)
    hub.clock.advance(days=8)                                                   # reopen_days default is 7
    deliver(hub, msg("l1", "New problem: payroll is broken", ts=int(hub.clock().timestamp()) - 10))
    new = _st(hub)
    assert new.previous_ticket_id == first.tid and f"Follows up #{first.tid}" in _creates_vals(hub)[1]["description"]


def test_reopen_days_is_configurable_for_linking():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "reopen_days": 1})
    run_to_resolved(hub)
    first = _st(hub)
    hub.clock.advance(days=2)
    deliver(hub, msg("l2", "Another thing", ts=int(hub.clock().timestamp()) - 10))
    assert _st(hub).previous_ticket_id == first.tid


def test_a_new_ticket_via_new_issue_after_close_is_linked_and_escaped():
    hub = build_hub()
    run_to_resolved(hub)
    first = _st(hub)
    deliver(hub, msg("n1", "Thanks! <b>also</b> the report is empty", ts=int(hub.clock().timestamp()) - 10))
    req = pending(hub, "reopen")
    hub.on_owner_decision(req["nonce"], 1003, approved=False, new_issue=True)
    new = _st(hub)
    desc = _creates_vals(hub)[1]["description"]
    assert new.previous_ticket_id == first.tid and f"Follows up #{first.tid}" in desc and "<b>also" not in desc


def test_linking_survives_a_retried_first_contact():
    hub = build_hub()
    run_to_resolved(hub)
    first = _st(hub)
    hub.clock.advance(days=8)
    real = hub.telegram.open_topic
    hub.telegram.open_topic = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("tg down"))
    deliver(hub, msg("r1", "New problem: payroll is broken", ts=int(hub.clock().timestamp()) - 10))
    hub.telegram.open_topic = real
    hub.clock.advance(seconds=31)
    hub.tick()
    assert _st(hub).previous_ticket_id == first.tid
    assert f"linked to #{first.tid}" in [t["brief"] for t in hub.telegram.topics.values()][-1]
