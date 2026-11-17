"""Telethon intake "TwinAgent-Laptop" (05 1.1, red-team #17, #18).

Rules:
- The allow-list check runs FIRST. A message from any other chat (his personal chats) is dropped in memory:
  no log line, no media download, no event. `normalize_tg` returns None for it.
- Principal's own messages are marked is_principal (also when he posted as the anonymous group admin: `out` is True),
  so the router can record his first reply. They never open a request (router rule).
- Screenshots are downloaded only for allow-listed messages, into data\\media.
- The sweep (`fetch_since`) reads only ids above min_id, at most `limit` (never a fixed re-read of 200).

`raw` dict shape (built by `raw_from_message` from a Telethon message, or by tests):
  chat_id, chat_title, is_private, topic_id, msg_id, sender_id, sender_name, sender_is_bot, out,
  date (aware datetime or ISO text), text, media (list of planned file names), reply_to, edited, is_service
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from ..clock import CAIRO
from ..models import Channel, Company, Event, Sender
from .tg_client import USER_VAULT_NAMES, require_live


class TelegramIn(Protocol):
    async def start(self, on_event: Callable[[Event], Awaitable[None]]) -> None: ...
    async def fetch_since(self, chat_id: int, topic_id: int | None, min_id: int, limit: int = 200) -> list[Event]: ...
    async def latest_id(self, chat_id: int, topic_id: int | None) -> int: ...
    async def stop(self) -> None: ...


# ---------------------------------------------------------------- allow-list

def chat_entry(cfg: dict, chat_id, topic_id: int | None = None) -> tuple[dict, dict | None] | None:
    """The allow-listed (chat, topic) config entries for a message, or None.

    Uses config.allowed_chat (the same rule the guards use): exact chat id as Telethon gives it (supergroups are
    "-100..."), `read: false` drops the chat, and if real topic ids are listed only those topics (or no topic) pass.
    Id 0 (not filled in yet) matches nothing.
    """
    from ..config import allowed_chat, topic_entry
    try:
        chat = allowed_chat(cfg, chat_id, topic_id)
    except (TypeError, ValueError):
        return None
    if chat is None or (chat.get("kind") == "dm" and str(chat_id).startswith("-")):
        return None
    return chat, topic_entry(cfg, chat_id, topic_id)


def _as_cairo(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=CAIRO)
    return value.astimezone(CAIRO)


def _company(entry: dict | None, chat: dict) -> Company:
    for src in (entry or {}, chat):
        try:
            if src.get("company"):
                return Company(src["company"])
        except ValueError:
            pass
    return Company.UNKNOWN


def _tags_principal(raw: dict, chat: dict) -> bool:
    """tagged_only chats: Telegram's own `mentioned` flag (an @mention of Principal, or a reply to one of his
    messages), or one of chats[].tag_handles (e.g. his bot) as a whole handle in the text, any case.
    11 Nov 2026 evening, after the twin missed every real ping of the day:
    - a reply to a message from chats[].tag_user_ids (his bot): `reply_to_sender_id`, filled by the adapter;
    - chats[].tag_names (his first name, "@Principal" too) as a whole word, any case. Sysadmin wrote "@Principal ..."
      (someone else's username) and "Principal, ..."; neither is a Telegram mention of Principal."""
    if raw.get("mentioned"):
        return True
    rs = raw.get("reply_to_sender_id")
    if rs and int(rs) in {int(u) for u in chat.get("tag_user_ids") or []}:
        return True
    text = str(raw.get("text") or "")
    for h in chat.get("tag_handles") or []:
        h = str(h).strip().lstrip("@")
        if h and re.search(rf"(?<![\w@])@{re.escape(h)}(?!\w)", text, re.IGNORECASE):
            return True
    for n in chat.get("tag_names") or []:
        n = str(n).strip().lstrip("@")
        if n and re.search(rf"(?<![\w@-])@?{re.escape(n)}(?![\w-])", text, re.IGNORECASE):
            return True
    return False


async def fill_reply_sender(msg, raw: dict, chat: dict | None) -> None:
    """tagged_only chats only: for a reply that is not already tagged, ask Telegram who wrote the message it
    answers (one call), so a reply to his bot's message counts (tag_user_ids). Any failure leaves it unset."""
    if not chat or not chat.get("tagged_only") or not chat.get("tag_user_ids") or raw.get("reply_to") is None:
        return
    if _tags_principal(raw, chat):
        return
    try:
        rep = await msg.get_reply_message()
    except Exception:                            # noqa: BLE001 - the message is then judged on its text alone
        return
    if rep is not None and getattr(rep, "sender_id", None):
        raw["reply_to_sender_id"] = int(rep.sender_id)


async def _raw_for(msg, cfg: dict, *, edited: bool = False) -> dict:
    raw = raw_from_message(msg, edited=edited)
    hit = chat_entry(cfg, raw.get("chat_id"), raw.get("topic_id"))
    await fill_reply_sender(msg, raw, hit[0] if hit else None)
    return raw


def normalize_tg(raw: dict, cfg: dict, principal_user_id: int) -> Event | None:
    """Raw message dict -> Event. None for chats not on the allow-list (dropped in memory, NEVER logged).

    A chat with tagged_only: true (Acme Team, Principal 2026-11-11) passes only messages that tag Principal or reply
    to him, plus Principal's own replies (so his answer is seen and closes the request); the rest is dropped in memory.
    """
    if raw.get("is_service"):
        return None
    hit = chat_entry(cfg, raw.get("chat_id"), raw.get("topic_id"))
    if hit is None:
        return None
    chat, topic = hit
    sender_id = int(raw.get("sender_id") or 0)
    is_principal = bool(raw.get("out")) or (bool(principal_user_id) and sender_id == int(principal_user_id))
    if chat.get("tagged_only") and not (raw.get("reply_to") is not None if is_principal else _tags_principal(raw, chat)):
        return None
    is_dm = chat.get("kind") == "dm"
    sender = Sender(tg_id=sender_id, name=str(raw.get("sender_name") or ""),
                    dept=(topic or {}).get("name", "") if topic else "",
                    is_principal=is_principal, is_bot=bool(raw.get("sender_is_bot")))
    chat_id = raw["chat_id"]
    msg_id = raw.get("msg_id")
    return Event(
        event_id=f"tg:{chat_id}:{msg_id}",
        channel=Channel.telegram_dm if is_dm else Channel.telegram_group,
        chat=chat.get("name") or str(raw.get("chat_title") or ""),
        chat_id=chat_id,
        topic=(topic or {}).get("name") if topic else None,
        topic_id=int(raw["topic_id"]) if raw.get("topic_id") is not None else None,
        sender=sender, ts=_as_cairo(raw["date"]), text=str(raw.get("text") or ""),
        media=list(raw.get("media") or []), reply_to=raw.get("reply_to"),
        company_hint=_company(topic, chat), untrusted=not is_principal,
        msg_id=msg_id, edited=bool(raw.get("edited")),
    )


# ---------------------------------------------------------------- reactions (closing rules: a requester's 👍)

def _peer_chat_id(peer) -> int | None:
    """Bot-API style chat id from a Telethon Peer: -100<channel_id> for supergroups, -<chat_id>, or the user id."""
    if getattr(peer, "channel_id", None):
        return int(f"-100{peer.channel_id}")
    if getattr(peer, "chat_id", None):
        return -int(peer.chat_id)
    if getattr(peer, "user_id", None):
        return int(peer.user_id)
    return None


def reaction_events(update, cfg: dict, principal_user_id: int) -> list[Event]:
    """UpdateMessageReactions -> one Event per recent reaction we can attribute to a person.

    Telegram lists who reacted only in `recent_reactions` (small groups, DMs); a reaction with no named person is
    dropped, so an unknown reactor never closes anything. Not allow-listed chat = [] (dropped in memory, not logged).
    UNTESTED against real Telegram (fakes only): check on day 1 that a 👍 in the group reaches the router.
    """
    from ..closing import rule
    if not rule(cfg, "close_on_ack"):
        return []
    chat_id = _peer_chat_id(getattr(update, "peer", None))
    topic_id = getattr(update, "top_msg_id", None)
    hit = chat_entry(cfg, chat_id, topic_id) if chat_id is not None else None
    if hit is None:
        return []
    chat, topic = hit
    out: list[Event] = []
    for r in getattr(getattr(update, "reactions", None), "recent_reactions", None) or []:
        uid = getattr(getattr(r, "peer_id", None), "user_id", None)
        emoji = getattr(getattr(r, "reaction", None), "emoticon", None)
        if not uid or not emoji:
            continue
        is_principal = bool(principal_user_id) and int(uid) == int(principal_user_id)
        out.append(Event(
            event_id=f"tg:{chat_id}:{update.msg_id}:react:{uid}:{emoji}",
            channel=Channel.telegram_dm if chat.get("kind") == "dm" else Channel.telegram_group,
            chat=chat.get("name") or "", chat_id=chat_id, topic=(topic or {}).get("name") if topic else None,
            topic_id=int(topic_id) if topic_id is not None else None,
            sender=Sender(tg_id=int(uid), is_principal=is_principal), text="",
            ts=_as_cairo(r.date) if getattr(r, "date", None) else datetime.now(CAIRO),
            reply_to=int(update.msg_id), reaction=str(emoji), untrusted=not is_principal))
    return out


# ---------------------------------------------------------------- Telethon message -> raw dict

def topic_of(msg) -> int | None:
    """Forum topic id of a Telethon message (None outside forum topics / the General topic)."""
    rt = getattr(msg, "reply_to", None)
    if rt is None or not getattr(rt, "forum_topic", False):
        return None
    return getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)


