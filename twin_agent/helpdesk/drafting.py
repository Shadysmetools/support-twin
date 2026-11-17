"""Build a TicketDraft from a request (state.db) + its latest packet, filled the way Supervisor fills her tickets.

Classification comes from the runtime conventions, never from hard-coded ids:
- team: `helpdesk.team_id` or Supervisor's most common team; assignee: Principal (he takes his own tickets);
- priority: Supervisor's most common priority, unless `helpdesk.priority` is set;
- x_track: "development" for a dev part / a T2a fix, else "functional" (only if the selection has the value);
- x_type: matched by NAME in x_ticket_type (Bug / New Request / Customization / Configuration / Inquiries);
- x_ai_confidence: the packet's confidence (one decimal), only if the field exists;
- partner: `helpdesk.partner_ids` by company, else a res.partner search by `helpdesk.partner_names`.
"""
from __future__ import annotations

import re

from .conventions import Conventions
from .style import X_TYPE_NAME, TicketDraft, clean

TIER_TYPE = {"T1": "Inquiry", "T2a": "Bug", "T2b": "Configuration", "T3": "Feature", "T4": "Feature"}
PART_TYPE = {"inquiry": "Inquiry", "functional": "Configuration", "dev": "Bug"}


def is_development(req: dict, packet: dict | None) -> bool:
    """Whether this request would become a DEVELOPMENT (code / bug) ticket - the signal the verify-first gate keys
    on. It is read from the DERIVED draft type, never from x_track (which `build` drops when the x_track selection
    lacks 'development', so it cannot be the signal) and never only from an explicit dev part (a T2a packet derives
    type Bug with parts=[]). True when: an explicit dev part, OR the type derived from the tier is a Bug."""
    packet = packet or {}
    parts = ((packet.get("ticket_draft") or {}).get("parts") or [])
    if any((p or {}).get("type") == "dev" for p in parts):
        return True
    part = parts[0] if parts else {}
    typ = PART_TYPE.get(part.get("type"), "") or TIER_TYPE.get(str(packet.get("tier") or req.get("tier") or ""), "Task")
    return typ == "Bug"


def _sentences(text: str) -> list[str]:
    parts = [p.strip(" -*•") for p in re.split(r"(?:\n+|(?<=[.!?])\s+|;\s+)", clean(text))]
    return [p.rstrip(".") for p in parts if len(p) > 3]


def _evidence_lines(packet: dict) -> list[str]:
    out = []
    for e in packet.get("evidence") or []:
        k = e.get("kind")
        if k == "runbook":
            out.append(f"Runbook entry {e.get('id')} ({e.get('status') or 'unknown status'})")
        elif k == "odoo_read":
            out.append(f"Read {e.get('model')} {e.get('id')}: {e.get('field')} = {e.get('value')}")
        elif k == "past_answer":
            out.append(f"Past answer: {e.get('source')}")
        elif k:
            out.append(f"{k}: {e.get('source') or e.get('id') or ''}".strip(": "))
    return out[:8]


def _trust(packet: dict, cfg: dict) -> str:
    occ = packet.get("runbook_occurrence") or {}
    has_rb = bool(occ) or any(e.get("kind") == "runbook" for e in packet.get("evidence") or [])
    if not has_rb:
        return "none"
    levels = ((cfg.get("helpdesk") or {}).get("runbook_trust") or {})
    entry = occ.get("entry") or next((e.get("id") for e in packet.get("evidence") or [] if e.get("kind") == "runbook"), "")
    return str(levels.get(entry) or "AMBER")          # knowledge rules: Principal's runbook is AMBER at best


def partner_for(odoo, cfg: dict, company: str | None) -> int | None:
    hd = cfg.get("helpdesk") or {}
    pid = int((hd.get("partner_ids") or {}).get(company or "", 0) or 0)
    if pid:
        return pid
    name = (hd.get("partner_names") or {}).get(company or "")
    if not name or odoo is None:
        return None
    try:
        rows = odoo.search_read("res.partner", [["name", "ilike", name], ["is_company", "=", True]], ["name"], limit=1)
    except Exception:                                   # noqa: BLE001
        return None
    return int(rows[0]["id"]) if rows else None


