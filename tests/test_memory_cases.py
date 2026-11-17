"""Case memory: write, retrieve, redaction, trust, idempotency."""
from __future__ import annotations

from twin_agent.memory import cases
from twin_agent.memory.store import MemoryDB


def _mem(tmp_home, clock) -> MemoryDB:
    return MemoryDB(clock=clock)


def test_add_and_similar_ranks_relevant_first(tmp_home, clock):
    mem = _mem(tmp_home, clock)
    cases.add(mem, cases.Case(request_ref="REQ-1", client="EG", track="T2a",
                              symptom="receipt will not validate, PO reference, line demand 0",
                              verified_root_cause="custom guard reads first move's PO", fix="cancel the stray line",
                              trust_level="AMBER", outcome="resolved", source="rca"))
    cases.add(mem, cases.Case(request_ref="REQ-2", client="EG", track="T1",
                              symptom="customer asks about delivery ETA", verified_root_cause="n/a",
                              trust_level="RED", outcome="answered", source="telegram"))
    hits = cases.similar(mem.conn, "the receipt refuses to validate because of the PO reference", "EG", k=5)
    assert hits and hits[0].request_ref == "REQ-1"
    assert hits[0].score > 0
    mem.close()


def test_similar_filters_by_client_and_requires_overlap(tmp_home, clock):
    mem = _mem(tmp_home, clock)
    cases.add(mem, cases.Case(client="KSA", symptom="receipt validate PO reference", source="rca"))
    # different client -> filtered out
    assert cases.similar(mem.conn, "receipt validate PO reference", "EG", k=5) == []
    # unrelated text -> no blind fallback
    assert cases.similar(mem.conn, "totally unrelated weather question", "KSA", k=5) == []
    assert cases.similar(mem.conn, "receipt validate PO reference", "KSA", k=5)
    mem.close()


def test_redaction_on_store(tmp_home, clock):
    mem = _mem(tmp_home, clock)
    cases.add(mem, cases.Case(client="EG", symptom="email principal@example.org phone +201234567890 token sk_live_abcdefabcdefabcdefabcd",
                              verified_root_cause="see +201234567890", source="x"))
    rows = cases.all_cases(mem.conn)
    blob = rows[0]["symptom"] + rows[0]["verified_root_cause"]
    assert "principal@example.org" not in blob
    assert "+201234567890" not in blob
    assert "REDACTED" in blob
    mem.close()


def test_idempotent_add(tmp_home, clock):
    mem = _mem(tmp_home, clock)
    first = cases.add(mem, cases.Case(client="EG", symptom="same symptom", source="rca"))
    dup = cases.add(mem, cases.Case(client="EG", symptom="same symptom", source="rca"))
    assert first > 0 and dup == 0
    assert len(cases.all_cases(mem.conn)) == 1
    mem.close()


def test_set_trust(tmp_home, clock):
    mem = _mem(tmp_home, clock)
    cid = cases.add(mem, cases.Case(client="EG", symptom="x y z", trust_level="RED", source="rca"))
    cases.set_trust(mem, cid, "GREEN")
    assert cases.all_cases(mem.conn)[0]["trust_level"] == "GREEN"
    mem.close()
