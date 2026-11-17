"""Principal's own Claude desk sessions for one request (~/.claude/projects/*/*.jsonl), read-only.

A session is kept ONLY if Principal tagged it for the request (config `watcher.desk.tagged_sessions`:
{session_id: REQ-xxxx}) or one of its lines mentions the request ref as a whole word (REQ-0142, not REQ-01420).
Every other session is read line by line, dropped in memory and never written anywhere.
Only plain text turns and the commands Claude ran are kept; tool outputs are skipped (they can hold anything).
The agent's own worker sessions (`claude-config` under the install home) are never read.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .. import paths
from ..clock import elapsed
from ..models import parse_ts

MAX_TURNS = 200
MAX_TURN_CHARS = 2000


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def ref_pattern(refs: list[str]) -> re.Pattern[str] | None:
    refs = [r for r in refs if r]
    if not refs:
        return None
    return re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(r) for r in refs) + r")(?![\w])", re.I)


def _turn(obj: dict) -> list[tuple[str, str]]:
    """(role, text) pieces from one jsonl line: user/assistant text, and Bash/PowerShell commands Claude ran."""
    kind = obj.get("type")
    if kind not in ("user", "assistant"):
        return []
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [(kind, content)]
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            out.append((kind, part["text"]))
        elif part.get("type") == "tool_use" and part.get("name") in ("Bash", "PowerShell"):
            cmd = (part.get("input") or {}).get("command")
            if cmd:
                out.append(("command", str(cmd)))
    return out                                              # tool_result parts are skipped on purpose


def read_session(path: Path, pattern: re.Pattern[str] | None, tagged: bool) -> dict | None:
    """Parse one session file. None unless tagged or a line mentions the ref."""
    turns, mentioned, sid = [], False, path.stem
    first: datetime | None = None
    last: datetime | None = None
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            sid = obj.get("sessionId") or sid
            ts = parse_ts(obj.get("timestamp")) if obj.get("timestamp") else None
            for role, text in _turn(obj):
                if pattern is not None and pattern.search(text):
                    mentioned = True
                if len(turns) < MAX_TURNS:
                    turns.append({"role": role, "ts": obj.get("timestamp"), "text": text[:MAX_TURN_CHARS]})
                if ts is not None:
                    first = first or ts
                    last = ts
    if not (tagged or mentioned) or not turns:
        return None
    minutes = round(elapsed(first, last).total_seconds() / 60, 1) if first and last else None
    return {"session_id": hashlib.sha256(str(sid).encode()).hexdigest()[:12], "project": hashlib.sha256(path.parent.name.encode()).hexdigest()[:10],
            "tagged": tagged, "turns": turns, "started": obj_ts(first), "ended": obj_ts(last), "minutes": minutes}


def obj_ts(dt: datetime | None) -> str | None:
    from ..models import utc_iso
    return utc_iso(dt) if dt else None


def find_sessions(request_id: str, *, refs: list[str] = (), projects_dir: Path | None = None,
                  tagged: dict[str, str] | None = None) -> list[dict]:
    """Every desk session that belongs to this request (tagged, or mentions REQ id / extra refs like a ticket id)."""
    base = Path(projects_dir) if projects_dir else default_projects_dir()
    if not base.is_dir() or _inside(base, paths.home() / "claude-config"):
        return []
    tags = {sid for sid, rid in (tagged or {}).items() if str(rid).upper() == request_id.upper()}
    pattern = ref_pattern([request_id, *refs])
    out = []
    for p in sorted(base.glob("*/*.jsonl")):
        s = read_session(p, pattern, p.stem in tags)
        if s is not None:
            out.append(s)
    return out
