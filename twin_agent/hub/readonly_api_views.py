"""Read-only JSON view models for the hub API (tickets, approvals, timeline). Nothing here writes.

Built on the same data layer the admin pages use (``admin.data``): the hub's own store, no Odoo call. The rules:
* No phone numbers, emails or message ids anywhere; the customer's display name is included for the approver (it is
  internal and the same as on the admin pages). Names and draft text are scrubbed of phone-shaped numbers.
* The ticket summary is the SAME safe line Teamhub gets for a real ticket: structured fields only (type, track, module,
  a count). It never carries client text, not even a redacted or truncated bit.
* The approval nonce is never exposed (only the fingerprint, which is a hash of the draft).
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from . import events
from .model import TicketState
from ..redact import redact
from .admin import data
from .team_posts import SAFE_MODULES, SAFE_TRACKS, SAFE_TYPES, _allowed

CLOSED_STAGES = frozenset({"resolved"})
WORKING_STAGES = frozenset({"awaiting_owner_advance", "in_progress", "awaiting_owner_resolve"})
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
CLOSED_VISIBLE = timedelta(hours=24)        # a ticket closed in the last 24h stays in the list (final state on screen)
log = logging.getLogger("twin_agent.hub")
# A phone-shaped candidate: digits split only by single spaces or dashes, not touching . , / : (so amounts, versions and
# times are left alone). Scrubbed only with a leading + (7-15 digits) or 10-15 digits, and never a date.
_PHONEISH = re.compile(r"(?<![\w.,/:])\+?\d(?:[ -]?\d)+(?![\w,/:]|\.\d)")
_DATE = re.compile(r"\d{4}-\d\d-\d\d")
_WAMID = re.compile(r"wamid\.\S+", re.IGNORECASE)


def _when(value) -> datetime | None:
    """An ISO string (naive = UTC) to an aware datetime, or None."""
    if not value or not str(value)[:1].isdigit():
        return None
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(UTC).isoformat() if d else None


def scrub(text: str) -> str:
    """Remove emails, phone-like numbers and wamids from free text."""
    out = _WAMID.sub("[removed]", redact(str(text or "")))
    return _PHONEISH.sub(_phone_or_same, out)


def _phone_or_same(m: re.Match) -> str:
    cand = m.group(0)
    n = sum(c.isdigit() for c in cand)
    phone = (cand.startswith("+") and 7 <= n <= 15) or 10 <= n <= 15
    return "[number removed]" if phone and not _DATE.search(cand) else cand


def display_name(state) -> str:
    """The customer's short name: no phone, no email, no id. Falls back to 'Customer'."""
    name = scrub(state.inbound.name or "")
    name = re.sub(r"\[(?:REDACTED:\w+|removed|number removed)\]", "", name).replace("@", " ")
    name = " ".join(name.split())[:40].strip()
    return name or "Customer"


def safe_summary(state) -> str:
    a = state.analysis
    n = len(a.open_questions)
    return (f"Type {_allowed(a.ticket_type, SAFE_TYPES)}, track {_allowed(a.track, SAFE_TRACKS)}, "
            f"module {_allowed(a.module, SAFE_MODULES)}, {n} open question{'' if n == 1 else 's'}")


def approver_names(cfg, doc: dict) -> list[str]:
    allowed = {int(x) for x in doc.get("allowed") or [] if str(x).lstrip("-").isdigit()}
    return sorted(u.display_name for u in cfg.users.values() if u.approver_id in allowed)


def _waiting_on(state, draft) -> str | None:
    """"approval" only for a client-facing draft; an internal owner tap (advance, fix_ready, reopen) is "owner"."""
    if draft and draft["kind"] in data.CLIENT_KINDS:
        return "approval"
    if state.stage in CLOSED_STAGES:
        return None
    return "customer" if state.stage == "awaiting_customer" else "owner"


def _updated(state, docs: list[dict]) -> datetime | None:
    stamps = [state.inbound.ts, state.last_inbound_at, state.resolved_at, state.last_email_at]
    stamps += [r.get("ts") for r in state.replies]
    for d in docs:
        stamps += [d.get("issued_at"), d.get("used_at")]
    found = [x for x in (s if isinstance(s, datetime) else _when(s) for s in stamps if s) if x]
    return max(found) if found else None


def approvals_by_ticket(hub) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for d in hub.store.scan("approvals"):
        out.setdefault(int(d.get("ticket_id") or 0), []).append(d)
    return out


