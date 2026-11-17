"""Summer-time end in Cairo (2026-10-29, 24:00 -> 23:00): every comparison is on instants, not wall clock.

Python compares, subtracts and adds a timedelta by WALL CLOCK when both datetimes share the Cairo tzinfo, so
inside the repeated hour 23:50 (+03, fold 0) looks LATER than 23:10 (+02, fold 1) although it is 20 min earlier,
and any span across the change is an hour off. Each test below fails on the old wall-clock code.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from twin_agent import clock as clk
from twin_agent import paths, timers
from twin_agent.checks import guards
from twin_agent.clock import CAIRO, FakeClock, elapsed, latest, utc
from twin_agent.db import StateDB
from twin_agent.models import SendTrigger, State, utc_iso

EARLY = datetime(2026, 10, 29, 23, 50, tzinfo=CAIRO, fold=0)    # 20:50 UTC (summer time, +03)
LATE = datetime(2026, 10, 29, 23, 10, tzinfo=CAIRO, fold=1)     # 21:10 UTC (winter time, +02): 20 min later


def test_the_trap_is_real():
    assert LATE < EARLY                                          # plain Python: wrong order
    assert utc(LATE) > utc(EARLY) and elapsed(EARLY, LATE) == timedelta(minutes=20)
    assert latest(EARLY, LATE) is LATE and clk.earliest(EARLY, LATE) is EARLY
    a, b = datetime(2026, 10, 29, 20, 0, tzinfo=CAIRO), datetime(2026, 10, 30, 8, 0, tzinfo=CAIRO)
    assert b - a == timedelta(hours=12) and elapsed(a, b) == timedelta(hours=13)


def test_fake_clock_advance_is_real_time():
    c = FakeClock(datetime(2026, 10, 29, 23, 30, tzinfo=CAIRO))
    start = c.now()
    c.advance(hours=1)
    assert elapsed(start, c.now()) == timedelta(hours=1)
    assert c.now().hour == 23 and c.now().utcoffset() == timedelta(hours=2)   # the repeated 23:30


def test_reopen_window_counts_real_days(tmp_home, cfg):
    c = FakeClock(datetime(2026, 11, 3, 11, 30, tzinfo=CAIRO))
    db = StateDB(paths.state_db(), c, cfg)
    closed = datetime(2026, 10, 20, 12, 0, tzinfo=CAIRO)         # 14 days and 30 real minutes earlier
    assert not db.within_reopen_window({"closed_at": utc_iso(closed)})
    assert db.within_reopen_window({"closed_at": utc_iso(closed + timedelta(hours=1))})
    db.close()


def test_find_stalled_and_rebase_in_the_repeated_hour(tmp_home, cfg):
    from twin_agent.testing.fakes import make_event
    c = FakeClock(LATE)
    db = StateDB(paths.state_db(), c, cfg)
    rid = db.create_request(make_event("WAVE/00001", ts=EARLY))
    db.transition(rid, State.TRIAGED, actor="t")
    db.add_timer(rid, "reminder", EARLY)                         # due 20 real minutes ago
    assert timers.find_stalled(db, c.now(), 10) == [rid]
    assert timers.rebase_overdue(db, c.now(), 20) == 1
    db.close()


def test_human_active_lock_uses_elapsed_time():
    ctx = guards.GuardContext(cfg={}, db=None, now=LATE, trigger=SendTrigger.timeout, kill_switch_on=False,
                              principal_last_msg_at=EARLY)
    assert guards._human_active(None, ctx) == []                 # 20 min ago: the 5 min lock is over
    ctx.principal_last_msg_at = clk.shift(LATE, -timedelta(minutes=2))     # NOT LATE - 2 min: that drops fold
    assert guards._human_active(None, ctx)


def test_loop_guard_counts_real_hours():
    from twin_agent.models import Channel, OutboundMessage, OutKind
    m = OutboundMessage(request_id="REQ-0001", step="draft-1", kind=OutKind.answer, channel=Channel.telegram_group,
                        text="x")
    now = datetime(2026, 11, 1, 22, 30, tzinfo=CAIRO)
    ctx = guards.GuardContext(cfg={"guards": {"loop_guard_max_age_hours": 72}}, db=None, now=now,
                              trigger=SendTrigger.timeout, kill_switch_on=False, original_msg_ts=datetime(2026, 10, 29, 23, 0, tzinfo=CAIRO))
    assert guards._loop_guard(m, ctx)                            # 72.5 real hours (wall clock says 71.5)


def test_export_uses_unixtime_in_the_repeated_hour():
    from twin_agent.replay import export as X
    raw = {"id": 1, "name": "g", "messages": [
        {"id": 1, "type": "message", "date": "2026-10-29T23:50:00", "date_unixtime": str(int(EARLY.timestamp())),
         "from": "A", "from_id": "user11", "text": "WAVE/00001"},
        {"id": 2, "type": "message", "date": "2026-10-29T23:10:00", "date_unixtime": str(int(LATE.timestamp())),
         "from": "A", "from_id": "user11", "text": "still"}]}
    evs = X.load_events(raw, principal_id=None, since=None, days=3)
    assert [e.msg_id for e in evs] == [1, 2] and elapsed(evs[0].ts, evs[1].ts) == timedelta(minutes=20)


def test_reaction_events_from_export():
    from twin_agent.replay import export as X
    raw = {"id": 1, "name": "g", "messages": [
        {"id": 5, "type": "message", "date": "2026-11-01T12:00:00", "date_unixtime": "1793268000", "from": "A",
         "from_id": "user11", "text": "done", "reactions": [
             {"type": "emoji", "count": 1, "emoji": "👍", "recent": [
                 {"from": "B", "from_id": "user12", "date": "2026-11-01T12:05:00"}]}]}]}
    assert len(X.load_events(raw, principal_id=None, since=None, days=3)) == 1
    evs = X.load_events(raw, principal_id=None, since=None, days=3, reactions=True)
    r = [e for e in evs if e.reaction]
    assert len(r) == 1 and r[0].reply_to == 5 and r[0].sender.tg_id == 12 and r[0].text == ""


def test_burst_cap_in_the_repeated_hour(tmp_home, cfg, bus):
    from twin_agent.router import Router
    from twin_agent.testing.fakes import STAFF_A, make_event
    c = FakeClock(datetime(2026, 10, 29, 23, 40, tzinfo=CAIRO, fold=0))
    db = StateDB(paths.state_db(), c, cfg)
    r = Router(db, cfg, timers.TimerPolicy(cfg), bus, c)
    rid = asyncio.run(r.on_event(make_event("WAVE/00001", msg_id=1, sender=STAFF_A, ts=c.now())))
    first = c.now()
    for i in range(2, 12):                                       # a message every 2.5 min, across the change
        c.advance(minutes=2.5)
        asyncio.run(r.on_event(make_event(f"more {i}", msg_id=i, sender=STAFF_A, ts=c.now())))
    due = [timers.parse_ts(t["due_at"]) for t in db.future_timers(rid) if t["kind"] == "burst_close"]
    assert len(due) == 1 and elapsed(first, due[0]) <= timedelta(minutes=30)   # the 30 min cap holds
    db.close()


@pytest.mark.parametrize("hours,expect", [(47.5, 0), (48.5, 1)])
def test_board_unanswered_48h_is_real_time(tmp_home, cfg, hours, expect):
    import sqlite3
    from twin_agent.board import queries
    from twin_agent.testing.fakes import make_event
    now = datetime(2026, 10, 30, 12, 0, tzinfo=CAIRO)            # 48 h back crosses the change
    db = StateDB(paths.state_db(), FakeClock(now), cfg)
    db.create_request(make_event("WAVE/00001", ts=clk.shift(now, -timedelta(hours=hours))))
    db.close()
    con = sqlite3.connect(paths.state_db())
    con.row_factory = sqlite3.Row
    try:
        assert queries.kpis(con, now)["unanswered_48h"] == expect
    finally:
        con.close()
