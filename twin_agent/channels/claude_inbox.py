"""BotIO that talks to Principal through his Claude chat instead of the Telegram bot (Principal, 2026-11-09:
"change what you send me in telegram to claude").

- Every card goes as one JSON line to data/claude_inbox.jsonl: text, screenshot path, and each button's callback.
  A Claude session watching that file shows the card to Principal in chat.
- Principal's answers come back as JSON lines in data/claude_actions.jsonl (written by `python -I -m
  twin_agent.channels.claude_inbox ...` from the Claude session). start() reads new lines every 2 s and hands them
  to the same BotController rules as a Telegram tap, as Principal's user id: nonces, one use, 24 h, Principal-only actions.
- The two files sit next to state.db, so whoever can write them could already write the DB. Nothing here sends.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..models import Button, Card
from .bot_io import render_card

log = logging.getLogger("twin_agent.claude_inbox")
INBOX, ACTIONS, OFFSET = "claude_inbox.jsonl", "claude_actions.jsonl", "claude_actions.offset"
LABELS = {"approve": "ap", "edit": "ed", "take": "to", "takeover": "to", "why": "wy", "send": "sd", "cancel": "cx"}


def _data_dir() -> Path:
    from .. import paths
    return Path(paths.state_db()).parent


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


class ClaudeInbox:
    def __init__(self, cfg: dict, *, data_dir: Path | None = None, poll_seconds: float = 2.0):
        people = cfg.get("people") or {}
        self.principal_id = int(people.get("principal_tg_user_id") or 0)
        self.dir = Path(data_dir) if data_dir else _data_dir()
        self.inbox, self.actions, self.offset_file = self.dir / INBOX, self.dir / ACTIONS, self.dir / OFFSET
        self.poll_seconds = poll_seconds
        self._next = int(time.time() * 1000) % 2_000_000_000      # unique across restarts
        self._running = False
        self._handlers: tuple = (None, None, None)
        self._last_prompt: int | None = None

    def __repr__(self) -> str:
        return f"ClaudeInbox({self.inbox})"

    def _id(self) -> int:
        self._next += 1
        return self._next

    def _write(self, rtype: str, **rec) -> int:
        msg_id = self._id()
        _append(self.inbox, {"id": msg_id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "type": rtype, **rec})
        self._trim()
        return msg_id

    def _trim(self, max_bytes: int = 1_500_000, keep: int = 400) -> None:
        """The Claude panel reads the whole file (4 MiB cap): past max_bytes keep only the newest `keep` lines."""
        try:
            if self.inbox.stat().st_size <= max_bytes:
                return
            lines = self.inbox.read_text(encoding="utf-8").splitlines(keepends=True)[-keep:]
            tmp = self.inbox.with_suffix(".tmp")
            tmp.write_text("".join(lines), encoding="utf-8")
            tmp.replace(self.inbox)
        except OSError as e:
            log.warning("claude inbox trim failed: %s", type(e).__name__)

    # ------------------------------------------------------------ BotIO

    async def send_card(self, card: Card, callback_for: Callable[[Button], str]) -> int:
        buttons = [{"label": b.label, "action": b.action, "callback": callback_for(b)}
                   for row in card.buttons for b in row]
        photo = next((str(Path(p)) for p in card.attachments[:1] if Path(p).is_file()), None)
        return self._write("card", kind=str(card.kind), request_id=card.request_id, text=render_card(card),
                           photo=photo, buttons=buttons, chat=card.chat or self._chat_of(card.request_id))

    def _chat_of(self, request_id: str | None) -> str | None:
        """The request's chat name from state.db next to the inbox, read-only (2026-11-12: the panel shows Acme
        Team cards in one session and the rest in another). None for system cards or when unknown."""
        if not request_id:
            return None
        db_path = self.dir / "state.db"
        if not db_path.is_file():
            return None
        import sqlite3
        try:
            db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                row = db.execute("SELECT chat FROM requests WHERE id = ?", (request_id,)).fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return None
        return str(row[0]) if row and row[0] else None

    async def edit_card(self, bot_msg_id: int, card: Card, callback_for) -> None:
        buttons = [{"label": b.label, "action": b.action, "callback": callback_for(b)}
                   for row in card.buttons for b in row]
        self._write("edit", of=bot_msg_id, request_id=card.request_id, text=render_card(card), buttons=buttons,
                    chat=card.chat or self._chat_of(card.request_id))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        if text and text != "OK":
            self._write("toast", text=text[:190])

    async def ask_force_reply(self, prompt: str, request_id: str) -> int:
        self._last_prompt = self._write("edit_prompt", request_id=request_id, text=prompt)
        return self._last_prompt

    async def start(self, on_callback, on_command, on_reply) -> None:
        self._handlers, self._running = (on_callback, on_command, on_reply), True
        while self._running:
            try:
                await self.poll_once()
            except Exception as e:                 # noqa: BLE001 - one bad line never stops the loop
                log.warning("claude inbox poll failed: %s", type(e).__name__)
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------ Principal's answers

    def _offset(self) -> int:
        try:
            return int(self.offset_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    async def poll_once(self) -> int:
        if not self.actions.is_file():
            return 0
        start = self._offset()
        with self.actions.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read()
        done = chunk.rfind(b"\n") + 1                 # only whole lines
        if done <= 0:
            return 0
        self.offset_file.write_text(str(start + done), encoding="utf-8")   # before acting: a line never runs twice
        n = 0
        for raw in chunk[:done].decode("utf-8").splitlines():
            if raw.strip():
                await self.dispatch(json.loads(raw))
                n += 1
        return n

    async def dispatch(self, act: dict) -> None:
        on_callback, on_command, on_reply = self._handlers
        uid, kind = self.principal_id, act.get("type")
        if kind == "tap":
            self._last_prompt = None
            await on_callback({"user_id": uid, "data": act["callback"], "callback_id": f"claude-{self._id()}"})
            if act.get("text") is not None and self._last_prompt:      # Edit: answer the prompt it just opened
                await on_reply({"user_id": uid, "text": act["text"], "to_bot_msg_id": self._last_prompt})
        elif kind == "command":
            await on_command({"user_id": uid, "command": act["command"], "args": act.get("args", "")})
        elif kind == "reply":
            await on_reply({"user_id": uid, "text": act["text"], "to_bot_msg_id": act.get("to")})


# ---------------------------------------------------------------- CLI for the Claude session

def find_callback(inbox: Path, request_id: str, action: str) -> str | None:
    """The newest card for this request that carries this button."""
    if not inbox.is_file():
        return None
    found = None
    for raw in inbox.read_text(encoding="utf-8").splitlines():
        rec = json.loads(raw) if raw.strip() else {}
        if rec.get("type") in ("card", "edit") and rec.get("request_id") == request_id:
            for b in rec.get("buttons") or []:
                if b.get("action") == action:
                    found = b["callback"]
    return found


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude_inbox", description="Answer a Digital Twin card from the Claude chat.")
    p.add_argument("what", help="approve | edit | take | why | send | cancel | command | reply")
    p.add_argument("target", help="REQ-x, or the command name for 'command', or the card id for 'reply'")
    p.add_argument("text", nargs="?", default=None, help="edit / reply text, or command args")
    p.add_argument("--data-dir", default=None)
    a = p.parse_args(argv)
    d = Path(a.data_dir) if a.data_dir else _data_dir()
    if a.what == "command":
        rec = {"type": "command", "command": a.target.lstrip("/"), "args": a.text or ""}
    elif a.what == "reply":
        rec = {"type": "reply", "to": int(a.target), "text": a.text or ""}
    else:
        action = LABELS.get(a.what) or a.what
        cb = find_callback(d / INBOX, a.target, action)
        if not cb:
            print(f"No {a.what} button on any card for {a.target}.", file=sys.stderr)
            return 2
        rec = {"type": "tap", "callback": cb}
        if action == "ed":
            if not a.text:
                print("edit needs the new reply text.", file=sys.stderr)
                return 2
            rec["text"] = a.text
    _append(d / ACTIONS, rec)
    print(f"queued: {a.what} {a.target}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
