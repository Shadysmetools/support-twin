"""The WhatsApp support hub (Phase 1 of the "Support Twin" shared middleware — Digital Twin engine).

A central HUB service that owns WhatsApp I/O, Odoo ticketing and the support pipeline. Built as the testable
core: every external system (WhatsApp, Groq STT, Odoo, Telegram, Wiki, ShipShape/proof, Metrics) sits behind a
small interface (``interfaces.py``) with a deterministic FAKE (``fakes.py``), so the whole 11-stage pipeline runs
in CI with ZERO credentials, no deploy and no client send.

Nothing here sends a real client message, opens a real socket to Meta, or writes to the live Odoo. The real
adapters (RealSTT, the Odoo transport, etc.) each block themselves with a plain reason when their key/dependency
is missing — never a silent fallback to a fake.

Pipeline stages (each has an acceptance test; ``Hub`` drives them — ``pipeline.py``):
  1 RECEIVE  2 STT  3 ANALYSE  4 CREATE  5 ASSIGN  6 TELEGRAM  7 ACK  8 CONFIRM
  9 PROVE-IT 10 RESOLVE 11 METRICS-KPIs   (+ §15 LearningLog and TeamAwareAssigner layered on ANALYSE/ASSIGN)
"""
from __future__ import annotations

from .pipeline import Hub, TicketState
from .model import Analysis, Inbound, Owner

__all__ = ["Hub", "TicketState", "Analysis", "Inbound", "Owner"]
