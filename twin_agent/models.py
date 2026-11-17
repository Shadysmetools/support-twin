"""Shared enums, data shapes and the request state machine (CONTRACT sections 2 to 4).

Every component imports its types from here so they all speak the same language.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .clock import CAIRO


# ---------------------------------------------------------------- enums

class Channel(StrEnum):
    telegram_group = "telegram_group"
    telegram_dm = "telegram_dm"
    email = "email"
    ticket_mail = "ticket_mail"
    bot = "bot"


class Company(StrEnum):
    EG = "EG"
    KSA = "KSA"
    UNKNOWN = "UNKNOWN"


ODOO_COMPANY_ID = {Company.EG: 1, Company.KSA: 2}


class Tier(StrEnum):
    T1 = "T1"
    T2a = "T2a"
    T2b = "T2b"
    T3 = "T3"
    T4 = "T4"
    UNKNOWN = "UNKNOWN"


class Dial(StrEnum):
    """Trust ladder per lane. 'draft' means Co-pilot: a card, Principal taps."""
    off = "off"
    observe = "observe"
    draft = "draft"
    auto_on_timeout = "auto_on_timeout"
    auto = "auto"

    def rank(self) -> int:
        return _DIAL_ORDER.index(self)

    def at_least(self, other: "Dial") -> bool:
        return self.rank() >= Dial(other).rank()


_DIAL_ORDER = [Dial.off, Dial.observe, Dial.draft, Dial.auto_on_timeout, Dial.auto]


class Lane(StrEnum):
    L1_holding = "L1_holding"
    L2_evidence_ask = "L2_evidence_ask"
    L3_t1_answer = "L3_t1_answer"
    L4_t2a_fix_proposal = "L4_t2a_fix_proposal"
    L5_t2b_config = "L5_t2b_config"
    L6_ticket_email = "L6_ticket_email"
    L7_ticket_chase = "L7_ticket_chase"
    L8_approver_question = "L8_approver_question"
    L9_worked_followup = "L9_worked_followup"
    L10_proof_video = "L10_proof_video"
    L11_internal_diagnosis = "L11_internal_diagnosis"
    L12_email_reply = "L12_email_reply"


class OutKind(StrEnum):
    holding = "holding"
    evidence_ask = "evidence_ask"
    answer = "answer"
    after_fix = "after_fix"
    worked_chaser = "worked_chaser"
    ticket_link = "ticket_link"
    ticket_email = "ticket_email"
    ticket_update = "ticket_update"
    email_reply = "email_reply"
    email_holding = "email_holding"
    approver_question = "approver_question"
    proof_link = "proof_link"


LANE_FOR_KIND: dict[OutKind, Lane] = {
    OutKind.holding: Lane.L1_holding,
    OutKind.evidence_ask: Lane.L2_evidence_ask,
    OutKind.answer: Lane.L3_t1_answer,
    OutKind.after_fix: Lane.L4_t2a_fix_proposal,
    OutKind.worked_chaser: Lane.L9_worked_followup,
    OutKind.ticket_link: Lane.L7_ticket_chase,
    OutKind.ticket_email: Lane.L6_ticket_email,
    OutKind.ticket_update: Lane.L7_ticket_chase,
    OutKind.email_reply: Lane.L12_email_reply,
    OutKind.email_holding: Lane.L12_email_reply,
    OutKind.approver_question: Lane.L8_approver_question,
    OutKind.proof_link: Lane.L10_proof_video,
}


class SendTrigger(StrEnum):
    approval = "approval"          # Principal tapped Approve
    principal_edit = "principal_edit"      # Principal's own Edit text, after [Send]
    timeout = "timeout"            # autopilot after a timer
    holding_floor = "holding_floor"  # the hard-floor holding reply


class SendStatus(StrEnum):
    sent = "sent"
    would_send = "would_send"
    blocked = "blocked"
    duplicate = "duplicate"
    maybe_sent = "maybe_sent"
    failed = "failed"
    drafted = "drafted"        # 2026-11-11: an email reply saved as a Gmail draft; Principal sends it himself


class CardKind(StrEnum):
    new_request = "new_request"
    run_this = "run_this"
    autopilot_happened = "autopilot_happened"
    observe_would_send = "observe_would_send"
    ticket = "ticket"
    proof_ready = "proof_ready"
    digest = "digest"
    promotion = "promotion"
    demotion = "demotion"
    health = "health"
    reminder = "reminder"
    escalation = "escalation"
    stalled = "stalled"
    holding_blocked = "holding_blocked"
    circuit_breaker = "circuit_breaker"
    could_not_verify = "could_not_verify"
    edit_echo = "edit_echo"
    done = "done"
    info = "info"
    timesheet = "timesheet"          # 2026-11-03: the daily timesheet card (Approve all / Edit / Reject)


class TimerKind(StrEnum):
    burst_close = "burst_close"
    worker_hard_stop = "worker_hard_stop"
    reminder = "reminder"
    holding = "holding"
    verified_answer = "verified_answer"
    escalate_backup = "escalate_backup"
    takeover_backup = "takeover_backup"
    evidence_chase_1 = "evidence_chase_1"
    evidence_chase_2 = "evidence_chase_2"
    email_holding = "email_holding"
    promised_update_card = "promised_update_card"
    promised_update_due = "promised_update_due"
    approval_reping_1 = "approval_reping_1"
    approval_reping_2 = "approval_reping_2"
    approval_expire = "approval_expire"
    supervisor_review = "supervisor_review"
    worked_chaser_1 = "worked_chaser_1"
    worked_chaser_2 = "worked_chaser_2"
    ticket_readback_1 = "ticket_readback_1"
    ticket_readback_2 = "ticket_readback_2"
    ticket_idle = "ticket_idle"
    client_question = "client_question"
    client_testing_due = "client_testing_due"
    sla_warning = "sla_warning"
    paused_recard = "paused_recard"
    weekly_recheck = "weekly_recheck"
    answered_close = "answered_close"     # 48 working h after Principal's own informational answer -> CLOSED_ANSWERED
    # global timers (request_id is NULL)
    sweep_light = "sweep_light"
    sweep_full = "sweep_full"
    digest = "digest"
    scorecard = "scorecard"
    deadman_ping = "deadman_ping"


REPLY_TIMERS = (TimerKind.reminder, TimerKind.holding, TimerKind.verified_answer)
"""Cancelled when Principal answers the request himself."""


# ---------------------------------------------------------------- state machine

class State(StrEnum):
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    AWAITING_FIRST_REPLY = "AWAITING_FIRST_REPLY"
    ACKED = "ACKED"
    NEEDS_INFO = "NEEDS_INFO"
    DIAGNOSED = "DIAGNOSED"
    PROPOSED = "PROPOSED"
    FIX_PENDING = "FIX_PENDING"
    TICKETED = "TICKETED"
    VERIFYING = "VERIFYING"
    PROOF_SENT = "PROOF_SENT"
    FIXED_UNCONFIRMED = "FIXED_UNCONFIRMED"
    CLOSED_PROVEN = "CLOSED_PROVEN"
    CLOSED_REREAD_PROVEN = "CLOSED_REREAD_PROVEN"
    CLOSED_BY_PRINCIPAL = "CLOSED_BY_PRINCIPAL"
    CLOSED_ANSWERED = "CLOSED_ANSWERED"   # Principal answered himself; requester acked, or silent 48 working h (info)
    REOPENED = "REOPENED"
    TAKEN_OVER = "TAKEN_OVER"
    ESCALATED = "ESCALATED"
    STALLED = "STALLED"
    PAUSED = "PAUSED"
    FAILED_WORKER = "FAILED_WORKER"


S = State
TERMINAL = frozenset({S.CLOSED_PROVEN, S.CLOSED_REREAD_PROVEN, S.CLOSED_BY_PRINCIPAL, S.CLOSED_ANSWERED})
OVERLAY = frozenset({S.TAKEN_OVER, S.ESCALATED, S.STALLED, S.PAUSED})
AUTO_CLOSED = frozenset({S.CLOSED_PROVEN, S.CLOSED_REREAD_PROVEN, S.CLOSED_ANSWERED})
"""Closed by the closing rules (closing.py), not by a tap. Each one can be reopened within the reopen window."""

TRANSITIONS: dict[State, frozenset[State]] = {
    S.NEW: frozenset({S.TRIAGED}),
    S.TRIAGED: frozenset({S.AWAITING_FIRST_REPLY, S.ACKED, S.NEEDS_INFO, S.DIAGNOSED, S.FAILED_WORKER,
                          S.CLOSED_ANSWERED}),
    S.AWAITING_FIRST_REPLY: frozenset({S.ACKED, S.NEEDS_INFO, S.DIAGNOSED, S.FAILED_WORKER, S.CLOSED_ANSWERED}),
    S.ACKED: frozenset({S.NEEDS_INFO, S.DIAGNOSED, S.FAILED_WORKER, S.CLOSED_ANSWERED}),
    S.NEEDS_INFO: frozenset({S.TRIAGED, S.ACKED, S.DIAGNOSED}),
    S.DIAGNOSED: frozenset({S.PROPOSED, S.PROOF_SENT, S.NEEDS_INFO, S.TRIAGED, S.CLOSED_ANSWERED}),
    S.PROPOSED: frozenset({S.FIX_PENDING, S.TICKETED, S.PROOF_SENT, S.DIAGNOSED, S.TRIAGED, S.NEEDS_INFO,
                           S.CLOSED_ANSWERED}),
    S.FIX_PENDING: frozenset({S.VERIFYING, S.DIAGNOSED}),
    S.TICKETED: frozenset({S.VERIFYING, S.DIAGNOSED}),
    S.VERIFYING: frozenset({S.PROOF_SENT, S.DIAGNOSED}),
    S.PROOF_SENT: frozenset({S.CLOSED_PROVEN, S.FIXED_UNCONFIRMED, S.CLOSED_REREAD_PROVEN, S.REOPENED}),
    S.FIXED_UNCONFIRMED: frozenset({S.CLOSED_PROVEN, S.CLOSED_REREAD_PROVEN, S.REOPENED}),
    S.CLOSED_PROVEN: frozenset({S.REOPENED}),
    S.CLOSED_REREAD_PROVEN: frozenset({S.REOPENED}),
    S.CLOSED_BY_PRINCIPAL: frozenset({S.REOPENED}),
    S.CLOSED_ANSWERED: frozenset({S.REOPENED}),
    S.REOPENED: frozenset({S.TRIAGED}),
    S.FAILED_WORKER: frozenset({S.TRIAGED, S.DIAGNOSED, S.ACKED, S.CLOSED_ANSWERED}),
    # overlay states: exits handled in can_transition (back to prev_state)
    S.TAKEN_OVER: frozenset(),
    S.ESCALATED: frozenset(),
    S.STALLED: frozenset(),
    S.PAUSED: frozenset(),
}


class IllegalTransition(Exception):
    pass


def can_transition(src: State, dst: State, prev_state: State | None = None) -> bool:
    """True if a request may move from src to dst (CONTRACT section 3)."""
    src, dst = State(src), State(dst)
    if src == dst:
        return False
    if src in OVERLAY:
        # Leave an overlay: back to where it was, to another overlay, or closed by Principal. A closing rule may also
        # close it if the state it came from could be closed that way (e.g. taken over, Principal answered, "تمام").
        if dst in AUTO_CLOSED and prev_state is not None and State(prev_state) not in OVERLAY:
            return dst in TRANSITIONS[State(prev_state)]
        return dst == prev_state or dst in OVERLAY or dst == S.CLOSED_BY_PRINCIPAL
    if src in TERMINAL:
        return dst in TRANSITIONS[src]
    if dst in OVERLAY or dst == S.CLOSED_BY_PRINCIPAL:
        return True
    return dst in TRANSITIONS[src]


def is_open(state: State | str) -> bool:
    return State(state) not in TERMINAL


# ---------------------------------------------------------------- time helpers

def utc_iso(dt: datetime | None) -> str | None:
    """Store a datetime as ISO-8601 in UTC, so SQLite text comparison sorts correctly across Egypt's DST change."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime: use clock.now()")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=CAIRO)).astimezone(CAIRO)


