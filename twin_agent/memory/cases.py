"""Case memory: one structured record per closed request, with similarity retrieval.

Record: {request_ref, client, track, symptom, verified_root_cause, fix, evidence_refs, trust_level,
outcome, ts}. Every free-text field is redacted before it is stored. `client` is a short role/company
code (EG/KSA/...), never a person. Retrieval = FTS5 candidates reranked by embedding-free token overlap.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..redact import redact
from .store import MemoryDB, accept_match, coverage, dedupe_key, fts_query, fts_set, normalize_trust, tokens


@dataclass
class Case:
    request_ref: str = ""
    client: str = ""
    track: str = ""
    symptom: str = ""
    verified_root_cause: str = ""
    fix: str = ""
    evidence_refs: str = ""
    trust_level: str = "RED"
    outcome: str = ""
    source: str = ""
    score: float = field(default=0.0, compare=False)


def _clean(c: Case) -> Case:
    return Case(
        request_ref=redact(c.request_ref or ""),
        client=redact((c.client or "").strip()),
        track=redact(c.track or ""),
        symptom=redact(c.symptom or ""),
        verified_root_cause=redact(c.verified_root_cause or ""),
        fix=redact(c.fix or ""),
        evidence_refs=redact(c.evidence_refs or ""),
        trust_level=normalize_trust(c.trust_level),
        outcome=redact(c.outcome or ""),
        source=redact(c.source or ""),
    )


def add(mem: MemoryDB, case: Case) -> int:
    """Store one case (redacted, idempotent on client+symptom+source). Returns the row id (0 if a duplicate)."""
    c = _clean(case)
    key = dedupe_key(c.client, c.symptom, c.source)
    cur = mem.conn.execute(
        "INSERT OR IGNORE INTO cases (request_ref, client, track, symptom, verified_root_cause, fix, "
        "evidence_refs, trust_level, outcome, source, ts, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (c.request_ref, c.client, c.track, c.symptom, c.verified_root_cause, c.fix, c.evidence_refs,
         c.trust_level, c.outcome, c.source, mem.now(), key))
    if cur.rowcount == 0:
        return 0
    rowid = cur.lastrowid
    fts_set(mem.conn, "cases_fts", rowid, c.symptom, c.verified_root_cause, c.fix)
    return rowid


def record_closed(mem: MemoryDB, request: dict, *, verified_root_cause: str = "", fix: str = "",
                  evidence_refs: str = "", trust_level: str = "RED", source: str = "listener") -> int:
    """Convenience the listener/closing or a replay can call on a closed request to grow case memory."""
    return add(mem, Case(
        request_ref=str(request.get("id") or request.get("request_ref") or ""),
        client=str(request.get("company") or request.get("client") or ""),
        track="/".join(str(request.get(k)) for k in ("lane", "tier") if request.get(k)),
        symptom=str(request.get("title") or request.get("symptom") or request.get("understanding") or ""),
        verified_root_cause=verified_root_cause, fix=fix, evidence_refs=evidence_refs,
        trust_level=trust_level, outcome=str(request.get("close_reason") or request.get("state") or ""),
        source=source))


def _row_to_case(r: sqlite3.Row, score: float = 0.0) -> Case:
    return Case(request_ref=r["request_ref"], client=r["client"], track=r["track"], symptom=r["symptom"],
                verified_root_cause=r["verified_root_cause"], fix=r["fix"], evidence_refs=r["evidence_refs"],
                trust_level=r["trust_level"], outcome=r["outcome"], source=r["source"], score=round(score, 4))


def similar(conn: sqlite3.Connection, text: str, client: str | None = None, k: int = 5) -> list[Case]:
    """The k most similar past cases. FTS5 candidates (optionally filtered to one client) reranked by token
    overlap; only cases that actually overlap are returned (no blind fallback, so recall stays relevant)."""
    k = max(1, min(int(k), 50))
    q = fts_query(text)
    want = tokens(text)
    rows: list[sqlite3.Row] = []
    if q:
        sql = ("SELECT c.* FROM cases c JOIN cases_fts f ON f.rowid = c.id WHERE cases_fts MATCH ?")
        args: list = [q]
        if client:
            sql += " AND c.client = ?"
            args.append(client)
        try:
            rows = conn.execute(sql + " LIMIT 200", args).fetchall()
        except sqlite3.OperationalError:
            rows = []
    scored: list[Case] = []
    for r in rows:
        doc = tokens(" ".join((r["symptom"] or "", r["verified_root_cause"] or "", r["fix"] or "")))
        if accept_match(want, doc):
            scored.append(_row_to_case(r, coverage(want, doc)))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:k]


def all_cases(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM cases ORDER BY id").fetchall()]


def set_trust(mem: MemoryDB, case_id: int, trust: str) -> None:
    mem.conn.execute("UPDATE cases SET trust_level = ? WHERE id = ?", (normalize_trust(trust), case_id))
