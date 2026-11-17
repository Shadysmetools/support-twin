"""§16b — an INDEPENDENT judge model (distinct from the drafter in production) that scores every draft client
reply BEFORE the owner sees it, plus the classification, the §15b assignment, and the ShipShape proof video.

Per-dimension scores (0..1): correctness, tone, language, policy, completeness. A low aggregate raises a FLAG;
the hub routes a flagged item to owner-review (and never lets a flagged draft ride the auto-ack-only path). Judge
scores feed §15a learning and the stage-11 Metrics KPI, and an agreement metric (``agreement``) computes the
judge↔human match rate for the weekly calibration (§16d).

The fake is deterministic (keyword heuristics) so CI needs no model; production swaps in a real model behind the
same ``score`` signature. ``model`` must differ from the drafter's model in production config (asserted by a test).
"""
from __future__ import annotations

DIMENSIONS = ("correctness", "tone", "language", "policy", "completeness")
DEFAULT_THRESHOLD = 0.7

# Phrases that violate the §13 style guide (over-promising, rudeness, missing sign-off, jargon).
_BAD_TONE = ("stupid", "obviously", "whatever", "idiot", "غبي", "طبعا انت")
_OVERPROMISE = ("guaranteed", "100%", "forever", "never fail", "مضمون ١٠٠")
SIGN_MARKERS = ("Acme Support", "دعم Acme")


def _lang_of(text: str) -> str:
    """'ar' when Arabic letters are the clear majority of the letters (brand/module/name fragments in Latin are
    tolerated); a mostly-English text with one Arabic character is 'en' (review #16)."""
    ar = sum(1 for c in text or "" if "؀" <= c <= "ۿ")
    latin = sum(1 for c in text or "" if c.isascii() and c.isalpha())
    return "ar" if ar and ar >= 0.6 * (ar + latin) else "en"


class FakeJudge:
    """Deterministic independent judge. ``model`` names the (distinct) judge model for the production assertion."""

    real = False                                            # a fake: no result from it may be called "judge-scored"

    def __init__(self, *, model: str = "claude-judge", threshold: float = DEFAULT_THRESHOLD):
        self.model = model
        self.threshold = threshold
        self.scored: list[dict] = []

    def score(self, kind: str, item: dict) -> dict:
        if kind == "draft":
            scores = self._score_draft(item)
        elif kind == "classification":
            scores = self._score_classification(item)
        elif kind == "assignment":
            scores = self._score_assignment(item)
        elif kind == "video":
            scores = self._score_video(item)
        elif kind == "ticket":
            scores = self._score_ticket(item)
        else:
            scores = {d: 0.5 for d in DIMENSIONS}
        agg = round(sum(scores.values()) / len(scores), 3)
        res = {"kind": kind, "model": self.model, "scores": scores, "aggregate": agg,
               "flag": agg < self.threshold, "threshold": self.threshold}
        self.scored.append(res)
        return res

    def _score_draft(self, item: dict) -> dict:
        text = str(item.get("text") or "")
        expect_lang = str(item.get("expect_lang") or _lang_of(text))
        low = text.lower()
        correctness = 0.9 if text.strip() else 0.2
        tone = 0.3 if any(b in low for b in _BAD_TONE) else 0.9
        policy = 0.3 if any(o in low for o in _OVERPROMISE) else (0.9 if any(s in text for s in SIGN_MARKERS) else 0.6)
        language = 0.9 if _lang_of(text) == expect_lang else 0.3
        completeness = 0.9 if (len(text.split()) >= 8 and any(s in text for s in SIGN_MARKERS)) else 0.5
        return {"correctness": correctness, "tone": tone, "language": language, "policy": policy,
                "completeness": completeness}

    def _score_classification(self, item: dict) -> dict:
        ok = bool(item.get("ticket_type")) and str(item.get("module")) != ""
        conf = float(item.get("confidence") or 0.5)
        base = 0.85 if ok else 0.4
        return {d: round(min(0.95, base + (conf - 0.5) * 0.2), 3) for d in DIMENSIONS}

    def _score_assignment(self, item: dict) -> dict:
        has_reason = bool(item.get("factors") or item.get("rationale"))
        base = 0.85 if has_reason else 0.4
        return {d: base for d in DIMENSIONS}

    def _score_ticket(self, item: dict) -> dict:
        """Scores the ticket-quality RUBRIC booleans (evalharness.ticket_rubric); a deterministic stand-in, not a model."""
        c = item.get("checks") or {}
        def g(*keys):
            return round(sum(1.0 if c.get(k) else 0.2 for k in keys) / len(keys), 3)

        return {"correctness": g("type_ok", "urgency_ok"), "completeness": g("title_ok", "sections_ok"),
                "language": g("original_ok"), "policy": g("no_invention"), "tone": 0.9}

    def _score_video(self, item: dict) -> dict:
        prepared = item.get("status") == "prepared" and item.get("verdict") in ("reproduced", "data_only")
        base = 0.88 if prepared else 0.45
        return {d: base for d in DIMENSIONS}


def agreement(pairs: list[tuple[bool, bool]]) -> float:
    """Judge↔human agreement rate from (judge_flag, human_flag) pairs (§16c target ≥ 0.85)."""
    if not pairs:
        return 0.0
    match = sum(1 for j, h in pairs if bool(j) == bool(h))
    return round(match / len(pairs), 3)


