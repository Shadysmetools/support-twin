"""Watcher capture side: consent gate, /pause windows, read-only case projection, desk sessions, bot + board."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from twin_agent.board import server as board
from twin_agent.channels.bot_controller import BotController
from twin_agent.db import StateDB
from twin_agent.testing.fakes import PRINCIPAL, PRINCIPAL_ID, BACKUP_ID, STAFF_A, FakeBot, make_event
from twin_agent.watcher import capture, desk, gate
from watcher_helpers import seed, watcher_on


def test_gate_needs_consent_switch_and_capability(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, cfg)
    assert not gate.allowed(cfg, db, "card_edits", clock.now())             # defaults: everything off
    cfg["watcher"]["enabled"] = True
    cfg["watcher"]["capture"]["card_edits"] = True
    assert not gate.allowed(cfg, db, "card_edits", clock.now())             # no D4 ref
    cfg["consent"]["d4_watcher_ref"] = "D4"
    assert gate.allowed(cfg, db, "card_edits", clock.now())
    assert not gate.allowed(cfg, db, "shell", clock.now())                   # own flag still off
    assert not gate.allowed(cfg, db, "nonsense", clock.now())


def test_gate_fails_closed_when_db_unreadable(cfg, clock):
    class Broken:
        def events(self, *a, **k):
            raise RuntimeError("locked")
    watcher_on(cfg)
    assert gate.pause_windows(Broken(), clock.now()) is None
    assert not gate.allowed(cfg, Broken(), "card_edits", clock.now())


def test_pause_windows_and_timed_pause(clock):
    t0 = clock.now()
    rows = [{"id": 1, "kind": gate.PAUSE, "ts": t0.isoformat(), "detail_json": "{}"},
            {"id": 2, "kind": gate.UNPAUSE, "ts": (t0 + timedelta(minutes=10)).isoformat(), "detail_json": "{}"},
            {"id": 3, "kind": gate.PAUSE, "ts": (t0 + timedelta(minutes=30)).isoformat(),
             "detail_json": json.dumps({"until": (t0 + timedelta(minutes=40)).isoformat()})}]
    w = gate.windows_from_rows(rows)
    assert gate.in_windows(w, t0 + timedelta(minutes=5))
    assert not gate.in_windows(w, t0 + timedelta(minutes=15))
    assert gate.in_windows(w, t0 + timedelta(minutes=35))
    assert not gate.in_windows(w, t0 + timedelta(minutes=45))               # timed pause ran out
    assert gate.in_windows(w, None)                                          # no timestamp = treated as paused


def test_capture_projection(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, watcher_on(cfg))
    rid = seed(db, clock)
    case = capture.build_case(db, cfg, rid, clock.now())
    kinds = [r["kind"] for r in case["records"]]
    assert kinds.count("tap") == 1                                           # Principal's tap only, not the backup's
    assert "shell_paste" in kinds and "instruction" not in kinds             # armed by the fx tap
    own = [r for r in case["records"] if r["kind"] == "own_message"]
    assert [r["text"] for r in own] == ["Done ya Staff A, try now please"]   # the agent's echo is excluded
    assert own[0]["reply_minutes"] == 10.0
    edit = next(r for r in case["records"] if r["kind"] == "edit")
    assert edit["final"].startswith("Done ya") and edit["unchanged"] is False and not edit["via_agent"]
    spent = next(r for r in case["records"] if r["kind"] == "time_spent")
    assert spent["touches"] >= 3


def test_capture_off_and_per_capability(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, cfg)
    rid = seed(db, clock)
    assert capture.build_case(db, cfg, rid, clock.now()) is None             # no consent: nothing at all
    watcher_on(cfg, card_edits=False, shell=False, desk_sessions=False, time_spent=False)
    case = capture.build_case(db, cfg, rid, clock.now())
    assert {r["kind"] for r in case["records"]} == {"own_message"}


def test_paused_stretch_never_exported_even_after_unpause(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, watcher_on(cfg))
    q = make_event("WAVE/00001 question", msg_id=1, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001")
    db.add_message(q, rid)
    db.log_event(gate.PAUSE, None, actor=str(PRINCIPAL_ID))
    clock.advance(minutes=2)
    db.add_message(make_event("private-ish reply while paused", msg_id=2, sender=PRINCIPAL, ts=clock.now()), rid)
    clock.advance(minutes=2)
    db.log_event(gate.UNPAUSE, None, actor=str(PRINCIPAL_ID))
    clock.advance(minutes=2)
    db.add_message(make_event("reply after unpause", msg_id=3, sender=PRINCIPAL, ts=clock.now()), rid)
    texts = [r.get("text") for r in capture.build_case(db, cfg, rid, clock.now())["records"]]
    assert "reply after unpause" in texts and "private-ish reply while paused" not in texts


def _session(path, sid, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"sessionId": sid, "timestamp": f"2026-11-01T12:{i:02d}:00Z", **l})
                              for i, l in enumerate(lines)), encoding="utf-8")


def test_desk_sessions_only_tagged_or_mentioning(tmp_path):
    base = tmp_path / "projects"
    _session(base / "p1" / "s-match.jsonl", "s-match", [
        {"type": "user", "message": {"content": "look at REQ-0001 please"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "checking"},
                                                      {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET OUTPUT"}]}}])
    _session(base / "p1" / "s-near.jsonl", "s-near", [{"type": "user", "message": {"content": "REQ-00010 other"}}])
    _session(base / "p2" / "s-personal.jsonl", "s-personal", [{"type": "user", "message": {"content": "my holiday"}}])
    _session(base / "p2" / "s-tagged.jsonl", "s-tagged", [{"type": "user", "message": {"content": "no ref here"}}])
    found = desk.find_sessions("REQ-0001", projects_dir=base, tagged={"s-tagged": "REQ-0001"})
    assert len(found) == 2
    texts = [t["text"] for s in found for t in s["turns"]]
    assert "SECRET OUTPUT" not in texts and "ls" in texts and "my holiday" not in texts
    assert all(s["session_id"] not in ("s-match", "s-tagged") for s in found)   # hashed


def test_desk_refuses_worker_claude_config(tmp_home):
    base = tmp_home / "claude-config" / "projects"
    _session(base / "p" / "x.jsonl", "x", [{"type": "user", "message": {"content": "REQ-0001"}}])
    assert desk.find_sessions("REQ-0001", projects_dir=base) == []


def test_bot_pause_is_principal_only_and_shows_in_status(tmp_home, cfg, clock, bus):
    db = StateDB(tmp_home / "s.db", clock, watcher_on(cfg))
    bot = FakeBot()
    ctl = BotController(bot, db, cfg, bus, clock, stop_file=lambda: tmp_home / "STOP")

    async def go():
        await ctl.on_command({"user_id": BACKUP_ID, "command": "pause", "args": ""})
        assert db.events(None, gate.PAUSE) == []
        await ctl.on_command({"user_id": PRINCIPAL_ID, "command": "pause", "args": "2h"})
        assert gate.paused_now(db, clock.now())
        await ctl.on_command({"user_id": PRINCIPAL_ID, "command": "status", "args": ""})
        await ctl.on_command({"user_id": PRINCIPAL_ID, "command": "unpause", "args": ""})
        assert not gate.paused_now(db, clock.now())
    asyncio.run(go())
    texts = " ".join(c.text if hasattr(c, "text") else str(c) for c in bot.cards)
    assert "Only Principal can use /pause" in texts and "Watcher: paused" in texts


def test_board_shows_watcher_indicator(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, cfg)
    db.log_event(gate.PAUSE, None, actor=str(PRINCIPAL_ID))
    data = board.collect(tmp_home / "s.db", tmp_home / "data" / "logs", clock.now())
    assert data["watcher"].startswith("paused")
    assert "Watcher:</b> paused" in board.render_html(data, "t" * 20)
    gate.recording_file().parent.mkdir(parents=True, exist_ok=True)
    gate.recording_file().write_text("REQ-0001", encoding="utf-8")
    assert "RECORDING SCREEN (REQ-0001)" in board.collect(tmp_home / "s.db", tmp_home, clock.now())["watcher"]
