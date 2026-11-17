"""Which ticket an interval of work belongs to, and why.

Keys: "T:<n>" a ticket number as people write it (#2636), "U:<id>" a helpdesk.ticket record id (from a URL or the
helpdesk ledger), "REQ-0001" a request with no ticket yet (shown, never written). In order:
1. `ticket`  an explicit ticket ref in Principal's own actions (prompt, tap, visit, command);
2. `request` a REQ ref in Principal's own actions (prompt, card tap, the worker folder in cwd) -> its ticket;
3. `session` the ref an earlier interval of the same session was attributed to;
4. `files`   a `timesheets.path_map` fragment in a file the twin EDITED (`activity.files` holds edited paths only).
Nothing matched = unattributed (shown on the card, never written).

Deliberately NOT used (ticket #2655 - they billed the wrong ticket):
- the git branch name: a branch outlives its ticket, so "fix/2636-..." kept collecting unrelated work;
- a REQ or ticket number that appears only in tool activity: grepping a file or reading code that mentions #2636
  (or REQ-0002) is not working on it - a REQ bills only when it is in Principal's OWN acts (prompt / tap / worker cwd);
- a `path_map` path that was only READ: `transcripts` records a file in `activity.files` only when the twin
  EDITED it, so reading or grepping a mapped module no longer bills its ticket.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

from .intervals import Interval


def _pick(refs: list[str]) -> str | None:
    return Counter(refs).most_common(1)[0][0] if refs else None


def attribute_one(iv: Interval, *, resolve_req: Callable[[str], int | None], carry: str | None,
                  path_map: dict[str, object]) -> tuple[str | None, str | None]:
    human = [r for a in iv.acts if a.human for r in a.refs]      # only Principal's own refs bill; tool refs never do
    t = _pick([r for r in human if r[:2] in ("T:", "U:")])
    if t:
        return t, "ticket"
    reqs = [r for r in human if r.startswith("REQ-")]
    if reqs:
        rid = _pick(reqs)
        tid = resolve_req(rid)
        return (f"U:{tid}" if tid else rid), "request"
    if carry:
        return carry, "session"
    for a in iv.acts:
        for f in a.files:
            for frag, ticket in (path_map or {}).items():
                if frag and frag.lower() in f.lower().replace("\\", "/"):
                    return f"T:{ticket}", "files"
    return None, None


def attribute(intervals: list[Interval], *, resolve_req: Callable[[str], int | None] = lambda r: None,
              path_map: dict | None = None) -> dict[int, tuple[str | None, str | None]]:
    """id(interval) -> (key, how). Intervals are taken in time order per session so the carry is causal."""
    out: dict[int, tuple[str | None, str | None]] = {}
    carry: dict[str, str | None] = {}
    for iv in sorted(intervals, key=lambda i: (i.session, i.start)):
        key, how = attribute_one(iv, resolve_req=resolve_req, carry=carry.get(iv.session), path_map=path_map or {})
        out[id(iv)] = (key, how)
        if key and not key.startswith("REQ-"):
            carry[iv.session] = key
    return out
