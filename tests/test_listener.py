"""listener.py + flow.py: lock, mode, adapters, end-to-end in observe (0 sends), kill switch, boot catch-up."""
import asyncio
from datetime import timedelta

import pytest

from twin_agent import listener, paths, timers
from twin_agent.bus import Bus, Topics
from twin_agent.db import StateDB
from twin_agent.flow import Flow
from twin_agent.flow_timers import boot_catchup, check_stalled, ensure_global_timers, on_timer
from twin_agent.models import CardKind, State, TimerKind, parse_ts, utc_iso
from twin_agent.testing import fakes
from twin_agent.testing.fakes import make_event


def run(coro):
    return asyncio.run(coro)


def test_instance_lock(tmp_home):
    a, b = listener.InstanceLock(), listener.InstanceLock()
    assert a.acquire()
    assert not b.acquire()                         # second listener exits
    a.release()
    assert b.acquire()                             # a leftover lock FILE does not block a restart
    b.release()


def test_main_exits_3_when_locked(tmp_home):
    held = listener.InstanceLock()
    assert held.acquire()
    try:
        assert listener.main(["--dry-run", "--once"]) == 3
    finally:
        held.release()


def test_apply_mode(cfg):
    cfg["dial"]["L3_t1_answer"] = "auto"
    cfg["send_enabled"] = True
    out = listener.apply_mode(cfg, "observe", True)
    assert out["run_mode"] == "dry_run" and out["send_enabled"] is False
    assert out["dial"]["L3_t1_answer"] == "observe" and out["dial"]["L11_internal_diagnosis"] == "auto"
    assert out["dial"]["L6_ticket_email"] == "off"


def test_build_adapters_dry_run_is_all_fakes(cfg):
    a = listener.build_adapters(cfg)
    assert isinstance(a.tg_in, fakes.FakeTelegram) and isinstance(a.claude, fakes.FakeClaude)
    assert isinstance(a.bot, fakes.FakeBot) and isinstance(a.gmail_send, fakes.FakeGmail)


def test_main_once_dry_run_writes_heartbeat(tmp_home):
    assert listener.main(["--dry-run", "--once"]) == 0
    beat = paths.heartbeat_file().read_text(encoding="utf-8").strip()
    assert beat.endswith("+00:00") and parse_ts(beat) is not None
    assert (paths.logs_dir() / "listener.log").exists()


@pytest.fixture
def flow(tmp_home, cfg, clock):
    db = StateDB(paths.state_db(), clock, cfg)
    bus = Bus()
    adapters = listener._dry_adapters()
    sender = listener._optional_sender(db, cfg, adapters, clock, bus)
    bot_ctl = listener._optional_bot_ctl(db, cfg, adapters, clock, bus)
    f = Flow(db, cfg, bus, clock, adapters, sender=sender, bot_ctl=bot_ctl)
    f.deadman = listener.DeadMan(cfg)
    f.spawner.retry_delay_s = 0
    f.wire()
    yield f
    db.close()


async def settle(f: Flow):
    for _ in range(10):
        await f.bus.drain()
        if f.tasks:
            await asyncio.gather(*list(f.tasks), return_exceptions=True)
        else:
            break
    await f.bus.drain()


def test_end_to_end_observe_never_sends(flow, clock):
    f = flow

    async def go():
        tg = f.adapters.tg_in
        ev = make_event("WAVE/00001 cannot validate", msg_id=1, ts=clock.now())
        f.bus.publish(Topics.EVENT_IN, ev)
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        assert f.db.get_cursor(f"tg:{fakes.GROUP_ID}:{fakes.TOPIC_LOGISTICS}") == "1"
        clock.advance(minutes=3)
        timers.tick(f.db, f.policy, clock.now(), f.bus)        # burst closes -> TRIAGED -> worker -> checks
        await settle(f)
        req = f.db.get_request(rid)
        assert req["state"] == "DIAGNOSED" and f.db.latest_packet(rid)
        assert f.adapters.claude.calls and f.adapters.claude.calls[0]["kind"] == "worker"
        clock.advance(minutes=61)                              # holding floor fires
        timers.tick(f.db, f.policy, clock.now(), f.bus)
        await settle(f)
        assert tg.sent == []                                   # observe / dry_run: 0 real sends
        hold = f.db.get_send(f"{rid}:holding")
        assert hold["status"] == "would_send" and hold["trigger"] == "holding_floor"
        kinds = [c.kind for _, c, _ in f.adapters.bot.cards]
        assert CardKind.new_request in kinds and CardKind.observe_would_send in kinds
        assert f.db.future_timers(rid)                         # invariant holds
        return rid
    run(go())


