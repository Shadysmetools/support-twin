"""Acme tickets by email to help@example.com, sent from Principal's ticket mailbox (05 section 4).

Owner: CHANNELS (CONTRACT 9.9). This module only BUILDS messages and PARSES mail. It never sends: the sender
does that after the guards and Principal's tap (ticket creation is Co-pilot in every mode).
- build_ticket_email: 05 4.2 shape + TICKET-STYLE (Title Case, "-" bullets, no em dash, no Markdown), redacted.
  The first 3 tickets carry "[TEST - please close]" (count of sent ticket emails is passed in; unknown = TEST).
- dedupe: skip when help@ is already on the source mail; reply on the open ticket's thread when one exists
  with the same class and record.
- parse_ticket_mail / is_auto_mail: Acme and auto-reply mail can update a request, never open one.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ..models import Channel, Event, OutboundMessage, OutKind
from ..redact import redact
from . import gmail_common as gc
from . import ticket_parse
from .ticket_parse import TEST_MARK, TicketUpdate, normalize_subject  # noqa: F401  (re-exported)

from .. import profile as _profile
HELP = _profile.get("helpdesk_email")
ACME_DOMAINS = list(_profile.get("our_domains"))          # also matches helpdesk.example.com, info@, help@
TEST_FIRST_N = 3
CLOSE_LINE = f"- {_profile.get('client_name')} will test. Please share the plan and timeline."
_SMALL = {"a", "an", "the", "and", "or", "but", "for", "nor", "on", "at", "to", "by", "in", "of", "up", "as",
          "via", "per", "vs", "from", "with", "into", "when"}


# ----------------------------------------------------------------- auto mail

def is_auto_mail(ev: Event) -> bool:
    """Acme mail (any @example.com / helpdesk.example.com sender), Odoo mail, or an auto-reply."""
    h = {k.lower(): v for k, v in (ev.headers or {}).items()}
    sender = (ev.sender.email or gc.address(h.get("from", ""))).lower()
    if sender and gc.domain_match(sender, ACME_DOMAINS):
        return True
    if h.get("auto-submitted", "no").strip().lower() not in ("", "no"):
        return True
    if h.get("x-autoreply") or h.get("x-autorespond"):
        return True
    if h.get("precedence", "").strip().lower() in ("bulk", "auto_reply", "junk", "list"):
        return True
    return "openerp-" in h.get("message-id", "")


def parse_ticket_mail(ev: Event, *, pending: list[dict] | None = None,
                      stage_ids: dict[str, int] | None = None) -> TicketUpdate | None:
    """Link Acme mail to a request. `pending` = our sent tickets [{request_id, part, subject, message_id}]
    so acks that only echo the subject still link. NEVER opens a request."""
    return ticket_parse.parse(ev, pending=pending, stage_ids=stage_ids, is_auto=is_auto_mail(ev))


# ----------------------------------------------------------------- style

def title_case(s: str) -> str:
    words = s.split()
    out = []
    for i, w in enumerate(words):
        if any(c.isupper() for c in w[1:]) or any(c.isdigit() for c in w) or "/" in w or w.startswith('"'):
            out.append(w)                    # keep IDs, product names and quoted field names as-is
        elif i and w.lower() in _SMALL:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def style_line(s: str) -> str:
    """One line in house style: no em/en dash, no Markdown emphasis, bullets as '- '."""
    s = s.replace("—", ", ").replace("–", "-").replace("**", "").replace("`", "").replace("\t", " ")
    s = re.sub(r"^\s*#+\s*", "", s)
    s = re.sub(r"^\s*(?:[•*]|\(-\))\s+", "- ", s)
    return s.rstrip()


def _bullets(body: str) -> list[str]:
    lines = []
    for raw in (body or "").splitlines():
        line = style_line(raw)
        if not line.strip() or line.strip().lower() in ("request", "ref:") or line.strip().lower().startswith("ref:"):
            continue
        lines.append(line if line.lstrip().startswith("- ") else "- " + line.strip())
    return lines


# ----------------------------------------------------------------- build

def ref_for(request_id: str, part: str, n_parts: int) -> str:
    return f"{request_id}-{part}" if n_parts > 1 else request_id


def build_ticket_email(request: dict, packet: dict, part: dict, cfg: dict, *,
                       sent_count: int | None = None, first_reported: str = "") -> OutboundMessage:
    """05 4.2 ticket email for one part. To help@ only. `sent_count` = ticket emails already sent (status sent);
    None (unknown) means the TEST mark is added, the safe side."""
    rid = request["id"]
    parts = ((packet.get("ticket_draft") or {}).get("parts") or [part])
    letter = part.get("part", "a")
    ref = ref_for(rid, letter, len(parts))
    company = request.get("company") or packet.get("company") or "UNKNOWN"
    client = _profile.get("client_name")
    tag = f"[{client} {company}]" if company in _profile.get("client_companies", []) else f"[{client}]"
    test_n = int((cfg.get("tickets") or {}).get("test_first_n", TEST_FIRST_N))
    is_test = sent_count is None or sent_count < test_n
    title = title_case(style_line(part.get("subject") or request.get("title") or "Support Request").strip())
    subject = f"{TEST_MARK + ' ' if is_test else ''}{tag} {title}"

    lines = ["Request"]
    if is_test:
        lines.append(f"- {TEST_MARK} This is a pilot test of Principal's assistant.")
    who = request.get("requester_name") or ""
    where = request.get("chat") or ""
    if request.get("topic"):
        where = f'{where}, {request["topic"]} topic' if where else request["topic"]
    if who or where:
        lines.append(f"- Requester: {who or 'staff member'}" + (f", via {where}" if where else ""))
    if first_reported or request.get("created_at"):
        lines.append(f"- First reported: {first_reported or request['created_at']}")
    if company in ("EG", "KSA"):
        lines.append(f"- Company: {company}")
    body = _bullets(part.get("body", ""))
    lines += [b for b in body if b.strip() != CLOSE_LINE]
    lines.append(CLOSE_LINE)
    text = redact("\n".join(lines) + f"\n\nref: {ref}")
    return OutboundMessage(
        request_id=rid, step=f"ticket-{letter}", kind=OutKind.ticket_email, channel=Channel.ticket_mail,
        text=text, to=[HELP], cc=[], subject=subject,
        draft_hash=hashlib.sha256(f"{subject}\n{text}".encode()).hexdigest())


def build_ticket_reply(request: dict, text: str, *, step: str, thread_id: str, in_reply_to: str,
                       subject: str) -> OutboundMessage:
    """Customer-side update on an existing ticket thread (05 4.4). Stays in that thread, To help@ only."""
    subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    text = redact("\n".join(style_line(x) for x in text.splitlines()))
    return OutboundMessage(request_id=request["id"], step=step, kind=OutKind.ticket_update,
                           channel=Channel.ticket_mail, text=text, to=[HELP], subject=subj,
                           thread_id=thread_id, in_reply_to=in_reply_to,
                           draft_hash=hashlib.sha256(f"{subj}\n{text}".encode()).hexdigest())


def link_back_text(url: str) -> str:
    """Posted to the requester once the ack is in (05 4.3 step 3). URL must already be token-free."""
    return f"Raised to Acme: {url}. I will update you here."


def count_sent_tickets(send_rows: list[dict]) -> int:
    """Ticket emails already sent: rows from db.sends with kind ticket_email and status sent / maybe_sent."""
    return sum(1 for r in send_rows if r.get("kind") == "ticket_email" and r.get("status") in ("sent", "maybe_sent"))


# ----------------------------------------------------------------- dedupe

@dataclass
class Dedupe:
    action: str                        # "new" | "skip" | "reply"
    reason: str = ""
    ticket: dict = field(default_factory=dict)


def dedupe(request: dict, packet: dict, part: dict, known_tickets: list[dict],
           source_headers: dict[str, str] | None = None) -> Dedupe:
    """known_tickets: [{ticket_id, subject, cls, records, thread_id, open}] built from our DB (our tickets plus
    every ack / notification seen in the ticket mailbox, so hand-filed tickets count too)."""
    h = {k.lower(): v for k, v in (source_headers or {}).items()}
    rcpts = gc.addresses(h.get("to", "")) + gc.addresses(h.get("cc", ""))
    if HELP in rcpts:
        return Dedupe("skip", "help@ is already on the source email")
    cls = (packet.get("class") or "").replace("RB ", "").strip().lower()
    records = set((packet.get("proposed_action") or {}).get("records") or []) | set(request.get("refs") or [])
    subj = normalize_subject(part.get("subject", ""))
    for t in known_tickets:
        if not t.get("open", True):
            continue
        same_cls = cls and cls != "unknown" and (t.get("cls") or "").lower() == cls
        same_rec = records & set(t.get("records") or [])
        if (same_cls and same_rec) or (subj and normalize_subject(t.get("subject", "")) == subj):
            return Dedupe("reply", f"open ticket #{t.get('ticket_id')} has the same class/record", t)
    return Dedupe("new")
