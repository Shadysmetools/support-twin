"""Small, self-contained dataclasses for the hub pipeline (kept separate from the big root ``models.py`` so the
hub stays loosely coupled). No behaviour here beyond trivial helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


# Arabic Unicode blocks: Arabic (0600-06FF), Arabic Supplement (0750-077F), Presentation Forms.
def detect_lang(text: str | None) -> str:
    """Reply language follows the sender: any Arabic-script character -> 'ar', otherwise 'en' (plan §13)."""
    for ch in str(text or ""):
        o = ord(ch)
        if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
            return "ar"
    return "en"


@dataclass
class Inbound:
    """One inbound WhatsApp message, parsed from a Meta Cloud-API webhook payload (``webhook.parse``)."""

    message_id: str                      # Meta wamid — the idempotency key
    wa_id: str                           # the sender's WhatsApp id (phone digits); maps to a res.partner
    name: str                            # the sender's WhatsApp profile name
    kind: str                            # "text" | "voice" | "media" (image/video/document)
    text: str | None = None              # text body / media caption, or the STT transcript once transcribed
    audio_id: str | None = None          # Meta media id for a voice note (kept; audio is retained, A1)
    lang: str = "en"                     # detected/declared reply language
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    transcript_placeholder: bool = False  # True when text came from STT on a synthetic/placeholder clip
    media_id: str | None = None          # Meta media id of an image/video/document (fetch is a tracked follow-up)
    media_type: str | None = None        # "image" | "video" | "document"
    mime: str | None = None
    button_id: str | None = None         # the id of a tapped interactive reply button (intake v2 request type)
    channel: str = "whatsapp"            # "email" = fed from the Odoo chatter by the hub poll (never opens a ticket / acks)

    def resolve_lang(self) -> str:
        if self.kind in ("text", "media") and self.text:
            self.lang = detect_lang(self.text)
        return self.lang

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["ts"] = self.ts.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Inbound":
        d = dict(d)
        d["ts"] = datetime.fromisoformat(d["ts"])
        return cls(**d)


@dataclass
class Analysis:
    """The ANALYSE output: issue-vs-feature plus the Odoo convention fields and a root-cause/scope read."""

    nature: str                          # "issue" | "feature"
    ticket_type: str                     # Bug | New Request | Customization | Configuration | Inquiries
    track: str                           # inquiry | functional | development  (plan §14.1)
    module: str                          # best-guess Odoo module, or "unknown"
    root_cause: str = ""                 # likely root cause / scope note (plain English)
    scope: str = ""
    open_questions: list[str] = field(default_factory=list)   # what blocks work (drives Template 2b, A9)
    confidence: float = 0.5
    summary: str = ""                    # one-line restated understanding (for Template 2)
    desired_outcome: str = ""
    urgency: str = "not stated"          # "High" when the client signals urgency, else "not stated" (never invented)


@dataclass
class Owner:
    """An assignment target (a support engineer)."""

    uid: int                             # Odoo res.users id
    name: str
    tg_id: int = 0                       # Telegram user id for the @-mention
    role: str = ""


@dataclass
class TicketState:
    """Per-TICKET pipeline state (not per phone number): a sender can have many tickets over time. Persisted as a
    JSON doc in the ``tickets`` namespace of the store (``to_dict`` / ``from_dict``).

    ``stage``: new -> awaiting_owner (CONFIRM draft pending) -> awaiting_customer -> awaiting_owner_advance (the
    customer replied; the OWNER must mark it confirmed) -> in_progress (work/fix; owner taps "fix ready") ->
    awaiting_owner_resolve (RESOLVE draft pending) -> resolved. A customer reply after ``resolved`` within
    ``reopen_days`` reopens it (-> awaiting_owner_advance); later it opens a NEW ticket."""

    wa_id: str
    inbound: Inbound
    analysis: Analysis
    partner_id: int
    tid: int = 0
    owner: Owner | None = None
    topic_id: int = 0                    # the Telegram topic (Telegram team bus only; 0 with Teamhub)
    thread_bus: str = ""                 # which bus thread_ref belongs to ("" = a pre-bus ticket: Telegram)
    thread_ref: str = ""                 # the team-bus thread ref (Telegram: topic id; Teamhub: "ticket-<tid>")
    lang: str = "en"
    stage: str = "new"
    client: str = "UNKNOWN"
    proof: dict = field(default_factory=dict)
    history: list[str] = field(default_factory=list)   # stage names walked, for assertions
    source_message_id: str = ""          # the inbound that opened this ticket (a retry resumes from it)
    acked: bool = False                  # the one automatic message: at most one ack per ticket
    pending_nonce: str = ""              # the single live owner approval for this ticket ("" = none)
    pending_kind: str = ""               # confirm | resolve | advance | fix_ready
    resolve_mode: str = ""               # resolution (Template 3/3b) | guidance
    last_inbound_at: str = ""            # ISO ts of the customer's latest message (drives the 24h window)
    resolved_at: str = ""
    reopen_count: int = 0
    reassigned: bool = False
    human_edits: bool = False
    replies: list[dict] = field(default_factory=list)  # customer messages attached to this ticket
    rationale: str = ""                  # the "assigned to X because..." text (kept so a retry can rebuild the brief)
    proof_judge: dict = field(default_factory=dict)    # {aggregate, flag} for the proof video
    declined: bool = False               # the owner declined the last draft: no auto re-ask (customer msg / reask do)
    keep_closed: bool = False            # the owner chose "keep closed": the NEXT message opens a new ticket
    needs_owner_text: bool = False       # inquiry/guidance: waiting for an owner-authored reply (on_owner_text)
    send_unknown: str = ""               # kind whose WhatsApp send outcome is unknown: NEVER auto re-sent
    post_resolve_ids: list[str] = field(default_factory=list)   # messages that raised the "reopen?" question
    side_nonces: list[str] = field(default_factory=list)   # live status (side) approvals: expired on resolve/close/reopen
    replies_at_attempt: int = 0          # len(replies) when the last client send started
    previous_ticket_id: int = 0          # the closed ticket this one FOLLOWS UP (0 = none)
    last_email_at: str = ""              # ISO ts of the client's latest EMAIL reply (never opens the WhatsApp 24h window)
    email_missing: bool = False          # the client has no email in Odoo: the owner may choose WhatsApp PER follow-up
    intake_subject: str = ""             # intake v2: the short subject the ticket was opened with ("" = the old flow)
    intake_company_id: int = 0           # intake v2: the Odoo company the client's answer matched (0 = none/pending)
    intake_type: str = ""                # intake v2: bug | feature | question
    intake_type_auto: bool = False       # the type was obvious, so the buttons were skipped (the confirmation says which)
    attempting_kind: str = ""            # a client send of this kind was started (persisted before the send)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("inbound", "analysis", "owner")}
        d["inbound"] = self.inbound.to_dict()
        d["analysis"] = dict(self.analysis.__dict__)
        d["owner"] = dict(self.owner.__dict__) if self.owner else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TicketState":
        d = dict(d)
        d["inbound"] = Inbound.from_dict(d["inbound"])
        d["analysis"] = Analysis(**d["analysis"])
        d["owner"] = Owner(**d["owner"]) if d.get("owner") else None
        return cls(**d)
