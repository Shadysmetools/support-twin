"""Clean words for the slots of a client-facing draft (Template 2 "confirm understanding") and the ticket summary.

A slot must hold ONE plain phrase about the problem, never the ticket description's section labels ("What the client
said (English)", "What the client expects", ...), and never a cut word followed by an ellipsis. ``client_message`` finds
the client's own words inside a hub ticket description; ``request_summary`` is the label-free request text for the
owner's brief; ``plain_slot`` picks the first sentence that says something (greetings and thanks are skipped) and ends
on a whole word. Arabic works the same way (sentence marks include the Arabic question mark; no letter-case rules).
"""
from __future__ import annotations

import re

from . import persona
from .ticket_text import NOT_TRANSLATED, SECTION_ORDER

_SAID = SECTION_ORDER[0]
_ORIGINAL = r"Original message(?: \([^)]*\))?"
_LABEL = re.compile("(" + "|".join([re.escape(x) for x in SECTION_ORDER] + [_ORIGINAL]) + r")\s*[:–-]?\s*")
_MAIN = re.compile(r"What the client (?:said|expects)")
_BREAK = re.compile(r"(?<=[.!?؟])\s+|(?<=…)\s*|\s*\n+\s*")
_LIST_MARK = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*•])\s+")
_ABBREV = frozenset({"v", "vs", "no", "nr", "e.g", "i.e", "inv", "ref", "qty", "approx", "dr", "mr", "mrs", "ms", "eng",
                     "st", "tel", "ver"})
_AR_HELLO = (r"(?:[اأإآ]هلاا?ً?(?: و?سهلاا?ً?)?(?: بيكم| بيك| بكم)?|(?:ال)?سلام عليكم(?: و?رحمة الله(?: و?بركاته)?)?"
             r"|مرحبا{0,2}ً?|صباح (?:الخير|النور)|مساء (?:الخير|النور)|[اإ]زي(?:ك|كم)|هاي|يا هلا"
             r"|يا (?:جماعة|فندم|باشمهندس|دكتور|[اأ]ستاذ|هندسة|ريس|شباب)(?: الخير)?|تحياتي)")
_GREETING = re.compile(
    r"^\s*(?:(?:hi|hello|hey|dear\s+\S+|good\s+(?:morning|afternoon|evening))\b(?:\s+(?:support|team|all|there|everyone|sir|madam))*[\s,!.:،-]*"
    rf"|{_AR_HELLO}[\s،,!.:\-…]*)+", re.IGNORECASE)
_THANKS = re.compile(
    r"^(?:(?:many |thanks |thank you )?thanks?(?: you)?(?: (?:a lot|so much|in advance|again))?|thank you|please|pls|regards|"
    r"best regards|kind regards|شكر[اًا]{0,2}(?: جزيلا| ليكم| لكم)?|متشكر(?:ين)?|تسلم(?:و|وا)?(?: ايدك| ايديك| ايديكم)?|"
    r"جزاكم الله خير[اًا]?|مع الشكر|لو سمحت|من فضلك(?:م)?)[\s،,!.:\-…]*$", re.IGNORECASE)
_DANGLING = frozenset({"and", "or", "but", "to", "of", "the", "a", "an", "in", "on", "at", "for", "with", "that", "is",
                       "و", "في", "من", "على", "إلى", "عن"})
_TAIL = " \t\r\n.,;:!?،؛؟…-–"
_FALLBACK = {"en": ("a problem", "it to work as expected"),
             "ar": ("مشكلة", "الموضوع يشتغل زي ما هو متوقع")}


def is_hub_description(text: str) -> bool:
    return bool(_MAIN.search(str(text or "")))


def _sections(text: str) -> list[tuple[str, str]]:
    """[(label, body)] in order; the label is the section name, the body the words until the next label."""
    marks = list(_LABEL.finditer(text))
    return [(m.group(1), text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip())
            for i, m in enumerate(marks)]


