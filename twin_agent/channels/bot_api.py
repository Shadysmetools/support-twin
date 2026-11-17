"""Real BotIO: the "Principal's Agent" bot over the plain Telegram Bot API (httpx, long poll).

- Refuses to start unless run_mode is live and 'tg-bot-token' is in the vault.
- The token sits in the API URL. It is never logged: errors are rebuilt from the method name and Telegram's
  description with the token scrubbed, and exception chains are cut ('from None').
- Only private chats are passed on. Who is heard (Principal, backup) is decided by BotController.
- Tests pass `http=` an httpx.AsyncClient with a MockTransport, so no network is ever used.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

from ..models import Button, Card
from .bot_io import render_card

CAPTION_MAX = 1024                               # Telegram photo caption limit
from .tg_client import BOT_VAULT_NAMES, quiet_library_logs, require_live

log = logging.getLogger("twin_agent.bot_api")
API_ROOT = "https://api.telegram.org"


class BotApiError(RuntimeError):
    pass


class TelegramBotApi:
    def __init__(self, cfg: dict, *, has_secret: Callable[[str], bool], get_secret: Callable[[str], str] | None = None,
                 http=None, poll_timeout: int = 30):
        require_live(cfg, BOT_VAULT_NAMES, has_secret)
        if get_secret is None:
            from .. import creds
            get_secret = creds._get
        self.__token = get_secret("tg-bot-token")
        people = cfg.get("people") or {}
        self.principal_id = int(people.get("principal_tg_user_id") or 0)
        self.backup_ids = [int(x) for x in (people.get("backup_tg_user_ids") or []) if int(x or 0)]
        self._http, self.poll_timeout = http, poll_timeout
        self._offset = 0
        self._running = False
        self._handlers: tuple = (None, None, None)
        quiet_library_logs()

    def __repr__(self) -> str:                 # never show the token
        return "TelegramBotApi(<token hidden>)"

    # ------------------------------------------------------------ HTTP

    def _client(self):
        if self._http is None:
            import httpx                       # lazy
            self._http = httpx.AsyncClient(timeout=self.poll_timeout + 15)
        return self._http

    def _scrub(self, text: str) -> str:
        return str(text).replace(self.__token, "<token>") if self.__token else str(text)

    async def call(self, method: str, payload: dict | None = None, files: dict | None = None) -> dict | list:
        url = f"{API_ROOT}/bot{self.__token}/{method}"
        try:
            if files:
                data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in (payload or {}).items()}
                resp = await self._client().post(url, data=data, files=files)
            else:
                resp = await self._client().post(url, json=payload or {})
            body = resp.json()
        except Exception as e:                  # noqa: BLE001
            raise BotApiError(f"{method}: {type(e).__name__}") from None
        if not body.get("ok"):
            raise BotApiError(self._scrub(f"{method}: {body.get('description', 'not ok')}"))
        return body.get("result")

    # ------------------------------------------------------------ BotIO

    async def start(self, on_callback, on_command, on_reply) -> None:
        """Long poll until stop(). Network errors back off 5 s and retry; they never end the loop."""
        self._handlers, self._running = (on_callback, on_command, on_reply), True
        while self._running:
            try:
                await self.poll_once()
            except BotApiError as e:
                log.warning("bot poll failed: %s", e)
                await asyncio.sleep(5)

    async def poll_once(self) -> int:
        updates = await self.call("getUpdates", {"offset": self._offset, "timeout": self.poll_timeout,
                                                 "allowed_updates": ["message", "callback_query"]})
        for upd in updates or []:
            self._offset = max(self._offset, int(upd["update_id"]) + 1)
            try:
                await self.dispatch(upd)
            except Exception as e:             # noqa: BLE001 - one bad update must never stop the bot (/stop path)
                log.warning("bot update failed: %s", type(e).__name__)
        return len(updates or [])

    async def dispatch(self, upd: dict) -> None:
        on_callback, on_command, on_reply = self._handlers
        cq = upd.get("callback_query")
        if cq:
            await on_callback({"user_id": cq["from"]["id"], "data": cq.get("data", ""), "callback_id": cq["id"]})
            return
        msg = upd.get("message")
        if not msg or (msg.get("chat") or {}).get("type") != "private" or "from" not in msg:
            return                                 # the bot joins no groups
        uid, text = msg["from"]["id"], msg.get("text") or msg.get("caption") or ""
        if msg.get("forward_origin") or msg.get("forward_date") or msg.get("forward_from"):
            await on_command({"user_id": uid, "command": "new", "args": text})
        elif text.startswith("/"):
            cmd, _, args = text[1:].partition(" ")
            await on_command({"user_id": uid, "command": cmd.split("@")[0], "args": args})
        else:
            to = (msg.get("reply_to_message") or {}).get("message_id")
            await on_reply({"user_id": uid, "text": text, "to_bot_msg_id": to})

    def _target(self, card: Card) -> int:
        target = (self.backup_ids[:1] or [0])[0] if card.to == "backup" else self.principal_id
        if not target:
            raise BotApiError("no recipient user id in config")
        return target

    @staticmethod
    def _keyboard(card: Card, callback_for: Callable[[Button], str]) -> dict | None:
        if not card.buttons:
            return None
        return {"inline_keyboard": [[{"text": b.label, "callback_data": callback_for(b)} for b in row]
                                    for row in card.buttons]}

    async def send_card(self, card: Card, callback_for: Callable[[Button], str]) -> int:
        chat_id = self._target(card)
        text = render_card(card)
        for path in card.attachments[:1]:            # the requester's screenshot (already secrets-scanned)
            p = Path(path)
            if p.is_file():
                with p.open("rb") as fh:
                    data = fh.read()
                if len(text) <= CAPTION_MAX:         # Principal 2026-11-09: ONE message = photo + card + buttons
                    payload = {"chat_id": chat_id, "caption": text, "disable_notification": card.silent}
                    kb = self._keyboard(card, callback_for)
                    if kb:
                        payload["reply_markup"] = kb             # call() JSON-encodes it for multipart
                    return int((await self.call("sendPhoto", payload, files={"photo": (p.name, data)}))["message_id"])
                await self.call("sendPhoto", {"chat_id": chat_id, "disable_notification": card.silent},
                                files={"photo": (p.name, data)})
        payload = {"chat_id": chat_id, "text": text, "disable_notification": card.silent,
                   "link_preview_options": {"is_disabled": True}}
        kb = self._keyboard(card, callback_for)
        if kb:
            payload["reply_markup"] = kb
        return int((await self.call("sendMessage", payload))["message_id"])

    async def edit_card(self, bot_msg_id: int, card: Card, callback_for) -> None:
        payload = {"chat_id": self._target(card), "message_id": bot_msg_id, "text": render_card(card)}
        kb = self._keyboard(card, callback_for)
        if kb:
            payload["reply_markup"] = kb
        await self.call("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    async def ask_force_reply(self, prompt: str, request_id: str) -> int:
        """The Edit prompt. Only Principal gets it; his answer to THIS message id is the replacement text."""
        if not self.principal_id:
            raise BotApiError("no Principal user id in config")
        res = await self.call("sendMessage", {"chat_id": self.principal_id, "text": prompt,
                                              "reply_markup": {"force_reply": True,
                                                               "input_field_placeholder": "New reply text"}})
        return int(res["message_id"])

    async def stop(self) -> None:
        self._running = False
        if self._http is not None:
            await self._http.aclose()
