"""Gmail drafts.create / drafts.send in Principal's ticket mailbox, recipient-restricted in code.

Owner: CHANNELS (CONTRACT 9.9). Only `sender.py` calls `send_draft`, after the guards and Principal's tap.
Recipient rules (checked when the draft is made AND again just before it is sent, because Principal could edit the
draft in Gmail):
- ticket_email: To exactly help@example.com, no Cc/Bcc, a new thread.
- ticket_update: To help@ only, on a known ticket thread (In-Reply-To + References + "Re:" subject).
- email_reply / email_holding: only on a known thread, recipients must be people already on that thread.
- anything else: refused.
The real adapter refuses to start unless run_mode is live, the vault holds the tokens and the mailbox has the
gmail.compose scope.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Callable, Protocol

from ..models import Channel, OutboundMessage, OutKind
from . import gmail_common as gc

from .. import profile as _profile
HELP = _profile.get("helpdesk_email")
COMPOSE = "gmail.compose"


class GmailSend(Protocol):
    def create_draft(self, msg: OutboundMessage) -> str: ...
    def send_draft(self, draft_id: str) -> str: ...


class RecipientRefused(ValueError):
    """The message's recipients break the code rules. Never sent, never drafted."""


def ticket_recipients(cfg: dict) -> list[str]:
    return [a.lower() for a in ((cfg.get("guards") or {}).get("tickets") or {}).get("recipients", [HELP])]


def check_recipients(msg: OutboundMessage, cfg: dict, known_threads: dict[str, list[str]] | None = None,
                     bcc: list[str] | None = None) -> list[str]:
    """[] = fine. known_threads: {gmail thread id: [participant addresses]} from our DB."""
    known = known_threads or {}
    to = [a.lower() for a in msg.to]
    cc = [a.lower() for a in msg.cc]
    problems: list[str] = []
    if bcc:
        problems.append("bcc is never allowed")
    if not to:
        problems.append("no recipient")
    kind = OutKind(msg.kind)
    if kind == OutKind.ticket_email:
        if to != ticket_recipients(cfg)[:1] or cc:
            problems.append("ticket email must go To help@ only, no Cc")
        if msg.thread_id:
            problems.append("a new ticket must start a new thread")
    elif kind == OutKind.ticket_update:
        if any(a not in ticket_recipients(cfg) for a in to + cc):
            problems.append("ticket update must go to help@ only")
        if msg.thread_id not in known:
            problems.append("ticket update must stay on a known ticket thread")
    elif kind in (OutKind.email_reply, OutKind.email_holding):
        if msg.thread_id not in known:
            problems.append("email reply must stay on a known thread")
        else:
            on_thread = {a.lower() for a in known[msg.thread_id]}
            if any(a not in on_thread for a in to + cc):
                problems.append("email reply may only go to people already on the thread")
    else:
        problems.append(f"kind {kind} is not sent by Gmail")
    return problems


def build_raw(msg: OutboundMessage, from_addr: str) -> str:
    """RFC 2822 message, base64url, as the Gmail API wants it. Threading headers are set together."""
    em = EmailMessage()
    if from_addr:
        em["From"] = from_addr
    em["To"] = ", ".join(msg.to)
    if msg.cc:
        em["Cc"] = ", ".join(msg.cc)
    em["Subject"] = msg.subject
    if msg.in_reply_to:
        em["In-Reply-To"] = msg.in_reply_to
        em["References"] = msg.in_reply_to
    em["X-Principal-Req"] = msg.request_id
    em.set_content(msg.text)
    return base64.urlsafe_b64encode(em.as_bytes()).decode()


class RealGmailSend:
    """Gmail drafts in one mailbox (default: the ticket mailbox)."""

    def __init__(self, cfg: dict, mailbox: str = "ticketmail", *,
                 known_threads: Callable[[], dict[str, list[str]]] | None = None,
                 service_factory: Callable[[dict, str], Any] | None = None):
        self.box = gc.require_live(cfg, mailbox, need_scope=COMPOSE)   # refuse to start otherwise
        self.cfg, self.mailbox = cfg, mailbox
        self._known = known_threads or (lambda: {})
        factory = service_factory or (lambda c, m: gc.build_service(c, m, need_scope=COMPOSE))
        self._svc = factory(cfg, mailbox)
        self._drafts: dict[str, OutboundMessage] = {}

    def create_draft(self, msg: OutboundMessage) -> str:
        problems = check_recipients(msg, self.cfg, self._known())
        if problems:
            raise RecipientRefused("; ".join(problems))
        body: dict[str, Any] = {"message": {"raw": build_raw(msg, self.box.get("address", ""))}}
        if msg.thread_id:
            body["message"]["threadId"] = msg.thread_id
        draft_id = self._svc.users().drafts().create(userId="me", body=body).execute()["id"]
        self._drafts[draft_id] = msg
        return draft_id

    def _recheck(self, draft_id: str) -> None:
        """Re-read the draft as it is now in Gmail and check its recipients again."""
        d = self._svc.users().drafts().get(userId="me", id=draft_id, format="metadata").execute()
        m = d.get("message") or {}
        h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers", [])}
        orig = self._drafts.get(draft_id)
        thread = m.get("threadId", "")
        if orig is None:
            # Draft made before a restart: apply the strictest rule that fits (help@ only).
            kind = OutKind.ticket_update if thread in self._known() else OutKind.ticket_email
            orig = OutboundMessage(request_id=h.get("x-principal-req", "?"), step="?", kind=kind,
                                   channel=Channel.ticket_mail, text="",
                                   thread_id=thread if kind == OutKind.ticket_update else "")
        live = OutboundMessage(request_id=orig.request_id, step=orig.step, kind=orig.kind, channel=orig.channel,
                               text="", to=gc.addresses(h.get("to", "")), cc=gc.addresses(h.get("cc", "")),
                               subject=h.get("subject", ""), thread_id=thread if orig.thread_id else "")
        problems = check_recipients(live, self.cfg, self._known(), bcc=gc.addresses(h.get("bcc", "")))
        if problems:
            raise RecipientRefused("; ".join(problems))

    def send_draft(self, draft_id: str) -> str:
        self._recheck(draft_id)
        sent = self._svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        self._drafts.pop(draft_id, None)
        return sent["id"]

    def rfc_message_id(self, gmail_message_id: str) -> str:
        """The sent mail's RFC Message-ID: stored so Acme' ack (References) links back to the request."""
        m = self._svc.users().messages().get(userId="me", id=gmail_message_id, format="metadata",
                                             metadataHeaders=["Message-ID"]).execute()
        for h in (m.get("payload") or {}).get("headers", []):
            if h.get("name", "").lower() == "message-id":
                return h.get("value", "")
        return ""
