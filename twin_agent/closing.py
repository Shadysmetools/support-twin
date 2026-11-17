"""Closing rules (05 2.1, 2.2, 9.x "closes when"): requests close on their own, never "assumed fixed". Owner: FLOW.

Three ways a request closes without a tap, each switched in `config close_rules` (see config.example.yaml):
- CLOSED_PROVEN: a proof went out (PROOF_SENT / FIXED_UNCONFIRMED, so the re-read already passed) and the requester
  confirms: "تمام", "تمام كده", "اشتغل", "done", "ok thanks", or a thumbs-up reaction. (`close_on_ack`)
- CLOSED_ANSWERED: Principal answered himself (a human answer, not the agent's holding reply) and the requester acks
  (`close_on_ack`), or, for an informational answer (tier T1), the requester stays silent for 48 working hours
  (`close_answered_silent`, timer `answered_close`).
- CLOSED_REREAD_PROVEN: a quick fix (T2a / T2b) whose re-read passed (VERIFYING -> PROOF_SENT), then 2 "worked?"
  pings that really went out (sends rows `sent`), and no word from the requester. ONLY if
  `close_rules.close_silent_quickfix` is true (default false: FIXED_UNCONFIRMED + the weekly "worked?" check).

"Human answered" is a fact read from the log, not a column: the latest `principal_replied` event (since the latest
reopen) is later than the latest non-ack message from the requester side. While it holds, Principal gets no
reminder, SLA or escalation cards for that request; the weekly re-check stays as the never-dropped net.

Reopen keeps working: every closed state can go to REOPENED within the reopen window (router.py).
Every auto-close posts one silent `done` card, so a wrong close is visible and can be reopened.
Never sends anything to a client.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime

from .bus import Topics
from .clock import UTC, utc
from .models import AUTO_CLOSED, OVERLAY, TERMINAL, Card, CardKind, IllegalTransition, State, TimerKind, parse_ts

S = State
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

DEFAULTS = {
    "close_on_ack": True,
    "close_answered_silent": True,
    "close_silent_quickfix": False,
    "informational_tiers": ["T1"],
    "ack_reactions": ["👍", "👌"],
}
# Where a requester's ack after Principal's own answer may close the request (a fix in flight never closes on "ok").
ANSWER_CLOSABLE = frozenset({S.TRIAGED, S.AWAITING_FIRST_REPLY, S.ACKED, S.DIAGNOSED, S.PROPOSED, S.FAILED_WORKER})
PROOF_STATES = frozenset({S.PROOF_SENT, S.FIXED_UNCONFIRMED})
QUICK_FIX_TIERS = frozenset({"T2a", "T2b"})


def rule(cfg: dict | None, name: str):
    """A close_rules switch. close_silent_quickfix also honours the older key reread_proven_accepted."""
    rules = (cfg or {}).get("close_rules") or {}
    if name == "close_silent_quickfix":
        return bool(rules.get("close_silent_quickfix") or rules.get("reread_proven_accepted"))
    val = rules.get(name)
    return DEFAULTS.get(name) if val is None else val


# ---------------------------------------------------------------- ack text (pure)

_ACK_CORE = {
    # Egyptian Arabic (normalised: alif forms -> ا, ة -> ه, ى -> ي, no tashkeel / tatweel)
    "تمام", "تمم", "تماما", "شكر", "اشتغل", "اشتغلت", "اشتغلو", "شغال", "شغاله", "تمت", "تم", "خلاص", "شكرا", "متشكر",
    "متشكره", "متشكرين", "مرسي", "ميرسي", "ماشي", "حلو", "جميل", "ممتاز", "الحمدلله", "الحمد", "تسلم", "تسلمي",
    "تسلموا", "يسلمو", "يسلموا", "اوك", "اوكي", "مظبوط", "اتحلت", "اتحل", "ظبطت", "الفل",
    # English
    "ok", "okay", "okey", "okie", "k", "kk", "thanks", "thank", "thx", "tnx", "thnx", "ty", "done", "worked", "works",
    "working", "great", "perfect", "good", "fine", "cool", "solved", "resolved", "fixed", "got", "noted",
    # Franco
    "tmam", "tamam", "tamaam", "tmm", "eshta", "ashta", "eshtaghal", "eshta8al", "shta8al", "ishtaghal", "shokran",
    "shukran", "merci", "mersi", "mashy", "mashi", "khalas", "7elw", "helw", "gamed", "gameel", "elfol",
    # emoji
    "👍", "👌", "✅", "🙏", "❤", "💯", "🌹",
}
_ACK_FILLER = {
    "كده", "كدا", "يا", "اليكس", "باشمهندس", "بشمهندس", "باشمهندسه", "بشمهندسه", "الف", "الله", "لله", "جزاك", "خير",
    "حبيبي", "ايدك", "ايديك", "جدا", "اوي", "خالص", "دلوقتي", "معايا", "عندي", "كل", "حاجه", "ده", "دي", "و", "زي",
    "it", "is", "its", "now", "you", "u", "ya", "principal", "eng", "so", "much", "very", "a", "lot", "all", "keda",
    "kda", "kdaa", "w", "el", "it's", "thats", "that's", "that", "for", "your", "help", "with", "me", "zay",
    "dlw2ty", "delwa2ty", "awy", "gedan", "fol", "the", "el7amdolelah", "alhamdulillah",
}
_NEGATION = re.compile(r"(?<!\w)(?:مش|مو|ما|لا|لأ|مفيش|مافيش|بس|لكن|not|no|don'?t|didn'?t|isn'?t|doesn'?t|but|"
                       r"msh|mesh|mafeesh|bas|still|lessa|lsa)(?!\w)", re.IGNORECASE)
_TASHKEEL = re.compile(r"[ً-ٰٟـ]")          # harakat, superscript alif, tatweel
_RUNS = re.compile(r"(.)\1{2,}")                                  # "تماااام" / "okkk" -> one letter
_EMOJI_SPLIT = re.compile(r"([\U0001F300-\U0001FAFF☀-➿])")
_PUNCT = re.compile(r"[.,!،؛;:…\-_~()\[\]\"'«»*]+")
MAX_ACK_WORDS = 8


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("️", "").replace("‍", "")
    t = re.sub(r"[\U0001F3FB-\U0001F3FF]", "", t)                 # skin tones
    t = _TASHKEEL.sub("", t)
    t = re.sub("[أإآٱ]", "ا", t).replace("ة", "ه").replace("ى", "ي")
    t = _RUNS.sub(r"\1", t)
    t = _EMOJI_SPLIT.sub(r" \1 ", t)
    return _PUNCT.sub(" ", t)


def _token_ok(tok: str) -> tuple[bool, bool]:
    """(known, core) for one token. A leading Arabic و ("وشكرا") is allowed on a known word."""
    for cand in (tok, tok[1:] if tok.startswith("و") and len(tok) > 2 else None):
        if cand is None:
            continue
        if cand in _ACK_CORE:
            return True, True
        if cand in _ACK_FILLER:
            return True, False
    return False, False


def is_ack_text(text: str) -> bool:
    """True only for a short message that is nothing but thanks / "it works": "تمام", "تمام كده", "اشتغل", "done",
    "ok thanks", "👍". Reopen words, negations, questions, record refs or any unknown word make it False."""
    if not text or not text.strip() or len(text) > 120:
        return False
    if "?" in text or "؟" in text:
        return False
    from .router import extract_refs, is_reopen_text
    if is_reopen_text(text) or extract_refs(text):
        return False
    t = _norm(text)
    if _NEGATION.search(t):
        return False
    toks = t.split()
    if not toks or len(toks) > MAX_ACK_WORDS:
        return False
    core = False
    for tok in toks:
        known, is_core = _token_ok(tok)
        if not known:
            return False
        core = core or is_core
    return core


def is_ack_reaction(emoji: str, cfg: dict | None = None) -> bool:
    allowed = {_norm(e).strip() for e in (rule(cfg, "ack_reactions") or [])}
    return bool(emoji) and _norm(emoji).strip() in allowed


# ---------------------------------------------------------------- facts from state.db

def _ts(value: str | None) -> datetime | None:
    dt = parse_ts(value)
    return utc(dt) if dt else None


def last_reopen_at(db, rid: str) -> datetime | None:
    out = None
    for e in db.events(rid, "state"):
        d = json.loads(e["detail_json"] or "{}")
        if d.get("dst") == S.REOPENED:
            out = _ts(e["ts"])
    return out


def last_answer_at(db, rid: str) -> datetime | None:
    """When Principal himself last answered this request (message time), since the latest reopen. None if never."""
    since = last_reopen_at(db, rid)
    best = None
    for e in db.events(rid, "principal_replied"):
        d = json.loads(e["detail_json"] or "{}")
        at = _ts(d.get("at")) or _ts(e["ts"])
        if at and (since is None or at >= since) and (best is None or at > best):
            best = at
    return best


def last_requester_at(db, rid: str, *, skip_event: str | None = None) -> datetime | None:
    """Latest message from the requester side (not Principal, not a bot, not a pure ack), as an instant."""
    best = None
    for m in db.thread(rid):
        if m["is_principal"] or m["is_bot"] or m["event_id"] == skip_event or is_ack_text(m["text"] or ""):
            continue
        at = _ts(m["ts"])
        if at and (best is None or at > best):
            best = at
    return best


def human_answered(db, req: dict, *, skip_event: str | None = None) -> bool:
    """Principal answered himself and the requester has not asked anything new since (acks do not count)."""
    ans = last_answer_at(db, req["id"])
    if ans is None:
        return False
    asked = last_requester_at(db, req["id"], skip_event=skip_event)
    return asked is None or ans >= asked


def is_informational(db, req: dict) -> bool:
    """An answer-only request: tier T1 on the request or its latest packet, and no fix / ticket in flight."""
    if req.get("ticket_id") or req.get("taken_over"):
        return False
    tiers = set(rule(getattr(db, "cfg", None), "informational_tiers") or [])
    tier = req.get("tier")
    row = db.latest_packet(req["id"]) if hasattr(db, "latest_packet") else None
    if row:
        try:
            tier = json.loads(row["packet_json"] or "{}").get("tier") or tier
        except ValueError:
            pass
    return tier in tiers


def annotate(db, req: dict) -> dict:
    """Timer inputs (timers._wanted): is the ball with the requester, and may silence close it?"""
    req = dict(req)
    if not req.get("id"):
        return req
    answered = human_answered(db, req)
    req["_human_answered"] = answered
    cfg = getattr(db, "cfg", None)
    req["_answered_close"] = bool(answered and rule(cfg, "close_answered_silent") and is_informational(db, req))
    return req


def _effective(req: dict) -> State:
    """The state that decides closing: an overlay counts as the state it came from."""
    st = State(req["state"])
    if st in OVERLAY and req.get("prev_state"):
        return State(req["prev_state"])
    return st


# ---------------------------------------------------------------- acting

def _close(db, bus, rid: str, dst: State, actor: str, reason: str) -> bool:
    try:
        db.transition(rid, dst, actor=actor, reason=reason)
    except IllegalTransition:
        return False
    db.cancel_timers(rid, None)
    db.add_kpi(f"closed_{str(dst).lower()}", 1, request_id=rid)
    req = db.get_request(rid) or {}
    if bus is not None:
        bus.publish(Topics.CARD_OUT, Card(
            kind=CardKind.done, request_id=rid, silent=True,
            text=f"{rid} closed: {dst} ({reason}). {req.get('title') or ''}\nWrong? Any 'still' / 'لسه' from the "
                 "requester reopens it within 14 days."[:600]))
    return True


def on_requester_ack(db, cfg: dict, bus, req: dict, *, event_id: str | None, how: str) -> State | None:
    """The requester said "تمام" / "ok thanks" / reacted 👍. Close if a rule allows; returns the closed state."""
    if not rule(cfg, "close_on_ack") or State(req["state"]) in TERMINAL:
        return None
    eff = _effective(req)
    rid = req["id"]
    if eff in PROOF_STATES:
        dst, reason = S.CLOSED_PROVEN, f"requester confirmed after proof ({how})"
    elif eff in ANSWER_CLOSABLE and human_answered(db, req, skip_event=event_id):
        dst, reason = S.CLOSED_ANSWERED, f"Principal answered; requester acked ({how})"
    else:
        db.log_event("ack_no_close", rid, actor="closing", how=how, state=str(req["state"]))
        return None
    return dst if _close(db, bus, rid, dst, "closing", reason) else None


def on_answered_close(flow, rid: str) -> bool:
    """answered_close timer: close CLOSED_ANSWERED only if every condition still holds and 48 working h passed."""
    db = flow.db
    req = db.get_request(rid)
    if not req or State(req["state"]) in TERMINAL or not rule(flow.cfg, "close_answered_silent"):
        return False
    if _effective(req) not in ANSWER_CLOSABLE or not human_answered(db, req) or not is_informational(db, req):
        return False                                   # ensure_timer re-plans (weekly re-check stays)
    due = flow.policy.due_at(TimerKind.answered_close, last_answer_at(db, rid))
    if utc(due) > utc(flow.clock.now()):               # Principal answered again later: wait the full 48 working h
        db.add_timer(rid, TimerKind.answered_close.value, due)
        return False
    return _close(db, flow.bus, rid, S.CLOSED_ANSWERED, "closing",
                  "informational answer; requester silent 48 working h")


def _reread_passed(db, rid: str) -> datetime | None:
    """When the latest VERIFYING -> PROOF_SENT happened (the re-read passed), or None."""
    out = None
    for e in db.events(rid, "state"):
        d = json.loads(e["detail_json"] or "{}")
        if d.get("src") == S.VERIFYING and d.get("dst") == S.PROOF_SENT:
            out = _ts(e["ts"])
    return out


def silent_quickfix_ok(db, req: dict) -> tuple[bool, str]:
    """CLOSED_REREAD_PROVEN preconditions (the flag is checked by the caller and again in db.transition)."""
    rid = req["id"]
    if (req.get("tier") or "") not in QUICK_FIX_TIERS:
        return False, "not a quick fix"
    proof_at = _reread_passed(db, rid)
    if proof_at is None:
        return False, "no passed re-read"
    pings = [s for s in db.sends_since(EPOCH, request_id=rid)
             if s.get("kind") == "worked_chaser" and (_ts(s.get("sent_at")) or proof_at) >= proof_at]
    if len(pings) < 2:
        return False, f"{len(pings)} 'worked?' pings really sent (need 2)"
    for m in db.thread(rid):
        if not m["is_principal"] and not m["is_bot"] and (_ts(m["ts"]) or proof_at) > proof_at:
            return False, "requester wrote after the proof"
    return True, ""


def on_worked_chaser_2(flow, rid: str, req: dict) -> State | None:
    """Second "worked?" chaser is due and the requester stayed silent. Default: FIXED_UNCONFIRMED (weekly check)."""
    if State(req["state"]) != S.PROOF_SENT:
        return None
    if rule(flow.cfg, "close_silent_quickfix"):
        ok, why = silent_quickfix_ok(flow.db, req)
        if ok and _close(flow.db, flow.bus, rid, S.CLOSED_REREAD_PROVEN, "closing",
                         "quick fix: re-read passed, 2 'worked?' pings, requester silent"):
            return S.CLOSED_REREAD_PROVEN
        flow.db.log_event("reread_close_skipped", rid, actor="closing", why=why)
    flow.move(rid, S.FIXED_UNCONFIRMED, "timers", "2 silent chasers")
    return S.FIXED_UNCONFIRMED


__all__ = ["AUTO_CLOSED", "rule", "is_ack_text", "is_ack_reaction", "human_answered", "last_answer_at",
           "is_informational", "annotate", "on_requester_ack", "on_answered_close", "on_worked_chaser_2",
           "silent_quickfix_ok"]


def needs_development(req: dict, packet: dict | None) -> bool:
    """Principal 2026-11-11: a DEVELOPMENT request (needs code / a build: tier T3, or a 'dev' part in the analysis) is
    not finished when he answers; its request and ticket stay open until he closes them. Everything else (an
    inquiry, a one-record fix he explains, a config step) closes when his answer goes out.
    Narrower than drafting.is_development, which also counts a T2a one-record fix (a Bug-type ticket)."""
    packet = packet or {}
    parts = ((packet.get("ticket_draft") or {}).get("parts") or [])
    if any((p or {}).get("type") == "dev" for p in parts):
        return True
    return str(packet.get("tier") or (req or {}).get("tier") or "") == "T3"


def latest_packet(db, rid: str) -> dict:
    row = db.latest_packet(rid)
    try:
        return json.loads(row["packet_json"]) if row else {}
    except (TypeError, ValueError):
        return {}


def mail_is_actionable(req: dict, packet: dict | None) -> bool:
    """Principal 2026-11-11: an email that is a request or an inquiry gets a Functional ticket; an FYI (no reply drafted
    and no tier the twin can work: T4 / UNKNOWN) does not."""
    packet = packet or {}
    if ((packet.get("draft_reply") or {}).get("text") or "").strip():
        return True
    return str(packet.get("tier") or (req or {}).get("tier") or "") in ("T1", "T2a", "T2b", "T3")
