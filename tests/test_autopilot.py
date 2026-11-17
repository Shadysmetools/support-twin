"""Autonomous cycle spine: trigger -> gate -> decision, OBSERVE-safe (prepares, never sends)."""
from __future__ import annotations

from twin_agent.autopilot import Autopilot
from twin_agent.helpdesk.odoo import OdooAdapter
from twin_agent.helpdesk.trigger import AssignmentTrigger
from twin_agent.testing.fake_odoo import PRINCIPAL_UID, FakeOdoo

NEW = 1


def _trigger_with_ticket():
    fake = FakeOdoo(uid=PRINCIPAL_UID)
    fake.data["helpdesk.ticket"][5001] = {
        "id": 5001, "name": "Receipt will not validate", "stage_id": NEW, "user_id": PRINCIPAL_UID,
        "partner_id": 3001, "x_track": "functional", "x_type": 1, "x_ai_confidence": 0.9,
        "create_date": "2026-11-04 09:05:00",
    }
    return fake, AssignmentTrigger(OdooAdapter(fake), principal_uid=PRINCIPAL_UID, new_stage_id=NEW)


def test_cycle_fires_gates_and_prepares_without_sending():
    fake, trig = _trigger_with_ticket()
    out = Autopilot(trig, observe=True).cycle()
    assert len(out) == 1
    o = out[0]
    assert o["ticket_id"] == 5001
    assert o["trust"] in ("GREEN", "AMBER", "RED")
    assert o["action"] == "prepared (observe)"
    assert o["sent"] is False
    assert fake.writes == []            # nothing written to Odoo


def test_no_evidence_is_not_green():
    _, trig = _trigger_with_ticket()
    o = Autopilot(trig, observe=True).cycle()[0]
    # a bare Analyst confidence with no concrete evidence must NOT be GREEN
    assert o["trust"] != "GREEN"
    assert o["honest_confidence"] <= 0.9


def test_dedup_second_cycle_is_empty():
    _, trig = _trigger_with_ticket()
    ap = Autopilot(trig, observe=True)
    assert len(ap.cycle()) == 1
    assert ap.cycle() == []             # the ticket already fired once


def test_live_mode_uses_recommendation_not_prepared():
    _, trig = _trigger_with_ticket()
    o = Autopilot(trig, observe=False).cycle()[0]
    assert o["action"] != "prepared (observe)"
    assert o["sent"] is False            # spine still never sends; the sender is a separate gated step
