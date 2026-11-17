"""Code re-review round 3 (X1-X3, M-a..M-c) + security MED-1: keep-closed never swallows an issue, the tick runs in
the server process (and a second hub on one db is refused), owner status updates are side messages, an interrupted
send is never re-offered, and the webhook worker pool does not queue everyone behind one customer."""
from __future__ import annotations

import threading

import pytest

from twin_agent.hub import fakes, proof_runner
from twin_agent.hub.store import SqliteStore
from hub_helpers import approve, build_hub, deliver, load, msg, pending, run_to_resolved
from test_hub_exits import FlakyTG, PHONE, _client_sends, _creates, _now, _st, _to_in_progress
from test_hub_webhook_security import post, serve, signed

OWNER = 1003
STATUS = "Hello Oscar, we are working on it, the ETA is Friday. — Acme Support"


def _inquiry_resolved(hub):
    deliver(hub, msg("i1", "How do I print a delivery slip in Inventory?", ts=1762857600))
    approve(hub, "confirm")
    deliver(hub, msg("i2", "yes", ts=_now(hub)))
    approve(hub, "advance")
    tid = _st(hub).tid
    ans = "Hello Oscar, open the delivery order and press Print, then choose Delivery Slip. — Acme Support"
    hub.on_owner_text(tid, OWNER, ans, kind="reply")
    approve(hub, "reply")
    hub.close_ticket(tid, OWNER)
    return tid


# ---------------------------------------------------------------- X1: keep-closed / new-issue

def test_n2_the_message_that_raised_the_question_can_become_a_new_ticket():
    hub = build_hub()
    run_to_resolved(hub)
    first = _st(hub)
    hub.clock.advance(hours=1)
    deliver(hub, msg("n2", "Thanks! Also the sales report is now empty", ts=_now(hub)))
    req = pending(hub, "reopen")
    assert hub.on_owner_decision(req["nonce"], 6666, approved=False, new_issue=True)["ok"] is False   # actor-gated
    assert _creates(hub) == 1
    out = hub.on_owner_decision(req["nonce"], OWNER, approved=False, new_issue=True)
    assert out["ok"] is True
    new = _st(hub)
    assert new.tid != first.tid and _creates(hub) == 2 and hub.whatsapp.automatic_count() == 2
    assert "sales report" in new.inbound.text and hub.ticket(first.tid).stage == "resolved"
    assert pending(hub, "confirm") is not None                       # the new ticket proceeds normally


def test_keep_closed_archives_the_message_to_triage_and_the_next_message_opens_a_new_ticket():
    hub = build_hub()
    run_to_resolved(hub)
    deliver(hub, msg("k1", "Thanks!", ts=_now(hub)))
    approve(hub, "reopen", approved=False)
    assert any("kept closed" in t.lower() and "note" in t.lower() for t in hub.telegram.triage)
    deliver(hub, msg("k2", "New problem: payroll is broken", ts=_now(hub)))
    assert len(hub.tickets_for(PHONE)) == 2 and hub.whatsapp.automatic_count() == 2


def test_n1_a_message_after_close_ticket_asks_the_owner_and_new_issue_opens_the_ticket():
    hub = build_hub()
    _inquiry_resolved(hub)
    hub.clock.advance(days=2)
    res = deliver(hub, msg("n1", "New problem: payroll is broken", ts=_now(hub)))
    assert res.get("reopen_asked") is True and len(hub.tickets_for(PHONE)) == 1      # no automatic ack / ticket
    req = pending(hub, "reopen")
    assert hub.on_owner_decision(req["nonce"], OWNER, approved=False, new_issue=True)["ok"] is True
    assert len(hub.tickets_for(PHONE)) == 2 and hub.whatsapp.automatic_count() == 2


def test_owner_text_on_a_resolved_ticket_tells_the_owner_what_to_use():
    hub = build_hub()
    run_to_resolved(hub)
    out = hub.on_owner_text(_st(hub).tid, OWNER, "Hello Oscar, one more thing. — Acme Support", kind="reply")
    assert out["ok"] is False and out["reason"] == "ticket_closed" and "reopen" in out["message"].lower()