def reply_to_of(msg) -> int | None:
    """The message this one answers (not the topic root)."""
    rt = getattr(msg, "reply_to", None)
    if rt is None:
        return None
    if getattr(rt, "forum_topic", False) and not getattr(rt, "reply_to_top_id", None):
        return None                       # a plain message in a topic: reply_to_msg_id is the topic itself
    return getattr(rt, "reply_to_msg_id", None)


def raw_from_message(msg, *, edited: bool = False) -> dict:
    """Pull only the fields we need from a Telethon Message. Reads no network."""
    chat_id = getattr(msg, "chat_id", None)
    sender = getattr(msg, "sender", None)
    first = getattr(sender, "first_name", None) or getattr(sender, "title", None) or ""
    last = getattr(sender, "last_name", None) or ""
    has_photo = getattr(msg, "photo", None) is not None
    return {
        "chat_id": chat_id, "chat_title": getattr(getattr(msg, "chat", None), "title", "") or "",
        "is_private": bool(getattr(msg, "is_private", False)), "topic_id": topic_of(msg),
        "msg_id": getattr(msg, "id", None), "sender_id": getattr(msg, "sender_id", None) or 0,
        "sender_name": f"{first} {last}".strip(), "sender_is_bot": bool(getattr(sender, "bot", False)),
        "out": bool(getattr(msg, "out", False)), "date": getattr(msg, "date", None),
        "text": getattr(msg, "message", None) or "",
        "media": [f"photo_{abs(int(chat_id or 0))}_{getattr(msg, 'id', 0)}.jpg"] if has_photo else [],
        "reply_to": reply_to_of(msg), "edited": edited,
        "is_service": getattr(msg, "action", None) is not None,
        "mentioned": bool(getattr(msg, "mentioned", False)),     # Telegram: @Principal or a reply to his message
    }


