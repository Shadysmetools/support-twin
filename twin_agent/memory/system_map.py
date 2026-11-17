"""System-map memory: facts about how Globex's Odoo / modules are wired, learned from investigations.

Each fact carries evidence (file:line / doc ref / record ids) and a trust level. Example:
"custom_receipt_validation raises the PO-ref validate error; the PO field reads the first move by sequence".
Retrieval: facts(topic) via FTS5 + token overlap. Every field is redacted before storage. A fact is unique
by (topic, fact): re-adding it updates evidence/trust/source rather than duplicating (idempotent bootstrap).
"""
from __future__ import annotations

import sqlite3

from ..redact import redact
from .store import MemoryDB, accept_match, coverage, fts_query, fts_set, normalize_trust, tokens


def add_fact(mem: MemoryDB, *, topic: str, fact: str, evidence: str = "", trust: str = "RED",
             source: str = "") -> int:
    """Store or update one fact (redacted). Returns the row id."""
    topic_r, fact_r = redact(topic or ""), redact(fact or "")
    evidence_r, source_r, trust_n = redact(evidence or ""), redact(source or ""), normalize_trust(trust)
    ts = mem.now()
    row = mem.conn.execute("SELECT id FROM system_map WHERE topic = ? AND fact = ?", (topic_r, fact_r)).fetchone()
    if row:
        rowid = row["id"]
        mem.conn.execute("UPDATE system_map SET evidence = ?, trust_level = ?, source = ?, ts = ? WHERE id = ?",
                         (evidence_r, trust_n, source_r, ts, rowid))
    else:
        cur = mem.conn.execute(
            "INSERT INTO system_map (topic, fact, evidence, trust_level, source, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (topic_r, fact_r, evidence_r, trust_n, source_r, ts))
        rowid = cur.lastrowid
    fts_set(mem.conn, "system_map_fts", rowid, topic_r, fact_r)
    return rowid


def facts(conn: sqlite3.Connection, topic: str, k: int = 8) -> list[dict]:
    """The k most relevant facts for a topic/query. FTS5 candidates reranked by token overlap."""
    k = max(1, min(int(k), 50))
    q = fts_query(topic)
    want = tokens(topic)
    rows: list[sqlite3.Row] = []
    if q:
        try:
            rows = conn.execute(
                "SELECT s.* FROM system_map s JOIN system_map_fts f ON f.rowid = s.id "
                "WHERE system_map_fts MATCH ? LIMIT 200", (q,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    out = []
    for r in rows:
        doc = tokens((r["topic"] or "") + " " + (r["fact"] or ""))
        if accept_match(want, doc):
            d = dict(r)
            d["score"] = round(coverage(want, doc), 4)
            out.append(d)
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:k]


def all_facts(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM system_map ORDER BY id").fetchall()]


def set_trust(mem: MemoryDB, fact_id: int, trust: str) -> None:
    mem.conn.execute("UPDATE system_map SET trust_level = ? WHERE id = ?", (normalize_trust(trust), fact_id))
