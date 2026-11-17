"""The §13 reply style guide, wired as the hub's message layer (plan v2 §13; baton "Reply templates").

Three deliberate client-facing message kinds, bilingual EN/AR, signed "Acme Support":
  - Template 1  : instant acknowledgement (the ONLY automatic client message)
  - Template 2  : confirm understanding (owner-approved before sending)
  - Template 2b : confirm understanding WITH open questions (owner-approved)
  - Template 3  : resolution (owner-approved)
  - Template 3b : resolution WITH a short proof video (owner-approved)

The Arabic text is copied verbatim from plan v2 §13 (do not paraphrase). Rendering only fills {placeholders};
it never translates. The reply language follows the sender (``model.detect_lang``).
"""
from __future__ import annotations

import re
import unicodedata

from . import persona
from .slots import slot_or

SIGN = {"en": persona.sign("en"), "ar": persona._DOC["sign"]["ar"]}   # the "ar" sign is only ever used when enabled

# Arabic-Indic digits for the AR numbered open-questions list.
_AR_DIGITS = {"0": "٠", "1": "١", "2": "٢", "3": "٣", "4": "٤", "5": "٥", "6": "٦", "7": "٧", "8": "٨", "9": "٩"}


def _ar_num(n: int) -> str:
    return "".join(_AR_DIGITS.get(c, c) for c in str(n))


def _lang(lang: str) -> str:
    """The language the message is WRITTEN in: the client's, if the persona enables it (default English only)."""
    return persona.reply_lang(lang)


# TODO(Riley/Supervisor): review these Arabic labels (module names + the ack "next step" phrases are new wording, so the
# Arabic messages contain no English fragments; the template sentences themselves are verbatim from plan v2 §13).
_MODULE_AR = {"Inventory": "المخزون", "Sales": "المبيعات", "Purchase": "المشتريات", "Accounting": "المحاسبة",
              "POS": "نقطة البيع", "Manufacturing": "التصنيع", "HR": "الموارد البشرية", "Helpdesk": "الدعم",
              "Website": "الموقع", "CRM": "العملاء المحتملين", "unknown": "غير محدد"}


def module_label(lang: str, module: str) -> str:
    """The module name in the client's language (Arabic messages must not carry English fragments)."""
    return _MODULE_AR.get(str(module), str(module)) if _lang(lang) == "ar" else str(module)


def next_step(lang: str, *, has_questions: bool) -> str:
    """The ack's {next step} phrase, in the client's language."""
    if _lang(lang) == "ar":
        return "نأكد معاك شوية تفاصيل" if has_questions else "نأكد فهمنا للمطلوب ونكمّل"
    return "confirm a few details with you" if has_questions else "confirm our understanding and proceed"


def ack(lang: str, *, name: str, n: int, owner: str = "", next_step: str = "", subject: str = "") -> str:
    """Instant acknowledgement (automatic, no approval): the persona's "confirmation" message. ``owner`` and ``next_step``
    are accepted for old callers and not used (the persona wording names neither)."""
    return persona.text("confirmation", lang, name=_clean_name(name), n=n, subject=subject or request_subject(lang, ""))


def request_subject(lang: str, module: str) -> str:
    """A short, plain subject for the ack ("your Accounting request"): never the client's own words."""
    known = module and str(module) != "unknown"
    return (f"your {module_label(lang, module)} request" if known else "your request") if _lang(lang) == "en" else         (f"طلبك في {module_label(lang, module)}" if known else "طلبك")


# A profile name is client-controlled and is greeted back in AUTOMATIC messages, so ``_clean_name`` is an ALLOWLIST, not a
# blocklist of link shapes: only letters, combining marks, spaces, an apostrophe, a hyphen between letters and a "." in
# an exact title form (Dr.Principal) survive. Every other character becomes a SPACE (never a deletion), so removing a
# character can never join two pieces into a domain, whatever punctuation or script the sender wraps around it.
_BLANKS = {0x3164, 0x115F, 0x1160, 0x2800, 0x034F}          # blank-looking letters that are not "C*" categories
_DOT_LOOKALIKES = {0xA4F8, 0x1427, 0xA78F}                  # LETTERS (Lisu, Canadian syllabics, Latin) that render as "."
_DOT_MARKS = {0x0307, 0x0323, 0x0338, 0x05C4, 0x05C5}       # combining dots
_TITLES = frozenset({"Dr", "Mr", "Mrs", "Ms", "Eng", "Prof", "Sh", "M", "A"})
_DOMAINISH = re.compile(r"[^\W\d_]+\.[^\W\d_]{2,}")


def _is_letter(ch: str) -> bool:
    return ch.isalpha() and ord(ch) not in _DOT_LOOKALIKES


def _is_mark(ch: str, s: str, i: int) -> bool:
    """A combining mark is kept only after a NON-Latin letter (Arabic harakat, Indic vowel signs). After a Latin letter
    it is dropped to a space: that is where a combining dot would fake a domain."""
    if unicodedata.category(ch) != "Mn" or ord(ch) in _DOT_MARKS:
        return False
    j = i - 1
    while j >= 0 and unicodedata.category(s[j]) == "Mn":          # several marks in a row: look at the base letter
        j -= 1
    return j >= 0 and s[j].isalpha() and ord(s[j]) > 0x24F


