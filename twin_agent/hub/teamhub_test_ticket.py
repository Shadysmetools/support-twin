"""Make and close ONE test helpdesk ticket for the Teamhub thread demo. Safe by construction:

  - DEFAULT IS DRY RUN: it only reads and prints the plan. Real writes need ``--live``.
  - The ticket is named ``TEST <middle dot> Sysadmin's Tickets flow`` in team 2 (Helpdesk Support), for the TEST contact
    (no email, no phone, no mobile; re-read live before every write), with mail switched off in the context. If team 2
    auto-assigns, ``user_id`` is our OWN uid (which skips auto-assignment); otherwise there is no owner.
  - A stage that carries an email template (e.g. stage 1 "New") is allowed ONLY for that TEST contact, with tracking
    off, and never with ``--skip-mail-table`` (then the mail safety net would be blind).
  - Before and after every write it snapshots ``mail.mail`` (max id and count) and the ticket's email/comment
    ``mail.message`` rows. Any NEW mail.mail id, or a new message, prints a loud warning naming the ids and stops.
  - Closing uses config ``close_stage_id``: a folded stage of team 2, never 9 or 17 (the guard enforces both).
  - There is no delete path at all. Every write goes through ``helpdesk/guard.py`` (purpose ``test_ticket``), and the
    guard itself re-reads the live contact (create) and the live ticket and contact (stage write).
    Writes use the raw transport of a read-only adapter, so the guard is the ONLY layer: that is deliberate and tested.

Run:  python -m twin_agent.hub.teamhub_test_ticket create|close [--ticket-id N] [--live] [--config path]
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from ..helpdesk import guard
from ..helpdesk.conventions import m2o
from .teamhub_threads import ConfigError, load_settings, stage_flag

TEAM_ID = guard.TEST_TEAM
TICKET_NAME = guard.TEST_PREFIX + "Sysadmin's Tickets flow"
TICKET_TEXT = ("<p>Test ticket for the Teamhub ticket thread demo. The Sales quotation report is not working: it shows "
               "an error when we print it. Please check asap.</p>")
SUPPRESS = guard.TEST_CONTEXT
NEVER_STAGES = guard.NEVER_TEST_STAGES
CONTACT_FIELDS = ["name", "email", "phone", "mobile"]


class Refuse(Exception):
    """A safety check failed: nothing (more) was written. The message says why, never a secret."""


def read(t, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
    guard.check("read", model, method, args, kwargs)
    return t.execute_kw(model, method, args, dict(kwargs or {}))


def _read_ids(t, model: str, ids: list, fields: list) -> list:
    return read(t, model, "read", [ids], {"fields": fields})


def write(t, model: str, method: str, args: list, *, safe: tuple = (), gated: int | None = None,
          self_uid: int | None = None, partner_id: int | None = None, team_stage_ids=()) -> Any:
    """The ONLY write path: guard first (with the exact call that is sent), then the transport."""
    kwargs = {"context": dict(SUPPRESS)}
    guard.check("test_ticket", model, method, args, kwargs, safe_stages=safe, gated_stage=gated, self_uid=self_uid,
                test_partner_id=partner_id, team_stage_ids=team_stage_ids, read_fn=lambda m, i, f: _read_ids(t, m, i, f))
    return t.execute_kw(model, method, args, kwargs)


def counts(t, tid: int = 0, *, skip_mail_table: bool = False) -> dict:
    out: dict = {"mail_mail": None, "messages": None, "max_id": None}
    if not skip_mail_table:
        try:
            out["mail_mail"] = int(read(t, "mail.mail", "search_count", [[]]))
            out["max_id"] = mail_max_id(t)
        except Exception as e:                           # noqa: BLE001
            raise Refuse(f"cannot read mail.mail ({type(e).__name__}); use --skip-mail-table to rely on mail.message "
                         "only (then template stages are refused)") from None
    if tid:
        out["messages"] = int(read(t, "mail.message", "search_count",
                                   [[["model", "=", "helpdesk.ticket"], ["res_id", "=", int(tid)],
                                     ["message_type", "in", ["email", "comment"]]]]))
    return out


def _delta(before: dict, after: dict) -> dict:
    return {k: (None if before[k] is None or after[k] is None else after[k] - before[k])
            for k in ("mail_mail", "messages")}


def mail_ids_after(t, max_id: int) -> list:
    return list(read(t, "mail.mail", "search", [[["id", ">", int(max_id)]]]))


def mail_max_id(t) -> int:
    ids = read(t, "mail.mail", "search", [[]], {"limit": 1, "order": "id desc"})
    return int(ids[0]) if ids else 0


def _alarm(t, before: dict, after: dict, what: str, out: Callable) -> None:
    """A NEW mail.mail id (not just a count change) or a new email/comment message = LOUD warning, then stop."""
    ids = mail_ids_after(t, before["max_id"]) if before["max_id"] is not None else []
    msgs = _delta(before, after)["messages"] or 0
    if ids or msgs > 0:
        out(f"!!!! WARNING: after {what}: new mail.mail ids {ids}, new messages {msgs} !!!! Check Odoo now.")
        raise Refuse(f"mail appeared after {what} (mail.mail ids {ids}, messages {msgs}): STOP and check Odoo")


def pick_stages(t, close_stage_id: int = 0, need_close: bool = True, skip_mail_table: bool = False) -> dict:
    """The open stage and the closing stage of team 2 (never 9 or 17). A stage with an email template is allowed only
    for the live-checked TEST contact (``needs_contact_check``) and never with ``skip_mail_table``."""
    flag = stage_flag(read(t, "helpdesk.stage", "fields_get", [], {"attributes": ["type"]}))
    all_rows = read(t, "helpdesk.stage", "search_read", [[["team_ids", "in", [TEAM_ID]]]],
                    {"fields": ["name", "template_id", "sequence", flag], "order": "sequence"})
    rows = [r for r in all_rows if int(r["id"]) not in NEVER_STAGES]
    silent = lambda r: not m2o(r.get("template_id"))                        # noqa: E731
    open_rows = [r for r in rows if not r.get(flag)]
    open_ = next((r for r in open_rows if silent(r)), open_rows[0] if open_rows else None)
    if not need_close:
        close = {"id": 0, "template_id": False}
    elif close_stage_id:
        close = next((r for r in rows if int(r["id"]) == int(close_stage_id) and r.get(flag)), None)
        if close is None:
            raise Refuse(f"close_stage_id {close_stage_id} is not a folded stage of team {TEAM_ID} (or is 9 / 17)")
    else:
        close = next((r for r in rows if r.get(flag) and silent(r)), None)
        if close is None:
            raise Refuse("no closing stage with an empty email template: set teamhub_tickets.close_stage_id")
    need = {"open": bool(open_ and not silent(open_)), "close": not silent(close)}
    if skip_mail_table and (need["close"] if need_close else need["open"]):
        raise Refuse("--skip-mail-table: a stage with an email template is refused (the mail check would be blind)")
    return {"open": int(open_["id"]) if open_ else 0, "close": int(close["id"]), "close_name": close.get("name"),
            "needs_contact_check": need, "team_stage_ids": [int(r["id"]) for r in all_rows]}


def _contact_ok(p: dict | None) -> bool:
    return bool(p) and p.get("name") == guard.TEST_PARTNER and not (p.get("email") or p.get("phone") or p.get("mobile"))


def verify_test_contact(t, tid: int = 0, pid: int = 0) -> int:
    """Re-read LIVE: the partner (of ticket ``tid``, or ``pid``) is the TEST contact with no email, phone or mobile."""
    if tid:
        rows = read(t, "helpdesk.ticket", "search_read", [[["id", "=", int(tid)]]], {"fields": ["partner_id"]})
        pid = (m2o(rows[0].get("partner_id")) or 0) if rows else 0
    p = _read_ids(t, "res.partner", [int(pid)], CONTACT_FIELDS) if pid else []
    if not _contact_ok(p[0] if p else None):
        raise Refuse("the ticket's contact is not the TEST contact with no email, phone or mobile: refusing")
    return int(pid)


def assignment_is_manual(t) -> bool:
    """Odoo may auto-assign an owner (and email them) even with no user_id. Only a manual team skips that."""
    flds = read(t, "helpdesk.team", "fields_get", [], {"attributes": ["type"]})
    if "assign_method" not in flds:
        return False                                      # cannot tell: treat as auto-assigning
    row = read(t, "helpdesk.team", "search_read", [[["id", "=", TEAM_ID]]], {"fields": ["assign_method"]})
    return bool(row) and row[0].get("assign_method") == "manual"


def _partner(t, *, live: bool, out: Callable) -> int:
    rows = read(t, "res.partner", "search_read", [[["name", "=", guard.TEST_PARTNER]]],
                {"fields": CONTACT_FIELDS, "limit": 1})
    if rows:
        if not _contact_ok(rows[0]):
            raise Refuse("the TEST client has an email, phone or mobile: refusing (it could be emailed)")
        return int(rows[0]["id"])
    out(f"{'creating' if live else 'WOULD CREATE'} contact {guard.TEST_PARTNER!r} (no email, no phone)")
    return int(write(t, "res.partner", "create", [{"name": guard.TEST_PARTNER}])) if live else 0


def create(t, *, live: bool, out: Callable = print, skip_mail_table: bool = False) -> dict:
    stages = pick_stages(t, need_close=False, skip_mail_table=skip_mail_table)
    if not stages["open"]:
        raise Refuse("team 2 has no open stage: refusing")
    manual = assignment_is_manual(t)
    before = counts(t, skip_mail_table=skip_mail_table)
    out(f"plan: create {TICKET_NAME!r} in team {TEAM_ID}, stage {stages['open']}. before={before}")
    if not manual:
        out(f"team 2 auto-assigns (or cannot be checked): --live creates with user_id = our own uid ({getattr(t, 'uid', 0)})")
    pid = _partner(t, live=live, out=out)
    if not live:
        return {"dry_run": True, "stages": stages, "manual_assignment": manual, "before": before}
    login = getattr(t, "authenticate", None)
    if login:
        login()                                           # the real transport logs in lazily: uid is 0 until it does
    me = int(getattr(t, "uid", 0) or 0)
    if not manual and me <= 0:
        raise Refuse("team 2 auto-assigns and our own uid is unknown: refusing")
    verify_test_contact(t, pid=pid)                       # always, live, right before the create
    if stages["needs_contact_check"]["open"]:
        out(f"stage {stages['open']} has an email template: contact verified, tracking is off for the write")
    vals = {"name": TICKET_NAME, "description": TICKET_TEXT, "team_id": TEAM_ID, "stage_id": stages["open"],
            "partner_id": pid}
    if not manual:
        vals["user_id"] = me            # explicit own uid skips auto-assignment; nobody else is ever used
    tid = int(write(t, "helpdesk.ticket", "create", [vals], safe=(stages["open"],), self_uid=me, partner_id=pid,
                    team_stage_ids=stages["team_stage_ids"]))
    after = counts(t, tid, skip_mail_table=skip_mail_table)
    out(f"created ticket {tid}. after={after} delta={_delta(before, after)}")
    _alarm(t, before, after, f"creating ticket {tid}", out)
    owner = read(t, "helpdesk.ticket", "search_read", [[["id", "=", tid]]], {"fields": ["user_id"]})
    got = m2o(owner[0].get("user_id")) if owner else None
    if got and got != vals.get("user_id"):
        out(f"WARNING: ticket {tid} got an unexpected owner {got}")
    return {"ticket_id": tid, "delta": _delta(before, after), "stages": stages}


def close(t, tid: int, *, live: bool, out: Callable = print, skip_mail_table: bool = False,
          close_stage_id: int = 0) -> dict:
    rows = read(t, "helpdesk.ticket", "search_read", [[["id", "=", int(tid)]]],
                {"fields": ["name", "team_id", "stage_id"]})
    if not rows or not str(rows[0].get("name") or "").startswith(guard.TEST_PREFIX) or m2o(rows[0].get("team_id")) != TEAM_ID:
        raise Refuse("not a TEST ticket of team 2: refusing")
    stages = pick_stages(t, close_stage_id, skip_mail_table=skip_mail_table)
    before = counts(t, tid, skip_mail_table=skip_mail_table)
    out(f"plan: move ticket {tid} to closing stage {stages['close']} ({stages['close_name']}). before={before}")
    if m2o(rows[0].get("stage_id")) == stages["close"]:
        return {"ticket_id": tid, "already_closed": True}
    if not live:
        return {"dry_run": True, "stages": stages, "before": before}
    pid = verify_test_contact(t, tid=tid)                 # always: even a silent stage is only for the TEST contact
    if stages["needs_contact_check"]["close"]:
        out(f"stage {stages['close']} has an email template: contact verified, tracking is off for the write")
    write(t, "helpdesk.ticket", "write", [[int(tid)], {"stage_id": stages["close"]}], gated=stages["close"],
          partner_id=pid, team_stage_ids=stages["team_stage_ids"])
    after = counts(t, tid, skip_mail_table=skip_mail_table)
    out(f"closed ticket {tid}. after={after} delta={_delta(before, after)}")
    _alarm(t, before, after, f"closing ticket {tid}", out)
    return {"ticket_id": tid, "delta": _delta(before, after)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="teamhub_test_ticket")
    ap.add_argument("command", choices=("create", "close"))
    ap.add_argument("--ticket-id", type=int, default=0)
    ap.add_argument("--live", action="store_true", help="really write (default is a dry run)")
    ap.add_argument("--skip-mail-table", action="store_true", help="the Odoo user cannot read mail.mail")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args(argv)
    try:
        from .odoo_ticket_watch import open_reader
        cfg = load_settings(a.config)
        t = open_reader(cfg).t
        if a.command == "create":
            create(t, live=a.live, skip_mail_table=a.skip_mail_table)
        elif not a.ticket_id:
            raise Refuse("close needs --ticket-id")
        else:
            close(t, a.ticket_id, live=a.live, skip_mail_table=a.skip_mail_table, close_stage_id=cfg.close_stage_id)
    except (Refuse, ConfigError, guard.WriteRefused, OSError, KeyError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
