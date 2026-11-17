"""sender.py: the only code that sends. All adapters are fakes; no network."""
import asyncio
import copy
from datetime import timedelta

import pytest
from sender_fakedb import FakeStateDB

from twin_agent import paths, sender as sender_mod
from twin_agent.audit import AuditLog
from twin_agent.bus import Topics
from twin_agent.models import (CardKind, Channel, Dial, GuardResult, Lane, OutboundMessage, OutKind, SendStatus,
                                SendTrigger)
from twin_agent.sender import Sender, text_hash
from twin_agent.testing import fakes

HOLD = "Got it, checking this now. - Principal's assistant"
ANSWER = "Open Inventory then Transfers and validate it. - Principal's assistant"


@pytest.fixture
def guard_ok(monkeypatch):
    seen = []

    def fake_check(msg, ctx):
        seen.append(ctx)
        return GuardResult(True, [], [])
    monkeypatch.setattr(sender_mod.guards, "check_outbound", fake_check)
    return seen


def _sleep_recorder(clock, on_sleep=None):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        clock.advance(seconds=seconds)
        if on_sleep:
            on_sleep()
    return calls, fake_sleep


def make_sender(cfg, clock, bus, tg, tmp_home, *, live=True, dial=Dial.auto_on_timeout, gmail=None, sleep=None):
    cfg = copy.deepcopy(cfg)
    if live:
        cfg["run_mode"], cfg["send_enabled"] = "live", True
    db = FakeStateDB(clock, {str(lane): dial for lane in Lane})
    db.requests["REQ-0001"] = {"id": "REQ-0001", "created_at": "2026-11-01T12:40:00+00:00", "first_reply_at": None}
    audit = AuditLog(tmp_home / "data" / "logs" / "actions.jsonl", clock)
    adapters = {Channel.telegram_group: tg, Channel.email: gmail or fakes.FakeGmail()}
    kw = {"sleep": sleep} if sleep else {}
    return Sender(db, cfg, adapters, clock, bus, audit, **kw), db


def hold_msg(step="holding", **kw):
    base = dict(request_id="REQ-0001", step=step, kind=OutKind.holding, channel=Channel.telegram_group, text=HOLD,
                chat_id=fakes.GROUP_ID, topic_id=fakes.TOPIC_LOGISTICS, reply_to=5, is_template=True)
    base.update(kw)
    return OutboundMessage(**base)


def answer_msg(clock, step="draft-1", **kw):
    return hold_msg(step=step, kind=OutKind.answer, text=ANSWER, is_template=False, draft_hash=text_hash(ANSWER),
                    draft_made_at=clock.now() - timedelta(minutes=5), **kw)


def run(coro):
    return asyncio.run(coro)


def cards(bus, kind=None):
    return [p for t, p in bus.published if t == Topics.CARD_OUT and (kind is None or p.kind == kind)]


