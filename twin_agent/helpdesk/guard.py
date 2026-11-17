"""The Odoo write guard: an allowlist of (model, method, fields) enforced in code, above every transport.

Never AI. The real adapter and FakeOdoo go through the same `check()`, so a test of one is a test of the other.

- Reads: only the models in READ_MODELS, only the methods in READ_METHODS.
- Writes, by purpose:
  - "helpdesk":  helpdesk.ticket create / write (one record) and message_post as an internal NOTE only;
  - "timesheet": account.analytic.line create only.
- Never: unlink, any `ir.*` model (so nobody can recreate or rename Supervisor's fields), `x_type_code` anywhere
  (writing it renames the ticket type globally), a field outside the allowlist, or a stage outside the safe set.
- A ticket may only enter a stage that does not email the client (the safe set, confirmed at runtime). The one
  exception is the exact stage Supervisor approved with her tap (`gated_stage`).
- `create` without `stage_id` would land in stage 1 "New", which emails the client: refused.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

READ_METHODS = frozenset({"fields_get", "search_read", "read", "search_count", "search"})
READ_MODELS = frozenset({"helpdesk.ticket", "helpdesk.stage", "helpdesk.team", "helpdesk.tag", "x_ticket_type",
                         "res.partner", "res.users", "mail.template", "hr.employee", "project.project",
                         "project.task", "account.analytic.line", "mail.message", "mail.mail"})
MAIL_MAIL_METHODS = frozenset({"search_count", "search"})   # mail.mail: a COUNT or ids only (the test-ticket mail check), never rows
TICKET_FIELDS = frozenset({"name", "description", "team_id", "stage_id", "tag_ids", "partner_id", "priority",
                           "user_id", "x_track", "x_type", "x_ai_confidence", "x_task_ids"})
TIMESHEET_FIELDS = frozenset({"name", "unit_amount", "date", "employee_id", "helpdesk_ticket_id", "project_id",
                              "task_id"})
FORBIDDEN_FIELDS = frozenset({"x_type_code"})
NOTE_KWARGS = frozenset({"body", "subtype_xmlid", "message_type", "body_is_html"})
# Principal 2026-11-12: the routed-ticket email is a comment to HIS partner only. He is the author too (the twin posts as
# his user), so Odoo needs mail_notify_author to email him. Nothing else may ride along.
COMMENT_KWARGS = NOTE_KWARGS | {"partner_ids", "context"}
COMMENT_CONTEXT = {"mail_notify_author": True}
# 2026-11-15 hub client email: the ONLY other comment. An owner-approved follow-up to the CLIENT, sent from the helpdesk
# team's alias (never a personal mailbox), to the ticket's own partner only, with no author signature.
CLIENT_EMAIL_KWARGS = frozenset({"body", "subtype_xmlid", "message_type", "partner_ids", "email_from",
                                 "email_add_signature", "subject", "body_is_html"})
CLIENT_EMAIL_SUBJECT = "Your Acme support request #"


def render_client_email_html(plain: str) -> str:
    """The ONLY HTML a client email may carry: the approved text, escaped, line breaks kept as <br/>."""
    import html
    return "<br/>".join(html.escape(line) for line in str(plain).splitlines())
WRITES = {
    "helpdesk": frozenset({("helpdesk.ticket", "create"), ("helpdesk.ticket", "write"),
                           ("helpdesk.ticket", "message_post")}),
    "timesheet": frozenset({("account.analytic.line", "create")}),
    # Teamhub test tickets only (teamhub_test_ticket.py): the TEST client, a "TEST · " ticket, and its stage move.
    "test_ticket": frozenset({("res.partner", "create"), ("helpdesk.ticket", "create"), ("helpdesk.ticket", "write")}),
    # Production time-limited test client (hub/test_clients.py): ONE res.partner create, nothing else.
    "test_client_contact": frozenset({("res.partner", "create")}),
    # Intake v2 (hub/intake_flow.py): save the WhatsApp sender as a client contact, and link it to its company. ONE
    # res.partner create, or ONE write on one partner (company/tag only). Never an email, a user or a delete.
    "client_contact": frozenset({("res.partner", "create"), ("res.partner", "write")}),
    # Production test-client autopilot (hub/autopilot.py): move ONE test client's ticket to the configured close stage.
    "test_client_close": frozenset({("helpdesk.ticket", "write")}),
}
CLIENT_CONTACT_FIELDS = frozenset({"name", "phone", "mobile", "parent_id", "category_id"})
CLIENT_CONTACT_WRITE_FIELDS = frozenset({"parent_id", "category_id"})    # a write never renames or re-phones a partner
_CLIENT_NAME_BAD = re.compile(r"[\x00-\x1f<>\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_CLIENT_PHONE = re.compile(r"\+?[0-9]{8,15}")
TEST_PREFIX = "TEST · "
TEST_PARTNER = "TEST Client (Support Twin)"
TEST_TEAM = 2
NEVER_TEST_STAGES = (9, 17)     # 9 auto-closes and emails "Solved"; 17 is off limits
TEST_CONTEXT = {"tracking_disable": True, "mail_create_nolog": True, "mail_create_nosubscribe": True,
                "mail_auto_subscribe_no_notify": True, "mail_notrack": True}
TEST_CLIENT_PREFIX = "Acme Test Client – "          # exactly this, with an en dash and a trailing space
TEST_CLIENT_FIELDS = frozenset({"name", "phone", "mobile", "comment"})
_TEST_CLIENT_NAME = re.compile(r"[A-Za-z][A-Za-z ]{0,38}")
_TEST_CLIENT_PHONE = re.compile(r"\+?[0-9]{8,15}")
TEST_TICKET_FIELDS = frozenset({"name", "description", "team_id", "stage_id", "partner_id", "user_id"})
MAX_TIMESHEET_HOURS = 12.0


class WriteRefused(PermissionError):
    """The guard said no. The message names the rule, never a value."""


def _check_comment_to_principal(kwargs: dict, notify_partner: int | None) -> None:
    """The one comment allowed: an email to Principal's own partner about a ticket the twin routed."""
    if not notify_partner:
        raise WriteRefused("message_post: only an internal note, or a comment to Principal's partner (needs helpdesk.analyst.notify_partner_id)")
    extra = set(kwargs) - COMMENT_KWARGS
    if extra:
        raise WriteRefused(f"message_post comment: not allowed: {', '.join(sorted(extra))}")
    if kwargs.get("message_type", "comment") != "comment":
        raise WriteRefused("message_post comment: message_type must be comment")
    if list(kwargs.get("partner_ids") or []) != [int(notify_partner)]:
        raise WriteRefused("message_post comment: it may go to Principal's partner only")
    if dict(kwargs.get("context") or {}) != COMMENT_CONTEXT:
        raise WriteRefused("message_post comment: only the mail_notify_author context is allowed")
    if not str(kwargs.get("body") or "").strip():
        raise WriteRefused("message_post: empty comment")