def client_message(text: str) -> str:
    """The client's own words inside a hub ticket description: their original message when it is there, else the
    "What the client said" body. Text that is not a hub description is returned unchanged. "" = nothing usable."""
    text = str(text or "")
    if not is_hub_description(text):
        return text
    secs = _sections(text)
    original = " ".join(b for lab, b in secs if lab.startswith("Original message") and b)
    if original.strip():
        return original
    said = next((b for lab, b in secs if lab == _SAID), "")
    return "" if not said or said.startswith(NOT_TRANSLATED[:20]) else said


def _cut(text: str, limit: int) -> str:
    """At most ``limit`` characters, ending on a whole word (a single long word is kept whole)."""
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    cut = head.rfind(" ") if not head[-1:].isspace() else limit
    return text[:cut] if cut > 0 else text.split(" ")[0]


def _trim_tail(text: str) -> str:
    words = text.strip(_TAIL).split(" ")
    while len(words) > 1 and words[-1].lower() in _DANGLING:
        words.pop()
    return " ".join(words).strip(_TAIL)


def request_summary(text: str, limit: int = 160) -> str:
    """The request as the owner reads it: the client's words with the description labels removed, whitespace collapsed,
    cut on a whole word, no ellipsis. Not shortened to one sentence (the owner needs the whole ask)."""
    return _cut(re.sub(r"\s+", " ", client_message(text)).strip(), limit)


def _is_abbrev(piece: str) -> bool:
    last = piece.rstrip().rsplit(" ", 1)[-1]
    return bool(re.fullmatch(r"\d{1,3}[.)]", last) and piece.strip() == last) or \
        (last.endswith(".") and last[:-1].lower() in _ABBREV)


def _sentences(text: str) -> list[str]:
    """Sentence pieces. "..." and the ellipsis character are sentence breaks (not the end of the text). A list number
    ("1.") or an abbreviation ("v.", "e.g.") does not end a sentence."""
    out: list[str] = []
    carry = ""
    for raw in _BREAK.split(text.replace("...", "…")):
        piece = f"{carry} {raw}".strip() if carry else raw.strip()
        if not piece:
            continue
        carry = piece if _is_abbrev(piece) and not piece.endswith(("…", "!", "?", "؟")) else ""
        if not carry:
            out.append(_LIST_MARK.sub("", piece))
    return out + ([_LIST_MARK.sub("", carry)] if carry else [])


def plain_slot(text: str, limit: int = 140, *, greeting: bool = True) -> str:
    """One short phrase from ``text``: description labels gone, then the FIRST sentence that says something. A greeting
    is dropped from the front of a sentence, and a sentence that is only a greeting or a thanks is skipped. Cut on a
    word boundary at ``limit`` characters; no trailing punctuation, no ellipsis. "" when nothing usable is left."""
    for s in _sentences(client_message(text)):
        s = re.sub(r"\s+", " ", _GREETING.sub("", s) if greeting else s).strip(_TAIL + " ")
        if s and not (greeting and _THANKS.match(s)) and re.search(r"\w", s):
            return _trim_tail(_cut(s, limit))
    return ""


_LABEL_PREFIX = re.compile(r"^\s*(?:\[[^\]]{1,24}\]|(?:test|fw|fwd|re|subject|urgent|help|issue|problem|bug|support|query)"
                           r"\s*[:\-–·|])\s*", re.IGNORECASE)
_ASKING = re.compile(r"^(?:please|pls|kindly|can|could|would|will|how|why|what|when|where|which|who|is|are|do|does|did|"
                     r"need|want|i need|i want|i would like|i'd like|we need|we want|help)\b", re.IGNORECASE)
_PERSON = [(r"\bI'm\b", "you're"), (r"\bI am\b", "you are"), (r"\bI've\b", "you've"), (r"\bI have\b", "you have"),
           (r"\bI'd\b", "you'd"), (r"\bI'll\b", "you'll"), (r"\bI was\b", "you were"), (r"\bI\b", "you"),
           (r"\bwe're\b", "you're"), (r"\bwe've\b", "you've"), (r"\bwe are\b", "you are"), (r"\bwe have\b", "you have"),
           (r"\bwe were\b", "you were"), (r"\bwe\b", "you"), (r"\bmy\b", "your"), (r"\bour\b", "your"),
           (r"\bmine\b", "yours"), (r"\bours\b", "yours"), (r"\bme\b", "you"), (r"\bus\b", "you")]
