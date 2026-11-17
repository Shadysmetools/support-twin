"""Telethon send as Principal (05 5, red-team #17). Only sender.py calls it, after the guards pass.

- Replies go into the same forum topic, threaded with reply_to (the asker's message, else the topic root).
- `send_message` returns {"msg_id", "from_id", "from_id_ok"}. from_id is the id Telegram shows as the author.
  If Principal's account still has "Remain anonymous" on, the author is the GROUP, not him: from_id_ok is False and
  sender.py must delete the message, demote the lane and alert (05 2.5).
- Telethon errors are mapped to plain exceptions so sender.py never imports telethon:
  TgFloodWait (wait + queue), TgPeerFlood (stop ALL account sends), TgNetworkDrop (maybe_sent: read back first).
- Defence in depth: the real adapter itself refuses to send unless run_mode is live, send_enabled is true, the
  STOP file is absent and the chat is on the allow-list. The guards remain the real gate.
- `send_voice` posts an OGG/Opus voice note (Telethon send_file voice_note=True) with the AI-voice disclosure as
  its caption. Same refusals plus voice consent (D1 + D2 + voice.enabled). Only sender.py calls it.
"""
from __future__ import annotations

from typing import Callable, Protocol

from ..models import Event
from .telegram_in import chat_entry, normalize_tg, raw_from_message, warm_entity_cache
from .tg_client import USER_VAULT_NAMES, require_live


class TelegramSend(Protocol):
    my_user_id: int

    async def send_message(self, chat_id: int, text: str, *, reply_to: int | None, topic_id: int | None) -> dict: ...
    async def delete_message(self, chat_id: int, msg_id: int) -> None: ...
    # Optional (voice notes). Adapters without it cannot send voice: sender blocks with no_adapter.
    # async def send_voice(self, chat_id: int, path: str, *, caption: str, reply_to: int | None,
    #                      topic_id: int | None) -> dict
    async def read_thread(self, chat_id: int, topic_id: int | None, after_msg_id: int) -> list[Event]: ...


class TgSendError(RuntimeError):
    """Base class for Telegram send problems sender.py handles."""


class TgFloodWait(TgSendError):
    def __init__(self, seconds: int):
        super().__init__(f"flood wait {seconds}s")
        self.seconds = int(seconds)


class TgPeerFlood(TgSendError):
    """PEER_FLOOD / spam restriction: stop every send from Principal's account, cards only, alert (05 2.5)."""


class TgNetworkDrop(TgSendError):
    """The connection dropped during the call: the message may or may not have gone out (maybe_sent)."""


class TgSendRefused(TgSendError):
    """The adapter's own safety check said no (not live, send disabled, STOP file, chat not allowed)."""


_PEER_FLOOD_NAMES = {"PeerFloodError", "UserRestrictedError", "UserBannedInChannelError"}


def map_error(e: BaseException) -> BaseException:
    """Turn a Telethon / network exception into one of ours (by class name, so no telethon import)."""
    name = type(e).__name__
    if name in ("FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError"):
        return TgFloodWait(getattr(e, "seconds", 60))
    if name in _PEER_FLOOD_NAMES:
        return TgPeerFlood(name)
    if isinstance(e, (ValueError, TypeError)):        # e.g. "Could not find the input entity": failed BEFORE the RPC
        return TgSendRefused(f"not sent: {name}")
    if isinstance(e, (ConnectionError, OSError, TimeoutError)) or name in ("RPCError", "ServerError"):
        return TgNetworkDrop(name)
    return TgSendError(name)


def author_id(msg) -> int:
    """Who Telegram shows as the author of a sent message. Anonymous admin posts carry the group as author."""
    peer = getattr(msg, "from_id", None)
    if peer is None:                              # anonymous admin: no from_id, the post is the chat's
        return int(getattr(msg, "chat_id", 0) or 0)
    for attr in ("user_id", "channel_id", "chat_id"):
        val = getattr(peer, attr, None)
        if val is not None:
            return int(val) if attr == "user_id" else -abs(int(val))
    return 0


