"""The rules of the "Principal's Agent" bot (see bot_io.py for the summary). Transport-free: works with any BotIO.

The db object needs: add_card, issue_approval, use_approval, log_event, open_requests, set_cursor (StateDB).
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ..bus import Topics
from ..clock import elapsed, shift
from ..models import Button, Card, CardKind, utc_iso
from .bot_io import (SUPERVISOR_ACTIONS, APPROVAL_TTL, BACKUP_ACTIONS, BACKUP_COMMANDS, NO_REQUEST, URGENT_KINDS, make_callback,
                     parse_callback, parse_duration, strip_for_backup, text_hash)

LOUD_IN_QUIET_HOURS = frozenset({CardKind.holding_blocked, CardKind.circuit_breaker})


class BotController:
    def __init__(self, bot, db, cfg: dict, bus, clock, *, stop_file: Callable[[], Path] | None = None):
        self.bot, self.db, self.cfg, self.bus, self.clock = bot, db, cfg, bus, clock
        people = cfg.get("people") or {}
        self.principal_id = int(people.get("principal_tg_user_id") or 0)        # 0 = not set: nobody matches
        self.backup_ids = [int(x) for x in (people.get("backup_tg_user_ids") or []) if int(x or 0)]
        self.supervisor_id = int(people.get("supervisor_tg_user_id") or 0)        # helpdesk stage gate only (ag / ax)
        if stop_file is None:
            from .. import paths
            stop_file = paths.stop_file
        self._stop_file = stop_file
        self.cards_per_hour = int((cfg.get("guards") or {}).get("cards_per_hour", 10))
        self._sent_at: deque[datetime] = deque()
        self.held: list[tuple[Card, int, str]] = []      # over the hourly cap: (card, recipient, draft_hash)
        self._card_request: dict[int, str | None] = {}   # bot message id -> request id (for plain replies)
        self._force_prompts: dict[int, str] = {}         # ForceReply prompt id -> request id (Edit only)

    # ------------------------------------------------------------ who may talk to the bot

    def is_heard(self, user_id) -> bool:
        uid = int(user_id or 0)
        return bool(uid) and (uid == self.principal_id or uid in self.backup_ids)

    def is_principal(self, user_id) -> bool:
        return bool(self.principal_id) and int(user_id or 0) == self.principal_id

    async def start(self) -> None:
        """Hear the bot, and send every Card published on 'card.out' (a Card, or {"card", "draft_hash"})."""
        if not getattr(self, "card_out_wired", False):     # the listener's Flow forwards card.out itself
            self.bus.subscribe(Topics.CARD_OUT, self._on_card_out)
            self.card_out_wired = True
        await self.bot.start(self.on_callback, self.on_command, self.on_reply)

    async def _on_card_out(self, payload) -> None:
        if isinstance(payload, dict):
            await self.post_card(payload["card"], draft_hash=payload.get("draft_hash", ""))
        else:
            await self.post_card(payload)

    # ------------------------------------------------------------ cards out

    def _quiet(self, now: datetime) -> bool:
        q = (self.cfg.get("timers") or {}).get("quiet_hours") or {"start": "22:00", "end": "08:00"}
        start, end, hm = q["start"], q["end"], now.strftime("%H:%M")
        return (hm >= start or hm < end) if start > end else (start <= hm < end)

    def _room(self, now: datetime) -> bool:
        while self._sent_at and elapsed(self._sent_at[0], now) >= timedelta(hours=1):
            self._sent_at.popleft()
        return len(self._sent_at) < self.cards_per_hour

    async def post_card(self, card: Card, *, draft_hash: str = "") -> int | None:
        """Send a card with one-time-code buttons. Returns the bot message id, or None if held / no recipient."""
        now = self.clock.now()
        if card.to == "backup":
            if not self.backup_ids:
                return None
            card, recipient = strip_for_backup(card), self.backup_ids[0]
        elif card.to == "supervisor":                           # a helpdesk stage-gate card: her buttons only
            if not self.supervisor_id or any(b.action not in SUPERVISOR_ACTIONS for row in card.buttons for b in row):
                return None
            recipient = self.supervisor_id
        else:
            if not self.principal_id:
                return None
            recipient = self.principal_id
        if self.held and card.kind not in URGENT_KINDS:
            await self.flush_digest()
        if card.kind not in URGENT_KINDS and not self._room(now):
            self.held.append((card, recipient, draft_hash))
            return None
        return await self._send(card, recipient, draft_hash, now)

    async def _send(self, card: Card, recipient: int, draft_hash: str, now: datetime) -> int:
        if self._quiet(now) and card.kind not in LOUD_IN_QUIET_HOURS:
            card.silent = True
        expires = now + APPROVAL_TTL
        if card.buttons:
            card.expires_at = expires
        card.card_id = self.db.add_card(card)
        rid = card.request_id or NO_REQUEST

        def callback_for(button: Button) -> str:
            nonce = self.db.issue_approval(rid, card.card_id, button.action, recipient, draft_hash, expires)
            return make_callback(rid, button.action, nonce)

        bot_msg_id = await self.bot.send_card(card, callback_for)
        if card.kind != CardKind.done:      # a silent "closed" note never uses up the 10/h room for real cards
            self._sent_at.append(now)
        self._card_request[bot_msg_id] = card.request_id
        self.db.log_event("card_sent", card.request_id, card_id=card.card_id, card_kind=str(card.kind),
                          to=card.to, bot_msg_id=bot_msg_id)
        return bot_msg_id

    async def flush_digest(self) -> int | None:
        """Release what the 10-per-hour cap held back. The listener calls this on every tick.

        Cards WITH buttons (approvals) are never folded away: they go out one by one as the hour frees up.
        Cards without buttons are folded into one digest card. Returns the digest's bot message id, if any.
        """
        now, digest_id = self.clock.now(), None
        plain = [h for h in self.held if not h[0].buttons]
        with_buttons = [h for h in self.held if h[0].buttons]
        self.held = []
        if plain and self._room(now) and self.principal_id:
            lines = [f"{len(plain)} more updates (held to keep your phone quiet):"]
            lines += [f"- {(c.request_id + ' ') if c.request_id else ''}{(c.text or '').splitlines()[0][:110]}"
                      for c, _, _ in plain]
            digest_id = await self._send(Card(kind=CardKind.digest, text="\n".join(lines)), self.principal_id, "", now)
        elif plain:
            self.held.extend(plain)
        for card, recipient, draft_hash in with_buttons:
            if self._room(now):
                await self._send(card, recipient, draft_hash, now)
            else:
                self.held.append((card, recipient, draft_hash))
        return digest_id

    async def info(self, text: str, request_id: str | None = None, to_user: int | None = None) -> int | None:
        card = Card(kind=CardKind.info, text=text, request_id=request_id,
                    to="backup" if to_user and to_user != self.principal_id else "principal")
        return await self.post_card(card)

    # ------------------------------------------------------------ button presses

    async def on_callback(self, update: dict) -> None:
        uid, cb_id = int(update.get("user_id") or 0), update.get("callback_id", "")
        if not self.is_heard(uid) and not (self.supervisor_id and uid == self.supervisor_id):   # Supervisor: gate buttons only
            return                                         # strangers: silence, no answer, no log of content
        try:
            request_id, action, nonce = parse_callback(update.get("data", ""))
        except ValueError:
            await self.bot.answer_callback(cb_id, "That button is not valid.")
            return
        rid = None if request_id == NO_REQUEST else request_id
        if not self.is_heard(uid) and action not in SUPERVISOR_ACTIONS:
            return
        if action in SUPERVISOR_ACTIONS:
            if not self.supervisor_id or uid != self.supervisor_id:
                self.db.log_event("approval_refused", rid, actor=str(uid), action=action, why="not Supervisor")
                await self.bot.answer_callback(cb_id, "Only Supervisor can approve this move.")
                return
        elif action not in BACKUP_ACTIONS and not self.is_principal(uid):
            self.db.log_event("approval_refused", rid, actor=str(uid), action=action, why="not Principal")
            await self.bot.answer_callback(cb_id, "Only Principal can do that. You can Take over.")
            return
        row = self.db.use_approval(nonce, uid, action, request_id)
        if not row or int(row.get("allowed_user_id") or uid) != uid:
            await self.bot.answer_callback(cb_id, "This button expired or was already used.")
            return
        if action == "ed":
            prompt = f"{rid}: type the new reply. I will show it back to you before anything is sent."
            prompt_id = await self.bot.ask_force_reply(prompt, rid or NO_REQUEST)
            self._force_prompts[prompt_id] = rid or NO_REQUEST
        self.db.log_event("button", rid, actor=str(uid), action=action, nonce=nonce)
        self.bus.publish(Topics.CALLBACK_IN, {"user_id": uid, "request_id": rid, "action": action,
                                              "nonce": nonce, "callback_id": cb_id})
        await self.bot.answer_callback(cb_id, "OK")

    # ------------------------------------------------------------ replies to cards

    async def on_reply(self, update: dict) -> None:
        """A plain reply = instruction. Only the answer to OUR Edit prompt (tracked id, Principal only) is new text.
        The update's own 'force_reply' flag is never trusted."""
        uid = int(update.get("user_id") or 0)
        if not self.is_heard(uid):
            return
        to, text = update.get("to_bot_msg_id"), str(update.get("text") or "")
        if to in self._force_prompts and self.is_principal(uid):
            rid = self._force_prompts.pop(to)
            rid = None if rid == NO_REQUEST else rid
            echo = Card(kind=CardKind.edit_echo, request_id=rid,
                        text=f"Your edit, to send as you:\n\"{text}\"\nNothing goes out until you tap Send.",
                        buttons=[[Button("Send", "sd"), Button("Cancel", "cx")]])
            await self.post_card(echo, draft_hash=text_hash(text))
            self.bus.publish(Topics.REPLY_IN, {"user_id": uid, "request_id": rid, "text": text, "force_reply": True})
            return
        rid = self._card_request.get(to)
        self.bus.publish(Topics.REPLY_IN, {"user_id": uid, "request_id": rid, "text": text, "force_reply": False})
        await self.info("Got it. I will treat that as an instruction"
                        + (f" for {rid}" if rid else "") + " and send you a new card. Nothing was sent.",
                        rid, to_user=uid)

    # ------------------------------------------------------------ commands

    async def on_command(self, update: dict) -> None:
        uid = int(update.get("user_id") or 0)
        if not self.is_heard(uid):
            return
        cmd, args = str(update.get("command") or "").lower().lstrip("/"), str(update.get("args") or "").strip()
        if not self.is_principal(uid) and cmd not in BACKUP_COMMANDS:
            await self.info(f"Only Principal can use /{cmd}.", to_user=uid)
            return
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is not None and await handler(uid, args) is False:
            return                                          # handler said: do not pass on (bad input)
        self.db.log_event("command", None, actor=str(uid), command=cmd)
        self.bus.publish(Topics.COMMAND_IN, {"user_id": uid, "command": cmd, "args": args})

    async def _cmd_stop(self, uid: int, args: str):
        path = self._stop_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"stopped by {uid} at {utc_iso(self.clock.now())}\n", encoding="utf-8")
        self.bus.publish(Topics.KILL_CHANGED, {"on": True, "by": uid})
        await self.info("Stopped. Nothing will be sent for anyone until Principal sends /resume. "
                        "Reminders still come to you.", to_user=uid)

    async def _cmd_resume(self, uid: int, args: str):
        self._stop_file().unlink(missing_ok=True)
        self.bus.publish(Topics.KILL_CHANGED, {"on": False, "by": uid})
        await self.info("Resumed. Sending follows the lane dials again.", to_user=uid)

    async def _cmd_away(self, uid: int, args: str):
        span = parse_duration(args)
        if span is None:
            await self.info("Use /away 3h (up to 24h).", to_user=uid)
            return False
        until = shift(self.clock.now(), span)
        self.db.set_cursor("away_until", utc_iso(until))
        await self.info(f"OK. Until {until:%H:%M} I treat it as out of hours (faster holding replies).", to_user=uid)

    async def _cmd_pause(self, uid: int, args: str):
        """Watcher privacy switch (Principal only: not in BACKUP_COMMANDS). /pause or /pause 2h. Kept as an event so a
        later export still skips the paused stretch."""
        span = parse_duration(args) if args else None
        if args and span is None:
            await self.info("Use /pause, or /pause 2h (up to 24h).", to_user=uid)
            return False
        end = shift(self.clock.now(), span) if span else None      # real time: right across the summer-time change
        until = utc_iso(end) if end else None
        self.db.log_event("watcher_pause", None, actor=str(uid), until=until)
        await self.info("Watcher paused: nothing you do is recorded"
                        + (f" until {end:%H:%M}" if end else " until you send /unpause")
                        + ". The agent itself keeps working.", to_user=uid)

    async def _cmd_unpause(self, uid: int, args: str):
        self.db.log_event("watcher_unpause", None, actor=str(uid))
        await self.info("Watcher on again (only if you consented and it is switched on). /pause stops it.",
                        to_user=uid)

    async def _cmd_status(self, uid: int, args: str):
        try:
            open_n = len(self.db.open_requests())
        except Exception:                                   # noqa: BLE001 - status must never crash the bot
            open_n = "?"
        kill = "ON (nothing is sent)" if self._stop_file().exists() else "off"
        mode = f"{self.cfg.get('run_mode', 'dry_run')}, send_enabled={bool(self.cfg.get('send_enabled'))}"
        try:
            from ..watcher import gate
            watcher = gate.indicator(self.cfg, gate.pause_windows(self.db, self.clock.now()), self.clock.now())
        except Exception:                                   # noqa: BLE001 - status must never crash the bot
            watcher = "?"
        await self.info(f"Status\n- Kill switch: {kill}\n- Mode: {mode}\n- Open requests: {open_n}\n"
                        f"- Cards held for the digest: {len(self.held)}\n- Watcher: {watcher}", to_user=uid)
