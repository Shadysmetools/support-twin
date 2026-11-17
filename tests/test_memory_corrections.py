"""Correction memory: write edit/reject rules, retrieve by context, redaction, idempotency."""
from __future__ import annotations

from twin_agent.memory import corrections
from twin_agent.memory.store import MemoryDB


def test_record_edit_and_rules(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    corrections.record_edit(mem, context="holding reply for a logistics ETA question",
                            draft="We will check and revert.", final="On it, you will have an update within the hour.")
    hits = corrections.rules(mem.conn, "ETA logistics holding reply", k=5)
    assert hits and "update within the hour" in hits[0]["rule"]
    mem.close()


def test_record_reject(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    corrections.record_reject(mem, context="fix proposal for a receipt", draft="Just delete the move.",
                              reason="never delete without a guard")
    hits = corrections.rules(mem.conn, "receipt fix proposal", k=5)
    assert hits and "do NOT" in hits[0]["rule"]
    mem.close()


def test_rules_require_overlap(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    corrections.add_rule(mem, context="receipt validation", what_changed="x", rule="prefer concise wording")
    assert corrections.rules(mem.conn, "completely different topic about payroll", k=5) == []
    mem.close()


def test_correction_redaction_and_idempotency(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    a = corrections.add_rule(mem, context="ctx principal@example.org", what_changed="w", rule="r for +201234567890")
    b = corrections.add_rule(mem, context="ctx principal@example.org", what_changed="w", rule="r for +201234567890")
    assert a > 0 and b == 0
    rows = corrections.all_rules(mem.conn)
    assert "principal@example.org" not in rows[0]["context"] and "+201234567890" not in rows[0]["rule"]
    mem.close()
