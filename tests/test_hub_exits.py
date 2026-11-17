"""Code re-review of c9051f7 (B1-B3, M1-M3): every ordinary event must leave the ticket MOVABLE (not just refused),
tickets must be closable, failed messages must be reported and retried by ``Hub.tick``, an unknown send result is
never re-sent, a held voice reply is re-transcribed, and one slow ticket never blocks another customer."""
from __future__ import annotations

import threading

import pytest

from twin_agent.hub import fakes, proof_runner
from hub_helpers import approve, build_hub, deliver, load, msg, pending, run_to_resolved

PHONE = "201000000001"
LONG = {"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 72}


def _now(hub) -> int:
    return int(hub.clock().timestamp()) - 10


def _st(hub):
    return hub.latest_ticket(PHONE)


def _creates(hub) -> int:
    return sum(1 for w in hub.odoo.t.writes if w["method"] == "create")


def _to_in_progress(hub):
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")
    return approve(hub, "advance")


def _client_sends(hub):
    return [s for s in hub.whatsapp.sent if not s["automatic"]]


# ---------------------------------------------------------------- B1: the ticket can always move on

def test_fix_ready_does_not_expire_with_the_24h_window():
    hub = build_hub()
    _to_in_progress(hub)
    hub.clock.advance(hours=30)                                    # a real fix takes more than a day
    out = approve(hub, "fix_ready")
    assert out["ok"] is True and len(hub.proof.calls) == 1


def test_internal_ttl_is_configurable():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "internal_approval_ttl_days": 1})
    _to_in_progress(hub)
    hub.clock.advance(days=2)
    assert approve(hub, "fix_ready")["reason"] == "expired"


def test_expired_tap_reissues_so_the_ticket_moves_on():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 1})
    deliver(hub, "webhook_text.json")
    hub.clock.advance(hours=2)
    first = approve(hub, "confirm")
    assert first["ok"] is False and first["reason"] == "expired"
    assert pending(hub, "confirm") is not None                     # a FRESH approval exists
    assert approve(hub, "confirm")["ok"] is True and len(_client_sends(hub)) == 1


def test_decline_then_customer_message_re_asks_the_owner():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm", approved=False)
    assert pending(hub, "confirm") is None and hub.holds()[0]["reason"] == "draft_rejected"
    deliver(hub, msg("d1", "hello?", ts=_now(hub)))
    assert pending(hub, "confirm") is not None                     # re-asked
    assert approve(hub, "confirm")["ok"] is True and len(_client_sends(hub)) == 1


def test_decline_then_owner_authored_text_goes_through_the_normal_path():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    tid = _st(hub).tid
    approve(hub, "confirm", approved=False)
    text = "Hello Oscar, we are checking the stock quantity and will update you today. — Acme Support"
    assert hub.on_owner_text(tid, 6666, text, kind="confirm")["ok"] is False        # not the owner
    assert hub.on_owner_text(tid, None, text, kind="confirm")["ok"] is False
    assert hub.on_owner_text(tid, 1003, "call me on +20 100 123 4567 — Acme Support",
                             kind="confirm")["ok"] is False                           # same scan as edits
    assert hub.on_owner_text(tid, 1003, text, kind="confirm")["ok"] is True
    req = pending(hub, "confirm")
    assert req["draft"] == text and _client_sends(hub) == []                          # still needs the bound approval
    assert approve(hub, "confirm")["ok"] is True and _client_sends(hub)[0]["text"] == text


def test_window_closed_resolve_holds_with_a_template_message_and_the_ticket_moves_on():
    hub = build_hub(cfg=LONG)
    _to_in_progress(hub)
    hub.clock.advance(days=3)
    approve(hub, "fix_ready")
    out = approve(hub, "resolve")
    assert out["reason"] == "window_closed"
    assert any("approved template" in t for t in hub.telegram.triage)
    deliver(hub, msg("w1", "any news?", ts=_now(hub)))              # the customer writes -> window open again
    assert pending(hub, "resolve") is not None                      # the owner is asked again
    assert approve(hub, "resolve")["ok"] is True and _st(hub).stage == "resolved"


class FlakyTG(fakes.FakeTelegram):
    down = False

    def post(self, t, x):
        if self.down:
            raise ConnectionError("tg down")
        return super().post(t, x)

    def request_approval(self, *a, **k):
        if self.down:
            raise ConnectionError("tg down")
        return super().request_approval(*a, **k)


def test_telegram_down_during_an_owner_tap_is_recoverable():
    tg = FlakyTG()
    hub = build_hub(telegram=tg)
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")
    req = pending(hub, "advance")
    tg.down = True
    out = hub.on_owner_decision(req["nonce"], req["allowed_approvers"][0])          # must not raise
    assert out["ok"] is True or out["reason"] == "action_failed"
    tg.down = False
    assert _st(hub).stage == "in_progress"
    hub.tick()                                                      # self-heals without waiting for the customer
    assert pending(hub, "fix_ready") is not None and _st(hub).stage == "in_progress"
    assert approve(hub, "fix_ready")["ok"] is True


# ---------------------------------------------------------------- B2: tickets can close

