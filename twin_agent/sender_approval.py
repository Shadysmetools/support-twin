"""Approval binding for sends as Principal (05 3.4 "Approval binding", red-team #3).

A send triggered by a tap needs ONE approvals row that:
- belongs to this request, is a send-type action for this trigger, and is not expired;
- was issued to, and used by, Principal's own user id (the backup can never approve a send as Principal);
- carries the same draft hash as the text about to go out (sha256 of the exact text);
- has not already been spent on another send.

Two callers may consume the nonce: the Agent bot consumes it when the button is pressed
(bot_controller.on_callback -> db.use_approval), or the sender consumes it itself if nobody did yet.
Either way the sender then marks it spent (cursor "approval_spent:<nonce>") so one tap = one send.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from .clock import elapsed, utc
from .models import SendTrigger, parse_ts

# nv = "Send without voice" on a voice card: the same text, no audio.
APPROVAL_ACTIONS = {SendTrigger.approval: ("ap", "as", "ct", "nv"), SendTrigger.principal_edit: ("sd",)}
SPENT_PREFIX = "approval_spent:"
USED_MAX_AGE = timedelta(minutes=30)     # a tap consumed by the bot must reach the sender soon after


def text_hash(text: str) -> str:
    """sha256 hex of the exact text (same as bot_io.text_hash / guards.draft_hash)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# A direct owner command that means "send this now" (HANDOFF correction #11). A bare command only - "send the PO to
# the client" is a task, not an approval, and "send it?" is a question, so neither counts.
OWNER_SEND_COMMANDS = {"send", "send it", "send now", "send it now"}


def is_owner_send_command(text, user_id, cfg: dict) -> bool:
    """True only when PRINCIPAL HIMSELF, on his own account id, types a bare send command. A backup or an unknown id
    can never approve a send as Principal (same identity rule as a tap); the text must match a command exactly
    (trimmed, case-insensitive) - not merely contain the word 'send'."""
    principal = int((cfg.get("people") or {}).get("principal_tg_user_id") or 0)
    if not principal or int(user_id or 0) != principal:
        return False
    return " ".join(str(text or "").split()).lower() in OWNER_SEND_COMMANDS


def approval_satisfied(db, cfg: dict, msg, trigger: SendTrigger, *, nonce: str | None = None,
                       user_id: int | None = None, now=None, owner_command: str | None = None,
                       owner_user_id: int | None = None) -> tuple[bool, str]:
    """Is this exact send authorised? Returns (ok, why). Correction #11: a direct owner "send it" command is an
    approval, the SAME as a board Approve tap - EITHER path satisfies the gate:
      - owner path: Principal, on his own id, typed a bare send command (is_owner_send_command);
      - board path: a valid, hash-bound, single-use approvals row (check_approval, unchanged).
    The owner path short-circuits before touching the db. This is approval ONLY; the dial, guards and kill switch
    in sender.py still apply on top of it.

    WIRING NOTE (real inter-Telegram path is a later step): when Principal types "send it" in reply to a draft card, the
    bot/sender should either call this with owner_command set, OR (preferred, so the board path's hash-binding and
    one-tap-one-send rules apply identically) mint+consume an approvals row via db.issue_approval(draft_hash=...)
    and call the Sender with SendTrigger.approval and that nonce. This module keeps only the pure decision."""
    cmd_user = owner_user_id if owner_user_id is not None else user_id
    if owner_command is not None and is_owner_send_command(owner_command, cmd_user, cfg):
        return True, "owner send command = approval (correction #11)"
    row, why = check_approval(db, cfg, msg, trigger, nonce, user_id, now)
    return (row is not None), why


def _row(db, nonce: str) -> dict | None:
    """Read an approvals row. Uses db.get_approval if CORE adds it, else a read-only SELECT."""
    getter = getattr(db, "get_approval", None)
    if callable(getter):
        return getter(nonce)
    one = getattr(db, "_one", None)
    return one("SELECT * FROM approvals WHERE nonce = ?", (nonce,)) if callable(one) else None


def check_approval(db, cfg: dict, msg, trigger: SendTrigger, nonce: str | None, user_id: int | None,
                   now) -> tuple[dict | None, str]:
    """Returns (approvals row, "") if the tap really authorises this exact send, else (None, why)."""
    principal = int((cfg.get("people") or {}).get("principal_tg_user_id") or 0)
    if not nonce or not user_id:
        return None, "no approval nonce"
    if not principal or int(user_id) != principal:
        return None, "only Principal's own tap can approve a send as Principal"
    if not msg.draft_hash or msg.draft_hash != text_hash(msg.text):
        return None, "text does not match the approved draft hash"
    if db.get_cursor(SPENT_PREFIX + nonce):
        return None, "this approval was already used for a send"
    row = None
    for action in APPROVAL_ACTIONS[SendTrigger(trigger)]:
        row = db.use_approval(nonce, principal, action, msg.request_id)
        if row:
            break
    if not row:                                   # maybe the bot consumed it at the button press
        row = _row(db, nonce)
        if not row or not row.get("used_at"):
            return None, "approval nonce unknown or expired"
        used_at = parse_ts(row.get("used_at"))
        if (row.get("request_id") != msg.request_id or row.get("action") not in APPROVAL_ACTIONS[SendTrigger(trigger)]
                or int(row.get("used_by") or 0) != principal or int(row.get("allowed_user_id") or 0) != principal
                or used_at is None or elapsed(used_at, now) > USED_MAX_AGE):
            return None, "approval does not match this request, action or user, or is too old"
    expires = parse_ts(row.get("expires_at"))
    if expires is not None and utc(expires) < utc(now) - USED_MAX_AGE:
        return None, "approval expired"
    if (row.get("draft_hash") or "") != msg.draft_hash:
        return None, "approval was for a different draft"
    db.set_cursor(SPENT_PREFIX + nonce, msg.send_key)
    return dict(row), ""
