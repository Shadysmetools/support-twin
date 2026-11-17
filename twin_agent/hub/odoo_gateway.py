"""CREATE / ASSIGN / RESOLVE-note writes, all inside the existing ``helpdesk/guard.py`` boundary.

Hard rules (baton): create/write tickets, post internal notes, set assignment — NO unlink, NO other models,
mail suppressed on create via context {tracking_disable, mail_create_nosubscribe, mail_create_nolog}.

Two-layer enforcement, exactly as the live desk does it:
  1. ``guard.check`` validates the (model, method, fields, stage) on the VALS only. The guard refuses any kwargs
     on create/write ("extra keyword arguments are not allowed"), so the mail-suppress CONTEXT is NOT passed
     through the guard — it is applied at the transport layer (Odoo reads ``context`` from execute_kw kwargs).
  2. The mail-suppress context is required on create here: a create missing any of the three keys is refused,
     so an Odoo stage email can never fire on our create.

The same guarded path runs against ``FakeOdoo`` (tests) and ``JsonRpcTransport`` (prod) — a test of one is a test
of the other. Binary attachments (a real video file on the ticket) are OUT of the guard's allowlist today; see
``RESOLVE`` in pipeline.py — the proof video is attached as an internal note with its Wiki share link, and a
raw ``ir.attachment`` create is refused by the guard (tested). Widening the guard for binaries is a tracked
follow-up, deliberately not done here.
"""
from __future__ import annotations

import re

from ..helpdesk import guard
from ..helpdesk.conventions import Conventions, discover, m2o
from ..helpdesk.odoo import OdooAdapter, writes_allowed
from .approval_check import StoredApproval

# The mail-suppress context every create must carry (baton / plan §4).
MAIL_SUPPRESS = {"tracking_disable": True, "mail_create_nosubscribe": True, "mail_create_nolog": True}


def mail_suppress_context() -> dict:
    return dict(MAIL_SUPPRESS)


_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _valid_email(value) -> bool:
    return isinstance(value, str) and _EMAIL.match(value.strip()) is not None


class NoClientEmail(guard.WriteRefused):
    """The client has no (valid) email in Odoo: nothing was sent. The hub holds and offers the owner a per-message
    WhatsApp choice."""


