"""`python -I aa.py monitor <sub>`: export | analyse | report | incident | scorecard.

Principal's laptop: export (redacted ops bundle into data/monitor/outbox; `--sync` pushes that outbox, its own git
repo, when monitor.sync is configured and live). Sysadmin's device: analyse (bundles -> monitor.db + alerts to his
bot with --send), report (daily), incident REQ-0142 | ticket | requester alias, scorecard (weekly).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from ..clock import CAIRO, SystemClock
from ..models import parse_ts


def _load(p):
    from ..config import load_config
    return load_config(p)


def _now(s: str | None) -> datetime:
    return parse_ts(s) if s else SystemClock().now()


def _sender(cfg, send: bool):
    from .deliver import FakeMonitorSender, MonitorRefused, make_sender
    if not send:
        return None
    try:
        return make_sender(cfg)
    except MonitorRefused as e:
        print(f"not delivered: {e} (printed only)")
        return FakeMonitorSender()


def sync_outbox(cfg, outbox: Path, now) -> str:
    from ..watcher.sync import FakeSync, GitSync
    from .export import monitor_on
    s = (cfg.get("monitor") or {}).get("sync") or {}
    remote = str(s.get("remote") or "").strip()
    if not (monitor_on(cfg) and cfg.get("run_mode") == "live" and s.get("enabled") is True and remote):
        return FakeSync().push(outbox, "").detail
    return GitSync(remote, str(s.get("branch") or "main")).push(outbox, f"monitor bundles {now:%Y-%m-%d %H:%M}").detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="monitor", description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=None, help="the monitor's own SQLite (default data/monitor/monitor.db)")
    ap.add_argument("--now", default=None, help="ISO time to report against (replays)")
    ap.add_argument("--label", default="", help="printed on the first line, e.g. 'SAMPLE / REPLAY'")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--sync", action="store_true")
    a = sub.add_parser("analyse")
    a.add_argument("--bundles", type=Path, required=True)
    a.add_argument("--dev", type=Path, default=None, help="push-trace folder (rows.json, pr_details.json)")
    a.add_argument("--pull", action="store_true")
    a.add_argument("--send", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--day", default=None)
    r.add_argument("--send", action="store_true")
    i = sub.add_parser("incident")
    i.add_argument("query")
    s = sub.add_parser("scorecard")
    s.add_argument("--week-end", default=None)
    s.add_argument("--send", action="store_true")
    args = ap.parse_args(argv)
    cfg = _load(args.config)
    now = _now(args.now)

    if args.cmd == "export":
        from .export import export, outbox_dir
        res = export(cfg, now)
        print(f"monitor export: {res.records} records -> {res.path.name}"
              + (" (consent heartbeat only: needs consent.d4_watcher_ref + monitor.enabled)" if res.consent_only else ""))
        if args.sync:
            print("sync:", sync_outbox(cfg, outbox_dir(cfg), now))
        return 0

    from .store import MonitorStore
    store = MonitorStore(args.db)
    try:
        if args.cmd == "analyse":
            from .analyse import analyse
            if args.pull:
                from ..watcher.sync import GitSync
                remote = str(((cfg.get("monitor") or {}).get("sync") or {}).get("remote") or "")
                print("pull:", GitSync(remote).pull(args.bundles).detail if remote and cfg.get("run_mode") == "live"
                      else "skipped (needs monitor.sync.remote and run_mode live)")
            res = analyse(args.bundles, store, now, cfg, sender=_sender(cfg, args.send), dev_dir=args.dev)
            print(f"monitor analyse: {res.bundles} bundle(s), {res.new_records} new records, {res.dev_rows} dev rows, "
                  f"{len(res.alerts)} new alert(s), {res.delivered} delivered")
            for x in res.alerts[:20]:
                print(" ", x["text"])
            return 0
        from . import report as rp
        if args.cmd == "incident":
            print(rp.incident(store, args.query))
            return 0
        if args.cmd == "report":
            day = date.fromisoformat(args.day) if args.day else now.astimezone(CAIRO).date()
            text = rp.daily_report(store, day, now, cfg, label=args.label)
        else:
            day = date.fromisoformat(args.week_end) if args.week_end else now.astimezone(CAIRO).date()
            text = rp.scorecard(store, day, now, cfg, label=args.label)
        print(text)
        snd = _sender(cfg, getattr(args, "send", False))
        if snd is not None:
            snd.send(text[:4000])
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