def test_inquiry_closes_through_an_owner_authored_reply_then_close_and_does_not_swallow_the_next_issue():
    hub = build_hub()
    deliver(hub, msg("i1", "How do I print a delivery slip in Inventory?", ts=1762857600))
    approve(hub, "confirm")
    deliver(hub, msg("i2", "yes", ts=_now(hub)))
    approve(hub, "advance")
    tid = _st(hub).tid
    assert [h["reason"] for h in hub.holds()] == ["guidance_needed"] and _st(hub).stage == "in_progress"
    ans = "Hello Oscar, open the delivery order and press Print, then choose Delivery Slip. — Acme Support"
    assert hub.on_owner_text(tid, 1003, ans, kind="reply")["ok"] is True
    assert approve(hub, "reply")["ok"] is True and _client_sends(hub)[-1]["text"] == ans
    assert hub.close_ticket(tid, 6666)["ok"] is False and hub.close_ticket(tid, None)["ok"] is False
    assert hub.close_ticket(tid, 1003)["ok"] is True and _st(hub).stage == "resolved"
    assert len(hub.metrics.pushed) == 1 and len(hub.learning.records) == 1
    hub.clock.advance(days=8)
    deliver(hub, msg("i3", "New problem: payroll is broken", ts=_now(hub)))
    assert len(hub.tickets_for(PHONE)) == 2 and _creates(hub) == 2 and hub.whatsapp.automatic_count() == 2


def test_not_reproduced_ticket_can_also_be_answered_and_closed():
    hub = build_hub(proof=proof_runner.FakeProofRunner(verdict="not_reproduced"))
    _to_in_progress(hub)
    approve(hub, "fix_ready")
    tid = _st(hub).tid
    ans = "Hello Oscar, we could not reproduce it; please send a short screen recording. — Acme Support"
    assert hub.on_owner_text(tid, 1003, ans, kind="reply")["ok"] is True
    approve(hub, "reply")
    assert hub.close_ticket(tid, 1003)["ok"] is True and _st(hub).stage == "resolved"


def test_thanks_after_resolve_kept_closed_then_a_new_issue_is_not_swallowed():
    hub = build_hub()
    run_to_resolved(hub)
    first = _st(hub)
    hub.clock.advance(hours=1)
    deliver(hub, msg("t1", "Thanks!", ts=_now(hub)))
    st = _st(hub)
    assert st.stage == "resolved" and pending(hub, "reopen") is not None      # stays resolved until the owner chooses
    n_asks = len([a for a in hub.telegram.approvals if a["kind"] == "reopen"])
    deliver(hub, msg("t2", "all good", ts=_now(hub)))                           # same question, no second ask
    assert len([a for a in hub.telegram.approvals if a["kind"] == "reopen"]) == n_asks
    assert len(_st(hub).replies) >= 2 and hub.metrics.pushed.__len__() == 1
    approve(hub, "reopen", approved=False)                                      # keep closed
    assert _st(hub).stage == "resolved"
    hub.clock.advance(minutes=5)
    res = deliver(hub, msg("t3", "New problem: the sales report is empty", ts=_now(hub)))
    assert res["ticket_id"] != first.tid and _creates(hub) == 2
    assert hub.whatsapp.automatic_count() == 2 and len(hub.tickets_for(PHONE)) == 2


def test_owner_can_choose_reopen():
    hub = build_hub()
    run_to_resolved(hub)
    deliver(hub, msg("r1", "It is still wrong", ts=_now(hub)))
    assert _st(hub).stage == "resolved"
    out = approve(hub, "reopen")
    st = _st(hub)
    assert out["ok"] is True and st.stage == "in_progress" and st.reopen_count == 1 and _creates(hub) == 1
    assert pending(hub, "fix_ready") is not None


def test_second_reply_supersedes_a_pending_advance():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    deliver(hub, msg("s1", "yes", ts=_now(hub)))
    first = pending(hub, "advance")
    deliver(hub, msg("s2", "actually no", ts=_now(hub)))
    second = pending(hub, "advance")
    assert second["nonce"] != first["nonce"] and second["draft"] == "actually no"
    assert approve(hub, nonce=first["nonce"], as_tg=1003)["ok"] is False


# ---------------------------------------------------------------- B3: failures are reported and retried by tick

class _Down:
    def __init__(self, hub):
        self.real = hub.odoo.create_ticket
        self.hub = hub

    def __enter__(self):
        self.hub.odoo.create_ticket = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("odoo down"))

    def __exit__(self, *a):
        self.hub.odoo.create_ticket = self.real


def test_failed_message_is_reported_and_tick_retries_with_backoff():
    hub = build_hub()
    down = _Down(hub)
    down.__enter__()
    res = deliver(hub, "webhook_text.json")
    assert res.get("error") and len(hub.telegram.triage) == 1 and "processing_failed" in hub.telegram.triage[0]
    assert PHONE not in hub.telegram.triage[0] and "stock quantity" not in hub.telegram.triage[0]   # no PII
    assert [h["reason"] for h in hub.holds()] == ["processing_failed"]
    down.__exit__()
    hub.tick()                                                      # inside the 30s backoff: not retried yet
    assert _creates(hub) == 0
    hub.clock.advance(seconds=31)
    out = hub.tick()
    assert _creates(hub) == 1 and hub.whatsapp.automatic_count() == 1 and out["retried"] == 1
    assert hub.holds() == []                                        # the failure hold closed itself
    hub.clock.advance(hours=1)
    hub.tick()
    assert hub.whatsapp.automatic_count() == 1                      # never a second ack


