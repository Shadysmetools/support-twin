"""ASSIGN (stage 5) + §15b team-aware assignment.

The router reads Odoo + Wiki (owner-by-client / owner-by-module / owner-by-type + current load) and scores
candidates; the top score wins, with the Principal=support / Supervisor=lead+implementation split as the FALLBACK
(plan §5). Every assignment carries an "assigned to X because…" rationale (the factors + weights that decided
it) for the owner's Telegram brief, and a one-tap override writes back a learning signal (§15a).

This is the REAL deterministic assigner (no creds needed): with the knowledge sources stubbed it still returns a
deterministic choice + rationale, which is exactly what the §15b acceptance tests require. In production the same
interface can be backed by a model; the Judge (§16) then scores the assignment.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Analysis, Owner

# Default scoring weights. Positive factors add; load subtracts (avoid piling onto a saturated owner).
WEIGHTS = {"owns_client": 3.0, "owns_module": 2.0, "owns_type": 1.5, "role_fit": 1.0, "load": 0.5}


@dataclass
class Candidate:
    """A seeded team member (role/skills/load). In production these come from Odoo membership + open-ticket counts."""

    owner: Owner
    skills: tuple[str, ...] = ()         # modules/areas this person handles
    handles_tracks: tuple[str, ...] = ()  # inquiry/functional/development they cover
    open_load: int = 0                   # current open-ticket count (from Odoo)


def _role_fit(cand: Candidate, analysis: Analysis) -> float:
    if analysis.track in cand.handles_tracks:
        return 1.0
    return 0.0


def score_candidate(cand: Candidate, analysis: Analysis, client_context: dict) -> tuple[float, list[str]]:
    """Score one candidate and return (score, factor-citing reasons)."""
    reasons: list[str] = []
    score = 0.0
    owns_client = int(cand.owner.uid) in {int(u) for u in (client_context.get("owner_uids") or [])}
    if owns_client:
        score += WEIGHTS["owns_client"]
        reasons.append(f"owns this client (history)")
    if analysis.module != "unknown" and analysis.module in cand.skills:
        score += WEIGHTS["owns_module"]
        reasons.append(f"handles the {analysis.module} module")
    type_owner = (client_context.get("type_owner") or {}).get(analysis.ticket_type)
    if type_owner and int(type_owner) == int(cand.owner.uid):
        score += WEIGHTS["owns_type"]
        reasons.append(f"usually handles {analysis.ticket_type} tickets")
    rf = _role_fit(cand, analysis)
    if rf:
        score += WEIGHTS["role_fit"] * rf
        reasons.append(f"role covers the {analysis.track} track")
    penalty = WEIGHTS["load"] * float(cand.open_load)
    score -= penalty
    if cand.open_load:
        reasons.append(f"current load {cand.open_load} open (−{penalty:g})")
    return round(score, 3), reasons


class RuleAssigner:
    """Deterministic §15b assigner. Ties break by lower load, then lower uid (stable)."""

    def __init__(self, candidates: list[Candidate], *, fallback_uid: int = 0):
        self.candidates = list(candidates)
        self.fallback_uid = fallback_uid

    def assign(self, analysis: Analysis, client_context: dict) -> dict:
        if not self.candidates:
            raise ValueError("no candidates to assign from")
        scored = []
        for c in self.candidates:
            s, reasons = score_candidate(c, analysis, client_context or {})
            scored.append((s, -c.open_load, -c.owner.uid, c, reasons))
        scored.sort(reverse=True)
        best = scored[0]
        cand, reasons = best[3], best[4]
        if not reasons:
            reasons = [f"fallback by track ({analysis.track})"]
        rationale = f"assigned to {cand.owner.name} because: " + "; ".join(reasons)
        runners = [{"name": s[3].owner.name, "uid": s[3].owner.uid, "score": s[0]} for s in scored]
        return {"owner": cand.owner, "uid": cand.owner.uid, "score": best[0],
                "rationale": rationale, "factors": reasons, "ranking": runners}


def default_team() -> list[Candidate]:
    """The Principal=support / Supervisor=lead+implementation split as a seeded fallback team (synthetic uids from FakeOdoo)."""
    principal = Owner(uid=777, name="Principal", tg_id=1001, role="support")
    supervisor = Owner(uid=778, name="Supervisor", tg_id=1003, role="implementation-lead")
    return [
        Candidate(principal, skills=("Helpdesk", "Sales", "POS"), handles_tracks=("inquiry", "functional"), open_load=0),
        Candidate(supervisor, skills=("Inventory", "Manufacturing", "Accounting"),
                  handles_tracks=("development", "functional"), open_load=0),
    ]
