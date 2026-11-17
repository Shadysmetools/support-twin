"""'Principal's Agent' bot: cards, buttons, one-time codes, commands, ForceReply (05 6, CONTRACT 7).

Two layers:
- `BotIO` = the transport (real: bot_api.TelegramBotApi; tests: fakes.FakeBot).
- `BotController` = the rules. It is the only code that turns a bot update into a bus message:
  * only Principal's user id and the backup ids are heard; everyone else is ignored silently;
  * every button is a one-time code (nonce) in `approvals`, valid 24 h, bound to one user id;
  * send-type buttons (ap, sd, as, ct) and every other action except Take over / Why are Principal's only
    (red-team #3); backup cards are stripped of them in code;
  * a plain reply to a card is an INSTRUCTION, never outgoing text. Only text typed into the ForceReply prompt that
    Edit opened replaces a draft, and it is echoed back as an edit_echo card with [Send] [Cancel] (red-team #1);
  * /stop writes the STOP file itself (works in under 5 s even if the listener is busy).
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Callable, Protocol

from ..models import Button, Card, CardKind
from ..redact import redact, strip_url_token

CALLBACK_MAX_BYTES = 64
APPROVAL_TTL = timedelta(hours=24)
CARD_MAX_CHARS = 3500
SEND_ACTIONS = frozenset({"ap", "sd", "as", "ct", "nv", "ac"})  # a message goes out as Principal (nv: text, no voice; ac: + close)
BACKUP_ACTIONS = frozenset({"to", "tn", "wy"})               # backup: info + Take over only
ACTIONS = frozenset("ap ed to tn gb ho wy sz dl fx ran am nn ct nt wf as rr nv pm ny sd cx rs hb ck kp rc".split()
                    # 2026-11-03 helpdesk + timesheets: hk create ticket, hx not a ticket, ag/ax Supervisor's stage gate,
                    # ta approve all timesheets, te edit, tr reject. Writes, never sends.
                    + "hk hx ag ax ta te tr".split()
                    # 2026-11-10: ac = Approve + close (post the reply, then close the request and its ticket)
                    + ["ac"])
SUPERVISOR_ACTIONS = frozenset({"ag", "ax"})                      # only Supervisor's own id may press these
BACKUP_COMMANDS = frozenset({"stop", "status", "take", "why", "board"})
URGENT_KINDS = frozenset({CardKind.holding_blocked, CardKind.circuit_breaker, CardKind.edit_echo, CardKind.info,
                          CardKind.escalation})          # never held back by the 10/hour cap
NO_REQUEST = "-"


class BotIO(Protocol):
    async def start(self, on_callback, on_command, on_reply) -> None: ...
    async def stop(self) -> None: ...
    async def send_card(self, card: Card, callback_for: Callable[[Button], str]) -> int: ...
    async def edit_card(self, bot_msg_id: int, card: Card, callback_for) -> None: ...
    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    async def ask_force_reply(self, prompt: str, request_id: str) -> int: ...


# ---------------------------------------------------------------- pure helpers

def make_callback(request_id: str, action: str, nonce: str) -> str:
    """"REQ-0142|ap|<nonce>", at most 64 bytes."""
    data = f"{request_id or NO_REQUEST}|{action}|{nonce}"
    if "|" in (request_id or "") + action + nonce or len(data.encode()) > CALLBACK_MAX_BYTES:
        raise ValueError("bad callback parts")
    return data


def parse_callback(data: str) -> tuple[str, str, str]:
    """Raises ValueError if malformed or longer than 64 bytes."""
    if not isinstance(data, str) or len(data.encode()) > CALLBACK_MAX_BYTES:
        raise ValueError("callback data too long or not text")
    parts = data.split("|")
    if len(parts) != 3 or not all(parts):
        raise ValueError("callback data must be request|action|nonce")
    request_id, action, nonce = parts
    if action not in ACTIONS or not (request_id == NO_REQUEST or re.fullmatch(r"REQ-\d{4,}", request_id)):
        raise ValueError("unknown action or request id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", nonce):
        raise ValueError("bad nonce")
    return request_id, action, nonce


def _clean_links(text: str) -> str:
    """Ticket links on cards never carry the portal access token."""
    return re.sub(r"https?://\S+", lambda m: strip_url_token(m.group(0)), text)


def render_card(card: Card) -> str:
    """Plain-text body: redacted, token-free links, at most 3500 characters."""
    text = redact(_clean_links(card.text or "")).strip()
    if card.request_id and not text.startswith(card.request_id):
        text = f"{card.request_id}  {text}"
    if len(text) > CARD_MAX_CHARS:
        text = text[:CARD_MAX_CHARS - 20].rstrip() + "\n... (cut, see board)"
    return text


def build_card(kind: CardKind, request_id: str | None, *, headline: str, happened: str = "", did: str = "",
               need: str = "", ticket_url: str | None = None, buttons: list[list[Button]] | None = None,
               to: str = "principal") -> Card:
    """The 05 section 6 shape: what happened, what I did, what I need from you, plus the ticket link."""
    lines = [headline]
    for label, val in (("What happened", happened), ("What I did", did), ("What I need from you", need)):
        if val:
            lines.append(f"{label}: {val}")
    if ticket_url:
        lines.append(f"Ticket: {strip_url_token(ticket_url)}")
    return Card(kind=kind, text="\n".join(lines), request_id=request_id, buttons=buttons or [], to=to)


def strip_for_backup(card: Card) -> Card:
    """The backup never sees a button that sends as Principal (or any non-info action)."""
    rows = [[b for b in row if b.action in BACKUP_ACTIONS] for row in card.buttons]
    card.buttons = [r for r in rows if r]
    return card


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_duration(arg: str) -> timedelta | None:
    """'3h', '90m', '1h30m' -> timedelta (max 24 h). None if not understood."""
    m = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*", (arg or "").lower())
    if not m or not any(m.groups()):
        return None
    d = timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    return d if timedelta(0) < d <= timedelta(hours=24) else None


def __getattr__(name: str):
    """bot_io.BotController / bot_io.TelegramBotApi live in their own small files (lazy, no import cycle)."""
    if name == "BotController":
        from .bot_controller import BotController
        return BotController
    if name == "TelegramBotApi":
        from .bot_api import TelegramBotApi
        return TelegramBotApi
    raise AttributeError(name)
