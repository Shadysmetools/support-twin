"""Honest, evidence-gated confidence for the Ticket Analyst's blind self-reported float.

WHAT THEIR PIPELINE HAS TODAY
    run-ticket-analyst.ps1 classifies each New ticket and returns {track, type, confidence, summary,
    root_cause, related, recommendation, to_verify}. `confidence` is a SINGLE self-reported float with
    NO evidence gate, NO threshold - the Analyst scores blind ("no live DB/repo/web this run") and writes
    on every ticket anyway (25-TICKET-ANALYST.md: "Confidence = yes-man, confirmed").

WHAT THIS SERVICE ADDS
    assess() takes that classification plus whatever evidence was gathered and returns an HONEST verdict:
      - it reuses the deterministic evidence gate + cross-model REFUTER already built
        (twin_agent/checks/refuter_xmodel.py) against the root-cause / classification claim;
      - it computes a TRUST LEVEL, never a bare vibe number:
          GREEN  = the cross-model refuter FAILED to overturn it AND concrete evidence is cited
                   (a record id / file:line / a reproduction);
          AMBER  = plausible, partial evidence (evidence present but not concrete, or concrete but no
                   independent cross-model run was possible);
          RED    = no evidence at all, OR the refuter overturned it.
      - the Analyst's raw confidence is an INPUT carried through for the record, NEVER the output. The
        honest_confidence is capped by the trust band, so a blind 0.95 with no evidence comes out RED.

OBSERVE / PREPARE-ONLY
    assess() is pure: it returns the verdict and touches nothing. prepare_note() renders the internal
    log-note DRAFT (blueprint rule: tag Supervisor in a log note, NO [AI] activity) - it does not post it, does
    not move a stage, does not write Odoo.

CLI
    `python -m twin_agent.verify_service` reads a classification JSON on stdin (or --file PATH) and prints
    the verdict JSON on stdout, so run-ticket-analyst.ps1 can call it as a post-classification step. The CLI
    default is offline/observe: it wires NO refuter unless a key is present, so it never makes a network call
    on its own - it reports mode="single-model" honestly in that case.
"""
from __future__ import annotations

import json
import sys

from .checks.refuter_xmodel import Claim, check_claim, concrete_evidence_reasons, _is_concrete

TRUST_GREEN = "GREEN"
TRUST_AMBER = "AMBER"
TRUST_RED = "RED"

# honest_confidence is capped by the trust band and never exceeds the (normalised) self-reported value either.
CONF_CAP = {TRUST_GREEN: 0.9, TRUST_AMBER: 0.6, TRUST_RED: 0.2}


def _normalise_confidence(raw) -> float | None:
    """The Analyst reports 0.0-1.0; the Odoo x_ai_confidence field is 0-100. Accept either: > 1 means percent."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > 1:
        v = v / 100.0
    return max(0.0, min(1.0, v))


def _claim_from(classification: dict, evidence_sources) -> Claim:
    """The matter-claim to gate: the Analyst's root-cause / classification call, with the evidence offered."""
    track = str(classification.get("track") or "").strip()
    typ = str(classification.get("type") or "").strip()
    root = str(classification.get("root_cause") or classification.get("root_cause_hypothesis")
               or classification.get("summary") or "").strip()
    statement = f"this ticket is {track}/{typ}: {root}".strip()
    evidence = [e for e in (evidence_sources or []) if isinstance(e, dict)]
    return Claim("diagnosis", statement, evidence)


def assess(classification: dict, evidence_sources=None, *, refuter=None) -> dict:
    """Turn one Analyst classification into an honest, evidence-gated verdict.

    classification: the Analyst's output dict {track, type, confidence, root_cause, ...}. `type` (incl.
        "inquiries", a Functional TYPE per Supervisor's blueprint - NOT a track) is carried through unchanged.
    evidence_sources: list of evidence item dicts (odoo_read / runbook / reproduction / file:line ...). May also
        be supplied as classification["evidence"].
    refuter: an optional cross-model refuter (see refuter_xmodel). When None, GREEN is unreachable: concrete
        evidence without an independent cross-model run is capped at AMBER (honest about the weaker check).

    Returns a JSON-friendly verdict dict:
      {trust, honest_confidence, evidence[], refuter_verdict, to_verify[], recommendation,
       track, type, self_reported_confidence, overturned, mode}
    """
    classification = classification or {}
    if evidence_sources is None:
        evidence_sources = classification.get("evidence") or classification.get("evidence_sources") or []
    claim = _claim_from(classification, evidence_sources)

    ev_reasons = concrete_evidence_reasons(claim)
    has_concrete = not ev_reasons
    any_evidence = bool(claim.evidence)
    concrete_items = [e for e in claim.evidence if _is_concrete(e)]

    cv = check_claim(claim, refuter, require_cross_model=(refuter is not None))
    # The gate bundles "no evidence" and "overturned" into verified=False; split them apart by reading reasons.
    overturned = any(("refuted by" in r) or ("fail-closed" in r) for r in cv.reasons)

    if overturned:
        trust = TRUST_RED
    elif has_concrete and cv.mode == "cross-model" and cv.verified:
        trust = TRUST_GREEN
    elif has_concrete or any_evidence:
        trust = TRUST_AMBER        # concrete-but-no-cross-model, or non-concrete partial evidence
    else:
        trust = TRUST_RED          # no evidence at all

    raw = _normalise_confidence(classification.get("confidence"))
    honest = CONF_CAP[trust] if raw is None else min(raw, CONF_CAP[trust])

    to_verify: list[str] = []
    if overturned:
        to_verify += [r for r in cv.reasons if "refuted by" in r or "fail-closed" in r]
    to_verify += ev_reasons
    for extra in (classification.get("to_verify"),):
        if isinstance(extra, str) and extra.strip():
            to_verify.append(extra.strip())

    return {
        "trust": trust,
        "honest_confidence": round(honest, 4),
        "evidence": concrete_items,
        "refuter_verdict": {"claim": cv.claim, "verified": cv.verified, "mode": cv.mode, "reasons": cv.reasons},
        "to_verify": to_verify,
        "recommendation": _recommend(trust, overturned, raw),
        "track": classification.get("track"),
        "type": classification.get("type"),            # INQUIRY etc. carried through as a Functional TYPE
        "self_reported_confidence": raw,
        "overturned": overturned,
        "mode": cv.mode,
    }


