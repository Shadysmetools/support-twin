"""How the hub talks to its ``TeamBus`` (mixed into ``OwnerFlow``/``Hub``).

Every team post has TWO texts: the full ``text`` (for a bus that may carry client text, i.e. Telegram) and a SAFE
line (ticket id, owner, redacted summary, links; never raw client text). The hub hands the bus exactly ONE of them
(``_bus_text``), so a bus that must not carry client data (Teamhub) is never given the raw text at all. A post with no
safe line falls back to a generic one. Use ``_post_static`` ONLY for fixed strings that contain no client data.
"""
from __future__ import annotations

import logging

from ..helpdesk import guard
from . import analyse as analyse_mod
from .model import TicketState

log = logging.getLogger("twin_agent.hub")
# The ONLY values a Teamhub brief may print for these fields. ``module`` can come from free text (the client context
# lists a single module verbatim), so anything outside the known set becomes "Other".
SAFE_TYPES = frozenset({"Bug", "New Request", "Customization", "Configuration", "Inquiries"})
SAFE_TRACKS = frozenset({"inquiry", "functional", "development"})
SAFE_MODULES = frozenset({n for n, _ in analyse_mod._MODULES} | {"unknown"})


def _allowed(value: str, allowed: frozenset) -> str:
    return value if value in allowed else "Other"


TEST_CLIENT_TAG = "(test client)"


def _test_client_tag(state: TicketState, safe: str | None) -> str | None:
    """A ticket of a production test client (a staff phone in an open window) says so in every SAFE Teamhub line, so a
    teammate never mistakes it for a real customer. No client text is added."""
    if not safe or not str(state.client or "").startswith(guard.TEST_CLIENT_PREFIX) or TEST_CLIENT_TAG in safe:
        return safe
    head = f"Ticket #{state.tid}"
    return safe.replace(head, f"{head} {TEST_CLIENT_TAG}", 1) if head in safe else f"{safe} {TEST_CLIENT_TAG}"


GENERIC_TRIAGE = "Hub notice: details are in the Telegram triage chat."


class TeamPosts:
    # ---------------------------------------------------------------- threads

    def _thread(self, state: TicketState) -> str:
        """The thread ref ON THE LIVE BUS ('' = none: open one). A ticket made under another bus (the operator switched
        telegram <-> teamhub on the same store) has no thread here yet. Pre-bus tickets carry only ``topic_id``."""
        bus = state.thread_bus or ("telegram" if state.topic_id or state.thread_ref else "")
        ref = state.thread_ref or (str(state.topic_id) if state.topic_id else "")
        return ref if ref and bus == self.team_bus.name else ""

    def _ensure_thread(self, state: TicketState) -> str:
        """Open (once) this ticket's thread on the live bus, lazily if it was created under the other bus."""
        ref = self._thread(state)
        if ref or state.owner is None:
            return ref
        brief = self._brief(state) if self.team_bus.carries_client_text else self._brief_safe(state)
        state.thread_ref = self.team_bus.open_thread(state.tid, state.owner, brief)
        state.thread_bus = self.team_bus.name
        state.topic_id = int(state.thread_ref) if self.team_bus.owns_approval_topic else 0
        self._save(state)
        return state.thread_ref

    def _approval_topic(self, state: TicketState) -> int:
        """Where the Telegram approval gateway posts this ticket's request: the per-ticket topic with the Telegram
        bus, else 0 (= the general approvals chat). Approvals NEVER move to another bus."""
        if not self.team_bus.owns_approval_topic:
            return 0
        self._ensure_thread(state)
        return state.topic_id

    def _bus_text(self, text: str, safe: str | None, tid: int) -> str:
        if self.team_bus.carries_client_text:
            return text
        return safe if safe else f"Ticket #{tid}: update (details in Odoo)."

    def _post(self, state: TicketState, text: str, *, safe: str | None = None) -> None:
        """Best-effort team post: a bus hiccup must never strand a ticket mid-transition."""
        try:
            ref = self._ensure_thread(state)
            if ref:
                self.team_bus.post(ref, self._bus_text(text, _test_client_tag(state, safe), state.tid))
        except Exception as e:                               # noqa: BLE001
            log.error("topic post failed on ticket %s: %s", state.tid, type(e).__name__)

    def _post_static(self, state: TicketState, text: str) -> None:
        """``text`` is a FIXED string with no client data in it, so it is its own safe line."""
        self._post(state, text, safe=text)

    def _triage(self, text: str, *, safe: str | None = None) -> None:
        """Telegram-bus: the full text. Teamhub: only the safe line goes to Teamhub, and the FULL text goes to the Telegram
        triage chat (which may carry client text), so the generic Teamhub line is always true."""
        if self.team_bus.carries_client_text:
            self._triage_to(self.team_bus, text)
            return
        self._triage_to(self.team_bus, safe or GENERIC_TRIAGE)
        self._triage_to(self.telegram, text)

    @staticmethod
    def _triage_to(sink, text: str) -> None:
        try:
            sink.post_triage(text)
        except Exception as e:                               # noqa: BLE001
            log.error("triage post failed: %s", type(e).__name__)

    def _triage_static(self, text: str) -> None:
        self._triage(text, safe=text)

    # ---------------------------------------------------------------- the safe brief

    def _brief_safe(self, state: TicketState) -> str:
        """STRUCTURED fields only (id, owner, type/track/module, a count, a link). NO client text at all, not even a
        redacted or truncated summary: redaction cannot catch names, IDs, amounts or an injection string."""
        a, link = state.analysis, (self.cfg.get("team_links") or {}).get("odoo_ticket")
        linked = f" (follow-up of #{state.previous_ticket_id})" if state.previous_ticket_id else ""
        lines = [_test_client_tag(state, f"Ticket #{state.tid}{linked} -> @{state.owner.name}"),
                 f"Type {_allowed(a.ticket_type, SAFE_TYPES)} · track {_allowed(a.track, SAFE_TRACKS)} · "
                 f"module {_allowed(a.module, SAFE_MODULES)}",
                 f"Open questions: {len(a.open_questions)}"]
        if link:
            lines.append(f"Odoo: {str(link).format(tid=state.tid)}")
        return "\n".join(lines)
