"""The `Act` record (one thing that happened at one instant) and the non-transcript sources.

- `db_acts(db, principal_id, since, until)`: Principal's own taps, instructions and commands from state.db events.
- `history_acts(visits)`: work-site visits from the Digital Twin browser profile (browser.history).
Refs found in text are normalised: "REQ-0001", "T:2636" (a ticket number), "U:<id>" (a helpdesk record id in a URL).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..clock import utc
from ..models import parse_ts

REQ = re.compile(r"\bREQ-\d{4,}\b")
TICKET_WORD = re.compile(r"\b(?:ticket|tkt|helpdesk)\s*(?:no\.?|number)?\s*#?\s*(\d{3,6})\b", re.I)
TICKET_HASH = re.compile(r"(?<![\w/&])#(\d{4,6})\b")          # "#2636"; PR numbers (#12, #123) are too short
TICKET_URL = re.compile(r"helpdesk[^\s?#]*/(\d+)(?=[/?#\s]|$)")              # .../odoo/helpdesk/2/2636
TICKET_QS = re.compile(r"model=helpdesk\.ticket\S*?[?&#]id=(\d+)|[?&#]id=(\d+)\S*?model=helpdesk\.ticket")


@dataclass
class Act:
    ts: datetime                       # aware; compared as UTC instants
    session: str
    human: bool                        # Principal's own action (prompt, tap, visit) vs assistant / tool activity
    kind: str                          # prompt | tool | assistant | tap | instruction | command | visit
    refs: list[str] = field(default_factory=list)
    branch: str = ""
    cwd: str = ""
    files: list[str] = field(default_factory=list)     # EDITED paths only (reads/greps are not billed) - see transcripts
    actions: list[str] = field(default_factory=list)   # summariser tokens, e.g. "read:x.py:16", "push:fix/2636"
    topic: str = ""                    # a few redacted words of a prompt (for the description)


def refs_in(text: str, *, url: bool = False) -> list[str]:
    t = str(text or "")
    out = [m.group(0) for m in REQ.finditer(t)]
    out += [f"T:{m.group(1)}" for m in TICKET_WORD.finditer(t)]
    out += [f"T:{m.group(1)}" for m in TICKET_HASH.finditer(t)]
    if url or "helpdesk" in t:
        out += [f"U:{m.group(1)}" for m in TICKET_URL.finditer(t)]
        out += [f"U:{m.group(1) or m.group(2)}" for m in TICKET_QS.finditer(t)]
    seen: list[str] = []
    for r in out:
        if r not in seen:
            seen.append(r)
    return seen


def in_window(ts: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    if ts is None:
        return False
    t = utc(ts)
    return (since is None or t >= utc(since)) and (until is None or t < utc(until))


def db_acts(db, principal_id: int, since: datetime, until: datetime) -> list[Act]:
    """Principal's taps / instructions / commands. Only his own id counts; text never leaves this function."""
    out: list[Act] = []
    if not principal_id:
        return out
    rows = db._all("SELECT ts, kind, request_id, detail_json FROM events WHERE actor = ? AND kind IN "
                   "('button', 'instruction', 'command') ORDER BY id", (str(principal_id),))
    for r in rows:
        ts = parse_ts(r["ts"])
        if not in_window(ts, since, until):
            continue
        try:
            d = json.loads(r.get("detail_json") or "{}")
        except ValueError:
            d = {}
        refs = [r["request_id"]] if r.get("request_id") else []
        if r["kind"] == "command":
            refs += refs_in(str(d.get("args") or ""))
        act = {"button": f"tap:{d.get('action')}", "instruction": "instruction",
               "command": f"command:{d.get('command')}"}[r["kind"]]
        out.append(Act(ts=ts, session="bot", human=True, kind={"button": "tap"}.get(r["kind"], r["kind"]), refs=refs,
                       actions=[act]))
    return out


def history_acts(visits: list[dict]) -> list[Act]:
    """Browser visits ({ts, domain, path, title}) -> human acts in a 'browser' session."""
    out = []
    for v in visits:
        path = str(v.get("path") or "")
        refs = refs_in(f"{v.get('domain')}{path}", url=True) + refs_in(str(v.get("title") or ""))
        out.append(Act(ts=v["ts"], session="browser", human=True, kind="visit", refs=refs,
                       actions=[f"visit:{v.get('domain')}"]))
    return out