def build(req: dict, packet: dict | None, conv: Conventions, cfg: dict, *, odoo=None, verified: bool = False,
          links: list[str] | None = None, ref: str | None = None) -> TicketDraft:
    packet = packet or {}
    hd = cfg.get("helpdesk") or {}
    rid = req["id"]
    parts = ((packet.get("ticket_draft") or {}).get("parts") or [])
    part = parts[0] if parts else {}
    typ = PART_TYPE.get(part.get("type"), "") or TIER_TYPE.get(str(packet.get("tier") or req.get("tier") or ""), "Task")
    if part.get("type") == "dev" and packet.get("tier") in ("T3", "T4"):
        typ = "Feature"
    title = part.get("subject") or req.get("title") or packet.get("understanding") or f"Review request {rid}"
    bullets = _sentences(part.get("body") or packet.get("understanding") or req.get("title") or "")[:4]
    where = " ".join(x for x in (req.get("chat") or "", req.get("topic") or "") if x)
    if len(bullets) < 2:
        bullets.append(f"Reported in {where or req.get('channel') or 'a message'}; company {req.get('company') or 'unknown'}")
    if len(bullets) < 2:
        bullets.append("Details are in the internal note")
    channel = str(req.get("channel") or "")
    source = f"{'Email' if 'mail' in channel else 'Telegram'}{', ' + where if where else ''} (ref {ref or rid})"
    conf = packet.get("confidence")
    ver = "verified" if verified else ("provisional" if isinstance(conf, (int, float)) and conf >= 0.7 else "unverified")
    d = TicketDraft(request_id=rid, title=title, type=typ, requester=str(req.get("requester_name") or ""),
                    source=source, bullets=bullets, evidence=_evidence_lines(packet),
                    diagnosis=str(packet.get("note") or packet.get("understanding") or ""), verification=ver,
                    runbook_trust=_trust(packet, cfg), links=list(links or []))
    supervisor = conv.supervisor or {}
    d.team_id = int(hd.get("team_id") or supervisor.get("team_id") or 0) or None
    d.priority = str(hd.get("priority") if hd.get("priority") is not None else supervisor.get("priority") or "0")
    track = "development" if (part.get("type") == "dev" or typ == "Bug") else "functional"
    d.x_track = track if track in conv.track_values else None
    if conv.has("x_ai_confidence") and isinstance(conf, (int, float)):
        d.x_ai_confidence = round(max(0.0, min(1.0, float(conf))), 1)
    d.partner_id = partner_for(odoo, cfg, req.get("company"))
    d.tag_names = list(hd.get("default_tags") or [])
    d.dedupe_keys = [rid] + ([f"msg:{req['root_msg_id']}"] if req.get("root_msg_id") else [])
    return d


def create_vals(d: TicketDraft, conv: Conventions, stage_id: int, description_html: str) -> dict:
    """helpdesk.ticket create values: only fields that exist at runtime, never x_type_code."""
    from .style import imperative_title
    vals: dict = {"name": imperative_title(d.title, d.type), "description": description_html, "stage_id": stage_id}
    if d.team_id and conv.has("team_id"):
        vals["team_id"] = d.team_id
    if conv.principal_uid and conv.has("user_id"):
        vals["user_id"] = conv.principal_uid
    if d.partner_id and conv.has("partner_id"):
        vals["partner_id"] = d.partner_id
    if d.priority is not None and conv.has("priority"):
        vals["priority"] = d.priority
    if d.x_track and conv.has("x_track"):
        vals["x_track"] = d.x_track
    tid = conv.types.get(X_TYPE_NAME.get(d.type.lower(), ""))
    if tid and conv.has("x_type"):
        vals["x_type"] = tid
    if d.x_ai_confidence is not None and conv.has("x_ai_confidence"):
        vals["x_ai_confidence"] = d.x_ai_confidence
    tags = [conv.tags[t.lower()] for t in d.tag_names if t.lower() in conv.tags]
    if tags and conv.has("tag_ids"):
        vals["tag_ids"] = [(6, 0, tags)]
    return vals