# ---------------------------------------------------------------- X2: one process

def test_hub_server_runs_the_tick_in_process_and_stops_it():
    hub = build_hub()
    calls = []
    hub.tick = lambda *a, **k: calls.append(1) or {}
    with serve(hub, tick_interval=0.05) as (srv, base):
        for _ in range(100):
            if len(calls) >= 2:
                break
            threading.Event().wait(0.05)
        assert len(calls) >= 2
    n = len(calls)
    threading.Event().wait(0.3)
    assert len(calls) <= n + 1                                      # stopped with the server


def test_tick_cli_dry_run_never_touches_a_real_file(tmp_path, monkeypatch):
    from twin_agent.hub import tick as tick_mod
    monkeypatch.chdir(tmp_path)
    assert tick_mod.main(["--dry-run"]) == 0
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(SystemExit):
        tick_mod.main(["--dry-run", "--db", "hub.db"])               # no way to point the fake hub at real state


def test_a_second_hub_on_the_same_sqlite_file_is_refused(tmp_path):
    db = str(tmp_path / "hub.db")
    s1, s2 = SqliteStore(db), SqliteStore(db)
    try:
        build_hub(store=s1)
        with pytest.raises(RuntimeError):
            build_hub(store=s2)
    finally:
        s1.close()
        s2.close()
    s3 = SqliteStore(db)                                            # after the first hub's store closed: fine again
    try:
        build_hub(store=s3)
    finally:
        s3.close()


def test_worker_pool_does_not_queue_every_customer_behind_one_slow_sender():
    gate = threading.Event()
    hub = build_hub()
    real = hub.handle_webhook

    def slow_for_sender_one(payload):
        if payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"] == "201000000001":
            gate.wait(10)
        return real(payload)

    hub.handle_webhook = slow_for_sender_one
    other = load("webhook_text.json")
    other["entry"][0]["changes"][0]["value"]["messages"][0].update({"id": "o2", "from": "201000000002"})
    with serve(hub) as (srv, base):
        body, h = signed(load("webhook_text.json"))
        assert post(base, body, h)[0] == 200
        body2, h2 = signed(other)
        assert post(base, body2, h2)[0] == 200
        for _ in range(100):
            if hub.tickets_for("201000000002"):
                break
            threading.Event().wait(0.05)
        assert hub.tickets_for("201000000002") and not hub.tickets_for(PHONE)
        gate.set()
        srv.drain()
    assert hub.tickets_for(PHONE)


# ---------------------------------------------------------------- X3: status updates are side messages

def test_n5_status_update_in_progress_keeps_stage_and_the_fix_ready_approval():
    hub = build_hub()
    _to_in_progress(hub)
    fr = pending(hub, "fix_ready")
    assert hub.on_owner_text(_st(hub).tid, OWNER, STATUS, kind="reply")["ok"] is True      # not a guidance ticket
    assert pending(hub, "status") is not None
    assert approve(hub, "status")["ok"] is True and _client_sends(hub)[-1]["text"] == STATUS
    st = _st(hub)
    assert st.stage == "in_progress" and st.pending_nonce == fr["nonce"]
    assert pending(hub, "fix_ready")["nonce"] == fr["nonce"]
    assert hub.reask(st.tid, OWNER)["ok"] is True and hub.tick()["reasked"] == 0
    assert approve(hub, "fix_ready")["ok"] is True and _st(hub).stage == "awaiting_owner_resolve"


def test_n6_status_update_before_resolve_keeps_the_proof_backed_resolve():
    hub = build_hub()
    _to_in_progress(hub)
    approve(hub, "fix_ready")
    res = pending(hub, "resolve")
    hub.on_owner_text(_st(hub).tid, OWNER, STATUS, kind="status")
    approve(hub, "status")
    assert _st(hub).stage == "awaiting_owner_resolve" and pending(hub, "resolve")["nonce"] == res["nonce"]
    assert approve(hub, "resolve")["ok"] is True and _st(hub).stage == "resolved"
    assert hub.learning.records[0]["proof_outcome"] == "reproduced"     # the ShipShape-proof resolve was reached


