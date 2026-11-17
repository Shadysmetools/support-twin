"""Twin Monitor, Principal's side: export (gate, redaction, read-only, /pause, cursor, writes)."""
from __future__ import annotations

from datetime import timedelta

from twin_agent import paths
from twin_agent.audit import AuditLog
from twin_agent.db import StateDB
from twin_agent.monitor import export as ex
from twin_agent.testing.fakes import PRINCIPAL_ID, BACKUP_ID, GROUP_ID

from monitor_helpers import SALT, add_write, bundle_text, files_hash, monitor_on, records, seed


def _db(cfg, clock):
    return StateDB(paths.state_db(), clock, cfg)


def _export(cfg, clock, tmp_path):
    return ex.export(cfg, clock.now(), outbox=tmp_path / "outbox", salt=SALT)


def test_off_without_consent_writes_heartbeat_only(cfg, clock, tmp_path):
    db = _db(cfg, clock)
    seed(db, clock)
    db.close()
    cfg.setdefault("monitor", {})["enabled"] = True            # monitor on, but no D4
    res = _export(cfg, clock, tmp_path)
    assert res.consent_only and res.records == 0
    head, recs = ex.read_bundle(res.path)
    assert recs == [] and head["consent"]["d4"] is False


def test_bundle_has_every_category_and_no_text_ids_or_names(cfg, clock, tmp_path):
    monitor_on(cfg)
    db = _db(cfg, clock)
    rid = seed(db, clock)
    db.close()
    AuditLog(paths.logs_dir() / "actions.jsonl", clock).append({"kind": "send", "request_id": rid})
    res = _export(cfg, clock, tmp_path)
    text = bundle_text(res.path)
    cats = {r["cat"] for r in records(res.path)}
    assert {"request", "routing", "check", "output", "timer", "audit"} <= cats
    for bad in ("WAVE/00001", "Staff A", "shorter please", "Totally different", str(PRINCIPAL_ID), str(BACKUP_ID),
                str(GROUP_ID), "Principal Test"):
        assert bad not in text, bad
    recs = records(res.path)
    req = next(r for r in recs if r["kind"] == "request")
    assert req["requester"].startswith("requester-") and req["msg"].startswith("msg-")
    assert req["first_reply_by"] == "principal" and req["twin_first_status"] == "would_send"
    assert req["edit"]["similarity"] < 0.5
    taps = [r for r in recs if r["kind"] == "tap"]
    assert {t["actor"] for t in taps} == {"principal", "backup"}
    blocked = next(r for r in recs if r["kind"] == "send" and r["status"] == "blocked")
    assert blocked["guards_blocked"] == ["personal_data"]           # mode guards are not "blocked"
    audit = next(r for r in recs if r["cat"] == "audit" and r["file"] == "actions.jsonl")
    assert audit["ok"] is True and audit["lines"] == 1


def test_export_never_changes_state_db(cfg, clock, tmp_path):
    monitor_on(cfg)
    db = _db(cfg, clock)
    seed(db, clock)
    db.close()
    before = files_hash(paths.state_db())
    _export(cfg, clock, tmp_path)
    _export(cfg, clock, tmp_path)
    assert files_hash(paths.state_db()) == before


def test_pause_drops_principals_actions_but_keeps_the_twins(cfg, clock, tmp_path):
    monitor_on(cfg)
    db = _db(cfg, clock)
    db.log_event("watcher_pause", actor=str(PRINCIPAL_ID))
    rid = seed(db, clock)                                       # everything inside the pause
    db.close()
    recs = records(_export(cfg, clock, tmp_path).path)
    assert not [r for r in recs if r["kind"] in ("tap", "human_reply", "instruction")]
    assert [r for r in recs if r["kind"] == "send" and r["request_id"] == rid]


def test_cursor_exports_each_event_once(cfg, clock, tmp_path):
    monitor_on(cfg)
    db = _db(cfg, clock)
    seed(db, clock)
    db.close()
    first = records(_export(cfg, clock, tmp_path).path)
    clock.advance(minutes=1)
    second = records(_export(cfg, clock, tmp_path).path)
    ev_keys = lambda rs: {r["key"] for r in rs if ":ev:" in r["key"]}   # noqa: E731
    assert ev_keys(first) and not ev_keys(first) & ev_keys(second)


def test_write_events_carry_roles_and_hash_not_words(cfg, clock, tmp_path):
    monitor_on(cfg)
    db = _db(cfg, clock)
    rid = seed(db, clock)
    add_write(db, rid, system="git", op="push", target="Stagelatest", status="done", login="personal",
              approvals=[{"kind": "principal_instruction", "by": str(PRINCIPAL_ID), "at": "2026-11-02T10:00:00+00:00",
                          "words": "push it to staging", "words_sha256": "abc123def456"}],
              preconditions={"tests_green": True, "ticket": "2607"})
    db.close()
    w = next(r for r in records(_export(cfg, clock, tmp_path).path) if r["cat"] == "write")
    assert w["approvals"][0]["by"] == "principal" and "words" not in w["approvals"][0]
    assert w["target"] == "Stagelatest" and w["preconditions"]["ticket"] == "2607"


def test_consent_fingerprint_changes_with_consent(cfg):
    a = ex.consent_state(cfg)
    monitor_on(cfg)
    b = ex.consent_state(cfg)
    assert a["hash"] != b["hash"] and b["d4"] and b["monitor_on"]


def test_close_codes():
    assert ex.close_code("Principal answered; requester acked (text)") == "ack_text"
    assert ex.close_code("Principal answered; requester acked (reaction)") == "ack_reaction"
    assert ex.close_code(None) is None


def test_minutes_between(clock):
    a = clock.now()
    assert ex.minutes_between(a, a + timedelta(minutes=90)) == 90.0


def test_listener_monitor_tick_is_periodic_and_gated(cfg, clock):
    import asyncio
    from types import SimpleNamespace
    from twin_agent import listener
    from twin_agent.bus import Bus
    db = _db(cfg, clock)
    seed(db, clock)
    flow = SimpleNamespace(cfg=cfg, clock=clock, db=db, bus=Bus())
    ops = paths.home() / "data" / "monitor" / "outbox" / "ops"
    asyncio.run(listener.monitor_tick(flow))                        # interval 0 = manual only
    assert not ops.exists()
    cfg["monitor"]["interval_minutes"] = 5
    asyncio.run(listener.monitor_tick(flow))                        # no consent: nothing
    assert not ops.exists()
    monitor_on(cfg)
    flow.monitor_last = None
    asyncio.run(listener.monitor_tick(flow))
    assert len(list(ops.glob("ops-*.jsonl"))) == 1
    clock.advance(minutes=2)
    asyncio.run(listener.monitor_tick(flow))                        # not due yet
    clock.advance(minutes=4)
    asyncio.run(listener.monitor_tick(flow))
    assert len(list(ops.glob("ops-*.jsonl"))) == 2
    db.close()
