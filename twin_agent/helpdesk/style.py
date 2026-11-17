"""Principal's TICKET-STYLE (handover/principal/triage/TICKET-STYLE.md) in code, plus the descriptive internal note.

Ticket: a Title Case imperative title (what + where), then `Requester:`, `Type:`, `Source:`, then `Request` with 2 to
4 plain "-" bullets. No em dash, no Markdown, simple English. `lint()` lists every rule a draft breaks; the desk
refuses to offer a draft that has problems.

Internal note (never emailed: posted as mail.mt_note with no partners): evidence, diagnosis, verification status,
runbook trust level and related commits / PRs / Wiki links. Stamped "[Digital Twin]" so a reader can tell the
twin's notes from Principal's own (02h 4.1: RPC actions are attributed to the human login).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from ..redact import has_secrets, redact, strip_url_token

SMALL = {"a", "an", "the", "and", "or", "but", "nor", "of", "on", "in", "to", "for", "at", "by", "with", "from",
         "as", "per", "via", "vs"}
VERBS = {"fix", "add", "allow", "stop", "show", "create", "update", "remove", "review", "investigate", "restore",
         "enable", "disable", "configure", "change", "correct", "exclude", "prevent", "move", "import", "export",
         "set", "make", "build", "support", "check", "resend", "unblock", "validate", "clean", "merge", "explain",
         "answer", "link", "split", "hide", "rename", "recompute", "deliver", "send", "post", "reconcile", "map"}
TYPE_VERB = {"bug": "Fix", "feature": "Add", "task": "Review", "configuration": "Configure", "inquiry": "Answer"}
# TICKET-STYLE "Type" -> x_ticket_type name (02h 2.3: 1 Bug, 2 New Request, 3 Customization, 4 Configuration,
# 5 Inquiries). Matched by name at runtime, never by a hard-coded id.
X_TYPE_NAME = {"bug": "bug", "feature": "new request", "customization": "customization",
               "configuration": "configuration", "task": "configuration", "inquiry": "inquiries"}
TRUST = ("GREEN", "AMBER", "RED", "none")
VERIFICATION = ("verified", "provisional", "unverified")
DASHES = ("—", "–")                   # em dash and en dash
MD = re.compile(r"(\*\*|`|^#|^\s*[*•]\s)", re.M)


@dataclass
class TicketDraft:
    request_id: str
    title: str
    type: str = "Task"                           # Bug | Feature | Task | Configuration | Inquiry | Customization
    requester: str = ""
    source: str = ""
    bullets: list[str] = field(default_factory=list)
    # internal note
    evidence: list[str] = field(default_factory=list)
    diagnosis: str = ""
    verification: str = "unverified"
    runbook_trust: str = "none"
    links: list[str] = field(default_factory=list)
    # Odoo classification (filled from conventions)
    partner_id: int | None = None
    team_id: int | None = None
    priority: str | None = None
    x_track: str | None = None
    x_ai_confidence: float | None = None
    tag_names: list[str] = field(default_factory=list)
    dedupe_keys: list[str] = field(default_factory=list)   # REQ id, email Message-ID, source link


def title_case(s: str) -> str:
    words, out = s.strip().split(), []
    for i, w in enumerate(words):
        core = w.strip("\"'()[]")
        if any(ch.isdigit() for ch in core) or (core.isupper() and len(core) > 1) or "." in core or "_" in core \
                or w[:1] in "\"'" or (core[:1].isupper() and any(ch.isupper() for ch in core[1:])):
            out.append(w)                          # IDs, acronyms, quoted field names, CamelCase: as-is
        elif core.lower() in SMALL and 0 < i < len(words) - 1:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def imperative_title(title: str, type_: str = "Task") -> str:
    t = re.sub(r"\s+", " ", clean(title)).strip(" .:-")
    first = t.split(" ", 1)[0].lower() if t else ""
    if first not in VERBS:
        word = t.split(" ", 1)[0] if t else ""
        # lower-case only a normal capitalised word ("Receipt" -> "receipt"); keep codes like BATCH/13502 or
        # WH/OUT/0001 as they are (Principal 2026-11-09: the title came out as "Answer bATCH/13502")
        plain = word[:1].isupper() and word[1:2].islower()
        t = f"{TYPE_VERB.get(type_.lower(), 'Review')} {(t[:1].lower() + t[1:]) if plain else t}" if t \
            else "Review Request"
    return title_case(t)[:120]


def clean(text: str) -> str:
    """Plain, paste-ready text: no dashes that break the style, no Markdown marks, no secrets, no URL tokens."""
    t = redact(str(text or ""))
    for d in DASHES:
        t = t.replace(d, "-")
    t = t.replace("**", "").replace("`", "").replace("•", "-")
    t = re.sub(r"https?://\S+", lambda m: strip_url_token(m.group(0)), t)
    return re.sub(r"[ \t]+", " ", t).strip()


def body_lines(d: TicketDraft) -> list[str]:
    lines = []
    if d.requester:
        lines.append(f"Requester: {clean(d.requester)}")
    if d.type:
        lines.append(f"Type: {clean(d.type)}")
    if d.source:
        lines.append(f"Source: {clean(d.source)}")
    lines.append("Request")
    lines += [f"- {clean(b).lstrip('-* ').strip()}" for b in d.bullets if clean(b).strip("-* ")]
    return lines


def plain_ticket(d: TicketDraft) -> str:
    return "\n".join([imperative_title(d.title, d.type), *body_lines(d)])


def html_lines(lines: list[str]) -> str:
    """Readable note HTML: '- x' lines become one bullet list, section words (Evidence, Diagnosis, Request...)
    are bold, every other line is a paragraph. All text is html.escape()d."""
    out, items = [], []

    def close_list():
        if items:
            out.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>")
            items.clear()
    for x in lines:
        s = str(x)
        if s.startswith("- "):
            items.append(s[2:])
            continue
        close_list()
        if s.strip() and len(s) <= 24 and ":" not in s and s.strip()[:1].isupper() and " " not in s.strip():
            out.append(f"<p><b>{html.escape(s)}</b></p>")
        else:
            out.append(f"<p>{html.escape(s)}</p>")
    close_list()
    return "".join(out)


def note_lines(d: TicketDraft) -> list[str]:
    ver = d.verification if d.verification in VERIFICATION else "unverified"
    trust = d.runbook_trust if d.runbook_trust in TRUST else "none"
    lines = [f"[Digital Twin] Internal note for {d.request_id}", "Evidence"]
    lines += [f"- {clean(e)}" for e in d.evidence] or ["- none recorded yet"]
    lines += ["Diagnosis", f"- {clean(d.diagnosis) or 'not diagnosed yet'}",
              f"Verification: {ver}" + ("" if ver == "verified" else " (not proven on the record yet)"),
              f"Runbook trust: {trust}" + (" (a hint, not truth)" if trust in ("AMBER", "RED") else "")]
    if d.links:
        lines += ["Related"] + [f"- {clean(x)}" for x in d.links]
    return lines


def lint(d: TicketDraft) -> list[str]:
    """Every TICKET-STYLE rule this draft breaks ([] = ready)."""
    probs = []
    title = imperative_title(d.title, d.type)
    raw = "\n".join([d.title, d.requester, d.source, *d.bullets, d.diagnosis, *d.evidence, *d.links])
    if title != title_case(title):
        probs.append("title is not Title Case")
    if len(title) > 100:
        probs.append("title longer than 100 characters")
    n = len([b for b in d.bullets if clean(b).strip("-* ")])
    if not 2 <= n <= 4:
        probs.append(f"Request needs 2 to 4 bullets (has {n})")
    if MD.search("\n".join(body_lines(d))):
        probs.append("Markdown marks in the body")
    if has_secrets(raw):
        probs.append("possible secret in the draft (redacted; check before creating)")
    return probs                                 # em dashes are not a problem: clean() replaces them
