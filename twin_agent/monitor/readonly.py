"""A read-only view of Principal's state.db for the Twin Monitor.

Two locks, either one is enough:
1. the connection is opened with the sqlite URI `mode=ro` (the file cannot be written through it);
2. an authorizer denies every action that is not a read (no INSERT, UPDATE, DELETE, CREATE, PRAGMA writes, ATTACH).
It never creates the schema, never switches WAL on and never logs an event: opening it changes no byte on disk.
It offers only what the Monitor and the reused Watcher helpers need: `_all`, `_one`, `events`, `thread`,
`send_by_remote`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


class ReadOnlyViolation(Exception):
    pass


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in READ_ACTIONS else sqlite3.SQLITE_DENY


def connect_ro(path: Path) -> sqlite3.Connection:
    """Open a sqlite file read-only (mode=ro + deny-all-writes authorizer)."""
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"no database at {path.name}")
    # With no -wal file nobody is writing: `immutable=1` reads without creating the -wal/-shm side files.
    # With a live writer (the listener) a -wal exists and plain mode=ro reads it consistently.
    wal = path.with_name(path.name + "-wal")
    uri = path.as_uri() + ("?mode=ro" if wal.exists() else "?mode=ro&immutable=1")
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_authorizer)
    return conn


class ReadOnlyState:
    """Duck-types the read side of StateDB that the Watcher helpers use. Writes are impossible."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = connect_ro(self.path)

    def close(self) -> None:
        self.conn.close()

    def _all(self, sql: str, args=()) -> list[dict]:
        try:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]
        except sqlite3.DatabaseError as e:
            if "not authorized" in str(e) or "readonly" in str(e).lower():
                raise ReadOnlyViolation("the monitor may only read state.db") from None
            raise

    def _one(self, sql: str, args=()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def events(self, request_id: str | None = None, kind: str | None = None, *, after_id: int = 0) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE id > ?", [int(after_id)]
        if request_id is not None:
            sql, args = sql + " AND request_id = ?", args + [request_id]
        if kind is not None:
            sql, args = sql + " AND kind = ?", args + [kind]
        return self._all(sql + " ORDER BY id", args)

    def thread(self, request_id: str) -> list[dict]:
        return self._all("SELECT * FROM messages WHERE request_id = ? ORDER BY ts", (request_id,))

    def send_by_remote(self, chat_id, remote_id) -> dict | None:
        return self._one("SELECT * FROM sends WHERE remote_id = ? AND json_extract(target_json, '$.chat_id') = ?",
                         (str(remote_id), str(chat_id)))

    # Anything that would write is simply not here. These names exist so a mistaken call fails loudly.
    def log_event(self, *a, **k):
        raise ReadOnlyViolation("the monitor never writes to Principal's state")

    update_request = transition = add_card = finish_send = claim_send = set_dial = set_cursor = log_event
