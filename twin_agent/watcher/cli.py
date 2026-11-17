"""`python -I aa.py watcher <sub>`: status | export | sync | record | analyse | bootstrap.

Principal's laptop: status, export (cases -> redacted bundles in the outbox), sync (export + push), record (opt-in
screen recording of one window for one request). Sysadmin's device: analyse (bundles -> fingerprint + digest).
Anywhere, offline: bootstrap (fingerprint from the handover export, read-only).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import paths
from ..clock import SystemClock


def _load(cfg_path):
    from ..config import load_config
    return load_config(cfg_path)


def _db(cfg, clock):
    from ..db import StateDB
    paths.ensure_dirs()
    return StateDB(paths.state_db(), clock, cfg)


def outbox_dir(cfg: dict) -> Path:
    rel = str((cfg.get("watcher") or {}).get("outbox") or "data/watcher/outbox")
    p = Path(rel)
    return p if p.is_absolute() else paths.home() / p


def _vault_hashes(cfg: dict) -> tuple[str, ...]:
    if cfg.get("run_mode") != "live":
        return ()
    try:
        from .. import creds
        return tuple(creds.vault_hashes())
    except Exception:                                       # noqa: BLE001 - still redacted by pattern
        return ()


def do_export(cfg: dict, db, now, *, projects_dir=None):
    from .bundle import Pseudonymiser, export_cases, known_names, load_salt
    from .capture import all_cases
    cases = all_cases(db, cfg, now, projects_dir=projects_dir)
    pseudo = Pseudonymiser(load_salt(), known_names(db, cfg))
    rep = export_cases(cases, outbox_dir(cfg), pseudo, _vault_hashes(cfg))
    db.log_event("watcher_export", None, actor="watcher", cases=rep.cases, records=rep.records, dropped=rep.dropped)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watcher", description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("export")
    sub.add_parser("sync")
    r = sub.add_parser("record")
    r.add_argument("request_id")
    r.add_argument("--seconds", type=int, default=300)
    a = sub.add_parser("analyse")
    a.add_argument("--bundles", type=Path, required=True)
    a.add_argument("--out", type=Path, default=None)
    a.add_argument("--addendum", type=Path, default=None)
    a.add_argument("--pull", action="store_true", help="git pull the bundles first (needs watcher.sync.remote)")
    a.add_argument("--judge", action="store_true", help="also ask the claude -p judge (live only)")
    a.add_argument("--send-digest", action="store_true")
    b = sub.add_parser("bootstrap")
    b.add_argument("--handover", type=Path, required=True)
    b.add_argument("--principal-tg-id", action="append", default=[])
    b.add_argument("--principal-email", default="")
    b.add_argument("--out", type=Path, default=None)
    b.add_argument("--docs", type=Path, default=None)
    args = ap.parse_args(argv)
    clock = SystemClock()

    if args.cmd == "bootstrap":
        from .bootstrap import run
        ids = {x if str(x).startswith("user") else f"user{x}" for x in args.principal_tg_id}
        res = run(args.handover, ids, principal_email=args.principal_email,
                  out=args.out or paths.data_dir() / "watcher" / "bootstrap", docs=args.docs)
        print(f"bootstrap: {res['cases']} cases, {res['messages']} messages, docs written={res['docs_written']}, "
              f"leak hits={res['leaks']}")
        return 0 if res["leaks"] == 0 else 1

    cfg = _load(args.config)
    if args.cmd == "analyse":
        from .analyse import analyse
        from .digest import BotDigestSender, DigestRefused, FakeDigestSender
        if args.pull:
            from .sync import make_sync
            print("pull:", make_sync(cfg, side="sysadmin").pull(args.bundles).detail)
        runner = None
        if args.judge and cfg.get("run_mode") == "live":
            from ..spawner import RealClaudeRunner
            runner = RealClaudeRunner(cfg)
        sender = FakeDigestSender()
        if args.send_digest:
            try:
                from .. import creds
                sender = BotDigestSender(cfg, has_secret=creds.has_secret)
            except DigestRefused as e:
                print(f"digest not sent: {e}")
        rep = analyse(args.bundles, args.out, addendum=args.addendum, runner=runner, sender=sender)
        print(rep.digest)
        return 0

    db = _db(cfg, clock)
    now = clock.now()
    from . import gate
    if args.cmd == "status":
        print("Watcher:", gate.indicator(cfg, gate.pause_windows(db, now), now))
        for c in gate.CAPABILITIES:
            print(f"- {c}: {'on' if gate.capability_on(cfg, c) else 'off'}")
        return 0
    if args.cmd == "record":
        from .screen import RecordingRefused, record
        try:
            out = record(cfg, db, args.request_id.upper(), now, seconds=args.seconds, notify=print)
        except RecordingRefused as e:
            print(f"not recording: {e}")
            return 2
        print(f"recorded: {out}")
        return 0
    if not any(gate.allowed(cfg, db, c, now) for c in gate.CAPTURE):
        print("Watcher is off or paused (needs consent.d4_watcher_ref, watcher.enabled and a capture switch).")
        return 0
    rep = do_export(cfg, db, now)
    print(f"export: {rep.cases} cases, {rep.records} records, {rep.dropped} dropped by the final guard")
    if args.cmd == "sync":
        from .sync import make_sync
        res = make_sync(cfg, db, now).push(outbox_dir(cfg), f"watcher bundles {now:%Y-%m-%d %H:%M}")
        print(f"sync: {res.detail}")
        return 0 if res.ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