def test_principal_answer_stops_holding(flow, clock):
    f = flow

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("how to print?", msg_id=1, ts=clock.now()))
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        clock.advance(minutes=3)
        timers.tick(f.db, f.policy, clock.now(), f.bus)
        await settle(f)
        clock.advance(minutes=5)
        f.bus.publish(Topics.EVENT_IN, make_event("Settings > Print", msg_id=2, sender=fakes.PRINCIPAL,
                                                  ts=clock.now(), reply_to=1))
        await settle(f)
        kinds = {t["kind"] for t in f.db.future_timers(rid)}
        assert "holding" not in kinds and "reminder" not in kinds
    run(go())


def test_kill_switch_file_pauses_and_resumes(flow, clock):
    f = flow

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("WAVE/00001", msg_id=1, ts=clock.now()))
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        await f.router.close_burst(rid)
        await settle(f)
        paths.stop_file().write_text("stop", encoding="utf-8")
        f.check_stop_file()
        await settle(f)
        req = f.db.get_request(rid)
        assert f.db.kill_on() and req["state"] == "PAUSED"
        assert {"paused_recard", "escalate_backup"} <= {t["kind"] for t in f.db.future_timers(rid)}
        paths.stop_file().unlink()
        f.check_stop_file()
        await settle(f)
        assert not f.db.kill_on() and f.db.get_request(rid)["state"] in ("TRIAGED", "DIAGNOSED")
        assert "paused_recard" not in {t["kind"] for t in f.db.future_timers(rid)}
    run(go())


def test_boot_catchup_rebases_and_mutes_stalled(flow, clock):
    f = flow

    async def go():
        rids = []
        for i in range(1, 4):
            f.bus.publish(Topics.EVENT_IN, make_event(f"WAVE/0000{i}", msg_id=i, ts=clock.now(),
                                                      sender=fakes.Sender(tg_id=2000 + i, name=f"S{i}")))
            await settle(f)
        for r in f.db.open_requests():
            await f.router.close_burst(r["id"])
            rids.append(r["id"])
        await settle(f)
        last = clock.now()
        clock.advance(hours=9)                                  # laptop off all night
        n = boot_catchup(f, last)
        assert n >= 3
        assert check_stalled(f) == [] or not any(
            c.kind == CardKind.stalled for c in [p for t, p in f.bus.published if t == Topics.CARD_OUT])
        summary = [p for t, p in f.bus.published if t == Topics.CARD_OUT and "Back online" in p.text]
        assert len(summary) == 1                                # one summary card, not one per request
        due = sorted(parse_ts(t["due_at"]) for t in f.db.pending_timers() if t["request_id"])
        assert due[0] == clock.now() and (due[1] - due[0]).total_seconds() == 20
    run(go())


def test_no_catchup_after_short_gap(flow, clock):
    assert boot_catchup(flow, clock.now() - timedelta(seconds=30)) == 0
    assert boot_catchup(flow, None) == 0


def test_global_timers_reschedule(flow, clock):
    f = flow

    async def go():
        ensure_global_timers(f)
        kinds = {t["kind"] for t in f.db.pending_timers(global_only=True)}
        assert kinds == {"sweep_light", "sweep_full", "digest", "scorecard", "deadman_ping"}
        clock.advance(minutes=6)
        timers.tick(f.db, f.policy, clock.now(), f.bus)
        await settle(f)
        assert f.deadman.pings == 1                             # dry run: counted, never sent
        assert "deadman_ping" in {t["kind"] for t in f.db.pending_timers(global_only=True)}
    run(go())


