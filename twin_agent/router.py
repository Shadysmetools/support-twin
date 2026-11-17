"""Router: dedupe, burst merge, reopen link, company, lane and tier (CONTRACT section 9.5, 05 2.1 to 2.3).

What happens to one inbound event:
1. Already stored (same event_id)? An edit updates the text; nothing new opens. Sweep re-finds are dropped.
2. From Principal? It is stored, marks the request's first reply and cancels its reply timers. Never opens a request.
3. From a bot or ticket mail? Stored or ignored. Never opens a request (red-team #12).
4. Otherwise: join an open burst (same chat, topic, sender, still NEW), or a reply-thread, or a follow-up on an
   open request, or reopen a recently closed one ("still", "لسه", "??"), or open a new request in NEW.
5. A pure ack from the requester ("تمام", "ok thanks", a 👍 reaction) goes to that requester's open request and
   may close it (closing.py). An ack with nowhere to go is stored, but never opens a request.
A burst closes 3 min after the last message (cap 30 min from the first) via the burst_close timer.
The reply timers it then schedules (holding, reminder) are measured from the FIRST message, not the close.
All time comparisons here are on instants (clock.utc / elapsed), never Cairo wall clock.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Callable

from . import closing
from . import timers as tm
from .bus import Bus, Topics
from .clock import Clock, earliest, elapsed, latest, utc
from .config import allowed_chat, chat_flag, topic_entry
from .redact import redact
from .models import (REPLY_TIMERS, TERMINAL, Card, CardKind, Channel, Company, Event, IllegalTransition, Lane, State,
                     Tier,
                     TimerKind, parse_ts, utc_iso)
from .timers import add_minutes

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- pure helpers

_SLASH_REF = re.compile(r"(?<![\w/])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*/(?:[A-Z0-9]+(?:-[A-Z0-9]+)*/)*\d{3,}(?:/\d{2,4})?(?![\w/])")
_SO_PO_REF = re.compile(r"(?<![\w/])(?:SO|PO|S|P)\d{4,}(?![\w/])")
_MODEL_REF = re.compile(r"\b(?:stock\.move|stock\.picking|sale\.order|purchase\.order|account\.move)\s+\d+\b")


def extract_refs(text: str) -> list[str]:
    """Record refs like WAVE/01234, IN/10001/26, SO10001, DLV/10001/26, L1-27/06-BATCH/09766, PO10001."""
    text = text or ""
    found: list[tuple[int, str]] = []
    for rx in (_SLASH_REF, _SO_PO_REF, _MODEL_REF):
        found += [(m.start(), m.group(0)) for m in rx.finditer(text)]
    out: list[str] = []
    for _, ref in sorted(found):
        if ref not in out:
            out.append(ref)
    return out


_KSA = re.compile(r"\b(?:KSA|Saudi|Riyadh|Jeddah|Dammam)\b|السعودية|السعوديه|الرياض|جدة", re.IGNORECASE)


def company_hint(ev: Event, cfg: dict) -> Company:
    """KSA if the text says so; else the topic's company, the chat's company, the event's hint, or UNKNOWN."""
    if _KSA.search(f"{ev.chat or ''} {ev.topic or ''} {ev.text or ''}"):
        return Company.KSA
    for entry in (topic_entry(cfg, ev.chat_id, ev.topic_id), allowed_chat(cfg, ev.chat_id, ev.topic_id)):
        if entry and entry.get("company") in ("EG", "KSA"):
            return Company(entry["company"])
    return Company(ev.company_hint) if ev.company_hint else Company.UNKNOWN


