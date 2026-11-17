"""The timesheet desk: compute a day, the daily card to Principal, his taps and /ts edits, the writer, the autopilot.

- Gate: consent D4 (`consent.d4_watcher_ref`) + `timesheets.enabled`. Acts inside a /pause window are dropped; if
  the pause windows cannot be read, nothing is counted (fail closed).
- The card lists the writable entries with [Approve all] [Edit] [Reject]; the tap is bound to the sha256 of exactly
  those rows. Edit = `/ts <n> <hours> [new description]` or `/ts <n> drop`, then a fresh card. Unapproved entries
  are never written.
- Autopilot (`timesheets.autopilot`, off): only when eligible = at least `autopilot_days` (14) of history, at least
  `autopilot_min_cards` decided cards in that window and more than `autopilot_share` (90%) approved unchanged.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from .. import paths
from ..bus import Topics
from ..clock import CAIRO, utc
from ..helpdesk.ledger import approval
from ..helpdesk.odoo import writes_allowed
from ..models import Button, Card, CardKind, parse_ts
from . import activity, attribute, entries, intervals, transcripts
from .store import TimesheetStore
from .writer import TimesheetWriter

TS_ACTIONS = frozenset({"ta", "te", "tr"})
MAX_ROWS = 10                                       # 10 lines x 240 chars stay under the 3400-char card limit


def _cfg(cfg: dict) -> dict:
    return cfg.get("timesheets") or {}


def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=CAIRO)
    return utc(start), entries.next_cairo_midnight(start)


class TimesheetDesk:
    def __init__(self, db, cfg: dict, bus, clock, odoo=None, *, ledger_of=None, projects_dir: Path | None = None):
        self.db, self.cfg, self.bus, self.clock, self.odoo = db, cfg, bus, clock, odoo
        self.store = TimesheetStore(db)
        self.ledger = ledger_of
        self.principal_id = int((cfg.get("people") or {}).get("principal_tg_user_id") or 0)
        t = _cfg(cfg)
        self.projects_dir = Path(projects_dir or t.get("projects_dir") or Path.home() / ".claude" / "projects")
        write_on = writes_allowed(cfg, "timesheets") and bool(getattr(odoo, "writes_on", False))
        self.writer = TimesheetWriter(db, self.store, odoo, write_on=write_on)
        self.visits = None                                 # callable(since, until) -> [visit]; set by the wiring

    def wire(self) -> None:
        self.bus.subscribe(Topics.CALLBACK_IN, self.on_callback)
        self.bus.subscribe(Topics.COMMAND_IN, self.on_command)

    def allowed(self, now: datetime) -> bool:
        from ..watcher.gate import consent_ok, paused_now
        return consent_ok(self.cfg) and _cfg(self.cfg).get("enabled") is True and not paused_now(self.db, now)

    # ---------------------------------------------------------------- compute

    def resolve_req(self, rid: str) -> int | None:
        tid = self.ledger.ticket_of(rid) if self.ledger is not None else None
        if tid:
            return tid
        req = self.db.get_request(rid)
        return int(req["ticket_id"]) if req and str(req.get("ticket_id") or "").isdigit() else None

    def acts(self, since: datetime, until: datetime) -> list:
        from ..watcher.gate import in_windows, pause_windows
        acts = list(transcripts.iter_acts(self.projects_dir, since, until, skip_cwd=(str(paths.home()),)))
        acts += activity.db_acts(self.db, self.principal_id, since, until)
        if self.visits is not None:
            acts += activity.history_acts(self.visits(since, until))
        windows = pause_windows(self.db, self.clock.now())
        if windows is None:
            return []                                      # cannot read /pause: count nothing
        return [a for a in acts if not in_windows(windows, a.ts)]

    def compute(self, day: date, acts: list | None = None) -> list[entries.Entry]:
        t = _cfg(self.cfg)
        since, until = day_window(day)
        acts = self.acts(since, until) if acts is None else [a for a in acts if activity.in_window(a.ts, since, until)]
        ivs = intervals.all_intervals(acts, idle_gap=timedelta(minutes=float(t.get("idle_gap_minutes", 10))),
                                      max_unattended=timedelta(minutes=float(t.get("max_unattended_minutes", 10))),
                                      tail=timedelta(minutes=float(t.get("tail_minutes", 1))))
        slices = intervals.split_at(intervals.allocate(ivs), entries.next_cairo_midnight)
        attr = attribute.attribute(ivs, resolve_req=self.resolve_req, path_map=t.get("path_map") or {})
        return entries.build_entries(slices, attr, min_minutes=float(t.get("min_minutes", 8)))

    # ---------------------------------------------------------------- the daily card

    def maybe_daily_card(self) -> int | None:
        now = self.clock.now()
        local = utc(now).astimezone(CAIRO)
        if local.hour < int(_cfg(self.cfg).get("card_hour", 18)) or self.db.get_cursor("timesheets_card") == \
                local.date().isoformat() or not self.allowed(now):
            return None
        self.db.set_cursor("timesheets_card", local.date().isoformat())
        return self.daily(local.date())

    def daily(self, day: date, acts: list | None = None) -> int | None:
        """Yesterday is recomputed too, so work done after yesterday's card (evening, out of hours) is not lost."""
        mins = float(_cfg(self.cfg).get("min_minutes", 8))
        for d in (day - timedelta(days=1), day):
            for e in self.compute(d, acts):
                self.store.upsert(e, min_minutes=mins)
        return self.post_card(day.isoformat())

    def post_card(self, day: str, *, edited: bool = False) -> int | None:
        all_rows = [r for r in self.store.entries(None, ("proposed",)) if r["day"] <= day]
        rows = [r for r in all_rows if r["key"][:2] in ("T:", "U:") and r["hours"] > 0][:MAX_ROWS]
        all_rows = [r for r in all_rows if r["day"] == day or r in rows]
        if not rows:
            return None
        cid, h = self.store.new_card(day, rows, edited=edited)
        if self.autopilot_on() and self.eligible()["eligible"] and not edited:
            stats = self.eligible()
            self.approve(cid, [approval("timesheet_autopilot", "autopilot", self.clock.now(), entries_sha256=h,
                                        share=stats["share"], cards=stats["cards"])], autopilot=True)
            self._card(f"Autopilot logged {sum(r['hours'] for r in rows):.2f} h for {day} "
                       f"({stats['share']:.0%} of {stats['cards']} cards approved unchanged in {stats['days']} days).")
            return cid
        self._card(self.card_text(day, rows, all_rows), [[Button("Approve all", "ta"), Button("Edit", "te"),
                                                          Button("Reject", "tr")]], draft_hash=h)
        return cid

    def card_text(self, day: str, rows: list[dict], all_rows: list[dict]) -> str:
        t = _cfg(self.cfg)
        lines = [f"Timesheet {day} (Cairo): your active time from sessions, taps and work sites. Gaps over "
                 f"{t.get('idle_gap_minutes', 10)} min are left out; parallel sessions are counted once."]
        for i, r in enumerate(rows, 1):
            when = f"{r['day'][5:]} " if r["day"] != day else ""
            lines.append(f"{i}. {when}#{r['key'][2:]}  {r['hours']:.2f} h  {r['name'][:240]}")
        lines.append(f"Total to log: {sum(r['hours'] for r in rows):.2f} h on {len(rows)} tickets.")
        rest = [r for r in all_rows if r not in rows]
        if rest:
            bits = [f"{'not linked to a ticket' if r['key'] == '-' else r['key'] + (' (too short)' if r['hours'] == 0 and r['key'][:2] in ('T:', 'U:') else ' (no ticket yet)')}"
                    f" {r['minutes']:.0f} min" for r in rest]
            lines.append("Not logged: " + "; ".join(bits[:6]))
        lines.append("Approve all writes these lines to Odoo as your timesheets. Edit: /ts 2 0.75 [new text] or "
                     "/ts 2 drop.")
        return "\n".join(lines)

    def _card(self, text: str, buttons=None, *, draft_hash: str = "") -> None:
        c = Card(kind=CardKind.timesheet if buttons else CardKind.info, text=text[:3400], buttons=buttons or [])
        self.bus.publish(Topics.CARD_OUT, {"card": c, "draft_hash": draft_hash} if draft_hash else c)

    # ---------------------------------------------------------------- taps and edits

    async def on_callback(self, p: dict) -> None:
        action, uid, nonce = p.get("action"), int(p.get("user_id") or 0), p.get("nonce")
        if action not in TS_ACTIONS or uid != self.principal_id or not nonce:
            return
        row = self.db.get_approval(nonce)
        if not row or not row.get("used_at") or int(row.get("used_by") or 0) != uid:
            return
        card = self.store.card_by_hash(row.get("draft_hash") or "")
        if not card or card["status"] != "offered":
            self._card("That timesheet card is out of date or already decided. Nothing was written.")
            return
        if action == "tr":
            self.store.decide_card(card["id"], "rejected")
            for r in self.store.card_rows(card["id"]):
                self.store.set(r["id"], status="rejected", decided_at=self.db._now())
            self._card(f"Timesheet {card['day']} rejected. Nothing was written.")
        elif action == "te":
            self._card("Edit a line: /ts <n> <hours> [new description], or /ts <n> drop. Then tap Approve all on "
                       "the new card.")
        else:
            self.approve(card["id"], [approval("principal_tap", uid, self.clock.now(), card_id=row.get("card_id"),
                                               entries_sha256=card["entries_hash"])])

    def approve(self, card_id: int, approvals: list, *, autopilot: bool = False) -> dict:
        rows = self.store.card_rows(card_id)
        if self.store.card_hash(rows) != approvals[0].get("entries_sha256"):
            self._card("The timesheet changed after the card was made. Nothing was written; see the new card.")
            return {"written": 0, "prepared": 0, "blocked": len(rows)}
        for r in rows:
            self.store.set(r["id"], status="approved", decided_at=self.db._now())
        self.store.decide_card(card_id, "approved", autopilot=autopilot)
        res = self.writer.write(self.store.card_rows(card_id), approvals)
        mode = "written to Odoo" if res["written"] else "prepared (observe: nothing written)" if res["prepared"] else "blocked"
        self._card(f"Timesheet approved: {len(rows)} lines {mode}." + (f" {res['blocked']} blocked." if res["blocked"] and mode != "blocked" else ""))
        return res

    async def on_command(self, p: dict) -> None:
        if int(p.get("user_id") or 0) != self.principal_id or p.get("command") != "ts":
            return
        args = str(p.get("args") or "").split(maxsplit=2)
        card = self.store.open_card()
        if not card or len(args) < 2 or not args[0].isdigit():
            self._card("Use /ts <n> <hours> [new description] or /ts <n> drop, on the open timesheet card.")
            return
        rows = self.store.card_rows(card["id"])
        n = int(args[0])
        if not 1 <= n <= len(rows):
            self._card(f"There is no line {n} on the open card.")
            return
        r = rows[n - 1]
        if args[1].lower() == "drop":
            self.store.set(r["id"], status="rejected", edited=1)
        else:
            try:
                hours = float(args[1])
            except ValueError:
                hours = -1
            if not (0 < hours <= 12 and hours * 4 == int(hours * 4)):
                self._card("Hours must be a multiple of 0.25, at most 12.")
                return
            fields = {"hours": hours, "edited": 1}
            if len(args) > 2:
                from ..helpdesk.style import clean
                fields["name"] = clean(args[2])[:240]
            self.store.set(r["id"], **fields)
        self.store.decide_card(card["id"], "superseded")
        self.post_card(card["day"], edited=True)

    # ---------------------------------------------------------------- autopilot

    def autopilot_on(self) -> bool:
        return _cfg(self.cfg).get("autopilot") is True

    def eligible(self) -> dict:
        t = _cfg(self.cfg)
        days, need, share_min = int(t.get("autopilot_days", 14)), int(t.get("autopilot_min_cards", 10)), \
            float(t.get("autopilot_share", 0.9))
        now = utc(self.clock.now())
        cards = [c for c in self.store.cards() if c["status"] in ("approved", "rejected") and not c["autopilot"]]
        first = min((parse_ts(c["created_at"]) for c in self.store.cards()), default=None)
        recent = [c for c in cards if parse_ts(c["decided_at"]) and utc(parse_ts(c["decided_at"])) >= now - timedelta(days=days)]
        unchanged = [c for c in recent if c["status"] == "approved" and not c["edited"]]
        share = len(unchanged) / len(recent) if recent else 0.0
        ok = bool(first and utc(first) <= now - timedelta(days=days) and len(recent) >= need and share > share_min)
        return {"eligible": ok, "share": share, "cards": len(recent), "days": days}
