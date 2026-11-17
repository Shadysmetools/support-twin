"""Search kb.db and read runbook entries. Read-only: the database is opened with mode=ro.

CLI (python -I aa.py kb-search "query" [--k 8] [--kind runbook ...] [--db <kb.db>]).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .. import paths

KINDS = ("runbook", "memory", "steering", "triage", "telegram", "email", "persona")
_STOP = {"the", "and", "for", "with", "this", "that", "from", "are", "was", "but", "not", "you", "please",
         "can", "how", "why", "what", "when", "have", "has", "his", "her", "its", "into", "on", "in", "to",
         "of", "is", "it", "a", "an", "or", "be", "i", "we", "el", "w"}


@dataclass
class Hit:
    kind: str
    doc_id: str
    title: str
    snippet: str
    status: str
    score: float


def _connect(db_path: Path) -> sqlite3.Connection:
    p = Path(db_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"kb.db not found: {p} (run kb-build first)")
    return sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query: every word quoted, joined with OR.

    Record refs like WAVE/06733 also add an exact phrase ("wave 06733") so the exact record ranks first.
    Returns "" when nothing searchable is left.
    """
    words = [w for w in re.findall(r"\w+", query.lower()) if w not in _STOP and (len(w) > 1 or w.isdigit())]
    terms = list(dict.fromkeys(f'"{w}"' for w in words))
    every = re.findall(r"\w+", query.lower())
    if 3 <= len(every) <= 16:   # the exact error text as a phrase ranks the entry that quotes it first
        terms.insert(0, '"' + " ".join(every) + '"')
    for ref in re.findall(r"\b[A-Za-z]{2,6}(?:/[A-Za-z0-9]+)+\b", query):
        parts = re.findall(r"\w+", ref.lower())
        terms.append('"' + " ".join(parts) + '"')
    return " OR ".join(terms[:40])


def _query(con, match: str, kinds: list[str], k: int, runbook_first: bool) -> list[tuple]:
    sql = ("SELECT d.kind, d.doc_id, d.title, snippet(docs_fts, 1, '[', ']', ' ... ', 24), d.status, "
           "bm25(docs_fts, 3.0, 1.0) AS r FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid "
           "WHERE docs_fts MATCH ?")
    args: list = [match]
    if kinds:
        sql += f" AND d.kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    sql += " ORDER BY " + ("(d.kind != 'runbook'), " if runbook_first else "") + "r LIMIT ?"
    args.append(k)
    return con.execute(sql, args).fetchall()


def search(query: str, *, db_path: Path, k: int = 8, kinds: Iterable[str] | None = None) -> list[Hit]:
    """Best k docs for the query. Two passes: docs quoting the whole query as a phrase come first
    (runbook entries before the rest: runbook first), then any-word matches by bm25 (title weighted 3x)."""
    q = fts_query(query)
    if not q:
        return []
    kinds = [x for x in (kinds or []) if x in KINDS]
    k = max(1, min(int(k), 50))
    phrase = q.split(" OR ")[0] if q.startswith('"') and " " in q.split(" OR ")[0] else None
    con = _connect(db_path)
    try:
        rows = _query(con, phrase, kinds, k, True) if phrase else []
        seen = {r[1] for r in rows}
        rows += [r for r in _query(con, q, kinds, k, False) if r[1] not in seen]
    finally:
        con.close()
    return [Hit(kind, doc_id, title or "", snip or "", status or "", round(-r, 4))
            for kind, doc_id, title, snip, status, r in rows[:k]]


def runbook_entries(db_path: Path) -> dict[str, dict]:
    """slug -> {title, signature, status, confirmed_date, tags} (plus status_raw, fix, root_cause)."""
    con = _connect(db_path)
    try:
        rows = con.execute("SELECT slug, title, signature, status, confirmed_date, tags, status_raw, fix, "
                           "root_cause FROM runbook").fetchall()
    finally:
        con.close()
    keys = ("title", "signature", "status", "confirmed_date", "tags", "status_raw", "fix", "root_cause")
    return {r[0]: dict(zip(keys, r[1:])) for r in rows}


def get_doc(doc_id: str, *, db_path: Path) -> dict | None:
    """The full (already redacted) doc for a hit."""
    con = _connect(db_path)
    try:
        r = con.execute("SELECT kind, doc_id, title, body, status, date, source FROM docs WHERE doc_id = ?",
                        (doc_id,)).fetchone()
    finally:
        con.close()
    return dict(zip(("kind", "doc_id", "title", "body", "status", "date", "source"), r)) if r else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb-search")
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--kind", action="append", choices=KINDS)
    ap.add_argument("--db", type=Path, default=None)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows console is cp1252
    for h in search(a.query, db_path=a.db or paths.kb_db(), k=a.k, kinds=a.kind):
        print(f"[{h.kind}] {h.doc_id}  ({h.status or '-'}, {h.score})\n  {h.title}\n  {h.snippet}\n")
    return 0
