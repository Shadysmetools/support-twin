"""Consent and privacy gate for the Watcher (Principal's written consent D4, 05 / HANDOFF 2026-11-02).

Every capability needs ALL of: `consent.d4_watcher_ref` set, `watcher.enabled: true`, its own flag on, and the
moment not inside a /pause window. Anything unreadable answers "no" (fail closed).

/pause is stored as timestamped events (`watcher_pause` / `watcher_unpause`), not as a flag, because capture runs
later at export time: anything stamped inside a paused stretch is left out even after Principal unpauses.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .. import paths
from ..clock import utc
from ..models import parse_ts

CAPTURE = ("card_edits", "outgoing_messages", "shell", "desk_sessions", "time_spent")
CAPABILITIES = CAPTURE + ("screen_record", "sync")
PAUSE, UNPAUSE = "watcher_pause", "watcher_unpause"

Window = tuple[datetime, datetime | None]          # (start, end); end None = still paused


def consent_ok(cfg: dict) -> bool:
    ref = (cfg.get("consent") or {}).get("d4_watcher_ref")
    return bool(ref and str(ref).strip() and str(ref).strip().lower() not in ("null", "none", "false"))


def _w(cfg: dict) -> dict:
    return cfg.get("watcher") or {}


def capability_on(cfg: dict, capability: str) -> bool:
    """Consent + master switch + the capability's own flag (no pause check)."""
    if capability not in CAPABILITIES or not consent_ok(cfg) or _w(cfg).get("enabled") is not True:
        return False
    w = _w(cfg)
    if capability == "screen_record":
        return (w.get("screen_record") or {}).get("enabled") is True
    if capability == "sync":
        return (w.get("sync") or {}).get("enabled") is True
    return (w.get("capture") or {}).get(capability) is True


def windows_from_rows(rows: list[dict], now: datetime | None = None) -> list[Window]:
    """Pause windows from events rows (kind, ts, detail_json). A pause may carry `until` (auto-unpause)."""
    out: list[Window] = []
    start: datetime | None = None
    end_at: datetime | None = None
    parsed = [(parse_ts(r.get("ts")), r) for r in rows]
    for ts, r in sorted(((t, r) for t, r in parsed if t is not None),     # by instant, not by text
                        key=lambda p: (utc(p[0]), p[1].get("id") or 0)):
        if start is not None and end_at is not None and utc(ts) >= utc(end_at):   # a timed pause ran out on its own
            out.append((start, end_at))
            start = end_at = None
        if r.get("kind") == PAUSE and start is None:
            start = ts
            try:
                end_at = parse_ts(json.loads(r.get("detail_json") or "{}").get("until"))
            except (ValueError, TypeError, AttributeError):
                end_at = None
        elif r.get("kind") == UNPAUSE and start is not None:
            out.append((start, ts))
            start = end_at = None
    if start is not None:
        out.append((start, end_at))
    return out


def in_windows(windows: list[Window], ts: datetime | None) -> bool:
    """True if ts falls in a pause window. No timestamp = treat as paused (fail closed)."""
    if ts is None:
        return True
    t = utc(ts)                                            # instants: right inside the repeated hour too
    return any(utc(s) <= t and (e is None or t < utc(e)) for s, e in windows)


def pause_windows(db, now: datetime) -> list[Window] | None:
    """Pause windows from state.db; None if they cannot be read (callers then capture nothing)."""
    try:
        rows = db.events(None, PAUSE) + db.events(None, UNPAUSE)
    except Exception:                                       # noqa: BLE001 - fail closed
        return None
    return windows_from_rows(rows, now)


def paused_now(db, now: datetime) -> bool:
    w = pause_windows(db, now)
    return True if w is None else in_windows(w, now)


def allowed(cfg: dict, db, capability: str, now: datetime) -> bool:
    """The one question every Watcher capability asks before it does anything."""
    try:
        return capability_on(cfg, capability) and not paused_now(db, now)
    except Exception:                                       # noqa: BLE001 - fail closed
        return False


def recording_file() -> Path:
    """Exists (holding the request id) only while a screen recording runs: the board shows it."""
    return paths.data_dir() / "watcher" / "RECORDING"


def indicator(cfg: dict | None, windows: list[Window] | None, now: datetime) -> str:
    """One line for the board and /status: what the Watcher is doing right now."""
    rec = recording_file()
    if rec.exists():
        rid = rec.read_text(encoding="utf-8", errors="replace").strip()[:20]
        return f"RECORDING SCREEN ({rid})"
    if windows is None or in_windows(windows, now):
        return "paused (/unpause to continue)" if windows is not None else "paused (state unreadable)"
    if cfg is None or not consent_ok(cfg) or _w(cfg).get("enabled") is not True:
        return "off"
    on = [c for c in CAPTURE if capability_on(cfg, c)]
    return f"watching work requests ({', '.join(on)})" if on else "off (no capture switched on)"
