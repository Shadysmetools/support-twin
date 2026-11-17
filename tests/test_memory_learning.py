"""Learning measurement (evidence for maturity dim 5): recall changes the worker's input and improves the
outcome. We run the SAME deterministic worker logic over its REAL work folder, without memory vs with recall,
and show the with-recall arm matches the known outcome at least as well. A negative control proves recall does
not inject a wrong class into an unrelated request.
"""
from __future__ import annotations

import copy
import json

from twin_agent import spawner
from twin_agent.memory import cases
from twin_agent.memory import recall as recall_mod
from twin_agent.memory.store import MemoryDB, open_ro

KNOWN_CLASS = "RB stray-in-line"


def _seed_known_case(clock):
    """A past, verified case plus a DISTRACTOR that shares the common tokens (po/line/quantity). Neither text
    contains the class slug the worker must output, and the distractor must not out-rank the real case."""
    mem = MemoryDB(clock=clock)
    cases.add(mem, cases.Case(
        request_ref="REQ-0142", client="EG", track="T2a",
        symptom="WAVE will not validate: a receipt line has demand 0 and no PO line",
        verified_root_cause="the custom custom_receipt_validation guard reads only the first move PO, so a "
                            "zero-demand line with no purchase_line_id blocks validate when it sorts first",
        fix="cancel and unlink the empty move, or fix the guard to test all non-cancelled moves",
        evidence_refs="02m-RCA-STRAY-IN-LINE.md; stock_picking.py:17", trust_level="GREEN", outcome="resolved",
        source="rca-02m"))
    cases.add(mem, cases.Case(
        request_ref="REQ-0200", client="EG", track="T2a",
        symptom="purchase order PO line quantity mismatch on the moves analysis report",
        verified_root_cause="a reporting field mix-up, not a data change",
        fix="read planned from Expected Arrival", trust_level="AMBER", outcome="resolved", source="runbook"))
    mem.close()


def _diagnose(workdir) -> str:
    """Stand-in for the worker: reads its real work folder (request.json + recall.md, like the real worker).
    Outputs the class only when the evidence names the custom guard; the request text alone is not enough."""
    req = json.loads((workdir / "request.json").read_text(encoding="utf-8"))
    recall = (workdir / "recall.md")
    recall_text = recall.read_text(encoding="utf-8") if recall.exists() else ""
    seen = json.dumps(req) + "\n" + recall_text
    assert KNOWN_CLASS not in json.dumps(req)        # the request never contains the answer (non-tautological)
    if "custom_receipt_validation" in seen or "first move" in seen:
        return KNOWN_CLASS
    return "unknown"


def test_recall_improves_diagnosis(tmp_home, clock, cfg):
    _seed_known_case(clock)
    rid = "REQ-0987"
    # A NEW request that paraphrases the known case and does NOT contain the class slug.
    request = {"id": rid, "company": "EG", "channel": "telegram_group",
               "title": "A receipt will not validate - it complains about the PO reference and one line shows quantity 0"}

    cfg_off = copy.deepcopy(cfg)
    cfg_off["memory"] = {"recall_enabled": False}
    cfg_on = copy.deepcopy(cfg)
    cfg_on["memory"] = {"recall_enabled": True, "recall_k": 3}

    # the real case must out-rank the distractor on the new request
    con = open_ro()
    try:
        top = recall_mod.recall(con, request, k=3)["cases"]
    finally:
        con.close()
    assert top and top[0].request_ref == "REQ-0142"

    # WITHOUT memory
    wd = spawner.prepare_workdir(rid, request, [], cfg_off)
    assert not (wd / "recall.md").exists()
    without = _diagnose(wd)

    # WITH recall (same request, same worker logic; recall adds recall.md to the input)
    wd2 = spawner.prepare_workdir(rid, request, [], cfg_on)
    assert (wd2 / "recall.md").exists()
    assert "custom_receipt_validation" in (wd2 / "recall.md").read_text(encoding="utf-8")   # recall changed the input
    with_memory = _diagnose(wd2)

    assert without == "unknown"
    assert with_memory == KNOWN_CLASS                 # second-pass improvement, matches the known outcome


def test_negative_control_unrelated_request(tmp_home, clock, cfg):
    _seed_known_case(clock)
    rid = "REQ-0990"
    request = {"id": rid, "company": "EG", "title": "please send me a new password reset link, mine expired"}
    wd = spawner.prepare_workdir(rid, request, [], cfg)
    # recall must not surface the stray-in-line case for an unrelated request
    assert not (wd / "recall.md").exists()
    assert _diagnose(wd) == "unknown"
