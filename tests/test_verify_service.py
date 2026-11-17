"""WRAP layer - verify_service: honest evidence-gated confidence over the Analyst's blind float.

The Analyst (run-ticket-analyst.ps1) self-reports a single confidence float with NO evidence gate. This
service turns that into a TRUST LEVEL (GREEN/AMBER/RED) gated on concrete evidence + the cross-model refuter.
OBSERVE-safe: assess() is pure; prepare_note() renders a DRAFT log note, never posts.
"""
from __future__ import annotations

import io
import json

from twin_agent import verify_service as vs
from twin_agent.checks.refuter_xmodel import FakeRefuter

# A real Odoo record read stands on its own as concrete evidence (refuter_xmodel._is_concrete).
CONCRETE = [{"kind": "odoo_read", "model": "helpdesk.ticket", "id": "2639",
             "field": "stage_id", "value": "Live"}]
# An assertion / note is NOT concrete - the yes-man trap the gate closes.
ASSERTION = [{"kind": "note", "value": "I checked the ticket, it is clearly a configuration task"}]


def _classification(**over):
    c = {
        "track": "functional",
        "type": "inquiries",
        "confidence": 0.8,
        "summary": "Client asks how to export the sales report",
        "root_cause": "How-to question; nothing to build (hypothesis)",
    }
    c.update(over)
    return c


def test_concrete_evidence_and_refuter_not_overturned_is_green():
    v = vs.assess(_classification(), CONCRETE, refuter=FakeRefuter(refuted=False))
    assert v["trust"] == "GREEN"
    assert v["mode"] == "cross-model"
    assert v["overturned"] is False
    assert v["evidence"]                       # the concrete item is surfaced
    assert 0 < v["honest_confidence"] <= vs.CONF_CAP["GREEN"]


def test_no_evidence_is_red_even_if_self_reported_high():
    v = vs.assess(_classification(confidence=0.95), [], refuter=FakeRefuter(refuted=False))
    assert v["trust"] == "RED"
    assert v["self_reported_confidence"] == 0.95
    # honest confidence is evidence-gated, NOT the blind 0.95
    assert v["honest_confidence"] <= vs.CONF_CAP["RED"]
    assert v["honest_confidence"] < 0.95


def test_refuter_overturns_is_red_with_counter_reason():
    v = vs.assess(_classification(), CONCRETE,
                  refuter=FakeRefuter(refuted=True, reasons=["it is a bug, not an inquiry"]))
    assert v["trust"] == "RED"
    assert v["overturned"] is True
    assert any("bug" in r for r in v["to_verify"])


def test_partial_evidence_not_overturned_is_amber():
    v = vs.assess(_classification(), ASSERTION, refuter=FakeRefuter(refuted=False))
    assert v["trust"] == "AMBER"
    assert v["honest_confidence"] <= vs.CONF_CAP["AMBER"]


def test_concrete_evidence_without_cross_model_is_capped_at_amber():
    # No refuter wired (the offline / no-key default): GREEN requires an actual cross-model run.
    v = vs.assess(_classification(), CONCRETE, refuter=None)
    assert v["trust"] == "AMBER"
    assert v["mode"] == "single-model"


def test_inquiry_type_is_carried_through_unchanged():
    v = vs.assess(_classification(type="inquiries"), CONCRETE, refuter=FakeRefuter(refuted=False))
    assert v["type"] == "inquiries"
    assert v["track"] == "functional"


def test_percent_confidence_is_normalised():
    v = vs.assess(_classification(confidence=95), [], refuter=FakeRefuter(refuted=False))
    assert v["self_reported_confidence"] == 0.95


def test_prepare_note_is_a_draft_tagging_supervisor_not_posted():
    v = vs.assess(_classification(), CONCRETE, refuter=FakeRefuter(refuted=False))
    note = vs.prepare_note(v)
    assert note["posted"] is False
    assert note["is_activity"] is False          # blueprint: NO [AI] activity, tag Supervisor in a log note
    assert "Supervisor" in note["mentions"]
    assert "[AI]" not in note["body"]
    assert "Supervisor" in note["body"]


def test_cli_reads_classification_and_prints_verdict(tmp_path, capsys):
    payload = {"classification": _classification(), "evidence_sources": CONCRETE}
    f = tmp_path / "c.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    rc = vs.main(["--file", str(f)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trust"] in ("GREEN", "AMBER", "RED")
    # observe-safe default: no key, no cross-model run -> honest single-model
    assert out["mode"] == "single-model"


def test_cli_reads_stdin(monkeypatch, capsys):
    payload = {"classification": _classification(), "evidence_sources": []}
    buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    class _Stdin:
        buffer = buf
    monkeypatch.setattr("sys.stdin", _Stdin())
    rc = vs.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trust"] == "RED"          # no evidence
