"""memory CLI (python -I aa.py memory <subcommand>). All local, read-only against Odoo/Telegram/Gmail.

    memory bootstrap [--kb ...] [--state ...]   seed the stores from offline data; prints the counts
    memory reflect [--min-cases N]              run the deterministic reflection loop; prints the report
    memory recall "<text>" [--k N]              print the recall block for a request text (debug)
"""
from __future__ import annotations

import argparse
import sys

from . import bootstrap as bootstrap_mod
from . import recall as recall_mod
from . import reflect as reflect_mod
from .store import MemoryDB, open_ro


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="memory")
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("bootstrap")
    b.add_argument("--kb", default=None)
    b.add_argument("--state", default=None)

    rf = sub.add_parser("reflect")
    rf.add_argument("--min-cases", type=int, default=1)

    rc = sub.add_parser("recall")
    rc.add_argument("text")
    rc.add_argument("--k", type=int, default=3)

    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if a.cmd == "bootstrap":
        return bootstrap_mod.main(
            (["--kb", a.kb] if a.kb else []) + (["--state", a.state] if a.state else []))
    if a.cmd == "reflect":
        mem = MemoryDB()
        try:
            report = reflect_mod.reflect(mem, min_cases=a.min_cases)
        finally:
            mem.close()
        for k, v in report.items():
            print(f"{k}: {v}")
        return 0
    if a.cmd == "recall":
        try:
            con = open_ro()
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1
        try:
            block = recall_mod.recall(con, a.text, k=a.k)["block"]
        finally:
            con.close()
        print(block or "(no relevant memory)")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