def all_states(hub) -> list:
    """Every readable ticket. One bad record is skipped and logged by its id; it never fails the whole list."""
    out = []
    for d in hub.store.scan("tickets"):
        try:
            out.append(TicketState.from_dict(d))
        except Exception:                                   # noqa: BLE001
            log.warning("readonly api: skipped an unreadable ticket record (ref %s)", _ref(d))
    return out


def _ref(d) -> str:
    try:
        return str(int(d.get("tid")))
    except Exception:                                       # noqa: BLE001
        return "unknown"


def _acked(state) -> bool:
    return bool(state.acked or any(h in ("7-ACK", "7-ACK(fallback)") for h in state.history))


def _approved_doc(docs: list[dict]) -> dict | None:
    done = [d for d in docs if d["kind"] in data.CLIENT_KINDS and d.get("approved") and _when(d.get("used_at"))]
    return max(done, key=lambda d: d["used_at"]) if done else None


def lifecycle(state, docs: list[dict], sends: list[dict], holds: list[dict], draft) -> tuple[str, str | None]:
    """The one word the desktop shows, and when the ticket got there. Most urgent first: closed, held (a person must
    act), needs_approval, then what happened to the last reply (sent / delivered / read), awaiting_customer, approved, new."""
    if state.stage in CLOSED_STAGES:
        return "closed", _iso(_when(state.resolved_at))
    if holds:
        return "held", _iso(_when(max(h.get("created_at", "") for h in holds)))
    if (draft is not None and draft["kind"] in data.CLIENT_KINDS) or state.stage == "awaiting_owner":
        # same rule as ``waiting_on == "approval"``: an internal owner tap (advance, fix_ready, reopen) is NOT an approval
        return "needs_approval", _iso(_when((draft or {}).get("issued_at")) or _updated(state, docs))
    replies = [r for r in sends if r["kind"] == "reply"]
    if state.stage == "awaiting_customer":
        if replies:
            r = replies[-1]
            word = {"sent": "reply_sent", "delivered": "reply_delivered", "read": "reply_read",
                    "failed": "held"}.get(r["status"], "reply_sent")      # a failed delivery needs a person: shown as held
            return word, r["at"].get(r["status"]) or r["sent_at"]
        return "awaiting_customer", _iso(_updated(state, docs))
    ok = _approved_doc(docs)
    if ok or state.stage in WORKING_STAGES:                  # the confirmation was approved; the team is working on it
        return "approved", _iso(_when(ok["used_at"]) if ok else _updated(state, docs))
    return "new", _iso(_when(state.inbound.ts))


def lifecycle_fields(hub, state, docs, sends, holds, cfg, audit) -> dict:
    word, at = lifecycle(state, docs, sends, holds, data.live_draft(hub, state))
    ok = _approved_doc(docs)
    return {"lifecycle": word, "lifecycle_at": at, "acked": _acked(state),
            "approved_by": data.approver_name(cfg, ok, audit) if ok else None,
            "last_delivery": events.last_delivery(sends)}


def ticket_item(hub, state, docs: list[dict], cfg=None, sends=(), holds=(), audit=()) -> dict:
    draft = data.live_draft(hub, state)
    created = state.inbound.ts if state.inbound.ts.tzinfo else state.inbound.ts.replace(tzinfo=UTC)
    return {**lifecycle_fields(hub, state, docs, list(sends), list(holds), cfg, audit), "id": state.tid, "odoo_ticket_id": state.tid, "created_at": _iso(created),
            "updated_at": _iso(_updated(state, docs)), "customer": display_name(state),
            "owner": state.owner.name if state.owner else None, "stage": state.stage,
            "status": data.ticket_status(hub, state), "waiting_on": _waiting_on(state, draft),
            "has_pending_approval": draft is not None, "channel": state.inbound.channel or "whatsapp",
            "summary": safe_summary(state)}


def _closed_recently(hub, state) -> bool:
    at = _when(state.resolved_at)
    return bool(at and hub.clock() - at <= CLOSED_VISIBLE)


def _holds_by_ticket(hub) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for h in hub.holds():
        if int(h.get("ticket_id") or 0):
            out.setdefault(int(h["ticket_id"]), []).append(h)
    return out


