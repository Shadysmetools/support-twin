"""Guards against the real StateDB (CORE) and the real persona templates (KB), not just the test stand-in."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from twin_agent.checks.guards import GuardContext, check_outbound, draft_hash
from twin_agent.checks.guards_selftest import PRINCIPAL, CHAT, MARKER, TOPIC, live_cfg, msg
from twin_agent.models import Channel, Dial, Lane, OutKind, SendStatus, SendTrigger
from twin_agent.testing.fakes import make_event

db_mod = pytest.importorskip("twin_agent.db")


@pytest.fixture
def sdb(tmp_home, clock):
    cfg = live_cfg()
    d = db_mod.StateDB(tmp_home / "data" / "state" / "state.db", clock, cfg)
    rid = d.create_request(make_event("WAVE/00001", chat_id=CHAT, topic_id=TOPIC))
    yield d, rid, cfg, clock
    d.close()


def gctx(cfg, d, clock, **kw):
    return GuardContext(cfg=cfg, db=d, now=clock.now(), trigger=kw.pop("trigger", SendTrigger.holding_floor),
                        kill_switch_on=False, **kw)


def test_clean_holding_passes_on_real_db(sdb):
    d, rid, cfg, clock = sdb
    r = check_outbound(msg(f"Received, checking it now.\n{MARKER}", request_id=rid), gctx(cfg, d, clock))
    assert r.ok, r.warnings


def test_rate_and_holding_once_on_real_db(sdb):
    d, rid, cfg, clock = sdb
    hold = msg(f"Received, checking it now.\n{MARKER}", request_id=rid)
    assert d.claim_send(hold, SendTrigger.holding_floor)
    d.finish_send(hold.send_key, SendStatus.sent, msg=hold)
    clock.advance(minutes=1)
    r = check_outbound(msg(f"Received.\n{MARKER}", request_id=rid, step="holding-2"), gctx(cfg, d, clock))
    assert "holding_once" in r.blocked_by and "rate_per_request_gap" in r.blocked_by
    assert not any("guard error" in w for w in r.warnings), r.warnings
    for i in range(10):                                  # fill the chat's hourly cap with other requests
        m = msg("x", request_id=f"REQ-9{i:03d}", step="a", kind=OutKind.answer)
        d.claim_send(m, SendTrigger.approval)
        d.finish_send(m.send_key, SendStatus.sent, msg=m)
    r = check_outbound(msg(f"Try again now.\n{MARKER}", request_id=rid, step="a9", kind=OutKind.answer),
                       gctx(cfg, d, clock, trigger=SendTrigger.timeout))
    assert "rate_per_chat_hour" in r.blocked_by


def test_db_dial_change_wins(sdb):
    d, rid, cfg, clock = sdb
    cfg["dial"]["L3_t1_answer"] = "observe"
    text = "Try again now."
    m = msg(text, request_id=rid, step="d1", kind=OutKind.answer, draft_hash=draft_hash(text))
    appr = {"request_id": rid, "action": "ap", "draft_hash": draft_hash(text), "allowed_user_id": PRINCIPAL,
            "used_by": PRINCIPAL}
    c = gctx(cfg, d, clock, trigger=SendTrigger.approval, approval=appr)
    assert "dial" in check_outbound(m, c).blocked_by
    d.set_dial(Lane.L3_t1_answer, Dial.draft, by="principal")          # /auto L3 draft
    assert check_outbound(m, c).ok


persona = pytest.importorskip("twin_agent.persona")


@pytest.mark.parametrize("trigger", [SendTrigger.holding_floor, SendTrigger.approval])
def test_real_holding_templates_pass(sdb, trigger):
    d, rid, cfg, clock = sdb
    tg = [persona.holding_telegram(in_hours=False), persona.holding_telegram(in_hours=True),
          persona.holding_telegram(in_hours=True, has_screenshot=False, has_ref=False),
          persona.fully_blocked_telegram(card_delivered=True),
          persona.ticket_chase_telegram("https://helpdesk.example.com/my/ticket/2641/abcdefgh1234")]
    when = date(2026, 11, 3)
    filled = persona.format_update_date(when)
    mails = [(persona.email_holding(name="Staff", restatement="the receipt validation", update_date=when), filled),
             (persona.email_approver(name="Staff", approver=next(iter(persona.APPROVERS.values()))), None)]
    cases = [msg(t, request_id=rid, is_template=True) for t in tg]
    cases += [msg(t, request_id=rid, kind=OutKind.email_holding, channel=Channel.email, in_reply_to="<m1>",
                  chat_id=None, topic_id=None, is_template=True, filled_date=f) for t, f in mails]
    for i, m in enumerate(cases):
        m.step = f"h{i}"
        kw = {}
        if trigger == SendTrigger.approval:
            m.draft_hash = draft_hash(m.text)
            kw["approval"] = {"request_id": rid, "action": "ap", "draft_hash": m.draft_hash,
                              "allowed_user_id": PRINCIPAL, "used_by": PRINCIPAL}
        r = check_outbound(m, gctx(cfg, d, clock, trigger=trigger, **kw))
        assert r.ok, (m.text, r.blocked_by, r.warnings)
