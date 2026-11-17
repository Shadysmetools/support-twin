"""WRAP layer - pipeline_hooks: the thin, documented integration points their pipeline calls.

on_analyst_classified -> verify_service (post-classification step for run-ticket-analyst.ps1).
sla_scan -> working_day_sla (the scheduled helpdesk auto-close / reminder job).
No side effects: both just compute and return.
"""
from __future__ import annotations

from datetime import date

from twin_agent.checks.refuter_xmodel import FakeRefuter
from twin_agent.wrap import pipeline_hooks as hooks

CONCRETE = [{"kind": "odoo_read", "model": "helpdesk.ticket", "id": "2639", "value": "Live"}]


def test_on_analyst_classified_returns_verdict_and_note_draft():
    ticket = {"id": 2639, "name": "Export sales report"}
    classification = {"track": "functional", "type": "inquiries", "confidence": 0.8,
                      "root_cause": "how-to (hypothesis)", "evidence": CONCRETE}
    out = hooks.on_analyst_classified(ticket, classification, refuter=FakeRefuter(refuted=False))
    assert out["verdict"]["trust"] == "GREEN"
    assert out["verdict"]["type"] == "inquiries"       # INQUIRY carried through as a type
    assert out["note_draft"]["posted"] is False        # observe/prepare-only


def test_on_analyst_classified_defaults_to_observe_single_model():
    out = hooks.on_analyst_classified({"id": 1}, {"track": "functional", "type": "configuration",
                                                  "confidence": 0.9, "evidence": CONCRETE})
    assert out["verdict"]["mode"] == "single-model"    # no refuter wired by default


def test_sla_scan_delegates_to_working_day_sla():
    tickets = [{"id": 2639, "stage": "Live (Deployment)", "entered_date": date(2026, 10, 21)}]
    out = hooks.sla_scan(tickets, today=date(2026, 11, 2))
    assert out[0]["id"] == 2639 and out[0]["action"] == "auto_close"