def test_sweep_first_run_baselines_then_finds_new(flow, clock):
    f = flow

    async def go():
        tg = f.adapters.tg_in
        from twin_agent.flow_timers import sweep
        # A message that predates the twin: the FIRST sweep BASELINES to 'now' and opens NOTHING (issue #45) -
        # the twin must not re-open months of already-answered threads on its first run.
        tg.thread.append(make_event("WAVE/00009 old already-answered", msg_id=9, ts=clock.now()))
        assert await sweep(f) == 0
        await settle(f)
        assert not f.db.open_requests()                        # nothing opened from the backlog
        # A NEW message after the baseline is found and marked by the sweep.
        tg.thread.append(make_event("WAVE/00010 new", msg_id=10, ts=clock.now()))
        assert await sweep(f) == 1
        await settle(f)
        req = f.db.open_requests()[0]
        assert req["found_by_sweep"] == 1
        assert await sweep(f) == 0                              # nothing new: no re-read of old ids
    run(go())


def test_plain_reply_is_instruction_not_text(flow, clock):
    f = flow

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("WAVE/00001", msg_id=1, ts=clock.now()))
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        f.bus.publish(Topics.REPLY_IN, {"user_id": fakes.PRINCIPAL_ID, "request_id": rid,
                                        "text": "ask him for the batch number", "force_reply": False})
        await settle(f)
        assert rid not in f.pending_edit
        assert f.adapters.tg_in.sent == []
        assert f.db.events(rid, "instruction")
    run(go())


def test_backup_reply_is_not_labelled_as_principal(flow, clock):
    """Review fix: a backup's plain reply reaches the worker NOTE, but never as "Principal's instruction"."""
    f = flow

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("WAVE/00001", msg_id=1, ts=clock.now()))
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        f.spawn = lambda coro: coro.close()                      # keep the NOTE as written (no worker run)
        f.bus.publish(Topics.REPLY_IN, {"user_id": fakes.BACKUP_ID, "request_id": rid,
                                        "text": "tell him it is fixed", "force_reply": False})
        await settle(f)
        note = (paths.work_dir(rid) / "NOTE.md").read_text(encoding="utf-8")
        assert "Principal's instruction: tell him" not in note and "Backup colleague's (not Principal)" in note
    run(go())


def test_main_async_once(tmp_home, cfg, clock):
    adapters = listener._dry_adapters()
    # First run: a message already in history BASELINES to 'now' and opens nothing (issue #45) - the twin must not
    # re-open months of already-answered threads on its first run.
    adapters.tg_in.thread.append(make_event("WAVE/00001 old", msg_id=5, ts=clock.now()))
    f = run(listener.main_async(cfg, adapters, clock, once=True))
    assert f is not None
    db = StateDB(paths.state_db(), clock, cfg)
    try:
        assert len(db.open_requests()) == 0            # baseline: the backlog is not reopened
        assert db.get_cursor("heartbeat")
    finally:
        db.close()
    # A NEW message (after the baseline) IS picked up on the next cycle.
    adapters.tg_in.thread.append(make_event("WAVE/00002 new", msg_id=6, ts=clock.now()))
    run(listener.main_async(cfg, adapters, clock, once=True))
    db = StateDB(paths.state_db(), clock, cfg)
    try:
        assert len(db.open_requests()) == 1
    finally:
        db.close()


# ------------------------------------------------------------ review fixes

def test_checks_run_in_background_so_events_keep_flowing(flow, clock, monkeypatch):
    import threading
    from twin_agent.checks import judge
    gate = threading.Event()

    def slow_checks(*a, **k):
        gate.wait(5)
        return []
    monkeypatch.setattr(judge, "run_checks", slow_checks)
    f = flow

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("WAVE/00001", msg_id=1, ts=clock.now()))
        await f.bus.drain()
        rid = f.db.open_requests()[0]["id"]
        await f.router.close_burst(rid)
        for _ in range(50):                                     # wait until the worker's packet is in
            await f.bus.drain()
            await asyncio.sleep(0.02)
            if f.db.latest_packet(rid):
                break
        f.bus.publish(Topics.EVENT_IN, make_event("unrelated", msg_id=2, ts=clock.now(), sender=fakes.STAFF_B))
        await f.bus.drain()
        assert len(f.db.open_requests()) == 2                   # routed while checks still run
        gate.set()
        await settle(f)
        assert f.db.latest_packet(rid)["run_no"] == 1           # one packet row per worker run
    run(go())


