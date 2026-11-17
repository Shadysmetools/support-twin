"""Deterministic fakes for the proof pipeline's adapters (15 steps 4,6,7,8).

These are used ONLY by tests, injected into ``pipeline.run(..., adapters=...)``. They are NEVER the production
fallback: in production, ``pipeline.resolve_adapters`` returns the REAL adapters, and a missing key/dep leaves that
step ``blocked`` (not faked). To make a faked artifact unmistakable, every fake marks ``simulated: True`` and the
fake share link uses the ``wiki.invalid`` domain, which can never be a real Wiki URL.

No network, no browser, no ffmpeg, no ElevenLabs, no Wiki. Each fake records nothing it was not told.
"""
from __future__ import annotations

from .pipeline import FIX_KINDS, GUIDANCE_VERDICTS


def _base(request_id: str) -> str:
    return f"data/proof/{request_id}"


class FakeReproducer:
    """Canned ShipShape capture. ``verdict`` drives the verify-first gate; ``secret_in_frame`` injects a real
    portal token into the OCR'd frame text so the privacy step can prove it blocks delivery."""

    def __init__(self, verdict: str = "reproduced", *, secret_in_frame: bool = False,
                 frame_texts: list[str] | None = None):
        self.verdict = verdict
        self.secret_in_frame = secret_in_frame
        self.frame_texts = frame_texts

    def available(self, cfg: dict) -> str | None:
        return None

    def capture(self, request: dict, packet: dict, kind: str, cfg: dict) -> dict:
        rid = request.get("id") or "REQ-0000"
        base = _base(rid)
        is_fix = kind in FIX_KINDS
        guidance = self.verdict in GUIDANCE_VERDICTS
        # Reproduced FIX: BEFORE (the repro) + AFTER (the fix) = the proof pair.
        # Guidance (not_reproduced / user_error) and INQUIRY (how-to): AFTER only; the failed repro attempt is
        # evidence, not part of the delivered how-to.
        if is_fix and not guidance:
            before, after = f"{base}/before.mp4", f"{base}/after.mp4"
        else:
            before, after = None, f"{base}/after.mp4"
        frames = list(self.frame_texts or ["Odoo: WAVE/00001 validated, status Done"])
        if self.secret_in_frame:
            frames.append("open https://helpdesk.example.com/my/ticket/5/abcdef123456")
        return {"step": "reproduce_capture", "status": "ok", "verdict": self.verdict,
                "before_path": before, "after_path": after,
                "screenshots": [f"{base}/shot-01.png"],
                "evidence": {"case": "TC-01", "note": "staging reproduction (simulated)"},
                "frame_texts": frames, "simulated": True}


class FakeNarrator:
    """Canned narration audio. Mimics the edge-tts fallback label so the artifact never claims Principal's clone."""

    def available(self, cfg: dict) -> str | None:
        return None

    def synth(self, script: dict, cfg: dict) -> dict:
        rid = script.get("request_id") or "REQ-0000"
        return {"step": "narration", "status": "ok", "audio_path": f"{_base(rid)}/narration.mp3",
                "provider": "fake", "lang": script.get("lang") or "ar",
                "narration_note": "simulated voice, not Principal's clone", "simulated": True}


class FakeMuxer:
    """Canned ffmpeg mux output (video + narration + burned captions)."""

    def available(self, cfg: dict) -> str | None:
        return None

    def combine(self, request_id: str, capture: dict, narration: dict, cfg: dict) -> dict:
        base = _base(request_id)
        return {"step": "mux", "status": "ok", "mp4_path": f"{base}/proof.mp4",
                "captions_path": f"{base}/proof.srt", "simulated": True}


class FakePublisher:
    """Canned Wiki upload. Returns an unmistakably fake share link (wiki.invalid) and the private gate off."""

    def available(self, cfg: dict) -> str | None:
        return None

    def upload(self, request_id: str, mp4_path: str, client: str, ticket_ref: str, cfg: dict) -> dict:
        client = client or "UNKNOWN"
        ticket_ref = ticket_ref or request_id
        return {"step": "publish", "status": "ok",
                "share_link": f"https://wiki.invalid/s/{client}/{ticket_ref}",
                "project": f"client:{client}", "folder": f"ticket:{ticket_ref}",
                "public": False, "simulated": True}


def fake_adapters(**overrides) -> dict:
    """A full set of fakes for ``pipeline.run(adapters=...)``. Override any one, e.g. ``reproduce=FakeReproducer(...)``."""
    a = {"reproduce": FakeReproducer(), "narrate": FakeNarrator(), "mux": FakeMuxer(), "publish": FakePublisher()}
    a.update(overrides)
    return a
