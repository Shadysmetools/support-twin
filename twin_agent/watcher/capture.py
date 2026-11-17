"""Capture side (Principal's laptop): what Principal did on one request, read from what the agent already stores.

Nothing here hooks the live path and nothing here writes: `build_case` is a read-only projection over state.db
(+ his desk sessions) that bundle.py redacts and pseudonymises before anything touches disk. Records:

- `tap`: his card button taps (events `button`), action code + time.                       capability card_edits
- `instruction`: his plain replies to cards (events `instruction`).                         capability card_edits
- `edit`: the agent's draft vs the first text that went out under his name after it.       capability card_edits
- `own_message`: his own messages in allow-listed chats (state.db holds nothing else),
  with minutes since the requester's last message. The agent's own sends are excluded.     capability outgoing_messages
- `shell_paste`: commands / outputs he pasted back after "Show fix block" / "I ran it".     capability shell
- `desk_session`: his Claude desk sessions that he tagged or that mention the request ref.  capability desk_sessions
- `time_spent`: open -> first reply -> close, and his active span.                          capability time_spent

Records stamped inside a /pause window are dropped. No keystrokes, no screen text, no other chats.
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime

from ..clock import elapsed
from ..models import parse_ts, utc_iso
from . import desk as desk_mod
from . import gate

SHELL_HINT = re.compile(r"(?m)^\s*(?:\$ |PS [A-Z]:|>>> |sudo |python3? |odoo-bin|psql |Traceback)")
SHELL_TAPS = {"fx", "ran"}


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    return round(elapsed(a, b).total_seconds() / 60, 1) if a and b else None


def _detail(row: dict) -> dict:
    try:
        d = json.loads(row.get("detail_json") or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _principal_actor(cfg: dict, actor) -> bool:
    principal = int((cfg.get("people") or {}).get("principal_tg_user_id") or 0)
    return bool(principal) and str(actor) == str(principal)


def diff_stats(draft: str, final: str) -> dict:
    """How Principal changed the draft: similarity, words added / removed, unchanged flag."""
    a, b = draft.split(), final.split()
    sm = difflib.SequenceMatcher(None, a, b)
    added, removed = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            removed += a[i1:i2]
        if op in ("replace", "insert"):
            added += b[j1:j2]
    return {"similarity": round(sm.ratio(), 3), "unchanged": draft.strip() == final.strip(),
            "draft_words": len(a), "final_words": len(b), "added": added[:60], "removed": removed[:60]}


def _own_messages(db, thread: list[dict]) -> list[dict]:
    """Thread rows that show as Principal, each marked whether it was really the agent's send echoing back."""
    out = []
    for m in thread:
        if not m.get("is_principal"):
            continue
        try:
            via_agent = db.send_by_remote(m.get("chat_id"), m.get("msg_id")) is not None
        except Exception:                                   # noqa: BLE001 - unknown = treat as the agent's
            via_agent = True
        out.append({**m, "via_agent": via_agent})
    return out


def _packet_draft(row: dict | None) -> str:
    if not row:
        return ""
    try:
        return ((json.loads(row["packet_json"]) or {}).get("draft_reply") or {}).get("text") or ""
    except (ValueError, TypeError, AttributeError):
        return ""