def _check_client_email(kwargs: dict, gate: dict) -> None:
    """An outbound client comment. ``gate`` is built by ``GuardedOdoo.send_client_email`` AFTER it re-read the stored,
    consumed approval (``nonce_consumed``, ``body_sha`` = the approved hash) and the LIVE ticket (``recipients_verified``:
    the live partner is the expected client and no other follower is external), plus ``aliases`` (the configured sender
    set) and ``partner_id``. Every rule fails closed and names the rule, never a value."""
    import hashlib
    if gate.get("nonce_consumed") is not True:
        raise WriteRefused("client email: no consumed owner approval for this draft")
    if gate.get("recipients_verified") is not True:
        raise WriteRefused("client email: the recipients were not verified against the live ticket")
    body, plain = str(kwargs.get("body") or ""), str(gate.get("plain_body") or "")
    if not body.strip() or not plain.strip():
        raise WriteRefused("client email: empty message")
    if not gate.get("body_sha") or hashlib.sha256(plain.encode("utf-8")).hexdigest() != gate["body_sha"] \
            or body != render_client_email_html(plain) or kwargs.get("body_is_html") is not True:
        raise WriteRefused("client email: the text is not the approved draft")
    extra = set(kwargs) - CLIENT_EMAIL_KWARGS
    if extra:
        raise WriteRefused(f"client email: not allowed: {', '.join(sorted(extra))}")
    if kwargs.get("message_type", "comment") != "comment":
        raise WriteRefused("client email: message_type must be comment")
    sender = str(kwargs.get("email_from") or "").strip().lower()
    if not sender or sender not in {str(a).strip().lower() for a in gate.get("aliases") or ()}:
        raise WriteRefused("client email: email_from is not a configured helpdesk alias")
    partner = gate.get("partner_id")
    if type(partner) is not int or partner <= 0 or list(kwargs.get("partner_ids") or []) != [partner]:
        raise WriteRefused("client email: it may go to the ticket's own client only")
    if str(kwargs.get("subject") or "") != f"{CLIENT_EMAIL_SUBJECT}{gate.get('ticket_id')}":
        raise WriteRefused("client email: the subject must be exactly the client-friendly request subject")
    if kwargs.get("email_add_signature") is not False:
        raise WriteRefused("client email: the personal signature must be switched off")


