"""BUILD 2: automatic descriptive timesheets (synthetic transcripts only)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from twin_agent.bus import Topics
from twin_agent.db import StateDB
from twin_agent.helpdesk.odoo import OdooAdapter
from twin_agent.monitor import alerts as al
from twin_agent.testing.fake_odoo import PRINCIPAL_EMPLOYEE, FakeOdoo
from twin_agent.testing.fakes import PRINCIPAL_ID
from twin_agent.timesheets import attribute, entries, intervals, transcripts
from twin_agent.timesheets.activity import Act
from twin_agent.timesheets.desk import TimesheetDesk
from twin_agent.timesheets.report import report

T0 = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)       # 10:00 Cairo
KW = dict(idle_gap=timedelta(minutes=10), max_unattended=timedelta(minutes=10), tail=timedelta(minutes=1))


def at(m: float) -> datetime:
    return T0 + timedelta(minutes=m)


def human(m, sid="s1", refs=(), topic="", branch=""):
    return Act(ts=at(m), session=sid, human=True, kind="prompt", refs=list(refs), topic=topic, branch=branch)


def tool(m, sid="s1", actions=(), refs=(), files=()):
    return Act(ts=at(m), session=sid, human=False, kind="tool", actions=list(actions), refs=list(refs),
               files=list(files))


# ---------------------------------------------------------------- transcripts (streaming, human detection)

def _jsonl(path: Path, recs: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return path


def _u(m, text, sid="s1", **kw):
    return {"type": "user", "timestamp": at(m).isoformat().replace("+00:00", "Z"), "sessionId": sid,
            "userType": "external", "cwd": kw.get("cwd", "E:/work/globex"), "gitBranch": kw.get("branch", "main"),
            "isSidechain": kw.get("side", False), "message": {"role": "user", "content": text}}


def _a(m, tools, sid="s1", **kw):
    return {"type": "assistant", "timestamp": at(m).isoformat().replace("+00:00", "Z"), "sessionId": sid,
            "cwd": "E:/work/globex", "gitBranch": kw.get("branch", "main"), "isSidechain": kw.get("side", False),
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": n, "input": i} for n, i in tools]}}


def test_transcripts_stream_and_tell_principal_from_tools(tmp_path):
    f = tmp_path / "proj" / "s1.jsonl"
    f.parent.mkdir(parents=True)
    recs = [json.dumps({"type": "api-request-blob", "message": "x" * 50000, "sessionId": "s1"}),
            json.dumps(_u(0, "why is #2636 blocked? receipt WAVE stuck")),
            json.dumps(_a(1, [("Grep", {"pattern": "Purchase Order", "path": "addons/ba"}),
                              ("Read", {"file_path": "addons/ba/models/stock_picking.py", "offset": 16})])),
            json.dumps({**_u(2, ""), "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}),
            json.dumps(_u(3, "<local-command-stdout>done</local-command-stdout>")),
            json.dumps(_a(4, [("Bash", {"command": "git push origin fix/2636-stray-line"})])),
            json.dumps(_u(5, "subagent prompt", side=True)), "not json"]
    f.write_text("\n".join(recs), encoding="utf-8")
    acts = list(transcripts.iter_acts(tmp_path, at(-60), at(60)))
    assert [(a.kind, a.human) for a in acts] == [("prompt", True), ("tool", False), ("tool", False), ("tool", False),
                                                 ("tool", False), ("tool", False)]
    assert acts[0].refs == ["T:2636"] and acts[0].topic.startswith("why is")
    assert "read:stock_picking.py:16" in acts[1].actions and acts[4].actions == ["push:fix/2636-stray-line"]
    assert list(transcripts.iter_acts(tmp_path, at(60), at(120))) == []        # outside the window
    old = tmp_path / "proj" / "old.jsonl"
    _jsonl(old, [_u(0, "old")])
    import os
    os.utime(old, (0, 0))                                                      # mtime before the window: skipped
    assert len(list(transcripts.iter_acts(tmp_path, at(-60), at(60)))) == 6


def test_agent_home_sessions_are_skipped(tmp_path):
    _jsonl(tmp_path / "p" / "w.jsonl", [_u(0, "worker", cwd="C:/TwinAgent/data/work/REQ-0001")])
    assert list(transcripts.iter_acts(tmp_path, at(-1), at(5), skip_cwd=("C:/TwinAgent",))) == []


# ---------------------------------------------------------------- idle gaps and unattended runs

def test_idle_gap_boundary_exactly_10_min_continues_10_01_breaks():
    a = intervals.session_intervals([human(0), human(10)], **KW)
    assert len(a) == 1 and (a[0].end - a[0].start) == timedelta(minutes=11)
    b = intervals.session_intervals([human(0), human(10 + 1 / 60)], **KW)
    assert len(b) == 2 and sum(((i.end - i.start) for i in b), timedelta()) == timedelta(minutes=2)


def test_assistant_activity_alone_is_not_principals_time():
    acts = [human(0)] + [tool(m) for m in range(1, 41)]                     # one prompt, a 40-minute autonomous run
    ivs = intervals.session_intervals(acts, **KW)
    assert len(ivs) == 1 and ivs[0].end == at(10) + timedelta(minutes=1)
    assert intervals.session_intervals([tool(m) for m in range(0, 30)], **KW) == []


def test_parallel_sessions_counted_once_and_given_to_the_foreground():
    s1 = [human(0, "s1", ["T:2636"]), tool(5, "s1"), human(9, "s1"), tool(18, "s1")]
    s2 = [human(6, "s2", ["T:2641"]), human(12, "s2"), tool(16, "s2")]
    ivs = intervals.all_intervals(s1 + s2, **KW)
    slices = intervals.allocate(ivs)
    total = sum(s.minutes for s in slices)
    union = (at(19) - at(0)).total_seconds() / 60                              # 0..18+1 tail, all covered
    assert total == pytest.approx(union)
    owner = {(s.start, s.end): s.interval.session for s in slices}
    assert owner[(at(0), at(6))] == "s1" and owner[(at(6), at(9))] == "s2"     # s2 typed at 6 -> foreground
    assert owner[(at(9), at(12))] == "s1" and owner[(at(12), at(17))] == "s2" and owner[(at(17), at(19))] == "s1"
    # a subagent / sidechain session with no human acts adds nothing
    side = [tool(m, "s1-sub") for m in range(0, 18)]
    assert sum(s.minutes for s in intervals.allocate(intervals.all_intervals(s1 + s2 + side, **KW))) == total


# ---------------------------------------------------------------- attribution and rounding

def test_attribution_order():
    ivs = intervals.all_intervals([
        human(0, "a", ["REQ-0001"]),                                           # request -> its ticket
        human(30, "a"),                                                        # carried from the session
        human(0, "b", branch="fix/2650-x"),                                    # branch alone never bills (#2655)
        Act(ts=at(0), session="c", human=True, kind="prompt", files=["addons/custom_claim_types/x.py"]),
        human(0, "d", ["T:2607", "REQ-0002"]),                                 # explicit ticket wins
        human(0, "e"),                                                         # nothing
        human(0, "f"), tool(1, "f", refs=["T:2636", "T:2636"]),                # tool-only ticket ref never bills (#2655)
        human(0, "g", ["T:2607"]), tool(1, "g", refs=["T:2636", "T:2636"]),    # Principal's ref beats tool noise
        human(0, "h"), tool(1, "h", refs=["REQ-0002"]),                        # tool-only REQ never bills (#2655)
        human(0, "i"), tool(1, "i", files=["addons/custom_claim_types/y.py"]),    # an EDITED path_map file still bills
    ], **KW)
    attr = attribute.attribute(ivs, resolve_req=lambda r: 9001 if r == "REQ-0001" else None,
                               path_map={"custom_claim_types": 2611})
    got = {(iv.session, iv.start): attr[id(iv)] for iv in ivs}
    assert got[("a", at(0))] == ("U:9001", "request") and got[("a", at(30))] == ("U:9001", "session")
    assert got[("b", at(0))] == (None, None) and got[("c", at(0))] == ("T:2611", "files")
    assert got[("d", at(0))] == ("T:2607", "ticket") and got[("e", at(0))] == (None, None)
    assert got[("f", at(0))] == (None, None) and got[("g", at(0))] == ("T:2607", "ticket")
    assert got[("h", at(0))] == (None, None)                                   # tool-only REQ-0002 does not attribute
    assert got[("i", at(0))] == ("T:2611", "files")                            # path_map bills from an edited file


def test_only_edited_files_land_in_activity_files():
    """timesheet #2655: a read/grep records the path for the description but NOT in `activity.files`; only an EDIT
    does, so a path_map module that was merely read or grepped never bills its ticket."""
    read_act = transcripts.acts_from_record(
        _a(0, [("Read", {"file_path": "addons/custom_claim_types/x.py", "offset": 16}),
               ("Grep", {"pattern": "PO", "path": "addons/custom_claim_types"})]))[0]
    edit_act = transcripts.acts_from_record(
        _a(1, [("Edit", {"file_path": "addons/custom_claim_types/x.py"})]))[0]
    assert read_act.files == [] and "read:x.py:16" in read_act.actions
    assert edit_act.files == ["addons/custom_claim_types/x.py"] and "edit:x.py" in edit_act.actions


@pytest.mark.parametrize("minutes,hours", [(0, 0), (7.9, 0), (8, 0.25), (22.4, 0.25), (22.5, 0.5), (52.5, 1.0),
                                           (61, 1.0), (67.5, 1.25)])
def test_rounding_to_quarter_hours(minutes, hours):
    assert entries.round_quarter(minutes) == hours


def test_entries_round_per_ticket_day_and_describe_the_work():
    acts = [human(0, "s1", ["T:2636"], topic="why is receipt blocked"),
            tool(2, "s1", ["read:stock_picking.py:16"]), tool(4, "s1", ["edit:stock_picking.py"]),
            tool(6, "s1", ["push:fix/2636-stray-line"]),
            human(20, "s1"), human(25, "s1")]                                  # 0..7 + 20..26 = 13 min
    ivs = intervals.all_intervals(acts, **KW)
    es = entries.build_entries(intervals.allocate(ivs), attribute.attribute(ivs))
    assert len(es) == 1 and es[0].minutes == 13 and es[0].hours == 0.25
    assert es[0].name == ("#2636 fixed: why is receipt blocked; traced to stock_picking.py:16; edited "
                          "stock_picking.py; pushed branch fix/2636-stray-line; result: fix pushed for review")


def _days(late):
    acts = [Act(ts=late, session="s", human=True, kind="prompt", refs=["T:2600"]),
            Act(ts=late + timedelta(minutes=9), session="s", human=True, kind="prompt")]
    ivs = intervals.all_intervals(acts, **KW)
    slices = intervals.split_at(intervals.allocate(ivs), entries.next_cairo_midnight)
    return {entries.cairo_day(s.start): round(s.minutes, 1) for s in slices}


def test_cairo_midnight_splits_days_and_dst_uses_real_minutes():
    assert _days(datetime(2026, 11, 3, 21, 55, tzinfo=timezone.utc)) == {"2026-11-03": 5.0, "2026-11-04": 5.0}
    # 2026-10-29 23:55 (+03) -> the clock falls back to 23:00 (+02) at 21:00 UTC: still the 29th, 10 real minutes
    assert _days(datetime(2026, 10, 29, 20, 55, tzinfo=timezone.utc)) == {"2026-10-29": 10.0}


# ---------------------------------------------------------------- the desk: card, approval, write, autopilot

def _desk(cfg, clock, bus, tmp_home, *, live=True):
    cfg["consent"]["d4_watcher_ref"] = "D4-test"
    cfg["timesheets"]["enabled"] = True
    if live:
        cfg["run_mode"] = "live"
        cfg["timesheets"]["write_enabled"] = True
    db = StateDB(tmp_home / "data" / "state" / "state.db", clock, cfg)
    fake = FakeOdoo()
    desk = TimesheetDesk(db, cfg, bus, clock, OdooAdapter(fake, writes_on=live), projects_dir=tmp_home / "none")
    cards = []
    bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))
    return db, fake, desk, cards


DAY_ACTS = [human(0, "s1", ["T:2600"], topic="check the sales analysis"), tool(3, "s1", ["read:sale_report.py:40"]),
            human(9, "s1"), human(18, "s1"),                                   # 0..19 = 19 min on #2600
            human(60, "s2", ["REQ-0009"]), human(64, "s2"),                   # REQ without a ticket: shown only
            human(120, "s3", ["T:2601"]), human(129, "s3"), human(138, "s3"), human(147, "s3"), human(156, "s3")]


def _tap(db, action, h):
    nonce = db.issue_approval("-", 1, action, PRINCIPAL_ID, h, datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert db.use_approval(nonce, PRINCIPAL_ID, action, "-")
    return {"action": action, "request_id": None, "user_id": PRINCIPAL_ID, "nonce": nonce}


def _writes(db):
    return [json.loads(e["detail_json"]) for e in db._all("SELECT * FROM events WHERE kind IN ('write','write_blocked')")]


def test_daily_card_then_approve_writes_analytic_lines(cfg, clock, bus, tmp_home):
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.daily(date(2026, 11, 3), acts=DAY_ACTS)
    asyncio.run(bus.drain())
    card = cards[-1]["card"]
    assert [b.action for b in card.buttons[0]] == ["ta", "te", "tr"]
    assert "1. #2600  0.25 h  #2600 diagnosed: check the sales analysis; traced to sale_report.py:40" in card.text
    assert "2. #2601  0.50 h" in card.text and "REQ-0009 (no ticket yet)" in card.text
    assert fake.writes == []                                                   # nothing before the tap
    asyncio.run(desk.on_callback(_tap(db, "ta", cards[-1]["draft_hash"])))
    lines = [w["vals"] for w in fake.writes if w["model"] == "account.analytic.line"]
    assert [(v["unit_amount"], v["helpdesk_ticket_id"], v["employee_id"], v["project_id"]) for v in lines] == \
        [(0.25, 9001, PRINCIPAL_EMPLOYEE, 501), (0.5, 9002, PRINCIPAL_EMPLOYEE, 501)]
    assert all(v["date"] == "2026-11-03" for v in lines)
    ws = _writes(db)
    assert all(w["approvals"][0]["kind"] == "principal_tap" and w["status"] == "done" for w in ws)
    assert all(al.judge_write({**w, "approvals": [{**w["approvals"][0], "by": "principal"}]}) == [] for w in ws)
    rep = report(desk.store)
    assert "#2600: 0.25 h" in rep and "Client Co A (SAMPLE): 0.75 h" in rep and "non-billable: 0.75 h" in rep
    asyncio.run(desk.on_callback(_tap(db, "ta", cards[-2]["draft_hash"] if len(cards) > 1 else "x")))
    assert len([w for w in fake.writes if w["model"] == "account.analytic.line"]) == 2   # never twice


def test_reject_edit_and_observe(cfg, clock, bus, tmp_home):
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home, live=False)
    desk.daily(date(2026, 11, 3), acts=DAY_ACTS)
    asyncio.run(bus.drain())
    first = cards[-1]["draft_hash"]
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "ts", "args": "2 0.75 #2601 checked the import"}))
    asyncio.run(bus.drain())
    assert "2. #2601  0.75 h  #2601 checked the import" in cards[-1]["card"].text
    asyncio.run(desk.on_callback(_tap(db, "ta", first)))                       # the old card: refused
    assert desk.store.entries(status=("approved",)) == []
    asyncio.run(desk.on_callback(_tap(db, "ta", cards[-1]["draft_hash"])))
    assert fake.writes == [] and {w["status"] for w in _writes(db)} == {"prepared"}   # observe: prepared only


def test_reject_writes_nothing(cfg, clock, bus, tmp_home):
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.daily(date(2026, 11, 3), acts=DAY_ACTS)
    asyncio.run(bus.drain())
    asyncio.run(desk.on_callback(_tap(db, "tr", cards[-1]["draft_hash"])))
    assert fake.writes == [] and _writes(db) == [] and desk.store.entries(status=("approved", "written")) == []


def test_autopilot_only_after_14_days_over_90_percent_unchanged(cfg, clock, bus, tmp_home):
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    cfg["timesheets"]["autopilot"] = True
    assert desk.eligible()["eligible"] is False
    for i in range(12):                                                        # 12 approved, unchanged
        db._exec("INSERT INTO ts_cards (day, entries_hash, status, edited, created_at, decided_at) VALUES "
                 "(?,?,?,?,?,?)", (f"d{i}", f"h{i}", "approved", 0, "2026-10-15T10:00:00+00:00",
                                   (clock.now() - timedelta(days=1)).astimezone(timezone.utc).isoformat()))
    assert desk.eligible()["eligible"] is True
    db._exec("UPDATE ts_cards SET edited = 1 WHERE day IN ('d0', 'd1')")      # 10/12 = 83% unchanged
    assert desk.eligible()["eligible"] is False
    db._exec("UPDATE ts_cards SET edited = 0")
    desk.daily(date(2026, 11, 3), acts=DAY_ACTS)
    ws = _writes(db)
    assert ws and all(w["approvals"][0]["kind"] == "timesheet_autopilot" for w in ws)
    cfg["timesheets"]["autopilot"] = False
    assert desk.autopilot_on() is False


def test_paused_time_is_never_counted(cfg, clock, bus, tmp_home):
    db, fake, desk, _ = _desk(cfg, clock, bus, tmp_home)
    db.log_event("watcher_pause", None, actor=str(PRINCIPAL_ID))
    assert desk.allowed(clock.now()) is False


# ---------------------------------------------------------------- review fixes (2026-11-03)

@pytest.mark.parametrize("extra,human", [
    ({"turnOrigin": "human", "promptSource": "typed"}, True),
    ({"turnOrigin": "human", "promptSource": "queued"}, True),
    ({"turnOrigin": "task_notification", "promptSource": "system"}, False),
    ({"turnOrigin": "scheduled", "promptSource": "system"}, False),
    ({"turnOrigin": "peer"}, False),
    ({"turnOrigin": "sdk", "promptSource": "sdk"}, False),
    ({"promptSource": "system"}, False),
    ({"entrypoint": "sdk-cli"}, False),
    ({"isMeta": True}, False),
    ({}, True),                                                    # older format, interactive cli
])
def test_only_prompts_principal_typed_count_as_his(extra, human):
    acts = transcripts.acts_from_record({**_u(0, "why is #2636 blocked"), **extra})
    assert acts[0].human is human
    assert transcripts.acts_from_record(_u(0, "<task-notification>done</task-notification>"))[0].human is False


def test_work_after_the_card_is_logged_next_day_and_never_twice(cfg, clock, bus, tmp_home):
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    day1 = [human(0, "s1", ["T:2600"]), human(9, "s1"), human(18, "s1")]               # 10:00-10:19 Cairo
    desk.daily(date(2026, 11, 3), acts=day1)
    asyncio.run(bus.drain())
    asyncio.run(desk.on_callback(_tap(db, "ta", cards[-1]["draft_hash"])))             # approved at 18:00
    evening = [human(11 * 60, "s9", ["T:2600"]), human(11 * 60 + 9, "s9"), human(11 * 60 + 18, "s9")]  # 21:00 Cairo
    day2 = [human(24 * 60, "s2", ["T:2601"]), human(24 * 60 + 9, "s2")]
    desk.daily(date(2026, 11, 4), acts=day1 + evening + day2)
    asyncio.run(bus.drain())
    text = cards[-1]["card"].text
    # 11-03 #2600 is 19 + 19 = 38 min across the two parts. The ticket-day total rounds ONCE (round_quarter(38) =
    # 0.75 h); 0.25 h was already booked by the approved morning part, so the evening part bills the remaining 0.5 h.
    assert "11-03 #2600  0.50 h" in text and "#2601  0.25 h" in text
    asyncio.run(desk.on_callback(_tap(db, "ta", cards[-1]["draft_hash"])))
    lines = [w["vals"] for w in fake.writes if w["model"] == "account.analytic.line"]
    assert [(v["date"], v["unit_amount"]) for v in lines] == [("2026-11-03", 0.25), ("2026-11-03", 0.5),
                                                             ("2026-11-04", 0.25)]
    desk.daily(date(2026, 11, 4), acts=day1 + evening + day2)                           # again: nothing new
    assert desk.store.entries(status=("proposed",)) == [] or all(
        r["hours"] == 0 for r in desk.store.entries(status=("proposed",)))


def test_ticket_day_total_rounds_once_not_per_part(cfg, clock, bus, tmp_home):
    """Bug f: later same-day work on a decided ticket-day rounds on the ticket-day TOTAL, never per part.
    40 min (0.75 h) already written, then +10 min: the day total is 50 min = 0.75 h, so the new part bills 0.0 h
    (the old per-part code billed round_quarter(10) = 0.25 h, over-counting the day to 1.0 h)."""
    from twin_agent.timesheets.entries import Entry
    db, fake, desk, _ = _desk(cfg, clock, bus, tmp_home)
    store = desk.store
    store.upsert(Entry(day="2026-11-03", key="T:9", minutes=40, hours=0, name="first 40 min"))
    row = [r for r in store.entries("2026-11-03", ("proposed",)) if r["key"] == "T:9"][0]
    assert row["hours"] == 0.75
    store.set(row["id"], status="written")
    store.upsert(Entry(day="2026-11-03", key="T:9", minutes=50, hours=0, name="10 min more"))
    new = [r for r in store.entries("2026-11-03", ("proposed",)) if r["key"] == "T:9"]
    assert len(new) == 1 and new[0]["hours"] == 0.0


def test_paused_stretches_are_dropped_from_real_transcripts(cfg, clock, bus, tmp_home):
    db, fake, desk, _ = _desk(cfg, clock, bus, tmp_home)
    desk.projects_dir = tmp_home / "projects"
    _jsonl(desk.projects_dir / "p" / "s.jsonl", [_u(5, "inside the pause #2600"), _u(40, "after the pause #2600")])
    clock.set(at(0))
    db.log_event("watcher_pause", None, actor=str(PRINCIPAL_ID))
    clock.set(at(30))
    db.log_event("watcher_unpause", None, actor=str(PRINCIPAL_ID))
    clock.set(at(120))
    got = desk.acts(at(-60), at(120))
    assert [a.ts for a in got] == [at(40)]


def test_write_evidence_survives_export_with_a_real_sized_telegram_id(cfg, clock, tmp_home, tmp_path):
    from twin_agent.helpdesk.ledger import approval, instruction, log_write
    from twin_agent.monitor import export as ex
    real_id = 6123456789
    cfg["people"]["principal_tg_user_id"] = real_id
    cfg["consent"]["d4_watcher_ref"] = "D4-test"
    cfg.setdefault("monitor", {})["enabled"] = True
    db = StateDB(tmp_home / "data" / "state" / "state.db", clock, cfg)
    log_write(db, "REQ-0001", op="create", model="helpdesk.ticket", status="done", purpose="helpdesk",
              approvals=[approval("principal_tap", real_id, clock.now(), card_id=1, draft_sha256="a" * 64)])
    log_write(db, "REQ-0001", op="write", model="helpdesk.ticket", status="done", purpose="helpdesk", stage_to=9,
              approvals=[instruction(real_id, clock.now(), "/stage REQ-0001 9")])
    log_write(db, None, op="create", model="account.analytic.line", status="done", purpose="timesheet",
              approvals=[approval("principal_tap", real_id, clock.now(), card_id=2, entries_sha256="b" * 64)])
    db.close()
    res = ex.export(cfg, clock.now(), outbox=tmp_path / "outbox", salt=b"s")
    _head, recs = ex.read_bundle(res.path)
    ws = [r for r in recs if r.get("cat") == "write"]
    assert len(ws) == 3 and all(r["approvals"][0]["by"] == "principal" for r in ws)
    assert all(al.judge_write(r) == [] for r in ws)
    assert str(real_id) not in res.path.read_text(encoding="utf-8")
    assert "words" not in json.dumps(ws).replace("words_sha256", "")
