"""router.py: refs, company, reopen words, burst merge, dedupe, Principal replies, reopen, classification."""
import asyncio
from datetime import timedelta

import pytest

from twin_agent import paths, router, timers
from twin_agent.bus import Topics
from twin_agent.db import StateDB
from twin_agent.models import Channel, Company, Lane, State, Tier, parse_ts
from twin_agent.testing import fakes
from twin_agent.testing.fakes import PRINCIPAL, BOT_SENDER, STAFF_A, STAFF_B, make_event


@pytest.fixture
def env(tmp_home, clock, cfg, bus):
    db = StateDB(paths.state_db(), clock, cfg)
    pol = timers.TimerPolicy(cfg)
    r = router.Router(db, cfg, pol, bus, clock)
    yield db, r, pol
    db.close()


def ev_at(clock, text, msg_id, **kw):
    return make_event(text, msg_id=msg_id, ts=clock.now(), **kw)


def run(coro):
    return asyncio.run(coro)


def test_extract_refs():
    t = "WAVE/01234 and IN/10001/26, SO10001 also DLV/10001/26 and L1-27/06-BATCH/09766 PO10001 stock.move 5"
    assert router.extract_refs(t) == ["WAVE/01234", "IN/10001/26", "SO10001", "DLV/10001/26",
                                      "L1-27/06-BATCH/09766", "PO10001", "stock.move 5"]
    assert router.extract_refs("call me at 3pm, no refs") == []


@pytest.mark.parametrize("text", ["still the same", "Same error", "??", "لسه", "ولسه مش شغال", "لسة",
                                  "lessa", "lesa msh sha8al", "نفس المشكلة", "it happened again", "؟؟"])
def test_reopen_words(text):
    assert router.is_reopen_text(text)


@pytest.mark.parametrize("text", ["Thanks, works now", "please try again now", "WAVE/00001", "", "stillwater"])
def test_not_reopen_words(text):
    assert not router.is_reopen_text(text)


def test_company_hint(cfg):
    assert router.company_hint(make_event("WAVE/1"), cfg) == Company.EG
    assert router.company_hint(make_event("KSA branch cannot validate"), cfg) == Company.KSA
    assert router.company_hint(make_event("فرع السعودية"), cfg) == Company.KSA


def test_classify_text():
    assert router.classify_text("how do I print the label?", has_refs=False, channel="telegram_group") == \
        (Lane.L3_t1_answer, Tier.T1)
    assert router.classify_text("cannot validate", has_refs=True, channel="telegram_group")[1] == Tier.T2a
    assert router.classify_text("", has_refs=True, channel="telegram_group")[1] == Tier.T2a      # photo + ref
    assert router.classify_text("need access to inventory menu", has_refs=False, channel="x")[1] == Tier.T2b
    assert router.classify_text("please approve the discount", has_refs=False, channel="x")[1] == Tier.T4
    assert router.classify_text("hello", has_refs=False, channel="email") == (Lane.L12_email_reply, Tier.UNKNOWN)


def test_burst_merge_and_close(env, clock, bus):
    db, r, _ = env

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001", 1))
        clock.advance(minutes=2)
        assert await r.on_event(ev_at(clock, "", 2, photo=True)) == rid             # photo joins the burst
        assert await r.on_event(ev_at(clock, "other person", 3, sender=STAFF_B)) != rid
        burst = db.timers_of(rid, "burst_close")
        pending = [t for t in burst if not t["cancelled_at"]]
        assert len(pending) == 1 and parse_ts(pending[0]["due_at"]) == clock.now() + timedelta(minutes=3)
        clock.advance(minutes=3)
        for t in timers.tick(db, r.policy, clock.now(), bus):
            if t["kind"] == "burst_close":
                await r.close_burst(t["request_id"])
        req = db.get_request(rid)
        assert req["state"] == "TRIAGED" and req["tier"] == "T2a" and req["company"] == "EG"
        assert len(db.thread(rid)) == 2
        assert {"holding", "reminder", "escalate_backup"} <= {t["kind"] for t in db.future_timers(rid)}
        assert (Topics.REQUEST_TRIAGED, {"request_id": rid}) in bus.published
    run(go())


