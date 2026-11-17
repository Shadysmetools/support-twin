"""REAL adapters — each blocks itself with a plain reason when its key/dependency is missing. NEVER a silent
fallback to a fake (that is the whole point of the interface+fake split). These are what ``resolve_adapters``
returns in production; tests inject the fakes instead.

Phase-1 reality: with no credentials and nothing deployed, every one of these is ``blocked``. The reasons ARE the
tracked-blockers list (fresh Odoo key, Executive's number, server, Wiki headless access, Groq key, Metrics key).
"""
from __future__ import annotations

from .. import creds


class RealSTT:
    """Groq whisper-large-v3. Blocks (never crashes) when GROQ_API_KEY / the vault 'groq' entry is absent."""

    def __init__(self, *, backend=None):
        self._backend = backend

    def available(self) -> str | None:
        import os
        if os.environ.get("GROQ_API_KEY"):
            return None
        try:
            if creds.has_secret("groq", backend=self._backend):
                return None
        except Exception:                           # noqa: BLE001 - vault unavailable must not crash
            pass
        return "no Groq key (set GROQ_API_KEY or the vault 'groq' entry); STT blocked"

    def transcribe(self, audio_id: str, *, lang_hint: str = "") -> dict:
        why = self.available()
        if why:
            return {"status": "blocked", "why": why, "text": None}
        # Real call deliberately not wired in this no-creds slice; a live build posts the OGG to Groq whisper here.
        return {"status": "blocked", "why": "real Groq call not enabled in Phase 1 (no live media / number yet)",
                "text": None}


class RealTicketWriter:
    """Writes the Odoo ticket words (translation + problem phrase + expectation + steps) with a Claude model. Blocks
    (never crashes, never falls back to the fake) when there is no key: ANTHROPIC_API_KEY or the vault 'anthropic' entry.

    The text is redacted (``prepare``) before any call. TODO(PDPL sign-off, OPEN): the live call is not enabled in this
    no-creds slice, and stays off until the sign-off."""

    def __init__(self, *, backend=None):
        self._backend = backend

    def available(self) -> str | None:
        import os
        if os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            if creds.has_secret("anthropic", backend=self._backend):
                return None
        except Exception:                           # noqa: BLE001 - vault unavailable must not crash
            pass
        return "no Claude key (set ANTHROPIC_API_KEY or the vault 'anthropic' entry); ticket translation blocked"

    @staticmethod
    def prepare(text: str, client: str, names: list[str] | None = None) -> str:
        """The ONLY text a live call may send: phones/emails/tokens and the client + sender names are masked first."""
        from .judge import redact_for_judge
        return redact_for_judge(text, names=[client, *(names or [])])

    def write(self, text: str, *, lang: str, analysis, client: str, names: list[str] | None = None) -> dict:
        _payload = self.prepare(text, client, names)         # redaction is a precondition of any provider call
        why = self.available()
        if why:
            return {"status": "blocked", "why": why}
        return {"status": "blocked", "why": "real ticket-writer call not enabled in Phase 1 (no live key yet)"}


class RealMetrics:
    """Metrics KPI push. Blocks without a Metrics connector key (tracked blocker)."""

    def __init__(self, *, backend=None):
        self._backend = backend

    def available(self) -> str | None:
        try:
            if creds.has_secret("metrics", backend=self._backend):
                return None
        except Exception:                           # noqa: BLE001
            pass
        return "no Metrics key in the vault ('metrics'); KPI push blocked"

    def push_kpis(self, outcome: dict) -> dict:
        why = self.available()
        return {"ok": False, "status": "blocked", "why": why or "not enabled in Phase 1"}