def ticket_list(hub, cfg, *, open_only: bool, limit: int) -> dict:
    by_t = approvals_by_ticket(hub)
    states = sorted(all_states(hub), key=lambda t: t.tid, reverse=True)
    if open_only:
        states = [s for s in states if s.stage not in CLOSED_STAGES or _closed_recently(hub, s)]
    sends, holds, audit = events.sends_for(hub.store), _holds_by_ticket(hub), data.approver_audit(hub)
    items = []
    for s in states:
        if len(items) >= limit:
            break
        try:
            items.append(ticket_item(hub, s, by_t.get(s.tid, []), cfg, sends.get(s.tid, []), holds.get(s.tid, []), audit))
        except Exception:                                   # noqa: BLE001 - skip this ticket, keep the list
            log.warning("readonly api: skipped ticket %s (could not be shown)", s.tid)
    return {"tickets": items, "count": len(items), "status": "open" if open_only else "all", "limit": limit}


def _pending(hub, state, cfg, admin_base: str, *, with_text: bool) -> dict | None:
    doc = data.live_draft(hub, state)
    if doc is None:
        return None
    out = {"ticket_id": state.tid, "kind": doc["kind"], "kind_label": data.KIND_LABEL.get(doc["kind"], doc["kind"]),
           "client_facing": doc["kind"] in data.CLIENT_KINDS, "fingerprint": doc["draft_hash"],
           "issued_at": _iso(_when(doc.get("issued_at"))), "expires_at": _iso(_when(doc.get("expires_at"))),
           "approvers": approver_names(cfg, doc),
           "approve_url": f"{admin_base}/tickets/{state.tid}" if admin_base else None}
    if with_text:
        out["draft_text"] = scrub(doc.get("draft") or "")
    return out


def approvals_pending(hub, cfg, admin_base: str) -> dict:
    rows = []
    for s in all_states(hub):
        try:
            p = _pending(hub, s, cfg, admin_base, with_text=False)
            if p:
                rows.append({**p, "customer": display_name(s), "owner": s.owner.name if s.owner else None,
                             "summary": safe_summary(s)})
        except Exception:                                   # noqa: BLE001 - skip this ticket, keep the list
            log.warning("readonly api: skipped ticket %s (could not be shown)", s.tid)
    rows.sort(key=lambda r: r["expires_at"] or "")
    return {"approvals": rows, "count": len(rows)}


def _ev(type_: str, actor: str, at, label: str) -> dict:
    return {"type": type_, "actor": actor, "at": _iso(_when(at) if not isinstance(at, datetime) else at), "label": label}


def _approval_events(state, docs: list[dict], cfg, audit=()) -> list[dict]:
    ev = []
    for d in sorted((d for d in docs if d["kind"] in data.CLIENT_KINDS), key=lambda d: d["issued_at"]):
        label = data.KIND_LABEL.get(d["kind"], d["kind"])
        ev.append(_ev("draft_created", "ai", d["issued_at"], f"AI draft created: {label}"))
        used = d.get("used_at")
        who = data.approver_name(cfg, d, audit)
        if used and _when(used) and d.get("approved"):
            ev.append(_ev("approved", who, used, data.approved_line(who)))
            if data.SENT_MARKER.get(d["kind"]) in state.history:
                ev.append(_ev("sent_to_customer", "hub", used, "Sent to the customer"))
        elif used and _when(used):
            ev.append(_ev("declined", who, used, f"Declined by {who}"))
        elif used:
            ev.append(_ev("draft_" + str(used), "hub", None, f"Draft {used}"))
    return ev


def timeline_events(state, docs: list[dict], cfg, audit=(), sends=()) -> list[dict]:
    ev = [_ev("customer_message_received", "customer", state.inbound.ts, "Customer message received")]
    acks = [s for s in sends if s["kind"] == "ack"]
    if any(h.startswith("7-ACK") for h in state.history):
        ev.append(_ev("ack_sent", "hub", acks[0]["sent_at"] if acks else None, "Automatic acknowledgement sent"))
    ev += [_ev("customer_reply_received", "customer", r.get("ts"), "Customer reply received") for r in state.replies]
    ev += _approval_events(state, docs, cfg, audit)
    if state.resolved_at:
        ev.append(_ev("resolved", "owner", state.resolved_at, "Ticket resolved"))
    return sorted(ev, key=lambda e: (e["at"] is None, e["at"] or ""))


def ticket_detail(hub, state, cfg, admin_base: str) -> dict:
    docs = approvals_by_ticket(hub).get(state.tid, [])
    sends = events.sends_for(hub.store).get(state.tid, [])
    audit = data.approver_audit(hub)
    item = ticket_item(hub, state, docs, cfg, sends, _holds_by_ticket(hub).get(state.tid, []), audit)
    item["pending_approval"] = _pending(hub, state, cfg, admin_base, with_text=True)
    item["timeline"] = timeline_events(state, docs, cfg, audit, sends)
    return {"ticket": item}
