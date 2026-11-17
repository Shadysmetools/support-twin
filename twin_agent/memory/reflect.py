"""Reflection loop: after N closed cases (or on a timer), review recent cases + corrections and distil
higher-level lessons, and adjust trust levels (Generative-Agents / Reflexion style).

v0 is DETERMINISTIC (no LLM, no network): it groups cases by (client, normalized root cause), counts
INDEPENDENT sources, and sets trust accordingly. A clean hook (`reflector`) is left for an optional LLM pass.

Trust rules (match the brief: nothing GREEN on one source; the runbook Stray-IN entry stays AMBER):
- GREEN  only when a group has >=2 distinct sources AND at least one is independent (outside runbook/replay).
- AMBER  when a group has >=2 distinct sources, or one independent source, but not both.
- a correction whose words strongly overlap a group DOWNGRADES it one notch (contradiction signal).
Writes only to memory.db (lessons + case trust). Returns a small report.
"""
from __future__ import annotations

from typing import Callable

from .store import TRUST_RANK, WEAK_SOURCES, MemoryDB, dedupe_key, overlap, tokens
from . import cases as cases_store
from . import corrections as corrections_store


def _group_key(client: str, root: str, symptom: str) -> str:
    sig = " ".join(sorted(tokens(root or symptom)))
    return f"{client}|{sig}"


def _target_trust(distinct_sources: set[str]) -> str:
    independent = any(s not in WEAK_SOURCES for s in distinct_sources)
    if len(distinct_sources) >= 2 and independent:
        return "GREEN"
    if len(distinct_sources) >= 2 or independent:
        return "AMBER"
    return "RED"


def _lower(trust: str) -> str:
    return {"GREEN": "AMBER", "AMBER": "RED", "RED": "RED"}[trust]


def reflect(mem: MemoryDB, *, min_cases: int = 1, reflector: Callable[[dict], list[str]] | None = None) -> dict:
    """Review memory and update it. `reflector`, if given, receives the aggregated context and may return
    extra free-text lessons (an optional LLM pass); v0 passes nothing to it by default."""
    rows = cases_store.all_cases(mem.conn)
    report = {"cases_reviewed": len(rows), "groups": 0, "lessons_written": 0,
              "trust_upgrades": 0, "trust_downgrades": 0}
    if len(rows) < max(0, min_cases):
        return report

    corr = corrections_store.all_rules(mem.conn)
    corr_tokens = [tokens((c["context"] or "") + " " + (c["rule"] or "")) for c in corr]

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(_group_key(r["client"] or "", r["verified_root_cause"] or "", r["symptom"] or ""), []).append(r)
    report["groups"] = len(groups)

    for key, members in groups.items():
        sources = {(m["source"] or "") for m in members}
        target = _target_trust(sources)
        group_tokens = set()
        for m in members:
            group_tokens |= tokens((m["verified_root_cause"] or "") + " " + (m["symptom"] or ""))
        contradicted = any(overlap(group_tokens, ct) >= 0.5 for ct in corr_tokens)

        for m in members:
            cur = m["trust_level"] or "RED"
            new = cur
            if TRUST_RANK[target] > TRUST_RANK[cur]:
                new = target
                report["trust_upgrades"] += 1
            if contradicted and TRUST_RANK[new] > 0:
                new = _lower(new)
                report["trust_downgrades"] += 1
            if new != cur:
                cases_store.set_trust(mem, m["id"], new)

        if len(members) >= 2 or contradicted or TRUST_RANK[target] > 0:
            verb = "contradicted by a correction; trust lowered" if contradicted else f"trust set to {target}"
            lesson = (f"Recurring issue for client {members[0]['client'] or '?'}: "
                      f"{(members[0]['verified_root_cause'] or members[0]['symptom'] or '')[:160]} "
                      f"- seen in {len(members)} case(s), {len(sources)} source(s); {verb}.")
            basis = f"group={key}; sources={sorted(sources)}"
            if _write_lesson(mem, "cases", lesson, basis):
                report["lessons_written"] += 1

    if reflector is not None:
        try:
            for extra in (reflector({"groups": report["groups"], "cases": len(rows)}) or []):
                if _write_lesson(mem, "llm", str(extra), "reflector hook"):
                    report["lessons_written"] += 1
        except Exception:
            pass
    return report


def _write_lesson(mem: MemoryDB, scope: str, lesson: str, basis: str) -> bool:
    key = dedupe_key(scope, lesson)
    cur = mem.conn.execute(
        "INSERT OR IGNORE INTO lessons (ts, scope, lesson, basis, dedupe_key) VALUES (?, ?, ?, ?, ?)",
        (mem.now(), scope, lesson, basis, key))
    return cur.rowcount > 0


def lessons(mem_or_conn) -> list[dict]:
    conn = getattr(mem_or_conn, "conn", mem_or_conn)
    return [dict(r) for r in conn.execute("SELECT * FROM lessons ORDER BY id").fetchall()]