def test_holding_floor_sends_once_and_audits_without_text(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.sent and len(fake_tg.sent) == 1
    assert fake_tg.sent[0]["reply_to"] == 5 and fake_tg.sent[0]["topic_id"] == fakes.TOPIC_LOGISTICS
    assert db.sends["REQ-0001:holding"]["status"] == "sent"
    assert db.requests["REQ-0001"]["first_reply_by"] == "agent"
    assert any(t == Topics.SEND_DONE for t, _ in bus.published)
    log_text = s.audit.path.read_text(encoding="utf-8")
    assert HOLD not in log_text and text_hash(HOLD) in log_text
    assert s.audit.verify() == (True, None)
    # same key again: duplicate, nothing new goes out
    r2 = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r2.status == SendStatus.duplicate and len(fake_tg.sent) == 1


def test_kill_switch_file_blocks_and_cards_principal_and_backup(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    paths.stop_file().write_text("stop", encoding="utf-8")
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.blocked and r.guard.blocked_by == ["kill_switch"] and not fake_tg.sent
    assert sorted(c.to for c in cards(bus, CardKind.holding_blocked)) == ["backup", "principal"]
    paths.stop_file().unlink()
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.sent   # key not burned


def test_kill_switch_db_flag(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    db.set_cursor("kill_switch", "on")
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.blocked and not fake_tg.sent


def test_observe_records_would_send_kpi_and_card(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, live=False, dial=Dial.observe)
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.would_send and not fake_tg.sent
    assert [k["name"] for k in db.kpis] == ["would_have_sent"]
    assert "would have sent" in cards(bus, CardKind.observe_would_send)[0].text
    # the key stays reusable: once live, the hard floor can still go out
    s.cfg.update(run_mode="live", send_enabled=True)
    db.dials[str(Lane.L1_holding)] = Dial.auto_on_timeout
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.sent


def test_shadow_mode_turns_gate_guards_into_would_send_but_not_safety(cfg, clock, bus, fake_tg, tmp_home, monkeypatch):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, live=False, dial=Dial.observe)
    monkeypatch.setattr(sender_mod.guards, "check_outbound",
                        lambda m, c: GuardResult(False, ["consent", "send_disabled", "dial"], []))
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.would_send
    monkeypatch.setattr(sender_mod.guards, "check_outbound", lambda m, c: GuardResult(False, ["consent", "secrets"], []))
    r = run(s.send(hold_msg(step="holding-b"), SendTrigger.holding_floor))
    assert r.status == SendStatus.blocked and "secrets" in r.guard.blocked_by
    assert not fake_tg.sent


def test_live_mode_any_guard_blocks(cfg, clock, bus, fake_tg, tmp_home, monkeypatch):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    monkeypatch.setattr(sender_mod.guards, "check_outbound", lambda m, c: GuardResult(False, ["consent"], []))
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.blocked and not fake_tg.sent
    assert any(k["name"] == "guard_trip" for k in db.kpis)


def test_guard_crash_blocks(cfg, clock, bus, fake_tg, tmp_home, monkeypatch):
    s, _ = make_sender(cfg, clock, bus, fake_tg, tmp_home)

    def boom(m, c):
        raise RuntimeError("bug")
    monkeypatch.setattr(sender_mod.guards, "check_outbound", boom)
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.blocked and r.guard.blocked_by[0].startswith("guards_error") and not fake_tg.sent


def test_dial_off_or_too_low_blocks(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.off)
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).guard.blocked_by == ["dial"]
    db.dials[str(Lane.L3_t1_answer)] = Dial.draft          # Co-pilot: a timeout may not send
    assert run(s.send(answer_msg(clock), SendTrigger.timeout)).guard.blocked_by == ["dial"]
    assert not fake_tg.sent


def _approve(db, clock, action="ap", user=fakes.PRINCIPAL_ID, draft_hash=None):
    return db.issue_approval("REQ-0001", 1, action, user, draft_hash or text_hash(ANSWER), clock.now() + timedelta(hours=24))


def test_approval_sends_once_and_only_for_principal(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    nonce = _approve(db, clock)
    r = run(s.send(answer_msg(clock), SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent and guard_ok[-1].approval["nonce"] == nonce
    # the nonce is one-time
    r2 = run(s.send(answer_msg(clock, step="draft-2"), SendTrigger.approval, approval_nonce=nonce,
                    approver_user_id=fakes.PRINCIPAL_ID))
    assert r2.guard.blocked_by == ["approval_binding"]
    # the backup can never approve a send as Principal (red-team #3)
    n2 = _approve(db, clock, user=fakes.BACKUP_ID)
    r3 = run(s.send(answer_msg(clock, step="draft-3"), SendTrigger.approval, approval_nonce=n2,
                    approver_user_id=fakes.BACKUP_ID))
    assert r3.guard.blocked_by == ["approval_binding"]
    # no nonce at all
    assert run(s.send(answer_msg(clock, step="draft-4"), SendTrigger.approval)).guard.blocked_by == ["approval_binding"]
    assert len(fake_tg.sent) == 1


def test_approval_bound_to_draft_hash(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    nonce = _approve(db, clock, draft_hash=text_hash("an older draft"))
    r = run(s.send(answer_msg(clock), SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.guard.blocked_by == ["approval_binding"]
    tampered = answer_msg(clock, step="draft-2")
    tampered.text = "something else"                       # text no longer matches its draft hash
    n2 = _approve(db, clock)
    r2 = run(s.send(tampered, SendTrigger.approval, approval_nonce=n2, approver_user_id=fakes.PRINCIPAL_ID))
    assert r2.guard.blocked_by == ["approval_binding"] and not fake_tg.sent


def test_edit_echo_send_uses_sd_action(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    nonce = _approve(db, clock, action="sd")
    r = run(s.send(answer_msg(clock), SendTrigger.principal_edit, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent


def test_stale_draft_recards_instead_of_sending(cfg, clock, bus, fake_tg, tmp_home, guard_ok, make_event):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    fake_tg.thread.append(make_event("also IN/00001/26", msg_id=6, ts=clock.now() - timedelta(minutes=1)))
    nonce = _approve(db, clock)
    r = run(s.send(answer_msg(clock), SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.blocked and "stale_draft" in r.guard.blocked_by and not fake_tg.sent
    assert any(t == Topics.REQUEST_UPDATED for t, _ in bus.published)
    assert len(guard_ok[-1].thread_after_draft) == 1
    # the holding template is not a draft: a new requester message does not stop it
    db.dials[str(Lane.L1_holding)] = Dial.auto_on_timeout
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.sent


def test_human_active_lock_and_already_answered(cfg, clock, bus, fake_tg, tmp_home, guard_ok, make_event):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    fake_tg.thread.append(make_event("I'm on it", msg_id=6, sender=fakes.PRINCIPAL, ts=clock.now() - timedelta(minutes=2),
                                     reply_to=5))
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.guard.blocked_by == ["human_active"]
    clock.advance(minutes=10)
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.guard.blocked_by == ["already_answered"] and not fake_tg.sent


def test_our_own_earlier_send_is_not_principal_activity(cfg, clock, bus, fake_tg, tmp_home, guard_ok, make_event):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    fake_tg.thread.append(make_event(HOLD, msg_id=int(r.remote_id), sender=fakes.PRINCIPAL, ts=clock.now()))
    clock.advance(minutes=1)
    assert run(s.send(answer_msg(clock), SendTrigger.timeout)).status == SendStatus.sent


def test_wrong_identity_deletes_demotes_and_alerts(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    fake_tg.from_id = fakes.GROUP_ID                       # posted as the anonymous group
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.failed and fake_tg.deleted == [(fakes.GROUP_ID, int(fake_tg.sent[0]["msg_id"]))]
    assert db.dials[str(Lane.L1_holding)] == Dial.draft
    assert db.sends["REQ-0001:holding"]["from_id_ok"] == 0
    assert cards(bus, CardKind.demotion)


def test_flood_wait_waits_then_sends(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    fake_tg.raise_on_send = "FloodWait"
    calls, sleep = _sleep_recorder(clock, on_sleep=lambda: setattr(fake_tg, "raise_on_send", None))
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, sleep=sleep)
    s.max_flood_wait_s = 60                                 # default 30: the bus awaits handlers one by one
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert calls == [60] and r.status == SendStatus.sent and len(fake_tg.sent) == 1


def test_long_flood_wait_queues_then_drains(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    class LongFlood(Exception):
        seconds = 3600
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)

    async def flood(*a, **k):
        raise type("FloodWaitError", (LongFlood,), {})("wait")
    real = fake_tg.send_message
    fake_tg.send_message = flood
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.failed and len(s.queue) == 1
    assert db.sends["REQ-0001:holding"]["status"] == "failed"
    fake_tg.send_message = real
    assert run(s.drain_queue()) == []                      # still inside the wait
    clock.advance(hours=1, seconds=1)
    out = run(s.drain_queue())
    assert [x.status for x in out] == [SendStatus.sent] and not s.queue


def test_peer_flood_stops_all_telegram_sends(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    fake_tg.raise_on_send = "PeerFlood"
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.failed and db.get_cursor("tg_send_blocked") == "1"
    assert cards(bus, CardKind.circuit_breaker)
    fake_tg.raise_on_send = None
    r2 = run(s.send(hold_msg(step="holding-2"), SendTrigger.holding_floor))
    assert r2.guard.blocked_by == ["tg_send_blocked"] and not fake_tg.sent


def test_network_drop_is_maybe_sent_and_never_blind_retried(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    fake_tg.raise_on_send = "NetworkDrop"
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.maybe_sent
    fake_tg.raise_on_send = None
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.duplicate
    assert not fake_tg.sent


def test_live_read_failure_blocks(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, _ = make_sender(cfg, clock, bus, fake_tg, tmp_home)

    async def broken(*a, **k):
        raise ConnectionError("down")
    fake_tg.read_thread = broken
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.guard.blocked_by == ["live_read"] and not fake_tg.sent


def test_email_send_via_gmail_draft(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    gmail = fakes.FakeGmail()
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, gmail=gmail, dial=Dial.draft)
    text = "Thanks, we are checking this. - Principal's assistant"
    msg = OutboundMessage(request_id="REQ-0001", step="email-1", kind=OutKind.email_reply, channel=Channel.email,
                          text=text, draft_hash=text_hash(text), to=["staff@example.org"], subject="Re: stock")
    nonce = db.issue_approval("REQ-0001", 1, "ap", fakes.PRINCIPAL_ID, text_hash(text), clock.now() + timedelta(hours=1))
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    # 2026-11-11: client email is drafts only: the reply is saved as a Gmail draft and NEVER sent by the twin
    assert r.status == SendStatus.drafted and len(gmail.sent) == 0 and len(gmail.drafts) == 1
    assert r.remote_id.startswith("draft:")


def test_email_can_still_send_when_draft_only_is_switched_off(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    gmail = fakes.FakeGmail()
    cfg.setdefault("sender", {})["email_draft_only"] = False
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, gmail=gmail, dial=Dial.draft)
    text = "Thanks, we are checking this. - Principal's assistant"
    msg = OutboundMessage(request_id="REQ-0001", step="email-1", kind=OutKind.email_reply, channel=Channel.email,
                          text=text, draft_hash=text_hash(text), to=["staff@example.org"], subject="Re: stock")
    nonce = db.issue_approval("REQ-0001", 1, "ap", fakes.PRINCIPAL_ID, text_hash(text), clock.now() + timedelta(hours=1))
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent and len(gmail.sent) == 1 and r.remote_id == "m1"


def test_zero_sends_in_default_dry_run_config(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    """Default config (dry_run, send disabled, observe): nothing ever reaches an adapter."""
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, live=False, dial=Dial.observe)
    for i, trig in enumerate([SendTrigger.holding_floor, SendTrigger.timeout]):
        run(s.send(hold_msg(step=f"h{i}"), trig))
    assert not fake_tg.sent and all(r["status"] != "sent" for r in db.sends.values())


def test_with_real_statedb_when_available(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    from twin_agent.db import StateDB
    try:
        db = StateDB(paths.state_db(), clock)
    except NotImplementedError:
        pytest.skip("db.py not shipped yet")
    db.set_dial(Lane.L1_holding, Dial.auto_on_timeout, by="test")
    c = copy.deepcopy(cfg)
    c["run_mode"], c["send_enabled"] = "live", True
    s = Sender(db, c, {Channel.telegram_group: fake_tg}, clock, bus, AuditLog(tmp_home / "a.jsonl", clock))
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.sent
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.duplicate
    assert len(fake_tg.sent) == 1
    db.close()


def test_real_guards_default_config_gives_would_send(cfg, clock, bus, fake_tg, tmp_home):
    """Default config with the real guards: a clean holding reply is a would_send, never a send."""
    from twin_agent.db import StateDB
    try:
        db = StateDB(paths.state_db(), clock)
    except NotImplementedError:
        pytest.skip("db.py not shipped yet")
    db.set_dial(Lane.L1_holding, Dial.observe, by="test")
    s = Sender(db, copy.deepcopy(cfg), {Channel.telegram_group: fake_tg}, clock, bus, AuditLog(tmp_home / "a.jsonl", clock))
    text = "Got it, checking this now.\n" + cfg["persona"]["marker"]
    r = run(s.send(hold_msg(text=text), SendTrigger.holding_floor))
    assert r.status == SendStatus.would_send, r
    assert not fake_tg.sent
    db.close()


def test_approval_already_consumed_by_bot_press_sends_once(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    nonce = _approve(db, clock)
    assert db.use_approval(nonce, fakes.PRINCIPAL_ID, "ap", "REQ-0001")          # bot_controller on the press
    r = run(s.send(answer_msg(clock), SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent
    r2 = run(s.send(answer_msg(clock, step="draft-2"), SendTrigger.approval, approval_nonce=nonce,
                    approver_user_id=fakes.PRINCIPAL_ID))
    assert r2.guard.blocked_by == ["approval_binding"] and len(fake_tg.sent) == 1


def test_bot_consumed_approval_too_old_is_refused(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    nonce = _approve(db, clock)
    db.use_approval(nonce, fakes.PRINCIPAL_ID, "ap", "REQ-0001")
    clock.advance(hours=2)
    r = run(s.send(answer_msg(clock), SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.guard.blocked_by == ["approval_binding"] and not fake_tg.sent


def test_real_statedb_bot_consumed_approval(cfg, clock, bus, fake_tg, tmp_home, monkeypatch):
    from twin_agent.db import StateDB
    try:
        db = StateDB(paths.state_db(), clock)
    except NotImplementedError:
        pytest.skip("db.py not shipped yet")
    monkeypatch.setattr(sender_mod.guards, "check_outbound", lambda m, c: GuardResult(True, [], []))
    db.set_dial(Lane.L3_t1_answer, Dial.draft, by="test")
    c = copy.deepcopy(cfg)
    c["run_mode"], c["send_enabled"] = "live", True
    s = Sender(db, c, {Channel.telegram_group: fake_tg}, clock, bus, AuditLog(tmp_home / "a.jsonl", clock))
    nonce = db.issue_approval("REQ-0001", 1, "ap", fakes.PRINCIPAL_ID, text_hash(ANSWER), clock.now() + timedelta(hours=24))
    assert db.use_approval(nonce, fakes.PRINCIPAL_ID, "ap", "REQ-0001")
    r = run(s.send(answer_msg(clock), SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent, r
    db.close()


def test_principal_answering_someone_else_in_topic_is_not_answered(cfg, clock, bus, fake_tg, tmp_home, guard_ok, make_event):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    fake_tg.thread.append(make_event("done", msg_id=6, sender=fakes.PRINCIPAL, ts=clock.now() - timedelta(minutes=20),
                                     reply_to=4))
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).status == SendStatus.sent


def test_router_marked_principal_first_reply_blocks_autopilot(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    db.requests["REQ-0001"]["first_reply_by"] = "principal"
    assert run(s.send(hold_msg(), SendTrigger.holding_floor)).guard.blocked_by == ["already_answered"]


def test_telegram_without_reply_to_is_blocked(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    s, _ = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    r = run(s.send(hold_msg(reply_to=None), SendTrigger.holding_floor))
    assert r.guard.blocked_by == ["thread_binding"] and not fake_tg.sent


def test_real_statedb_own_send_is_not_principal_activity(cfg, clock, bus, fake_tg, tmp_home, monkeypatch, make_event):
    from twin_agent.db import StateDB
    try:
        db = StateDB(paths.state_db(), clock)
    except NotImplementedError:
        pytest.skip("db.py not shipped yet")
    monkeypatch.setattr(sender_mod.guards, "check_outbound", lambda m, c: GuardResult(True, [], []))
    for lane in (Lane.L1_holding, Lane.L3_t1_answer):
        db.set_dial(lane, Dial.auto_on_timeout, by="test")
    c = copy.deepcopy(cfg)
    c["run_mode"], c["send_enabled"] = "live", True
    s = Sender(db, c, {Channel.telegram_group: fake_tg}, clock, bus, AuditLog(tmp_home / "a.jsonl", clock))
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.sent
    fake_tg.thread.append(make_event(HOLD, msg_id=int(r.remote_id), sender=fakes.PRINCIPAL, ts=clock.now(), reply_to=5))
    clock.advance(minutes=1)
    assert run(s.send(answer_msg(clock), SendTrigger.timeout)).status == SendStatus.sent
    db.close()


def test_identity_guard_wired_for_owner_requester(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    """Regression (dead guard): the sender must set requester_is_bot_or_staff_owner on the GuardContext, so an
    autonomous reply to Principal / a backup / an owner is card-only. Before the fix the field defaulted to False
    and the identity guard was never fed."""
    cfg["people"]["principal_tg_user_id"] = int(fakes.PRINCIPAL_ID)
    snd, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    db.requests["REQ-0001"]["requester_id"] = str(fakes.PRINCIPAL_ID)      # the requester IS Principal (an owner)
    run(snd.send(hold_msg(), SendTrigger.holding_floor))
    assert guard_ok[-1].requester_is_bot_or_staff_owner is True


# ---------------------------------------------------------------- anonymous admin (Principal 2026-11-09)

def test_anonymous_ok_accepts_only_posts_shown_as_that_same_group():
    from twin_agent.sender import anonymous_ok, is_the_chat
    cfg = {"chats": [{"chat_id": -1009000000001, "anonymous_ok": True}, {"chat_id": -1009000000002}]}
    assert anonymous_ok(cfg, "-1009000000001") and not anonymous_ok(cfg, -1009000000002) and not anonymous_ok(cfg, 5)
    assert is_the_chat(-9000000001, -1009000000001) and is_the_chat(-1009000000001, "-1009000000001")
    assert not is_the_chat(9000000003, -1009000000001) and not is_the_chat(-9000000002, -1009000000001)
    assert not is_the_chat(None, -1009000000001)


def test_anonymous_ok_chat_keeps_a_post_shown_as_that_group(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    """With chats[].anonymous_ok the same anonymous-group post is KEPT (sent, no delete, no demotion); without it
    (the test above) it is deleted. A post shown as some other account is still a wrong identity."""
    for c in cfg["chats"]:
        c["anonymous_ok"] = True
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    msg = hold_msg()
    fake_tg.from_id = int(msg.chat_id)                     # Telegram shows THIS group as the author
    r = run(s.send(msg, SendTrigger.holding_floor))
    assert r.status == SendStatus.sent and fake_tg.deleted == [] and not cards(bus, CardKind.demotion)


def test_anonymous_ok_still_deletes_a_post_shown_as_someone_else(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    for c in cfg["chats"]:
        c["anonymous_ok"] = True
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home)
    fake_tg.from_id = 987654321                            # a different user account
    r = run(s.send(hold_msg(), SendTrigger.holding_floor))
    assert r.status == SendStatus.failed and fake_tg.deleted


def test_principal_tap_sends_despite_a_new_requester_message(cfg, clock, bus, fake_tg, tmp_home, guard_ok, make_event):
    """sender.principal_tap_overrides_stale (Principal 2026-11-09): HIS Approve sends even after the requester wrote again
    (the real case was a lone '؟'); he gets a card with what came in. Off by default (test above)."""
    cfg.setdefault("sender", {})["principal_tap_overrides_stale"] = True
    s, db = make_sender(cfg, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    fake_tg.thread.append(make_event("؟", msg_id=6, ts=clock.now() - timedelta(minutes=1)))
    nonce = _approve(db, clock)
    r = run(s.send(answer_msg(clock), SendTrigger.approval, approval_nonce=nonce, approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent and len(fake_tg.sent) == 1
    assert any("sent on your Approve" in c.text for c in cards(bus, CardKind.info))


def test_principal_tap_override_never_covers_autonomous_sends():
    from twin_agent.sender import principal_tap_overrides_stale
    cfg = {"sender": {"principal_tap_overrides_stale": True}}
    assert principal_tap_overrides_stale(cfg, SendTrigger.approval) and principal_tap_overrides_stale(cfg, SendTrigger.principal_edit)
    assert not principal_tap_overrides_stale(cfg, SendTrigger.timeout)
    assert not principal_tap_overrides_stale(cfg, SendTrigger.holding_floor)
    assert not principal_tap_overrides_stale({}, SendTrigger.approval)
