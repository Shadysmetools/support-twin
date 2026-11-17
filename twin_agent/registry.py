"""Live session registry: which lanes have an open Claude session, for Principal's visibility (doc 15 "the twin's
board"). A tiny JSON store under data/state. No sends, no db writes - just a record the launcher updates and
Principal (or a status card) can list.

Times come from a Clock (CONTRACT hard rule 8: never datetime.now() directly), stored as UTC ISO via
models.utc_iso, like everything else written to disk.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths
from .clock import SystemClock
from .models import utc_iso
from .redact import redact

# A last-task hint is stored on disk, so it is redacted (like drop_task) and kept short - a glance, not a transcript.
_TASK_HINT_MAX = 200

# A lane is PREPARED (observe wrote its INBOX + command, no tab) or OPEN (a real visible tab launched and running),
# then IDLE / CLOSED. is_open means a REAL running tab, so a prepared/failed lane never blocks a later live launch.
PREPARED, OPEN, IDLE, CLOSED = "prepared", "open", "idle", "closed"


def registry_path() -> Path:
    return paths.data_dir() / "state" / "lane_sessions.json"


def _hint(task: str) -> str:
    """A short, redacted one-line hint of a task for the board (it is written to disk, like drop_task's INBOX)."""
    return redact(" ".join((task or "").split()))[:_TASK_HINT_MAX]


class LaneRegistry:
    """Track lane sessions in one JSON file: {lane: {status, title, cmd, opened_at, updated_at, ...}}."""

    def __init__(self, path: Path | None = None, clock=None):
        self.path = path or registry_path()
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _now(self) -> str:
        return utc_iso(self.clock.now())

    # ------------------------------------------------------------ api
    def add(self, lane: str, *, title: str = "", cmd: str = "", launched: bool = False,
            status: str | None = None, last_task: str | None = None, tags=None, **meta) -> dict:
        """Record (or refresh) a lane. `launched` True = a real tab started now (status OPEN); False = observe
        PREPARED. The `launched` FLAG is sticky (ever-launched survives), but the STATUS reflects THIS call, so an
        observe-prepared lane is never OPEN and so never blocks a later live launch. An explicit `status` wins.

        `last_task` is a short hint of the task just dropped (redacted + truncated, it lands on disk); `tags` are the
        knowledge hint Digital Twin routes by (correction #12). The free-text `status_text` set by mark_status is
        kept SEPARATE from the lifecycle `status`, so a status note never flips a lane out of OPEN."""
        data = self._load()
        now = self._now()
        row = data.get(lane) or {}
        row.update({"lane": lane, "status": status or (OPEN if launched else PREPARED),
                    "title": title or row.get("title") or lane, "cmd": cmd or row.get("cmd", ""),
                    "launched": bool(launched) or bool(row.get("launched")), "updated_at": now})
        row.setdefault("opened_at", now)
        if last_task is not None:
            row["last_task"] = _hint(last_task)
        if tags is not None:
            row["tags"] = [str(t) for t in tags]
        for k, v in meta.items():
            row[k] = v
        data[lane] = row
        self._save(data)
        return row

    # -- thin wrappers so the vocabulary reads register / open / close (the launcher already calls add/close) --
    def register(self, lane: str, **kw) -> dict:
        """Record a lane without claiming a real tab (observe PREPARED unless launched=True is passed)."""
        return self.add(lane, **kw)

    def open(self, lane: str, **kw) -> dict:
        """Mark a real visible tab open for this lane (launched sticky, status OPEN)."""
        kw.pop("launched", None)
        return self.add(lane, launched=True, **kw)

    def touch(self, lane: str) -> dict | None:
        """Bump updated_at without changing status (an INBOX drop onto an already-open lane)."""
        data = self._load()
        row = data.get(lane)
        if row is None:
            return None
        row["updated_at"] = self._now()
        data[lane] = row
        self._save(data)
        return row

    def get(self, lane: str) -> dict | None:
        return self._load().get(lane)

    def is_open(self, lane: str) -> bool:
        return (self.get(lane) or {}).get("status") == OPEN

    def ever_launched(self, lane: str) -> bool:
        """True once a real tab has started for this lane (so a later tab should `claude --continue`)."""
        return bool((self.get(lane) or {}).get("launched"))

    def list(self, *, status: str | None = None) -> list[dict]:
        rows = sorted(self._load().values(), key=lambda r: r.get("updated_at", ""), reverse=True)
        return [r for r in rows if status is None or r.get("status") == status]

    def list_open(self) -> list[dict]:
        return self.list(status=OPEN)

    def list_sessions(self) -> list[dict]:
        """Every lane Digital Twin can SEE (correction #12): open AND prepared, newest first, each with its
        lifecycle status, free-text status_text, last_task hint and knowledge tags."""
        return self.list()

    def mark_status(self, lane: str, text: str) -> dict | None:
        """Set a short free-text status (what the lane is doing now), SEPARATE from the lifecycle status so it
        never flips a lane out of OPEN. None if the lane is unknown."""
        data = self._load()
        row = data.get(lane)
        if row is None:
            return None
        row["status_text"] = str(text or "")
        row["updated_at"] = self._now()
        data[lane] = row
        self._save(data)
        return row

    def note_task(self, lane: str, task: str) -> dict | None:
        """Refresh the last_task hint (redacted + truncated) without changing lifecycle status - used when a task is
        dropped onto an already-open lane (the inbox-only path). None if the lane is unknown."""
        data = self._load()
        row = data.get(lane)
        if row is None:
            return None
        row["last_task"] = _hint(task)
        row["updated_at"] = self._now()
        data[lane] = row
        self._save(data)
        return row

    def knows(self, lane: str, topic: str) -> bool:
        """Does this lane's knowledge hint (its tags) match the topic? Word-boundary, case-insensitive - never a
        substring (comms_router deliberately avoids 'gate' inside 'investigate'; 'delivery' must not hit
        'deliverables'). An unknown lane knows nothing."""
        row = self.get(lane)
        if not row:
            return False
        text = (topic or "").lower()
        for tag in row.get("tags") or []:
            t = str(tag).strip().lower()
            if t and re.search(r"\b" + re.escape(t) + r"\b", text):
                return True
        return False

    def mark_idle(self, lane: str) -> dict | None:
        return self._set_status(lane, IDLE)

    def close(self, lane: str) -> dict | None:
        return self._set_status(lane, CLOSED)

    def _set_status(self, lane: str, status: str) -> dict | None:
        data = self._load()
        row = data.get(lane)
        if row is None:
            return None
        row["status"] = status
        row["updated_at"] = self._now()
        data[lane] = row
        self._save(data)
        return row