def _id_of(v) -> int:
    return int(v[0]) if isinstance(v, (list, tuple)) and v else (int(v) if isinstance(v, int) and v is not True else 0)


def _live_test_contact(read_fn, test_partner_id: int) -> None:
    p = read_fn("res.partner", [test_partner_id], ["name", "email", "phone", "mobile"]) if read_fn else None
    if not p or p[0].get("name") != TEST_PARTNER or p[0].get("email") or p[0].get("phone") or p[0].get("mobile"):
        raise WriteRefused("helpdesk.ticket: the contact is not the TEST contact with no email, phone or mobile")


def _live_test_ticket(args: list, read_fn, test_partner_id: int) -> None:
    """The stage write re-reads the LIVE ticket and its partner itself: a TEST name, team 2, the checked TEST
    contact, and that contact still has no email, phone or mobile. It never trusts what the caller says."""
    tid = _one_id(args, "write")
    rows = read_fn("helpdesk.ticket", [tid], ["name", "partner_id", "team_id"]) if read_fn else None
    row = rows[0] if rows else {}
    if not str(row.get("name") or "").startswith(TEST_PREFIX) or _id_of(row.get("team_id")) != TEST_TEAM \
            or _id_of(row.get("partner_id")) != test_partner_id:
        raise WriteRefused("helpdesk.ticket.write: the live ticket is not a TEST ticket of the TEST contact")
    _live_test_contact(read_fn, test_partner_id)


def _check_test_stage(vals: dict, team_stage_ids, safe_stages: set[int], gated_stage: int | None, *, create: bool) -> None:
    sid = vals.get("stage_id")
    if type(sid) is int and sid in NEVER_TEST_STAGES:
        raise WriteRefused(f"stage {sid} is never allowed for a test ticket")
    if type(sid) is int and sid not in {int(x) for x in (team_stage_ids or ())}:
        raise WriteRefused("stage_id is not a stage of the test team")
    _check_stage(vals, safe_stages, gated_stage, required=create)


