"""Backup approval by TEXT REPLY in a Teamhub ticket thread. The Approve / Edit / Reject card stays the primary method.

Rules: ``docs/TEAMHUB-TICKET-THREAD-RULES.md`` section 5b. Accepted replies (whole text, trimmed, keyword case-insensitive):
``approve #<ticket>``, ``reject #<ticket>``, ``edit #<ticket>: <new text>``. Anything else is ignored.

WHO decided is NOT taken from the text and NOT from the poster-written submitter id. It is Teamhub's own message author:
``senderType == "human"`` and ``senderUserId`` of the reply, read with
``teamhub workspace thread get --root <root> --scope-room <room> --json`` (``details.replies[]``). That id must equal the
pending card's target. Agent or bot authors, and a reply that only CLAIMS a name, are refused. The decision is recorded
with identity ``teamhub_message_author``. Approving still sends NOTHING to a client in this version.

TRUST LIMIT: this identity is only as strong as the Teamhub logins on the hub PC. The Teamhub CLI there is logged in as a
human, so a local program could post as that human. What stops it is the PLATFORM ALLOWLIST: only a reply whose
``senderPlatform`` is in ``Settings.text_approval_platforms`` (default ``web``, the Teamhub web app) counts; ``api``, ``cli``,
empty and unknown values are refused with a visible message. NOTHING may act on this identity: approvals send nothing.

One decision per ticket: this goes through ``ApprovalBook.decide`` (same nonce, expiry, single use, one final decision per
ticket as a card press), so the first valid decision wins whether it came from a card or a text reply. A reply only
counts while the ticket has an unused approval, and only if it was posted AFTER that card (by ``threadSeq``, else
``createdAt``). Each reply id is processed once. Expiry is judged at scan time (fail closed).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

from . import teamhub_threads as kt

NS = "teamhub_text_replies"
IDENTITY = "teamhub_message_author"
_ID = re.compile(r"[A-Za-z0-9_]+")
_SIMPLE = re.compile(r"(approve|reject)\s+#(\d+)", re.I)
_EDIT = re.compile(r"edit\s+#(\d+)\s*:\s*(\S.*)", re.I | re.S)
WHY = {**kt.WHY, "unknown_action": "the card in force is an edit card: use its Approve edited text button",
       "wrong_ticket": "the ticket number in the reply does not match this thread",
       "platform_refused": "it was not posted from the Teamhub web app: use the card button or the Teamhub web app",
       "edit_unpostable": "the new text is too long or cannot be posted"}
_VERB = {"approve": "Approved", "reject": "Rejected", "edit": "Edit requested"}
_ACT = {"approved": "approve", "rejected": "reject", "edit_requested": "edit"}


def parse_text_decision(content: Any) -> tuple[str, int, str | None] | None:
    """(action, ticket, new_text) when the WHOLE trimmed text is exactly one accepted form, else None."""
    if not isinstance(content, str):
        return None
    s = content.strip()
    m = _SIMPLE.fullmatch(s)
    if m:
        return m.group(1).lower(), int(m.group(2)), None
    m = _EDIT.fullmatch(s)
    return ("edit", int(m.group(1)), m.group(2).strip()) if m else None


def fetch_replies(ctx: kt.Ctx, root: str, room: str) -> list[dict] | None:
    """Read-only Teamhub CLI call. None = could not read (nothing is decided on a guess)."""
    if not all(isinstance(x, str) and _ID.fullmatch(x) for x in (root, room)):
        return None                                       # an odd id is never passed to the CLI
    cmd = [ctx.st.teamhub_exe, "workspace", "thread", "get", "--root", root, "--scope-room", room, "--json"]
    try:
        r = ctx.cli_runner(cmd, cwd=os.path.expanduser("~"), capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=60)
        data = json.loads(getattr(r, "stdout", "") or "")
        replies = data["details"]["replies"] if getattr(r, "returncode", 1) == 0 else None
    except Exception:                                     # noqa: BLE001
        return None
    return [m for m in replies if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]] \
        if isinstance(replies, list) else None


def _ts(m: dict):
    """Timezone-aware createdAt, else None (naive or unparseable = unknown = refuse)."""
    try:
        t = datetime.fromisoformat(str(m.get("createdAt")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_card(m: dict) -> bool:
    """A real card: posted by an agent and carrying action buttons. Anything else is no anchor."""
    md = m.get("metadata")
    ui = md.get("ui") if isinstance(md, dict) else None
    return m.get("senderType") == "agent" and isinstance(ui, dict) and bool(ui.get("actions"))


def after_card(replies: list[dict], card_mid: str, msg: dict) -> bool:
    """True only when the reply is provably later than the card. Unknown = False (fail closed)."""
    card = next((m for m in replies if m.get("id") == card_mid), None)
    if card is None or not _is_card(card):
        return False
    a, b = card.get("threadSeq"), msg.get("threadSeq")
    if _is_int(a) and _is_int(b):
        return b > a
    ta, tb = _ts(card), _ts(msg)
    return bool(ta and tb and tb > ta)


def _seq(m: dict) -> int:
    x = m.get("threadSeq")
    return x if _is_int(x) else 0


def _open_doc(ctx: kt.Ctx, tid: int) -> dict | None:
    docs = [d for d in ctx.store.scan(ctx.approvals.NS) if d["ticket_id"] == int(tid) and not d.get("used_at")]
    return max(docs, key=lambda d: d["issued_at"]) if docs else None


def _edit_card(ctx: kt.Ctx, parent: dict, text: str) -> dict:
    """Edit by text: a NEW Approve / Edit / Reject card for the same approver ONLY, carrying the new text as its draft."""
    kids = [d for d in ctx.store.scan(ctx.approvals.NS) if d.get("parent") == parent["nonce"]]
    return kids[0] if kids else ctx.approvals.issue(parent["ticket_id"], text, parent["target"], parent=parent["nonce"],
                                                    only_target=True)


def _confirm_text(ctx: kt.Ctx, action: str, who: str, reason: str | None) -> str:
    if reason:
        return f"[Coordinator] Text reply refused: {WHY.get(reason, reason)}. Nothing was approved and nothing was sent."
    name = ctx.st.approvers.get(who, "an approver")
    tail = " A new card with the new text follows." if action == "edit" else ""
    return f"[Coordinator] {_VERB[action]} by {name} (text reply). Nothing was sent to the client.{tail}"


def _post(ctx: kt.Ctx, entry: dict, rid: str, rec: dict) -> bool:
    tid, root = rec["ticket_id"], entry["root_id"]
    ops = [kt.reply_op(tid, f"textdec-{kt.sha(rid)[:10]}", root, rec["reply"])]
    new = ctx.approvals.get(rec.get("new_nonce") or "")
    if new:
        k = new["nonce"][:8]
        who = ctx.st.approvers.get(new["target"], "the approver")
        ops += [kt.draft_op(tid, root, new["draft"], f"t{tid}-textedit-draft-{k}"),
                kt.card_op(tid, root, new, who, key=f"t{tid}-textedit-card-{k}")]
    res = kt.queue_for(ctx, entry.get("route")).run(ops)
    if new and res[2].get("ok") and res[2].get("message_id"):
        ctx.approvals.attach_card(new["nonce"], res[2]["message_id"])
        ctx.threads.patch(tid, card={"nonce": new["nonce"], "message_id": res[2]["message_id"]})
    return all(r.get("ok") or r.get("refused") or r.get("capped") for r in res)


def _save(ctx: kt.Ctx, rid: str, **kw) -> dict:
    return ctx.store.update(NS, rid, lambda d: {**d, **kw})


def _commit(ctx: kt.Ctx, rid: str) -> None:
    """Idempotent: decide (once), patch the thread map, issue the edit card. Safe to run again after a crash."""
    rec = ctx.store.get(NS, rid)
    doc = ctx.approvals.get(rec["nonce"])
    if not (doc.get("press_id") == rid and doc.get("decision")):      # not yet decided by THIS reply
        doc, reason = ctx.approvals.decide(rec["nonce"], rec["who"], rec["action"], None, doc.get("card_message_id") or "",
                                           IDENTITY, rid)
        if reason:
            _save(ctx, rid, reason=reason)
            return
    if rec["action"] == "edit":
        _save(ctx, rid, new_nonce=_edit_card(ctx, doc, rec.get("text") or "")["nonce"])
    else:
        ctx.threads.patch(rec["ticket_id"], decision={"by": rec["who"], "decision": doc["decision"], "nonce": doc["nonce"],
                                                      "identity": IDENTITY})


def _finish(ctx: kt.Ctx, entry: dict, rid: str) -> None:
    """Complete one stored reply record: commit, build the confirmation, post it. Retried by the next scan if it fails."""
    rec = ctx.store.get(NS, rid)
    if rec.get("posted"):
        return
    if rec["reason"] is None:
        _commit(ctx, rid)
        rec = ctx.store.get(NS, rid)
    rec = _save(ctx, rid, reply=_confirm_text(ctx, rec["action"], rec["who"], rec["reason"]))
    if _post(ctx, entry, rid, rec):
        _save(ctx, rid, posted=True)


def _refused(tid: int, reason: str) -> dict:
    return {"ticket_id": tid, "reason": reason, "reply": "", "posted": True}       # logged only, no answer


def _judge(ctx: kt.Ctx, entry: dict, replies: list[dict], m: dict, parsed: tuple) -> dict | None:
    """A PURE look at one decision-shaped reply: the record to store, or None (look again next scan). Decides nothing."""
    action, n, text = parsed
    tid, author = entry["ticket_id"], m.get("senderUserId")
    doc = _open_doc(ctx, tid) or ctx.approvals.for_ticket(tid)
    if doc is None or m.get("senderType") != "human" or not author or author != doc["target"]:
        return _refused(tid, "author_refused")            # agent/system/bot/other person
    if not doc.get("card_message_id"):
        return None                                       # the card's id is not recorded yet: look again next scan
    if not after_card(replies, doc["card_message_id"], m):
        return _refused(tid, "before_card")
    rec = {"reply_id": m["id"], "ticket_id": tid, "reason": None, "action": action, "who": author, "nonce": doc["nonce"],
           "text": text, "posted": False,
           "platform": m.get("senderPlatform")}
    edit_bad = kt._edit_problem(doc, text) if action == "edit" else None
    if m.get("senderPlatform") not in set(ctx.st.text_approval_platforms or ()):
        rec["reason"] = "platform_refused"                # empty, api, cli or unknown: not the Teamhub web app
    elif n != tid:
        rec["reason"] = "wrong_ticket"
    elif doc.get("used_at") or ctx.approvals.decided(tid):
        rec["reason"] = "revoked" if doc.get("used_at") == "revoked" else "ticket_decided"
    elif edit_bad:
        rec["reason"] = edit_bad
    elif action == "edit" and kt.refusal_reason(kt.draft_op(tid, entry["root_id"], text or "", "k")):
        rec["reason"] = "edit_unpostable"                 # the whole draft post (with its prefix) must pass BEFORE a card
    else:
        rec["reason"] = ctx.approvals.check(doc["nonce"], author, action, None, doc["card_message_id"])
    return rec


def _scan_thread(ctx: kt.Ctx, entry: dict) -> list[dict]:
    room = (entry.get("route") or {}).get("room_id") or ctx.st.room_id
    replies = fetch_replies(ctx, entry["root_id"], room)
    if replies is None:
        ctx.out(f"ticket {entry['ticket_id']}: could not read the thread; no text reply was acted on")
        return []
    out = []
    for m in sorted(replies, key=lambda x: (_seq(x), str(x.get("createdAt")))):
        parsed = None if m.get("deletedAt") else parse_text_decision(m.get("content"))
        if parsed is None or ctx.store.get(NS, m["id"]) is not None:
            continue
        rec = _judge(ctx, entry, replies, m, parsed)
        if rec is None:
            continue
        ctx.out(f"text reply {m['id']} (platform {m.get('senderPlatform')}): {rec['reason'] or 'accepted'}")
        out.append({"reply_id": m["id"], "ticket_id": rec["ticket_id"], "reason": rec["reason"]})
        if not ctx.dry_run:
            ctx.store.put_if_absent(NS, m["id"], rec)    # the record is written BEFORE anything is decided
            if not rec["posted"]:
                _finish(ctx, entry, m["id"])
                out[-1]["reason"] = ctx.store.get(NS, m["id"])["reason"]
    return out


def _recover(ctx: kt.Ctx) -> None:
    """Finish every stored reply that did not complete (a crash, or a post that failed). Idempotent."""
    for rec in [r for r in ctx.store.scan(NS) if not r.get("posted")]:
        entry = ctx.threads.get(rec["ticket_id"])
        if entry and entry.get("root_id") and rec.get("reply_id"):
            _finish(ctx, entry, rec["reply_id"])


def scan_replies(ctx: kt.Ctx) -> list[dict]:
    """First finish any unfinished reply, then read every thread with an unused approval and decide valid text replies."""
    if not ctx.dry_run:
        _recover(ctx)
    out: list[dict] = []
    for entry in ctx.threads.open_threads():
        if _open_doc(ctx, entry["ticket_id"]):
            out += _scan_thread(ctx, entry)
    return out
