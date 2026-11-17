"""The Odoo ticket the team reads: an excellent TITLE and DESCRIPTION for a hub-created helpdesk ticket.

Title      ``<Client> · <Module> · <problem in a few words>`` (plain English; fallbacks "Client #<id>" / "Module not
           identified" / a phrase built from the classification, never raw Arabic cut into the title).
Description, in this order, every section ALWAYS present (so a reader can scan one shape):
    What the client said (English) · What the client expects · Steps to reproduce · Attachments and evidence ·
    Type · Urgency · Suggested owner · Open questions · "Follows up #N" (only when linked) · the ORIGINAL message in a
    fold-out (a plain heading above it, so it still reads if Odoo's sanitizer strips details/summary/dir).

Where the words come from:
  - A ``TicketWriter`` (an LLM: translation + a short problem phrase + expectation + steps) when one is injected AND it
    answers. A blocked or failing writer NEVER stops intake: the deterministic path is used and the English section
    says plainly that no translation is available yet (it never shows the original under an "English" label).
  - The deterministic path: ``analyse.py`` output + the message itself. An English message is shown as written; an
    Arabic one is NOT translated (no model in CI) and the section says so.
  - Nothing is invented: no steps unless the client numbered them; no attachments unless one arrived; urgency is
    "not stated" unless the client said so; module "not identified" when unknown.

Reuse: ``helpdesk.style.clean`` (redact secrets / URL tokens, no em dashes or Markdown) and ``html.escape`` on EVERY
client string. The Odoo-facing title and description are NEVER passed to the team bus (Teamhub / Telegram briefs are
built from structured fields only, see ``team_posts``): they carry the client's name and words.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from ..helpdesk.style import clean
from ..redact import strip_url_token
from .model import Analysis, Inbound

SECTION_ORDER = ("What the client said (English)", "What the client expects", "Steps to reproduce",
                 "Attachments and evidence", "Type", "Urgency", "Suggested owner", "Open questions")
NOT_TRANSLATED = "Not translated yet: no translation service is connected. Please read the original message below."
STEPS_NONE = "Not given yet"
OWNER_PENDING = "Set when the ticket is assigned."
_EXPECT = {"Bug": "The problem is fixed so it works as expected.",
           "New Request": "The requested capability is added.",
           "Customization": "The requested change is made to the existing behaviour.",
           "Configuration": "The requested setting or setup is done.",
           "Inquiries": "A clear answer to the question."}
_PROBLEM = {"Bug": "Issue reported", "New Request": "New request", "Customization": "Customization request",
            "Configuration": "Configuration request", "Inquiries": "Question"}
_AR = re.compile("[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")
_URL = re.compile(r"https?://[^\s<>\"']+")
_STEP = re.compile(r"^\s*(?:\d+[.)]|[-*]\s*step\s*\d+)\s+\S", re.IGNORECASE)
MAX_TITLE, MAX_PART = 120, 50


@dataclass
class Content:
    """The words for the ticket. ``english`` None = no translation available (see ``note``)."""

    english: str | None = None
    problem: str = ""
    expect: str = ""
    steps: str = ""
    note: str = ""                    # why the writer's words are missing (shown in the English section)


def _line(text: str, limit: int) -> str:
    t = re.sub(r"\s+", " ", clean(text)).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _words(text: str, n: int) -> str:
    return " ".join(re.sub(r"\s+", " ", clean(text)).split()[:n])


def _client_label(client: str, partner_id: int) -> str:
    c = _line(client or "", MAX_PART)
    if not c or c.lower().startswith("partner:") or c == "UNKNOWN":
        return f"Client #{partner_id}" if partner_id else "Unknown client"
    return c


def build_title(analysis: Analysis, client: str, partner_id: int, content: Content, lang: str) -> str:
    """``<Client> · <Module> · <problem>``. The problem phrase is the writer's, else (English text) the first few
    words, else a phrase from the classification: raw Arabic is never cut into the title."""
    module = analysis.module if analysis.module and analysis.module != "unknown" else "Module not identified"
    problem = _words(content.problem, 8) if content.problem else ""
    if not problem and content.english and lang == "en":
        problem = _words(re.split(r"[.!?\n]", content.english.strip())[0], 8)
    problem = _line(problem or _PROBLEM.get(analysis.ticket_type, "Request"), MAX_PART + 10)
    return _line(f"{_client_label(client, partner_id)} · {module} · {problem}", MAX_TITLE)


def deterministic(analysis: Analysis, inbound: Inbound, note: str = "") -> Content:
    """No model: an English message is shown as written; anything else is honestly marked not translated."""
    text = (inbound.text or "").strip()
    english = _line(text, 1500) if (text and inbound.lang == "en") else None
    steps = "\n".join(ln.strip() for ln in text.splitlines() if _STEP.match(ln))
    return Content(english=english, steps=steps, note=note or ("" if english else NOT_TRANSLATED))


def from_writer(res: dict, base: Content) -> Content:
    """The writer's fields over the deterministic base; a field the writer left empty keeps the base value."""
    problem = _line(res.get("problem") or "", MAX_PART + 10)
    if _AR.search(problem):                                   # the title is plain English: use the classification phrase
        problem = ""
    return Content(english=_line(res["english"], 1500) if res.get("english") else base.english,
                   problem=problem, expect=_line(res.get("expect") or "", 300),
                   steps=clean(res.get("steps") or "") or base.steps, note="")