def _check_test_ticket(model: str, method: str, args: list, kwargs: dict, safe_stages: set[int],
                       gated_stage: int | None, self_uid: int | None = None, test_partner_id: int | None = None,
                       team_stage_ids=None, read_fn=None) -> None:
    """The narrowest allowance for the Teamhub test flow. Only the mail-off context; never a real client or ticket.
    create: a ``TEST \u00b7`` name, team 2, the checked TEST contact id (``test_partner_id``, required), a stage of
    the team (never 9 or 17), and ``user_id`` only if it is the authenticated uid. write: ``stage_id`` only, and the
    guard re-reads the live ticket and contact itself (``read_fn``)."""
    if kwargs != {"context": TEST_CONTEXT}:
        raise WriteRefused(f"{model}.{method}: only the exact mail-off context is allowed")
    vals = _vals_of(method, args)
    if model == "res.partner":
        if set(vals) != {"name"} or vals["name"] != TEST_PARTNER:
            raise WriteRefused("res.partner.create: only the TEST client (name only) may be created")
        return
    if type(test_partner_id) is not int or test_partner_id <= 0:
        raise WriteRefused("helpdesk.ticket: the checked TEST contact id is required")
    if method == "write":
        if set(vals) != {"stage_id"}:
            raise WriteRefused("helpdesk.ticket.write: only the stage of a TEST ticket may change")
        _live_test_ticket(args, read_fn, test_partner_id)
    else:
        _check_fields(vals, TEST_TICKET_FIELDS, "helpdesk.ticket.create")
        if not str(vals.get("name") or "").startswith(TEST_PREFIX) or vals.get("team_id") != TEST_TEAM:
            raise WriteRefused("helpdesk.ticket.create: a TEST ticket needs a TEST name and team 2")
        if type(vals.get("partner_id")) is not int or vals["partner_id"] != test_partner_id:
            raise WriteRefused("helpdesk.ticket.create: the contact must be the checked TEST contact")
        _live_test_contact(read_fn, test_partner_id)      # re-read live here too: never trust the declared id
        if "user_id" in vals and (type(self_uid) is not int or self_uid <= 0 or type(vals["user_id"]) is not int
                                  or vals["user_id"] != self_uid):
            raise WriteRefused("helpdesk.ticket.create: user_id may only be the authenticated account itself")
    _check_test_stage(vals, team_stage_ids, safe_stages, gated_stage, create=method == "create")


def _check_test_client_contact(args: list, kwargs: dict) -> None:
    """The narrowest allowance for the production test-client window: create ONE ``res.partner`` named
    ``Acme Test Client – <Name>`` with only name, phone/mobile and an optional comment. NEVER an email (a test
    client can not be emailed), never a link to a user or company, and only with the mail-off context."""
    if kwargs != {"context": TEST_CONTEXT}:
        raise WriteRefused("res.partner.create: only the exact mail-off context is allowed")
    if len(args) != 1:
        raise WriteRefused("res.partner.create: exactly one argument (the vals)")
    vals = _vals_of("create", args)
    if "email" in vals:
        raise WriteRefused("res.partner.create: a test client has no email")
    _check_fields(vals, TEST_CLIENT_FIELDS, "res.partner.create")
    name = vals.get("name")
    if not isinstance(name, str) or not name.startswith(TEST_CLIENT_PREFIX) \
            or not _TEST_CLIENT_NAME.fullmatch(name[len(TEST_CLIENT_PREFIX):]):
        raise WriteRefused("res.partner.create: the name must be 'Acme Test Client – <Name>'")
    phones = [vals[k] for k in ("phone", "mobile") if k in vals]
    if not phones or any(not isinstance(p, str) or not _TEST_CLIENT_PHONE.fullmatch(p) for p in phones):
        raise WriteRefused("res.partner.create: a test client needs a phone or mobile (digits only)")
    note = vals.get("comment", "")
    if not isinstance(note, str) or len(note) > 300:
        raise WriteRefused("res.partner.create: comment must be short text")


def _check_client_contact(method: str, args: list, kwargs: dict) -> None:
    """Purpose ``client_contact``: the mail-off context only; create ONE partner (name, phone/mobile, parent_id, category
    link) or write parent_id / a category link on ONE partner. No email, no user, no unlink (refused above)."""
    if kwargs != {"context": TEST_CONTEXT}:
        raise WriteRefused(f"res.partner.{method}: only the exact mail-off context is allowed")
    if method == "create":
        if len(args) != 1:
            raise WriteRefused("res.partner.create: exactly one argument (the vals)")
        allowed = CLIENT_CONTACT_FIELDS
    else:
        _one_id(args, "res.partner.write")
        allowed = CLIENT_CONTACT_WRITE_FIELDS
    vals = _vals_of(method, args)
    if "email" in vals or "user_ids" in vals or "user_id" in vals:
        raise WriteRefused(f"res.partner.{method}: no email and no user on a client contact")
    _check_fields(vals, allowed, f"res.partner.{method}")
    if not vals:
        raise WriteRefused(f"res.partner.{method}: nothing to write")
    if method == "create":
        _check_client_create(vals)
    if "parent_id" in vals and (type(vals["parent_id"]) is not int or vals["parent_id"] <= 0):
        raise WriteRefused(f"res.partner.{method}: parent_id must be a company id")
    if "category_id" in vals:
        cmds = vals["category_id"]
        if not isinstance(cmds, list) or not cmds or any(
                not (isinstance(c, (list, tuple)) and len(c) == 2 and c[0] == 4 and type(c[1]) is int) for c in cmds):
            raise WriteRefused(f"res.partner.{method}: category_id takes link commands (4, id) only")


