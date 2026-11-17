"""Recompute 'verified' and the fix-gate exemption in code (05 3.2, 3.4; red-team #2).

The worker's own values for `verified` and `proposed_action.approver` are never trusted. Everything here is
deterministic and fails towards the safe answer: not verified, and "supervisor" (Supervisor reviews the shell block).
"""
from __future__ import annotations

import hashlib
import hmac
import re

_SAFE_TIERS = {"T1", "T2a"}
_PARAM = r"[A-Za-z0-9_./:-]{1,64}"       # filled template parameters: ids and record refs only, no quotes or code
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_ASSERT_ONE = re.compile(r"assert\s+len\s*\([^)]*\)\s*==\s*1\b")
_WRITE_CALL = re.compile(r"\.(?:write|unlink|create|copy|action_\w+|button_\w+|_action_\w+)\s*\(")
_LOOP = re.compile(r"(?m)^\s*(?:for|while)\s")


# ---------------------------------------------------------------- hashes

def draft_hash(text: str) -> str:
    """sha256 of the judged draft text; the Approve button must carry the same value."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fix_block_hash(script_text: str) -> str:
    """sha256 of the exact bytes of fix.py (no line-ending normalisation), shown on the card."""
    return hashlib.sha256(script_text.encode("utf-8")).hexdigest()


def fix_hash_matches(script_text: str, reviewed_sha: str) -> bool:
    """True only if what Supervisor (or Principal) reviewed is byte-identical to what will run."""
    return bool(reviewed_sha) and hmac.compare_digest(fix_block_hash(script_text), reviewed_sha.lower())


# ---------------------------------------------------------------- runbook lookups

def _slugify(s: str) -> str:
    s = s.split("(")[0]
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def class_slug(packet_class: str | None) -> str:
    """'RB stray-in-line' -> 'stray-in-line'."""
    c = (packet_class or "").strip()
    if c.upper().startswith("RB "):
        c = c[3:].strip()
    return c


def find_entry(slug: str, runbook: dict[str, dict]) -> tuple[str, dict] | None:
    """Find a runbook entry by slug, case-insensitive, or by the slug of its title."""
    if not slug or slug.lower() == "unknown":
        return None
    if slug in runbook:
        return slug, runbook[slug]
    low = slug.lower()
    for key, entry in runbook.items():
        if key.lower() == low or _slugify(str(entry.get("title", ""))) == low:
            return key, entry
    return None


def is_confirmed(entry: dict | None) -> bool:
    """Status must be exactly 'confirmed' (optionally followed by a date), never 'provisional'."""
    status = str((entry or {}).get("status", "")).strip().lower()
    return status.split(" ")[0].rstrip(",;:") == "confirmed" if status else False


# ---------------------------------------------------------------- verified

def recompute_verified(packet: dict, runbook: dict[str, dict], *, read_user_exists: bool, judges_passed: bool) -> bool:
    """05 3.2. Any doubt means False (the request then gets the holding path)."""
    try:
        if not judges_passed:
            return False
        if packet.get("tier") not in _SAFE_TIERS:
            return False
        if packet.get("questions_for_principal"):
            return False
        evidence = packet.get("evidence") or []
        if not evidence:
            return False
        confirmed_rb = past_answers = good_reads = 0
        for item in evidence:
            kind = item.get("kind")
            if str(item.get("status", "")).lower().startswith("provisional"):
                return False
            if kind == "runbook":
                found = find_entry(str(item.get("id", "")), runbook)
                if not found or not is_confirmed(found[1]):
                    return False
                confirmed_rb += 1
            elif kind == "odoo_read":
                if not read_user_exists or packet.get("company") not in ("EG", "KSA"):
                    return False
                good_reads += 1
            elif kind == "past_answer":
                past_answers += 1
        slug = class_slug(packet.get("class"))
        if slug and slug.lower() != "unknown":
            found = find_entry(slug, runbook)
            if not found or not is_confirmed(found[1]):
                return False
        return confirmed_rb >= 1 or past_answers >= 2 or good_reads >= 1
    except Exception:
        return False


# ---------------------------------------------------------------- fix-gate exemption

def _matches_template(template: str, script: str) -> bool:
    """True if script is the template with each {{param}} filled by a plain id or record ref."""
    parts, pos, names = [], 0, {}
    for m in _PLACEHOLDER.finditer(template):
        parts.append(re.escape(template[pos:m.start()]))
        name = m.group(1)
        if name in names:
            parts.append(f"(?P={name})")           # the same parameter must get the same value
        else:
            names[name] = True
            parts.append(f"(?P<{name}>{_PARAM})")
        pos = m.end()
    parts.append(re.escape(template[pos:]))
    if not names:
        return template == script
    return re.fullmatch("".join(parts), script) is not None


def recompute_exemption(packet: dict, runbook: dict[str, dict], b5_exempt: set[str], script_text: str | None) -> str:
    """'exempt' only if ALL hold, else 'supervisor' (05 3.4 Fix-gate exemption):
    confirmed runbook class on Principal's B5 list; block is that class's template with filled params;
    it asserts exactly 1 record; exactly 1 record in the action; no chained moves."""
    try:
        action = packet.get("proposed_action") or {}
        if action.get("type") != "run_this" or not script_text:
            return "supervisor"
        found = find_entry(class_slug(packet.get("class")), runbook)
        if not found or not is_confirmed(found[1]):
            return "supervisor"
        key, entry = found
        exempt = {s.lower() for s in (b5_exempt or set())}
        if key.lower() not in exempt and _slugify(str(entry.get("title", ""))) not in exempt:
            return "supervisor"
        records = action.get("records") or []
        if len(records) != 1:
            return "supervisor"
        template = entry.get("template")
        if not template or not _matches_template(template, script_text):
            return "supervisor"
        if not _ASSERT_ONE.search(script_text):
            return "supervisor"
        if len(_WRITE_CALL.findall(script_text)) > 1 or _LOOP.search(script_text):
            return "supervisor"
        return "exempt"
    except Exception:
        return "supervisor"
