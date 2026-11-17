"""Read a Telegram Desktop JSON export (result.json) into Events. Read only; treat every text as data.

Topics: in a forum export a message's reply_to_message_id is the topic's root ("topic_created") id, or a message
in that topic. We walk the reply chain up to a root to find the topic. No root = the General topic (None).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ..clock import CAIRO, UTC, utc
from ..models import Channel, Company, Event, Sender


def _text(raw) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in raw)
    return ""


def _uid(from_id: str | None) -> int:
    digits = "".join(c for c in str(from_id or "") if c.isdigit())
    return int(digits) if digits else 0


def read_raw(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def topic_roots(raw: dict) -> dict[int, str]:
    return {m["id"]: str(m.get("title") or "") for m in raw.get("messages", [])
            if m.get("type") == "service" and m.get("action") == "topic_created"}


def chat_key(raw: dict) -> int:
    """Telegram API style id for a supergroup export id (-100<id>), so it matches config chats[].chat_id."""
    return int(f"-100{raw.get('id', 0)}")


def load_events(raw: dict, *, principal_id: int | None, since: datetime | None, days: int,
                reactions: bool = False) -> list[Event]:
    """Messages in the window, oldest first. reactions=True adds one Event per recent reaction (Event.reaction set,
    reply_to = the message reacted to, empty text), so the closing rules see a requester's thumbs-up."""
    msgs = [m for m in raw.get("messages", []) if m.get("type") == "message"]
    if not msgs:
        return []
    by_id = {m["id"]: m for m in raw.get("messages", [])}
    roots = topic_roots(raw)
    chat_id = chat_key(raw)

    def topic_of(m) -> int | None:
        seen, cur = 0, m.get("reply_to_message_id")
        while cur is not None and seen < 50:
            if cur in roots:
                return cur
            parent = by_id.get(cur)
            cur = parent.get("reply_to_message_id") if parent else None
            seen += 1
        return None

    def ts(m) -> datetime:
        # date_unixtime is the exact instant; the local "date" is ambiguous in the repeated summer-time hour.
        if str(m.get("date_unixtime") or "").isdigit():
            return datetime.fromtimestamp(int(m["date_unixtime"]), tz=UTC).astimezone(CAIRO)
        return datetime.fromisoformat(m["date"]).replace(tzinfo=CAIRO)

    last = max((ts(m) for m in msgs), key=utc)
    start = since if since is not None else last - timedelta(days=days)
    out = []
    for m in msgs:
        when = ts(m)
        if utc(when) < utc(start):
            continue
        fid = str(m.get("from_id") or "")
        name = str(m.get("from") or "")
        is_principal = fid.startswith("channel") or (bool(principal_id) and _uid(fid) == int(principal_id))
        is_bot = name.lower().endswith("bot") or "via_bot" in m
        tid = topic_of(m)
        media = [k for k in ("photo", "file") if m.get(k)]
        out.append(Event(
            event_id=f"tg:{chat_id}:{m['id']}", channel=Channel.telegram_group, chat=str(raw.get("name") or ""),
            chat_id=chat_id, topic=roots.get(tid) if tid else None, topic_id=tid,
            sender=Sender(tg_id=_uid(fid), name=name, is_principal=is_principal, is_bot=is_bot), ts=when,
            text=_text(m.get("text")), media=media, reply_to=m.get("reply_to_message_id"),
            company_hint=Company.UNKNOWN, untrusted=not is_principal, msg_id=m["id"]))
        for r in (m.get("reactions") or []) if reactions else []:
            for who in r.get("recent") or []:
                rfid = str(who.get("from_id") or "")
                rwhen = datetime.fromisoformat(who["date"]).replace(tzinfo=CAIRO) if who.get("date") else when
                out.append(Event(
                    event_id=f"tg:{chat_id}:{m['id']}:react:{_uid(rfid)}:{r.get('emoji', '')}",
                    channel=Channel.telegram_group, chat=str(raw.get("name") or ""), chat_id=chat_id,
                    topic=roots.get(tid) if tid else None, topic_id=tid,
                    sender=Sender(tg_id=_uid(rfid), name=str(who.get("from") or ""),
                                  is_principal=rfid.startswith("channel") or (bool(principal_id) and _uid(rfid) == int(principal_id))),
                    ts=rwhen if utc(rwhen) >= utc(when) else when, text="", reply_to=m["id"],
                    reaction=str(r.get("emoji") or ""), msg_id=None))
    return sorted(out, key=lambda e: (utc(e.ts), int(e.msg_id or 0)))


def replay_cfg(base: dict, raw: dict) -> dict:
    """The live config with the export's chat and topics allow-listed (replay only, never saved)."""
    cfg = json.loads(json.dumps(base, default=str))
    topics = [{"id": tid, "name": name, "company": "KSA" if "ksa" in name.lower() else "EG"}
              for tid, name in topic_roots(raw).items()]
    cfg["chats"] = [{"name": str(raw.get("name") or "export"), "chat_id": chat_key(raw), "kind": "group",
                     "company": "EG", "read": True, "all_topics": True, "topics": topics}]
    cfg["run_mode"], cfg["send_enabled"] = "dry_run", False
    return cfg