@pytest.mark.parametrize("hour, target", [(11, 60), (23, 15)])     # working hours / out of hours (Cairo)
def test_holding_and_reminder_run_from_the_first_message_not_the_burst_close(tmp_home, cfg, bus, hour, target):
    clock = fakes.FakeClock(fakes.FakeClock().now().replace(hour=hour, minute=0))
    db = StateDB(paths.state_db(), clock, cfg)
    pol = timers.TimerPolicy(cfg)
    r = router.Router(db, cfg, pol, bus, clock)
    assert pol.is_working_time(clock.now()) == (target == 60)

    async def go():
        first = clock.now()
        rid = await r.on_event(ev_at(clock, "WAVE/00001 cannot validate", 1))
        clock.advance(minutes=2)
        await r.on_event(ev_at(clock, "still stuck", 2))                    # joins the burst
        clock.advance(minutes=3)                                            # burst closes 5 min after the first
        for t in timers.tick(db, pol, clock.now(), bus):
            if t["kind"] == "burst_close":
                await r.close_burst(t["request_id"])
        due = {t["kind"]: parse_ts(t["due_at"]) for t in db.future_timers(rid)}
        assert due["holding"] == first + timedelta(minutes=target)          # not first + 5 + target
        assert due["reminder"] == pol.due_at(timers.TimerKind.reminder, first)
        assert due["reminder"] < due["holding"]
    try:
        run(go())
    finally:
        db.close()


def test_long_burst_past_the_holding_target_fires_holding_at_once(tmp_home, cfg, bus):
    """Out of hours a 30-min burst outlasts the 15-min target: the holding timer is due now, never later."""
    clock = fakes.FakeClock(fakes.FakeClock().now().replace(hour=23, minute=0))
    db = StateDB(paths.state_db(), clock, cfg)
    pol = timers.TimerPolicy(cfg)
    r = router.Router(db, cfg, pol, bus, clock)

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001", 1))
        for i in range(2, 15):
            clock.advance(minutes=2.5)
            await r.on_event(ev_at(clock, f"more {i}", i))
        clock.advance(minutes=3)
        await r.close_burst(rid)
        fired = {t["kind"] for t in timers.tick(db, pol, clock.now(), bus)}
        assert "holding" in fired
    try:
        run(go())
    finally:
        db.close()


def test_burst_cap_30_min(env, clock):
    db, r, _ = env

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001", 1))
        start = clock.now()
        for i in range(2, 15):
            clock.advance(minutes=2.5)
            await r.on_event(ev_at(clock, f"more {i}", i))
        due = [parse_ts(t["due_at"]) for t in db.timers_of(rid, "burst_close") if not t["cancelled_at"]]
        assert due == [max(clock.now(), start + timedelta(minutes=30))]
    run(go())


def test_dedupe_and_edit(env, clock, bus):
    db, r, _ = env

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001", 1))
        again = ev_at(clock, "WAVE/00001", 1)
        again.found_by_sweep = True
        assert await r.on_event(again) == rid
        assert await r.on_event(ev_at(clock, "WAVE/00002 fixed typo", 1, edited=True)) == rid
        assert len(db.open_requests()) == 1
        assert db.thread(rid)[0]["text"] == "WAVE/00002 fixed typo"
    run(go())


def test_principal_reply_never_opens_and_cancels_reply_timers(env, clock):
    db, r, _ = env

    async def go():
        assert await r.on_event(ev_at(clock, "hello", 1, sender=PRINCIPAL)) is None
        assert db.open_requests() == []
        rid = await r.on_event(ev_at(clock, "WAVE/00001 stuck", 2))
        await r.close_burst(rid)
        clock.advance(minutes=5)
        assert await r.on_event(ev_at(clock, "checking", 3, sender=PRINCIPAL, reply_to=2)) == rid
        req = db.get_request(rid)
        assert req["first_reply_by"] == "principal" and req["state"] == "ACKED"
        kinds = {t["kind"] for t in db.future_timers(rid)}
        assert not kinds & {"holding", "reminder", "verified_answer"} and kinds
        assert db.get_cursor(f"principal_active:{fakes.GROUP_ID}")
    run(go())


def test_bot_and_ticket_mail_never_open(env, clock):
    db, r, _ = env

    async def go():
        assert await r.on_event(ev_at(clock, "WAVE/1", 1, sender=BOT_SENDER)) is None
        assert await r.on_event(ev_at(clock, "ack ref: REQ-0001", 2, channel=Channel.ticket_mail)) is None
        assert db.open_requests() == []
    run(go())


def test_reopen_closed_request(env, clock, bus):
    db, r, _ = env

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001 cannot validate", 1))
        await r.close_burst(rid)
        for s in (State.DIAGNOSED, State.PROOF_SENT, State.CLOSED_PROVEN):
            db.transition(rid, s, actor="t")
        clock.advance(hours=20)
        assert await r.on_event(ev_at(clock, "لسه", 9)) == rid
        req = db.get_request(rid)
        assert req["state"] == "TRIAGED" and req["first_reply_at"] is None
        assert db.kpi_sum("reopen_48h", request_id=rid) == 1
        assert db.future_timers(rid)
    run(go())