def test_pause_rearms_holding_on_resume(flow, clock):
    f = flow

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("WAVE/00001", msg_id=1, ts=clock.now()))
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        await f.router.close_burst(rid)
        await settle(f)
        f.bus.publish(Topics.KILL_CHANGED, {"on": True, "by": "test"})
        await settle(f)
        assert "holding" not in {t["kind"] for t in f.db.future_timers(rid)}
        f.bus.publish(Topics.EVENT_IN, make_event("new while stopped", msg_id=2, ts=clock.now(),
                                                  sender=fakes.STAFF_B))
        await settle(f)
        rid2 = [r["id"] for r in f.db.open_requests() if r["id"] != rid][0]
        await f.router.close_burst(rid2)
        await settle(f)
        assert f.db.get_request(rid2)["state"] == "PAUSED"
        f.bus.publish(Topics.KILL_CHANGED, {"on": False, "by": "test"})
        await settle(f)
        for r in (rid, rid2):
            assert "holding" in {t["kind"] for t in f.db.future_timers(r)}   # hard floor is back
    run(go())


def test_live_adapter_failure_is_none_not_fake(cfg, monkeypatch):
    from twin_agent import creds, spawner
    cfg["run_mode"] = "live"
    monkeypatch.setattr(creds, "has_secret", lambda *a, **k: True)
    monkeypatch.setattr(creds, "_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no value")))

    def refuse(*a, **k):
        raise RuntimeError("no claude")
    monkeypatch.setattr(spawner, "RealClaudeRunner", refuse)
    a = listener.build_adapters(cfg)
    assert a.claude is None and a.tg_send is None and a.bot is None
    assert not isinstance(a.tg_in, fakes.FakeTelegram)
    assert a.problems


def test_health_card_once_per_change(flow):
    f = flow

    async def go():
        for _ in range(3):
            f.bus.publish(Topics.HEALTH, {"component": "tg_in", "ok": False, "detail": "AuthKeyUnregistered"})
        f.bus.publish(Topics.HEALTH, {"component": "tg_in", "ok": True})
        await settle(f)
        health = [c for _, c, _ in f.adapters.bot.cards if c.kind == CardKind.health]
        assert len(health) == 2 and "problem" in health[0].text and "normal" in health[1].text
    run(go())


def test_own_send_echo_is_not_principal_typing(flow, clock):
    from twin_agent.models import Channel, OutboundMessage, OutKind, SendStatus
    f = flow

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("WAVE/00001", msg_id=1, ts=clock.now()))
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        m = OutboundMessage(request_id=rid, step="holding", kind=OutKind.holding, channel=Channel.telegram_group,
                            text="x", chat_id=fakes.GROUP_ID, topic_id=fakes.TOPIC_LOGISTICS)
        f.db.finish_send(m.send_key, SendStatus.sent, msg=m, remote_id="777")
        f.bus.publish(Topics.EVENT_IN, make_event("x", msg_id=777, sender=fakes.PRINCIPAL, ts=clock.now()))
        await settle(f)
        assert f.db.get_cursor(f"principal_active:{fakes.GROUP_ID}") is None
        assert f.db.get_request(rid)["first_reply_by"] is None
    run(go())


def test_bad_config_exits_2(tmp_home):
    (tmp_home / "config" / "config.yaml").write_text("dial:\n  L3_t1_answer: yolo\n", encoding="utf-8")
    assert listener.main(["--once"]) == 2


def test_gmail_send_not_a_problem_when_send_disabled(tmp_home, cfg):
    # read-only on purpose (send_enabled false) + a ticket mailbox: gmail_send must NOT be flagged a problem (#46)
    live = dict(cfg, run_mode="live", send_enabled=False,
                mailboxes={"ticketmail": {"address": "help@example.com"}})
    adapters = listener.build_adapters(live)
    probs = getattr(adapters, "problems", [])
    assert not any("gmail_send" in p for p in probs), probs
