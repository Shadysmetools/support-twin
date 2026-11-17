"""Fakes for the Teamhub thread tests (not collected as tests): a fake poster agent, a fake Odoo, a ready Ctx.
No network, no real Teamhub, no real Odoo."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from twin_agent.helpdesk.odoo import OdooAdapter
from twin_agent.hub.teamhub_threads import (ApprovalBook, Ctx, TeamhubQueue, Settings, ThreadMap)
from twin_agent.hub.store import MemoryStore

SYSADMIN, EXECUTIVE, STRANGER = "user_sysadmin", "user_executive", "user_stranger"
T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
TEST_TEXT = "The sales quotation report is not working, it shows an error when we print it. Please fix asap."


def make_settings(tmp: Path, **kw) -> Settings:
    base = dict(room_id="room1", workflow_id="wf1", teamhub_exe="teamhub-fake", workdir=str(tmp / "work"),
                state_dir=str(tmp / "state"), wait_seconds=5, poll_seconds=1, trigger_retries=2,
                approvers={SYSADMIN: "Sysadmin", EXECUTIVE: "Executive"}, approver_allowlist=[SYSADMIN, EXECUTIVE],
                default_approver=SYSADMIN, test_partner_id=10, filter={"team_id": 2, "name_like": "TEST ·%"},
                odoo_link_template="https://odoo.example/web#id={tid}&model=helpdesk.ticket&view_type=form")
    base.update(kw)
    return Settings.from_cfg({"teamhub_tickets": base})


class FakeAgent:
    """Stands in for ``teamhub.exe ... trigger``: when triggered it posts every queued op, oldest filename first."""

    def __init__(self, st: Settings, down: int = 0, *, swallow=False, fail_prefix=None, no_ids=False, fail_keys=()):
        self.st, self.down, self.triggers, self.ops, self.n = st, down, 0, [], 0
        self.gateway_up, self.swallow, self.fail_prefix, self.no_ids, self.fail_keys = True, swallow, fail_prefix, no_ids, set(fail_keys)

    def __call__(self, cmd, **kw):
        if cmd[1] == "gateway":
            return SimpleNamespace(returncode=0 if self.gateway_up else 1, stderr="",
                                   stdout="Gateway daemon is running and connected." if self.gateway_up else "not running")
        self.triggers += 1
        if self.triggers <= self.down:
            return SimpleNamespace(returncode=1, stdout="", stderr="gateway down")
        pairs = [(self.st.queue_dir, self.st.results_dir)] + [
            (r.get("queue_dir") or self.st.queue_dir, r.get("results_dir") or self.st.results_dir)
            for r in self.st.owner_rooms.values()]
        for qd, od in dict.fromkeys(pairs):
            self._process(Path(qd), Path(od))
        return SimpleNamespace(returncode=0, stdout="Triggered", stderr="")

    def _process(self, q: Path, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        for f in sorted(q.glob("*.json")) if q.is_dir() else []:
            op = json.loads(f.read_text(encoding="utf-8"))
            self.n += 1
            self.ops.append(op)
            if self.swallow:                              # posted, but the poster died before writing the result
                f.unlink()
                continue
            bad = (self.fail_prefix and op["key"].startswith(self.fail_prefix)) or op["key"] in self.fail_keys
            res = {"key": op["key"], "op": op["op"], "ok": not bad}
            if bad:
                res["error"] = "nothing posted"
            elif op["op"] == "create_thread" and not self.no_ids:
                res["root_id"] = f"root{self.n}"
            elif op["op"] in ("reply", "card") and not self.no_ids:
                res["message_id"] = f"msg{self.n}"
            (out / f"{op['key']}.result.json").write_text(json.dumps(res), encoding="utf-8")
            f.unlink()

    def of(self, kind: str) -> list[dict]:
        return [o for o in self.ops if o["op"] == kind]


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeOdoo:
    """Just enough helpdesk data for the watcher. Reads only; ``writes`` stays empty."""

    def __init__(self, tickets: list[dict]):
        self.tickets = {t["id"]: t for t in tickets}
        self.stages = {1: {"id": 1, "name": "New", "fold": False, "is_close": False},
                       3: {"id": 3, "name": "Solved", "fold": True, "is_close": True},
                       4: {"id": 4, "name": "Cancelled", "fold": False, "is_close": True}}   # fold and is_close can disagree
        self.partners = {10: {"id": 10, "name": "TEST Client (Support Twin)", "is_company": False},
                         11: {"id": 11, "name": "Real Co", "is_company": True},
                         12: {"id": 12, "name": "Some Person", "is_company": False}}
        self.uid, self.url, self.calls, self.writes = 7, "https://odoo.example", [], []

    @staticmethod
    def _ok(row: dict, dom: list) -> bool:
        for f, op, v in dom:
            x = row.get(f)
            x = x[0] if isinstance(x, list) and f.endswith("_id") else x
            if op == "=" and x != v or op == ">" and not x > v or op == "in" and x not in v:
                return False
            if op == "=like" and not str(x).startswith(v.rstrip("%")):
                return False
        return True

    def execute_kw(self, model, method, args, kwargs):
        self.calls.append((model, method))
        if method == "fields_get":
            return {"fold": {}, "is_close": {}, "name": {}}
        table = {"helpdesk.ticket": self.tickets, "helpdesk.stage": self.stages, "res.partner": self.partners}[model]
        rows = [r for r in table.values() if self._ok(r, args[0])]
        rows.sort(key=lambda r: r["id"])
        return rows[: kwargs["limit"]] if kwargs.get("limit") else rows


def ticket(tid: int, *, name="TEST · Demo", text=TEST_TEXT, stage=1, partner=10, owner=False) -> dict:
    return {"id": tid, "name": name, "partner_id": [partner, "x"], "description": f"<p>{text}</p>", "team_id": [2, "HS"],
            "user_id": [owner, "Owner"] if owner else False, "stage_id": [stage, "x"]}


def make_ctx(tmp: Path, tickets: list[dict], *, dry_run=False, down=0, agent_kw=None, **kw):
    st = make_settings(tmp, **kw)
    store, clock = MemoryStore(), Clock()
    agent = FakeAgent(st, down, **(agent_kw or {}))
    q = TeamhubQueue(st, dry_run=dry_run, runner=agent, sleep=lambda s: setattr(clock, "t", clock.t), out=lambda *_: None,
                   clock=_ticker(), store=store, now=clock)
    odoo = FakeOdoo(tickets)
    ctx = Ctx(st=st, store=store, queue=q, threads=ThreadMap(store), approvals=ApprovalBook(store, st, clock),
              odoo=OdooAdapter(odoo), dry_run=dry_run, out=lambda *_: None)
    return ctx, agent, odoo, clock


def _ticker():
    t = [0.0]

    def tick():
        t[0] += 1.0                       # every look at the clock is one second: waits end fast
        return t[0]
    return tick


def press(st: Settings, name: str, *, mid: str, action: str, nonce: str, who=SYSADMIN, text=None, stamp="1") -> Path:
    """Write a submission file the way the poster agent does (raw, unverified)."""
    d = Path(st.results_dir)
    d.mkdir(parents=True, exist_ok=True)
    sub = {"source_message_id": mid, "action_id": action, "value": nonce,
           "submitted_values": {"text": text} if text is not None else {}, "received_at": "2026-11-16T09:01:00Z"}
    if who:
        sub["submitter_user_id"] = who
    p = d / f"submission-{mid}-{stamp}.json"
    p.write_text(json.dumps(sub), encoding="utf-8")
    return p


def later(clock: Clock, **kw) -> None:
    clock.t = clock.t + timedelta(**kw)
