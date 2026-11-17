"""Packet and card validation, stdlib only (CONTRACT section 5).

The JSON schema files in worker/ document the same rules for the worker; this module is what code trusts.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .models import Button, Card, CardKind, Company, Tier, parse_ts, utc_iso

REQ_ID = re.compile(r"^REQ-\d{4,}(-[a-c])?$")
EVIDENCE_KINDS = {"runbook", "odoo_read", "past_answer", "memory", "thread", "ocr"}
LANGS = {"en", "franco", "ar"}
ACTION_TYPES = {"run_this", "ui_steps", "ticket", "approver_question"}
PART_TYPES = {"inquiry", "functional", "dev"}
CARD_TO = {"principal", "backup"}

REQUIRED = ["request_id", "class", "tier", "company", "understanding", "evidence", "verified",
            "draft_reply", "after_fix_reply", "proposed_action", "ticket_draft",
            "questions_for_principal", "runbook_occurrence", "note", "confidence"]


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def validate_packet(p: Any, request_id: str | None = None) -> list[str]:
    """Return a list of human-readable problems; [] means the packet is well formed."""
    if not isinstance(p, dict):
        return ["packet is not a JSON object"]
    errs = [f"missing key: {k}" for k in REQUIRED if k not in p]
    if errs:
        return errs
    if not _is_str(p["request_id"]) or not REQ_ID.match(p["request_id"]):
        errs.append("request_id must look like REQ-0001")
    elif request_id and p["request_id"] != request_id:
        errs.append(f"request_id {p['request_id']} does not match {request_id}")
    if not _is_str(p["class"]) or not p["class"]:
        errs.append("class must be a non-empty string")
    if p["tier"] not in {t.value for t in Tier}:
        errs.append("tier must be one of T1,T2a,T2b,T3,T4,UNKNOWN")
    if p["company"] not in {c.value for c in Company}:
        errs.append("company must be EG, KSA or UNKNOWN")
    if not _is_str(p["understanding"]) or not p["understanding"].strip():
        errs.append("understanding must be a non-empty string")
    if not isinstance(p["evidence"], list):
        errs.append("evidence must be a list")
    else:
        for i, ev in enumerate(p["evidence"]):
            if not isinstance(ev, dict) or ev.get("kind") not in EVIDENCE_KINDS:
                errs.append(f"evidence[{i}].kind must be one of {sorted(EVIDENCE_KINDS)}")
            elif ev["kind"] == "runbook" and not (_is_str(ev.get("id")) and _is_str(ev.get("status"))):
                errs.append(f"evidence[{i}] runbook item needs id and status")
    if not isinstance(p["verified"], bool):
        errs.append("verified must be a boolean")
    dr = p["draft_reply"]
    if dr is not None:
        if not isinstance(dr, dict) or not _is_str(dr.get("text")) or not _is_str(dr.get("channel")):
            errs.append("draft_reply must be null or {channel, lang, text}")
        elif dr.get("lang") not in LANGS:
            errs.append("draft_reply.lang must be en, franco or ar")
    if p["after_fix_reply"] is not None and not _is_str(p["after_fix_reply"]):
        errs.append("after_fix_reply must be a string or null")
    pa = p["proposed_action"]
    if pa is not None:
        if not isinstance(pa, dict) or pa.get("type") not in ACTION_TYPES:
            errs.append(f"proposed_action.type must be one of {sorted(ACTION_TYPES)}")
        elif not isinstance(pa.get("records", []), list):
            errs.append("proposed_action.records must be a list")
    td = p["ticket_draft"]
    if td is not None:
        parts = td.get("parts") if isinstance(td, dict) else None
        if not isinstance(parts, list) or not parts or len(parts) > 3:
            errs.append("ticket_draft.parts must be a list of 1 to 3 parts")
        else:
            for i, part in enumerate(parts):
                if not isinstance(part, dict) or part.get("type") not in PART_TYPES \
                        or not _is_str(part.get("subject")) or not _is_str(part.get("body")):
                    errs.append(f"ticket_draft.parts[{i}] needs part, type, subject, body")
    if not isinstance(p["questions_for_principal"], list) or not all(_is_str(q) for q in p["questions_for_principal"]):
        errs.append("questions_for_principal must be a list of strings")
    ro = p["runbook_occurrence"]
    if ro is not None and (not isinstance(ro, dict) or not _is_str(ro.get("entry"))):
        errs.append("runbook_occurrence must be null or {entry, date, record, status}")
    if not _is_str(p["note"]) or len(p["note"].splitlines()) > 15:
        errs.append("note must be a string of at most 15 lines")
    c = p["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
        errs.append("confidence must be a number from 0 to 1")
    return errs


def normalize_packet(p: dict) -> dict:
    """Throw away the fields the model must never decide (red-team #2). Returns a copy."""
    q = copy.deepcopy(p)
    q["verified"] = False
    if isinstance(q.get("proposed_action"), dict):
        q["proposed_action"]["approver"] = None
        q["proposed_action"]["script"] = None   # the listener writes fix.py itself
        q["proposed_action"]["sha256"] = None
    if isinstance(q.get("runbook_occurrence"), dict):
        q["runbook_occurrence"]["status"] = "provisional"
    # A worker must NEVER author proof provenance: `proof` is written only by the proof pipeline (proof/pipeline.py)
    # and is the field the verify-first dev-ticket gate (proof.pipeline.proof_cleared) trusts. A prompt-injected or
    # hallucinating worker could otherwise set proof={"status":"prepared",...} and forge a cleared gate. Strip it
    # (and the derived proof_link) so only the pipeline can set them. proof_cleared ALSO requires a signature, so a
    # forged proof cannot clear the gate even on a packet that did not pass through here (defence in depth).
    for k in ("proof", "proof_link"):
        q.pop(k, None)
    return q


# ---------------------------------------------------------------- cards

def card_to_dict(card: Card) -> dict:
    return {
        "kind": str(card.kind), "text": card.text, "request_id": card.request_id,
        "buttons": [[{"label": b.label, "action": b.action} for b in row] for row in card.buttons],
        "to": card.to, "silent": card.silent, "attachments": list(card.attachments),
        "expires_at": utc_iso(card.expires_at),
    }


def card_from_dict(d: dict) -> Card:
    return Card(
        kind=CardKind(d["kind"]), text=d["text"], request_id=d.get("request_id"),
        buttons=[[Button(**b) for b in row] for row in d.get("buttons", [])],
        to=d.get("to", "principal"), silent=bool(d.get("silent", False)),
        attachments=list(d.get("attachments", [])), expires_at=parse_ts(d.get("expires_at")),
    )


def validate_card(c: Any) -> list[str]:
    if not isinstance(c, dict):
        return ["card is not a JSON object"]
    errs = []
    if c.get("kind") not in {k.value for k in CardKind}:
        errs.append("unknown card kind")
    if not _is_str(c.get("text")) or not c["text"].strip():
        errs.append("text must be non-empty")
    elif len(c["text"]) > 3500:
        errs.append("text longer than 3500 characters")
    if c.get("to", "principal") not in CARD_TO:
        errs.append("to must be principal or backup")
    rid = c.get("request_id")
    if rid is not None and (not _is_str(rid) or not REQ_ID.match(rid)):
        errs.append("request_id must be null or REQ-0001 style")
    buttons = c.get("buttons", [])
    if not isinstance(buttons, list) or not all(isinstance(r, list) for r in buttons):
        errs.append("buttons must be a list of rows")
    else:
        for row in buttons:
            for b in row:
                if not isinstance(b, dict) or not _is_str(b.get("label")) or not _is_str(b.get("action")):
                    errs.append("each button needs label and action")
                elif not re.fullmatch(r"[a-z0-9]{2,3}", b["action"]):
                    errs.append(f"bad action code {b['action']!r}")
    return errs
