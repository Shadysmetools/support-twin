"""Twin Monitor, Sysadmin's side: ingest, alerts, write rules, dev KPIs, reports, delivery, CLI."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from twin_agent import paths
from twin_agent.audit import AuditLog
from twin_agent.db import StateDB
from twin_agent.monitor import alerts as al
from twin_agent.monitor import cli, dev, report
from twin_agent.monitor.analyse import analyse
from twin_agent.monitor.deliver import FakeMonitorSender
from twin_agent.monitor.export import export
from twin_agent.monitor.store import MonitorStore

from monitor_helpers import SALT, add_write, monitor_on, seed

AT = "2026-11-02T10:00:00+00:00"
INSTR = {"kind": "principal_instruction", "by": "principal", "at": AT, "words_sha256": "abc123def456"}
GATE = {"kind": "supervisor_gate", "by": "backup", "at": AT}
LIVE = {"kind": "riley_live", "by": "person-1a2b", "at": AT}
PRE = {"tests_green": True, "ticket": "2607", "judge_review": True}


def _run(cfg, clock, tmp_path, store=None, write=None, **kw):
    db = StateDB(paths.state_db(), clock, cfg)
    rid = seed(db, clock)
    if write:
        add_write(db, rid, **write)
    db.close()
    export(cfg, clock.now(), outbox=tmp_path / "outbox", salt=SALT)
    store = store or MonitorStore(tmp_path / "monitor.db")
    return rid, store, analyse(tmp_path / "outbox", store, clock.now(), cfg, **kw)


def test_ingest_is_idempotent_and_raises_core_alerts(cfg, clock, tmp_path):
    monitor_on(cfg)
    snd = FakeMonitorSender()
    rid, store, res = _run(cfg, clock, tmp_path, sender=snd)
    kinds = {a["kind"] for a in res.alerts}
    assert {"send_blocked", "stalled", "divergence"} <= kinds
    assert res.delivered == len(snd.sent) >= 3 and all("Evidence" in t for t in snd.sent)
    again = analyse(tmp_path / "outbox", store, clock.now(), cfg, sender=snd)
    assert again.new_records == 0 and again.alerts == [] and again.delivered == 0


def test_timer_breach_and_volume(cfg, clock):
    class S:
        def __init__(self, reqs): self.reqs = reqs
        def requests(self): return self.reqs
        def records(self, **k): return []
    now = clock.now()
    t0 = (now - timedelta(hours=3)).isoformat()
    reqs = [{"request_id": "REQ-0001", "ts": t0, "in_hours": False, "twin_first_at": None, "first_reply_at": None},
            {"request_id": "REQ-0002", "ts": t0, "in_hours": True,
             "twin_first_at": (now - timedelta(hours=2, minutes=30)).isoformat()}]
    a = al.request_alerts(S(reqs), now, cfg)
    assert [x["request_id"] for x in a if x["kind"] == "timer_breach"] == ["REQ-0001"]
    days = [{"request_id": f"R{i}", "ts": (now - timedelta(days=d)).isoformat(), "in_hours": True,
             "twin_first_at": (now - timedelta(days=d)).isoformat()} for i, d in
            enumerate([4, 3, 2] + [0] * 12)]
    assert any(x["kind"] == "volume" for x in al.request_alerts(S(days), now, cfg))


def test_audit_break_and_consent_change(cfg, clock, tmp_path):
    monitor_on(cfg)
    log = AuditLog(paths.logs_dir() / "actions.jsonl", clock)
    log.append({"kind": "send"})
    log.append({"kind": "send"})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text(lines[0].replace("send", "sendX") + "\n" + lines[1] + "\n", encoding="utf-8")
    _, store, res = _run(cfg, clock, tmp_path)
    assert "audit_break" in {a["kind"] for a in res.alerts}
    cfg["consent"]["d4_watcher_ref"] = None                     # consent withdrawn: heartbeat only
    clock.advance(minutes=10)
    export(cfg, clock.now(), outbox=tmp_path / "outbox", salt=SALT)
    res2 = analyse(tmp_path / "outbox", store, clock.now(), cfg)
    assert [a for a in res2.alerts if a["kind"] == "consent" and "OFF" in a["text"]]


@pytest.mark.parametrize("w,expect", [
    ({"system": "git", "op": "push", "target": "Stagelatest", "approvals": [INSTR], "preconditions": PRE}, set()),
    ({"system": "git", "op": "push", "target": "fix/2607-x", "approvals": [INSTR], "preconditions": PRE}, set()),
    ({"system": "git", "op": "push", "target": "Dispatch-5", "approvals": [], "preconditions": PRE},
     {"write_without_instruction"}),
    ({"system": "git", "op": "push", "target": "Stagelatest", "preconditions": PRE,
      "approvals": [{**INSTR, "by": "backup"}]}, {"write_without_instruction"}),   # only Principal's own words count
    ({"system": "git", "op": "pr_merge", "target": "main", "approvals": [INSTR, GATE], "preconditions": PRE},
     {"prod_write_unapproved"}),
    ({"system": "git", "op": "pr_merge", "target": "main", "approvals": [GATE, LIVE], "preconditions": PRE}, set()),
    ({"system": "git", "op": "merge", "target": "fix/x-live", "approvals": [INSTR], "preconditions": PRE},
     {"prod_write_unapproved"}),
    ({"system": "git", "op": "push", "target": "random", "approvals": [INSTR], "preconditions": PRE},
     {"out_of_scope_write"}),
    ({"system": "git", "op": "push", "target": "Stagelatest", "approvals": [INSTR], "login": "shared",
      "preconditions": PRE}, {"shared_login_write"}),
    ({"system": "git", "op": "pr_open", "target": "fix/a", "approvals": [INSTR],
      "preconditions": {"tests_green": False, "version_bump": True}}, {"push_precondition", "version_bump",
                                                                       "push_no_ticket"}),
    ({"system": "odoo", "op": "write", "model": "stock.move", "env": "production", "approvals": [INSTR]},
     {"odoo_write", "prod_write_unapproved"}),
    ({"kind": "tool_denied", "tool": "odoo_write"}, {"forbidden_tool"}),
    ({"kind": "write_blocked", "system": "git", "op": "push", "target": "main", "status": "blocked",
      "approvals": [GATE, LIVE], "preconditions": PRE}, {"write_blocked"}),
])
def test_write_rules(w, expect):
    assert {k for _s, k, _t in al.judge_write(w)} == expect
    if "prod_write_unapproved" in expect:
        assert any(s == "critical" for s, k, _t in al.judge_write(w) if k == "prod_write_unapproved")


def test_admin_uid_write_alerts_end_to_end(cfg, clock, tmp_path):
    """Bug e: the authenticated uid recorded on an Acme write must survive the export (WRITE_KEYS carries it)
    and clean_record (an int is not pseudonymised), so the Monitor alerts when a write is made as an admin uid."""
    monitor_on(cfg)
    cfg["monitor"]["odoo"] = {"admin_uids": [1], "pinned_host": "helpdesk.example.com"}
    w = {"system": "odoo", "env": "sme_helpdesk", "op": "write", "model": "helpdesk.ticket", "status": "done",
         "target": "helpdesk.example.com", "uid": 1, "approvals": [INSTR]}
    _, _store, res = _run(cfg, clock, tmp_path, write=w)
    assert "admin_uid_write" in {a["kind"] for a in res.alerts}


def test_write_alert_end_to_end(cfg, clock, tmp_path):
    monitor_on(cfg)
    w = {"system": "git", "op": "pr_merge", "target": "main", "status": "done", "approvals": [INSTR],
         "preconditions": PRE}
    _, store, res = _run(cfg, clock, tmp_path, write=w)
    crit = [a for a in res.alerts if a["kind"] == "prod_write_unapproved"]
    assert crit and crit[0]["severity"] == "critical" and "riley_live + supervisor_gate" in crit[0]["text"]


def _trace(tmp_path):
    d = tmp_path / "trace"
    d.mkdir()
    base = {"author": "Principal (Globex)", "in_main": "", "is_merge": "", "is_revert": "", "is_hotfix": "",
            "other_live_branches": "", "modules": "sme_x", "manifest_version_set": "", "prs": ""}
    rows = [{**base, "sha": "aaaaaaa001", "authored_cairo": "2026-10-26 10:00", "first_parent_of": "main",
             "tickets": ""},
            {**base, "sha": "aaaaaaa002", "authored_cairo": "2026-10-27 10:00", "first_parent_of": "Dispatch-5",
             "tickets": "2607", "manifest_version_set": "sme_x=17.0.1.1.0", "prs": "7"},
            {**base, "sha": "bbbbbbb001", "author": "Other", "authored_cairo": "2026-10-28 10:00",
             "first_parent_of": "main", "tickets": "", "is_revert": "Y"}]
    prs = {"7": {"user": "someone", "base": "Stagelatest", "head": "fix/2607-x-stagelatest",
                 "created_at": "2026-10-27T09:00:00Z", "merged_at": "2026-10-27T10:00:00Z", "merged_by": "someone",
                 "reviews": [], "commit_shas": ["aaaaaaa002" + "0" * 30]},
           "8": {"user": "someone", "base": "main", "head": "fix/2607-x-live", "created_at": "2026-10-28T09:00:00Z",
                 "merged_at": "2026-10-28T22:00:00Z", "merged_by": "someone", "reviews": [],
                 "commit_shas": ["aaaaaaa002" + "0" * 30]}}
    (d / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
    (d / "pr_details.json").write_text(json.dumps(prs), encoding="utf-8")
    (d / "builds.json").write_text(json.dumps([{"branch": "Dispatch-5", "sha": "x", "status": "failed",
                                                "at": "2026-10-27T11:00:00Z"}]), encoding="utf-8")
    return d


def test_dev_alerts_and_kpis(cfg, tmp_path):
    rows = dev.load(_trace(tmp_path), cfg)
    kinds = {a["kind"] for a in dev.dev_alerts(rows)}
    assert {"direct_push_main", "version_bump", "push_no_ticket", "unreviewed_merge", "prod_write_unapproved",
            "build_failed"} <= kinds
    k = dev.dev_kpis(rows, "2026-10-26T00:00:00+00:00", "2026-11-02T00:00:00+00:00")
    assert k["pushes"] == 2 and k["prs_merged"] == 2 and k["prs_reviewed"] == 0
    assert k["staging_to_live_h_median"] == 36.0 and k["reverts_hotfixes_after"] == ["bbbbbbb"]
    assert k["to_production"] == 1 and k["to_production_gated"] == 0 and k["builds"]["failed"] == 1


def test_reports_have_outcome_kpis_evidence_and_baseline(cfg, clock, tmp_path):
    monitor_on(cfg)
    rid, store, _ = _run(cfg, clock, tmp_path, dev_dir=_trace(tmp_path))
    day = clock.now().date()
    d = report.daily_report(store, day, clock.now(), cfg, label="TEST")
    assert "[TEST]" in d and "Outcome KPIs" in d and f"{rid}#msg-" in d and len(d) < 4000
    assert "Odoo helpdesk ratings: 0" in d and "Writes:" in d and "Alerts:" in d
    s = report.scorecard(store, day, clock.now(), cfg)
    assert "Baseline: Principal (Telegram) | 18 min | 13.7 h | 25% | 11%" in s and "126 h" in s and "12.9 d" in s
    assert "Lanes on the dial" in s and "Autopilot share" in s
    t = report.incident(store, rid)
    lines = t.splitlines()[1:]
    assert [x[:16] for x in lines] == sorted(x[:16] for x in lines) and any("ALERT" in x for x in lines) and "Staff A" not in t
    req = store.requests()[0]
    assert req["request_id"] in report.incident(store, req["requester"])
    assert report.incident(store, "REQ-9999").startswith("No request")


def test_delivery_cap_sends_a_summary(cfg, clock, tmp_path):
    monitor_on(cfg)
    cfg["monitor"]["max_alerts_per_run"] = 1
    snd = FakeMonitorSender()
    _, store, res = _run(cfg, clock, tmp_path, sender=snd)
    assert res.delivered == 1 and len(snd.sent) == 2 and "more alert" in snd.sent[1]
    assert store.unsent_alerts() == []


def test_cli_export_analyse_report(cfg, clock, tmp_home, tmp_path, capsys):
    monitor_on(cfg)
    import yaml
    (tmp_home / "config" / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    db = StateDB(paths.state_db(), clock, cfg)
    rid = seed(db, clock)
    db.close()
    c = ["--config", str(tmp_home / "config" / "config.yaml"), "--db", str(tmp_path / "m.db")]
    assert cli.main(c + ["export"]) == 0
    assert cli.main(c + ["analyse", "--bundles", str(tmp_home / "data" / "monitor" / "outbox")]) == 0
    assert cli.main(c + ["--label", "X", "report", "--day", str(clock.now().date())]) == 0
    assert cli.main(c + ["incident", rid]) == 0
    assert cli.main(c + ["scorecard", "--week-end", str(clock.now().date())]) == 0
    out = capsys.readouterr().out
    assert "monitor export" in out and "Outcome KPIs" in out and f"Incident timeline {rid}" in out
