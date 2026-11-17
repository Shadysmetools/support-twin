"""Wording lint for client-facing messages (SUPPORT-PERSONA-2026-11-17). Available here; wired into the send path by the autopilot PR #85.

``lint(text, *, client_text, lang) -> list[str]`` returns the reasons the message must NOT be sent as it is (an empty list =
clean). A failure goes to a human (a ``wording_lint`` hold with the reasons, never the text). The same function is used for
the drafts the hub writes, the owner's edits, the automatic ack and (later) autopilot.

Rules: no internal names; no template labels or placeholders; no verbatim paste of the client's text (8+ words in a row, or
60%+ of the words); length within bounds; at most ONE question; the language must be one the persona enables
(``client_languages``, English only for now: an Arabic client still gets an English reply).
"""
from __future__ import annotations

import re

from . import persona
from .ticket_text import SECTION_ORDER

MIN_CHARS, MAX_CHARS = 15, 700
PASTE_RUN, PASTE_RATIO, PASTE_MIN_WORDS = 8, 0.6, 8

_INTERNAL = re.compile(r"\b(?:claude|hub|teamhub|automation|automated|agent|helpdesk\.ticket|"
                       r"awaiting_(?:owner|customer)\w*|in_progress|resolved_stage|working stage)\b", re.IGNORECASE)
_LABELS = [re.compile(re.escape(x) + r"\s*[:–-]", re.IGNORECASE) for x in SECTION_ORDER] + [
    re.compile(r"\{\{?\s*\w+\s*\}?\}"), re.compile(r"\[\["), re.compile(r"\bOriginal message\b", re.IGNORECASE),
    re.compile(r"\bTemplate \d\w?\b", re.IGNORECASE), re.compile(r"(?m)^\s*(?:TEST|SUBJECT|RE|FWD?)\s*:", re.IGNORECASE),
    re.compile(r"\b(?:What the client|Steps to reproduce)\b", re.IGNORECASE), re.compile(r"\bNone\b|\bnull\b|\bundefined\b")]
_ENGLISH_ONE = frozenset({"of", "more", "moment", "thing", "last", "day", "time", "by", "way", "step", "at", "click", "minute",
                          "second", "other", "another", "or", "to", "can", "could", "that", "who", "which"})
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-z]")
_WORDS = re.compile(r"\w+", re.UNICODE)
_QUESTION = re.compile(r"[?؟]")


def written_language(text: str) -> str:
    """"ar" when the message is mostly Arabic letters (an Arabic NAME inside an English message does not count)."""
    return "ar" if len(_ARABIC.findall(text)) > len(_LATIN.findall(text)) else "en"


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORDS.findall(text or "")]


def _internal_names(text: str) -> bool:
    if _INTERNAL.search(text):
        return True
    for m in re.finditer(r"\bOne\b", text):               # "One" the product, not the word: skip a sentence start
        before = text[:m.start()].rstrip()
        if before and before[-1] not in ".!?؟\n" and not (before.endswith("—") or before.endswith("-")):
            nxt = (_WORDS.findall(text[m.end():]) or [""])[0].lower()
            if nxt not in _ENGLISH_ONE:                    # "One of", "One moment" are English; "One has", "One says" are not
                return True
    return False


def _pasted(text: str, client_text: str) -> bool:
    mine, theirs = _words(text), _words(client_text)
    if len(mine) < PASTE_RUN or not theirs:
        return False
    runs = {tuple(theirs[i:i + PASTE_RUN]) for i in range(len(theirs) - PASTE_RUN + 1)}
    if any(tuple(mine[i:i + PASTE_RUN]) in runs for i in range(len(mine) - PASTE_RUN + 1)):
        return True
    shared = set(theirs)
    return len(mine) >= PASTE_MIN_WORDS and sum(w in shared for w in mine) / len(mine) >= PASTE_RATIO


def lint(text: str, *, client_text: str = "", lang: str = "en") -> list[str]:
    """Reasons this message must not go out as written ([] = clean). Reasons are fixed phrases: they never quote the text."""
    text = str(text or "")
    out: list[str] = []
    if _internal_names(text):
        out.append("internal_name")
    if any(rx.search(text) for rx in _LABELS):
        out.append("template_label")
    if _pasted(text, client_text):
        out.append("client_text_pasted")
    n = len(text.strip())
    if n < MIN_CHARS:
        out.append("too_short")
    elif n > MAX_CHARS:
        out.append("too_long")
    if len(_QUESTION.findall(text)) > 1:
        out.append("more_than_one_question")
    allowed = persona.client_languages()
    written = written_language(text)
    if written not in allowed or (lang in allowed and written != lang):
        out.append("language_mismatch")
    return out
