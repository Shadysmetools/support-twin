"""Stream Claude Code session transcripts (~/.claude/projects/**/*.jsonl) into `Act` records.

Memory-safe on a small laptop: files are read line by line; a line is parsed only if it is a user or assistant
record (`api-request-blob` and other huge lines are skipped by a substring check first); files last written before
the window are skipped by mtime. Nothing is kept but the few fields an Act needs.

Human = Principal typed it: a `user` record from an external user with text content, not a tool_result, not a
sidechain (subagent) record, not a meta / command-output line. Everything else is assistant or tool activity.
Sessions run by the agent's own workers live under C:\\TwinAgent\\claude-config (never ~/.claude), and any cwd under
the agent home is skipped as well.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..clock import utc
from ..redact import has_secrets, redact
from .activity import Act, in_window, refs_in

WANTED = ('"type":"user"', '"type":"assistant"', '"type": "user"', '"type": "assistant"')
SKIP = ('"type":"api-request', '"type": "api-request')
NOT_HUMAN_PREFIX = ("<local-command-stdout", "<local-command-stderr", "<local-command-caveat", "<system-reminder",
                    "<task-notification", "Caveat:", "[Request interrupted")
HUMAN_ORIGINS = {"human"}                 # turnOrigin: task_notification / scheduled / peer / sdk are not Principal
HUMAN_SOURCES = {"typed", "queued"}       # promptSource: "system" / "sdk" are not Principal
READS = {"Read", "Grep", "Glob", "NotebookRead"}
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELLS = {"Bash", "PowerShell"}
PUSH = re.compile(r"\bgit\s+push\b(?:\s+-\S+)*(?:\s+\S+)?(?:\s+([\w./-]+))?")
WORDS = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def topic_of(text: str, n: int = 8) -> str:
    """A few plain words of a prompt for the description; dropped entirely if it carries anything secret-like."""
    t = redact(str(text or "")).strip()
    if has_secrets(t) or "[REDACTED" in t:
        return ""
    words = WORDS.findall(t)[:n]
    return " ".join(words)


def is_human_record(r: dict) -> bool:
    """Did Principal type this user record? Newer Claude Code marks it (turnOrigin / promptSource). Headless SDK runs,
    scheduled routines, /loop wake-ups, peer messages and task notifications are never his own time."""
    if r.get("isSidechain") or r.get("isMeta") or r.get("userType", "external") != "external":
        return False
    if r.get("turnOrigin") is not None:
        return r.get("turnOrigin") in HUMAN_ORIGINS
    if r.get("promptSource") is not None:
        return r.get("promptSource") in HUMAN_SOURCES
    return not str(r.get("entrypoint") or "").startswith("sdk")


def _human_text(msg: dict) -> str | None:
    content = (msg or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith(NOT_HUMAN_PREFIX):
        return None
    return text


def _tool_act(name: str, inp: dict) -> tuple[list[str], list[str], str]:
    """(files, actions, text-for-refs) of one tool_use block."""
    inp = inp if isinstance(inp, dict) else {}
    path = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
    if name in READS:
        line = inp.get("offset")
        tag = f"{Path(path).name}:{line}" if path and line else Path(path).name if path else str(inp.get("pattern") or "")[:40]
        # A read/grep records the path for the description (read:<tag>) but NOT in `files`: `activity.files` holds
        # EDITED paths only, so a mere read of a path_map module never bills its ticket (timesheet #2655).
        return [], [f"read:{tag}"], f"{path} {inp.get('pattern') or ''}"
    if name in EDITS:
        return ([path] if path else []), [f"edit:{Path(path).name}"], path
    if name in SHELLS:
        cmd = redact(str(inp.get("command") or ""))[:300]
        acts = []
        m = PUSH.search(cmd)
        if m:
            acts.append(f"push:{m.group(1) or 'branch'}")
        elif re.search(r"\bgit\s+commit\b", cmd):
            acts.append("commit")
        if re.search(r"\bgh\s+pr\s+create\b", cmd):
            acts.append("pr_open")
        if re.search(r"\b(pytest|npm test|odoo-bin.*--test)", cmd):
            acts.append("tests")
        return [], acts or ["shell"], cmd
    if name.startswith("mcp__"):
        return [], [f"tool:{name.split('__')[-1][:30]}"], json.dumps(inp)[:300]
    return [], [f"tool:{name[:30]}"], ""


def acts_from_record(r: dict, *, skip_cwd: tuple[str, ...] = ()) -> list[Act]:
    ts = parse_ts(r.get("timestamp"))
    if ts is None:
        return []
    cwd, branch, sid = str(r.get("cwd") or ""), str(r.get("gitBranch") or ""), str(r.get("sessionId") or "?")
    if any(cwd.lower().startswith(x.lower()) for x in skip_cwd if x):
        return []
    base_refs = refs_in(cwd)                          # the branch is kept apart: it is a weaker signal
    msg = r.get("message") or {}
    if r.get("type") == "user":
        text = _human_text(msg) if is_human_record(r) else None
        if text is None:
            return [Act(ts=ts, session=sid, human=False, kind="tool", branch=branch, cwd=cwd, refs=base_refs)]
        return [Act(ts=ts, session=sid, human=True, kind="prompt", branch=branch, cwd=cwd,
                    refs=refs_in(text) + base_refs, topic=topic_of(text))]
    files, actions, reftext = [], [], ""
    for b in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            f, a, t = _tool_act(str(b.get("name") or ""), b.get("input") or {})
            files += f
            actions += a
            reftext += " " + t
    return [Act(ts=ts, session=sid, human=False, kind="tool" if actions else "assistant", branch=branch, cwd=cwd,
                files=files, actions=actions, refs=refs_in(reftext) + base_refs)]


def iter_acts(root: Path, since: datetime, until: datetime, *, skip_cwd: tuple[str, ...] = ()) -> Iterator[Act]:
    root = Path(root)
    if not root.exists():
        return
    for f in root.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < utc(since):
                continue                                   # last written before the window: nothing inside
            fh = f.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                head = line[:400]
                if any(s in head for s in SKIP) or not any(w in line for w in WANTED):
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") not in ("user", "assistant"):
                    continue
                for a in acts_from_record(r, skip_cwd=skip_cwd):
                    if in_window(a.ts, since, until):
                        yield a
