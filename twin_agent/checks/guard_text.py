"""Text rules used by the code guards (05 3.4, 07 sections 2, 3, 8). Pure functions, no AI, no I/O.

Each function returns a list of short reasons. An empty list means the text passed that rule.
Reasons never contain the matched secret itself, only its kind.
"""
from __future__ import annotations

import re
from typing import Iterable

from .. import redact

# ---------------------------------------------------------------- system text

DEFAULT_SYSTEM_TEXT = [
    r"NO_REPLY",
    r"Traceback \(most recent call last\)",
    r"(?i)^\s*(error|exception)\b[:\s]",
    r"(?i)\b\w+(Error|Exception):",
    r"mcp__|tool_use|tool_result",
    r"^\s*[\[{]",
    r"^\s*(null|None|undefined)\s*$",
]


def system_text(text: str, patterns: Iterable[str] | None = None) -> list[str]:
    """Empty text, NO_REPLY, stack traces, 'Error: ...', JSON, tool names (07 3.2)."""
    if not text or not text.strip():
        return ["empty text"]
    out = []
    for pat in patterns or DEFAULT_SYSTEM_TEXT:
        if re.search(pat, text):
            out.append(f"system text matches {pat!r}")
    return out


# ---------------------------------------------------------------- secrets and personal data

def secrets(text: str, vault_hashes: Iterable[str] = ()) -> list[str]:
    kinds = sorted({f.kind for f in redact.scan(text, vault_hashes) if f.kind not in ("email", "phone")})
    return [f"secret-like value ({k})" for k in kinds]


def personal_data(text: str, allowed_emails: Iterable[str] = ()) -> list[str]:
    """Phone numbers and email addresses (customer names cannot be found by code; the judges look for them)."""
    allowed = {e.lower() for e in allowed_emails if e}
    out = set()
    for f in redact.scan(text):
        if f.kind == "phone":
            out.add("phone number")
        elif f.kind == "email" and text[f.start:f.end].lower() not in allowed:
            out.add("email address")
    return sorted(out)


# ---------------------------------------------------------------- links and ticket refs

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def links(text: str, allow_prefixes: Iterable[str]) -> list[str]:
    """Every link must start with an allow-listed prefix and carry no token (05 3.4 Links)."""
    allow = [p.lower() for p in allow_prefixes]
    out = []
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:")
        if not any(url.lower().startswith(p) for p in allow):
            out.append("link not on the allow-list")
        elif redact.strip_url_token(url) != url:
            out.append("link carries a token")
    return sorted(set(out))


_BARE_TICKET = re.compile(r"(?<![\w/&])#\s?\d{2,}\b")


def ticket_refs(text: str) -> list[str]:
    """Never a bare '#NNNN' (collides with Braincrew tickets and Shopify orders, 07 8 #12)."""
    return ["bare #NNNN ticket number"] if _BARE_TICKET.search(text) else []


# ---------------------------------------------------------------- promises

DEFAULT_PROMISE_WORDS = ["today", "in 1 hour", "in an hour", "guaranteed", "will be fixed by", "tomorrow"]
_DATE_PATTERNS = [
    re.compile(r"\b20\d{2}-\d{1,2}-\d{1,2}\b"),                       # 2026-11-05
    re.compile(r"(?<![\w/])\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?(?![\w/])"),  # 5/11, 05.11.2026 (not IN/10001/26)
    re.compile(r"(?i)\b(?:by|before|on|until)\s+(?:sun|mon|tues|wednes|thurs|fri|satur)day\b"),
    re.compile(r"(?i)\b(?:by|before|until)\s+(?:end of (?:the )?(?:day|week)|eod|next week)\b"),
    re.compile(r"(?i)\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"),
]


def promises(text: str, words: Iterable[str] | None = None, filled_date: str | None = None,
             check_dates: bool = True) -> list[str]:
    """Block promise words and dates. The one date code filled into a template is allowed (05 3.4, red-team #20).
    check_dates=False keeps the promise words but lets dates through (Principal's Approve on the exact text)."""
    if filled_date:
        text = text.replace(filled_date, " ")
    out = []
    for w in words or DEFAULT_PROMISE_WORDS:
        if re.search(r"(?i)(?<!\w)" + re.escape(w) + r"(?!\w)", text):
            out.append(f"promise word {w!r}")
    if check_dates and any(p.search(text) for p in _DATE_PATTERNS):
        out.append("date or deadline in text")
    return out


# ---------------------------------------------------------------- persona

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
_ARABIC = re.compile("[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_SIGNOFF = re.compile(r"(?im)^\s*(?:best regards|kind regards|regards|best|thanks and regards|cheers|sincerely|"
                      r"br|thank you)\s*,?\s*$")
_ACME_ROUTE = re.compile(r"(?i)\b(?:my|his|principal'?s)\s+(?:move|moving|transfer)\s+to\s+acme\b|"
                             r"\bacme\b[^.\n]{0,40}\b(?:faster|quicker|instead of principal)\b")


def persona(text: str, *, telegram: bool, marker: str, want_marker: bool | None) -> list[str]:
    """07 section 2 hard rules. want_marker: True = must end with the marker, False = must not, None = either."""
    out = []
    if "—" in text or "–" in text:
        out.append("em or en dash")
    if _EMOJI.search(text):
        out.append("emoji")
    if "!" in text:
        out.append("exclamation mark")
    if re.search(r"(?i)\bsorry\b|\bapolog", text):
        out.append("apology")
    if re.search(r"(?m)^\s*(?:•|\*\s)", text):
        out.append("bullet that is not '-'")
    if _SIGNOFF.search(text):
        out.append("typed sign-off")
    if _ACME_ROUTE.search(text):
        out.append("mentions the Acme move or route")
    if telegram and _ARABIC.search(text):
        out.append("Arabic script on Telegram")
    ends_with_marker = bool(marker) and text.rstrip().endswith(marker)
    if want_marker is True and not ends_with_marker:
        out.append("autopilot text must end with the marker")
    if want_marker is False and ends_with_marker:
        out.append("approved text must not carry the marker")
    return out


# ---------------------------------------------------------------- no writes, no stage moves

_WRITE_PATTERNS = [
    re.compile(r"```"),
    re.compile(r"(?i)\benv\s*\[\s*['\"]"),
    re.compile(r"\.(?:write|unlink|create|sudo)\s*\("),
    re.compile(r"(?i)\b(?:update\s+\w+\s+set|delete\s+from|insert\s+into|drop\s+table|alter\s+table)\b"),
    re.compile(r"(?i)\b(?:execute_kw|xmlrpc|jsonrpc|odoo-bin|odoo shell|psql)\b"),
    re.compile(r"(?im)^\s*(?:\$|PS [A-Z]:\\|>>>)\s"),
]


def no_writes(text: str) -> list[str]:
    """Never send a write script, SQL, or a shell block to a client (07 8 #14)."""
    return ["write script, SQL or shell block"] if any(p.search(text) for p in _WRITE_PATTERNS) else []


def no_stage_moves(text: str) -> list[str]:
    return ["stage move instruction"] if re.search(r"(?i)\bstage_id\b", text) else []
