"""The WRAP layer: Principal's Agent as a SAFETY + HONEST-CONFIDENCE + SLA service that wraps Acme'
existing live pipeline (their Ticket Analyst run-ticket-analyst.ps1, their consultant/developer seats,
ShipShape) - it does NOT rebuild any of it.

Two documented integration points (pipeline_hooks):
  - on_analyst_classified(ticket, classification) -> verdict + note_draft   (verify_service)
  - sla_scan(tickets)                              -> due_actions            (working_day_sla)

Everything here is OBSERVE / prepare-only: it computes verdicts and prepared actions and returns them.
It never writes Odoo, never sends an email, never moves a stage.
"""
from .pipeline_hooks import on_analyst_classified, sla_scan

__all__ = ["on_analyst_classified", "sla_scan"]
