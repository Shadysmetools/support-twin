"""Reply checker (05 3.3 check 5): persona, language, every question answered, promises, links.

Pure code, no AI. Two groups of rules:
- safety (always problems): system text, secrets, personal data, links, bare ticket numbers, promises,
  the Acme-move rule, Arabic script on Telegram, wrong language for the channel;
- persona (07 section 2): dashes, emoji, "!", "sorry", bullets, signatures, greetings, length.
On Principal's own edited text (own_text=True) persona rules and promises are warnings only (red-team #24): he may
type a date himself. On a fixed template (is_template=True) the filled update date is allowed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import persona
from ..models import Channel
from ..redact import scan

from .. import profile as _profile
KEEP_EMAILS = {_profile.get("helpdesk_email")}


@dataclass
class ReplyCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
_URL = re.compile(r"https?://[^\s<>\")\]]+", re.I)
_ALLOWED_LINKS = [
    re.compile(r"^https://helpdesk\.example\.com/(my/)?(ticket|tickets|helpdesk)s?/\d+/?$", re.I),
    re.compile(r"^https://helpdesk\.example\.com/odoo/helpdesk/\d+/\d+/?$", re.I),
    re.compile(r"^https://wiki\.example\.com/share/[A-Za-z0-9_-]+/?$", re.I),
    re.compile(r"^https://([a-z0-9-]+\.)*globex[a-z0-9-]*\.(odoo\.com|com)/(odoo|web)[^\s?]*(#[^\s]*)?$", re.I),
]
_SYSTEM_TEXT = re.compile(
    r"(\bNO_REPLY\b|Traceback \(most recent call last\)|^\s*(Error|Exception|ERROR)\s*:|\bat line \d+\b|"
    r"\b(submit_packet|mcp__\w+|kb_search|tg_history|claude -p|packet\.json|json)\b|^\s*[\[{])", re.I | re.M)
_PROMISES = re.compile(
    r"\b(today|tonight|tomorrow|this (morning|afternoon|evening|week)|in (an? |\d+ )(min(ute)?s?|hours?|days?)|"
    r"within \d+ (min(ute)?s?|hours?|days?)|guarantee[ds]?|will be (fixed|done|ready|solved|resolved)|"
    r"by (sunday|monday|tuesday|wednesday|thursday|friday|saturday|end of (day|week)|eod|\d{1,2}(:\d\d)? ?(am|pm)))\b",
    re.I)
_TEMPLATE_DATE = re.compile(r"update by (sunday|monday|tuesday|wednesday|thursday|friday|saturday) \d{1,2} \w+", re.I)
_ACME_MOVE = re.compile(
    r"\b(i('m| am) (now )?(with|at|joining|moving to) acme|my (new )?(job|role) (at|with) acme|"
    r"(moved|moving|joined|joining) (to )?acme|acme (is )?(faster|quicker|a separate|another) (route|way|channel))",
    re.I)
_SIGNATURE = re.compile(r"^\s*(best regards|kind regards|regards|thanks and regards|br|sincerely|principal doe)\s*,?\s*$",
                        re.I | re.M)
_TITLE_LINE = re.compile(r"(business intelligence|operations systems analyst|\bmobile\s*:)", re.I)
_SMALL_TALK = re.compile(r"\b(hope (you are|you're|this finds you) (well|doing well)|i hope you are well)\b", re.I)
_APOLOGY = re.compile(r"\b(sorry|apolog(y|ies|ise|ize|ised|ized))\b", re.I)
_BAD_BULLET = re.compile(r"^\s*([•●▪‣⁃]|\*\s)", re.M)
_BARE_TICKET = re.compile(r"(?<![\w/&])#\d{3,6}\b")
_STOP = {"the", "and", "for", "with", "this", "that", "what", "when", "where", "which", "why", "how", "does",
         "can", "could", "should", "would", "will", "are", "was", "were", "have", "has", "there", "from", "into",
         "please", "you", "your", "our", "not", "any", "all", "is", "it", "its", "of", "to", "in", "on", "a", "an"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9/]{3,}", text.lower()) if w not in _STOP}


def _personal_and_secrets(text: str) -> list[str]:
    out = []
    for f in scan(text):
        piece = text[f.start:f.end]
        if f.kind == "email" and piece.lower() in KEEP_EMAILS:
            continue
        label = "personal data" if f.kind in ("email", "phone") else "secret"
        out.append(f"{label}: {f.kind}")
    return sorted(set(out))


def _links(text: str) -> list[str]:
    bad = []
    for url in _URL.findall(text):
        url = url.rstrip(".,;:")
        if not any(rx.match(url) for rx in _ALLOWED_LINKS):
            bad.append(f"link not on the allow-list: {url[:60]}")
    return bad


def unanswered(text: str, questions) -> list[str]:
    """Questions with no content word (or record ref) in the reply. A crude floor; the AI checker goes deeper."""
    have = _words(text)
    return [q for q in questions if _words(q) and not (_words(q) & have)]


def check_reply(text: str, *, channel: Channel, lang: str, questions: list[str] = (), own_text: bool = False,
                is_template: bool = False) -> ReplyCheck:
    problems: list[str] = []
    style: list[str] = []          # persona rules: problems, or warnings on Principal's own text
    family = persona.channel_family(str(channel))
    body = (text or "").strip()

    # ---- safety: always block
    if not body or body.upper() in ("NO_REPLY", "NONE", "NULL"):
        return ReplyCheck(False, ["empty or NO_REPLY text"], [])
    if _SYSTEM_TEXT.search(body):
        problems.append("system text (error, JSON, tool name or NO_REPLY)")
    problems += _personal_and_secrets(body)
    problems += _links(body)
    if _BARE_TICKET.search(body):
        problems.append('bare "#NNNN" ticket number: use the full ticket URL')
    if _ACME_MOVE.search(body):
        problems.append("mentions the Acme move or a separate Acme route")
    allowed = persona.LANGS.get(family, ("en",))
    if lang not in allowed:
        problems.append(f"language {lang!r} not allowed on {family} (allowed: {', '.join(allowed)})")
    if family == "telegram" and _ARABIC.search(body):
        problems.append("Arabic script on Telegram")
    if family != "telegram" and _ARABIC.search(body):
        style.append("Arabic script in an English-only channel")
    if _SIGNATURE.search(body):
        problems.append("typed sign-off or signature (Gmail adds it)")
    if _TITLE_LINE.search(body):
        problems.append("signature block (title or phone line)")

    # ---- promises: blocked unless the fixed template's filled date, or Principal typed it
    promise_text = _TEMPLATE_DATE.sub("", body) if is_template else body
    m = _PROMISES.search(promise_text)
    if m:
        (style if own_text else problems).append(f"promise or date: {m.group(0)!r}")

    # ---- persona hard rules (07 section 2)
    if re.search("[—–]", body):
        style.append("em dash or en dash")
    if _EMOJI.search(body):
        style.append("emoji")
    if "!" in body:
        style.append("exclamation mark")
    if _APOLOGY.search(body):
        style.append('apology ("sorry")')
    if _BAD_BULLET.search(body):
        style.append('bullet that is not "-"')
    if _SMALL_TALK.search(body):
        style.append("small talk opener")
    if family == "email" and (re.search(r"<(p|br|div|b|i|a|table)\b", body, re.I) or "```" in body):
        style.append("HTML or code block in email")
    if family == "telegram" and not is_template:
        words = len(body.split())
        if words > 60:
            style.append(f"too long for Telegram ({words} words; his p90 is about 25)")

    # ---- every question answered (crude floor)
    for q in unanswered(body, list(questions or [])):
        problems.append(f"question not answered: {q[:80]}")

    warnings: list[str] = []
    if own_text:
        warnings += style
    else:
        problems += style
    return ReplyCheck(not problems, problems, warnings)
