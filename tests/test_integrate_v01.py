"""v0.1 integration: Watcher timed export/sync in the listener, /pause on real time, aa.py voice + whatsapp."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from twin_agent import listener, paths
from twin_agent.bus import Bus, Topics
from twin_agent.channels import whatsapp_cli
from twin_agent.channels.bot_controller import BotController
from twin_agent.clock import CAIRO, FakeClock, elapsed, utc
from twin_agent.db import StateDB
from twin_agent.flow import Flow
from twin_agent.models import parse_ts
from twin_agent.testing.fakes import PRINCIPAL_ID, FakeBot
from twin_agent.voice import cli as voice_cli
from twin_agent.watcher import gate
from twin_agent.watcher import sync as sync_mod
from watcher_helpers import seed, watcher_on


def run(coro):
    return asyncio.run(coro)


def _flow(db, cfg, clock):
    f = Flow(db, cfg, Bus(), clock, listener._dry_adapters())
    f.wire()
    return f


@pytest.fixture
def fake_sync(monkeypatch):
    fs = sync_mod.FakeSync()
    monkeypatch.setattr(sync_mod, "make_sync", lambda *a, **k: fs)
    return fs


def test_watcher_tick_default_config_does_nothing(tmp_home, cfg, clock, fake_sync):
    db = StateDB(paths.state_db(), clock, cfg)
    seed(db, clock)
    run(listener.tick_extras(_flow(db, cfg, clock)))
    assert fake_sync.pushes == [] and not (tmp_home / "data" / "watcher" / "outbox").exists()
    assert db.events(None, "watcher_export") == []
    db.close()


def test_watcher_tick_needs_consent_even_with_interval(tmp_home, cfg, clock, fake_sync):
    cfg["watcher"]["sync"]["interval_minutes"] = 30                 # timer on, but no D4 / watcher.enabled
    db = StateDB(paths.state_db(), clock, cfg)
    seed(db, clock)
    run(listener.watcher_tick(_flow(db, cfg, clock)))
    assert fake_sync.pushes == [] and not (tmp_home / "data" / "watcher" / "outbox").exists()
    db.close()


def test_watcher_tick_exports_and_syncs_on_interval_and_respects_pause(tmp_home, cfg, clock, fake_sync):
    watcher_on(cfg, desk_sessions=False)
    cfg["watcher"]["sync"]["interval_minutes"] = 30
    db = StateDB(paths.state_db(), clock, cfg)
    seed(db, clock)
    f = _flow(db, cfg, clock)
    run(listener.watcher_tick(f))
    assert len(fake_sync.pushes) == 1 and len(db.events(None, "watcher_export")) == 1
    assert any((tmp_home / "data" / "watcher" / "outbox").rglob("*"))
    clock.advance(minutes=10)
    run(listener.watcher_tick(f))                                   # not due yet
    assert len(fake_sync.pushes) == 1
    db.log_event(gate.PAUSE, None, actor=str(PRINCIPAL_ID))
    clock.advance(minutes=31)
    run(listener.watcher_tick(f))                                   # due, but Principal paused: nothing
    assert len(fake_sync.pushes) == 1
    db.close()


def test_watcher_tick_failure_is_a_health_event(tmp_home, cfg, clock, monkeypatch):
    watcher_on(cfg, desk_sessions=False)
    cfg["watcher"]["sync"]["interval_minutes"] = 5
    monkeypatch.setattr(sync_mod, "make_sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    db = StateDB(paths.state_db(), clock, cfg)
    f = _flow(db, cfg, clock)
    seen = []
    f.bus.subscribe(Topics.HEALTH, seen.append)
    run(listener.tick_extras(f))
    run(f.bus.drain())
    assert any(h.get("component") == "watcher" and h.get("ok") is False for h in seen)
    db.close()


def test_pause_span_is_real_time_across_summer_time_end(tmp_home, cfg):
    c = FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=CAIRO))       # 24 h /pause across 24:00 -> 23:00
    db = StateDB(paths.state_db(), c, watcher_on(cfg))
    ctl = BotController(FakeBot(), db, cfg, Bus(), c, stop_file=lambda: tmp_home / "STOP")
    run(ctl.on_command({"user_id": PRINCIPAL_ID, "command": "pause", "args": "24h"}))
    import json
    until = parse_ts(json.loads(db.events(None, gate.PAUSE)[0]["detail_json"])["until"])
    assert elapsed(c.now(), until) == timedelta(hours=24)
    c.advance(hours=23, minutes=30)
    assert gate.paused_now(db, c.now())
    c.advance(minutes=31)
    assert not gate.paused_now(db, c.now())
    db.close()


def test_pause_window_order_inside_repeated_hour():
    early = datetime(2026, 10, 29, 23, 50, tzinfo=CAIRO, fold=0)    # 20:50 UTC
    late = datetime(2026, 10, 29, 23, 10, tzinfo=CAIRO, fold=1)     # 21:10 UTC, 20 real minutes later
    w = [(early, late)]
    assert gate.in_windows(w, datetime(2026, 10, 29, 23, 55, tzinfo=CAIRO, fold=0))   # 20:55 UTC: paused
    assert not gate.in_windows(w, datetime(2026, 10, 29, 23, 15, tzinfo=CAIRO, fold=1))  # 21:15 UTC: after
    assert utc(late) > utc(early)


def test_voice_status_is_off_by_default_and_names_why(cfg):
    lines = voice_cli.status_lines(cfg, has_secret=lambda n: False, which=lambda n: None)
    text = "\n".join(lines)
    assert lines[0] == "Voice notes: off"
    for why in ("d1_ref", "d2_voice_ref", "voice.enabled", "not live", "ffmpeg", "elevenlabs"):
        assert why in text


def test_voice_status_on_when_everything_is_set(cfg):
    cfg.update(run_mode="live")
    cfg["consent"].update(d1_ref="D1", d2_voice_ref="D2")
    cfg["voice"]["enabled"] = True
    assert voice_cli.status_lines(cfg, has_secret=lambda n: True, which=lambda n: "ffmpeg")[0] == "Voice notes: ON"


def test_voice_check_command(tmp_home, capsys):
    assert voice_cli.main(["check", "Checking it now, give me a few minutes"]) == 0
    assert "OK as a voice note" in capsys.readouterr().out
    assert voice_cli.main(["check", "Invoice IN/00001/26 is 12500 EGP, see https://x.example.test"]) == 1
    out = capsys.readouterr().out
    assert "not as a voice note" in out and "12500" not in out           # reasons only, never the text


def test_whatsapp_status_refused_by_default_and_hides_numbers(cfg):
    cfg["whatsapp"]["allowed_recipients"] = ["201000000001"]
    text = "\n".join(whatsapp_cli.status_lines(cfg, has_secret=lambda n: False))
    assert "refused" in text and "run_mode is not live" in text and "not wired into the sender" in text
    assert "201000000001" not in text and "recipients: 1" in text


def test_whatsapp_status_allowed_only_with_every_gate(cfg):
    cfg.update(run_mode="live", send_enabled=True)
    cfg["consent"]["d1_ref"] = "D1"
    cfg["consent"]["channels"]["whatsapp"] = True
    cfg["whatsapp"].update(enabled=True, graph_version="v0-test")
    assert "vault entry" in "\n".join(whatsapp_cli.status_lines(cfg, has_secret=lambda n: False))
    assert whatsapp_cli.status_lines(cfg, has_secret=lambda n: True)[0] == "WhatsApp Cloud adapter: allowed"


@pytest.mark.parametrize("cmd", [["voice", "status"], ["whatsapp", "status"]])
def test_aa_commands_run_offline(tmp_home, capsys, monkeypatch, cmd):
    import sys
    from pathlib import Path

    from twin_agent import creds
    monkeypatch.setattr(creds, "has_secret", lambda *a, **k: False)   # never touch the real vault
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import aa
    assert aa.main(cmd) == 0
    out = capsys.readouterr().out
    assert "Voice notes: off" in out or "WhatsApp Cloud adapter: refused" in out


class FakeProc:
    def __init__(self, runs_for: int):
        self.left, self.sent, self.killed = runs_for, None, False

    def poll(self):
        self.left -= 1
        return None if self.left > 0 and self.sent is None else 0

    def communicate(self, data, timeout=None):
        self.sent = data

    def kill(self):
        self.killed = True


def test_pause_stops_a_running_screen_recording(tmp_home, cfg, clock):
    from twin_agent.watcher import screen
    db = StateDB(paths.state_db(), clock, watcher_on(cfg))
    cfg["watcher"]["screen_record"].update(enabled=True, window_title="Odoo - Browser")
    proc, polls, notes = FakeProc(runs_for=100), [], []

    def now_fn():                                                    # one call per poll while recording
        polls.append(1)
        if len(polls) == 3:                                          # Principal sends /pause mid-recording
            db.log_event(gate.PAUSE, None, actor=str(PRINCIPAL_ID))
        return clock.now()
    out = screen.record(cfg, db, "REQ-0001", clock.now(), seconds=60, notify=notes.append,
                        popen=lambda *a, **k: proc, now_fn=now_fn, poll_s=0)
    assert proc.sent == b"q" and len(polls) == 3                     # stopped at the first check after /pause
    assert out.parent.name == "REQ-0001" and not gate.recording_file().exists()
    assert db.events("REQ-0001", "watcher_recording_paused") and "stopped" in notes[-1]
    db.close()


def test_record_until_paused_sends_q_and_reports_pause():
    from twin_agent.watcher import screen
    proc, calls = FakeProc(runs_for=100), []

    def should_stop():
        calls.append(1)
        return len(calls) >= 3
    assert screen.record_until_paused(["ffmpeg"], 60, should_stop, popen=lambda *a, **k: proc,
                                      poll_s=0, sleep=lambda s: None) is True
    assert proc.sent == b"q" and len(calls) == 3


def test_record_until_paused_fails_closed_and_runs_to_the_end():
    from twin_agent.watcher import screen
    broken = FakeProc(runs_for=100)
    assert screen.record_until_paused(["ffmpeg"], 60, lambda: 1 / 0, popen=lambda *a, **k: broken,
                                      poll_s=0, sleep=lambda s: None) is True
    assert broken.sent == b"q"
    done = FakeProc(runs_for=3)
    assert screen.record_until_paused(["ffmpeg"], 60, lambda: False, popen=lambda *a, **k: done,
                                      poll_s=0, sleep=lambda s: None) is False
    assert done.sent is None
