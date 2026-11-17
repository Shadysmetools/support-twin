"""System-map memory: write, retrieve by topic, redaction, trust, idempotent upsert."""
from __future__ import annotations

from twin_agent.memory import system_map
from twin_agent.memory.store import MemoryDB


def test_add_and_facts_by_topic(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    system_map.add_fact(mem, topic="stray-in-line",
                        fact="custom_receipt_validation raises the PO-ref error; PO field reads the first move",
                        evidence="stock_picking.py:17", trust="GREEN", source="rca-02m")
    system_map.add_fact(mem, topic="pos-sync", fact="unsynced POS order shows a red badge", trust="AMBER", source="runbook")
    hits = system_map.facts(mem.conn, "the PO ref error that blocks validate: which module raises it, reads first move", k=5)
    assert hits and hits[0]["topic"] == "stray-in-line"
    assert hits[0]["trust_level"] == "GREEN"
    mem.close()


def test_upsert_is_idempotent(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    system_map.add_fact(mem, topic="t", fact="same fact text", trust="RED", source="runbook")
    system_map.add_fact(mem, topic="t", fact="same fact text", trust="AMBER", source="runbook")
    rows = system_map.all_facts(mem.conn)
    assert len(rows) == 1 and rows[0]["trust_level"] == "AMBER"  # updated in place, not duplicated
    mem.close()


def test_fact_redaction(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    system_map.add_fact(mem, topic="leak", fact="owner is principal@example.org", trust="RED", source="x")
    assert "principal@example.org" not in system_map.all_facts(mem.conn)[0]["fact"]
    mem.close()
