"""Intake v2: the 3 request-type buttons, parsing the client's pick, and a short ticket subject.

Client-facing text is ENGLISH ONLY for now (Sysadmin, 17 Nov): ``CLIENT_LANGUAGES`` picks which languages may go out; the
Arabic strings stay in code and switch on by adding "ar" there. PARSING accepts Arabic replies regardless.
"""
from __future__ import annotations

import re

from .slots import _cut, _strip_labels, _trim_tail, plain_slot

CLIENT_LANGUAGES: tuple[str, ...] = ("en",)
TITLE_MAX = 20
BUTTON_IDS = {"bug": "type_bug", "feature": "type_feature", "question": "type_question"}

_TITLES = {"en": ("Bugs & issues", "Features & requests", "Question"),
           "ar": ("مشكلة أو خطأ", "ميزة أو طلب", "سؤال")}
_BODY = {"en": "Thanks! To route this to the right person, please choose the type of your request:",
         "ar": "شكراً! علشان نوصّل طلبك للشخص المناسب، اختار نوع طلبك:"}
_REASK = {"en": ("Sorry, I did not catch that. Please tap one button, or reply 1 (problem), 2 (feature or request) "
                 "or 3 (question).",
                 "Just reply with a number: 1 = problem, 2 = feature or request, 3 = question."),
          "ar": ("آسفين، مفهمتش ردك. اضغط على زرار، أو ابعت 1 (مشكلة) أو 2 (ميزة أو طلب) أو 3 (سؤال).",
                 "ابعت رقم بس: 1 = مشكلة، 2 = ميزة أو طلب، 3 = سؤال.")}


def _lang(lang: str, client_languages=None) -> str:
    """The language to send in: the client's own only if it is allowed, otherwise English."""
    allowed = tuple(client_languages) if client_languages is not None else CLIENT_LANGUAGES
    code = str(lang or "en").lower()[:2]
    return code if code in allowed and code in _TITLES else "en"


def build_type_buttons(lang: str, client_languages=None) -> dict:
    """WhatsApp Cloud API ``messages`` payload part: ``{"type": "interactive", "interactive": {...}}``."""
    code = _lang(lang, client_languages)
    buttons = [{"type": "reply", "reply": {"id": BUTTON_IDS[k], "title": t}}
               for k, t in zip(("bug", "feature", "question"), _TITLES[code])]
    return {"type": "interactive",
            "interactive": {"type": "button", "body": {"text": _BODY[code]}, "action": {"buttons": buttons}}}


def reask_text(lang: str, attempt: int = 1, client_languages=None) -> str:
    """A simpler re-ask for a lost client (attempt 1 = friendly, 2+ = number-only)."""
    return _REASK[_lang(lang, client_languages)][0 if int(attempt or 1) <= 1 else 1]


_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_WORDS = {
    "bug": {"1", "bug", "bugs", "issue", "issues", "problem", "problems", "error", "bugs & issues", "bugs and issues",
            "مشكلة", "مشكله", "مشاكل", "خطأ", "خطا", "أخطاء", "اخطاء"},
    "feature": {"2", "feature", "features", "request", "requests", "features & requests", "features and requests",
                "ميزة", "ميزه", "مميزة", "طلب", "طلبات"},
    "question": {"3", "question", "questions", "سؤال", "استفسار"},
}


def _from_event(ev: dict) -> str | None:
    inter = ev.get("interactive") if isinstance(ev.get("interactive"), dict) else ev
    reply = inter.get("button_reply") if isinstance(inter, dict) else None
    if not isinstance(reply, dict):
        text = ev.get("text")
        return text.get("body") if isinstance(text, dict) else text
    rid = str(reply.get("id", "")).strip()
    for kind, bid in BUTTON_IDS.items():
        if rid == bid:
            return "id:" + kind
    return str(reply.get("title", ""))


def parse_type_reply(event_or_text) -> str | None:
    """'bug' | 'feature' | 'question' | None. Accepts a button_reply event (by id) or typed text (EN/AR, digits too)."""
    raw = _from_event(event_or_text) if isinstance(event_or_text, dict) else event_or_text
    if not isinstance(raw, str):
        return None
    if raw.startswith("id:"):
        return raw[3:] if raw[3:] in BUTTON_IDS else None
    s = re.sub(r"\s+", " ", raw.translate(_AR_DIGITS)).strip().strip(" .!،,:;-").lower()
    if s in ("?", "؟"):
        return "question"
    s = s.strip("?؟ ")
    for kind, words in _WORDS.items():
        if s in words:
            return kind
    return None


_EMAIL = re.compile(r"\S+@\S+\.\S+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
_SUBJ_WORDS, _SUBJ_CHARS = 10, 80


def short_subject(text: str, analysis=None) -> str:
    """<= 10 words and <= 80 chars, in the client's language, no label prefix, no phone or email. Deterministic: the
    first meaningful clause of the message (falls back to ``analysis.summary``), cut on a word boundary."""
    for source in (text, getattr(analysis, "summary", "") if analysis is not None else ""):
        cleaned = _PHONE.sub(" ", _EMAIL.sub(" ", _strip_labels(str(source or ""))))
        clause = plain_slot(_strip_labels(plain_slot(cleaned, 200) or ""), _SUBJ_CHARS * 2)
        words = clause.split()[:_SUBJ_WORDS]
        out = _trim_tail(_cut(" ".join(words), _SUBJ_CHARS))
        if out:
            return out
    return ""