def test_message_that_keeps_failing_goes_dead_with_a_triage_post():
    hub = build_hub()
    _Down(hub).__enter__()
    deliver(hub, "webhook_text.json")
    for _ in range(8):
        hub.clock.advance(hours=7)
        hub.tick()
    assert hub.store.get("messages", "wamid.TEXT0001")["status"] == "dead"
    assert any("dead" in t for t in hub.telegram.triage)
    n = len(hub.telegram.triage)
    hub.clock.advance(hours=7)
    hub.tick()
    assert len(hub.telegram.triage) == n                            # a dead message is not retried or re-announced


def test_tick_expires_stale_approvals_with_a_triage_post_and_reasks_on_demand():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 1})
    deliver(hub, "webhook_text.json")
    tid = _st(hub).tid
    hub.clock.advance(hours=2)
    out = hub.tick()
    assert out["expired"] == 1 and any("expired" in t for t in hub.telegram.triage)
    assert pending(hub, "confirm") is None
    assert hub.reask(tid, 6666)["ok"] is False and hub.reask(tid, 1003)["ok"] is True
    assert pending(hub, "confirm") is not None


def test_tick_cli_dry_run_and_blocked_without_adapters(tmp_path, capsys):
    from twin_agent.hub import tick as tick_mod
    assert tick_mod.main(["--dry-run"]) == 0
    assert '"retried"' in capsys.readouterr().out
    assert tick_mod.main([]) == 2                                   # real adapters not configured -> blocked, no network


# ---------------------------------------------------------------- M1: an unknown send result is never re-sent

def test_timeout_during_confirm_send_is_marked_unknown_and_never_resent():
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
    req = pending(hub, "confirm")
    out = approve(hub, nonce=req["nonce"], as_tg=1003)
    assert out["ok"] is False and out["reason"] == "send_unknown"
    assert pending(hub, "confirm") is None                          # NOT re-asked
    assert any("check" in t.lower() and "manually" in t.lower() for t in hub.telegram.triage)
    assert approve(hub, nonce=req["nonce"], as_tg=1003)["ok"] is False
    hub.tick()
    assert pending(hub, "confirm") is None and len(_client_sends(hub)) == 1       # at most ONE send, ever
    tid = _st(hub).tid
    assert hub.confirm_unknown_send(tid, 6666, sent=True)["ok"] is False
    assert hub.confirm_unknown_send(tid, 1003, sent=True)["ok"] is True
    assert _st(hub).stage == "awaiting_customer"


# ---------------------------------------------------------------- M2: releasing a held voice reply re-runs STT

def test_releasing_a_held_voice_reply_re_runs_stt():
    class DownSTT(fakes.FakeSTT):
        down = False

        def transcribe(self, a, **k):
            if self.down:
                self.calls.append(a)
                return {"status": "blocked", "why": "groq down"}
            return super().transcribe(a, **k)

    stt = DownSTT()
    hub = build_hub(stt=stt)
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    stt.down = True
    v = load("webhook_voice.json")
    v["entry"][0]["changes"][0]["value"]["messages"][0].update({"id": "vr1", "timestamp": str(_now(hub))})
    deliver(hub, v)
    assert "unavailable" in _st(hub).replies[-1]["text"]
    hold = [h for h in hub.holds() if h["reason"] == "stt_unavailable"]
    assert len(hold) == 1
    stt.down = False
    hub.release_hold(hold[0]["id"], actor_tg_id=1003)
    assert "المخزون" in _st(hub).replies[-1]["text"] and len(_st(hub).replies) == 1
    assert hub.holds() == []


# ---------------------------------------------------------------- M3: per-ticket lock, not a global one

def test_slow_proveit_on_one_ticket_does_not_block_another_customer():
    gate, started = threading.Event(), threading.Event()

    class SlowProof(proof_runner.FakeProofRunner):
        def prove(self, *a, **k):
            started.set()
            gate.wait(10)
            return super().prove(*a, **k)

    hub = build_hub(proof=SlowProof())
    _to_in_progress(hub)
    req = pending(hub, "fix_ready")
    t = threading.Thread(target=lambda: hub.on_owner_decision(req["nonce"], req["allowed_approvers"][0]))
    t.start()
    assert started.wait(5)
    done = threading.Event()
    other = load("webhook_text.json")
    m = other["entry"][0]["changes"][0]["value"]["messages"][0]
    m.update({"id": "other1", "from": "201000000002"})
    th2 = threading.Thread(target=lambda: (hub.handle_webhook(other), done.set()))
    th2.start()
    try:
        assert done.wait(3), "another customer was blocked behind PROVE-IT"
    finally:
        gate.set()
        t.join(10)
        th2.join(10)
