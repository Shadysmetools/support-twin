"""Principal's work fingerprint: how he writes, how fast, in which language, what he checks first, what he edits.

Input: case dicts (bundle shape: {request_id, requester, records: [...]}). Output: one plain dict (JSON-safe) that
analyse.py renders to FINGERPRINT.md and the persona addendum. Deterministic, stdlib only.
`render_md(fp, public=True)` is the de-identified view (numbers + generic phrases only) that may go into git docs.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict

ARABIC = re.compile(r"[؀-ۿ]")
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9']*")
FRANCO_DIGIT = re.compile(r"(?i)\b[a-z]*[a-z][2356789][a-z]+\b|\b[2356789][a-z]{2,}\b")
FRANCO_WORDS = frozenset("""ana enta enty inta inti msh mesh mish ezay izay keda kda tamam bas lazem howa heya dlwa2ty
delwa2ty 3shan 3ashan leh emta ya3ni yaany khalas tab yalla yala inshallah 7adrtak hadretak mafeesh mafish 3ayez 3awez
momken ba2a kaman bardo awi gedan ahlan shokran ma3lesh walahy wallahy el fe fi da di dah deh ely elly""".split())
FRANCO_TOKEN = re.compile(r"(?i)^[a-z]*[2356789][a-z]+$")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
WORD = re.compile(r"\w+", re.U)

# Moves: what a message (or tap) does in the diagnosis. First match wins, so the order matters.
MOVES: list[tuple[str, re.Pattern[str]]] = [
    ("escalate", re.compile(r"(?i)\b(ticket|supervisor|escalat\w*|development team|dev team)\b|تيكت")),
    ("decline", re.compile(r"(?i)\b(can'?t|cannot|not possible|msh momken|mesh momken|not supported)\b|مش ممكن")),
    ("ask_evidence", re.compile(r"(?i)\b(screen ?shot|screenshot|send me|share (the|a)|which (record|order|ref)"
                                r"|ab3at|eb3at|ab3atly|reference)\b|صورة|سكرين|ابعت")),
    ("fix_done", re.compile(r"(?i)\b(fixed|done|solved|updated|corrected|resolved|3adelt|zabatt?)\b|تم|اتعدل|اتحل")),
    ("ask_retry", re.compile(r"(?i)\b(try (again|now)|please try|check (again|now)|garrab|jarrab)\b|جرب")),
    ("explain_cause", re.compile(r"(?i)\b(because|the reason|caused|since the|3shan|3ashan)\b|علشان|عشان|السبب")),
    ("checking", re.compile(r"(?i)\b(checking|let me (check|see)|looking|will check|hashoof|ha?shof)\b|هشوف|بشوف")),
]
TAP_MOVES = {"fx": "open_fix_block", "ran": "ran_fix", "am": "escalate", "to": "take_over", "tn": "take_over",
             "ed": "edit_draft", "ap": "approve_draft", "as": "approve_draft", "ck": "close"}


def lang_of(text: str) -> str:
    """'ar' | 'franco' | 'en' | 'mixed' | 'none' (Egyptian Arabic in Latin letters = franco)."""
    ar = len(ARABIC.findall(text or ""))
    words = LATIN_WORD.findall(text or "")
    latin = sum(len(w) for w in words)
    if not ar and not words:
        return "none"
    share = ar / (ar + latin)
    if share > 0.7:
        return "ar"
    if share > 0.2:
        return "mixed"
    franco = sum(1 for w in words if w.lower() in FRANCO_WORDS) + len(FRANCO_DIGIT.findall(text))
    return "franco" if franco and franco / max(1, len(words)) >= 0.15 else "en"


def move_of(text: str) -> str:
    for name, rx in MOVES:
        if rx.search(text or ""):
            return name
    return "reply"


def features(text: str) -> dict:
    words = WORD.findall(text or "")
    return {"words": len(words), "chars": len(text or ""), "lang": lang_of(text), "emoji": bool(EMOJI.search(text or "")),
            "question": "?" in (text or "") or "؟" in (text or ""), "move": move_of(text)}


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def _mix(langs) -> dict:
    c = Counter(langs)
    n = sum(c.values()) or 1
    return {k: round(v / n, 2) for k, v in c.most_common()}


def _ngrams(text: str, n: int) -> list[str]:
    toks = [t.lower() for t in WORD.findall(text or "") if not any(ch.isdigit() for ch in t) or FRANCO_TOKEN.match(t)]
    return [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]


def _moves(case: dict) -> list[str]:
    out = []
    for r in case.get("records") or []:
        k = r.get("kind")
        m = (move_of(r.get("text") or "") if k == "own_message" else TAP_MOVES.get(r.get("action")) if k == "tap"
             else "shell" if k == "shell_paste" else "desk_session" if k == "desk_session" else None)
        if m and (not out or out[-1] != m):
            out.append(m)
    return out


def build_fingerprint(cases: list[dict]) -> dict:
    msgs = [(c, r) for c in cases for r in c.get("records") or [] if r.get("kind") == "own_message"]
    feats = [features(r.get("text") or "") for _, r in msgs]
    by_req: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for (c, r), f in zip(msgs, feats):
        by_req[str(r.get("requester") or c.get("requester") or "requester-unknown")].append((r, f))
    phrases: dict[str, Counter] = {2: Counter(), 3: Counter()}
    phrase_reqs: dict[str, set] = defaultdict(set)
    openers, closers = Counter(), Counter()
    for (c, r) in msgs:
        text, req = r.get("text") or "", str(r.get("requester") or c.get("requester"))
        for n in (2, 3):
            for g in set(_ngrams(text, n)):
                phrases[n][g] += 1
                phrase_reqs[g].add(req)
        toks = [t.lower() for t in WORD.findall(text)]
        if toks:
            openers[" ".join(toks[:2])] += 1
            closers[" ".join(toks[-2:])] += 1
    edits = [r for c in cases for r in c.get("records") or [] if r.get("kind") == "edit"]
    seqs = [s for s in (_moves(c) for c in cases) if s]
    hours = Counter(r.get("hour") for _, r in msgs if r.get("hour") is not None)
    per_req = {}
    for alias, items in sorted(by_req.items(), key=lambda kv: -len(kv[1])):
        fs = [f for _, f in items]
        per_req[alias] = {"messages": len(items), "language_mix": _mix(f["lang"] for f in fs),
                          "median_words": _median([f["words"] for f in fs]),
                          "median_reply_min": _median([r.get("reply_minutes") for r, _ in items]),
                          "emoji_rate": round(sum(f["emoji"] for f in fs) / len(fs), 2),
                          "top_moves": [m for m, _ in Counter(f["move"] for f in fs).most_common(3)]}
    return {
        "version": 2, "cases": len(cases), "messages": len(msgs),
        "overall": {"language_mix": _mix(f["lang"] for f in feats),
                    "median_words": _median([f["words"] for f in feats]), "p90_words": _pct([f["words"] for f in feats], .9),
                    "emoji_rate": round(sum(f["emoji"] for f in feats) / len(feats), 2) if feats else 0,
                    "question_rate": round(sum(f["question"] for f in feats) / len(feats), 2) if feats else 0,
                    "median_reply_min": _median([r.get("reply_minutes") for _, r in msgs]),
                    "p75_reply_min": _pct([r.get("reply_minutes") for _, r in msgs], .75),
                    "hours": {str(h): n for h, n in sorted(hours.items())},
                    "moves": dict(Counter(f["move"] for f in feats).most_common())},
        "diagnosis": {"first_move": dict(Counter(s[0] for s in seqs).most_common()),
                      "top_sequences": [{"moves": list(k), "count": v} for k, v in
                                        Counter(tuple(s[:3]) for s in seqs).most_common(8)],
                      "escalate_rate": round(sum("escalate" in s for s in seqs) / len(seqs), 2) if seqs else 0,
                      "decline_rate": round(sum("decline" in s for s in seqs) / len(seqs), 2) if seqs else 0},
        "edits": {"count": len(edits), "unchanged_rate": round(sum(bool(e.get("unchanged")) for e in edits) / len(edits), 2)
                  if edits else None, "median_similarity": _median([e.get("similarity") for e in edits]),
                  "length_ratio": _median([(e.get("final_words") or 0) / max(1, e.get("draft_words") or 0) for e in edits]),
                  "words_added": [w for w, _ in Counter(w.lower() for e in edits for w in e.get("added") or []).most_common(15)],
                  "words_removed": [w for w, _ in Counter(w.lower() for e in edits for w in e.get("removed") or []).most_common(15)]},
        "phrase_bank": [{"phrase": g, "count": n, "requesters": len(phrase_reqs[g])}
                        for g, n in (phrases[3] + phrases[2]).most_common(60) if n >= 2],
        "openers": [{"phrase": p, "count": n} for p, n in openers.most_common(10)],
        "closers": [{"phrase": p, "count": n} for p, n in closers.most_common(10)],
        "per_requester": per_req,
    }


def generic(phrase: str, blocked: set[str], min_requesters: int, item: dict) -> bool:
    """Safe for git: seen across enough requesters, no digits, no alias or redaction marker, no known name part."""
    toks = phrase.lower().split()
    return (item.get("requesters", min_requesters) >= min_requesters and item.get("count", 0) >= 3
            and not any(ch.isdigit() for ch in phrase) and not any(t in blocked for t in toks)
            and not re.search(r"redacted|person|requester", phrase))
