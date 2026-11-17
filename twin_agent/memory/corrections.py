"""Correction memory: one-line rules distilled from Principal's edits/rejects of drafts.

Signal source (never invented): a tap with action in {ed, sd} or a send with trigger "principal_edit" means he
edited; a reject tap means he declined (monitor/kpis.py decisions()). Each edit/reject becomes a rule:
{context, what_changed, rule, ts}. Retrieval: rules(context) via FTS5 + token overlap. All fields redacted.
"""
from __future__ import annotations

import sqlite3

from ..redact import redact
from .store import MemoryDB, accept_match, coverage, dedupe_key, fts_query, fts_set, tokens


def add_rule(mem: MemoryDB, *, context: str, what_changed: str, rule: str) -> int:
    """Store one correction rule (redacted, idempotent on context+rule). Returns the row id (0 if duplicate)."""
    ctx, changed, rule_r = redact(context or ""), redact(what_changed or ""), redact(rule or "")
    key = dedupe_key(ctx, rule_r)
    cur = mem.conn.execute(
        "INSERT OR IGNORE INTO corrections (context, what_changed, rule, ts, dedupe_key) VALUES (?, ?, ?, ?, ?)",
        (ctx, changed, rule_r, mem.now(), key))
    if cur.rowcount == 0:
        return 0
    fts_set(mem.conn, "corrections_fts", cur.lastrowid, ctx, rule_r)
    return cur.lastrowid


def record_edit(mem: MemoryDB, *, context: str, draft: str, final: str) -> int:
    """Principal edited a draft: capture the change as a rule ('prefer the edited phrasing in this context')."""
    changed = f"draft -> final: {redact(draft)[:120]} => {redact(final)[:120]}"
    rule = f"In '{redact(context)[:80]}', prefer: {redact(final)[:140]}"
    return add_rule(mem, context=context, what_changed=changed, rule=rule)


def record_reject(mem: MemoryDB, *, context: str, draft: str, reason: str = "") -> int:
    """Principal rejected a draft: capture it as a 'do not' rule."""
    changed = f"rejected draft: {redact(draft)[:160]}"
    rule = f"In '{redact(context)[:80]}', do NOT send that kind of draft" + (f" ({redact(reason)[:80]})" if reason else "")
    return add_rule(mem, context=context, what_changed=changed, rule=rule)


def rules(conn: sqlite3.Connection, context: str, k: int = 5) -> list[dict]:
    """The k most relevant correction rules for a context/query. FTS5 reranked by token overlap."""
    k = max(1, min(int(k), 50))
    q = fts_query(context)
    want = tokens(context)
    rows: list[sqlite3.Row] = []
    if q:
        try:
            rows = conn.execute(
                "SELECT c.* FROM corrections c JOIN corrections_fts f ON f.rowid = c.id "
                "WHERE corrections_fts MATCH ? LIMIT 200", (q,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    out = []
    for r in rows:
        doc = tokens((r["context"] or "") + " " + (r["rule"] or ""))
        if accept_match(want, doc):
            d = dict(r)
            d["score"] = round(coverage(want, doc), 4)
            out.append(d)
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:k]


def all_rules(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM corrections ORDER BY id").fetchall()]
