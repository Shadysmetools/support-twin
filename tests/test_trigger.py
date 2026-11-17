"""Odoo assignment trigger: fires once for each ticket assigned to Principal in the New stage, read-only."""
from __future__ import annotations

from twin_agent.helpdesk.odoo import OdooAdapter
from twin_agent.helpdesk.trigger import AssignmentTrigger
from twin_agent.testing.fake_odoo import PRINCIPAL_UID, SUPERVISOR_UID, FakeOdoo

NEW = 1  # New stage id (STAGE_NAMES[1] == "New")


def _fake_with_assigned_ticket(tid=5001):
    fake = FakeOdoo(uid=PRINCIPAL_UID)
    # a ticket Supervisor just assigned to Principal, sitting in New
    fake.data["helpdesk.ticket"][tid] = {
        "id": tid, "name": "Receipt will not validate", "stage_id": NEW, "user_id": PRINCIPAL_UID,
        "partner_id": 3001, "x_track": "functional", "x_type": 1, "x_type_code": "1", "create_date": "2026-11-04 09:05:00",
    }
    return fake


def test_fires_for_ticket_assigned_to_principal_in_new():
    fake = _fake_with_assigned_ticket(5001)
    trig = AssignmentTrigger(OdooAdapter(fake), principal_uid=PRINCIPAL_UID, new_stage_id=NEW)
    fresh = trig.poll()
    assert [t["id"] for t in fresh] == [5001]


def test_dedup_fires_each_ticket_only_once():
    fake = _fake_with_assigned_ticket(5001)
    trig = AssignmentTrigger(OdooAdapter(fake), principal_uid=PRINCIPAL_UID, new_stage_id=NEW)
    assert len(trig.poll()) == 1
    assert trig.poll() == []            # second poll: nothing new


def test_ignores_not_assigned_or_not_new():
    fake = _fake_with_assigned_ticket(5001)
    # assigned to Supervisor, in New -> ignore
    fake.data["helpdesk.ticket"][5002] = {"id": 5002, "name": "x", "stage_id": NEW, "user_id": SUPERVISOR_UID,
                                          "partner_id": 3001, "create_date": "2026-11-04 09:06:00"}
    # assigned to Principal but already moved to Functional (stage 2) -> ignore
    fake.data["helpdesk.ticket"][5003] = {"id": 5003, "name": "y", "stage_id": 2, "user_id": PRINCIPAL_UID,
                                          "partner_id": 3001, "create_date": "2026-11-04 09:07:00"}
    trig = AssignmentTrigger(OdooAdapter(fake), principal_uid=PRINCIPAL_UID, new_stage_id=NEW)
    assert [t["id"] for t in trig.poll()] == [5001]


def test_drain_hands_each_fresh_ticket_to_the_flow():
    fake = _fake_with_assigned_ticket(5001)
    trig = AssignmentTrigger(OdooAdapter(fake), principal_uid=PRINCIPAL_UID, new_stage_id=NEW)
    fired: list[int] = []
    n = trig.drain(lambda t: fired.append(t["id"]))
    assert n == 1 and fired == [5001]
    assert trig.drain(lambda t: fired.append(t["id"])) == 0   # no re-fire


def test_trigger_never_writes_to_odoo():
    fake = _fake_with_assigned_ticket(5001)
    trig = AssignmentTrigger(OdooAdapter(fake), principal_uid=PRINCIPAL_UID, new_stage_id=NEW)
    trig.poll()
    assert fake.writes == []            # read-only: safe in OBSERVE
