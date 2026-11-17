"""Read-only view models over the hub's own store (tickets, approvals, delivery). Nothing here writes.

The page only reads what the hub already persists. Two things the hub does not persist are stated honestly in the UI
instead of invented: history entries carry no timestamps (the ack row has no time). Meta delivery-status callbacks are
stored per sent message by ``events.ingest_statuses`` (the ``sends`` namespace) and shown here; the old per-number
``delivery`` record is only a fallback for tickets whose sends were not recorded."""
from __future__ import annotations

from datetime import datetime

from .. import events
from ..owner_flow import CLIENT_KINDS
from .config import AdminConfig

SENT_MARKER = {"confirm": "8-CONFIRM", "reply": "OWNER-REPLY", "resolve": "10-RESOLVE", "status": "OWNER-STATUS"}
KIND_LABEL = {"confirm": "Confirmation", "reply": "Reply", "resolve": "Resolution", "status": "Status update",
              "advance": "Mark customer reply as confirmation", "fix_ready": "Fix ready (internal step)",
              "reopen": "Reopen (internal step)"}


def mask_number(wa_id: str) -> str:
    digits = "".join(c for c in str(wa_id) if c.isdigit())
    return ("•" * 6 + " " + digits[-4:]) if digits else "unknown"


def live_draft(hub, state) -> dict | None:
    """The ticket's one live approval doc (unused and unexpired), or None."""
    if not state.pending_nonce:
        return None
    doc = hub.store.get("approvals", state.pending_nonce)
    if not doc or doc.get("used_at"):
        return None
    if datetime.fromisoformat(doc["expires_at"]) <= hub.clock():
        return None
    return doc


def _row_text(r: dict) -> str:
    err = f" (error {r['error_code']})" if r["status"] == "failed" and r.get("error_code") else ""
    return f"{r['kind']} {r['status']}{err}"


def delivery_events(hub, state) -> list[tuple[str, str]]:
    """Timeline lines (time, text) for every status Meta reported on this ticket's messages."""
    out = []
    for r in events.sends_for(hub.store).get(state.tid, []):
        for status, at in r["at"].items():
            err = f" (error {r['error_code']})" if status == "failed" and r.get("error_code") else ""
            out.append((at, f"Delivery status: {status} ({r['kind']}){err}"))
    return out


def delivery_for(hub, state) -> str:
    """Per message the hub sent ("ack delivered; reply read"), from Meta's status callbacks. Falls back to the old
    per-number ``delivery`` record, or '' when no client message went out."""
    rows = events.sends_for(hub.store).get(state.tid, [])
    if rows:
        return "; ".join(_row_text(r) for r in rows)
    if not any(m in state.history for m in SENT_MARKER.values()):
        return ""
    doc = hub.store.get("delivery", state.wa_id)
    return str((doc or {}).get("status") or "sent (no delivery report yet)")


def ticket_status(hub, state) -> str:
    draft = live_draft(hub, state)
    if draft and draft["kind"] in CLIENT_KINDS:
        return "Awaiting approval"
    if draft:
        return "Awaiting internal step"
    return state.stage.replace("_", " ").capitalize()


HOLD_LABELS = {"staff_number": "Message from a team member's own phone (no ticket, no ack)", "unknown_sender": "Message from a number that is not a known client",
               "stt_unavailable": "Voice note (transcription pending)", "voice_note_pending": "Voice note (transcription pending)",
               "empty_message": "Message with no text", "processing_failed": "Processing failed (a retry is scheduled)",
               "processing_dead": "Processing failed for good: needs a person"}


def hold_rows(hub) -> list[dict]:
    """Open holds, newest first. Fixed labels, the last 4 digits and the ticket id only: never client text."""
    rows = []
    for h in sorted(hub.holds(), key=lambda x: x.get("created_at", ""), reverse=True):
        wa = str(((h.get("inbound") or {}).get("wa_id")) or "")
        rows.append({"id": str(h.get("id", "")), "label": HOLD_LABELS.get(str(h.get("reason")), "Held message"),
                     "sender": mask_number(wa) if wa and h.get("reason") != "staff_number" else "-", "ticket_id": int(h.get("ticket_id") or 0),
                     "created": str(h.get("created_at", ""))[:19].replace("T", " ")})
    return rows


def ticket_rows(hub) -> list[dict]:
    rows = []
    for s in sorted(hub.all_tickets(), key=lambda t: t.tid, reverse=True):
        rows.append({"tid": s.tid, "customer": s.inbound.name or "Customer", "number": mask_number(s.wa_id),
                     "kind": s.inbound.kind, "status": ticket_status(hub, s), "delivery": delivery_for(hub, s)})
    return rows


