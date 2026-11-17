"""Parse Acme help@ notification mail (acks, stage changes, public messages) into a TicketUpdate.

Owner: CHANNELS. Used through `tickets.parse_ticket_mail`. Parsing only: this module can link a mail to an
existing request, it can NEVER open a request or trigger a ticket (red-team #12).

Ticket number sources, most reliable first (from real Acme mail shapes):
  1. Message-ID / References / In-Reply-To: "...-openerp-9001-helpdesk.ticket@..."
  2. Subject "(#9001)"
  3. Portal URL "/my/ticket/9001/<token>" or "/my/ticket/9001?access_token=..."
  4. Body "The reference of your ticket is 9001"
  5. "res_id=9001" inside (URL-encoded) "View Helpdesk Ticket" redirect links
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from ..models import Event
from ..redact import redact, strip_url_token

PORTAL = "https://helpdesk.example.com/my/ticket/{id}"
TEST_MARK = "[TEST - please close]"

# Stage names as spelled in Acme Odoo (note the typo "Requsted"). Ids are unknown until Riley confirms them;
# config `tickets.stage_ids` ({name: id}) fills them in. Matching is by name, the id is reported when known.
STAGE_NAMES = ["New", "On Hold", "Functional", "Pending Internal Review", "Customer Feedback",
               "Development Requsted", "Development Submitted (Backlog)", "Acme / Client Testing (Client Review)",
               "Live (Deployment)", "Solved", "Closed", "Cancelled"]

_REQ = re.compile(r"\b(REQ-\d{4,})(?:-([a-c]))?\b")
_REF = re.compile(r"(?im)^\s*ref:\s*(REQ-\d{4,})(?:-([a-c]))?\b")
_ID_SOURCES = [
    re.compile(r"openerp-(\d{2,7})-helpdesk\.ticket"),
    re.compile(r"\(#(\d{2,7})\)"),
    re.compile(r"/my/tickets?/(\d{2,7})\b"),
    re.compile(r"(?i)reference of your ticket is\s*#?(\d{2,7})"),
    re.compile(r"model=helpdesk\.ticket&res_id=(\d{2,7})"),
]
_URL = re.compile(r"https://helpdesk\.example\.com/my/tickets?/\d+[^\s<>\")\]]*")


@dataclass
class TicketUpdate:
    request_id: str | None
    ticket_id: str | None
    ticket_url: str | None
    kind: str                      # "ack" | "stage" | "message" | "closed"
    stage_id: int | None
    text: str                      # short, redacted, for the card / relay
    part: str | None = None        # "a"/"b"/"c" for split tickets (added 2026-11-02, CHANNELS)
    stage: str | None = None       # stage name when kind == "stage"
    message_id: str = ""           # RFC Message-ID of this notification


def normalize_subject(s: str) -> str:
    """Compare subjects across our mail and Odoo's ack: drop Re:/Fwd:, TEST mark, [Globex ..] tag and (#N)."""
    s = s or ""
    s = re.sub(r"(?i)^\s*((re|fwd?|aw)\s*:\s*)+", "", s)
    s = s.replace(TEST_MARK, " ")
    s = re.sub(r"\[\s*globex[^\]]*\]", " ", s, flags=re.I)
    s = re.sub(r"\(#\d+\)", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _ticket_id(ev: Event, body: str) -> str | None:
    h = ev.headers
    sources = [" ".join([h.get("message-id", ""), h.get("references", ""), h.get("in-reply-to", "")]),
               ev.subject, body, body, body]
    for rx, text in zip(_ID_SOURCES, sources):
        m = rx.search(text)
        if m:
            return m.group(1)
    return None


def _request_ref(ev: Event, body: str, pending: list[dict] | None) -> tuple[str | None, str | None]:
    m = _REF.search(body) or _REQ.search(ev.headers.get("x-principal-req", "")) or _REQ.search(ev.subject)
    if m:
        return m.group(1), m.group(2)
    refs = " ".join([ev.headers.get("references", ""), ev.headers.get("in-reply-to", "")])
    subj = normalize_subject(ev.subject)
    for p in pending or []:
        if p.get("message_id") and p["message_id"].strip("<>") in refs:
            return p["request_id"], p.get("part")
    for p in pending or []:
        ps = normalize_subject(p.get("subject", ""))
        if ps and (ps == subj or f"your request {ps} has been received" in body.lower()):
            return p["request_id"], p.get("part")
    return None, None


def _stage(body: str, subject: str) -> str | None:
    text = f"{subject}\n{body}"
    for name in sorted(STAGE_NAMES, key=len, reverse=True):
        if re.search(r"(?i)stage[^\n]{0,40}" + re.escape(name), text) or \
                re.search(r"(?i)" + re.escape(name) + r"[^\n]{0,10}\(stage\)", text):
            return name
    return None


def parse(ev: Event, *, pending: list[dict] | None = None, stage_ids: dict[str, int] | None = None,
          is_auto: bool = True) -> TicketUpdate | None:
    """Return a TicketUpdate for Acme ticket mail, else None. Never opens anything."""
    body = unquote(ev.text or "")
    ticket_id = _ticket_id(ev, body)
    req, part = _request_ref(ev, body, pending)
    if not is_auto or (ticket_id is None and req is None):
        return None
    url = None
    m = _URL.search(body)
    if m:
        url = strip_url_token(m.group(0).rstrip(".,;"))
    elif ticket_id:
        url = PORTAL.format(id=ticket_id)
    low = body.lower()
    stage = _stage(body, ev.subject)
    if "has been received" in low or "reference of your ticket" in low:
        kind = "ack"
    elif stage in ("Solved", "Closed", "Cancelled"):
        kind = "closed"
    elif stage:
        kind = "stage"
    else:
        kind = "message"
    snippet = re.sub(r"\s+", " ", body.split("\n>")[0])[:400]
    return TicketUpdate(request_id=req, ticket_id=ticket_id, ticket_url=url, kind=kind,
                        stage_id=(stage_ids or {}).get(stage) if stage else None, text=redact(snippet),
                        part=part, stage=stage, message_id=ev.headers.get("message-id", ""))
