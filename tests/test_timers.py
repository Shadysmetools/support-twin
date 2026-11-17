"""timers.py: working-hours policy (Cairo, DST), the 05 2.3 table, the always-a-timer invariant, catch-up."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from twin_agent import paths, timers
from twin_agent.bus import Topics
from twin_agent.clock import CAIRO
from twin_agent.db import StateDB
from twin_agent.models import (TERMINAL, Channel, OutboundMessage, OutKind, SendStatus, SendTrigger, State,
                                TimerKind as K, parse_ts)

SUN_1440 = datetime(2026, 11, 1, 14, 40, tzinfo=CAIRO)     # Sunday, in hours
THU_2300 = datetime(2026, 11, 5, 23, 0, tzinfo=CAIRO)      # Thursday night, weekend next


@pytest.fixture
def policy(cfg):
    return timers.TimerPolicy(cfg)


@pytest.fixture
def db(tmp_home, clock, cfg):
    d = StateDB(paths.state_db(), clock, cfg)
    yield d
    d.close()


def test_working_and_quiet(policy):
    assert policy.is_working_time(SUN_1440)
    assert not policy.is_working_time(datetime(2026, 11, 6, 12, 0, tzinfo=CAIRO))     # Friday
    assert not policy.is_working_time(datetime(2026, 11, 1, 18, 0, tzinfo=CAIRO))     # end is exclusive
    assert policy.is_quiet_time(THU_2300) and policy.is_quiet_time(datetime(2026, 11, 2, 7, 59, tzinfo=CAIRO))
    assert not policy.is_quiet_time(SUN_1440)


def test_holidays_and_away(cfg):
    p = timers.TimerPolicy(cfg, holidays={date(2026, 11, 1)})
    assert not p.is_working_time(SUN_1440)
    p2 = timers.TimerPolicy(cfg, away_until=SUN_1440 + timedelta(hours=3))
    assert not p2.is_working_time(SUN_1440)
    assert p2.due_at(K.holding, SUN_1440) == SUN_1440 + timedelta(minutes=15)       # away = out of hours


def test_table_in_and_out_of_hours(policy):
    assert policy.due_at(K.reminder, SUN_1440) == SUN_1440 + timedelta(minutes=40)
    assert policy.due_at(K.holding, SUN_1440) == SUN_1440 + timedelta(minutes=60)
    assert policy.due_at(K.reminder, THU_2300) == THU_2300 + timedelta(minutes=10)
    assert policy.due_at(K.holding, THU_2300) == THU_2300 + timedelta(minutes=15)
    assert policy.due_at(K.verified_answer, THU_2300) == THU_2300 + timedelta(minutes=30)
    assert policy.due_at(K.email_holding, THU_2300) == THU_2300 + timedelta(minutes=120)
    # Escalation out of hours: next working day (Sunday) 10:15
    assert policy.due_at(K.escalate_backup, THU_2300) == datetime(2026, 11, 8, 10, 15, tzinfo=CAIRO)
    assert policy.due_at(K.escalate_backup, THU_2300, fully_blocked=True) == THU_2300 + timedelta(minutes=30)
    assert policy.due_at(K.escalate_backup, SUN_1440) == SUN_1440 + timedelta(hours=3)
    assert policy.due_at(K.worked_chaser_1, THU_2300) == datetime(2026, 11, 8, 10, 30, tzinfo=CAIRO)
    assert policy.due_at(K.approval_reping_1, THU_2300) == datetime(2026, 11, 8, 10, 15, tzinfo=CAIRO)
    assert policy.due_at(K.approval_expire, SUN_1440) == SUN_1440 + timedelta(hours=24)
    assert policy.due_at(K.sla_warning, SUN_1440) == SUN_1440 + timedelta(hours=36)
    assert policy.due_at(K.sla_warning, SUN_1440, channel="email") == SUN_1440 + timedelta(hours=126)
    assert policy.due_at(K.weekly_recheck, SUN_1440) == datetime(2026, 11, 8, 10, 30, tzinfo=CAIRO)
    assert policy.due_at(K.digest, SUN_1440) == datetime(2026, 11, 2, 9, 30, tzinfo=CAIRO)
    for kind in K:
        assert policy.due_at(kind, SUN_1440) >= SUN_1440 - timedelta(days=3)       # every kind has a rule


def test_working_hours_and_days(policy):
    # Thursday 16:00 + 4 working hours = Sunday 12:00 (2 h Thu, then 2 h Sun)
    thu = datetime(2026, 11, 5, 16, 0, tzinfo=CAIRO)
    assert policy.add_working_hours(thu, 4) == datetime(2026, 11, 8, 12, 0, tzinfo=CAIRO)
    assert policy.add_working_days(thu, 1) == datetime(2026, 11, 8, 16, 0, tzinfo=CAIRO)
    assert policy.add_working_days(datetime(2026, 11, 8, 10, 0, tzinfo=CAIRO), -1) == \
        datetime(2026, 11, 5, 10, 0, tzinfo=CAIRO)
    assert policy.due_at(K.ticket_idle, thu) == datetime(2026, 11, 10, 16, 0, tzinfo=CAIRO)


def _transition_around(year: int) -> datetime:
    """Find a moment just before a Cairo UTC-offset change (no hard-coded DST date)."""
    t = datetime(year, 1, 1, tzinfo=timezone.utc)
    prev = t.astimezone(CAIRO).utcoffset()
    for _ in range(366 * 24):
        t += timedelta(hours=1)
        off = t.astimezone(CAIRO).utcoffset()
        if off != prev:
            return (t - timedelta(minutes=5)).astimezone(CAIRO)
        prev = off
    pytest.skip("no DST change this year")


def test_minutes_are_real_elapsed_time_across_dst(policy):
    before = _transition_around(2026)
    later = policy.due_at(K.reminder, before)
    assert (later.astimezone(timezone.utc) - before.astimezone(timezone.utc)) == timedelta(minutes=10)
    assert later.utcoffset() != before.utcoffset()


@pytest.mark.parametrize("state", [s for s in State if s not in TERMINAL])
def test_every_open_state_gets_a_timer(db, policy, clock, make_event, state):
    rid = db.create_request(make_event())
    db.update_request(rid, state=state.value)
    timers.schedule_for_state(db, policy, rid, state, clock.now())
    assert db.future_timers(rid), f"{state} has no timer"
    # Even after every once-only timer has fired, the fallback keeps one alive.
    for t in db.future_timers(rid):
        db.mark_timer(t["id"], fired=True)
    timers.schedule_for_state(db, policy, rid, state, clock.now())
    assert db.future_timers(rid), f"{state} has no timer after the first round fired"


def test_holding_done_ignores_a_bare_pending_send(db, make_event):
    """Bug g: a send stuck at 'pending' is in flight (or crashed mid-send); it must NOT count the holding floor as
    done, or a genuinely-unsent holding reply would be suppressed forever. Only a resolved status counts."""
    rid = db.create_request(make_event())
    msg = OutboundMessage(request_id=rid, step="holding", kind=OutKind.holding, channel=Channel.telegram_group,
                          text="x", chat_id=-100500)
    assert db.claim_send(msg, SendTrigger.holding_floor)             # -> status 'pending'
    assert db.get_send(msg.send_key)["status"] == "pending"
    assert timers.holding_done(db, rid) is False                     # a bare 'pending' is NOT done
    db.finish_send(msg.send_key, SendStatus.maybe_sent)              # boot's stale-send sweep does exactly this
    assert timers.holding_done(db, rid) is True                      # 'maybe_sent' counts as done (never re-sent)


def test_holding_floor_rules(db, policy, clock, make_event):
    rid = db.create_request(make_event())
    timers.schedule_for_state(db, policy, rid, State.TRIAGED, clock.now())
    kinds = {t["kind"] for t in db.future_timers(rid)}
    assert {"reminder", "holding", "escalate_backup", "sla_warning"} <= kinds
    # A holding timer that fired but whose send was blocked is re-armed: the hard floor never dies (9.8)
    for t in db.timers_of(rid, "holding"):
        db.mark_timer(t["id"], fired=True)
    timers.schedule_for_state(db, policy, rid, State.FAILED_WORKER, clock.now())
    assert len(db.timers_of(rid, "holding")) == 2
    # Once it went out (or was recorded in observe), it is never scheduled again
    msg = OutboundMessage(request_id=rid, step="holding", kind=OutKind.holding, channel=Channel.telegram_group,
                          text="x", chat_id=-100500)
    db.finish_send(msg.send_key, SendStatus.would_send, msg=msg)
    for t in db.timers_of(rid, "holding"):
        db.mark_timer(t["id"], fired=True)
    timers.schedule_for_state(db, policy, rid, State.FAILED_WORKER, clock.now())
    assert len(db.timers_of(rid, "holding")) == 2
    # TAKEN_OVER keeps holding unless no_holding (red-team #9)
    r2 = db.create_request(make_event(msg_id=2))
    db.update_request(r2, taken_over=1)
    timers.schedule_for_state(db, policy, r2, State.TAKEN_OVER, clock.now())
    assert {"holding", "takeover_backup"} <= {t["kind"] for t in db.future_timers(r2)}
    r3 = db.create_request(make_event(msg_id=3))
    db.update_request(r3, taken_over=1, no_holding=1)
    timers.schedule_for_state(db, policy, r3, State.TAKEN_OVER, clock.now())
    assert "holding" not in {t["kind"] for t in db.future_timers(r3)}
    # PAUSED: re-card every 2 h + backup escalation, no holding
    r4 = db.create_request(make_event(msg_id=4))
    timers.schedule_for_state(db, policy, r4, State.PAUSED, clock.now())
    assert {t["kind"] for t in db.future_timers(r4)} == {"paused_recard", "escalate_backup"}
    # Answered requests get no reply timers
    r5 = db.create_request(make_event(msg_id=5))
    db.update_request(r5, first_reply_at=clock.now(), first_reply_by="principal")
    timers.schedule_for_state(db, policy, r5, State.ACKED, clock.now())
    assert not {"holding", "reminder"} & {t["kind"] for t in db.future_timers(r5)}


def test_terminal_cancels_all(db, policy, clock, make_event):
    rid = db.create_request(make_event())
    timers.schedule_for_state(db, policy, rid, State.TRIAGED, clock.now())
    timers.schedule_for_state(db, policy, rid, State.CLOSED_BY_PRINCIPAL, clock.now())
    assert db.future_timers(rid) == []


def test_tick_fires_and_publishes(db, policy, clock, bus, make_event):
    rid = db.create_request(make_event())
    timers.schedule(db, rid, K.reminder, clock.now() + timedelta(minutes=1), {"a": 1})
    assert timers.tick(db, policy, clock.now(), bus) == []
    clock.advance(minutes=2)
    fired = timers.tick(db, policy, clock.now(), bus)
    assert fired[0]["kind"] == "reminder" and fired[0]["payload"] == {"a": 1}
    assert bus.published[-1][0] == Topics.TIMER_FIRED
    assert timers.tick(db, policy, clock.now(), bus) == []          # fires once


def test_find_stalled(db, policy, clock, make_event):
    r1 = db.create_request(make_event())                            # no timer at all
    r2 = db.create_request(make_event(msg_id=2))
    timers.schedule(db, r2, K.reminder, clock.now() + timedelta(minutes=5))
    assert timers.find_stalled(db, clock.now()) == [r1]
    clock.advance(minutes=16)                                       # r2's timer now 11 min late
    assert set(timers.find_stalled(db, clock.now())) == {r1, r2}


def test_rebase_overdue_staggers_oldest_first(db, clock, make_event):
    rids = [db.create_request(make_event(msg_id=i)) for i in range(1, 4)]
    base = clock.now()
    for i, rid in enumerate(rids):
        timers.schedule(db, rid, K.holding, base + timedelta(minutes=i))
    for _ in range(3):
        db.add_timer(None, "sweep_light", base)
    clock.advance(hours=9)                                          # laptop was off
    n = timers.rebase_overdue(db, clock.now(), stagger_s=20)
    assert n == 4
    due = [parse_ts(t["due_at"]) for t in db.pending_timers() if t["request_id"]]
    assert [(d - clock.now()).total_seconds() for d in due] == [0, 20, 40]
    assert len(db.pending_timers(global_only=True)) == 1           # duplicate sweeps dropped


def test_email_request_gets_no_holding_card():
    """2026-11-10: mail has no holding reply; no "please answer by hand" card per email."""
    from twin_agent import flow_timers
    from twin_agent.models import Channel

    class F:
        def __init__(self):
            self.cards, self.events = [], []
            self.db = self

        def log_event(self, kind, rid=None, **kw):
            self.events.append(kind)

        def card(self, *a, **kw):
            self.cards.append(a)

    f = F()
    flow_timers.send_holding(f, "REQ-9", {"channel": Channel.email})
    assert f.cards == [] and f.events == ["holding_skipped"]


def test_sweep_reads_gmail_in_a_thread_and_resets_on_error(monkeypatch):
    """2026-11-10: the sweep's Gmail read runs off the event loop; a failure drops the client, logs, keeps the
    cursor (no mail lost) and sends no health card (the poller owns that)."""
    import asyncio
    from twin_agent import flow_timers

    monkeypatch.setattr(flow_timers, "check_stalled", lambda f: None)

    class G:
        def __init__(self, fail):
            self.fail, self.resets = fail, []

        def list_new(self, name, cur):
            if self.fail:
                raise TimeoutError()
            return ["ev1"], "99"

        def reset(self, name):
            self.resets.append(name)

    class F:
        def __init__(self, g):
            self.cfg = {"mailboxes": {"ticketmail": {"address": "a@b.c"}}}
            self.adapters = type("A", (), {"tg_in": None, "gmail_in": g})()
            self.db, self.bus, self.cursors, self.events, self.seen, self.health = self, self, {}, [], [], []

        def get_cursor(self, k):
            return self.cursors.get(k, "1")

        def set_cursor(self, k, v):
            self.cursors[k] = v

        def log_event(self, kind, rid=None, **kw):
            self.events.append(kind)

        def publish(self, topic, p):
            self.health.append(p)

        async def on_event_in(self, ev):
            self.seen.append(ev)

    ok = F(G(False))
    asyncio.run(flow_timers.sweep(ok))
    assert ok.seen == ["ev1"] and ok.cursors["gmail:ticketmail"] == "99"
    bad = F(G(True))
    asyncio.run(flow_timers.sweep(bad))
    assert bad.adapters.gmail_in.resets == ["ticketmail"] and "sweep_gmail_failed" in bad.events
    assert bad.health == [] and "gmail:ticketmail" not in bad.cursors
