"""memory.db: schema, connection and the shared helpers every store uses.

One SQLite file (default data/memory.db). Times are stored as UTC ISO text (models.utc_iso) so text sorts
correctly across Egypt's DST change, read back as Cairo (models.parse_ts). Rows come back as sqlite3.Row.

No WAL: the store is low-traffic and local, and recall opens it read-only (mode=ro), which a WAL database
cannot always do without write access to its -shm file. DELETE journal keeps read-only opens simple.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from .. import paths
from ..clock import Clock, SystemClock
from ..models import utc_iso

TRUST = ("GREEN", "AMBER", "RED")
TRUST_RANK = {"RED": 0, "AMBER": 1, "GREEN": 2}
# Sources that are a single hint, never independent proof: a GREEN upgrade needs a source outside this set.
WEAK_SOURCES = {"", "runbook", "replay"}

_STOP = {"the", "a", "an", "of", "to", "in", "on", "is", "it", "and", "or", "for", "with", "no", "not",
         "this", "that", "has", "have", "was", "are", "be", "at", "by", "as", "i", "we", "el", "w"}


def db_path() -> Path:
    """The memory database. The task names data/memory.db; data/ is gitignored."""
    return paths.data_dir() / "memory.db"


def normalize_trust(value: str | None) -> str:
    v = (value or "").strip().upper()
    return v if v in TRUST else "RED"


def tokens(text: str | None) -> set[str]:
    """Significant lowercase tokens (keeps record refs like in/4451/26 together-ish via the /)."""
    return {w for w in re.findall(r"[a-z0-9/]+", (text or "").lower()) if w not in _STOP and (len(w) > 1 or w.isdigit())}


def overlap(a: set[str], b: set[str]) -> float:
    """Token-overlap similarity (Jaccard). Embedding-free, deterministic. Used for clustering in reflect."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def coverage(want: set[str], doc: set[str]) -> float:
    """Share of the QUERY's tokens present in the document. Unlike Jaccard this does not penalise long
    documents (the denominator is the query size, constant across candidates), so a long, highly relevant
    entry is not out-ranked by a short, barely relevant one. Deterministic, no network."""
    if not want:
        return 0.0
    return len(want & doc) / len(want)


def accept_match(want: set[str], doc: set[str]) -> bool:
    """Noise floor for retrieval: a single shared common token ("po", "line") is not a match. Require at
    least two shared significant tokens, unless the query itself has fewer than two tokens."""
    shared = len(want & doc)
    if shared >= 2:
        return True
    return shared >= 1 and len(want) <= 2


def fts_query(text: str) -> str:
    """Free text -> a safe FTS5 query: each distinct word quoted, joined with OR. "" when nothing is left."""
    words = [w for w in re.findall(r"\w+", (text or "").lower()) if w not in _STOP and (len(w) > 1 or w.isdigit())]
    terms = list(dict.fromkeys(f'"{w}"' for w in words))
    return " OR ".join(terms[:40])


def dedupe_key(*parts: str) -> str:
    return hashlib.sha1("\u0001".join((p or "") for p in parts).encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY, request_ref TEXT, client TEXT, track TEXT, symptom TEXT,
  verified_root_cause TEXT, fix TEXT, evidence_refs TEXT, trust_level TEXT, outcome TEXT,
  source TEXT, ts TEXT, dedupe_key TEXT UNIQUE
);
CREATE VIRTUAL TABLE IF NOT EXISTS cases_fts USING fts5(symptom, verified_root_cause, fix,
                                                        tokenize='unicode61 remove_diacritics 2');

CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY, request_ref TEXT, ticket_ref TEXT, client TEXT, ts TEXT, kind TEXT, summary TEXT,
  src TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_ep_req ON episodes(request_ref);
CREATE INDEX IF NOT EXISTS ix_ep_ticket ON episodes(ticket_ref);
CREATE INDEX IF NOT EXISTS ix_ep_client ON episodes(client);

CREATE TABLE IF NOT EXISTS system_map (
  id INTEGER PRIMARY KEY, topic TEXT, fact TEXT, evidence TEXT, trust_level TEXT, source TEXT, ts TEXT,
  UNIQUE(topic, fact)
);
CREATE VIRTUAL TABLE IF NOT EXISTS system_map_fts USING fts5(topic, fact,
                                                            tokenize='unicode61 remove_diacritics 2');

CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY, context TEXT, what_changed TEXT, rule TEXT, ts TEXT, dedupe_key TEXT UNIQUE
);
CREATE VIRTUAL TABLE IF NOT EXISTS corrections_fts USING fts5(context, rule,
                                                             tokenize='unicode61 remove_diacritics 2');

CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY, ts TEXT, scope TEXT, lesson TEXT, basis TEXT, dedupe_key TEXT UNIQUE
);
"""


class MemoryDB:
    """Read/write handle. Creates the schema on open. Pass a Clock in tests; SystemClock otherwise."""

    def __init__(self, path: Path | None = None, clock: Clock | None = None):
        self.path = Path(path or db_path())
        self.clock = clock or SystemClock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def now(self) -> str:
        return utc_iso(self.clock.now())

    def close(self) -> None:
        self.conn.close()


def open_ro(path: Path | None = None) -> sqlite3.Connection:
    """A read-only connection for recall. Raises FileNotFoundError when the DB does not exist yet."""
    p = Path(path or db_path()).resolve()
    if not p.exists():
        raise FileNotFoundError(f"memory.db not found: {p} (run `aa.py memory bootstrap` first)")
    con = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def fts_set(conn: sqlite3.Connection, table: str, rowid: int, *values: str) -> None:
    """Upsert one external-less FTS5 row by rowid: delete any existing, then insert the fresh text."""
    cols = {"cases_fts": ("symptom", "verified_root_cause", "fix"),
            "system_map_fts": ("topic", "fact"),
            "corrections_fts": ("context", "rule")}[table]
    conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
    placeholders = ", ".join(["?"] * (len(cols) + 1))
    conn.execute(f"INSERT INTO {table} (rowid, {', '.join(cols)}) VALUES ({placeholders})",
                 (rowid, *values))
