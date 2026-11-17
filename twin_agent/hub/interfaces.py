"""The seams. Every external system the hub touches is a small Protocol here, with a FAKE in ``fakes.py`` and a
REAL adapter that blocks itself (plain reason) when its key/dependency is missing. Tests inject the fakes; the
whole 11-stage pipeline runs with zero credentials.

A Protocol (structural typing) keeps the hub decoupled: any object with these methods qualifies, so the real
adapters, the fakes, and future implementations are interchangeable without a base class import.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------- external I/O seams

@runtime_checkable
class WhatsAppGateway(Protocol):
    """Outbound WhatsApp (the hub is the ONLY sender). Every send is gated on the 24h customer-service window."""

    def send_text(self, to: str, text: str, *, last_inbound_at: Any, now: Any, automatic: bool = False) -> dict: ...


@runtime_checkable
class SpeechToText(Protocol):
    """Groq whisper-large-v3 transcription (plan §14.3). Real impl blocks without a key; fake returns a fixture."""

    def transcribe(self, audio_id: str, *, lang_hint: str = "") -> dict: ...


@runtime_checkable
class TicketWriter(Protocol):
    """Writes the WORDS of the Odoo ticket (an LLM in production): the English translation of the client's message, a
    few-word problem phrase, what they expect, and the steps they gave. Returns ``{"status": "ok", "english", "problem",
    "expect", "steps"}`` or ``{"status": "blocked", "why": <plain reason>}``. A blocked writer never stops intake: the
    ticket is built from the deterministic ``analyse`` output (see ``ticket_text``) and says it is not translated."""

    def write(self, text: str, *, lang: str, analysis: "Any", client: str, names: "list[str] | None" = None) -> dict: ...


@runtime_checkable
class OdooGateway(Protocol):
    """Guarded helpdesk writes (create/assign/note) — all pass ``helpdesk/guard.py``, mail suppressed on create."""

    def create_ticket(self, vals: dict, *, context: dict) -> int: ...
    def assign(self, tid: int, user_id: int) -> None: ...
    def post_note(self, tid: int, body: str) -> None: ...


@runtime_checkable
class TelegramGateway(Protocol):
    """Per-ticket topic + the owner-approval channel (plan §7). Approval is ASYNCHRONOUS and BOUND:

    ``request_approval`` only posts the request (inline buttons carrying ``nonce``) and returns
    ``{"status": "pending"}``. The tap comes back later as ``Hub.on_owner_decision(nonce, approver_tg_id, ...)``.
    The hub is the source of truth for validity: an approval is bound to ``(ticket_id, kind, draft_hash, nonce)``,
    to the approvers in ``allowed_approvers`` (the ticket owner's tg_id + a configured allowlist), to
    ``expires_at``, and is single use. A real gateway must pass the tapping user's Telegram id (callback_query.from.id) through unchanged, for approvals
    AND for ``Hub.reassign(..., actor_tg_id=)`` / ``Hub.release_hold(..., actor_tg_id=)`` (owner or allowlist only)."""

    def open_topic(self, tid: int, owner: "Any", brief: str) -> int: ...
    def post(self, topic_id: int, text: str) -> None: ...
    def post_triage(self, text: str) -> None: ...
    def request_approval(self, topic_id: int, *, ticket_id: int, kind: str, draft: str, nonce: str,
                         draft_hash: str, allowed_approvers: list[int], expires_at: "Any", offer_whatsapp: bool = False) -> dict: ...


@runtime_checkable
class KnowledgeSource(Protocol):
    """A read-only customer-knowledge source. TWO of these feed ANALYSE/routing/brief: Odoo + Wiki (plan §14)."""

    def partner_for(self, wa_id: str) -> int | None: ...
    def client_context(self, partner_id: int) -> dict: ...


@runtime_checkable
class ProofRunner(Protocol):
    """PROVE-IT (stage 9): ShipShape drives the proof-video pipeline. Real blocks without staging/keys; fake cans it."""

    def prove(self, request: dict, packet: dict, cfg: dict) -> dict: ...


@runtime_checkable
class MetricsConnector(Protocol):
    """METRICS-KPIs (stage 11): push the resolved ticket's outcome/metrics to Metrics. Real blocks without a key."""

    def push_kpis(self, outcome: dict) -> dict: ...


# ---------------------------------------------------------------- §15 learning + team-aware assignment

@runtime_checkable
class LearningLog(Protocol):
    """§15a — append-only learning record on close, plus a periodic review job that proposes (never auto-applies)."""

    def record(self, signals: dict) -> dict: ...
    def review(self) -> dict: ...


@runtime_checkable
class TeamAwareAssigner(Protocol):
    """§15b — pick owner from role/skills/history/load (Odoo+Wiki), with an 'assigned to X because…' rationale."""

    def assign(self, analysis: "Any", client_context: dict) -> dict: ...


# ---------------------------------------------------------------- §16 eval + independent judge

@runtime_checkable
class Judge(Protocol):
    """§16b — an INDEPENDENT model (distinct from the drafter) that scores a draft/classification/assignment/video
    before the owner sees it, returning per-dimension scores and a low-score flag."""

    def score(self, kind: str, item: dict) -> dict: ...


@runtime_checkable
class EvalHarness(Protocol):
    """§16a — load the golden set, run classify/route/draft, score vs ground truth, fail the gate on regression."""

    def run_gate(self, baseline: dict | None = None) -> dict: ...