def _check_client_create(vals: dict) -> None:
    name = vals.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or _CLIENT_NAME_BAD.search(name):
        raise WriteRefused("res.partner.create: a plain name of at most 80 characters is required")
    if name.startswith(TEST_CLIENT_PREFIX.strip()[:20]):
        raise WriteRefused("res.partner.create: a client contact may not carry the test-client name")
    phones = [vals[k] for k in ("phone", "mobile") if k in vals]
    if not phones or any(not isinstance(p, str) or not _CLIENT_PHONE.fullmatch(p) for p in phones):
        raise WriteRefused("res.partner.create: a client contact needs a phone or mobile (digits only)")
def _check_test_client_close(args: list, kwargs: dict, close_stage_id: int | None, team_stage_ids, read_fn: Any) -> None:
    """The narrowest allowance for closing a test client's ticket: ``helpdesk.ticket.write`` of ``{stage_id: <the
    configured close stage>}`` on ONE ticket, mail-off context only. The guard re-reads the LIVE ticket and partner
    itself: team 2, the partner named exactly ``Acme Test Client – <Name>`` (en dash) with no email, no user, no
    company; the ticket has no ``partner_email`` and no ``email_cc``; every follower is the test partner or an internal
    user's partner (``partner_share`` False).
    Never stage 9 or 17. The stage must equal the CONFIGURED close stage AND be a stage of team 2 (``team_stage_ids``,
    read from Odoo at preflight): a stage the caller merely names, or one outside the team, is refused."""
    if kwargs != {"context": TEST_CONTEXT}:
        raise WriteRefused("helpdesk.ticket.write: only the exact mail-off context is allowed")
    if len(args) != 2 or not isinstance(args[1], dict):
        raise WriteRefused("write: (ids, vals) expected")
    tid, vals = _one_id(args, "write"), args[1]
    if set(vals) != {"stage_id"}:
        raise WriteRefused("helpdesk.ticket.write: only stage_id may change when closing a test ticket")
    sid = vals["stage_id"]
    if type(sid) is not int or type(close_stage_id) is not int or close_stage_id <= 0 or sid != close_stage_id:
        raise WriteRefused("helpdesk.ticket.write: the stage must be the configured test-client close stage")
    if sid in NEVER_TEST_STAGES:
        raise WriteRefused(f"stage {sid} is never allowed for a test ticket")
    if sid not in {int(x) for x in (team_stage_ids or ())}:
        raise WriteRefused("helpdesk.ticket.write: the close stage is not a stage of team 2")
    if read_fn is None:
        raise WriteRefused("helpdesk.ticket.write: the live ticket could not be checked")
    rows = read_fn("helpdesk.ticket", [tid], ["partner_id", "team_id", "partner_email", "email_cc", "message_partner_ids"])
    row = rows[0] if rows else {}
    pid = _id_of(row.get("partner_id"))
    if pid <= 0 or _id_of(row.get("team_id")) != TEST_TEAM:
        raise WriteRefused("helpdesk.ticket.write: the live ticket is not a team 2 ticket with a client")
    if row.get("partner_email") or row.get("email_cc"):
        raise WriteRefused("helpdesk.ticket.write: the ticket has an email address on it")
    others = [int(f) for f in (row.get("message_partner_ids") or []) if int(f) != pid]
    if others:
        fol = read_fn("res.partner", others, ["partner_share"]) or []
        if len(fol) != len(others) or any(f.get("partner_share") is not False for f in fol):
            raise WriteRefused("helpdesk.ticket.write: the ticket has an external follower")
    prow = read_fn("res.partner", [pid], ["name", "email", "user_ids", "parent_id"])
    p = prow[0] if prow else {}
    name = p.get("name")
    if not isinstance(name, str) or not name.startswith(TEST_CLIENT_PREFIX) \
            or not _TEST_CLIENT_NAME.fullmatch(name[len(TEST_CLIENT_PREFIX):]):
        raise WriteRefused("helpdesk.ticket.write: the ticket's client is not a test client")
    if p.get("email") or p.get("user_ids") or _id_of(p.get("parent_id")):
        raise WriteRefused("helpdesk.ticket.write: the test client has an email, a user or a company")