def test_a_declined_status_changes_nothing():
    hub = build_hub()
    _to_in_progress(hub)
    hub.on_owner_text(_st(hub).tid, OWNER, STATUS, kind="status")
    approve(hub, "status", approved=False)
    assert pending(hub, "fix_ready") is not None and hub.holds() == [] and _st(hub).declined is False


# ---------------------------------------------------------------- M1: an interrupted send is unknown

class _Crash(BaseException):
    pass


def test_crash_between_the_attempt_record_and_the_send_result_never_gives_two_sends():
    class Dies(fakes.FakeWhatsApp):
        n = 0

        def send_text(self, to, text, **kw):
            r = super().send_text(to, text, **kw)
            if not kw.get("automatic") and Dies.n == 0:
                Dies.n += 1
                raise _Crash()                                       # the process dies after Meta accepted it
            return r

    hub = build_hub(whatsapp=Dies())
    deliver(hub, "webhook_text.json")
    with pytest.raises(_Crash):
        approve(hub, "confirm")
    tid = _st(hub).tid
    assert _st(hub).attempting_kind == "confirm" and len(_client_sends(hub)) == 1
    assert hub.reask(tid, OWNER)["asked"] is False                  # the interrupted send is treated as unknown
    deliver(hub, msg("c1", "hello?", ts=_now(hub)))
    hub.tick()
    assert pending(hub, "confirm") is None
    assert len(_client_sends(hub)) == 1 and any("manually" in t.lower() for t in hub.telegram.triage)
    assert hub.on_owner_text(tid, OWNER, STATUS, kind="status")["reason"] == "send_outcome_unknown"
    assert hub.confirm_unknown_send(tid, OWNER, sent=True)["ok"] is True and len(_client_sends(hub)) == 1


# ---------------------------------------------------------------- M2 / M3 / N9

def test_n17_a_retried_reply_re_asks_an_expired_confirm():
    tg = FlakyTG()
    hub = build_hub(telegram=tg, cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 1})
    deliver(hub, "webhook_text.json")
    hub.clock.advance(hours=2)
    hub.tick()                                                      # the confirm expired and was cleared
    assert pending(hub, "confirm") is None
    orig = tg.request_approval
    tg.request_approval = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("tg down"))
    res = deliver(hub, msg("n17", "hello?", ts=_now(hub)))
    assert res.get("error")
    tg.request_approval = orig
    hub.retry_failed()
    assert pending(hub, "confirm") is not None and _st(hub).stage == "awaiting_owner"


def test_n7_reopening_after_an_owner_authored_resolve_clears_needs_owner_text():
    hub = build_hub(proof=proof_runner.FakeProofRunner(verdict="not_reproduced"))
    _to_in_progress(hub)
    approve(hub, "fix_ready")
    tid = _st(hub).tid
    assert _st(hub).needs_owner_text is True
    ans = "Hello Oscar, it works as designed; the report only shows posted entries. — Acme Support"
    hub.on_owner_text(tid, OWNER, ans, kind="resolve")
    approve(hub, "resolve")
    assert _st(hub).stage == "resolved" and _st(hub).needs_owner_text is False
    deliver(hub, msg("n7", "Still not right", ts=_now(hub)))
    approve(hub, "reopen")
    st = _st(hub)
    assert st.stage == "in_progress" and st.needs_owner_text is False and pending(hub, "fix_ready") is not None


def test_n9_tapping_an_approval_tick_already_expired_says_expired():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 1})
    deliver(hub, "webhook_text.json")
    req = pending(hub, "confirm")
    hub.clock.advance(hours=2)
    hub.tick()
    assert hub.on_owner_decision(req["nonce"], OWNER)["reason"] == "expired"