# English, Franco and Arabic ways people say "it is still broken". Counted in Principal's export (counts only).
_REOPEN = re.compile(
    r"(?<!\w)(?:still|same\s+(?:error|issue|problem|message)|not\s+working|didn'?t\s+work|does\s*n'?t\s+work|"
    r"(?<!try\s)again|happened\s+again|lessa|lesa|lsa|bardo|nafs\s+el\s*\w+|zay\s+ma\s+howa|"
    r"[وف]?لس[هةا]|نفس\s+(?:المشكل[ةه]|الخطأ|الغلط|الايرور|الإيرور|الرسال[ةه])|مش\s+شغال|زي\s+ما\s+هو|برده|برضه|برضو|مازال|ما\s+زال)(?!\w)"
    r"|\?\?|؟؟", re.IGNORECASE)


def is_reopen_text(text: str) -> bool:
    """True for "still", "same error", "??", "لسه", "lessa" and friends."""
    return bool(text and _REOPEN.search(text))


# ---------------------------------------------------------------- classification (keyword rules)

_RULES: list[tuple[Tier, re.Pattern]] = [
    (Tier.T4, re.compile(r"\b(approv\w*|price|pricing|discount|policy|should we|allowed to|budget)\b|موافق|سعر|خصم",
                         re.I)),
    (Tier.T2b, re.compile(r"\b(access|permission|rights?|password|log\s?in|login|new user|create user|menu|"
                          r"group|config\w*|settings?)\b|صلاحي|باسورد|يوزر", re.I)),
    (Tier.T3, re.compile(r"\b(bug|feature|develop\w*|customi[sz]\w*|module|report (?:is )?wrong|new field|"
                         r"traceback|server error|odoo error|ticket)\b|تطوير", re.I)),
    (Tier.T2a, re.compile(r"\b(validat\w*|stuck|cancel\w*|can'?t|cannot|unable|not able|blocked|wrong qty|"
                          r"quantity|reserve\w*|unreserve|return|scrap|mismatch|negative)\b|مش راضي|مش عارف|مش قادر|"
                          r"مفيش|ما ينفعش|\bmsh\b|\bmesh\b", re.I)),
    (Tier.T1, re.compile(r"\b(how|where|what|which|explain|ezay|ezzay|fen|feen|leh)\b|ازاي|إزاي|فين|ليه|ايه", re.I)),
]
LANE_FOR_TIER = {Tier.T1: Lane.L3_t1_answer, Tier.T2a: Lane.L4_t2a_fix_proposal, Tier.T2b: Lane.L5_t2b_config,
                 Tier.T3: Lane.L6_ticket_email, Tier.T4: Lane.L8_approver_question,
                 Tier.UNKNOWN: Lane.L11_internal_diagnosis}


def classify_text(text: str, *, has_refs: bool, channel: str) -> tuple[Lane, Tier]:
    """Keyword guess (agrees with hand labels ~66% of the time, so a classifier can override it)."""
    tier = Tier.UNKNOWN
    for t, rx in _RULES:
        if rx.search(text or ""):
            tier = t
            break
    if tier == Tier.UNKNOWN and has_refs:
        tier = Tier.T2a                  # a bare record ref (often with a photo) is usually a stuck record
    lane = Lane.L12_email_reply if channel == Channel.email else LANE_FOR_TIER[tier]
    return lane, tier


# ---------------------------------------------------------------- the router

# agent_wire posts "[seat] @bot Received" / "Relayed" by itself (11 Nov 2026, #3295): not a message for Principal.
_WIRE_ACK = re.compile(r"^\s*\[[\w-]+\]\s*(?:@\w+\s*)*(?:Received|Relayed)\.?\s*$", re.I)