def _lang_label(text: str) -> tuple[str, bool]:
    ar = sum(1 for c in text if "؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    if ar and latin >= 0.3 * ar:
        return "mixed Arabic and English", True
    return ("Arabic", True) if ar else ("English", False)


def _attachments(inbound: Inbound) -> list[str]:
    out: list[str] = []
    if inbound.kind == "voice":
        out.append("Voice note (transcribed automatically" + ("; the transcript is from a placeholder clip"
                   if inbound.transcript_placeholder else "") + ")")
    if inbound.kind == "media":
        out.append(f"{inbound.media_type or 'File'} received on WhatsApp (not copied into Odoo yet)")
    for u in dict.fromkeys(_URL.findall(inbound.text or "")):
        out.append("Link: " + strip_url_token(u.rstrip(".,)")))
    return out


def _p(label: str, body: str) -> str:
    return f"<p><b>{html.escape(label)}</b></p>{body}"


def _para(text: str) -> str:
    return "<p>" + "<br/>".join(html.escape(ln) for ln in str(text).splitlines() if ln.strip()) + "</p>"


def _ul(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>"


def _original(inbound: Inbound) -> str:
    text = inbound.text or ""
    label, rtl = _lang_label(text)
    body = "<br/>".join(html.escape(ln) for ln in text.splitlines()) or html.escape(text)
    head = f"Original message ({label})"
    div = f'<div dir="rtl">{body}</div>' if rtl else f"<div>{body}</div>"
    return _p(head, f"<details><summary>{html.escape(head)}</summary>{div}</details>")


def build_description(analysis: Analysis, inbound: Inbound, content: Content, *, previous: int = 0,
                      suggested: str = "") -> str:
    from .analyse import question_en
    english = content.english if content.english else (content.note or NOT_TRANSLATED)
    expect = content.expect or _EXPECT.get(analysis.ticket_type, "The request is handled.")
    qs = [question_en(q) for q in analysis.open_questions]
    parts = [
        _p(SECTION_ORDER[0], _para(english)),
        _p(SECTION_ORDER[1], _para(expect)),
        _p(SECTION_ORDER[2], _ul(content.steps.splitlines()) if content.steps.strip() else _para(STEPS_NONE)),
        _p(SECTION_ORDER[3], _ul(_attachments(inbound)) if _attachments(inbound) else _para("None received")),
        _p(SECTION_ORDER[4], _para(f"{analysis.ticket_type} · track {analysis.track} · module "
                                   f"{analysis.module if analysis.module != 'unknown' else 'not identified'} "
                                   f"(AI confidence {analysis.confidence})")),
        _p(SECTION_ORDER[5], _para(analysis.urgency or "not stated")),
        _p(SECTION_ORDER[6], _para(_line(suggested, 300) if suggested else OWNER_PENDING)),
        _p(SECTION_ORDER[7], _ul(qs) if qs else _para("None")),
    ]
    if previous:
        parts.append(f"<p>{html.escape(f'Follows up #{previous}')}</p>")
    parts.append(_original(inbound))
    return "".join(parts)


def compose(analysis: Analysis, inbound: Inbound, *, client: str, partner_id: int, writer=None,
            previous: int = 0, suggested: str = "") -> tuple[str, str]:
    """(title, description_html). A missing/blocked/failing writer degrades to the deterministic words."""
    base = deterministic(analysis, inbound)
    content = base
    if writer is not None:
        content = _ask_writer(writer, analysis, inbound, client, base)
    return (build_title(analysis, client, partner_id, content, inbound.lang),
            build_description(analysis, inbound, content, previous=previous, suggested=suggested))


def _ask_writer(writer, analysis: Analysis, inbound: Inbound, client: str, base: Content) -> Content:
    try:
        res = writer.write(inbound.text or "", lang=inbound.lang, analysis=analysis, client=client,
                           names=[inbound.name] if inbound.name else None)
    except Exception as e:                                   # noqa: BLE001 - intake must never stop on the writer
        return deterministic(analysis, inbound, note=f"Not translated: the translation service failed ({type(e).__name__}).")
    if not isinstance(res, dict) or res.get("status") != "ok":
        why = (res or {}).get("why") if isinstance(res, dict) else None
        return deterministic(analysis, inbound, note=f"Not translated: {_line(str(why or 'translation unavailable'), 160)}.")
    return from_writer(res, base)
