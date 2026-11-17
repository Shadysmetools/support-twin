"""aa.py timesheets compute|report  (Principal's laptop).

- `compute [--day YYYY-MM-DD]`: the entries for that Cairo day from his transcripts, taps and work-site visits.
  Prints them; nothing is written (the daily card in the listener is the only path to Odoo). Needs consent D4 and
  `timesheets.enabled`.
- `report [--since D] [--until D]`: hours per ticket, per client, billable vs not, from the local ledger.
"""
from __future__ import annotations

import argparse
from datetime import date

from .. import paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="timesheets")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compute")
    c.add_argument("--day", default=None)
    c.add_argument("--config", default=None)
    r = sub.add_parser("report")
    r.add_argument("--since", default=None)
    r.add_argument("--until", default=None)
    r.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    from ..clock import SystemClock
    from ..config import load_config
    from ..db import StateDB
    from .desk import TimesheetDesk
    from .report import report
    cfg = load_config(a.config)
    clock = SystemClock()
    db = StateDB(paths.state_db(), clock, cfg)
    try:
        desk = TimesheetDesk(db, cfg, None, clock, None)
        if a.cmd == "report":
            print(report(desk.store, since=a.since, until=a.until))
            return 0
        if not desk.allowed(clock.now()):
            print("timesheets: off (needs consent.d4_watcher_ref, timesheets.enabled, and not /paused)")
            return 2
        day = date.fromisoformat(a.day) if a.day else clock.now().date()
        for e in desk.compute(day):
            print(f"{e.day}  {e.label():<28} {e.minutes:6.1f} min  {e.hours:4.2f} h  [{e.how or '-'}]  {e.name}")
        return 0
    finally:
        db.close()