def test_old_closed_request_is_not_reopened(env, clock):
    db, r, _ = env

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001", 1))
        db.transition(rid, State.CLOSED_BY_PRINCIPAL, actor="principal", reason="done")
        clock.advance(days=20)
        new = await r.on_event(ev_at(clock, "still", 2))
        assert new != rid and db.get_request(new)["state"] == "NEW"
    run(go())


def test_needs_info_answer_retriages(env, clock, bus):
    db, r, _ = env

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001", 1))
        await r.close_burst(rid)
        db.transition(rid, State.NEEDS_INFO, actor="t")
        timers.schedule_for_state(db, r.policy, rid, State.NEEDS_INFO, clock.now())
        clock.advance(hours=3)
        assert await r.on_event(ev_at(clock, "here is the screenshot", 5, photo=True)) == rid
        assert db.get_request(rid)["state"] == "TRIAGED"
        assert not {"evidence_chase_1", "evidence_chase_2"} & {t["kind"] for t in db.future_timers(rid)}
    run(go())


def test_classifier_hook_and_fallback(env, clock):
    db, r, _ = env
    r.classifier = lambda req, thread: {"lane": "L8_approver_question", "tier": "T4"}

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001", 1))
        await r.close_burst(rid)
        assert db.get_request(rid)["tier"] == "T4"
        r.classifier = lambda req, thread: 1 / 0
        rid2 = await r.on_event(ev_at(clock, "how to print", 2, sender=STAFF_B))
        await r.close_burst(rid2)
        assert db.get_request(rid2)["tier"] == "T1"
    run(go())


def test_today_only_old_thread_message_opens_a_fresh_request(env, clock, bus, cfg):
    """router.today_only (Principal 2026-11-09, widened 2026-11-11): a reply in a thread whose request was active or
    closed within reopen_days (7) goes to the SAME request; an older thread never reopens: a NEW request."""
    db, r, _ = env
    r.today_only = True
    r.reopen_days = 7

    async def go():
        old = await r.on_event(ev_at(clock, "WAVE/00001 cannot validate", 1))
        await r.close_burst(old)
        for s in (State.DIAGNOSED, State.PROOF_SENT, State.CLOSED_PROVEN):
            db.transition(old, s, actor="t")
        clock.advance(days=8)                            # older than the window
        new = await r.on_event(ev_at(clock, "لسه", 9, reply_to=1))
        assert new != old and db.get_request(old)["state"] == "CLOSED_PROVEN"
        assert db.get_request(new)["root_msg_id"] == "9"
        assert db._all("SELECT * FROM events WHERE kind = 'old_thread_new_request'")
        # the next day, inside the window: the same request reopens
        await r.close_burst(new)
        for s in (State.DIAGNOSED, State.PROOF_SENT, State.CLOSED_PROVEN):
            db.transition(new, s, actor="t")
        clock.advance(hours=30)
        assert await r.on_event(ev_at(clock, "لسه", 10, reply_to=9)) == new
    run(go())


def test_reply_after_approve_close_reopens_but_thanks_does_not(env, clock, bus, cfg):
    """2026-11-11: Approve closes (CLOSED_BY_PRINCIPAL). Any follow-up in the thread reopens it and publishes
    REQUEST_REOPENED (the helpdesk reopens the ticket); a bare thanks does not."""
    from twin_agent.bus import Topics
    db, r, _ = env
    r.today_only = True
    r.reopen_days = 7
    reopened, ids = [], []

    async def on_reopened(p):
        reopened.append(p["request_id"])
    bus.subscribe(Topics.REQUEST_REOPENED, on_reopened)

    async def go():
        rid = await r.on_event(ev_at(clock, "WAVE/00001 cannot validate", 1))
        ids.append(rid)
        await r.close_burst(rid)
        db.transition(rid, State.CLOSED_BY_PRINCIPAL, actor="t")
        clock.advance(hours=20)
        await r.on_event(ev_at(clock, "تمام", 2, reply_to=1))
        assert db.get_request(rid)["state"] == "CLOSED_BY_PRINCIPAL"            # a thanks never reopens
        assert await r.on_event(ev_at(clock, "the chair is in the warehouse", 3, reply_to=1)) == rid
        assert db.get_request(rid)["state"] == "TRIAGED"
        await bus.drain()
    run(go())
    assert reopened == ids