def _recommend(trust: str, overturned: bool, raw) -> str:
    if trust == TRUST_GREEN:
        return ("Evidence-backed and not overturned by the cross-model refuter. Prepare the log note tagging "
                "Supervisor; act only on Supervisor's decision (observe/prepare-only).")
    if trust == TRUST_AMBER:
        return ("Plausible but only partial evidence. Tag Supervisor; cite a concrete record id / file:line / "
                "reproduction (and run the cross-model refuter) before any action.")
    if overturned:
        return ("The cross-model refuter OVERTURNED this classification. Do not act; tag Supervisor with the "
                "counter-evidence and re-investigate.")
    tail = f" (self-reported {raw:.2f} is not trusted without evidence)" if raw is not None else ""
    return (f"No concrete evidence{tail}. Do not act on this classification; tag Supervisor to gather evidence "
            "and re-run.")


def prepare_note(verdict: dict) -> dict:
    """Render the INTERNAL log-note DRAFT for a verdict. Blueprint rule: the agent tags Supervisor in a LOG NOTE,
    never an [AI] activity. OBSERVE: this returns the draft; it does NOT post, does NOT create an activity, does
    NOT move a stage. The live runner posts it only after Supervisor's decision.

    supervisor_partner_id is left None: Supervisor's Odoo partner id is not known on this laptop and is wired on device day.
    """
    lines = [
        f"@Supervisor - Ticket Analyst check: trust {verdict['trust']} "
        f"(honest confidence {verdict['honest_confidence']}).",
        f"Classification: {verdict.get('track')}/{verdict.get('type')} "
        f"(self-reported {verdict.get('self_reported_confidence')}).",
        f"Refuter: {verdict['refuter_verdict']['mode']}, "
        f"{'overturned' if verdict['overturned'] else 'not overturned'}.",
    ]
    if verdict.get("evidence"):
        lines.append(f"Evidence cited: {len(verdict['evidence'])} concrete item(s).")
    if verdict.get("to_verify"):
        lines.append("To verify: " + "; ".join(verdict["to_verify"]))
    lines.append(f"Recommendation: {verdict['recommendation']}")
    body = "\n".join(lines)
    return {
        "kind": "log_note",        # a chatter log note, NOT an activity
        "is_activity": False,
        "internal": True,
        "posted": False,           # prepared, not posted (observe/prepare-only)
        "mentions": ["Supervisor"],
        "supervisor_partner_id": None,  # wired on device day
        "body": body,
    }


# --- CLI ------------------------------------------------------------------------------------------------------

def _read_input(argv: list[str]) -> str:
    """Read the classification JSON from --file PATH or stdin. Decode as utf-8-sig: PS 5.1 pipes native-exe input
    as ASCII and Out-File writes a UTF-8 BOM, so Arabic survives only if we strip the BOM and decode UTF-8."""
    if "--file" in argv:
        path = argv[argv.index("--file") + 1]
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8-sig")
    data = sys.stdin.buffer.read()
    return data.decode("utf-8-sig") if isinstance(data, (bytes, bytearray)) else str(data)


def build_refuter():
    """The CLI's refuter, or None. Returns None by default so the CLI makes NO network call on its own; wiring a
    real cross-model refuter (keys, model choice) happens on device day with Principal. Kept as a seam so the pipeline
    can pass a refuter into assess() directly when it wants GREEN-capable verdicts."""
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    raw = _read_input(argv)
    obj = json.loads(raw)
    if isinstance(obj, dict) and "classification" in obj:
        classification = obj.get("classification") or {}
        evidence = obj.get("evidence_sources")
        if evidence is None:
            evidence = obj.get("evidence")
    else:
        classification = obj
        evidence = obj.get("evidence_sources") if isinstance(obj, dict) else None
    verdict = assess(classification, evidence, refuter=build_refuter())
    # ensure_ascii=True so the JSON survives any Windows console code page.
    print(json.dumps(verdict, ensure_ascii=True))
    return 0


if __name__ == "__main__":        # pragma: no cover - exercised via main([...]) in tests
    raise SystemExit(main())
