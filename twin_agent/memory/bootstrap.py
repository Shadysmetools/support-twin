"""Bootstrap the stores OFFLINE from existing data, so memory is non-empty on day 1.

Sources (all already-redacted, all local):
- Principal's runbook entries (read from kb.db, which was built redacted) -> system_map facts, RED/AMBER trust
  (confirmed -> AMBER, everything else -> RED). Nothing from a single source is GREEN.
- Each CONFIRMED runbook entry that has a real root cause + fix -> a case (AMBER, source "runbook"): these are
  genuine resolved cases written by Principal, so case memory is useful on day 1.
- The 02m "Stray IN line" RCA -> one GREEN system_map fact (it has code file:line evidence, not one source).
- Closed requests in state.db (the live/replay path) -> cases, RED, source "replay", with the real redacted
  symptom only and an empty root cause (the replay diagnosed with FakeClaude, so its root cause is not real).
  The e2e harness runs in a throwaway, wiped home, so offline this is usually 0 and is reported honestly.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .. import paths
from . import cases as cases_store
from . import episodes as episodes_store
from . import system_map as system_map_store
from .store import MemoryDB

# 02m-RCA-STRAY-IN-LINE.md, verified against the code. Code file:line only (no customer record ids in source).
STRAY_FACT = (
    "The receipt 'PO reference' validate error on Globex is raised by a CUSTOM guard, not core Odoo: "
    "custom_receipt_validation/models/stock_picking.py:17 (button_validate) and "
    "custom_claim_types/models/stock_picking.py:144-152 (action_confirm). purchase_id is a core RELATED field "
    "through move_ids.purchase_line_id.order_id, resolved via the FIRST move only, so a zero-demand stray line "
    "with no purchase_line_id blocks the receipt only when it happens to sort first."
)
STRAY_EVIDENCE = ("02m-RCA-STRAY-IN-LINE.md; custom_receipt_validation/models/stock_picking.py:17; "
                  "custom_claim_types/models/stock_picking.py:144-152")


def _runbook_entries(kb_path: Path | None):
    from ..kb.kb_search import runbook_entries
    p = Path(kb_path or paths.kb_db())
    if not p.exists():
        return {}
    return runbook_entries(p)


def system_map_from_kb(mem: MemoryDB, kb_path: Path | None = None) -> int:
    entries = _runbook_entries(kb_path)
    n = 0
    for slug, e in entries.items():
        trust = "AMBER" if (e.get("status") == "confirmed") else "RED"
        detail = " | ".join(x for x in (e.get("signature"), e.get("root_cause"), e.get("fix")) if x)
        system_map_store.add_fact(
            mem, topic=slug or (e.get("title") or ""),
            fact=f"{e.get('title') or slug}: {detail}" if detail else (e.get("title") or slug),
            evidence=f"runbook:{slug} (status {e.get('status')})", trust=trust, source="runbook")
        n += 1
    return n


def cases_from_runbook(mem: MemoryDB, kb_path: Path | None = None) -> int:
    entries = _runbook_entries(kb_path)
    n = 0
    for slug, e in entries.items():
        if e.get("status") == "confirmed" and e.get("root_cause") and e.get("fix"):
            n += 1 if cases_store.add(mem, cases_store.Case(
                request_ref=f"runbook:{slug}", client="EG", track=e.get("tags") or "",
                symptom=e.get("signature") or e.get("title") or slug,
                verified_root_cause=e.get("root_cause") or "", fix=e.get("fix") or "",
                evidence_refs=f"runbook:{slug}", trust_level="AMBER",
                outcome="resolved_confirmed", source="runbook")) else 0
    return n


def stray_in_line_fact(mem: MemoryDB) -> int:
    system_map_store.add_fact(mem, topic="stray-in-line", fact=STRAY_FACT, evidence=STRAY_EVIDENCE,
                              trust="GREEN", source="rca-02m")
    return 1


def cases_from_state_db(mem: MemoryDB, state_path: Path | None = None) -> int:
    p = Path(state_path or paths.state_db())
    if not p.exists():
        return 0
    con = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    n = 0
    try:
        rows = con.execute("SELECT id, title, lane, tier, company, state, close_reason FROM requests "
                           "WHERE state LIKE 'CLOSED%'").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    for r in rows:
        n += 1 if cases_store.record_closed(mem, dict(r), source="replay", trust_level="RED") else 0
    return n


def bootstrap_all(kb_path: Path | None = None, state_path: Path | None = None,
                  clock=None) -> dict:
    """Populate every store from the offline sources. Returns the counts."""
    mem = MemoryDB(clock=clock)
    try:
        counts = {
            "system_map_facts_runbook": system_map_from_kb(mem, kb_path),
            "system_map_stray_in_line_GREEN": stray_in_line_fact(mem),
            "cases_from_runbook": cases_from_runbook(mem, kb_path),
            "cases_from_replay_statedb": cases_from_state_db(mem, state_path),
            "episodes_from_state_db": episodes_store.from_state_db(mem, state_path),
        }
        return counts
    finally:
        mem.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memory-bootstrap")
    ap.add_argument("--kb", type=Path, default=None, help="kb.db (default: paths.kb_db())")
    ap.add_argument("--state", type=Path, default=None, help="state.db (default: paths.state_db())")
    a = ap.parse_args(argv)
    counts = bootstrap_all(a.kb, a.state)
    for k, v in counts.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
