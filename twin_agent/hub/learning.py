"""§15a — self-learning from every closed ticket.

On close, ONE append-only learning record captures the ground-truth signals (final type/module, final owner incl.
reassignment, resolution text, human edits to the draft, client rating, ShipShape proof outcome, judge scores).
Records feed routing weights, the KB and template phrasings; a periodic ``review`` job SUMMARISES recent records
and emits a change-proposal artifact for human approval — it NEVER auto-mutates templates or weights.

No client PII beyond what the ticket already holds: a record is restricted to ``ALLOWED_FIELDS`` (a learning
record with any field outside this set is refused — the §15a acceptance test).
"""
from __future__ import annotations

from collections import defaultdict

ALLOWED_FIELDS = frozenset({
    "ticket_id", "client", "partner_id", "final_type", "final_module", "final_owner_uid", "final_owner_name",
    "reassigned", "resolution", "human_edits", "client_rating", "proof_outcome", "judge_scores", "ts",
})


class LearningRefused(ValueError):
    """A learning record carried a field outside ALLOWED_FIELDS (possible PII leak): refused."""


class FakeLearningLog:
    """In-memory append-only log + a deterministic review job. In production this is a durable append-only store."""

    def __init__(self):
        self.records: list[dict] = []
        self.signals: list[dict] = []   # one-tap-override / correction signals (§15b feedback)

    def record(self, signals: dict) -> dict:
        extra = set(signals) - ALLOWED_FIELDS
        if extra:
            raise LearningRefused(f"learning record has non-ticket fields: {sorted(extra)}")
        rec = dict(signals)
        self.records.append(rec)
        return rec

    def signal(self, kind: str, payload: dict) -> dict:
        """A lightweight learning SIGNAL (e.g. a one-tap reassignment override) — feeds routing (§15b -> §15a)."""
        sig = {"kind": kind, **payload}
        self.signals.append(sig)
        return sig

    # ---------------------------------------------------------------- aggregation (routing weights, KB)

    def routing_weight(self, client: str, ticket_type: str) -> dict:
        """Owner-share for (client, type) from the records: the direction routing weights should move. A batch of
        records that all resolved to one owner shifts the weight toward that owner (the §15a acceptance test)."""
        counts: dict = defaultdict(int)
        total = 0
        for r in self.records:
            if str(r.get("client")) == str(client) and str(r.get("final_type")) == str(ticket_type):
                counts[int(r.get("final_owner_uid") or 0)] += 1
                total += 1
        weights = {uid: round(n / total, 3) for uid, n in counts.items()} if total else {}
        return {"client": client, "type": ticket_type, "n": total, "weights": weights}

    def review(self) -> dict:
        """Periodic review: summarise recent corrections/ratings and PROPOSE routing/KB/template changes for a
        human. Never applies anything (a proposal artifact only)."""
        proposals: list[dict] = []
        # Routing: for each (client, type) seen, propose the majority owner as the preferred routing target.
        seen = {(r.get("client"), r.get("final_type")) for r in self.records}
        for client, ttype in sorted(seen, key=lambda x: (str(x[0]), str(x[1]))):
            rw = self.routing_weight(client, ttype)
            if rw["weights"]:
                top = max(rw["weights"], key=rw["weights"].get)
                proposals.append({"kind": "routing", "client": client, "type": ttype,
                                  "propose_owner_uid": top, "support": rw["weights"][top], "n": rw["n"]})
        # KB: resolved Q->A notes worth appending.
        for r in self.records:
            if r.get("resolution"):
                proposals.append({"kind": "kb", "ticket_id": r.get("ticket_id"),
                                  "module": r.get("final_module"), "answer": r.get("resolution")})
        # Templates: phrasings that correlated with a high rating (surface, never auto-apply).
        good = [r for r in self.records if (r.get("client_rating") or 0) >= 4 and r.get("resolution")]
        for r in good:
            proposals.append({"kind": "template", "phrasing_from_ticket": r.get("ticket_id"),
                              "rating": r.get("client_rating")})
        return {"status": "proposal", "auto_applied": False, "n_records": len(self.records),
                "proposals": proposals}
