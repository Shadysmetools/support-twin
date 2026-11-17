"""Integrator tests: wiring between components (listener, flow, bot controller, sender) and the e2e dry run."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from twin_agent import listener, paths, timers
from twin_agent.bus import Bus, Topics
from twin_agent.db import StateDB
from twin_agent.flow import Flow
from twin_agent.flow_timers import on_timer
from twin_agent.models import Card, CardKind, TimerKind
from twin_agent.testing import fakes
from twin_agent.testing.fakes import make_event


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
        if not f.tasks:
            break
        await asyncio.gather(*list(f.tasks), return_exceptions=True)
    await f.bus.drain()


def test_a_card_is_posted_once_even_after_the_bot_starts(flow):
    async def go():
        await flow.bot_ctl.start()                      # main_async starts the controller as a task
        flow.bus.publish(Topics.CARD_OUT, Card(kind=CardKind.info, text="hello"))
        flow.bus.publish(Topics.CARD_OUT, {"card": Card(kind=CardKind.info, text="with hash"), "draft_hash": ""})
        await settle(flow)
        assert len(flow.adapters.bot.cards) == 2
    asyncio.run(go())


def test_tick_extras_flush_held_cards_and_drain_sender_queue(flow, monkeypatch):
    called = []

    async def drain():
        called.append("drain")
        return []
    monkeypatch.setattr(flow.sender, "drain_queue", drain)

    async def go():
        flow.bot_ctl.held.append((Card(kind=CardKind.info, text="held one"), flow.bot_ctl.principal_id, ""))
        await listener.tick_extras(flow)
        assert called == ["drain"]
        assert flow.bot_ctl.held == [] and flow.adapters.bot.cards
    asyncio.run(go())


def test_backup_is_not_told_no_answer_after_principal_replied(flow, clock):
    async def go():
        flow.bus.publish(Topics.EVENT_IN, make_event("WAVE/00001 stuck", msg_id=1, ts=clock.now()))
        await settle(flow)
        rid = flow.db.open_requests()[0]["id"]
        flow.db.update_request(rid, first_reply_at=clock.now().isoformat(), first_reply_by="principal")
        before = len(flow.adapters.bot.cards)
        await on_timer(flow, {"timer_id": 0, "request_id": rid, "kind": TimerKind.escalate_backup, "payload": {}})
        await settle(flow)
        new = [c for _, c, _ in flow.adapters.bot.cards[before:]]
        assert not any(c.kind == CardKind.escalation for c in new)
        assert flow.db.future_timers(rid)               # still has a next timer
    asyncio.run(go())


def test_vault_hashes_only_in_live_mode(cfg, monkeypatch):
    from twin_agent import creds
    monkeypatch.setattr(creds, "vault_hashes", lambda **k: {"abc"})
    assert listener._vault_hashes(cfg) == ()
    cfg["run_mode"] = "live"
    assert listener._vault_hashes(cfg) == ("abc",)
    monkeypatch.setattr(creds, "vault_hashes", lambda **k: (_ for _ in ()).throw(RuntimeError("no keyring")))
    assert listener._vault_hashes(cfg) == ()


def test_sender_shadow_guards_are_the_guards_mode_list():
    from twin_agent import sender
    from twin_agent.checks import guards
    assert sender.SHADOW_ONLY_GUARDS == guards.MODE_GUARDS


def test_worker_settings_is_strict_json_with_deny_rules():
    from twin_agent.spawner import REPO
    data = json.loads((REPO / "twin_agent" / "worker" / "worker-settings.json").read_text(encoding="utf-8"))
    assert "Bash" in data["permissions"]["deny"] and "WebFetch" in data["permissions"]["deny"]


def test_aa_registers_every_worker_tool_server():
    import aa
    for name in ("mcp-submit-packet", "mcp-kb-search", "mcp-tg-history", "e2e-dry-run"):
        assert name in aa.COMMANDS
    from twin_agent.spawner import write_mcp_config
    assert write_mcp_config  # spawner points at the same names


def _synthetic_export(path, start):
    """A tiny Telegram export: two staff questions in a topic, Principal answers one."""
    def m(i, minutes, who, fid, text, reply=None):
        d = {"id": i, "type": "message", "date": (start + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S"),
             "from": who, "from_id": fid, "text": text}
        if reply:
            d["reply_to_message_id"] = reply
        return d
    msgs = [{"id": 1, "type": "service", "action": "topic_created", "title": "Logistics",
             "date": start.strftime("%Y-%m-%dT%H:%M:%S")},
            m(2, 1, "Staff A", "user2001", "WAVE/00001 cannot validate", 1),
            m(3, 30, "Principal Test", "user1001", "Check the picking", 2),
            m(4, 120, "Staff B", "user2002", "IN/00002 stuck in ready, please help", 1)]
    path.write_text(json.dumps({"name": "Test Group", "id": 500, "messages": msgs}), encoding="utf-8")


def test_e2e_dry_run_on_a_synthetic_export(tmp_path, monkeypatch):
    from twin_agent.replay import e2e
    start = fakes.FakeClock().now().replace(hour=11, minute=0, second=0, microsecond=0, tzinfo=None)
    export = tmp_path / "result.json"
    _synthetic_export(export, start)
    s = e2e.run(export, days=3, home=tmp_path / "home", principal_id=1001, approve_n=1, tail_hours=6)
    assert s["sends_real"] == 0 and s["fake_telegram_sent"] == 0
    assert s["requests"] >= 1 and s["cards_posted"] > 0
    assert s["would_send_total"] > 0                     # the send path ran and stopped at observe
    assert s["invariant_violations"] == 0 and not s["step_cap_hit"]
    assert e2e.passed(s)


def test_e2e_holding_lands_within_target_of_the_first_message(tmp_path):
    """Twin Monitor finding 2026-11-03: holding came 62-66 / 18-19 min after the request. Targets are 60 / 15 min
    from the requester's FIRST message, burst merge included."""
    from twin_agent.replay import e2e
    day = fakes.FakeClock().now().replace(second=0, microsecond=0, tzinfo=None)

    def m(i, at, who, fid, text):
        return {"id": i, "type": "message", "date": at.strftime("%Y-%m-%dT%H:%M:%S"), "from": who,
                "from_id": fid, "text": text, "reply_to_message_id": 1}
    work, night = day.replace(hour=11, minute=0), day.replace(hour=23, minute=0)
    msgs = [{"id": 1, "type": "service", "action": "topic_created", "title": "Logistics",
             "date": (work - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")},
            m(2, work, "Staff A", "user2001", "WAVE/00001 cannot validate"),
            m(3, work + timedelta(minutes=2), "Staff A", "user2001", "please check"),      # burst: +2 min
            m(4, night, "Staff B", "user2002", "IN/00002 stuck in ready"),
            m(5, night + timedelta(minutes=2), "Staff B", "user2002", "urgent")]
    export = tmp_path / "result.json"
    export.write_text(json.dumps({"name": "Test Group", "id": 500, "messages": msgs}), encoding="utf-8")
    s = e2e.run(export, days=3, home=tmp_path / "home", principal_id=1001, approve_n=0, tail_hours=6)
    lat = s["holding_after_first_msg_min"]
    assert lat["in_hours"]["n"] == 1 and lat["in_hours"]["max"] <= 60
    assert lat["out_of_hours"]["n"] == 1 and lat["out_of_hours"]["max"] <= 15
    assert e2e.passed(s)


def test_claude_error_reason_is_the_real_reason_not_success():
    from twin_agent.spawner import parse_output
    out = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Not logged in · Please run /login"})
    res = parse_output(out, "", 1, 3.0, None)
    assert not res.ok and "Not logged in" in res.error


def test_tool_servers_use_console_python_not_pythonw(tmp_path):
    from twin_agent.spawner import console_python
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "python.exe")
    assert console_python(str(tmp_path / "python.exe")) == str(tmp_path / "python.exe")


def test_e2e_refuses_to_wipe_a_home_it_did_not_make(tmp_path, tmp_home):
    from twin_agent.replay import e2e
    export = tmp_path / "result.json"
    export.write_text(json.dumps({"name": "x", "id": 1, "messages": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        e2e.run(export, home=tmp_home)                   # the real agent home (TWIN_AGENT_HOME)
    other = tmp_path / "someone-elses"
    other.mkdir()
    (other / "config.yaml").write_text("keep me", encoding="utf-8")
    with pytest.raises(SystemExit):
        e2e.run(export, home=other)
    assert (other / "config.yaml").read_text(encoding="utf-8") == "keep me"
