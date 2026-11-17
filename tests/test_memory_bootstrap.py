"""Offline bootstrap: runbook (kb.db) -> system-map facts RED/AMBER + cases; 02m -> one GREEN fact;
state.db closed requests -> cases. Memory is non-empty on day 1."""
from __future__ import annotations

import sqlite3

from twin_agent.memory import bootstrap, cases, system_map
from twin_agent.memory.store import MemoryDB, open_ro


def _make_kb(path):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE runbook (slug TEXT PRIMARY KEY, title TEXT, signature TEXT, status TEXT, "
                "status_raw TEXT, confirmed_date TEXT, tags TEXT, fix TEXT, root_cause TEXT)")
    con.executemany(
        "INSERT INTO runbook (slug,title,signature,status,status_raw,confirmed_date,tags,fix,root_cause) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [("stray-in-line", "Stray IN line", "receipt will not validate", "confirmed", "confirmed 2026-10-07",
          "2026-10-07", "inventory", "cancel the empty move", "orphan zero-demand move with no PO line"),
         ("phantom-reservation", "Phantom reservation", "on hand 0 but reserved > 0", "provisional",
          "provisional", "", "inventory", "release the reservation", "cancelled transfer left a reservation")])
    con.commit()
    con.close()


def _make_state(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, title TEXT, lane TEXT, tier TEXT, company TEXT, "
                "state TEXT, close_reason TEXT)")
    con.executemany("INSERT INTO requests (id,title,lane,tier,company,state,close_reason) VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_system_map_from_kb_trust_mapping(tmp_home, clock):
    kb = tmp_home / "kb.db"
    _make_kb(kb)
    mem = MemoryDB(clock=clock)
    n = bootstrap.system_map_from_kb(mem, kb)
    assert n == 2
    facts = {f["topic"]: f for f in system_map.all_facts(mem.conn)}
    assert facts["stray-in-line"]["trust_level"] == "AMBER"       # confirmed
    assert facts["phantom-reservation"]["trust_level"] == "RED"   # provisional
    mem.close()


def test_stray_green_and_cases_from_runbook(tmp_home, clock):
    kb = tmp_home / "kb.db"
    _make_kb(kb)
    mem = MemoryDB(clock=clock)
    bootstrap.stray_in_line_fact(mem)
    greens = [f for f in system_map.all_facts(mem.conn) if f["trust_level"] == "GREEN"]
    assert len(greens) == 1 and "custom_receipt_validation" in greens[0]["fact"]
    assert bootstrap.cases_from_runbook(mem, kb) == 1            # only the confirmed one with cause+fix
    assert cases.all_cases(mem.conn)[0]["trust_level"] == "AMBER"
    mem.close()


def test_cases_from_state_db(tmp_home, clock):
    st = tmp_home / "state.db"
    _make_state(st, [("REQ-1", "receipt validate issue", "L4_t2a_fix_proposal", "T2a", "EG", "CLOSED_ANSWERED", "answered"),
                     ("REQ-2", "open one", "L3", "T1", "EG", "DIAGNOSED", "")])
    mem = MemoryDB(clock=clock)
    assert bootstrap.cases_from_state_db(mem, st) == 1          # only the CLOSED one
    row = cases.all_cases(mem.conn)[0]
    assert row["trust_level"] == "RED" and row["source"] == "replay" and row["verified_root_cause"] == ""
    mem.close()


def test_bootstrap_all_makes_memory_non_empty(tmp_home, clock):
    kb = tmp_home / "kb.db"
    _make_kb(kb)
    st = tmp_home / "state.db"
    _make_state(st, [("REQ-1", "something closed", "L3", "T1", "EG", "CLOSED_ANSWERED", "answered")])
    counts = bootstrap.bootstrap_all(kb_path=kb, state_path=st, clock=clock)
    assert counts["system_map_facts_runbook"] == 2
    assert counts["system_map_stray_in_line_GREEN"] == 1
    assert counts["cases_from_runbook"] == 1
    assert counts["cases_from_replay_statedb"] == 1
    con = open_ro()
    try:
        assert con.execute("SELECT COUNT(*) FROM system_map").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 2
    finally:
        con.close()
