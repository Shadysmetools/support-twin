"""Thin, documented integration points where Acme' EXISTING pipeline calls into the WRAP layer.

These are deliberately tiny: they hold no logic of their own, they just call verify_service / working_day_sla
and hand back the result. No side effects - they compute and return. Wiring these into the live runner and the
live Odoo happens on device day with Principal (see the call-site notes below and STATUS.md).
"""
from __future__ import annotations

from .. import verify_service, working_day_sla


def on_analyst_classified(ticket: dict, classification: dict, *, refuter=None,
                          evidence_sources=None) -> dict:
    """POST-CLASSIFICATION HOOK for the Ticket Analyst.

    WHERE THIS IS INVOKED (on device day):
        In run-ticket-analyst.ps1, immediately AFTER the classify_ticket tool returns and BEFORE (or instead of)
        the blind write of x_track / x_type / x_ai_confidence + the HTML note. The PowerShell runner pipes the
        classification JSON to `python -m twin_agent.verify_service` and reads back this verdict, OR a Python
        host calls this function directly. The verdict's honest trust level + the prepared log note REPLACE the
        yes-man confidence write; nothing is posted until Supervisor decides.

    ticket:          the Odoo ticket dict (id, name, ...); carried for context/logging, not required by assess().
    classification:  the Analyst's output (track, type, confidence, root_cause, ...). Its "type" (incl.
                     "inquiries", a Functional TYPE per Supervisor's blueprint - NOT a track) is carried through.
    evidence_sources: evidence gathered for this ticket; defaults to classification["evidence"].
    refuter:         optional cross-model refuter. Omit for the offline/observe default (verdict capped at AMBER).

    Returns {verdict, note_draft, ticket_id}. OBSERVE: no write, no send, no stage move.
    """
    verdict = verify_service.assess(classification, evidence_sources, refuter=refuter)
    note_draft = verify_service.prepare_note(verdict)
    return {"ticket_id": ticket.get("id") if isinstance(ticket, dict) else None,
            "verdict": verdict,
            "note_draft": note_draft}


def sla_scan(tickets, *, today=None, clock=None) -> list[dict]:
    """SLA HOOK for the scheduled helpdesk automation.

    WHERE THIS IS INVOKED (on device day):
        From the scheduled job that today relies on Odoo's (calendar-day) team auto-close. That job reads the
        open tickets in Live / Customer Feedback / Client Testing and their stage-entry dates, calls this with
        the list, and gets back the PREPARED actions (which tickets are due for a reminder, due to move to No
        Response, or due to auto-close) - counted in WORKING days (Sun-Thu), which Odoo cannot do. The runner
        then executes each unblocked action under Supervisor's rules; it must skip any action marked blocked and must
        de-duplicate reminders against what it already sent (scan is stateless - see working_day_sla.scan).

    tickets: iterable of {id, stage, entered_date, last_inbound_unanswered?}.
    today:   a Cairo date / datetime / ISO string; omit to read the system clock.

    Returns the prepared due_actions list. OBSERVE: it executes nothing.
    """
    return working_day_sla.scan(tickets, today=today, clock=clock)