# ---------------------------------------------------------------- data shapes

@dataclass(frozen=True)
class Sender:
    tg_id: int = 0
    name: str = ""
    dept: str = ""
    email: str = ""
    is_principal: bool = False
    is_bot: bool = False


@dataclass
class Event:
    """Normalised inbound message from any channel (05 1.3)."""
    event_id: str
    channel: Channel
    chat: str
    chat_id: int | str
    topic: str | None
    topic_id: int | None
    sender: Sender
    ts: datetime
    text: str
    media: list[str] = field(default_factory=list)
    reply_to: int | str | None = None
    refs: list[str] = field(default_factory=list)
    company_hint: Company = Company.UNKNOWN
    untrusted: bool = True
    msg_id: int | str | None = None
    edited: bool = False
    found_by_sweep: bool = False
    subject: str = ""
    thread_id: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    reaction: str = ""          # a Telegram reaction (e.g. "👍") by sender ON message reply_to; text is empty

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = utc_iso(self.ts)
        d["channel"] = str(self.channel)
        d["company_hint"] = str(self.company_hint)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        d = dict(d)
        d["sender"] = Sender(**d.get("sender", {}))
        d["ts"] = parse_ts(d["ts"])
        d["channel"] = Channel(d["channel"])
        d["company_hint"] = Company(d.get("company_hint", "UNKNOWN"))
        return cls(**d)


