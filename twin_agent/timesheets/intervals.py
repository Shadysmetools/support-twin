"""Active intervals per session, then one union timeline where overlapping time is counted once.

`session_intervals(acts, idle_gap, max_unattended, tail)`:
- an interval STARTS only at one of Principal's own acts (human=True);
- it continues through later acts while the gap to the previous act is <= idle_gap (a gap of exactly idle_gap is
  still active; one second more is idle) AND, for assistant / tool acts, while they come within max_unattended of
  his last own act;
- it ENDS at its last act plus `tail` (time to read the last output), merged with an overlapping next interval.

`allocate(intervals)`: elementary slices between all interval edges. A slice covered by several sessions goes to
the foreground one = the session whose latest own act at or before the slice start is the most recent (ties: the
session id, so the result is deterministic). Each minute is counted once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..clock import utc
from .activity import Act


@dataclass
class Interval:
    session: str
    start: datetime
    end: datetime
    acts: list[Act] = field(default_factory=list)

    def last_human_at(self, t: datetime) -> datetime:
        hs = [utc(a.ts) for a in self.acts if a.human and utc(a.ts) <= utc(t)]
        return max(hs) if hs else utc(self.start)


@dataclass
class Slice:
    start: datetime
    end: datetime
    interval: Interval

    @property
    def minutes(self) -> float:
        return (utc(self.end) - utc(self.start)).total_seconds() / 60

    def acts(self) -> list[Act]:
        """The interval's acts inside this slice (the slice owns the evidence of its own minutes)."""
        s, e = utc(self.start), utc(self.end)
        inside = [a for a in self.interval.acts if s <= utc(a.ts) < e]
        return inside or [a for a in self.interval.acts if utc(a.ts) <= e][-1:]


def session_intervals(acts: list[Act], *, idle_gap: timedelta, max_unattended: timedelta,
                      tail: timedelta) -> list[Interval]:
    out: list[Interval] = []
    cur: Interval | None = None
    last = last_human = None
    for a in sorted(acts, key=lambda x: (utc(x.ts), not x.human)):
        t = utc(a.ts)
        if cur is not None and (t - last > idle_gap or (not a.human and t - last_human > max_unattended)):
            cur.end = last + tail
            out.append(cur)
            cur = None
        if cur is None:
            if not a.human:
                continue                              # assistant activity alone never starts Principal's time
            cur = Interval(session=a.session, start=t, end=t)
            last_human = t
        cur.acts.append(a)
        last = t
        if a.human:
            last_human = t
    if cur is not None:
        cur.end = last + tail
        out.append(cur)
    merged: list[Interval] = []
    for iv in out:                                    # a tail that runs into the next interval: one interval
        if merged and iv.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, iv.end)
            merged[-1].acts += iv.acts
        else:
            merged.append(iv)
    return merged


def all_intervals(acts: list[Act], **kw) -> list[Interval]:
    by: dict[str, list[Act]] = {}
    for a in acts:
        by.setdefault(a.session, []).append(a)
    return [iv for sid in sorted(by) for iv in session_intervals(by[sid], **kw)]


def allocate(intervals: list[Interval]) -> list[Slice]:
    edges = sorted({utc(i.start) for i in intervals} | {utc(i.end) for i in intervals}
                   | {utc(a.ts) for i in intervals for a in i.acts if a.human})   # foreground can switch here
    raw: list[Slice] = []
    for a, b in zip(edges, edges[1:]):
        cover = [i for i in intervals if utc(i.start) <= a and utc(i.end) >= b]
        if not cover:
            continue
        fg = max(cover, key=lambda i: (i.last_human_at(a), i.session))
        if raw and raw[-1].interval is fg and raw[-1].end == a:
            raw[-1].end = b
        else:
            raw.append(Slice(a, b, fg))
    return raw


def split_at(slices: list[Slice], cut) -> list[Slice]:
    """Split slices at day boundaries. `cut(t)` returns the next boundary instant after t (UTC)."""
    out = []
    for s in slices:
        a = utc(s.start)
        while True:
            b = min(utc(s.end), cut(a))
            out.append(Slice(a, b, s.interval))
            if b >= utc(s.end):
                break
            a = b
    return [s for s in out if s.minutes > 0]
