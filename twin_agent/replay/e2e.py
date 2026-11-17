"""End-to-end DRY RUN: the Telegram export through the whole listener wiring, on a fake clock. Never sends.

Unlike replay.py (Router + timer rules only), this runs the same objects the listener runs: Flow (router,
spawner with FakeClaude, checks, timer handlers), the real Sender (guards, observe mode) and the real
BotController (cards, 10/h cap, one-time codes) on FakeTelegram / FakeBot / FakeGmail. It proves, on real
traffic shapes:
- 0 sends: FakeTelegram.sent, FakeGmail.sent and the sends table (sent / maybe_sent / pending) are all empty;
- the send path really ran: holding replies end as `would_send` (observe), so "0 sends" is not just "nothing tried";
- cards are produced;
- every open request always has a next timer (checked after every step, never repaired by the check).
Principal's Approve tap is simulated on the first few draft cards, to prove a tap in observe mode is still not a send.

Time is walked timer by timer between messages (oldest first), so "now" is right for holding and stale-draft rules.
The run uses a throwaway home (default <repo>/data/replay/e2e-home, gitignored). The report holds numbers only.

CLI: python -I aa.py e2e-dry-run --export <result.json> [--days 14] [--principal-id N] [--approve 5]
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from ..clock import FakeClock
from . import export as X

REPO = Path(__file__).resolve().parents[2]
MAX_STEPS = 200_000                       # safety net: a timer loop that never ends is a bug, not a long run


MARKER = ".e2e-home"                      # written into every home this harness creates


def _safe_to_wipe(home: Path, real_home: Path) -> None:
    """The run starts by deleting its home. Refuse anything that is not a throwaway home made by this harness
    (e.g. --home C:/TwinAgent would delete config, state.db and logs)."""
    target = home.resolve()
    if target == Path(real_home).resolve() or target in Path(real_home).resolve().parents:
        raise SystemExit(f"refusing to use the real agent home as a throwaway e2e home: {home}")
    if target.exists() and any(target.iterdir()) and not (target / MARKER).exists():
        raise SystemExit(f"refusing to wipe {home}: it was not created by e2e-dry-run (no {MARKER} file)")


def detect_principal_id(raw: dict, name_prefix: str) -> int | None:
    """Most frequent user id whose display name starts with people.principal_name. Used only when no id is given."""
    counts: collections.Counter = collections.Counter()
    for m in raw.get("messages", []):
        fid = str(m.get("from_id") or "")
        if m.get("type") == "message" and fid.startswith("user") and str(m.get("from") or "").startswith(name_prefix):
            counts[X._uid(fid)] += 1
    return counts.most_common(1)[0][0] if counts else None


class Harness:
    def __init__(self, cfg: dict, clock: FakeClock, approve_n: int):
        from .. import listener, paths
        from ..bus import Bus
        from ..db import StateDB
        from ..flow import Flow
        paths.ensure_dirs()
        self.cfg, self.clock, self.approve_n = cfg, clock, approve_n
        self.db = StateDB(paths.state_db(), clock, cfg)
        self.bus = Bus(on_error=lambda topic, e: self.errors.append(f"{topic}:{type(e).__name__}"))
        self.adapters = listener._dry_adapters()
        sender = listener._optional_sender(self.db, cfg, self.adapters, clock, self.bus)
        bot_ctl = listener._optional_bot_ctl(self.db, cfg, self.adapters, clock, self.bus)
        self.flow = Flow(self.db, cfg, self.bus, clock, self.adapters, sender=sender, bot_ctl=bot_ctl)
        self.flow.deadman = listener.DeadMan(cfg)
        self.flow.spawner.retry_delay_s = 0
        self.flow.wire()
        listener.wire_desks(self.flow)          # helpdesk desk on FakeOdoo, writes off (observe): prepared only
        self.ticket_taps: set[str] = set()
        self.errors: list[str] = []
        self.violations: list[dict] = []
        self.steps = 0
        self.approved: set[str] = set()
        self.max_open = 0
        self.fired: collections.Counter = collections.Counter()

    async def settle(self) -> None:
        from ..listener import tick_extras
        for _ in range(20):                    # background tasks (workers, checks) can publish more work
            await self.bus.drain()
            if not self.flow.tasks:
                break
            await asyncio.gather(*list(self.flow.tasks), return_exceptions=True)
        await tick_extras(self.flow)           # sender FloodWait queue + cards held by the 10/h cap
        await self.bus.drain()

    def check_invariant(self, where: str) -> None:
        """Every open request has a next timer. Recorded, never repaired here."""
        self.steps += 1
        open_reqs = self.db.open_requests()
        self.max_open = max(self.max_open, len(open_reqs))
        for req in open_reqs:
            if not self.db.future_timers(req["id"]):
                self.violations.append({"request_id": req["id"], "state": req["state"], "at": where,
                                        "now": self.clock.now().isoformat(timespec="minutes")})

    async def advance_to(self, target: datetime) -> None:
        from .. import timers as tm
        from ..clock import latest
        from ..models import parse_ts
        while self.steps < MAX_STEPS:
            due = self.db.due_timers(target)
            if not due:
                break
            # Compare instants, not Cairo wall time: in the repeated hour when summer time ends, two Cairo
            # datetimes compare by wall clock (Python ignores fold), so 23:56+03:00 looks later than 23:01+02:00.
            when = latest(self.clock.now(), parse_ts(due[0]["due_at"]))
            self.clock.set(when)
            for item in tm.tick(self.db, self.flow.policy, when, self.bus):
                self.fired[item["kind"]] += 1
            await self.settle()
            await self.approve_some()
            self.check_invariant("timer")
        if target.timestamp() > self.clock.now().timestamp():
            self.clock.set(target)

    async def approve_some(self) -> None:
        """Principal taps Approve on the first few draft cards (observe mode: must end as would_send, not a send)."""
        if len(self.approved) >= self.approve_n and len(self.ticket_taps) >= self.approve_n:
            return
        principal = int((self.cfg.get("people") or {}).get("principal_tg_user_id") or 0)
        for _, card, datas in list(self.adapters.bot.cards):
            for row in datas:
                for data in row:
                    if "|ap|" in data and data not in self.approved and len(self.approved) < self.approve_n:
                        self.approved.add(data)
                        await self.adapters.bot.press(data, user_id=principal)
                        await self.settle()
                    elif "|hk|" in data and data not in self.ticket_taps and len(self.ticket_taps) < self.approve_n:
                        self.ticket_taps.add(data)       # Principal taps "Create ticket" (observe: a prepared write)
                        await self.adapters.bot.press(data, user_id=principal)
                        await self.settle()


async def _drive(events, h: Harness, tail_hours: int) -> None:
    from ..flow_timers import ensure_global_timers

    async def on_event(ev):
        h.bus.publish("event.in", ev)
    tg = h.adapters.tg_in
    await tg.start(on_event)
    if h.flow.bot_ctl is not None:
        await h.flow.bot_ctl.start()          # as main_async does (FakeBot.start returns at once)
    ensure_global_timers(h.flow)
    for ev in events:
        await h.advance_to(ev.ts)
        await tg.inject(ev)                  # also stored in the fake thread: the sender's live re-read sees it
        await h.settle()
        h.check_invariant("event")
    if events:
        await h.advance_to(events[-1].ts + timedelta(hours=tail_hours))


def holding_latency(db, policy) -> dict:
    """Minutes from the requester's FIRST message to the holding reply record, split by working / out of hours.
    The client-facing targets are holding.in_hours_min / out_of_hours_min (60 / 15)."""
    from ..models import parse_ts
    rows = db._all("SELECT r.created_at AS first_at, MIN(s.created_at) AS held_at FROM sends s "
                   "JOIN requests r ON r.id = s.request_id WHERE s.kind = 'holding' GROUP BY s.request_id")
    out: dict = {"in_hours": [], "out_of_hours": []}
    for r in rows:
        first, held = parse_ts(r["first_at"]), parse_ts(r["held_at"])
        if first and held:
            key = "in_hours" if policy.is_working_time(first) else "out_of_hours"
            out[key].append(round((held - first).total_seconds() / 60, 1))
    return {k: {"n": len(v), "min": min(v) if v else None, "max": max(v) if v else None} for k, v in out.items()}


def _summary(h: Harness, events, days: int, principal_known: bool) -> dict:
    db = h.db
    rows = db._all("SELECT state, lane, COUNT(*) AS n FROM requests GROUP BY state, lane")
    sends = db._all("SELECT status, trigger, kind, COUNT(*) AS n FROM sends GROUP BY status, trigger, kind")
    real = [s for s in sends if s["status"] in ("sent", "maybe_sent", "pending")]
    by_state: dict = collections.Counter()
    by_lane: dict = collections.Counter()
    for r in rows:
        by_state[r["state"]] += r["n"]
        by_lane[r["lane"] or "unclassified"] += r["n"]
    card_kinds = collections.Counter(str(c.kind) for _, c, _ in h.adapters.bot.cards)
    held = len(getattr(h.flow.bot_ctl, "held", []) or [])
    total_requests = sum(by_state.values())
    # Which verification mode each packet's cross_model verdict ran in. In observe this must be single-model or
    # cross-model via the INDEPENDENT in-subscription Claude session - never an external (groq) call.
    cm_modes: dict = collections.Counter()
    for p in db._all("SELECT verdicts_json FROM packets WHERE verdicts_json IS NOT NULL"):
        try:
            for v in json.loads(p["verdicts_json"]):
                if isinstance(v, dict) and v.get("check") == "cross_model":
                    items = v.get("items") or []
                    cm_modes[(items[0] or {}).get("mode") if items else "unknown"] += 1
        except Exception:                               # noqa: BLE001 - a summary tally must never crash the run
            pass
    return {
        "window": {"days": days, "events": len(events),
                   "from": events[0].ts.isoformat(timespec="minutes") if events else None,
                   "to": events[-1].ts.isoformat(timespec="minutes") if events else None,
                   "principal_id_known": principal_known},
        "requests": total_requests, "requests_by_state": dict(by_state), "requests_by_lane": dict(by_lane),
        "max_open_at_once": h.max_open,
        "sends_real": sum(s["n"] for s in real) + len(h.adapters.tg_send.sent) + len(h.adapters.gmail_send.sent),
        "fake_telegram_sent": len(h.adapters.tg_send.sent), "fake_gmail_sent": len(h.adapters.gmail_send.sent),
        "send_rows": {f"{s['status']}/{s['trigger']}/{s['kind']}": s["n"] for s in sends},
        "would_send_holding": sum(s["n"] for s in sends if s["status"] == "would_send" and s["kind"] == "holding"),
        "would_send_total": sum(s["n"] for s in sends if s["status"] == "would_send"),
        "holding_after_first_msg_min": holding_latency(db, h.flow.policy),
        "approve_taps": len(h.approved),
        "cards_posted": len(h.adapters.bot.cards), "cards_held_for_digest": held, "card_kinds": dict(card_kinds),
        "worker_runs": sum(1 for c in h.adapters.claude.calls if c["kind"] == "worker"),
        "check_calls": sum(1 for c in h.adapters.claude.calls if c["kind"] != "worker"),
        "refuter_calls": sum(1 for c in h.adapters.claude.calls if c["kind"] == "refuter"),
        "cross_model_modes": dict(cm_modes),
        "packets": db._one("SELECT COUNT(*) AS n FROM packets")["n"],
        "reaction_events": sum(1 for e in events if e.reaction),
        "closed_by_rule": {r["state"]: r["n"] for r in db._all(
            "SELECT state, COUNT(*) AS n FROM requests WHERE state LIKE 'CLOSED%' GROUP BY state")},
        "acks_without_request": db._one("SELECT COUNT(*) AS n FROM events WHERE kind = 'ack_no_target'")["n"],
        "acks_not_closing": db._one("SELECT COUNT(*) AS n FROM events WHERE kind = 'ack_no_close'")["n"],
        "reopened": db._one("SELECT COUNT(*) AS n FROM events WHERE kind = 'state' AND detail_json LIKE '%\"dst\": \"REOPENED\"%'")["n"],
        "timer_steps": h.steps, "step_cap_hit": h.steps >= MAX_STEPS, "timers_fired": dict(h.fired.most_common()),
        "invariant_violations": len(h.violations),
        "violation_samples": h.violations[:10],
        "handler_errors": collections.Counter(h.errors).most_common(10),
        "db_handler_errors": db._one("SELECT COUNT(*) AS n FROM events WHERE kind = 'handler_error'")["n"],
        "runner": type(h.adapters.claude).__name__,
        **odoo_summary(h),
    }


def odoo_summary(h: Harness) -> dict:
    """Digital Twin's Odoo side: ticket cards, taps, and writes (real ones must be 0 in observe)."""
    desk = getattr(h.flow, "helpdesk", None)
    fake = getattr(getattr(desk, "odoo", None), "t", None)
    ws = h.db._all("SELECT kind, detail_json FROM events WHERE kind IN ('write', 'write_blocked')")
    status = collections.Counter(json.loads(w["detail_json"]).get("status") for w in ws)
    return {"ticket_cards": h.db._one("SELECT COUNT(*) AS n FROM events WHERE kind = 'ticket_offered'")["n"],
            "ticket_taps": len(h.ticket_taps), "odoo_writes_real": len(getattr(fake, "writes", []) or []),
            "odoo_writes_prepared": status.get("prepared", 0), "odoo_writes_blocked": status.get("blocked", 0)}


