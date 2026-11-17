"""Fakes for every external adapter (CONTRACT section 12). No network, ever.

Each fake records what it was asked to do, so tests can assert things like "0 sends in observe".
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..clock import CAIRO, FakeClock  # re-exported for tests
from ..models import Button, Card, Channel, Company, Event, OutboundMessage, Sender

FIXTURES = Path(__file__).parent / "fixtures"

# Synthetic identities (never real people)
PRINCIPAL_ID = 1001
BACKUP_ID = 1002
STAFF_A = Sender(tg_id=2001, name="Staff A", dept="Logistics")
STAFF_B = Sender(tg_id=2002, name="Staff B", dept="Stores")
PRINCIPAL = Sender(tg_id=PRINCIPAL_ID, name="Principal Test", is_principal=True)
BOT_SENDER = Sender(tg_id=3001, name="Bot X", is_bot=True)
GROUP_ID = -100500
TOPIC_LOGISTICS = 7
TOPIC_STORES = 8

__all__ = ["FakeTelegram", "FakeBot", "FakeGmail", "FakeClaude", "FakeKeyring", "FakeClock", "FakeClaudeResult",
           "canned_packet", "make_event", "FIXTURES", "PRINCIPAL_ID", "BACKUP_ID", "STAFF_A", "STAFF_B", "PRINCIPAL",
           "BOT_SENDER", "GROUP_ID", "TOPIC_LOGISTICS", "TOPIC_STORES", "CAIRO"]


class FakeSendError(Exception):
    """Raised by a fake when .raise_on_send is set (e.g. 'FloodWait', 'PeerFlood', 'NetworkDrop')."""


def canned_packet(request_id: str = "REQ-0142") -> dict:
    p = json.loads((FIXTURES / "packet_req0142.json").read_text(encoding="utf-8"))
    p["request_id"] = request_id
    return p


def make_event(text: str = "WAVE/00001", *, msg_id: int = 1, sender: Sender = STAFF_A, ts: datetime | None = None,
               chat_id: int = GROUP_ID, topic_id: int | None = TOPIC_LOGISTICS, photo: bool = False,
               reply_to: int | None = None, channel: Channel = Channel.telegram_group, edited: bool = False) -> Event:
    ts = ts or datetime(2026, 11, 1, 14, 40, tzinfo=CAIRO)
    return Event(
        event_id=f"tg:{chat_id}:{msg_id}", channel=channel, chat="Test Group", chat_id=chat_id,
        topic="Logistics" if topic_id == TOPIC_LOGISTICS else ("Stores" if topic_id == TOPIC_STORES else None),
        topic_id=topic_id, sender=sender, ts=ts, text=text, media=[f"photo_{msg_id}.jpg"] if photo else [],
        reply_to=reply_to, company_hint=Company.EG, untrusted=not sender.is_principal, msg_id=msg_id, edited=edited,
    )


# ---------------------------------------------------------------- Telegram (user account: in + send)

class FakeTelegram:
    """Implements TelegramIn and TelegramSend."""

    def __init__(self, my_user_id: int = PRINCIPAL_ID):
        self.my_user_id = my_user_id
        self.from_id = my_user_id          # set to another id to simulate posting as the anonymous group
        self.sent: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.thread: list[Event] = []      # what read_thread / fetch_since return
        self.raise_on_send: str | None = None
        self._on_event: Callable[[Event], Awaitable[None]] | None = None
        self._next_id = 10_000
        self.started = False

    # TelegramIn
    async def start(self, on_event: Callable[[Event], Awaitable[None]]) -> None:
        self._on_event, self.started = on_event, True

    async def inject(self, ev: Event) -> None:
        """Simulate a live message arriving."""
        self.thread.append(ev)
        if self._on_event:
            await self._on_event(ev)

    async def fetch_since(self, chat_id: int, topic_id: int | None, min_id: int, limit: int = 200) -> list[Event]:
        out = [e for e in self.thread if e.chat_id == chat_id and (topic_id is None or e.topic_id == topic_id)
               and int(e.msg_id or 0) > min_id]
        return out[:limit]

    async def latest_id(self, chat_id: int, topic_id: int | None) -> int:
        return max((int(e.msg_id or 0) for e in self.thread if e.chat_id == chat_id
                    and (topic_id is None or e.topic_id == topic_id)), default=0)

    async def stop(self) -> None:
        self.started = False

    # TelegramSend
    async def send_message(self, chat_id: int, text: str, *, reply_to: int | None, topic_id: int | None) -> dict:
        if self.raise_on_send:
            raise FakeSendError(self.raise_on_send)
        self._next_id += 1
        rec = {"chat_id": chat_id, "text": text, "reply_to": reply_to, "topic_id": topic_id,
               "msg_id": self._next_id, "from_id": self.from_id}
        self.sent.append(rec)
        return {"msg_id": self._next_id, "from_id": self.from_id}

    async def delete_message(self, chat_id: int, msg_id: int) -> None:
        self.deleted.append((chat_id, msg_id))

    async def read_thread(self, chat_id: int, topic_id: int | None, after_msg_id: int) -> list[Event]:
        return await self.fetch_since(chat_id, topic_id, after_msg_id)


# ---------------------------------------------------------------- Agent bot

class FakeBot:
    """Implements BotIO. Tests drive it with press(), command() and reply()."""

    def __init__(self):
        self.cards: list[tuple[int, Card, list[list[str]]]] = []   # (bot_msg_id, card, callback data per button)
        self.edits: list[tuple[int, Card]] = []
        self.answered: list[tuple[str, str]] = []
        self.force_replies: list[tuple[int, str, str]] = []
        self._cb = self._cmd = self._reply = None
        self._next = 0

    async def start(self, on_callback, on_command, on_reply) -> None:
        self._cb, self._cmd, self._reply = on_callback, on_command, on_reply

    async def stop(self) -> None:
        pass

    async def send_card(self, card: Card, callback_for: Callable[[Button], str]) -> int:
        self._next += 1
        datas = [[callback_for(b) for b in row] for row in card.buttons]
        self.cards.append((self._next, card, datas))
        return self._next

    async def edit_card(self, bot_msg_id: int, card: Card, callback_for) -> None:
        self.edits.append((bot_msg_id, card))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    async def ask_force_reply(self, prompt: str, request_id: str) -> int:
        self._next += 1
        self.force_replies.append((self._next, prompt, request_id))
        return self._next

    def last_card(self) -> Card | None:
        return self.cards[-1][1] if self.cards else None

    async def press(self, data: str, user_id: int = PRINCIPAL_ID, callback_id: str = "cb1") -> None:
        await self._cb({"user_id": user_id, "data": data, "callback_id": callback_id})

    async def command(self, text: str, user_id: int = PRINCIPAL_ID) -> None:
        cmd, _, args = text.partition(" ")
        await self._cmd({"user_id": user_id, "command": cmd.lstrip("/"), "args": args})

    async def reply(self, text: str, *, to_bot_msg_id: int, user_id: int = PRINCIPAL_ID, force_reply: bool = False) -> None:
        await self._reply({"user_id": user_id, "text": text, "to_bot_msg_id": to_bot_msg_id,
                           "force_reply": force_reply})


# ---------------------------------------------------------------- Gmail

class FakeGmail:
    """Implements GmailIn and GmailSend."""

    def __init__(self):
        self.inbox: list[Event] = []
        self.drafts: dict[str, OutboundMessage] = {}
        self.sent: list[OutboundMessage] = []
        self.history_id = 0
        self.raise_on_send: str | None = None

    def add(self, ev: Event) -> None:
        self.inbox.append(ev)

    def list_new(self, mailbox: str, history_id: str | None) -> tuple[list[Event], str]:
        start = int(history_id or 0)
        new = self.inbox[start:]
        return new, str(len(self.inbox))

    def create_draft(self, msg: OutboundMessage) -> str:
        draft_id = f"d{len(self.drafts) + 1}"
        self.drafts[draft_id] = msg
        return draft_id

    def send_draft(self, draft_id: str) -> str:
        if self.raise_on_send:
            raise FakeSendError(self.raise_on_send)
        self.sent.append(self.drafts.pop(draft_id))
        return f"m{len(self.sent)}"


# ---------------------------------------------------------------- Claude

@dataclass
class FakeClaudeResult:
    ok: bool
    text: str
    session_id: str | None
    exit_code: int = 0
    duration_s: float = 0.1
    error: str = ""


class FakeClaude:
    """Implements ClaudeRunner. Worker -> canned packet; checks -> pass verdicts. Override via .responses[kind]."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[str, Any] = {}
        self.fail_kinds: set[str] = set()

    def _default(self, kind: str, prompt: str) -> Any:
        if kind == "worker":
            import re
            m = re.search(r"REQ-\d{4,}", prompt)
            return canned_packet(m.group(0) if m else "REQ-0142")
        if kind == "classifier":
            return {"lane": "L4_t2a_fix_proposal", "tier": "T2a"}
        # Satisfies BOTH refuter contracts: judge.* read "pass"; the cross-model ClaudeRefuter reads "refuted".
        return {"pass": True, "refuted": False, "reasons": [], "items": []}

    def run(self, prompt: str, *, kind: str, cwd: Path, session_id: str | None = None,
            timeout_s: int = 900, model: str | None = None) -> FakeClaudeResult:
        self.calls.append({"kind": kind, "prompt": prompt, "cwd": str(cwd), "session_id": session_id,
                           "model": model})
        sid = session_id or f"sess-{len(self.calls)}"
        if kind in self.fail_kinds:
            return FakeClaudeResult(False, "", sid, exit_code=1, error="fake failure")
        body = self.responses.get(kind)
        body = copy.deepcopy(body) if body is not None else self._default(kind, prompt)
        if callable(body):
            body = body(prompt)
        text = body if isinstance(body, str) else "```json\n" + json.dumps(body) + "\n```"
        return FakeClaudeResult(True, text, sid)


# ---------------------------------------------------------------- keyring

class FakeKeyring:
    """In-memory stand-in for keyring's backend (get_password / set_password / delete_password)."""

    priority = 1

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)
