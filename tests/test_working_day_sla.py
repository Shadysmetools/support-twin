"""WRAP layer - working_day_sla: the SLA / auto-close Odoo cannot do (counts WORKING days, Sun-Thu).

From TICKET-BLUEPRINT.md section 5:
  - Live (Deployment) -> Closed after 7 working days (Odoo's built-in counts CALENDAR days - wrong).
  - Customer Feedback: 5 working days + 2 reminders, then No Response.
  - Client Testing: 10 working days + 2 reminders, then No Response.
  - Working days = Sunday to Thursday. Fri/Sat never count. Timezone = Africa/Cairo date.
OBSERVE: scan() returns prepared actions, never executes them.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from twin_agent import working_day_sla as sla


def test_friday_and_saturday_are_not_working_days():
    assert sla.is_working_day(date(2026, 10, 23)) is False      # Friday
    assert sla.is_working_day(date(2026, 10, 24)) is False      # Saturday
    assert sla.is_working_day(date(2026, 10, 25)) is True       # Sunday
    assert sla.is_working_day(date(2026, 10, 29)) is True       # Thursday


def test_add_working_days_skips_the_weekend():
    # A Friday or Saturday start counts from the next Sunday.
    assert sla.add_working_days(date(2026, 10, 23), 1) == date(2026, 10, 25)   # Fri +1 -> Sun
    assert sla.add_working_days(date(2026, 10, 24), 1) == date(2026, 10, 25)   # Sat +1 -> Sun
    assert sla.add_working_days(date(2026, 10, 25), 0) == date(2026, 10, 25)   # +0 is the start


def test_2639_live_seven_working_days_beats_odoo_calendar():
    # Blueprint #2639: Live on Wed 21 Oct. Odoo's calendar auto-close fired Thu 29 Oct.
    # 7 WORKING days lands Sun 1 Nov - the correct date.
    assert sla.add_working_days(date(2026, 10, 21), 7) == date(2026, 11, 1)
    d = sla.due("Live (Deployment)", date(2026, 10, 21))
    assert d["deadline"] == date(2026, 11, 1)
    assert d["terminal_action"] == "auto_close"
    assert d["reminders"] == []                       # Live has no reminders


def test_working_days_between_is_the_inverse_of_add():
    a = date(2026, 10, 21)
    assert sla.working_days_between(a, date(2026, 11, 1)) == 7
    for n in range(0, 12):
        assert sla.working_days_between(a, sla.add_working_days(a, n)) == n


def test_entered_date_naive_datetime_is_read_as_utc_then_cairo():
    # Odoo stores naive UTC. A late-UTC Wednesday is already Thursday in Cairo.
    d = sla.to_cairo_date(datetime(2026, 10, 21, 22, 30))           # naive -> UTC
    assert d == date(2026, 10, 22)                                  # Thursday in Cairo
    # aware datetimes are honoured
    assert sla.to_cairo_date(datetime(2026, 10, 21, 22, 30, tzinfo=timezone.utc)) == date(2026, 10, 22)
    assert sla.to_cairo_date("2026-10-21") == date(2026, 10, 21)


def test_customer_feedback_two_reminders_then_no_response():
    entered = date(2026, 11, 1)                      # a Sunday
    d = sla.due("Customer Feedback", entered)
    assert len(d["reminders"]) == 2
    assert d["deadline_working_days"] == 5
    assert d["terminal_action"] == "no_response"
    # all reminders fall strictly before the deadline
    assert all(r < d["deadline"] for r in d["reminders"])
    # at the deadline, scan prepares the No Response move
    actions = sla.scan([{"id": 2700, "stage": "Customer Feedback", "entered_date": entered}],
                       today=d["deadline"])
    assert actions and actions[0]["action"] == "no_response"


def test_client_testing_is_ten_working_days_with_two_reminders():
    d = sla.due("Client Testing (Client Review)", date(2026, 11, 1))
    assert d["deadline_working_days"] == 10
    assert len(d["reminders"]) == 2
    assert d["terminal_action"] == "no_response"


def test_scan_reports_the_latest_milestone_passed():
    entered = date(2026, 11, 1)
    d = sla.due("Customer Feedback", entered)
    t = [{"id": 1, "stage": "Customer Feedback", "entered_date": entered}]
    # before the first reminder: nothing due
    assert sla.scan(t, today=entered) == []
    # on the first reminder date: one reminder due
    a1 = sla.scan(t, today=d["reminders"][0])
    assert a1[0]["action"] == "reminder" and a1[0]["reminder_index"] == 1
    # on the second reminder date: the second reminder
    a2 = sla.scan(t, today=d["reminders"][1])
    assert a2[0]["action"] == "reminder" and a2[0]["reminder_index"] == 2


def test_auto_close_is_blocked_when_last_client_inbound_is_unanswered():
    entered = date(2026, 10, 21)                     # Live, due to auto-close Sun 1 Nov
    past = date(2026, 11, 2)
    blocked = sla.scan([{"id": 2639, "stage": "Live (Deployment)", "entered_date": entered,
                         "last_inbound_unanswered": True}], today=past)
    assert blocked[0]["action"] == "auto_close"
    assert blocked[0]["blocked"] is True
    assert "unanswered" in blocked[0]["block_reason"]
    # control: when the client has no unanswered inbound, the auto-close is NOT blocked
    ok = sla.scan([{"id": 2639, "stage": "Live (Deployment)", "entered_date": entered,
                    "last_inbound_unanswered": False}], today=past)
    assert ok[0]["blocked"] is False


def test_scan_returns_prepared_actions_only_no_execution():
    # scan is pure: it returns dicts, it has no Odoo handle and performs no side effect.
    entered = date(2026, 10, 21)
    actions = sla.scan([{"id": 2639, "stage": "Live (Deployment)", "entered_date": entered}],
                       today=date(2026, 11, 2))
    assert isinstance(actions, list) and actions[0]["id"] == 2639
    assert actions[0]["action"] == "auto_close"
