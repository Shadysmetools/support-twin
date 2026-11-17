"""The autonomous cycle spine: fire on each freshly-assigned ticket, gate it honestly, decide the next action.

Chains the real trigger (helpdesk.trigger.AssignmentTrigger) to the honest-confidence gate (verify_service):

    trigger.poll()  ->  for each ticket: form the claim  ->  verify_service.assess (evidence + cross-model)
                    ->  trust GREEN/AMBER/RED + recommendation  ->  decide

OBSERVE by default: it PREPARES and records what it WOULD do; it never sends and never writes. Going live is a
separate, lane-by-lane switch (observe=False only once a human flips a lane). This is the spine the full loop
(worker brain + proof + sender) hangs off; here it is proven end-to-end on fakes without touching the world.
"""
from __future__ import annotations

from typing import Any

from . import verify_service


class Autopilot:
    def __init__(self, trigger, *, refuter=None, observe: bool = True):
        self.trigger = trigger
        self.refuter = refuter
        self.observe = bool(observe)

    @staticmethod
    def _claim_from_ticket(t: dict) -> dict:
        """The Analyst-style claim the gate scores: the ticket's track/type + its root-cause/summary."""
        return {
            "track": t.get("x_track"),
            "type": t.get("x_type"),
            "root_cause": t.get("root_cause") or t.get("name"),
            "confidence": t.get("x_ai_confidence"),
        }

    def handle(self, ticket: dict, evidence: list | None = None) -> dict:
        """Gate ONE ticket and decide. In OBSERVE the action is always 'prepared' — nothing is sent."""
        verdict = verify_service.assess(self._claim_from_ticket(ticket), evidence or [], refuter=self.refuter)
        action = "prepared (observe)" if self.observe else verdict["recommendation"]
        return {
            "ticket_id": ticket.get("id"),
            "trust": verdict["trust"],
            "honest_confidence": verdict["honest_confidence"],
            "recommendation": verdict["recommendation"],
            "action": action,
            "sent": False,            # OBSERVE spine never sends; the sender is a separate gated step
        }

    def cycle(self, evidence_for: Any = None) -> list[dict]:
        """One autonomous pass: fire for each freshly-assigned ticket, gate it, decide. Sends nothing.

        evidence_for: optional callable(ticket)->evidence list, so a caller can supply gathered evidence."""
        out: list[dict] = []
        for t in self.trigger.poll():
            ev = evidence_for(t) if callable(evidence_for) else None
            out.append(self.handle(t, ev))
        return out
