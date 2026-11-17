"""Tests for replay/ (KB role, built by guards-board-replay): offline, capped, 0 sends, aggregates only."""
from __future__ import annotations

import json
from pathlib import Path

from twin_agent.replay import replay
from twin_agent.testing.fakes import PRINCIPAL_ID, FakeClaude


def test_load_export_marks_principal_and_window(export_path):
    evs = replay.load_export(export_path, days=3650, principal_id=PRINCIPAL_ID)
    assert evs and all(e.ts.tzinfo for e in evs)
    assert any(e.sender.is_principal for e in evs) and any(not e.sender.is_principal for e in evs)
    assert evs == sorted(evs, key=lambda e: e.ts)
    assert replay.load_export(export_path, days=0, principal_id=PRINCIPAL_ID) == [e for e in evs if e.ts == evs[-1].ts]


def test_anonymous_group_posts_count_as_principal(tmp_path):
    raw = {"name": "G", "id": 9, "messages": [
        {"id": 1, "type": "service", "action": "topic_created", "title": "Stores", "date": "2026-10-01T10:00:00"},
        {"id": 2, "type": "message", "date": "2026-10-01T11:00:00", "from": "G", "from_id": "channel9",
         "text": [{"type": "plain", "text": "Try again now"}], "reply_to_message_id": 1}]}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    ev = replay.load_export(p, days=30)[0]
    assert ev.sender.is_principal and ev.topic_id == 1 and ev.topic == "Stores" and ev.text == "Try again now"


def test_replay_end_to_end(tmp_home, export_path, clock):
    fc = FakeClaude()
    rep = replay.run(export_path, days=3650, max_requests=99, runner=fc, home=tmp_home, clock=clock,
                     principal_id=PRINCIPAL_ID)
    assert rep.sends == 0
    assert rep.requests >= 1 and rep.cards >= rep.requests
    assert len(fc.calls) == rep.worker_runs <= replay.HARD_MAX
    assert rep.drafts >= 1 and rep.window["runner"] == "FakeClaude"
    assert sum(v["requests"] for v in rep.by_lane.values()) == rep.requests
    md = Path(rep.report_path)
    assert md.exists() and md.parent == tmp_home / "data" / "replay"
    body = md.read_text(encoding="utf-8") + next(md.parent.glob("replay-*.json")).read_text(encoding="utf-8")
    assert "Staff A" not in body                       # no staff names or texts in the report
    assert not (tmp_home / "data" / "state" / "state.db").exists()   # the live state.db is never touched


def test_worker_cap_is_hard(tmp_home, export_path, clock):
    fc = FakeClaude()
    rep = replay.run(export_path, days=3650, max_requests=0, runner=fc, home=tmp_home, clock=clock, principal_id=PRINCIPAL_ID)
    assert fc.calls == [] and rep.worker_runs == 0 and rep.drafts == 0


def test_same_substance_estimate():
    assert replay.same_substance("Try again now.", "try again now")
    assert not replay.same_substance("Send the batch number please.", "Try again now")