def is_write(model: str, method: str) -> bool:
    return method not in READ_METHODS


def _vals_of(method: str, args: list) -> dict:
    if method == "create":
        v = args[0] if args else None
        if isinstance(v, list):                      # create([vals]) form: exactly one record
            if len(v) != 1:
                raise WriteRefused("create: exactly one record per call")
            v = v[0]
        if not isinstance(v, dict):
            raise WriteRefused("create: vals must be a dict")
        return v
    if method == "write":
        if len(args) != 2 or not isinstance(args[1], dict):
            raise WriteRefused("write: (ids, vals) expected")
        return args[1]
    return {}


def _one_id(args: list, what: str) -> int:
    ids = args[0] if args else None
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], int) or ids[0] <= 0:
        raise WriteRefused(f"{what}: exactly one record id")
    return ids[0]


def _check_x2many(name: str, cmds: Any, *, on_create: bool) -> None:
    """Link-only commands: (4, id) adds a link; (6, 0, ids) only on create. Never create/update/delete/unlink."""
    if not isinstance(cmds, list):
        raise WriteRefused(f"{name}: a command list is required")
    for c in cmds:
        ok = (isinstance(c, (list, tuple)) and len(c) >= 2 and c[0] == 4 and isinstance(c[1], int)) or \
             (on_create and isinstance(c, (list, tuple)) and len(c) == 3 and c[0] == 6 and isinstance(c[2], list))
        if not ok:
            raise WriteRefused(f"{name}: only link commands (4, id) are allowed")


def _check_fields(vals: dict, allowed: Iterable[str], what: str) -> None:
    bad = set(vals) & FORBIDDEN_FIELDS
    if bad:
        raise WriteRefused(f"{what}: forbidden field {sorted(bad)[0]} (it renames ticket types globally)")
    extra = set(vals) - set(allowed)
    if extra:
        raise WriteRefused(f"{what}: field not on the allowlist: {', '.join(sorted(extra))}")


def _check_stage(vals: dict, safe_stages: set[int], gated_stage: int | None, *, required: bool) -> None:
    if "stage_id" not in vals:
        if required:
            raise WriteRefused("create: stage_id is required (stage New emails the client)")
        return
    sid = vals["stage_id"]
    if not isinstance(sid, int):
        raise WriteRefused("stage_id must be an id")
    if sid in safe_stages or (gated_stage is not None and sid == gated_stage):
        return
    raise WriteRefused(f"stage {sid} emails the client or is not confirmed silent: needs Supervisor's tap")


