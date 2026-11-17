"""Slices -> timesheet entries: one per Cairo day and ticket, rounded to 0.25 h, with a descriptive name.

Rounding: total minutes of the ticket-day to the NEAREST quarter hour (half up: 7.5 min -> 0.25 h). A ticket-day
under `min_minutes` (default 8) is listed as "too short" and never written. Rounding is per ticket-day, not per
interval, so many short bursts are not each rounded up.

The name follows Principal's own best lines (02h 6.2: "#ticket verb: what was done"): what was done, the evidence and
the result, made only from that time's actions, e.g.
  "#2636 diagnosed: receipt blocked by stray line; traced to stock_picking.py:16; pushed branch fix/2636; result:
   fix pushed for review". It goes through redaction; a prompt topic that looks secret is dropped.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..clock import CAIRO, utc
from ..helpdesk.style import clean
from .activity import Act
from .intervals import Slice

TAP_WORDS = {"tap:ap": "approved the drafted reply", "tap:as": "approved the drafted reply",
             "tap:sd": "sent his edited reply", "tap:hk": "created the ticket", "tap:ed": "edited the draft",
             "tap:ta": "approved timesheets"}


@dataclass
class Entry:
    day: str                       # Cairo date, YYYY-MM-DD
    key: str | None                # T:<n> | U:<id> | REQ-x | None (unattributed)
    minutes: float
    hours: float
    name: str
    how: str | None = None
    sessions: list[str] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return bool(self.key) and self.key[:2] in ("T:", "U:") and self.hours > 0

    def label(self) -> str:
        if not self.key:
            return "not linked to a ticket"
        return f"#{self.key[2:]}" if self.key[:2] in ("T:", "U:") else f"{self.key} (no ticket yet)"


def round_quarter(minutes: float, min_minutes: float = 8) -> float:
    if minutes < min_minutes:
        return 0.0
    return int((minutes + 7.5) // 15) * 0.25


def next_cairo_midnight(t: datetime) -> datetime:
    local = utc(t).astimezone(CAIRO)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return utc(nxt.replace(tzinfo=CAIRO) if nxt.tzinfo is None else nxt)


def cairo_day(t: datetime) -> str:
    return utc(t).astimezone(CAIRO).date().isoformat()


def _uniq(xs):
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


def describe(label: str, acts: list[Act]) -> str:
    acts_ = [x for a in acts for x in a.actions]
    topics = _uniq(a.topic for a in acts if a.human and a.topic)
    reads = _uniq(x[5:] for x in acts_ if x.startswith("read:"))
    reads = [r for r in reads if ":" in r][:2] or reads[:2]
    edits = _uniq(x[5:] for x in acts_ if x.startswith("edit:"))
    pushes = _uniq(x[5:] for x in acts_ if x.startswith("push:"))
    taps = _uniq(TAP_WORDS.get(x) for x in acts_)
    visits = _uniq(x[6:] for x in acts_ if x.startswith("visit:"))
    verb = "fixed" if edits and pushes else "diagnosed" if reads or topics else "reviewed"
    parts = [f"{label} {verb}: {topics[0] if topics else 'the request'}"]
    if reads:
        parts.append("traced to " + ", ".join(reads))
    if edits:
        parts.append("edited " + ", ".join(edits[:3]))
    if "tests" in acts_:
        parts.append("ran the tests")
    if "commit" in acts_ and not pushes:
        parts.append("committed")
    if pushes:
        parts.append(f"pushed branch {pushes[-1]}")
    if "pr_open" in acts_:
        parts.append("opened a PR")
    parts += taps
    if visits:
        parts.append("checked " + ", ".join(visits[:2]))
    result = ("PR opened" if "pr_open" in acts_ else "fix pushed for review" if pushes else
              "reply approved" if any("reply" in t for t in taps) else "ticket created" if "created the ticket" in taps
              else "change made locally, not pushed" if edits else "diagnosis noted")
    return clean("; ".join(parts) + f"; result: {result}")[:240]


def build_entries(slices: list[Slice], attribution: dict, *, min_minutes: float = 8) -> list[Entry]:
    groups: dict[tuple[str, str | None], dict] = defaultdict(lambda: {"min": 0.0, "acts": [], "how": None,
                                                                       "sessions": []})
    for s in slices:
        key, how = attribution.get(id(s.interval), (None, None))
        g = groups[(cairo_day(s.start), key)]
        g["min"] += s.minutes
        g["acts"] += s.acts()
        g["how"] = g["how"] or how
        if s.interval.session not in g["sessions"]:
            g["sessions"].append(s.interval.session)
    out = []
    for (day, key), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "~")):
        e = Entry(day=day, key=key, minutes=round(g["min"], 1), hours=round_quarter(g["min"], min_minutes), name="",
                  how=g["how"], sessions=g["sessions"])
        e.name = describe(e.label() if key else "Unlinked work", g["acts"])
        out.append(e)
    return out
