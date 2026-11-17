"""Hash-chained, append-only JSONL audit log (CONTRACT section 9.3).

Files: logs/vault-audit.jsonl (every vault use) and logs/actions.jsonl (sends, approvals, dial changes).

Each line is one JSON object. It carries the hash of the line before it (prev_hash) and its own hash
(sha256 of the canonical JSON of every other field). Editing or deleting any line breaks the chain, and
verify() reports the first broken line. String values are redacted BEFORE hashing, so verify() recomputes
over exactly what is on disk and no secret ever reaches the file.

Two processes may append to the same file (the creds CLI and the listener), so every append takes a
small lock file and reads the last hash fresh from disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock
from .models import utc_iso
from .redact import redact

GENESIS = "0" * 64


def canonical(record: dict) -> str:
    """Stable JSON text used for hashing: keys in order, no spaces, real unicode."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def record_hash(record_without_hash: dict) -> str:
    return hashlib.sha256(canonical(record_without_hash).encode("utf-8")).hexdigest()


def _redact_deep(value: Any, vault_hashes: Iterable[str]) -> Any:
    """Redact every string inside a record (keys are left alone; they are our own field names)."""
    if isinstance(value, str):
        return redact(value, vault_hashes)
    if isinstance(value, dict):
        return {str(k): _redact_deep(v, vault_hashes) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v, vault_hashes) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value), vault_hashes)


@contextmanager
def _file_lock(lock_path: Path, timeout_s: float = 10.0):
    """Cross-process lock: create a lock file exclusively; remove it when done.

    A lock file older than 60 s is treated as left over from a crash and taken over.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 60:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"audit log lock busy: {lock_path.name}")
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def _last_hash(path: Path) -> str:
    """Hash of the last good line, or GENESIS for a new file."""
    if not path.exists():
        return GENESIS
    last = GENESIS
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line).get("hash", last)
            except json.JSONDecodeError:
                continue
    return last


class AuditLog:
    """Append-only, hash-chained log. Never stores secret values (everything is redacted first)."""

    def __init__(self, path: Path, clock: Clock, vault_hashes: Iterable[str] = ()):
        self.path, self.clock = Path(path), clock
        self.vault_hashes = set(vault_hashes)   # optional: exact vault-value hashes to redact as well

    def append(self, record: dict) -> str:
        """Add ts, prev_hash, hash; redact; write one line. Returns the new hash."""
        body = _redact_deep(dict(record), self.vault_hashes)
        body.pop("hash", None)
        body.pop("prev_hash", None)
        body.setdefault("ts", utc_iso(self.clock.now()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.path.with_name(self.path.name + ".lock")):
            body["prev_hash"] = _last_hash(self.path)
            body["hash"] = record_hash(body)
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(canonical(body) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return body["hash"]

    def verify(self) -> tuple[bool, int | None]:
        """(ok, first bad line number, 1-based). A missing or empty file is ok."""
        if not self.path.exists():
            return True, None
        prev = GENESIS
        with self.path.open("r", encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                if not line.strip():
                    return False, n            # blank or truncated line: the file was edited
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    return False, n
                if not isinstance(rec, dict) or rec.get("prev_hash") != prev:
                    return False, n
                claimed = rec.pop("hash", None)
                if claimed != record_hash(rec):
                    return False, n
                prev = claimed
        return True, None

    def count(self, since_iso: str | None = None) -> int:
        """Number of lines (optionally with ts >= since_iso, UTC ISO). Used for the daily vault-read count."""
        if not self.path.exists():
            return 0
        n = 0
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_iso is None or str(rec.get("ts", "")) >= since_iso:
                    n += 1
        return n
