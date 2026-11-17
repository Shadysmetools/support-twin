"""Work-site history from the Digital Twin profile (read by the Watcher / timesheets).

- Gate: consent D4 (`consent.d4_watcher_ref`) + `browser.history.enabled`.
- Chrome keeps `History` open, so it is COPIED to a private temp folder first, opened with sqlite `mode=ro`,
  read, and the copy is deleted. The live file is never opened for writing.
- Only visits whose host is on `browser.history.work_domains` (exact or a subdomain) are returned. The query string
  is dropped except the `id` / `model` keys (Odoo record links); fragments keep only `id=` / `model=` too. Titles
  are redacted and cut to 80 characters. Chrome time = microseconds since 1601-01-01 UTC.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..clock import utc
from ..redact import redact

EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
KEEP_KEYS = {"id", "model"}


def to_chrome(t: datetime) -> int:
    return int((utc(t) - EPOCH) / timedelta(microseconds=1))


def from_chrome(us: int) -> datetime:
    return EPOCH + timedelta(microseconds=int(us))


def profile_dir(cfg: dict) -> Path:
    from .. import paths
    b = cfg.get("browser") or {}
    return Path(b.get("user_data_dir") or paths.home() / "browser" / "DigitalTwin")


def allowed_host(host: str, domains) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == d.lower() or h.endswith("." + d.lower()) for d in domains or [])


def clean_path(url: str) -> tuple[str, str]:
    u = urlsplit(url)
    q = urlencode([(k, v) for k, v in parse_qsl(u.query) if k in KEEP_KEYS])
    frag = urlencode([(k, v) for k, v in parse_qsl(u.fragment) if k in KEEP_KEYS])
    return (u.hostname or "").lower(), u.path + (f"?{q}" if q else "") + (f"#{frag}" if frag else "")


def enabled(cfg: dict) -> bool:
    from ..watcher.gate import consent_ok
    return consent_ok(cfg) and ((cfg.get("browser") or {}).get("history") or {}).get("enabled") is True


def visits(cfg: dict, since: datetime, until: datetime, *, history_file: Path | None = None) -> list[dict]:
    if not enabled(cfg):
        return []
    src = Path(history_file or profile_dir(cfg) / "Default" / "History")
    if not src.exists():
        return []
    domains = ((cfg.get("browser") or {}).get("history") or {}).get("work_domains") or []
    tmp = Path(tempfile.mkdtemp(prefix="da-hist-"))
    try:
        copy = tmp / "History"
        shutil.copyfile(src, copy)
        con = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT v.visit_time, u.url, u.title FROM visits v JOIN urls u ON u.id = v.url "
                               "WHERE v.visit_time >= ? AND v.visit_time < ? ORDER BY v.visit_time",
                               (to_chrome(since), to_chrome(until))).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = []
    for us, url, title in rows:
        host, path = clean_path(str(url or ""))
        if not allowed_host(host, domains):
            continue
        out.append({"ts": from_chrome(us), "domain": host, "path": path, "title": redact(str(title or ""))[:80]})
    return out
