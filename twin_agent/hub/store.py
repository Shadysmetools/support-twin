"""Durable hub state behind one tiny interface (architecture §3: SQLite WAL is the Phase-1 store).

``DocStore`` is a namespaced JSON-document store with exactly the four operations the hub needs. Namespaces in use:
``tickets`` (one doc per ticket, key = Odoo ticket id), ``messages`` (one doc per Meta message id: status + the
parsed inbound, so a failed message can be re-driven), ``approvals`` (one doc per nonce), ``holds`` (the human
queue), ``odoo_audit`` (who acted in Odoo, append-only) and ``pending_notes`` (notes blocked on a missing key). ``MemoryStore`` is the CI fake; ``SqliteStore`` is the real one (stdlib sqlite3, WAL). Both are thread-safe.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from copy import deepcopy
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class DocStore(Protocol):
    def put(self, ns: str, key: str, doc: dict) -> None: ...
    def get(self, ns: str, key: str) -> dict | None: ...
    def put_if_absent(self, ns: str, key: str, doc: dict) -> bool: ...
    def update(self, ns: str, key: str, fn: Callable[[dict], dict]) -> dict | None: ...
    def scan(self, ns: str) -> list[dict]: ...


AUDIT_NS = "odoo_audit"


def audit_append(store, row: dict, now=None) -> None:
    """One audit row per Odoo action (who acted, on whose behalf, what, outcome). The caller passes only names, ids
    and plain reasons: never a key, login, phone number or message id. Rows are append-only (a unique key)."""
    import uuid
    from datetime import datetime, timezone
    ts = (now or datetime.now(timezone.utc)).isoformat()
    store.put(AUDIT_NS, f"{ts}-{uuid.uuid4().hex[:8]}", {"ts": ts, **row})


def audit_rows(store) -> list[dict]:
    return sorted(store.scan(AUDIT_NS), key=lambda r: r.get("ts", ""))


class MemoryStore:
    """Dict-backed fake. Every read returns a deep copy, so callers can never mutate stored state by accident."""

    def __init__(self):
        self._d: dict[str, dict[str, dict]] = {}
        self._lock = threading.RLock()

    def put(self, ns, key, doc):
        with self._lock:
            self._d.setdefault(ns, {})[str(key)] = deepcopy(doc)

    def get(self, ns, key):
        with self._lock:
            d = self._d.get(ns, {}).get(str(key))
            return deepcopy(d) if d is not None else None

    def put_if_absent(self, ns, key, doc):
        with self._lock:
            bucket = self._d.setdefault(ns, {})
            if str(key) in bucket:
                return False
            bucket[str(key)] = deepcopy(doc)
            return True

    def update(self, ns, key, fn):
        with self._lock:
            cur = self._d.get(ns, {}).get(str(key))
            if cur is None:
                return None
            new = fn(deepcopy(cur))
            self._d[ns][str(key)] = deepcopy(new)
            return deepcopy(new)

    def scan(self, ns):
        with self._lock:
            return [deepcopy(v) for v in self._d.get(ns, {}).values()]

    def close(self) -> None:
        return None


class SqliteStore:
    """sqlite3 + WAL. ``update`` runs inside ``BEGIN IMMEDIATE`` so a read-modify-write is atomic across processes."""

    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._path = path
        # PII at rest (phone numbers, names, message text): owner-only files, including the WAL side files.
        # TODO(PDPL sign-off, OPEN - Sysadmin): retention/erasure purge for messages, holds and unknown-sender inbound.
        if path != ":memory:":
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._c.execute("CREATE TABLE IF NOT EXISTS docs (ns TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, "
                        "PRIMARY KEY (ns, key))")
        self._secure()

    def lock_exclusive(self) -> None:
        """Take an OS file lock on ``<db>.lock`` (released on close or process death): a SECOND hub process on the
        same state file is refused, because the one-ticket / one-ack guarantees are per process."""
        if self._path == ":memory:" or getattr(self, "_lockf", None):
            return
        fd = os.open(self._path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        f = os.fdopen(fd, "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise RuntimeError("another hub process already uses this SQLite state file") from None
        self._lockf = f

    def _secure(self) -> None:
        if self._path == ":memory:":
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self._path + suffix, 0o600)
            except OSError:
                pass                                 # a side file may not exist yet; put() re-applies below

    def put(self, ns, key, doc):
        with self._lock:
            self._secure()
            self._c.execute("INSERT INTO docs (ns, key, body) VALUES (?,?,?) "
                            "ON CONFLICT(ns, key) DO UPDATE SET body = excluded.body",
                            (ns, str(key), json.dumps(doc)))

    def get(self, ns, key):
        with self._lock:
            row = self._c.execute("SELECT body FROM docs WHERE ns=? AND key=?", (ns, str(key))).fetchone()
        return json.loads(row[0]) if row else None

    def put_if_absent(self, ns, key, doc):
        with self._lock:
            cur = self._c.execute("INSERT OR IGNORE INTO docs (ns, key, body) VALUES (?,?,?)",
                                  (ns, str(key), json.dumps(doc)))
            return cur.rowcount == 1

    def update(self, ns, key, fn):
        with self._lock:
            self._c.execute("BEGIN IMMEDIATE")
            try:
                row = self._c.execute("SELECT body FROM docs WHERE ns=? AND key=?", (ns, str(key))).fetchone()
                if row is None:
                    self._c.execute("ROLLBACK")
                    return None
                new = fn(json.loads(row[0]))
                self._c.execute("UPDATE docs SET body=? WHERE ns=? AND key=?", (json.dumps(new), ns, str(key)))
                self._c.execute("COMMIT")
                return new
            except BaseException:
                self._c.execute("ROLLBACK")
                raise

    def scan(self, ns):
        with self._lock:
            rows = self._c.execute("SELECT body FROM docs WHERE ns=? ORDER BY rowid", (ns,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._c.close()
            f, self._lockf = getattr(self, "_lockf", None), None
            if f:
                f.close()
