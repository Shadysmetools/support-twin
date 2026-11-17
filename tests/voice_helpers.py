"""Shared helpers for the voice tests (synthetic data only)."""
from __future__ import annotations

import copy

from twin_agent.voice.tts import CharBudget, VoiceMaker
from twin_agent.testing.voice_fakes import FakeFfmpeg, FakeTTS

SHORT_EN = "Please try again now, it should work."
SHORT_AR = "جرب تاني دلوقتي من فضلك، المفروض تشتغل."


def voice_on(cfg: dict, **over) -> dict:
    """The test config with voice consent (D1 + D2) and voice switched on."""
    c = copy.deepcopy(cfg)
    c["consent"]["d1_ref"] = "D1-test"
    c["consent"]["d2_voice_ref"] = "D2-test"
    c["voice"] = {**(c.get("voice") or {}), "enabled": True, **over}
    return c


def make_maker(cfg, db, clock, out_dir, *, tts=None, ffmpeg=None):
    tts = tts or FakeTTS()
    ff = ffmpeg or FakeFfmpeg()
    return VoiceMaker(tts, cfg, budget=CharBudget(db, cfg, clock), out_dir=out_dir, run=ff), tts, ff
