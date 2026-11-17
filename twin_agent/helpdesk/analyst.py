"""Ticket Analyst inside the twin (Principal 2026-11-12).

Every request the twin turns into an Acme ticket is routed at once:
- Functional (stage 2) or Development Requested (stage 6).
- It writes exactly x_track, x_type and x_ai_confidence, plus one internal note.

Base: Supervisor's analyst as described in 25-TICKET-ANALYST.md (Sysadmin: the latest version).
- TRACK: functional (configuration / automation, no code) or development (code / module / bug fix).
- TYPE: functional -> bug | new_request | configuration | inquiries; development -> bug | new_request | customization.
- Bug = existing behaviour broken. Configuration = a SMALL no-code task. New Request = a SUBSTANTIAL reusable,
  chargeable feature. Customization = a code change to existing custom behaviour. Inquiries = a question or how-to,
  nothing to build. When unsure, lean Configuration.

Our changes:
- Inquiry stays a Functional TYPE (MASTER-PROMPT), not its own track.
- Confidence comes from CHECKED evidence: records the worker read on Globex live (or code it read), the judges that
  passed, and the independent check. It is never the worker's own number. Weak evidence = "unsure, please check".
"""
from __future__ import annotations

from dataclasses import dataclass, field

TRACKS = ("functional", "development")
TYPES = {"functional": ("bug", "new_request", "configuration", "inquiries"),
         "development": ("bug", "new_request", "customization")}
TYPE_NAME = {"bug": "Bug", "new_request": "New Request", "configuration": "Configuration",
             "inquiries": "Inquiries", "customization": "Customization"}
STAGE = {"functional": 2, "development": 6}
STAGE_NAME = {2: "Functional", 6: "Development Requested"}
CHECKED_KINDS = ("odoo_read", "code", "repo_read")
GREEN, AMBER = 0.7, 0.4

# The tier the worker already gives (T1 answer, T2a fix, T2b configuration, T3 needs code, T4 project).
TIER_TRACK = {"T3": "development"}
TIER_TYPE = {"T1": "inquiries", "T2a": "bug", "T2b": "configuration", "T3": "customization", "T4": "new_request"}


@dataclass
class Routing:
    track: str
    type: str
    confidence: float
    level: str                      # GREEN / AMBER / RED
    unsure: bool
    reasons: list[str] = field(default_factory=list)
    source: str = "worker"          # "worker": the worker's classification; "tier": derived from its tier

    @property
    def stage_id(self) -> int:
        return STAGE[self.track]

    @property
    def stage_name(self) -> str:
        return STAGE_NAME[self.stage_id]

    @property
    def type_name(self) -> str:
        return TYPE_NAME[self.type]


def _has_dev_part(packet: dict) -> bool:
    td = packet.get("ticket_draft") or {}
    parts = td.get("parts") if isinstance(td, dict) else None
    return any(isinstance(p, dict) and p.get("type") == "dev" for p in parts or [])


def _tier_track(packet: dict) -> str:
    if _has_dev_part(packet):
        return "development"
    return TIER_TRACK.get(str(packet.get("tier") or ""), "functional")


def _default_type(track: str, tier: str) -> str:
    t = TIER_TYPE.get(tier, "configuration" if track == "functional" else "customization")
    if t not in TYPES[track]:
        t = "configuration" if track == "functional" else "customization"
    return t


def classify(packet: dict, verdicts: list | None, verified: bool) -> Routing:
    packet = packet or {}
    tier = str(packet.get("tier") or "")
    reasons: list[str] = []
    wt = str(packet.get("track") or "").strip().lower()
    wtype = str(packet.get("ticket_type") or "").strip().lower().replace(" ", "_")
    if wt in ("inquiry", "inquiries"):
        wt, wtype = "functional", "inquiries"
    tier_track = _tier_track(packet)
    if wt in TRACKS:
        track, source = wt, "worker"
    else:
        track, source = tier_track, "tier"
        reasons.append("the worker gave no track: taken from its tier")
    typ = wtype if wtype in TYPES[track] else _default_type(track, tier)
    if wtype and wtype not in TYPES[track]:
        reasons.append(f"type '{wtype}' does not fit the {track} track")

    reads = [e for e in packet.get("evidence") or [] if isinstance(e, dict) and e.get("kind") in CHECKED_KINDS]
    vs = [v if isinstance(v, dict) else getattr(v, "__dict__", {}) for v in verdicts or []]
    passed = sum(1 for v in vs if v.get("passed"))
    ratio = passed / len(vs) if vs else 0.0
    score = 0.45 * min(len(reads), 3) / 3 + 0.40 * ratio + (0.15 if verified else 0.0)
    if not reads:
        score = min(score, 0.3)
        reasons.append("no records were read to check it")
    if vs and ratio < 1:
        reasons.append(f"{passed} of {len(vs)} checks passed")
    if source == "worker" and wt != tier_track:
        score = min(score, 0.5)
        reasons.append(f"the worker's track ({wt}) and its tier ({tier or 'none'}) disagree")
    if not verified:
        score = min(score, 0.65)                     # green needs the independent check (REQ-0069, 12 Nov)
        reasons.append("not independently verified")
    if source == "tier":
        score = min(score, 0.6)                      # and the worker's own classification

    score = round(max(0.0, min(1.0, score)), 2)
    level = "GREEN" if score >= GREEN else ("AMBER" if score >= AMBER else "RED")
    return Routing(track=track, type=typ, confidence=score, level=level, unsure=level != "GREEN",
                   reasons=reasons, source=source)


def failed(why: str) -> Routing:
    """The worker could not analyse the request: route it to Functional anyway, confidence 0, unsure, with why."""
    return Routing(track="functional", type="inquiries", confidence=0.0, level="RED", unsure=True,
                   reasons=[f"the analysis failed: {str(why or 'unknown')[:200]}"], source="failed")