@dataclass
class GuardResult:
    ok: bool
    blocked_by: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OutboundMessage:
    """The only input to sender.send."""
    request_id: str
    step: str
    kind: OutKind
    channel: Channel
    text: str
    chat_id: int | str | None = None
    topic_id: int | None = None
    reply_to: int | str | None = None
    is_template: bool = False
    filled_date: str | None = None
    draft_hash: str = ""
    draft_made_at: datetime | None = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    in_reply_to: str = ""
    thread_id: str = ""
    voice_path: str | None = None  # voice note (OGG/Opus) of exactly `text`; sender re-checks voice_sha256
    voice_sha256: str = ""
    voice_lang: str | None = None

    @property
    def send_key(self) -> str:
        return f"{self.request_id}:{self.step}"

    @property
    def lane(self) -> Lane:
        return LANE_FOR_KIND[OutKind(self.kind)]


@dataclass
class SendResult:
    status: SendStatus
    send_key: str
    reason: str = ""
    remote_id: str | None = None
    guard: GuardResult | None = None


@dataclass
class Button:
    label: str
    action: str


@dataclass
class Card:
    kind: CardKind
    text: str
    request_id: str | None = None
    buttons: list[list[Button]] = field(default_factory=list)
    to: str = "principal"
    silent: bool = False
    attachments: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    card_id: int | None = None
    chat: str | None = None          # the chat it came from (2026-11-12: the Acme Team panel vs the main one)
