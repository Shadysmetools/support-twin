"""The voice card and Principal's voice taps (FLOW calls these; sending still goes through sender.py).

Card: the exact text + the path of a playable preview file, buttons
  [Send as voice `as`]  [Send without voice `nv`] [Re-record `rr`]  [Edit `ed`] [Take over `to`].
- `as` sends THIS card's recording only: the recording is bound to the card (card id, else card time), so an old
  card tapped after a Re-record never sends audio Principal did not hear. The sender re-hashes the file (voice_binding).
- `nv` sends the same text without audio (approval trigger, same draft hash).
- `rr` makes a fresh take (cache skipped, counts against the budget) and posts a new card.
The current recording per request is state.db cursor `voice_rec:<REQ>` (JSON: path, sha256, text_sha256, lang,
made_at, card_id). No client text is stored there.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from typing import Awaitable, Callable

from ..models import Button, Card, CardKind, OutboundMessage, parse_ts, utc_iso
from ..redact import redact
from ..sender_approval import text_hash
from .policy import detect_lang, voice_cfg, voice_consent_ok, voice_text_problems
from .tts import CharBudget, ElevenLabsTTS, VoiceError, VoiceMaker, VoiceNote

REC_PREFIX = "voice_rec:"


def voice_card(request_id: str, text: str, note: VoiceNote, *, caption: str) -> Card:
    body = (f"{request_id}: voice reply ready ({note.lang}, {caption}).\n\n"
            f"Text:\n{redact(text)[:1500]}\n\n"
            f"Preview (play this file):\n{note.path}")
    buttons = [[Button("Send as voice", "as")], [Button("Send without voice", "nv"), Button("Re-record", "rr")],
               [Button("Edit", "ed"), Button("Take over", "to")]]
    return Card(kind=CardKind.new_request, text=body, request_id=request_id, buttons=buttons,
                attachments=[str(note.path)])


class VoiceDesk:
    def __init__(self, db, cfg: dict, clock, maker: VoiceMaker,
                 post_card: Callable[[Card, str], Awaitable[object]], info: Callable[[str, str], None]):
        """post_card(card, draft_hash); info(request_id, text) posts a plain info card."""
        self.db, self.cfg, self.clock, self.maker = db, cfg, clock, maker
        self.post_card, self.info = post_card, info

    def current(self, rid: str) -> dict | None:
        raw = self.db.get_cursor(REC_PREFIX + rid)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    async def offer(self, rid: str, text: str, *, kind, lang: str | None = None, fresh: bool = False) -> VoiceNote | None:
        """Make a voice note for this draft and post the voice card. None = no voice (text card stands)."""
        if not voice_consent_ok(self.cfg) or not text:
            return None
        lang = lang or detect_lang(text)
        problems = voice_text_problems(text, kind=kind, lang=lang, cfg=self.cfg)
        if problems:
            self.db.log_event("voice_skipped", rid, actor="voice", reasons=problems)
            return None
        try:
            note = await asyncio.to_thread(self.maker.make, text, lang=lang, request_id=rid, fresh=fresh)
        except VoiceError as e:
            self.db.log_event("voice_failed", rid, actor="voice", why=str(e))
            self.info(rid, f"{rid}: the voice note could not be made ({e}). The text reply card stands.")
            return None
        rec = {"path": str(note.path), "sha256": note.sha256, "text_sha256": note.text_sha256, "lang": note.lang,
               "made_at": utc_iso(self.clock.now()), "card_id": None}
        self.db.set_cursor(REC_PREFIX + rid, json.dumps(rec))
        card = voice_card(rid, text, note, caption=str(voice_cfg(self.cfg)["caption"]))
        await self.post_card(card, text_hash(text))
        if card.card_id is not None:                    # sent now (a held card gets its id later: time is the fallback)
            rec["card_id"] = card.card_id
            self.db.set_cursor(REC_PREFIX + rid, json.dumps(rec))
        self.db.log_event("voice_card", rid, actor="voice", sha256=note.sha256, cached=note.cached, chars=note.chars)
        return note

    def attach(self, msg: OutboundMessage, approval: dict | None) -> tuple[OutboundMessage | None, str]:
        """For an `as` tap. No recording -> the message is unchanged (text, as before). A recording that does not
        belong to the tapped card or to this exact text -> (None, why): nothing is sent, never silently text."""
        rec = self.current(msg.request_id)
        if rec is None:
            return msg, ""
        if rec.get("text_sha256") != text_hash(msg.text):
            return None, "the draft changed after the voice note was made"
        card_id = (approval or {}).get("card_id")
        if rec.get("card_id") is not None and card_id is not None:
            if int(card_id) != int(rec["card_id"]):
                return None, "this card is older than the latest recording (Re-record made a new card)"
        else:
            issued, made = parse_ts((approval or {}).get("issued_at")), parse_ts(rec.get("made_at"))
            if issued is None or made is None or issued < made:
                return None, "this card is older than the latest recording"
        msg.voice_path, msg.voice_sha256, msg.voice_lang = rec["path"], rec["sha256"], rec.get("lang")
        return msg, ""


def build_voice_desk(cfg: dict, db, clock, post_card, info, *, has_secret, get_fields=None,
                     which=shutil.which) -> tuple[VoiceDesk | None, str]:
    """The listener's voice desk, or (None, why). Live + consent + vault + ffmpeg, or nothing at all."""
    if cfg.get("run_mode") != "live" or not voice_consent_ok(cfg):
        return None, "voice off (needs live, consent D1 + D2 and voice.enabled)"
    if which(str(voice_cfg(cfg)["ffmpeg"])) is None:
        return None, "ffmpeg not found"
    try:
        tts = ElevenLabsTTS(cfg, has_secret=has_secret, get_fields=get_fields)
    except Exception as e:  # noqa: BLE001 - a missing login never crashes the listener
        return None, f"voice adapter refused: {type(e).__name__}"
    maker = VoiceMaker(tts, cfg, budget=CharBudget(db, cfg, clock))
    return VoiceDesk(db, cfg, clock, maker, post_card, info), ""