def check(purpose: str, model: str, method: str, args: list | None = None, kwargs: dict | None = None, *,
          safe_stages: Iterable[int] = (), gated_stage: int | None = None, notify_partner: int | None = None,
          client_email: dict | None = None, self_uid: int | None = None, test_partner_id: int | None = None,
          team_stage_ids: Iterable[int] | None = None, read_fn: Any = None, close_stage_id: int | None = None) -> None:
    """Raise WriteRefused unless this exact call is allowed. Returns None when it is."""
    args, kwargs = list(args or []), dict(kwargs or {})
    model, method = str(model or ""), str(method or "")
    if method == "unlink" or method.startswith("_"):
        raise WriteRefused(f"{method or 'method'} is never allowed")
    if model.startswith("ir.") or model.startswith("base."):
        raise WriteRefused(f"model {model} is never allowed")
    if method in READ_METHODS:
        if model not in READ_MODELS:
            raise WriteRefused(f"read of {model} is not on the allowlist")
        if model == "mail.mail" and method not in MAIL_MAIL_METHODS:
            raise WriteRefused("mail.mail: only a count may be read")
        return
    if (model, method) not in WRITES.get(purpose, frozenset()):
        raise WriteRefused(f"{model}.{method} is not allowed for purpose {purpose!r}")
    if purpose == "test_client_contact":
        return _check_test_client_contact(args, kwargs)
    if purpose == "client_contact":
        return _check_client_contact(method, args, kwargs)
    if purpose == "test_client_close":
        return _check_test_client_close(args, kwargs, close_stage_id, team_stage_ids, read_fn)
    if purpose == "test_ticket":
        return _check_test_ticket(model, method, args, kwargs, {int(s) for s in safe_stages}, gated_stage, self_uid,
                                  test_partner_id, team_stage_ids, read_fn)
    if method in ("create", "write") and kwargs:
        raise WriteRefused(f"{model}.{method}: extra keyword arguments are not allowed")
    safe = {int(s) for s in safe_stages}
    if model == "helpdesk.ticket" and method in ("create", "write"):
        if method == "write":
            _one_id(args, "write")
        vals = _vals_of(method, args)
        _check_fields(vals, TICKET_FIELDS, f"helpdesk.ticket.{method}")
        if method == "write" and "partner_id" in vals:
            raise WriteRefused("helpdesk.ticket.write: the client (partner_id) is set at create only")
        if method == "write" and "team_id" in vals:
            raise WriteRefused("helpdesk.ticket.write: team_id is not writable (Odoo recomputes the stage to a "
                               "client-emailing 'New'); change the team at create only")
        _check_stage(vals, safe, gated_stage, required=method == "create")
        for x2m in ("tag_ids", "x_task_ids"):
            if x2m in vals:
                _check_x2many(x2m, vals[x2m], on_create=method == "create")
        conf = vals.get("x_ai_confidence")
        if conf is not None and not (isinstance(conf, (int, float)) and 0 <= conf <= 1):
            raise WriteRefused("x_ai_confidence must be between 0 and 1")
        return
    if model == "helpdesk.ticket" and method == "message_post":
        _one_id(args, "message_post")
        if kwargs.get("subtype_xmlid") == "mail.mt_comment":
            if client_email is not None:                 # the hub's approved client email (own, separate rule)
                _check_client_email(kwargs, client_email)
            else:
                _check_comment_to_principal(kwargs, notify_partner)
            return
        extra = set(kwargs) - NOTE_KWARGS
        if extra:
            raise WriteRefused(f"message_post: not allowed: {', '.join(sorted(extra))} (internal notes only)")
        if kwargs.get("subtype_xmlid") != "mail.mt_note" or kwargs.get("message_type", "comment") != "comment":
            raise WriteRefused("message_post: only an internal note (mail.mt_note, comment)")
        if not str(kwargs.get("body") or "").strip():
            raise WriteRefused("message_post: empty note")
        return
    if model == "account.analytic.line" and method == "create":
        vals = _vals_of("create", args)
        _check_fields(vals, TIMESHEET_FIELDS, "account.analytic.line.create")
        hours = vals.get("unit_amount")
        if not isinstance(hours, (int, float)) or not 0 < hours <= MAX_TIMESHEET_HOURS or round(hours * 4) != hours * 4:
            raise WriteRefused("unit_amount must be a positive multiple of 0.25 h, at most 12")
        emp = vals.get("employee_id")
        if not str(vals.get("name") or "").strip() or type(emp) is not int or emp <= 0:
            raise WriteRefused("a timesheet needs a description and Principal's employee id (a positive integer)")
        if not vals.get("date"):
            raise WriteRefused("a timesheet needs a date")
        return
    raise WriteRefused(f"{model}.{method} is not allowed")