# ---------------------------------------------------------------- real adapter

async def warm_entity_cache(client) -> None:
    """A StringSession keeps the login, not the chat access hashes. Load the dialogs once after connect, or
    get_messages / send_message on "-100..." ids fail with "Could not find the input entity" after a restart."""
    await client.get_dialogs()


class TelethonIn:
    """Real TelegramIn. Pass the shared client from tg_client.make_user_client (tests pass a duck-typed fake)."""

    def __init__(self, cfg: dict, client, *, has_secret: Callable[[str], bool], media_dir: Path | None = None,
                 principal_user_id: int | None = None):
        require_live(cfg, USER_VAULT_NAMES, has_secret)
        self.cfg, self.client = cfg, client
        self.principal_user_id = int(principal_user_id or (cfg.get("people") or {}).get("principal_tg_user_id") or 0)
        if media_dir is None:
            from .. import paths
            media_dir = paths.data_dir() / "media"
        self.media_dir = Path(media_dir)
        self._on_event: Callable[[Event], Awaitable[None]] | None = None
        self._handlers: list = []

    def allowed_chat_ids(self) -> list[int]:
        return [int(c["chat_id"]) for c in self.cfg.get("chats") or []
                if c.get("chat_id") and c.get("read", True) is not False]

    async def start(self, on_event: Callable[[Event], Awaitable[None]]) -> None:
        from telethon import events           # lazy
        self._on_event = on_event
        if not self.client.is_connected():
            await self.client.connect()
        await warm_entity_cache(self.client)
        if not self.principal_user_id:
            me = await self.client.get_me()
            self.principal_user_id = int(me.id)
        chats = self.allowed_chat_ids()           # second lock: Telethon filters too
        async def new_h(e):
            await self._safe_handle(e.message, edited=False)

        async def edit_h(e):
            await self._safe_handle(e.message, edited=True)

        self.client.add_event_handler(new_h, events.NewMessage(chats=chats))
        self.client.add_event_handler(edit_h, events.MessageEdited(chats=chats))
        self._handlers = [new_h, edit_h]
        try:                                      # a requester's thumbs-up can close a request (closing.py)
            from telethon.tl.types import UpdateMessageReactions

            async def react_h(update):
                await self.handle_reactions(update)
            self.client.add_event_handler(react_h, events.Raw(UpdateMessageReactions))
            self._handlers.append(react_h)
        except Exception as e:                    # noqa: BLE001 - reactions are optional; messages must flow
            import logging
            logging.getLogger("twin_agent.telegram_in").warning("no reaction intake: %s", type(e).__name__)

    async def handle_reactions(self, update) -> list[Event]:
        try:
            evs = reaction_events(update, self.cfg, self.principal_user_id)
            for ev in evs:
                if self._on_event:
                    await self._on_event(ev)
            return evs
        except Exception as e:                    # noqa: BLE001 - log the type only, never the content
            import logging
            logging.getLogger("twin_agent.telegram_in").warning("reaction handler failed: %s", type(e).__name__)
            return []

    async def _safe_handle(self, msg, *, edited: bool) -> None:
        """Telethon would log a handler's exception text (which can quote a message). Log only the type."""
        try:
            await self.handle(msg, edited=edited)
        except Exception as e:                   # noqa: BLE001
            import logging
            logging.getLogger("twin_agent.telegram_in").warning("intake handler failed: %s", type(e).__name__)

    async def handle(self, msg, *, edited: bool = False) -> Event | None:
        """Normalise, drop if not allow-listed, download the screenshot, emit. Logs nothing about dropped chats."""
        ev = normalize_tg(await _raw_for(msg, self.cfg, edited=edited), self.cfg, self.principal_user_id)
        if ev is None:
            return None
        await self._download_media(msg, ev)
        if self._on_event:
            await self._on_event(ev)
        return ev

    async def _download_media(self, msg, ev: Event) -> None:
        if not ev.media or getattr(msg, "photo", None) is None:
            return
        self.media_dir.mkdir(parents=True, exist_ok=True)
        target = self.media_dir / ev.media[0]
        try:
            await self.client.download_media(msg, file=str(target))
        except Exception:                        # a failed download must not lose the message
            ev.media = []

    async def fetch_since(self, chat_id: int, topic_id: int | None, min_id: int, limit: int = 200) -> list[Event]:
        """Sweep: only ids above min_id, oldest first. Non-allow-listed chats return [] without a network call."""
        if chat_entry(self.cfg, chat_id, topic_id) is None:
            return []
        kwargs = {"min_id": int(min_id), "limit": int(limit), "reverse": True}     # oldest first from min_id
        if topic_id is not None:
            kwargs["reply_to"] = int(topic_id)
        msgs = await self.client.get_messages(chat_id, **kwargs)
        out: list[Event] = []
        for m in sorted(msgs, key=lambda m: m.id):
            ev = normalize_tg(await _raw_for(m, self.cfg), self.cfg, self.principal_user_id)
            if ev is not None:
                ev.found_by_sweep = True
                await self._download_media(m, ev)
                out.append(ev)
        return out

    async def latest_id(self, chat_id: int, topic_id: int | None) -> int:
        """Newest message id in an allow-listed chat/topic (0 if none / not allow-listed). Opens nothing - used to
        baseline a first run to 'now' so the twin does not re-open months of answered threads."""
        if chat_entry(self.cfg, chat_id, topic_id) is None:
            return 0
        kwargs = {"limit": 1}                              # newest only
        if topic_id is not None:
            kwargs["reply_to"] = int(topic_id)
        msgs = await self.client.get_messages(chat_id, **kwargs)
        return int(max((m.id for m in msgs), default=0))

    async def stop(self) -> None:
        for h in self._handlers:
            self.client.remove_event_handler(h)
        self._handlers = []