def from_id_ok(result: dict, my_user_id: int) -> bool:
    """The post-check: the message must show Principal (my_user_id), never the anonymous group."""
    return bool(my_user_id) and int(result.get("from_id") or 0) == int(my_user_id)


class TelethonSend:
    """Real TelegramSend over the shared Telethon client."""

    def __init__(self, cfg: dict, client, *, has_secret: Callable[[str], bool],
                 stop_file: Callable[[], object] | None = None):
        require_live(cfg, USER_VAULT_NAMES, has_secret)
        self.cfg, self.client = cfg, client
        self.my_user_id = 0
        if stop_file is None:
            from .. import paths
            stop_file = paths.stop_file
        self._stop_file = stop_file

    async def connect(self) -> int:
        """Connect and learn Principal's real user id from Telegram (never trust the config alone)."""
        if not self.client.is_connected():
            await self.client.connect()
        await warm_entity_cache(self.client)
        me = await self.client.get_me()
        self.my_user_id = int(me.id)
        return self.my_user_id

    def _refuse_reason(self, chat_id, topic_id) -> str | None:
        if self.cfg.get("run_mode") != "live":
            return "run_mode is not live"
        if self.cfg.get("send_enabled") is not True:
            return "send_enabled is false"
        if self._stop_file().exists():
            return "kill switch (STOP file)"
        if chat_entry(self.cfg, chat_id, topic_id) is None:
            return "chat or topic not on the allow-list"
        return None

    async def send_message(self, chat_id: int, text: str, *, reply_to: int | None, topic_id: int | None) -> dict:
        why = self._refuse_reason(chat_id, topic_id)
        if why:
            raise TgSendRefused(why)
        if not self.my_user_id:
            await self.connect()
        target = reply_to or topic_id          # reply_to keeps the topic; the topic root posts into the topic
        try:
            msg = await self.client.send_message(int(chat_id), text, reply_to=target, link_preview=False)
        except Exception as e:                  # noqa: BLE001
            raise map_error(e) from None        # 'from None': Telethon's text never travels with it
        result = {"msg_id": int(msg.id), "from_id": author_id(msg)}
        result["from_id_ok"] = from_id_ok(result, self.my_user_id)
        return result

    async def send_voice(self, chat_id: int, path: str, *, caption: str, reply_to: int | None,
                         topic_id: int | None) -> dict:
        """A voice note in Principal's cloned voice. Same result shape and post-check as send_message."""
        from ..voice.policy import voice_consent_ok
        why = self._refuse_reason(chat_id, topic_id) or (None if voice_consent_ok(self.cfg) else "no voice consent")
        if why:
            raise TgSendRefused(why)
        if not self.my_user_id:
            await self.connect()
        try:
            msg = await self.client.send_file(int(chat_id), str(path), voice_note=True, caption=caption,
                                              reply_to=reply_to or topic_id)
        except Exception as e:                  # noqa: BLE001
            raise map_error(e) from None
        result = {"msg_id": int(msg.id), "from_id": author_id(msg)}
        result["from_id_ok"] = from_id_ok(result, self.my_user_id)
        return result

    async def delete_message(self, chat_id: int, msg_id: int) -> None:
        """Used after a failed from_id post-check or 'Delete it + I'll reply'. Allowed even under STOP."""
        try:
            await self.client.delete_messages(int(chat_id), [int(msg_id)], revoke=True)
        except Exception as e:                  # noqa: BLE001
            raise map_error(e) from None

    async def read_thread(self, chat_id: int, topic_id: int | None, after_msg_id: int) -> list[Event]:
        """Live re-read before a send (stale-draft and human-active guards). Allow-listed chats only."""
        if chat_entry(self.cfg, chat_id, topic_id) is None:
            return []
        kwargs = {"min_id": int(after_msg_id or 0), "limit": 100, "reverse": True}
        if topic_id is not None:
            kwargs["reply_to"] = int(topic_id)
        msgs = await self.client.get_messages(int(chat_id), **kwargs)
        out = []
        for m in sorted(msgs, key=lambda m: m.id):
            ev = normalize_tg(raw_from_message(m), self.cfg, self.my_user_id)
            if ev is not None:
                out.append(ev)
        return out
