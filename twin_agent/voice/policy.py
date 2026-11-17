"""Voice-note rules: consent gate, persona rule and the sender-side voice checks (never AI).

Persona rule (Sysadmin 2026-11-02): a voice note only for SHORT replies (holding, "try again now", a quick
explanation). Never for numbers-heavy text (record refs, amounts, dates, steps with numbers), never for
anything sensitive (links, personal data, secrets, money, passwords), never in Franco (Latin-script Arabic:
the TTS cannot say it). The spoken text IS `msg.text`, so every text guard still applies to it.

Gate: `consent.d1_ref` + `consent.d2_voice_ref` + `voice.enabled: true`. Autopilot voice (timeout or the
holding floor) also needs `voice.autopilot: true` (default false): until then only Principal's own tap sends a voice.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..models import Channel, OutKind, SendTrigger
from ..redact import scan

TELEGRAM = frozenset({Channel.telegram_group, Channel.telegram_dm})
AUTOPILOT = frozenset({SendTrigger.timeout, SendTrigger.holding_floor})
# Short reply kinds only. Tickets, emails, approver questions and proof links are never spoken.
VOICE_KINDS = frozenset({OutKind.holding, OutKind.answer, OutKind.after_fix, OutKind.worked_chaser,
                         OutKind.evidence_ask})
VOICE_LANGS = ("en", "ar")
# Not allowed out of shadow mode for lack of consent: like guards.MODE_GUARDS, these mean "not yet", so the
# sender records would_send in observe / dry_run, and blocked when live.
VOICE_MODE_GUARDS = frozenset({"voice_consent"})
DEFAULTS = {"enabled": False, "autopilot": False, "max_chars": 300, "max_words": 55, "max_digits": 4,
            "lanes": ["L1_holding", "L3_t1_answer", "L9_worked_followup"], "langs": list(VOICE_LANGS),
            "caption": "AI voice of Principal's assistant", "model": "eleven_multilingual_v2",
            "vault_name": "elevenlabs", "monthly_char_cap": 20000, "ffmpeg": "ffmpeg"}

_ARABIC = re.compile(r"[؀-ۿ]")
_URL = re.compile(r"(?i)\bhttps?://|\bwww\.|\b[a-z0-9-]+\.(com|io|ai|net|org|sa|eg)\b")
# Franco: digits used as letters inside a word (3arabi, 7aga, ma2fool) or very common Franco words.
_FRANCO = re.compile(r"(?i)\b[a-z]*[a-z][2378][a-z]+\b|\b(lessa|lesa|keda|kda|msh|mesh|3shan|ana|enta|enty|"
                     r"ezay|leh|tamam|ya3ni|delwa2ty|dlw2ty|7ader|keda)\b")
_SENSITIVE = re.compile(
    r"(?i)\b(password|passcode|otp|pin|token|api[ _-]?key|secret|credentials?|price|cost|invoice|"
    r"payment|paid|salary|refund|bank|iban|card number|credit|discount|contract|legal|lawyer|penalty|"
    r"egp|sar|usd)\b|كلمة السر|كلمة المرور|باسورد|سعر|فاتورة|دفع|مرتب|راتب|خصم|بنك|حساب بنكي|"
    r"جنيه|ريال|غرامة")


def voice_cfg(cfg: dict) -> dict:
    """The `voice` config section with defaults filled in."""
    return {**DEFAULTS, **((cfg or {}).get("voice") or {})}


def voice_consent_ok(cfg: dict) -> bool:
    """D1 + D2 (written voice consent) on file AND voice switched on. Nothing voice happens without this."""
    consent = (cfg or {}).get("consent") or {}
    return bool(consent.get("d1_ref")) and bool(consent.get("d2_voice_ref")) and voice_cfg(cfg)["enabled"] is True


def detect_lang(text: str) -> str:
    """'ar' for Arabic script, 'franco' for Latin-script Arabic, else 'en'. Used when the draft has no lang."""
    if _ARABIC.search(text or ""):
        return "ar"
    return "franco" if _FRANCO.search(text or "") else "en"


def voice_text_problems(text: str, *, kind, lang: str | None, cfg: dict) -> list[str]:
    """Why this text may NOT become a voice note ([] = fine). Plain reasons, no client text in them."""
    v, text, out = voice_cfg(cfg), text or "", []
    try:
        if OutKind(kind) not in VOICE_KINDS:
            out.append(f"kind {kind} is never spoken")
    except ValueError:
        out.append("unknown kind")
    lang = lang or detect_lang(text)
    if lang not in v["langs"] or lang not in VOICE_LANGS:
        out.append(f"language {lang} cannot be spoken (en / ar only)")
    elif lang == "en" and detect_lang(text) == "franco":
        out.append("text looks like Franco (Latin-script Arabic): cannot be spoken")
    if not text.strip():
        out.append("empty text")
    if len(text) > int(v["max_chars"]) or len(text.split()) > int(v["max_words"]):
        out.append("too long for a voice note")
    if sum(ch.isdigit() for ch in text) > int(v["max_digits"]):
        out.append("numbers-heavy")
    from ..router import extract_refs           # local import: router pulls in timers and config
    if extract_refs(text):
        out.append("contains record references")
    if _URL.search(text):
        out.append("contains a link")
    if scan(text):
        out.append("contains personal data or a secret")
    if _SENSITIVE.search(text):
        out.append("sensitive topic")
    return out


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def voice_blockers(msg, cfg: dict, trigger, *, lang: str | None = None) -> list[str]:
    """Sender-side checks for a message carrying `voice_path`. [] for plain text messages.

    Names (CONTRACT Changes, voice): voice_consent, voice_channel, voice_autopilot, voice_lane, voice_persona,
    voice_binding. voice_binding = the audio file is missing or is not the exact file that was previewed.
    """
    path = getattr(msg, "voice_path", None)
    if not path:
        return []
    v, out = voice_cfg(cfg), []
    if not voice_consent_ok(cfg):
        out.append("voice_consent")
    if msg.channel not in TELEGRAM:
        out.append("voice_channel")
    if SendTrigger(trigger) in AUTOPILOT and v["autopilot"] is not True:
        out.append("voice_autopilot")
    if str(msg.lane) not in [str(x) for x in v["lanes"]]:
        out.append("voice_lane")
    if voice_text_problems(msg.text, kind=msg.kind, lang=lang or getattr(msg, "voice_lang", None), cfg=cfg):
        out.append("voice_persona")
    want = getattr(msg, "voice_sha256", "") or ""
    try:
        ok = bool(want) and Path(path).is_file() and file_sha256(path) == want
    except OSError:
        ok = False
    if not ok:
        out.append("voice_binding")
    return out