def run(export_path: Path, *, days: int = 14, home: Path | None = None, principal_id: int | None = None,
        approve_n: int = 5, tail_hours: int = 24) -> dict:
    """Run the dry run in a throwaway home. Returns the summary (numbers only)."""
    from ..config import load_config
    from ..listener import apply_mode
    from .. import paths
    home = Path(home or REPO / "data" / "replay" / "e2e-home")
    _safe_to_wipe(home, paths.home())
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER).write_text("throwaway home for e2e-dry-run; safe to delete", encoding="utf-8")
    old = os.environ.get("TWIN_AGENT_HOME")
    os.environ["TWIN_AGENT_HOME"] = str(home)
    try:
        raw = X.read_raw(export_path)
        cfg = X.replay_cfg(load_config(REPO / "config.example.yaml"), raw)
        cfg.setdefault("helpdesk", {})["enabled"] = True   # ticket cards on fakes; observe keeps writes off
        cfg = apply_mode(cfg, "observe", True)
        people = cfg.setdefault("people", {})
        principal_id = principal_id or int(people.get("principal_tg_user_id") or 0) or \
            detect_principal_id(raw, str(people.get("principal_name") or "Principal"))
        people["principal_tg_user_id"] = int(principal_id or 1)             # the bot needs someone to post cards to
        events = X.load_events(raw, principal_id=principal_id, since=None, days=days, reactions=True)
        clock = FakeClock(events[0].ts if events else datetime.now().astimezone())
        h = Harness(cfg, clock, approve_n)
        try:
            asyncio.run(_drive(events, h, tail_hours))
            summary = _summary(h, events, days, bool(principal_id))
        finally:
            h.db.close()
        out = home.parent / f"e2e-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        summary["report_path"] = str(out)
        return summary
    finally:
        if old is None:
            os.environ.pop("TWIN_AGENT_HOME", None)
        else:
            os.environ["TWIN_AGENT_HOME"] = old


def passed(summary: dict) -> bool:
    return (summary["sends_real"] == 0 and summary.get("odoo_writes_real", 0) == 0
            and summary["invariant_violations"] == 0 and summary["cards_posted"] > 0
            and (summary["requests"] == 0 or summary["would_send_total"] > 0)
            and not summary["step_cap_hit"] and not summary["handler_errors"] and not summary["db_handler_errors"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="e2e-dry-run", description="Whole-listener dry run on a Telegram export.")
    ap.add_argument("--export", type=Path, required=True)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--principal-id", type=int, default=None)
    ap.add_argument("--approve", type=int, default=5, help="simulated Approve taps (observe: still no send)")
    ap.add_argument("--home", type=Path, default=None)
    a = ap.parse_args(argv)
    s = run(a.export, days=a.days, home=a.home, principal_id=a.principal_id, approve_n=a.approve)
    print(json.dumps({k: v for k, v in s.items() if k != "violation_samples"}, indent=1, default=str))
    ok = passed(s)
    print("E2E DRY RUN:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
