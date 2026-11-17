"""Ticket read-back (05 4.3): never trust "sent". Check for the Acme ack at 15 and 30 minutes.

Owner: CHANNELS. Pure decisions; the listener runs them on `ticket_readback_1` / `_2` timers and the
[Resend] (`rs`) / [I created it by hand: URL] (`hb`) buttons. There is never a blind resend:
- an ack found -> link the ticket number + token-free URL to the request
- 15 min, no ack -> check again at 30 min
- 30 min, no ack -> card to Principal AND the backup (Supervisor), buttons Resend / I created it by hand
- Resend first re-reads the mailbox for a late ack and our own Sent copy; a "maybe sent" is read back first.
"""
from __future__ import annotations

from typing import Iterable

from ..models import Button, Card, CardKind, Event
from .tickets import TicketUpdate, parse_ticket_mail

READBACK_1 = "ticket_readback_1"
READBACK_2 = "ticket_readback_2"


def ack_query() -> str:
    """Gmail search for recent Acme mail (the ack echoes our subject, not our body)."""
    return "from:example.com newer_than:2d"


def sent_query(rfc_message_id: str) -> str:
    """Gmail search for our own sent copy of a ticket email (the 'maybe sent' check)."""
    return f"in:sent rfc822msgid:{rfc_message_id.strip('<>')}"


def find_ack(events: Iterable[Event], pending: dict) -> TicketUpdate | None:
    """pending = {request_id, part, subject, message_id}. First update with a ticket number for that request."""
    for ev in events:
        upd = parse_ticket_mail(ev, pending=[pending])
        if upd and upd.ticket_id and upd.request_id == pending["request_id"] and \
                (upd.part or None) == (pending.get("part") or None):
            return upd
    return None


def readback_step(timer_kind: str, ack: TicketUpdate | None) -> str:
    """'linked' | 'recheck' (schedule ticket_readback_2) | 'alert' (cards to Principal + backup)."""
    if ack and ack.ticket_id:
        return "linked"
    return "recheck" if timer_kind == READBACK_1 else "alert"


def resend_decision(send_status: str, late_ack: TicketUpdate | None, sent_copy_found: bool | None) -> str:
    """After Principal taps Resend. sent_copy_found: True/False from the Sent search, None if not checked yet.
    Returns 'linked' | 'readback_first' | 'resend_ok'. A resend uses a NEW step key (resend_step) so the
    first send's idempotency record stays intact; send_status is kept for the card text / audit."""
    if late_ack and late_ack.ticket_id:
        return "linked"
    if sent_copy_found is None:
        return "readback_first"          # always re-read the mailbox (late ack + Sent copy) before a second send
    return "resend_ok"                   # Principal decided, after a read-back that found no ack


def resend_step(step: str, attempt: int) -> str:
    """'ticket-a' -> 'ticket-a-r1'. Never reuse the original send key."""
    return f"{step}-r{attempt}"


def alert_cards(request: dict, subject: str, *, minutes: int = 30) -> list[Card]:
    """No ack after 30 min: one actionable card to Principal, one info card to the backup."""
    rid = request["id"]
    text = (f"{rid}: no Acme ack after {minutes} min for the ticket email \"{subject}\".\n"
            "Nothing was resent. Check helpdesk.example.com, then choose.")
    principal = Card(kind=CardKind.ticket, text=text, request_id=rid, to="principal",
                 buttons=[[Button("Resend", "rs"), Button("I created it by hand: URL", "hb")]])
    backup = Card(kind=CardKind.ticket, text=text + "\n(Info for backup: help@ intake may be down.)",
                  request_id=rid, to="backup")
    return [principal, backup]