def build_case(db, cfg: dict, request_id: str, now: datetime, *, projects_dir=None) -> dict | None:
    """All allowed records for one request, oldest first. None if unknown, unreadable or nothing allowed."""
    on = {c for c in gate.CAPTURE if gate.capability_on(cfg, c)}
    req = db.get_request(request_id) if on else None
    windows = gate.pause_windows(db, now) if req else None
    if not req or windows is None:
        return None
    records: list[dict] = []

    def add(kind: str, ts: str | None, **data) -> None:
        if not gate.in_windows(windows, parse_ts(ts)):
            records.append({"kind": kind, "request_id": request_id, "ts": ts, **data})

    thread = db.thread(request_id)
    mine = _own_messages(db, thread)
    events = db.events(request_id)
    if on & {"card_edits", "shell"}:
        armed_shell = False
        for e in events:
            if not _principal_actor(cfg, e.get("actor")):
                continue
            d = _detail(e)
            if e["kind"] == "button":
                armed_shell = d.get("action") in SHELL_TAPS
                if "card_edits" in on:
                    add("tap", e["ts"], action=d.get("action"))
            elif e["kind"] == "instruction":
                text = str(d.get("text") or "")
                if "shell" in on and (armed_shell or SHELL_HINT.search(text)):
                    add("shell_paste", e["ts"], text=text)
                elif "card_edits" in on:
                    add("instruction", e["ts"], text=text)
                armed_shell = False
    if "card_edits" in on:
        row = db.latest_packet(request_id)
        draft = _packet_draft(row)
        after = [m for m in mine if row and (m.get("ts") or "") >= (row.get("created_at") or "")]
        if draft and after:
            final = after[0]
            add("edit", final.get("ts"), draft=draft, final=final.get("text") or "",
                via_agent=final["via_agent"], **diff_stats(draft, final.get("text") or ""))
    if "outgoing_messages" in on:
        last_requester: datetime | None = None
        by_id = {m["event_id"]: m for m in mine}
        for m in thread:
            ts = parse_ts(m.get("ts"))
            if not m.get("is_principal"):
                if not m.get("is_bot"):
                    last_requester = ts
                continue
            if by_id.get(m["event_id"], {}).get("via_agent", True):
                continue
            add("own_message", m.get("ts"), channel=m.get("channel"), text=m.get("text") or "",
                requester=req.get("requester_id") or req.get("requester_name") or "",
                reply_minutes=_minutes(last_requester, ts), hour=ts.hour if ts else None)
    if "desk_sessions" in on:
        dcfg = (cfg.get("watcher") or {}).get("desk") or {}
        refs = [str(req["ticket_id"])] if req.get("ticket_id") else []
        for s in desk_mod.find_sessions(request_id, refs=refs, projects_dir=projects_dir or dcfg.get("projects_dir")
                                        or None, tagged=dcfg.get("tagged_sessions") or {}):
            s["turns"] = [t for t in s["turns"]
                          if not gate.in_windows(windows, parse_ts(t.get("ts")) or parse_ts(s.get("started")))]
            if s["turns"]:
                add("desk_session", s.get("started"), **s)
    for e in events:                                        # a screen recording exists: note only, never the video
        if e["kind"] == "watcher_recording":
            d = _detail(e)
            add("screen_recording", e["ts"], seconds=d.get("seconds"), file=d.get("file"))
    if "time_spent" in on:
        own_ts = [parse_ts(m.get("ts")) for m in mine if not m["via_agent"]]
        own_ts += [parse_ts(e["ts"]) for e in events if _principal_actor(cfg, e.get("actor"))]
        own_ts = [t for t in own_ts if t]
        created = parse_ts(req.get("created_at"))
        add("time_spent", utc_iso(now), first_reply_min=_minutes(created, parse_ts(req.get("first_reply_at"))),
            first_reply_by=req.get("first_reply_by"),
            open_to_close_min=_minutes(created, parse_ts(req.get("closed_at"))),
            principal_active_span_min=_minutes(min(own_ts), max(own_ts)) if own_ts else None,
            principal_minutes=req.get("principal_minutes"), touches=len(own_ts))
    if not records:
        return None
    records.sort(key=lambda r: r.get("ts") or "")
    return {"request_id": request_id, "lane": req.get("lane"), "tier": req.get("tier"),
            "company": req.get("company"), "channel": req.get("channel"), "state": req.get("state"),
            "requester": req.get("requester_id") or req.get("requester_name") or "", "records": records}


def all_cases(db, cfg: dict, now: datetime, *, projects_dir=None) -> list[dict]:
    """Every request that has something allowed to capture."""
    rows = db._all("SELECT id FROM requests ORDER BY id", ())
    return [c for c in (build_case(db, cfg, r["id"], now, projects_dir=projects_dir) for r in rows) if c]
