"""Comms backbone ROUTER (doc 15 "Intake also includes internal task assignments", doc 10 who-can-order).

Pure, deterministic classification + authorisation + lane routing for an inbound message. No I/O, no sends,
no db writes - it only decides. The listener hands the decision to the launcher (session_launch.py) and the
Sender; this module never touches the world.

Envelope reused from the real team wire (handover/.../agent-wire/agent_wire.py): seat-to-seat memos headed
`[from-seat] -> role`, department TOPICS, and the "Received"/"Relayed" acks. We adopt the department names and
the idea that routing follows the SENDER, but we do NOT copy any real ids / bot handles into the repo
(CONTRACT hard rule 7): authority is by the account id in config.people.*, never by a display name or a seat
string in the text (doc 15: an assignment is valid only "from their own account"; a name/seat can be spoofed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Wiki department topics, same names the team wire uses (names are not secret). Routing follows the SENDER.
DEPARTMENTS = ("Delivery", "Sales", "Content", "Communication", "Planning", "R&D", "HR", "Finance", "Product",
               "Support")
PERSON_DEPT = {"supervisor": "Delivery", "riley": "Sales", "executive": "Communication", "principal": "Support"}

# Who can ORDER what (doc 10, restated in doc 15): Supervisor = ticket work + gates; Riley = MD/decisions + live push;
# Executive = owner-level (everything); Principal = himself (everything). An item of a category not in a person's set is
# NOT an authorised order from them; an id we do not know is nobody and may order nothing.
CATEGORIES = ("task", "inquiry", "chatter", "decision", "gate")
WHO_CAN_ORDER: dict[str, set[str]] = {
    "principal": {"task", "inquiry", "decision", "gate"},        # himself
    "executive": {"task", "inquiry", "decision", "gate"},       # owner-level
    "riley": {"task", "inquiry", "decision"},                 # MD / decisions (+ live push, a gate she signs)
    "supervisor": {"task", "inquiry", "gate"},                    # ticket work + the client-emailing / prod gate
}

# Word-boundary keyword sets (never substrings: "gate" must not fire inside "investigate", "do" not inside
# "done"/"document"). \b with \w also spans Arabic letters. Precedence is gate > decision > task > inquiry.
_GATE = re.compile(r"\b(go[\s-]?live|push to prod(?:uction)?|deploy to prod(?:uction)?|release to prod(?:uction)?|"
                   r"sign[\s-]?off|signoff|gate|approve the push|live push|ship to prod(?:uction)?)\b", re.I)
_DECISION = re.compile(r"\b(decide|decision|which option|go with|approve the plan|pick the|prioriti[sz]e|"
                       r"budget approval|approve the budget|strategy call|your call|قرار|نقرر|نختار)\b", re.I)
_TASK = re.compile(r"\b(please|can you|could you|need you|i need|pls|kindly|assign|handle|investigate|reproduce|"
                   r"fix|build|create|update|check|prepare|draft|review|resolve|look into|set up|سوّ?ي|اعمل|"
                   r"محتاج|عايز|عاوز|ظبّ?ط|راجع|صلّ?ح|حضّ?ر|لو سمحت|ممكن)\b", re.I)
_INQUIRY = re.compile(r"\b(how|what|why|when|where|which|who|is there|do we|can we|إزاي|ازاي|ليه|امتى|فين|ايه|"
                      r"هل|ينفع)\b", re.I)
_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class InboundMessage:
    """A normalised inbound item for the comms backbone. `sender_id` is the authority (an account id)."""
    text: str
    sender_id: int = 0
    sender_name: str = ""          # label only; NEVER used for authority
    channel: str = ""
    topic: str = ""                # an explicit department name, when the message is about that department
    refs: tuple[str, ...] = ()     # ticket / REQ references found in the text

    @classmethod
    def from_event(cls, ev, cfg: dict | None = None) -> "InboundMessage":
        """Build from a models.Event (duck-typed, so tests need no full Event)."""
        sender = getattr(ev, "sender", None)
        topic = (getattr(ev, "topic", "") or "").strip()
        # Team topic names carry a " Department" suffix ("Delivery Department"); the short name is the department.
        short = topic[:-len(" Department")].strip() if topic.endswith(" Department") else topic
        dept = short if short in DEPARTMENTS else ""
        return cls(text=getattr(ev, "text", "") or "", sender_id=int(getattr(sender, "tg_id", 0) or 0),
                   sender_name=getattr(sender, "name", "") or "", channel=str(getattr(ev, "channel", "") or ""),
                   topic=dept, refs=tuple(getattr(ev, "refs", ()) or ()))


@dataclass
class RouteDecision:
    category: str                  # one of CATEGORIES
    person: str | None             # resolved from sender_id, or None if the id is unknown / 0
    authorised: bool
    action: str                    # "ignore" | "reject" | "human_gate" | "launch"
    lane: str | None = None        # the lane folder slug, only when action == "launch"
    to_gate: bool = False          # a decision/gate routed to a human, never auto-executed
    reason: str = ""
    tags: list[str] = field(default_factory=list)


def classify(text: str) -> str:
    """Deterministic keyword classification. Precedence: gate > decision > task > inquiry > chatter."""
    t = text or ""
    if _GATE.search(t):
        return "gate"
    if _DECISION.search(t):
        return "decision"
    if _TASK.search(t):
        return "task"
    if t.strip().endswith("?") or _INQUIRY.search(t):
        return "inquiry"
    return "chatter"


def safe_slug(value: str) -> str:
    return (_SANITISE.sub("-", str(value or "")).strip("-").lower() or "lane")[:60]


def lane_for(msg: InboundMessage, person: str | None) -> str:
    """Per-ticket lane when a ref is present, else per-department-topic lane (routing follows the sender)."""
    if msg.refs:
        return "ticket-" + safe_slug(msg.refs[0])
    dept = msg.topic or PERSON_DEPT.get(person or "", "")
    return "dept-" + safe_slug(dept) if dept else "general"


class CommsRouter:
    """Resolves authority from config.people.*_tg_user_id and routes an InboundMessage. Pure / deterministic."""

    def __init__(self, cfg: dict | None = None):
        self.people_ids = self._id_map(cfg or {})

    @staticmethod
    def _id_map(cfg: dict) -> dict[int, str]:
        people = cfg.get("people") or {}
        out: dict[int, str] = {}
        for person in ("principal", "supervisor", "riley", "executive"):
            raw = people.get(f"{person}_tg_user_id")
            try:
                uid = int(raw) if raw not in (None, "") else 0
            except (TypeError, ValueError):
                uid = 0
            if uid:                                   # 0 / empty / unset => unknown, never authorised
                out[uid] = person
        return out

    def person_of(self, sender_id: int) -> str | None:
        return self.people_ids.get(int(sender_id)) if sender_id else None

    def route(self, msg: InboundMessage) -> RouteDecision:
        category = classify(msg.text)
        person = self.person_of(msg.sender_id)
        tags = [category]
        if category == "chatter":
            return RouteDecision(category, person, authorised=True, action="ignore",
                                 reason="chatter, not an order", tags=tags)
        authorised = bool(person) and category in WHO_CAN_ORDER.get(person or "", set())
        if not authorised:
            why = "unknown sender id" if not person else f"{person} may not order a {category}"
            return RouteDecision(category, person, authorised=False, action="reject", reason=why, tags=tags)
        if category in ("decision", "gate"):
            # Doc 15: a decision or a gate is routed to the owner/human, never auto-executed. A relayed
            # "Supervisor said push to prod" is never itself a prod approval.
            return RouteDecision(category, person, authorised=True, action="human_gate", to_gate=True,
                                 reason=f"{category} routed to the human gate", tags=tags)
        lane = lane_for(msg, person)
        return RouteDecision(category, person, authorised=True, action="launch", lane=lane,
                             reason=f"{category} from {person} -> lane {lane}", tags=tags)