class Router:
    def __init__(self, db, cfg: dict, policy, bus: Bus, clock: Clock,
                 classifier: Callable[[dict, list[dict]], Any] | None = None):
        """classifier (optional): f(request, thread) -> (Lane, Tier) | {"lane", "tier"} | None. Errors fall back."""
        self.db, self.cfg, self.policy, self.bus, self.clock = db, cfg, policy, bus, clock
        self.classifier = classifier
        t = cfg.get("timers", {})
        self.quiet_min = (t.get("burst") or {}).get("quiet_min", 3)
        self.cap_min = (t.get("burst") or {}).get("cap_min", 30)
        self.reopen_days = (t.get("reopen_window") or {}).get("days", 14)
        self.reopen_kpi_h = (t.get("reopen_window") or {}).get("kpi_hours", 48)
        # Principal 2026-11-09: "only run what has not been replied to recently, i.e. only for the day". A message that
        # lands in a request opened BEFORE today (Cairo) starts a NEW request rooted at itself: the old one is never
        # reopened, its card never shows old messages, and no reply goes under an old message.
        self.today_only = bool((cfg.get("router") or {}).get("today_only"))

    def _opened_today(self, req: dict) -> bool:
        from .clock import CAIRO
        opened = parse_ts(req.get("created_at"))
        return bool(opened) and opened.astimezone(CAIRO).date() == self.clock.now().astimezone(CAIRO).date()

    # ---- entry point

    async def on_event(self, ev: Event) -> str | None:
        rid = await self._on_event(ev)
        if rid and self._thread_key(ev):                  # 2026-11-11: remember which request a mail thread is
            self.db.set_cursor(self._thread_key(ev), rid)
            hdr = {str(k).lower(): v for k, v in (getattr(ev, "headers", None) or {}).items()}
            old = json.loads(self.db.get_cursor(f"gmreq:{rid}") or "{}")
            # what a reply draft needs: the thread, its subject and the last message id (In-Reply-To)
            self.db.set_cursor(f"gmreq:{rid}", json.dumps({
                "mailbox": str(ev.chat_id), "thread_id": ev.thread_id,
                "subject": old.get("subject") or getattr(ev, "subject", "") or hdr.get("subject", ""),
                "last_message_id": hdr.get("message-id") or old.get("last_message_id", "")}))
        return rid

    # ---- mail threads (2026-11-11: Signatory's second mail in the same thread opened a second request)

    @staticmethod
    def _thread_key(ev: Event) -> str | None:
        tid = getattr(ev, "thread_id", "") or ""
        return f"gmthread:{ev.chat_id}:{tid}" if ev.channel == Channel.email and tid else None

    def _thread_target(self, ev: Event) -> str | None:
        key = self._thread_key(ev)
        rid = self.db.get_cursor(key) if key else None
        return rid if rid and self.db.get_request(rid) else None

    async def _on_event(self, ev: Event) -> str | None:
        if not ev.refs:
            ev.refs = extract_refs(ev.text)
        old = self.db.get_message(ev.event_id)
        if old:                                            # dedupe: sweep re-finds, duplicates, edits
            if ev.edited:
                self.db.add_message(ev, old["request_id"])
                if old["request_id"]:
                    self.bus.publish(Topics.REQUEST_UPDATED, {"request_id": old["request_id"], "event_id": ev.event_id})
            return old["request_id"]
        if ev.reaction:
            return self._on_reaction(ev)
        if ev.sender.is_principal:
            return self._on_principal(ev)
        if ev.sender.is_bot or ev.channel == Channel.ticket_mail:
            self.db.add_message(ev, None)
            if (ev.sender.is_bot and chat_flag(self.cfg, ev.chat_id, "tagged_only", False) and not ev.edited
                    and not _WIRE_ACK.match(ev.text or "")      # agent_wire's own "Received" / "Relayed" acks
                    and not self._own_bot(ev)):                  # his own bot's post is not FOR him (#3292)
                # Acme Team (Principal 2026-11-11): a bot that tags his bot (e.g. Sysadmin's agent answering) reaches
                # him as an info card. Never a request, a reply or a ticket.
                where = ev.chat + (f" / {ev.topic or ev.topic_id}" if (ev.topic or ev.topic_id) else "")
                self.bus.publish(Topics.CARD_OUT, Card(kind=CardKind.info, text=redact(
                    f"Bot message for you | {ev.sender.name or 'bot'} | {where} | {ev.ts:%H:%M} Cairo\n\n"
                    f"{ev.text}\n\nNo reply goes out from here: answer in the group if needed.")[:3400],
                    chat=ev.chat))
            return None
        ev.company_hint = company_hint(ev, self.cfg)
        rid = self._find_target(ev)
        if rid:
            return await self._attach(rid, ev)
        if closing.is_ack_text(ev.text) and not ev.media:
            self.db.add_message(ev, None)                  # "تمام" / "thanks" alone is never a new request
            self.db.log_event("ack_no_target", None, actor="router", event_id=ev.event_id)
            return None
        return self._open(ev)

    # ---- reactions

    def _own_bot(self, ev: Event) -> bool:
        """A message sent by one of chats[].tag_user_ids (Principal's own bot)."""
        chat = allowed_chat(self.cfg, ev.chat_id, ev.topic_id) or {}
        return int(ev.sender.tg_id or 0) in {int(u) for u in chat.get("tag_user_ids") or []}

    def _on_reaction(self, ev: Event) -> str | None:
        """A reaction on a message of a request. Only the requester's 👍 (close_rules.ack_reactions) counts.
        Never stored as a message, never opens a request; Principal's and bots' reactions are ignored."""
        if ev.sender.is_principal or ev.sender.is_bot or ev.reply_to is None:
            return None
        msg = self.db.get_message(f"tg:{ev.chat_id}:{ev.reply_to}")
        rid = msg["request_id"] if msg else None
        if not rid:
            return None
        req = self.db.get_request(rid)
        me = str(ev.sender.tg_id or ev.sender.email or "")
        if not req or req["requester_id"] != me or not closing.is_ack_reaction(ev.reaction, self.cfg):
            return None
        self.db.log_event("reaction", rid, actor="router", event_id=ev.event_id)
        closing.on_requester_ack(self.db, self.cfg, self.bus, req, event_id=None, how="reaction")
        return rid

    # ---- Principal

    def _on_principal(self, ev: Event) -> str | None:
        """Principal wrote: remember it (human-active lock) and mark the request answered. Never opens one.
        Our own sends post from Principal's account too: their echo is stored, but is not "Principal is typing"."""
        own = self.db.send_by_remote(ev.chat_id, ev.msg_id) if ev.msg_id is not None else None
        if own:
            self.db.add_message(ev, own["request_id"])
            return own["request_id"]
        self.db.set_cursor(f"principal_active:{ev.chat_id}", utc_iso(ev.ts))
        # Not only "nobody answered yet": a request where only the agent's holding reply went out, or where the
        # requester asked again after Principal's last answer, is still waiting for Principal.
        if ev.channel == Channel.email:                      # mail: only the same thread is his answer
            rid = self._thread_target(ev)
        else:
            rid = self._reply_target(ev) or self._latest_open(ev, awaiting_principal=True)
        self.db.add_message(ev, rid)
        if not rid:
            return None
        req = self.db.get_request(rid)
        if not req["first_reply_at"]:
            self.db.update_request(rid, first_reply_at=ev.ts, first_reply_by="principal")
            self.db.add_kpi("first_reply_by_principal", 1, request_id=rid)
        tm.cancel(self.db, rid, REPLY_TIMERS)
        if req["state"] in (State.TRIAGED, State.AWAITING_FIRST_REPLY):
            self._move(rid, State.ACKED, "principal", "Principal replied")
        self.db.log_event("principal_replied", rid, actor="principal", event_id=ev.event_id, at=utc_iso(ev.ts))
        if ev.channel == Channel.email and State(self.db.get_request(rid)["state"]) not in TERMINAL:
            req = self.db.get_request(rid)
            if closing.needs_development(req, closing.latest_packet(self.db, rid)):
                # a development request is not finished when he answers: it stays open until he closes it
                self.bus.publish(Topics.CARD_OUT, Card(kind=CardKind.info, request_id=rid, text=(
                    f"{rid}: you replied by email. This is a development request, so it and its ticket stay open "
                    f"until it is delivered. Close it with: close {rid}")))
            else:
                # Principal 2026-11-11: his reply sent from Gmail closes the mail request and its ticket, like Approve
                # on Telegram. A new mail from them in this thread within the reopen window reopens both.
                self._move(rid, State.CLOSED_BY_PRINCIPAL, "principal", "Principal replied by email")
                tm.cancel(self.db, rid, REPLY_TIMERS)
                if str(req.get("ticket_id") or "").isdigit():
                    principal = int((self.cfg.get("people") or {}).get("principal_tg_user_id") or 0)
                    self.bus.publish(Topics.COMMAND_IN, {"user_id": principal, "command": "close", "args": rid})
                self.bus.publish(Topics.CARD_OUT, Card(kind=CardKind.done, request_id=rid,
                                                       text=f"{rid} closed: you replied by email."))
                return rid
        tm.ensure_timer(self.db, self.policy, rid, self.clock.now())
        self.bus.publish(Topics.REQUEST_UPDATED, {"request_id": rid, "event_id": ev.event_id})
        return rid

    # ---- finding where a message belongs

    def _same_place(self, req: dict, ev: Event) -> bool:
        return req["chat_id"] == str(ev.chat_id) and (req["topic_id"] == ev.topic_id)

    def _reply_target(self, ev: Event) -> str | None:
        if ev.reply_to is None:
            return None
        prefix = ev.event_id.rsplit(":", 1)[0] if ev.event_id.count(":") >= 2 else ""
        msg = self.db.get_message(f"{prefix}:{ev.reply_to}") if prefix else None
        if msg and msg["request_id"]:
            return msg["request_id"]
        # 15 Nov 2026 (REQ-0069 / ticket 2684): a client answered a reply the twin itself posted ("say REQ-x", an
        # approved draft). Our own sends are not always in `messages` (their echo may never come back), so the
        # reply was filed under the same person's newest open request instead. Trace it through `sends`.
        own = self.db.send_by_remote(ev.chat_id, ev.reply_to)
        return own["request_id"] if own and own["request_id"] and self.db.get_request(own["request_id"]) else None

    def _latest_open(self, ev: Event, *, owed_only: bool = False, requester: str | None = None,
                     awaiting_principal: bool = False) -> str | None:
        best = None
        for req in self.db.open_requests():
            if not self._same_place(req, ev):
                continue
            if owed_only and req["first_reply_at"]:
                continue
            if awaiting_principal and closing.human_answered(self.db, req):
                continue
            if requester is not None and req["requester_id"] != requester:
                continue
            if best is None or req["updated_at"] > best["updated_at"]:
                best = req
        return best["id"] if best else None

    def _find_target(self, ev: Event) -> str | None:
        rid = self._find_target_any(ev)
        if rid and self.today_only:
            req = self.db.get_request(rid)
            # Principal 2026-11-11: a request lives on for timers.reopen_window.days (7) after its last activity or its
            # close: a reply in that window goes to the SAME request (and reopens it and its ticket). Older threads
            # (like REQ-0021 from April) still start a fresh request: never reopened.
            if req and not self._opened_today(req) and not self._recent(req):
                self.db.log_event("old_thread_new_request", rid, actor="router", event_id=ev.event_id,
                                  why=f"older than {self.reopen_days} days")
                return None                                  # -> _open(ev): a fresh request for this message
        return rid

    def _recent(self, req: dict) -> bool:
        last = parse_ts(req.get("closed_at") or req.get("updated_at") or req.get("created_at"))
        return bool(last) and elapsed(last, self.clock.now()) <= timedelta(days=self.reopen_days)

    def _find_target_any(self, ev: Event) -> str | None:
        me = str(ev.sender.tg_id or ev.sender.email or "")
        threaded = self._thread_target(ev)                  # a mail in a thread we already track
        if threaded:
            req = self.db.get_request(threaded)
            if req["state"] not in TERMINAL or self._reopenable(req, ev):
                return threaded
        if self._thread_key(ev):                            # mail: another Gmail thread is another request
            return threaded if threaded and self.db.get_request(threaded)["state"] not in TERMINAL else None
        target = self._reply_target(ev)
        if target:
            req = self.db.get_request(target)
            if req["state"] not in TERMINAL:
                return target
            if self._reopenable(req, ev):
                return target
        open_reqs = [r for r in self.db.open_requests() if self._same_place(r, ev)]
        for r in open_reqs:                                  # an open burst from the same person
            if r["state"] == State.NEW and r["requester_id"] == me:
                return r["id"]
        if ev.refs:                                          # same record ref as an open request here
            for r in open_reqs:
                if set(ev.refs) & self._refs_of(r["id"]):
                    return r["id"]
        mine = self._latest_open(ev, requester=me)
        if mine:
            req = self.db.get_request(mine)
            recent = utc(parse_ts(req["updated_at"])) >= utc(add_minutes(self.clock.now(), -self.cap_min))
            if req["state"] == State.NEEDS_INFO or recent or is_reopen_text(ev.text) or closing.is_ack_text(ev.text):
                return mine
        if is_reopen_text(ev.text):                          # "still / لسه / ??" on a recently closed request
            for req in reversed(self.db.requests_in(TERMINAL)):
                if self._same_place(req, ev) and (req["requester_id"] == me or set(ev.refs) & self._refs_of(req["id"])) \
                        and self._reopenable(req, ev):
                    return req["id"]
        return None

    def _refs_of(self, request_id: str) -> set[str]:
        refs: set[str] = set()
        for m in self.db.thread(request_id):
            refs.update(extract_refs(m["text"] or ""))
        return refs

    def _reopenable(self, req: dict, ev: Event) -> bool:
        """A reply in the thread of a closed request. Since 2026-11-11 Principal's Approve closes the request, so any
        follow-up by the requester (not a bare thanks / تمام) reopens it within the reopen window."""
        if closing.is_ack_text(ev.text) and not ev.media:
            return False
        if not (is_reopen_text(ev.text) or set(ev.refs) & self._refs_of(req["id"])
                or str(req.get("state")) == str(State.CLOSED_BY_PRINCIPAL)):
            return False
        return self.db.within_reopen_window(req)

    # ---- acting

    def _open(self, ev: Event) -> str:
        rid = self.db.create_request(ev, company=ev.company_hint)
        self.db.add_message(ev, rid)
        now = self.clock.now()
        tm.schedule(self.db, rid, TimerKind.burst_close, self.policy.due_at(TimerKind.burst_close, now),
                    {"first_at": utc_iso(now)})
        self.bus.publish(Topics.REQUEST_NEW, {"request_id": rid})
        return rid

    async def _attach(self, rid: str, ev: Event) -> str:
        self.db.add_message(ev, rid)
        req = self.db.get_request(rid)
        state = State(req["state"])
        now = self.clock.now()
        me = str(ev.sender.tg_id or ev.sender.email or "")
        if state == State.NEW:
            self._extend_burst(rid, now)
        elif state not in TERMINAL and me == req["requester_id"] and closing.is_ack_text(ev.text) \
                and closing.on_requester_ack(self.db, self.cfg, self.bus, req, event_id=ev.event_id, how="text"):
            return rid
        elif state in TERMINAL:
            self._reopen(rid, req)
        elif state == State.NEEDS_INFO:
            tm.cancel(self.db, rid, [TimerKind.evidence_chase_1, TimerKind.evidence_chase_2])
            self._move(rid, State.TRIAGED, "router", "requester answered")
            tm.ensure_timer(self.db, self.policy, rid, now)
            self.bus.publish(Topics.REQUEST_TRIAGED, {"request_id": rid})
        else:
            self.db.update_request(rid)
            self.bus.publish(Topics.REQUEST_UPDATED, {"request_id": rid, "event_id": ev.event_id})
        return rid

    def _extend_burst(self, rid: str, now) -> None:
        first = now
        for t in self.db.timers_of(rid, TimerKind.burst_close.value):
            first = earliest(first, parse_ts(json.loads(t["payload_json"] or "{}").get("first_at")) or now)
        tm.cancel(self.db, rid, [TimerKind.burst_close])
        due = earliest(add_minutes(now, self.quiet_min), add_minutes(first, self.cap_min))
        tm.schedule(self.db, rid, TimerKind.burst_close, latest(due, now), {"first_at": utc_iso(first)})
        self.db.update_request(rid)

    def _reopen(self, rid: str, req: dict) -> None:
        closed = parse_ts(req["closed_at"])
        self._move(rid, State.REOPENED, "router", "requester says it is not fixed")
        self._move(rid, State.TRIAGED, "router", "reopened")
        self.db.update_request(rid, first_reply_at=None, first_reply_by=None, closed_at=None)
        if closed and elapsed(closed, self.clock.now()) <= timedelta(hours=self.reopen_kpi_h):
            self.db.add_kpi("reopen_48h", 1, lane=req["lane"], request_id=rid)
        self.db.add_kpi("reopen", 1, lane=req["lane"], request_id=rid)
        now = self.clock.now()
        tm.schedule(self.db, rid, TimerKind.reminder, self.policy.due_at(TimerKind.reminder, now))   # fresh nudge
        tm.ensure_timer(self.db, self.policy, rid, now)
        self.bus.publish(Topics.REQUEST_REOPENED, {"request_id": rid})   # the helpdesk reopens its ticket
        self.bus.publish(Topics.REQUEST_TRIAGED, {"request_id": rid})

    def _move(self, rid: str, dst: State, actor: str, reason: str) -> bool:
        try:
            self.db.transition(rid, dst, actor=actor, reason=reason)
            return True
        except IllegalTransition as e:
            log.info("skipped transition: %s", e)
            return False

    async def close_burst(self, request_id: str) -> None:
        """Burst is quiet: guess lane/tier/company, NEW -> TRIAGED, schedule timers, publish request.triaged."""
        req = self.db.get_request(request_id)
        if not req or req["state"] != State.NEW:
            return
        lane, tier = self.classify(request_id)
        fields: dict[str, Any] = {"lane": lane, "tier": tier, "burst_closed_at": self.clock.now()}
        if req["company"] in (None, "", Company.UNKNOWN):
            text = " ".join(m["text"] or "" for m in self.db.thread(request_id))
            if _KSA.search(text):
                fields["company"] = Company.KSA
        self.db.update_request(request_id, **fields)
        tm.cancel(self.db, request_id, [TimerKind.burst_close])
        self._move(request_id, State.TRIAGED, "router", "burst closed")
        first_at = parse_ts(req["created_at"]) or self.clock.now()     # the requester's first message
        tm.schedule_for_state(self.db, self.policy, request_id, State.TRIAGED, self.clock.now(),
                              reply_start=earliest(first_at, self.clock.now()))
        self.bus.publish(Topics.REQUEST_TRIAGED, {"request_id": request_id})

    def classify(self, request_id: str) -> tuple[Lane, Tier]:
        req = self.db.get_request(request_id) or {}
        thread = self.db.thread(request_id)
        if self.classifier is not None:
            try:
                got = self.classifier(req, thread)
                if isinstance(got, dict):
                    got = (got.get("lane"), got.get("tier"))
                if got and got[0] and got[1]:
                    return Lane(got[0]), Tier(got[1])
            except Exception as e:                       # a bad classifier never blocks routing
                log.warning("classifier failed, using keyword rules: %s", type(e).__name__)
        text = "\n".join(m["text"] or "" for m in thread if not m["is_principal"])
        return classify_text(text, has_refs=bool(extract_refs(text)), channel=req.get("channel", ""))
