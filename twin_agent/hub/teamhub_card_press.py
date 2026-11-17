"""Approve / Edit / Reject CARD PRESSES, read back from the card message itself. Rules: ``docs/TEAMHUB-TICKET-THREAD-RULES.md`` 5b.

Why: when a person presses a button, Teamhub records it ON THE CARD MESSAGE. Nothing else writes a ``submission-*.json`` file
for it, so presses were never processed. For every tracked ticket with an unused approval card this module reads the card
(read-only: ``teamhub workspace message get --message <card> --scope-room <room> --json``) and takes the interactions from
``details.message.metadata.ui.interactionsByActor`` (each: ``actorId``, ``actionId``, ``value`` = the card nonce, ``actedAt``,
and for the edit card the submitted text under ``inputValues``/``submittedValues``).

Identity is ``teamhub_card_actor``: the ``actorId`` is recorded by the PLATFORM, so it is stronger than the poster-written
submitter id. The actor must equal the card's target approver and ``value`` must equal the card's nonce. The decision goes
through ``ApprovalBook.decide`` (single use, expiry, one final decision per ticket), so a card press, a text reply and a
submission file share "first wins". Each (card, actor, action, actedAt) is processed once. A press is stored BEFORE anything is
decided (crash safe, same pattern as the text replies). Approving still sends NOTHING to a client in this version.

One press per person per card: the cards are ``scoped_per_user`` and Teamhub locks the card for that person after one click
(``per_user`` locks the same way and cannot target one approver). So a next step always needs a NEW card: Edit issues one,
and that new card takes its own press.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from . import teamhub_threads as kt
from .teamhub_text_approval import _ID, _is_card, _open_doc

NS = "teamhub_card_presses"
IDENTITY = "teamhub_card_actor"
_SILENT = ("not_authorised", "wrong_card", "unknown_action", "unknown_approval")   # logged only: nothing is said in the thread
_TEXT_KEYS = ("inputValues", "submittedValues", "values")


def fetch_card(ctx: kt.Ctx, mid: str, room: str) -> dict | None:
    """Read-only Teamhub CLI call. The card message, or None (could not read / not a card: nothing is decided on a guess)."""
    if not all(isinstance(x, str) and _ID.fullmatch(x) for x in (mid, room)):
        return None
    cmd = [ctx.st.teamhub_exe, "workspace", "message", "get", "--message", mid, "--scope-room", room, "--json"]
    try:
        r = ctx.cli_runner(cmd, cwd=os.path.expanduser("~"), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60)
        msg = json.loads(getattr(r, "stdout", "") or "")["details"]["message"] if getattr(r, "returncode", 1) == 0 else None
    except Exception:                                     # noqa: BLE001
        return None
    return msg if isinstance(msg, dict) and msg.get("id") == mid and _is_card(msg) else None


def interactions(card: dict) -> list[dict]:
    """The well-formed interactions on the card, oldest first. A malformed one is dropped (fail closed)."""
    ui = (card.get("metadata") or {}).get("ui")
    by_actor = ui.get("interactionsByActor") if isinstance(ui, dict) else None
    out = []
    for key, i in (by_actor.items() if isinstance(by_actor, dict) else ()):
        if not isinstance(i, dict) or i.get("actorId") != key or not all(
                isinstance(i.get(f), str) and i[f] for f in ("actorId", "actionId", "value", "actedAt")):
            continue
        try:
            datetime.fromisoformat(i["actedAt"].replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append(i)
    return sorted(out, key=lambda i: i["actedAt"])


def _text(i: dict) -> str | None:
    for k in _TEXT_KEYS:
        v = i.get(k)
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            return v["text"]
    return None


def press_id(mid: str, i: dict) -> str:
    return "cp-" + kt.sha("|".join((mid, i["actorId"], i["actionId"], i["actedAt"])))[:24]


def _judge(ctx: kt.Ctx, doc: dict, i: dict) -> dict:
    """A PURE look at one interaction: the record to store. Decides nothing."""
    tid, mid, action, who = doc["ticket_id"], doc["card_message_id"], i["actionId"], i["actorId"]
    rec = {"ticket_id": tid, "card": mid, "nonce": doc["nonce"], "who": who, "action": action, "text": _text(i), "acted_at": i["actedAt"],
           "reason": None, "posted": False}
    if who != doc["target"]:
        rec["reason"] = "not_authorised"                  # the actor must BE the card's one target approver
    elif i["value"] != doc["nonce"]:
        rec["reason"] = "wrong_card"                      # the nonce must match this card
    elif action not in kt.ACTIONS:
        rec["reason"] = "unknown_action"
    elif doc.get("used_at") or ctx.approvals.decided(tid):
        rec["reason"] = "revoked" if doc.get("used_at") == "revoked" else "ticket_decided"
    else:
        rec["reason"] = ctx.approvals.check(doc["nonce"], who, action, rec["text"], mid)
    if rec["reason"] in _SILENT:
        rec.update(posted=True, reply="")
    return rec


def _commit(ctx: kt.Ctx, rid: str) -> None:
    """Idempotent: decide (once), then patch the thread map or issue the edit card. Safe to run again after a crash."""
    rec = ctx.store.get(NS, rid)
    doc = ctx.approvals.get(rec["nonce"])
    if not (doc.get("press_id") == rid and doc.get("decision")):
        doc, reason = ctx.approvals.decide(rec["nonce"], rec["who"], rec["action"], rec["text"], rec["card"], IDENTITY, rid)
        if reason:
            ctx.store.update(NS, rid, lambda d: {**d, "reason": reason})
            return
    if doc["decision"] == "edit_requested":
        nonce = ctx.approvals.issue_edit(doc, rec["who"])["nonce"]
        ctx.store.update(NS, rid, lambda d: {**d, "edit_nonce": nonce})
    else:
        ctx.threads.patch(rec["ticket_id"], decision={"by": rec["who"], "decision": doc["decision"], "nonce": doc["nonce"],
                                                      "identity": IDENTITY})


def _post(ctx: kt.Ctx, entry: dict, rid: str, rec: dict) -> bool:
    tid, root = rec["ticket_id"], entry["root_id"]
    ops = [kt.reply_op(tid, f"cardpress-{kt.sha(rid)[:10]}", root, rec["reply"])]
    edoc = ctx.approvals.get(rec.get("edit_nonce") or "")
    if edoc:
        ops += [kt.draft_op(tid, root, edoc["draft"], f"t{tid}-editdraft-{edoc['nonce'][:8]}"), kt.edit_card_op(tid, root, edoc)]
    res = kt.queue_for(ctx, entry.get("route")).run(ops)
    if edoc and res[2].get("ok") and res[2].get("message_id"):
        ctx.approvals.attach_card(edoc["nonce"], res[2]["message_id"])
    return all(r.get("ok") or r.get("refused") or r.get("capped") for r in res)


def _finish(ctx: kt.Ctx, entry: dict, rid: str) -> None:
    """Complete one stored press: commit, build the confirmation, post it. Retried by the next scan if the post fails."""
    rec = ctx.store.get(NS, rid)
    if rec.get("posted"):
        return
    if rec["reason"] is None:
        _commit(ctx, rid)
        rec = ctx.store.get(NS, rid)
    reply = kt._reply_text(ctx.st, ctx.approvals.get(rec["nonce"]), rec["action"], rec["who"], rec["reason"])
    rec = ctx.store.update(NS, rid, lambda d: {**d, "reply": reply})
    if _post(ctx, entry, rid, rec):
        ctx.store.update(NS, rid, lambda d: {**d, "posted": True})


def _scan_card(ctx: kt.Ctx, entry: dict, doc: dict) -> list[dict]:
    room = (entry.get("route") or {}).get("room_id") or ctx.st.room_id
    card = fetch_card(ctx, doc["card_message_id"], room)
    if card is None:
        ctx.out(f"ticket {entry['ticket_id']}: could not read the card; no card press was acted on")
        return []
    out = []
    for i in interactions(card):
        rid = press_id(doc["card_message_id"], i)
        if ctx.store.get(NS, rid) is not None:
            continue                                      # this exact press was already seen
        rec = _judge(ctx, doc, i)
        ctx.out(f"card press {rid} ({i['actionId']}): {rec['reason'] or 'accepted'}")
        out.append({"press_id": rid, "ticket_id": rec["ticket_id"], "reason": rec["reason"]})
        if not ctx.dry_run:
            ctx.store.put_if_absent(NS, rid, rec)         # the record is written BEFORE anything is decided
            if not rec["posted"]:
                _finish(ctx, entry, rid)
                out[-1]["reason"] = ctx.store.get(NS, rid)["reason"]
            doc = ctx.approvals.get(doc["nonce"])
    return out


def _recover(ctx: kt.Ctx) -> None:
    """Finish every stored press that did not complete (a crash, or a post that failed). Idempotent."""
    for rec in [r for r in ctx.store.scan(NS) if not r.get("posted")]:
        entry = ctx.threads.get(rec["ticket_id"])
        if entry and entry.get("root_id"):
            _finish(ctx, entry, press_id_of(rec))


def press_id_of(rec: dict) -> str:
    return press_id(rec["card"], {"actorId": rec["who"], "actionId": rec["action"], "actedAt": rec["acted_at"]})


def scan_card_presses(ctx: kt.Ctx) -> list[dict]:
    """First finish any unfinished press, then read the card of every ticket with an unused approval and decide its presses."""
    if not ctx.dry_run:
        _recover(ctx)
    out: list[dict] = []
    for entry in ctx.threads.open_threads():
        doc = _open_doc(ctx, entry["ticket_id"])
        if doc and doc.get("card_message_id"):
            out += _scan_card(ctx, entry, doc)
    return out
