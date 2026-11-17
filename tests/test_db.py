"""db.py: schema, requests, transitions, messages, timers, approvals, sends, dials."""
from datetime import timedelta

import pytest

from twin_agent import paths
from twin_agent.db import StateDB
from twin_agent.models import (Card, CardKind, Channel, Dial, IllegalTransition, Lane, OutboundMessage, OutKind,
                                SendStatus, SendTrigger, State, parse_ts)
from twin_agent.testing import fakes


@pytest.fixture
def db(tmp_home, clock, cfg):
    d = StateDB(paths.state_db(), clock, cfg)
    yield d
    d.close()


def test_create_and_ids(db, make_event):
    r1 = db.create_request(make_event("WAVE/00001", msg_id=1))
    r2 = db.create_request(make_event("photo", msg_id=2))
    assert (r1, r2) == ("REQ-0001", "REQ-0002")
    req = db.get_request(r1)
    assert req["state"] == "NEW" and req["chat_id"] == str(fakes.GROUP_ID) and req["requester_id"] == "2001"
    assert req["created_at"].endswith("+00:00")                  # stored as UTC
    assert parse_ts(req["created_at"]).hour == 14                # read back as Cairo
    assert [r["id"] for r in db.open_requests()] == [r1, r2]


def test_transitions_and_overlays(db, make_event):
    rid = db.create_request(make_event())
    seen = []
    db.on_state_change = lambda r, s, d: seen.append((s, d))
    db.transition(rid, State.TRIAGED, actor="router")
    with pytest.raises(IllegalTransition):
        db.transition(rid, State.PROOF_SENT, actor="x")
    db.transition(rid, State.PAUSED, actor="principal")
    db.transition(rid, State.STALLED, actor="timers")               # overlay to overlay keeps prev_state
    assert db.get_request(rid)["prev_state"] == "TRIAGED"
    with pytest.raises(IllegalTransition):
        db.transition(rid, State.DIAGNOSED, actor="x")              # can only go back to prev_state
    db.transition(rid, State.TRIAGED, actor="principal")
    assert db.get_request(rid)["prev_state"] is None
    assert seen[-1] == ("STALLED", "TRIAGED")
    assert len(db.events(rid, "state")) == 4


def test_reread_proven_needs_config(db, make_event, cfg):
    rid = db.create_request(make_event())
    for s in (State.TRIAGED, State.DIAGNOSED, State.PROOF_SENT):
        db.transition(rid, s, actor="t")
    with pytest.raises(IllegalTransition):
        db.transition(rid, State.CLOSED_REREAD_PROVEN, actor="t")
    cfg["close_rules"]["reread_proven_accepted"] = True
    db.transition(rid, State.CLOSED_REREAD_PROVEN, actor="t")
    assert db.get_request(rid)["closed_at"]


def test_reopen_window(db, make_event, clock):
    rid = db.create_request(make_event())
    db.transition(rid, State.CLOSED_BY_PRINCIPAL, actor="principal", reason="dup")
    assert db.get_request(rid)["close_reason"] == "dup"
    clock.advance(days=15)
    with pytest.raises(IllegalTransition):
        db.transition(rid, State.REOPENED, actor="router")
    rid2 = db.create_request(make_event(msg_id=5))
    db.transition(rid2, State.CLOSED_BY_PRINCIPAL, actor="principal")
    clock.advance(days=13)
    db.transition(rid2, State.REOPENED, actor="router")


def test_messages_dedupe_and_edit(db, make_event):
    rid = db.create_request(make_event())
    assert db.add_message(make_event("first", msg_id=1), rid) is True
    assert db.add_message(make_event("first", msg_id=1), rid) is False
    assert db.add_message(make_event("edited text", msg_id=1, edited=True), rid) is False
    t = db.thread(rid)
    assert len(t) == 1 and t[0]["text"] == "edited text" and t[0]["edited_at"]


def test_timers(db, clock, make_event):
    rid = db.create_request(make_event())
    now = clock.now()
    t1 = db.add_timer(rid, "reminder", now + timedelta(minutes=40))
    t2 = db.add_timer(rid, "holding", now + timedelta(minutes=60), {"x": 1})
    assert db.due_timers(now) == []
    clock.advance(minutes=45)
    assert [t["id"] for t in db.due_timers(clock.now())] == [t1]
    db.mark_timer(t1, fired=True)
    assert [t["id"] for t in db.future_timers(rid)] == [t2]
    assert db.cancel_timers(rid, ["holding"]) == 1
    assert db.future_timers(rid) == []