_PERSON = [(re.compile(p, re.IGNORECASE), r) for p, r in _PERSON]


PLACEHOLDER_SUMMARY = "(voice note / empty)"
_INTRO = re.compile(r"^(?:i am|i'm|my name|this is)\b", re.IGNORECASE)


def _strip_labels(text: str) -> str:
    """Drop leading tags such as "TEST:", "Re:", "[URGENT]" (repeatedly), so what is left starts with the client's words."""
    prev = None
    while prev != text:
        prev, text = text, _LABEL_PREFIX.sub("", text, count=1)
    return text


_VERBS = frozenset({"is", "are", "was", "were", "shows", "show", "showing", "fails", "fail", "failed", "failing", "does", "doesn't",
                    "do", "don't", "did", "didn't", "can't", "cannot", "won't", "wont", "gives", "give", "giving", "crashes",
                    "crash", "keeps", "stopped", "stops", "has", "have", "had", "isn't", "aren't", "not", "gets", "get",
                    "takes", "take", "prints", "print", "opens", "open", "loads", "load", "freezes", "hangs", "throws",
                    "displays", "returns", "calculates", "creates", "sends", "needs", "looks"})
_RUN = 7          # never repeat more than this many of the client's words in a row (the wording lint refuses 8)


def _summary_line(clause: str, limit: int) -> str:
    """One short line: a clause of up to 7 words keeps its sentence ("an issue where you cannot post a bill"); a longer one
    is cut to its subject, the words before the first verb ("an issue with your invoice export to PDF")."""
    words = clause.split()
    if len(words) <= _RUN:
        return _trim_tail(_cut(persona.phrase("issue_prefix", "en") + clause, limit))
    verb = next((i for i, w in enumerate(words[:_RUN + 1]) if i >= 2 and w.lower().strip(",.;:") in _VERBS), 0)
    if verb:
        return _trim_tail(_cut("an issue with " + " ".join(words[:verb]), limit))
    return _trim_tail(_cut(persona.phrase("issue_prefix", "en") + " ".join(words[:_RUN]), limit))


def issue_phrase(text: str, limit: int = 140) -> str:
    """The problem as ONE short English phrase for "you're seeing ___": a paraphrase, never the pasted message. Labels
    and greetings are dropped, first person becomes second person, and the clause is introduced by "an issue where".
    A question or a request (no statement to restate) gives "" so the caller uses its plain fallback."""
    clause = plain_slot(_strip_labels(client_message(text)), limit + 40)
    clause = plain_slot(_strip_labels(clause), limit + 40) if clause else ""
    if not clause or _ASKING.match(clause) or "?" in clause or _INTRO.match(clause):
        return ""
    for rx, repl in _PERSON:
        clause = rx.sub(repl, clause)
    if clause[:1].isupper() and not clause[:2].isupper():
        clause = clause[0].lower() + clause[1:]
    return _summary_line(clause, limit)


def slot_or(text: str, lang: str, which: int, limit: int) -> str:
    """The slot phrase, or a plain fallback (which: 0 = the problem, 1 = the outcome). The English problem slot is a
    paraphrase (``issue_phrase``); the others are the client's own first sentence with labels removed."""
    ar = str(lang).lower().startswith("ar")
    if str(text).strip().startswith(PLACEHOLDER_SUMMARY):           # the analyser's "(voice note / empty)": no client words
        text = ""
    if not ar and re.search(r"[\u0600-\u06FF]", str(text)):         # an English message never carries Arabic client words
        text = ""
    first = issue_phrase(text, limit) if which == 0 and not ar else plain_slot(_strip_labels(client_message(text)), limit)
    return first or (persona.phrase("issue_fallback", "ar" if ar else "en") if which == 0 else _FALLBACK["ar" if ar else "en"][which])