# ---------------------------------------------------------------- §16e concrete judge/eval config (provisional)

# All provisional until the ~60-ticket bake-off. CI always runs ``FakeJudge``; these are the LIVE config, gated on
# creds (a tracked blocker). The judge MUST be a different model family from the drafter (a production assertion).
JUDGE_CONFIG = {
    "primary": {"provider": "vertex", "model": "gemini-3.6-flash", "zdr": True, "modalities": ("text", "video")},
    "fallback": {"provider": "openai", "model": "gpt-6.1-terra", "modalities": ("text", "video_frames"),
                 "note": "scores sampled video KEY-FRAMES, not whole videos"},
    "redact_before_every_call": True,
    "scored_by_judge": ("draft", "classification", "video", "ticket"),          # confirm/resolution drafts, classification sanity, videos
    "scored_by_exact_match": ("type", "assignment"),                  # vs the owner's FINAL Odoo values, NOT the judge
    "ack": "templated — no blocking judge",
    "video_method": "ShipShape step-log check + key-frame vision",
    "eval_framework": {"name": "deepeval", "pytest_based": True, "telemetry": "off",
                       "target": "the LangGraph graph"},
}


def redact_for_judge(text: str, names: list[str] | None = None) -> str:
    """Strip names + phone numbers (and the other secret/PII patterns) BEFORE every judge call (§16e). Phones,
    emails and tokens come from the shared ``redact.redact``; names are masked here (the judge never needs them)."""
    from ..redact import redact
    out = redact(str(text or ""))
    for n in (names or []):
        n = str(n or "").strip()
        if len(n) >= 2:
            import re
            out = re.sub(re.escape(n), "[NAME]", out, flags=re.IGNORECASE)
    return out


def redact_item_for_judge(kind: str, item: dict, names: list[str] | None = None) -> dict:
    """Redact the free-text fields of a judge ``item`` before the call. Scores/flags are unaffected."""
    red = dict(item)
    for f in ("text", "rationale", "note", "understanding", "summary", "title", "description"):
        if isinstance(red.get(f), str):
            red[f] = redact_for_judge(red[f], names)
    return red


class _RealJudgeBase:
    """A live judge adapter: redacts every item, then (when its creds are present) would call the provider. With no
    creds it returns ``{"status": "blocked", ...}`` — never a fabricated score. ``model`` differs from the drafter."""

    provider = "?"
    real = True

    def __init__(self, *, model: str, threshold: float = DEFAULT_THRESHOLD, backend=None):
        self.model = model
        self.threshold = threshold
        self._backend = backend

    def available(self) -> str | None:                    # overridden per provider
        return "not configured"

    def score(self, kind: str, item: dict, *, names: list[str] | None = None) -> dict:
        red = redact_item_for_judge(kind, item, names)    # §16e: redact BEFORE any provider call
        why = self.available()
        if why:
            return {"kind": kind, "model": self.model, "status": "blocked", "why": why, "redacted": True}
        # Live provider call intentionally not wired in the no-creds slice.
        return {"kind": kind, "model": self.model, "status": "blocked",
                "why": f"{self.provider} call not enabled in Phase 1 (bake-off pending)", "redacted": True}


class VertexJudge(_RealJudgeBase):
    """Primary: Gemini 3.6 Flash via Vertex AI (ZDR, text+video). Blocks without Vertex/Google creds."""

    provider = "vertex"

    def available(self) -> str | None:
        import os
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("VERTEX_PROJECT"):
            return None
        return "no Vertex AI creds (GOOGLE_APPLICATION_CREDENTIALS / VERTEX_PROJECT); judge blocked"


class FrameFallbackJudge(_RealJudgeBase):
    """Fallback: GPT-6.1 Terra, scoring sampled video KEY-FRAMES (not whole videos). Blocks without an OpenAI key."""

    provider = "openai"

    def available(self) -> str | None:
        import os
        return None if os.environ.get("OPENAI_API_KEY") else "no OPENAI_API_KEY; fallback judge blocked"


class PrimaryFallbackJudge:
    """§16e primary+fallback shape: try the primary judge; if it is unavailable, fall back. Always redacts first."""

    def __init__(self, primary, fallback, *, model: str | None = None):
        self.primary = primary
        self.fallback = fallback
        self.model = model or getattr(primary, "model", "judge")

    def score(self, kind: str, item: dict, *, names: list[str] | None = None) -> dict:
        res = self.primary.score(kind, item, names=names)
        if res.get("status") == "blocked":
            fb = self.fallback.score(kind, item, names=names)
            fb["via"] = "fallback"
            return fb
        res["via"] = "primary"
        return res


def resolve_judge(cfg: dict | None = None):
    """Production factory: the primary+fallback live judge. CI injects ``FakeJudge`` directly instead."""
    cfg = cfg or {}
    primary = VertexJudge(model=JUDGE_CONFIG["primary"]["model"])
    fallback = FrameFallbackJudge(model=JUDGE_CONFIG["fallback"]["model"])
    return PrimaryFallbackJudge(primary, fallback)