def test_approvals_one_time_and_principal_only(db, clock, make_event):
    rid = db.create_request(make_event())
    card_id = db.add_card(Card(kind=CardKind.new_request, text="x", request_id=rid))
    exp = clock.now() + timedelta(hours=24)
    nonce = db.issue_approval(rid, card_id, "ap", fakes.PRINCIPAL_ID, "h1", exp)
    assert len(nonce) == 11
    assert db.use_approval(nonce, fakes.BACKUP_ID, "ap", rid) is None          # backup cannot approve
    assert db.use_approval(nonce, fakes.PRINCIPAL_ID, "sd", rid) is None           # wrong action
    assert db.use_approval(nonce, fakes.PRINCIPAL_ID, "ap", "REQ-9999") is None    # wrong request
    row = db.use_approval(nonce, fakes.PRINCIPAL_ID, "ap", rid)
    assert row and row["used_by"] == fakes.PRINCIPAL_ID
    assert db.use_approval(nonce, fakes.PRINCIPAL_ID, "ap", rid) is None           # reused
    n2 = db.issue_approval(rid, card_id, "ap", fakes.PRINCIPAL_ID, "h1", exp)
    clock.advance(hours=25)
    assert db.use_approval(n2, fakes.PRINCIPAL_ID, "ap", rid) is None              # expired


def _msg(step="holding"):
    return OutboundMessage(request_id="REQ-0001", step=step, kind=OutKind.holding, channel=Channel.telegram_group,
                           text="Checking it", chat_id=fakes.GROUP_ID, topic_id=7)


def test_send_keys(db, clock):
    m = _msg()
    db.finish_send(m.send_key, SendStatus.would_send, msg=m, trigger=SendTrigger.holding_floor)   # no claim first
    assert db.get_send(m.send_key)["status"] == "would_send"
    assert db.claim_send(m, SendTrigger.holding_floor) is True        # would_send does not burn the key
    assert db.claim_send(m, SendTrigger.holding_floor) is False       # pending burns it
    db.finish_send(m.send_key, SendStatus.sent, remote_id="10001", from_id_ok=True)
    row = db.get_send(m.send_key)
    assert row["status"] == "sent" and row["sent_at"] and row["lane"] == "L1_holding"
    assert db.claim_send(m, SendTrigger.holding_floor) is False
    assert len(db.sends_since(clock.now() - timedelta(hours=1), chat_id=fakes.GROUP_ID)) == 1
    assert db.sends_since(clock.now() - timedelta(hours=1), chat_id=123) == []
    b = _msg("draft-1")
    db.finish_send(b.send_key, SendStatus.blocked, msg=b, guard={"ok": False, "blocked_by": ["kill_switch"]})
    assert db.claim_send(b, SendTrigger.approval) is True             # blocked does not burn either
    assert len(db.sends_since(clock.now() - timedelta(hours=1), request_id="REQ-0001")) == 1   # only 'sent'


def test_dials_cursors_kill(db, cfg):
    assert db.get_dial(Lane.L11_internal_diagnosis) == Dial.auto      # from config
    db.set_dial(Lane.L3_t1_answer, Dial.draft, by="principal")
    assert db.get_dial(Lane.L3_t1_answer) == Dial.draft
    assert db.get_cursor("tg:-100500") is None
    db.set_cursor("tg:-100500", "42")
    db.set_cursor("tg:-100500", "43")
    assert db.get_cursor("tg:-100500") == "43"
    assert not db.kill_on()
    db.set_kill(True, by="principal")
    assert db.kill_on()


def test_no_config_dial_is_off(tmp_home, clock):
    d = StateDB(tmp_home / "x.db", clock)
    try:
        assert d.get_dial(Lane.L1_holding) == Dial.off
    finally:
        d.close()


def test_log_event_redacts(db):
    db.log_event("note", None, token="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    detail = db.events(kind="note")[0]["detail_json"]
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in detail


def test_packets_and_kpis(db, make_event):
    rid = db.create_request(make_event())
    assert db.save_packet(rid, fakes.canned_packet(rid), False, []) == 1
    assert db.save_packet(rid, fakes.canned_packet(rid), True, [{"check": "judge"}]) == 2
    assert db.latest_packet(rid)["run_no"] == 2
    db.add_kpi("claude_call", 1, request_id=rid, kind="worker")
    db.add_kpi("claude_call", 1, request_id=rid, kind="judge")
    assert db.kpi_sum("claude_call", request_id=rid) == 2