class GuardedOdoo:
    """Wraps an Odoo transport (``FakeOdoo`` or ``JsonRpcTransport``). Writes are OFF unless ``writes_on`` is True;
    in production build it with ``GuardedOdoo.from_config`` so the ``run_mode: live`` + ``write_enabled`` gate
    (``helpdesk.odoo.writes_allowed``) decides, never a default."""

    def __init__(self, transport, *, conv: Conventions | None = None, writes_on: bool = False):
        self.t = transport
        self.adapter = OdooAdapter(transport, writes_on=writes_on)
        self.conv = conv or discover(self.adapter)

    @classmethod
    def from_config(cls, transport, cfg: dict, *, section: str = "helpdesk", conv: Conventions | None = None):
        return cls(transport, conv=conv, writes_on=writes_allowed(cfg or {}, section))

    @property
    def safe_stages(self):
        return self.conv.safe_stages

    # ---------------------------------------------------------------- CREATE (mail suppressed)

    def create_ticket(self, vals: dict, *, context: dict) -> int:
        if context != MAIL_SUPPRESS:                 # EXACT: nothing missing, nothing extra (e.g. force_send)
            raise guard.WriteRefused("create refused: context must equal the mail-suppress context exactly")
        # Guard the vals/fields/stage (no kwargs through the guard — it refuses them on create).
        guard.check("helpdesk", "helpdesk.ticket", "create", [vals], safe_stages=self.conv.safe_stages)
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        # Apply the mail-suppress context at the transport (Odoo reads context from execute_kw kwargs).
        return int(self.t.execute_kw("helpdesk.ticket", "create", [vals], {"context": context}))

    # ---------------------------------------------------------------- the production test-client contact (one create)

    def create_test_client_contact(self, name: str, phone: str, comment: str = "") -> int:
        """Create ONE ``Acme Test Client - <Name>`` res.partner: name, phone and an optional comment only, no email,
        mail-off context. Guard purpose ``test_client_contact``; the caller does the find-or-create (never a duplicate)."""
        vals = {"name": name, "phone": phone, **({"comment": comment} if comment else {})}
        ctx = dict(guard.TEST_CONTEXT)
        guard.check("test_client_contact", "res.partner", "create", [vals], {"context": ctx})
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        return int(self.t.execute_kw("res.partner", "create", [vals], {"context": ctx}))

    # ---------------------------------------------------------------- intake v2: save the sender as a client contact

    def read_partner(self, partner_id: int) -> dict | None:
        rows = self.adapter.search_read("res.partner", [["id", "=", int(partner_id)]],
                                        ["name", "parent_id", "is_company"], limit=1)
        return rows[0] if rows else None

    def find_by_phone(self, phone: str) -> list[int]:
        """Dedupe before a create: ANY partner (a person, a company, a user's) whose phone or mobile ends with these digits,
        tolerant of spaces and dashes, as the live lookup is."""
        digits = "".join(c for c in phone if c.isdigit())
        pat = "%" + "%".join(digits[-8:]) + "%"
        out: set[int] = set()
        for fld in ("phone", "mobile"):
            for r in self.adapter.search_read("res.partner", [[fld, "=ilike", pat]], ["id"], limit=5):
                out.add(int(r["id"]))
        return sorted(out)

    def create_client_contact(self, name: str, phone: str, parent_id: int | None = None,
                              category_id: int | None = None) -> int:
        """Guard purpose ``client_contact``. FIND-OR-CREATE by exact phone: one hit is reused (never a duplicate), several
        hits are ambiguous and refused. Name, phone, company link and an optional tag link only; mail-off context."""
        if self.find_by_phone(phone):                      # partner_for said "no client": never reuse, never duplicate
            raise guard.WriteRefused("client contact: a partner already has this phone; a human must pick the right one")
        vals: dict = {"name": name, "phone": phone}
        if parent_id:
            vals["parent_id"] = int(parent_id)
        if category_id:
            vals["category_id"] = [(4, int(category_id))]
        ctx = dict(guard.TEST_CONTEXT)
        guard.check("client_contact", "res.partner", "create", [vals], {"context": ctx})
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        return int(self.t.execute_kw("res.partner", "create", [vals], {"context": ctx}))

    def link_client_contact(self, partner_id: int, parent_id: int) -> None:
        """Write the company onto a contact that has none (re-read first: never overwrite a company, never touch a
        company record, never a test client)."""
        row = self.read_partner(partner_id)
        if not row or row.get("is_company") or row.get("parent_id"):
            raise guard.WriteRefused("client contact: only a person without a company may be linked")
        if str(row.get("name") or "").startswith(guard.TEST_CLIENT_PREFIX):
            raise guard.WriteRefused("client contact: a test client is never linked to a company")
        args = [[int(partner_id)], {"parent_id": int(parent_id)}]
        ctx = dict(guard.TEST_CONTEXT)
        guard.check("client_contact", "res.partner", "write", args, {"context": ctx})
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        self.t.execute_kw("res.partner", "write", args, {"context": ctx})
    def close_test_client_ticket(self, tid: int, stage_id: int, team_stage_ids) -> None:
        """Move ONE test client's ticket to the configured close stage (guard purpose ``test_client_close``: it re-reads
        the live ticket and partner itself), with the mail-off context."""
        args, ctx = [[int(tid)], {"stage_id": int(stage_id)}], dict(guard.TEST_CONTEXT)
        read = lambda m, ids, f: self.t.execute_kw(m, "read", [list(ids)], {"fields": list(f)})   # noqa: E731
        guard.check("test_client_close", "helpdesk.ticket", "write", args, {"context": ctx},
                    close_stage_id=int(stage_id), team_stage_ids=team_stage_ids, read_fn=read)
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        self.t.execute_kw("helpdesk.ticket", "write", args, {"context": ctx})

    # ---------------------------------------------------------------- ASSIGN (write user_id only)

    def assign(self, tid: int, user_id: int) -> None:
        vals = {"user_id": int(user_id)}
        args = [[int(tid)], vals]
        guard.check("helpdesk", "helpdesk.ticket", "write", args, safe_stages=self.conv.safe_stages)
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        self.t.execute_kw("helpdesk.ticket", "write", args, {"context": mail_suppress_context()})

    # ---------------------------------------------------------------- internal NOTE (mail.mt_note only)

    def post_note(self, tid: int, body: str) -> None:
        kw = {"body": body, "subtype_xmlid": "mail.mt_note", "message_type": "comment"}
        guard.check("helpdesk", "helpdesk.ticket", "message_post", [[int(tid)]], kw)
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        self.t.execute_kw("helpdesk.ticket", "message_post", [[int(tid)]], kw)

    # ---------------------------------------------------------------- client EMAIL (owner-approved, team alias)

    def _verify_recipients(self, tid: int, partner_id: int) -> None:
        """Right before the post, read the LIVE ticket and client: the client must still be the expected partner and HAVE
        a valid email address, and no OTHER follower may be external (a ``mail.mt_comment`` post also notifies followers).
        Reads only; any doubt (including a read error) refuses with "nothing was sent". Internal followers (Odoo users)
        are not refused: Odoo may email them per their own notification setting."""
        try:
            self._verify_reads(int(tid), int(partner_id))
        except guard.WriteRefused:
            raise
        except Exception as e:                               # noqa: BLE001 - the post never happened
            raise guard.WriteRefused(f"client email: could not check the ticket in Odoo ({type(e).__name__}); "
                                     "nothing was sent") from None

    def _verify_reads(self, tid: int, partner_id: int) -> None:
        rows = self.adapter.search_read("helpdesk.ticket", [["id", "=", tid]], ["partner_id", "message_partner_ids"],
                                        limit=1)
        if not rows:
            raise guard.WriteRefused("client email: the ticket could not be read")
        if m2o(rows[0].get("partner_id")) != partner_id:
            raise guard.WriteRefused("client email: the ticket's client changed in Odoo since it was opened")
        others = sorted({int(f) for f in (rows[0].get("message_partner_ids") or [])} - {partner_id})
        found = {int(r["id"]): r for r in self.adapter.search_read(
            "res.partner", [["id", "in", others + [partner_id]]], ["partner_share", "email"])}
        if not _valid_email((found.get(partner_id) or {}).get("email")):
            raise NoClientEmail("client has no email in Odoo; add it, then re-approve")
        for f in others:
            if f not in found or found[f].get("partner_share") is not False:       # unknown = external (fail closed)
                raise guard.WriteRefused("client email: the ticket has an external follower besides the client")

    def send_client_email(self, tid: int, body: str, *, partner_id: int, email_from: str, aliases, store,
                          nonce: str) -> None:
        """An owner-approved follow-up to the client by EMAIL: a comment from the team alias to the ticket's own partner,
        no personal signature, a client-friendly subject. The gateway builds the stored-approval check ITSELF
        (``approval_check.StoredApproval``: consumed, allowed approver, this ticket, the approved text, not used yet) and marks it
        used right before the post (one approval, one email). The live ticket and the client's email are re-read first;
        then ``guard._check_client_email`` enforces the alias set, the recipient, the subject and the HTML rule."""
        approval = StoredApproval(store, nonce, tid)
        matched = approval.check(body)
        self._verify_recipients(tid, partner_id)
        kw = {"body": guard.render_client_email_html(body), "body_is_html": True,
              "subject": f"{guard.CLIENT_EMAIL_SUBJECT}{int(tid)}", "subtype_xmlid": "mail.mt_comment",
              "message_type": "comment", "partner_ids": [int(partner_id)], "email_from": email_from,
              "email_add_signature": False}
        gate = {"nonce_consumed": True, "body_sha": matched, "plain_body": body, "aliases": sorted(aliases),
                "partner_id": int(partner_id), "recipients_verified": True, "ticket_id": int(tid)}
        guard.check("helpdesk", "helpdesk.ticket", "message_post", [[int(tid)]], kw, client_email=gate)
        if not self.adapter.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off")
        approval.claim()
        self.t.execute_kw("helpdesk.ticket", "message_post", [[int(tid)]], kw)

    # ---------------------------------------------------------------- reads (routing / dedupe, guarded)

    def search_read(self, model, domain, fields, **kw):
        return self.adapter.search_read(model, domain, fields, **kw)

    def open_tickets_for(self, user_id: int) -> int:
        """Current open load for a candidate (routing). Open = not in a closed stage; the fake seeds stage 2."""
        rows = self.adapter.search_read("helpdesk.ticket", [["user_id", "=", int(user_id)]], ["stage_id"])
        closed = {3, 4}   # Closed, Cancelled (FakeOdoo stage ids)
        from ..helpdesk.conventions import m2o
        return sum(1 for r in rows if m2o(r.get("stage_id")) not in closed)


def team_id_for(track: str, conv: Conventions) -> int:
    """Teams 2 (Helpdesk Support) and 11 (Customer Inquiries) per the baton. Inquiries -> 11, else -> 2."""
    want = 11 if track == "inquiry" else 2
    return want if want in conv.teams else (2 if 2 in conv.teams else next(iter(conv.teams), 2))


def x_type_id(ticket_type: str, conv: Conventions) -> int | None:
    return conv.types.get(str(ticket_type).lower())


def x_track_value(track: str, conv: Conventions) -> str:
    """Map the analysis track onto a value the live x_track selection actually offers (fallback 'functional').
    The richer 'inquiry' label is preserved in the analysis + the internal note even when x_track stores it as
    'functional' (the live selection only offers functional/development in the discovered conventions)."""
    vals = set(conv.track_values or [])
    if track in vals:
        return track
    return "functional" if "functional" in vals else (next(iter(vals)) if vals else "functional")