def approver_audit(hub) -> list[dict]:
    """The hub's own audit rows for approvals (who clicked Approve), oldest first. Ids and hub user names only."""
    from ..store import audit_rows
    return [r for r in audit_rows(hub.store) if r.get("role") == "approver" and r.get("method") == "approve"
            and r.get("outcome") == "ok"]


from ..autopilot import AUTOPILOT_LABEL  # noqa: E402


def approved_line(who: str) -> str:
    """"Approved by Riley", or the autopilot's own wording as it is ("Approved automatically by One (test client)")."""
    return who if who == AUTOPILOT_LABEL else f"Approved by {who}"


def approver_name(cfg: AdminConfig, doc: dict, audit=()) -> str:
    """WHO decided this approval, by the logged-in hub user, never by ``approver_id`` (every hub user shares one).
    1. the name the admin page stamped on the approval; 2. (approvals made before that) the audit row of the same
    ticket written right after the decision; 3. only then the approver id, and only when it names exactly one person."""
    if doc.get("used_by_name"):
        return str(doc["used_by_name"])
    used = str(doc.get("used_at") or "")
    rows = [r for r in audit if int(r.get("ticket_id") or 0) == int(doc.get("ticket_id") or 0)
            and used[:1].isdigit() and r.get("ts", "") >= used]
    if rows and _within(used, rows[0]["ts"], 300):
        user = cfg.users.get(str(rows[0].get("hub_user") or ""))
        return user.display_name if user else str(rows[0].get("hub_user") or "Staff approver")
    return cfg.name_for_approver(doc.get("used_by"))


def _within(a: str, b: str, seconds: int) -> bool:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() <= seconds
    except ValueError:
        return False


def _approval_events(hub, state, cfg: AdminConfig) -> list[tuple[str, str]]:
    docs = [d for d in hub.store.scan("approvals") if d.get("ticket_id") == state.tid and d["kind"] in CLIENT_KINDS]
    ev: list[tuple[str, str]] = []
    audit = approver_audit(hub)
    for d in sorted(docs, key=lambda d: d["issued_at"]):
        label = KIND_LABEL.get(d["kind"], d["kind"])
        ev.append((d["issued_at"], f"AI draft created: {label}"))
        used = d.get("used_at")
        if used and used[:1].isdigit():                       # a real timestamp (not 'superseded' / 'expired')
            who = approver_name(cfg, d, audit)
            if d.get("approved"):
                ev.append((used, approved_line(who)))
                sent = SENT_MARKER.get(d["kind"]) in state.history
                ev.append((used, "Sent to the customer" if sent else "Approved but NOT sent (see ticket holds)"))
            else:
                ev.append((used, f"Declined by {who}"))
        elif used:
            ev.append(("", f"Draft {used}"))
    return ev


def timeline(hub, state, cfg: AdminConfig) -> list[tuple[str, str]]:
    kind = "voice note" if state.inbound.kind == "voice" else "message"
    ev = [(state.inbound.ts.isoformat(), f"Customer {kind} received")]
    if any(h.startswith("7-ACK") for h in state.history):
        ev.append(("", "Automatic acknowledgement sent (the only automatic message)"))
    later = [(r["ts"], f"Customer {r['kind']} reply received") for r in state.replies]
    ev += sorted(later + _approval_events(hub, state, cfg), key=lambda x: x[0] or "~")
    d = delivery_for(hub, state)
    if events.sends_for(hub.store).get(state.tid):
        ev += delivery_events(hub, state)
        ev.sort(key=lambda x: x[0] or "~")
    elif d.startswith(("delivered", "read")):
        ev.append((str((hub.store.get("delivery", state.wa_id) or {}).get("ts") or ""), f"Delivery status: {d}"))
    return ev


def ticket_detail(hub, state, cfg: AdminConfig) -> dict:
    draft = live_draft(hub, state)
    return {"tid": state.tid, "customer": state.inbound.name or "Customer", "number": mask_number(state.wa_id),
            "client": state.client, "kind": state.inbound.kind, "message": state.inbound.text or "",
            "transcript": state.inbound.text if state.inbound.kind == "voice" else "",
            "transcript_placeholder": bool(state.inbound.transcript_placeholder),
            "replies": state.replies, "status": ticket_status(hub, state), "draft": draft,
            "editable": bool(draft and draft["kind"] in CLIENT_KINDS), "delivery": delivery_for(hub, state),
            "timeline": timeline(hub, state, cfg), "owner": state.owner.name if state.owner else ""}