def _title_dot(s: str, i: int) -> bool:
    """s[i] is "." in the exact title form: a title word at the start of a word, then a letter."""
    j = i
    while j > 0 and s[j - 1].isalpha():
        j -= 1
    return (s[j:i] in _TITLES and (j == 0 or s[j - 1] == " ") and i + 1 < len(s) and _is_letter(s[i + 1]))


def _clean_name(name: str) -> str:
    """NFKC, no control/format/invisible characters, cut to 40, then the allowlist above. Used by EVERY template that
    greets by name. The title dot is rendered ". " (Dr. Principal), so a "." is NEVER followed by a letter in the output.
    No letters left, or anything domain-like left in the ACTUAL output -> "there"."""
    s = unicodedata.normalize("NFKC", str(name or "")).replace("\u3002", ".").replace("\uff61", ".")
    s = re.sub(r"\s+", " ", s)
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("C") and ord(ch) not in _BLANKS)[:40]
    out = []
    for i, ch in enumerate(s):
        prev, nxt = (s[i - 1] if i else ""), (s[i + 1] if i + 1 < len(s) else "")
        if ch == "." and _title_dot(s, i):
            out.append(". ")
        elif (_is_letter(ch) or _is_mark(ch, s, i) or ch == " " or ch in "'\u2019"
              or (ch == "-" and _is_letter(prev) and _is_letter(nxt))):
            out.append(ch)
        else:
            out.append(" ")
    res = re.sub(r" +", " ", "".join(out)).strip()[:40].strip()
    if not any(ch.isalpha() for ch in res) or _DOMAINISH.search(res):
        return "there"
    return res


def fallback_ack(lang: str, *, name: str) -> str:
    """Fallback acknowledgement (automatic, NO ticket number): sent when the normal ack is late (``fallback_ack_seconds``).
    Exclusive with the normal ack: at most one automatic message per ticket."""
    return persona.text("fallback_ack", lang, name=_clean_name(name))


def confirm(lang: str, *, name: str, issue: str, module: str, outcome: str = "",
            questions: list[str] | None = None, n: int | None = None) -> str:
    """The confirm-understanding draft: ONE line restating the issue (a paraphrase, never the client's words) and ONE
    question. With open questions, only the first is asked (one question per message). ``outcome`` and ``n`` are accepted
    for old callers and not used."""
    reply = _lang(lang)
    issue = slot_or(issue, reply, 0, 140)
    if reply != "ar" and str(lang).lower().startswith("ar"):      # the client writes Arabic, the reply is English only
        issue = persona.phrase("issue_fallback", reply)            # never the client's words
    known = bool(module) and str(module) != "unknown"
    place = (f" في {module_label(lang, module)}" if reply == "ar" else f" on {module}") if known else ""
    who = _clean_name(name)
    if n:                                             # the fallback ack carries no number, so the CONFIRM does
        who = f"{who} ({'تذكرة' if _lang(lang) == 'ar' else 'ticket'} #{n})"
    qs = [_question_for(q, reply) for q in (questions or []) if str(q).strip()]
    qs = [q for q in qs if q]
    if qs:
        return persona.text("confirm_need", lang, who=who, issue=issue, place=place, need=qs[0])
    return persona.text("confirm", lang, who=who, issue=issue, place=place)


_ARABIC_CHARS = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def _question_for(q: str, reply: str) -> str:
    """An open question in the REPLY language: an Arabic one is mapped to its English twin, or dropped when it cannot be
    (no client words, no Arabic script in an English message)."""
    from .analyse import question_en
    q = str(q).strip().rstrip(".?؟")
    if reply == "ar":
        return q
    q = question_en(str(q)).strip().rstrip(".?؟")
    return "" if _ARABIC_CHARS.search(q) else q


def resolved_what(lang: str, module: str) -> str:
    """What was resolved, in plain words ("the Accounting issue"): never a paste of the client's message."""
    known = module and str(module) != "unknown"
    if _lang(lang) == "ar":
        return f"مشكلة {module_label(lang, module)}" if known else "المشكلة"
    return f"the {module} issue" if known else "the issue"


def _resolve_core(lang: str, *, name: str, n: int, what: str, link: str) -> str:
    return persona.text("resolve", lang, who=name, n=n, what=what, link=link)


def resolve(lang: str, *, name: str, n: int, what: str, link: str,
            video: str | None = None, verify_step: str = "") -> str:
    """Resolution (no video) or resolution with a short proof clip. Owner-approved before sending."""
    core = _resolve_core(lang, name=_clean_name(name), n=n, what=what, link=link)
    if not video:
        return f"{core} {persona.sign(lang)}"
    step = verify_step or persona.text("verify_step", lang)
    return f"{core} {persona.text('resolve_video', lang, video=video, step=step)} {persona.sign(lang)}"
