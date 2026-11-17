"""The listener's wiring: who reacts to which bus topic (CONTRACT section 10). Owner: CORE.

The listener (listener.py) builds one Flow, which:
- routes inbound events (router), starts workers on request.triaged (spawner), runs checks on packet.ready,
- turns timer.fired into cards, holding replies or verified answers (flow_timers.py),
- turns button presses into send requests for the sender (the only code that sends),
- handles the kill switch: /stop or the STOP file pauses every open request; /resume brings them back.
Other components (sender, bot controller, checks) are optional here: if one is missing, Flow logs a health
event and keeps the rest running.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from . import closing, paths
from . import timers as tm
from .bus import Bus, Topics
from .clock import elapsed
from .models import (REPLY_TIMERS, TERMINAL, Button, Card, CardKind, Channel, IllegalTransition, OutboundMessage,
                     OutKind, SendTrigger, State, TimerKind, parse_ts)
from .redact import redact
from .router import Router
from .spawner import SpawnBudget, Spawner

log = logging.getLogger(__name__)
TELEGRAM = (Channel.telegram_group, Channel.telegram_dm)
KIND_FOR_TIER = {"T1": OutKind.answer, "T2a": OutKind.answer, "T2b": OutKind.answer, "T3": OutKind.answer,
                 "T4": OutKind.approver_question}


def draft_hash(text: str) -> str:
    from .sender_approval import text_hash
    return text_hash(text)


CAPTION_MAX = 1024          # Telegram photo caption limit: the whole card fits under the screenshot


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _first_sentence(s) -> str:
    import re
    s = " ".join(str(s or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    return m.group(1) if m else s


def approval_card_text(req: dict, thread: list, packet: dict, draft: str, status: str = "", *,
                       full: bool = False) -> tuple[str, str | None]:
    """Principal's approval card (his format, 2026-11-09): ONE Telegram message = the requester's screenshot with this
    caption: header, Message, ONE-sentence Analysis, the exact reply Approve posts, Check first, then a short
    status line. Returns (text, screenshot path or None). The text fits a 1024-char caption when there is a photo.
    full=True (cards to the Claude panel, Principal 2026-11-12 "not shortened"): no caption, nothing clipped."""
    from .clock import CAIRO
    staff = [m for m in thread if not m.get("is_principal")]
    each = 10**6 if full else 200
    said = " / ".join(_clip(m.get("text"), each) for m in staff if (m.get("text") or "").strip()) or "(photo)"
    photo = None
    for m in staff:
        for name in __import__("json").loads(m.get("media_json") or "[]"):
            p = paths.data_dir() / "media" / name
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                photo = photo or str(p)
    first = parse_ts((staff[0].get("ts") if staff else None) or req.get("created_at"))
    when = f"{first.astimezone(CAIRO):%H:%M} Cairo" if first else ""
    where = " / ".join(x for x in (req.get("chat") or "", req.get("topic") or "") if x)
    head = " | ".join(x for x in (req.get("id") or "", req.get("requester_name") or "", where, when) if x)
    analysis = packet.get("analysis") or _first_sentence(packet.get("understanding"))
    qs = packet.get("questions_for_principal") or []

    def build(msg_n: int, an_n: int, q_n: int) -> str:
        lines = [head, "", f"Message: {_clip(said, msg_n)}", "", f"Analysis: {_clip(analysis, an_n)}", "",
                 "REPLY APPROVE WILL POST:" if draft else "NO DRAFT REPLY (Take over or Why?)"]
        if draft:
            lines.append(draft)
        if qs:
            lines += ["", f"Check first: {_clip(qs[0], q_n)}"]
        if status:
            lines += ["", status]
        return "\n".join(lines)
    if full:
        return build(10**6, 10**6, 10**6), photo
    text = build(300, 400, 220)
    if photo:
        for msg_n, an_n, q_n in ((200, 330, 160), (120, 260, 120), (80, 200, 90)):
            if len(text) <= CAPTION_MAX:
                break
            text = build(msg_n, an_n, q_n)
        if len(text) > CAPTION_MAX:            # very long draft: the photo goes first, the card as its own message
            text = build(300, 400, 220)
    return text, photo


class Flow:
    def _full_cards(self) -> bool:
        """Cards go to the Claude panel (bot.transport: claude): no Telegram length limits, nothing shortened."""
        return str((self.cfg.get("bot") or {}).get("transport") or "").lower() == "claude"

    def _card_cap(self) -> int:
        return 200_000 if self._full_cards() else 3400

    def __init__(self, db, cfg: dict, bus: Bus, clock, adapters, *, policy=None, sender=None, bot_ctl=None):
        self.db, self.cfg, self.bus, self.clock, self.adapters = db, cfg, bus, clock, adapters
        self.policy = policy or tm.TimerPolicy(cfg)
        self.router = Router(db, cfg, self.policy, bus, clock)
        self.budget = SpawnBudget(db, cfg, clock)
        self.spawner = Spawner(db, cfg, adapters.claude, self.budget, bus, clock)
        self.sender, self.bot_ctl = sender, bot_ctl
        self.tasks: set[asyncio.Task] = set()
        self.pending_edit: dict[str, str] = {}           # request_id -> Principal's ForceReply text (until Send)
        self.pending_close: dict[str, tuple] = {}        # send_key -> (request_id, user): Approve + close
        self.mute_stalled_until: datetime | None = None  # boot catch-up mutes STALLED alarms
        self.catchup_note: str = ""
        self.health: dict[str, bool] = {}                # component -> last ok, so health cards are not repeated
        self.voice = None                                # voice.desk.VoiceDesk when voice is live + consented
        db.on_state_change = lambda rid, s, d: bus.publish(Topics.STATE_CHANGED,
                                                           {"request_id": rid, "src": s, "dst": d})

    # ------------------------------------------------------------ wiring

    def wire(self) -> None:
        from .flow_timers import on_timer
        sub = self.bus.subscribe
        sub(Topics.EVENT_IN, self.on_event_in)
        sub(Topics.REQUEST_TRIAGED, self.on_triaged)
        sub(Topics.PACKET_READY, self.on_packet_ready)
        sub(Topics.HEALTH, self.on_health)
        sub(Topics.TIMER_FIRED, lambda item: on_timer(self, item))
        sub(Topics.CARD_OUT, self.on_card)
        if self.bot_ctl is not None:                     # one subscriber only, or every card goes out twice
            self.bot_ctl.card_out_wired = True
        sub(Topics.CALLBACK_IN, self.on_callback)
        sub(Topics.REPLY_IN, self.on_reply)
        sub(Topics.COMMAND_IN, self.on_command)
        sub(Topics.SEND_REQUEST, self.on_send_request)
        sub(Topics.SEND_DONE, self.on_send_done)
        sub(Topics.KILL_CHANGED, self.on_kill)

    def spawn(self, coro) -> asyncio.Task:
        """Run in the background; keep a reference so the task is not garbage collected."""
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def card(self, kind: CardKind, text: str, request_id: str | None = None, *, to: str = "principal",
             buttons: list[list[Button]] | None = None) -> None:
        self.bus.publish(Topics.CARD_OUT, Card(kind=kind, text=redact(text)[:3400], request_id=request_id, to=to,
                                               buttons=buttons or []))

    def move(self, rid: str, dst: State, actor: str, reason: str = "") -> bool:
        try:
            self.db.transition(rid, dst, actor=actor, reason=reason)
        except IllegalTransition as e:
            log.info("skipped: %s", e)
            return False
        tm.schedule_for_state(self.db, self.policy, rid, dst, self.clock.now())
        return True

    # ------------------------------------------------------------ inbound

    async def on_event_in(self, ev) -> None:
        rid = await self.router.on_event(ev)
        if ev.channel in TELEGRAM and str(ev.msg_id or "").isdigit():
            key = f"tg:{ev.chat_id}:{ev.topic_id or 0}"
            if int(ev.msg_id) > int(self.db.get_cursor(key) or 0):
                self.db.set_cursor(key, str(ev.msg_id))
        if rid and ev.found_by_sweep:
            self.db.update_request(rid, found_by_sweep=1)

    def workers_allowed(self) -> tuple[bool, str]:
        """05 B1.7: in live mode, staff messages reach Claude only with sign-off; budget breaker = holding only."""
        if self.cfg.get("run_mode") == "live" and not (self.cfg.get("consent") or {}).get("b17_staff_data_to_claude"):
            return False, "B1.7 sign-off missing: staff messages are not sent to Claude"
        if self.adapters.claude is None:
            return False, "no Claude runner (live adapter failed to start)"
        if self.db.get_cursor("holding_only") == self.clock.now().date().isoformat():
            return False, "holding-only mode (daily Claude budget reached)"
        return True, ""

    async def on_triaged(self, payload: dict) -> None:
        rid = payload["request_id"]
        req = self.db.get_request(rid)
        if not req:
            return
        if self.db.kill_on() and req["state"] != State.PAUSED:
            self.pause(rid, "kill switch")                    # opened while stopped: quiet, re-carded every 2 h
            req = self.db.get_request(rid)
        first = (req["requester_name"] or "someone")
        waited = ""
        created = parse_ts(req["created_at"])
        if created and elapsed(created, self.clock.now()).total_seconds() > 3600:
            mins = int(elapsed(created, self.clock.now()).total_seconds() // 60)
            waited = f"\nWaited {mins // 60} h {mins % 60} min" + (" (laptop was off)" if self.catchup_note else "")
        # Principal 2026-11-11: "why aren't you showing the card with the steps we agreed": the first card uses his
        # format too (request, screenshot, analysis, reply); the full analysis card replaces it in the panel.
        text, photo = approval_card_text(
            req, self.db.thread(rid),
            {"analysis": "Checking it now: the full analysis and the reply follow in a few minutes."}, "",
            f"First guess: {req['tier']} {req['lane']} {req['company'] or ''}".strip() + waited.replace("\n", " | "),
            full=self._full_cards())
        text = text.replace("NO DRAFT REPLY (Take over or Why?)", "NO DRAFT REPLY YET (Take over if you answer it)")
        await self.post_card(Card(kind=CardKind.new_request, text=redact(text)[:self._card_cap()], request_id=rid,
                                  buttons=[[Button("Take over", "to"), Button("Take over, no holding", "tn")]],
                                  attachments=[photo] if photo else []))
        ok, why = self.workers_allowed()
        if not ok:
            self.db.log_event("worker_skipped", rid, actor="listener", why=why)
            return
        self.spawn(self.spawner.run_worker(rid))

    async def on_packet_ready(self, payload: dict) -> None:
        """Checks can take many minutes (several claude calls): run them in the background so the bus keeps
        routing events and firing the holding floor meanwhile."""
        self.spawn(self.on_packet(payload))

    async def on_packet(self, payload: dict) -> None:
        """packet.ready -> checks (CHECKS module) -> verified recomputed in code -> DIAGNOSED + draft card."""
        rid, packet = payload["request_id"], payload["packet"]
        verified, verdicts, mode = False, [], "single-model"
        try:
            from .checks.judge import run_checks
            req, thread = self.db.get_request(rid), self.db.thread(rid)
            text = "\n".join(m["text"] or "" for m in thread)
            got = await asyncio.to_thread(run_checks, req, packet, text, self.adapters.claude, self.budget,
                                          paths.work_dir(rid))
            verdicts = [getattr(v, "__dict__", v) for v in got]
            passed = bool(got) and all(getattr(v, "passed", False) for v in got)
            # Cross-model refuter + evidence gate (17). A pure AND over the Claude checks: it can veto, never
            # rescue. Runs in a thread because the real Groq refuter makes a blocking network call.
            result = await asyncio.to_thread(self._verify, packet, passed)
            verified, mode = result.verified, result.mode
            from .checks.refuter_xmodel import gate_verdict
            verdicts.append(gate_verdict(result).__dict__)
        except Exception as e:                                   # noqa: BLE001 - checks failing = not verified
            self.db.log_event("checks_error", rid, actor="listener", error=type(e).__name__)
        if payload.get("run_no"):
            self.db.update_packet_checks(rid, payload["run_no"], verified, verdicts)
        else:
            self.db.save_packet(rid, packet, verified, verdicts)
        req = self.db.get_request(rid)
        if State(req["state"]) in TERMINAL:              # closed while the worker ran: keep the packet, no card
            self.db.log_event("packet_after_close", rid, actor="listener", state=str(req["state"]))
            return
        if State(req["state"]) in (State.TRIAGED, State.AWAITING_FIRST_REPLY, State.ACKED, State.NEEDS_INFO,
                                   State.FAILED_WORKER):
            self.move(rid, State.DIAGNOSED, "listener", "packet checked")
        if verified and packet.get("tier") == "T1":
            tm.schedule(self.db, rid, TimerKind.verified_answer,
                        self.policy.due_at(TimerKind.verified_answer, parse_ts(req["created_at"]) or self.clock.now()))
        self.bus.publish(Topics.CHECKS_DONE, {"request_id": rid, "verified": verified, "verdicts": verdicts,
                                              "packet": packet, "mode": mode})
        await self._draft_card(rid, packet, verified, verdicts, mode)

    def _verify(self, packet: dict, judges_passed: bool):
        """Evidence gate + Claude judges + cross-model refutation (17). Returns a refuter_xmodel.VerifyResult.
        The DEFAULT refuter is an INDEPENDENT fresh in-subscription Claude session (a different model ONLY when one
        is configured; no third-party egress) built on our own Claude runner; an optional non-Claude external
        backend is live-gated. With no refuter available the packet still verifies in degraded single-model mode
        (never refute-all)."""
        from .checks.refuter_xmodel import make_refuter, verify_packet
        from .kb.kb_search import runbook_entries
        try:
            runbook = runbook_entries(paths.kb_db()) if paths.kb_db().exists() else {}
        except Exception:                                        # noqa: BLE001 - no runbook = not verified
            runbook = {}
        return verify_packet(packet, runbook=runbook, judges_passed=judges_passed,
                             read_user_exists=bool(self.cfg.get("claude", {}).get("read_user_exists")),
                             refuter=make_refuter(self.cfg, runner=self.adapters.claude))

    async def _draft_card(self, rid: str, packet: dict, verified: bool, verdicts: list,
                          mode: str = "single-model") -> None:
        from .checks.refuter_xmodel import mode_label
        from .spawner import models_diverse
        draft = (packet.get("draft_reply") or {}).get("text") or ""
        passed = sum(1 for v in verdicts if (v.get("passed") if isinstance(v, dict) else False))
        # Honest: "cross-model" only when the refuter runs a model that DIFFERS from the worker's. A pin present but
        # equal to claude.model is the SAME model (fix: presence != difference) and must not imply diversity.
        diverse = models_diverse(self.cfg)
        status = (f"{packet.get('tier')} | checks {passed}/{len(verdicts)} | "
                  f"{mode_label(verified, mode, diverse=diverse)}")
        text, photo = approval_card_text(self.db.get_request(rid) or {"id": rid}, self.db.thread(rid), packet, draft,
                                         status, full=self._full_cards())
        buttons = [[Button("Approve", "ap"), Button("Edit", "ed")],
                   [Button("Take over", "to"), Button("Why?", "wy")]] \
            if draft else [[Button("Take over", "to"), Button("Why?", "wy")]]
        if self._is_email(rid):
            # 2026-11-11 email: Approve saves a Gmail draft (never sends); Close for mail that needs nothing
            # (an FYI like 'Basem, start the pilot') or that Principal already answered.
            if draft and not self._mail_only(rid):
                buttons = [[Button("Approve (Gmail draft)", "ap"), Button("Edit", "ed"), Button("Close", "ck")],
                           [Button("Take over", "to"), Button("Why?", "wy")]]
                text += "\n\nEmail: Approve saves this reply as a draft in your Gmail thread. You send it from Gmail; " \
                        "your sent reply closes the request."
            else:
                buttons = [[Button("Close", "ck"), Button("Take over", "to"), Button("Why?", "wy")]]
                text += ("\n\nEmail: nothing for you to answer here. Close it, or reply from Gmail (your sent reply "
                         "closes it).") if not draft else \
                    "\n\nEmail: the twin cannot save Gmail drafts yet. Copy the reply into Gmail; your sent reply " \
                    "closes the request."
        card = Card(kind=CardKind.new_request, text=redact(text)[:self._card_cap()], request_id=rid, buttons=buttons,
                    attachments=[photo] if photo else [])
        await self.post_card(card, draft_hash(draft) if draft else "")
        if draft and self.voice is not None:             # a second card: the same text as a voice note
            # Spawned: ElevenLabs + ffmpeg can take a minute and the bus runs handlers one at a time.
            self.spawn(self.voice.offer(rid, draft, kind=KIND_FOR_TIER.get(packet.get("tier"), OutKind.answer),
                                        lang=(packet.get("draft_reply") or {}).get("lang")))

    def _is_email(self, rid: str) -> bool:
        return str((self.db.get_request(rid) or {}).get("channel")) == str(Channel.email)

    def _mail_only(self, rid: str) -> bool:
        """An email request while the twin has no email sender (read-only mailbox): no Approve / Edit, which could
        only fail. Principal copies the reply into Gmail (2026-11-10)."""
        req = self.db.get_request(rid) or {}
        return str(req.get("channel")) == str(Channel.email) and getattr(self.adapters, "gmail_send", None) is None

    async def _why_card(self, rid: str) -> None:
        """Why? (Principal, 2026-11-10: "it should tell me why you will answer this"): the reasoning behind the draft,
        i.e. what the twin understood, the records it read and what it is unsure of, with Approve / Edit / Take over
        on the same card so he can act right after reading. The button used to log the tap and do nothing."""
        import json
        row = self.db.latest_packet(rid)
        packet = json.loads(row["packet_json"]) if row else {}
        draft = (packet.get("draft_reply") or {}).get("text") or ""

        def clip(s, n):
            s = " ".join(str(s or "").split())
            return s if len(s) <= n else s[: n - 1] + "…"

        lines = [f"{rid}: why this reply", ""]
        lines += ["Reply:", draft or "(no draft reply)", ""]
        if packet.get("understanding"):
            lines += ["What I understood:", clip(packet["understanding"], 900), ""]
        ev = [e for e in packet.get("evidence") or [] if isinstance(e, dict)]
        if ev:
            lines.append("What I checked:")
            for e in ev[:8]:
                where = e.get("model") or e.get("kind") or ""
                lines.append(f"- {where}: {clip(e.get('summary'), 260)}" if where else f"- {clip(e.get('summary'), 260)}")
            lines.append("")
        qs = packet.get("questions_for_principal") or []
        if qs:
            lines.append("Not sure about:")
            lines += [f"- {clip(q, 260)}" for q in qs[:3]]
            lines.append("")
        if not ev and not packet.get("understanding"):
            lines.append("No saved analysis for this request yet.")
        buttons = [[Button("Approve", "ap"), Button("Edit", "ed"),
                    Button("Take over", "to")]] if draft and not self._mail_only(rid) else [[Button("Take over", "to")]]
        if self._is_email(rid):
            buttons = [[Button("Approve (Gmail draft)", "ap"), Button("Close", "ck"), Button("Take over", "to")]] \
                if draft and not self._mail_only(rid) else [[Button("Close", "ck"), Button("Take over", "to")]]
        card = Card(kind=CardKind.new_request, text=redact("\n".join(lines).strip())[:3400], request_id=rid,
                    buttons=buttons)
        await self.post_card(card, draft_hash(draft) if draft else "")
        self.db.log_event("why_card", rid, actor="listener", evidence=len(ev))

    async def post_card(self, card: Card, draft_hash_: str = "") -> None:
        if self.bot_ctl is not None:
            await self.bot_ctl.post_card(card, draft_hash=draft_hash_)
        else:
            self.bus.publish(Topics.CARD_OUT, card)

    # ------------------------------------------------------------ cards out

    async def on_card(self, card) -> None:
        if self.bot_ctl is not None:
            await self.bot_ctl._on_card_out(card)            # a Card, or {"card", "draft_hash"}
            return
        if isinstance(card, dict):
            card = card["card"]
        card.card_id = self.db.add_card(card)                    # no controller: record (+ raw send if a bot)
        if self.adapters.bot is not None:
            bot_id = await self.adapters.bot.send_card(card, lambda b: f"{card.request_id or '-'}|{b.action}|x")
            self.db.mark_card(card.card_id, bot_msg_id=bot_id)

    # ------------------------------------------------------------ Principal's taps, replies, commands

    async def on_send_done(self, result) -> None:
        """Approve + close: the reply went out, so close the request and its ticket through the same path as
        Principal's own /close. Not sent (blocked, duplicate, failed): nothing is closed and Principal is told why."""
        key = getattr(result, "send_key", None)
        if key not in self.pending_close:
            return
        rid, uid = self.pending_close.pop(key)
        if str(getattr(result, "status", "")) == "drafted":
            # email: the draft is in his Gmail thread; his own sent reply closes the request (router._on_principal)
            self.card(CardKind.info, f"{rid}: reply saved as a draft in your Gmail, in the same thread. Open Gmail, "
                      "check To / Cc and press Send. Your sent reply closes the request.", rid)
            self.db.log_event("email_drafted", rid, actor=str(uid), send_key=key)
            return
        status = str(getattr(result, "status", ""))
        if status == "duplicate":
            # 2026-11-11 (REQ-0062): Approve on a second copy of a card whose reply was ALREADY posted is refused as
            # a duplicate (never posted twice); the reply is out, so close the request anyway.
            prev = self.db.get_send(key) if hasattr(self.db, "get_send") else None
            if prev and str(prev.get("status")) == "sent":
                status = "sent"
        if status == "sent" and closing.needs_development(self.db.get_request(rid) or {},
                                                          closing.latest_packet(self.db, rid)):
            # Principal 2026-11-11: a development request is not finished when he answers; it closes when he says so
            self.card(CardKind.info, f"{rid}: reply posted. This is a development request, so the request and its "
                      f"ticket stay open until it is delivered. Close it with: close {rid}", rid)
            self.db.log_event("approve_kept_open", rid, actor=str(uid), why="development")
            return
        if status == "sent":
            self.bus.publish(Topics.COMMAND_IN, {"user_id": uid, "command": "close", "args": rid})
            self.db.log_event("approve_close", rid, actor=str(uid), send_key=key)
        else:
            self.card(CardKind.info, f"{rid}: the reply was not sent ({result.status}: {result.reason or 'see card'}), "
                      "so the request and its ticket stay open.", rid)

    def outbound_for(self, rid: str, *, text: str | None = None, step: str | None = None,
                     kind: OutKind | None = None) -> OutboundMessage | None:
        """Build the message that a tap would send: the latest packet's draft (or given text), in the source thread."""
        req = self.db.get_request(rid)
        row = self.db.latest_packet(rid)
        if not req:
            return None
        import json
        packet = json.loads(row["packet_json"]) if row else {}
        if text is None:
            text = (packet.get("draft_reply") or {}).get("text")
        if not text:
            return None
        made = parse_ts(row["created_at"]) if row else self.clock.now()
        if str(req["channel"]) == str(Channel.email):
            # 2026-11-11: an email reply is a Gmail DRAFT in the same thread, To the requester only (Principal adds Cc in
            # Gmail if he wants), "Re:" subject, In-Reply-To the last mail of the thread (router keeps them).
            info = json.loads(self.db.get_cursor(f"gmreq:{rid}") or "{}")
            subject = info.get("subject") or req.get("title") or ""
            if subject and not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            return OutboundMessage(
                request_id=rid, step=step or f"draft-{row['run_no'] if row else 0}", kind=OutKind.email_reply,
                channel=Channel.email, text=text, chat_id=req["chat_id"], to=[req["requester_id"]] if req.get(
                    "requester_id") else [], subject=subject, in_reply_to=info.get("last_message_id", ""),
                thread_id=info.get("thread_id", ""), draft_hash=draft_hash(text), draft_made_at=made)
        return OutboundMessage(
            request_id=rid, step=step or f"draft-{row['run_no'] if row else 0}",
            kind=kind or KIND_FOR_TIER.get(packet.get("tier"), OutKind.answer), channel=Channel(req["channel"]),
            text=text, chat_id=req["chat_id"], topic_id=req["topic_id"], reply_to=req["root_msg_id"],
            draft_hash=draft_hash(text), draft_made_at=made)

    async def on_callback(self, p: dict) -> None:
        rid, action, uid, nonce = p.get("request_id"), p.get("action"), p.get("user_id"), p.get("nonce")
        tapped = action
        if action == "nv":                               # voice card "Send without voice" = Approve, text only
            action = "ap"
        if not rid:
            return
        req = self.db.get_request(rid)
        if not req:
            return
        if action in ("ap", "as", "sd", "nv", "ac") and State(req["state"]) in TERMINAL:
            self.pending_edit.pop(rid, None)             # closed meanwhile: a stale tap never sends
            self.card(CardKind.info, f"{rid} is already closed ({req['state']}); nothing was sent.", rid)
            self.db.log_event("callback_after_close", rid, actor=str(uid), action=action)
            return
        if action in ("ap", "as", "ac"):
            msg = self.outbound_for(rid)
            if msg and action == "as" and self.voice is not None:   # the voice card's own recording, or nothing
                msg, why = self.voice.attach(msg, self.db.get_approval(nonce) if nonce else None)
                if msg is None:
                    self.card(CardKind.info, f"{rid}: voice note not sent: {why}. Use the newest card.", rid)
            if msg:
                # Principal 2026-11-11: Approve closes the request and its ticket once the reply is SENT; if the
                # requester writes again, the request and ticket reopen (router._reopen + desk.on_reopened).
                self.pending_close[msg.send_key] = (rid, uid)
                self.bus.publish(Topics.SEND_REQUEST, {"msg": msg, "trigger": SendTrigger.approval, "nonce": nonce,
                                                       "user_id": uid})
        elif action == "sd" and rid in self.pending_edit:
            text = self.pending_edit.pop(rid)
            msg = self.outbound_for(rid, text=text, step=f"edit-{len(self.db.events(rid, 'state'))}")
            if msg:
                msg.draft_made_at = self.clock.now()
                self.pending_close[msg.send_key] = (rid, uid)    # his own edit, sent: closes like Approve
                self.bus.publish(Topics.SEND_REQUEST, {"msg": msg, "trigger": SendTrigger.principal_edit, "nonce": nonce,
                                                       "user_id": uid})
        elif action == "rr" and self.voice is not None:
            msg = self.outbound_for(rid)
            if msg:
                rec = self.voice.current(rid) or {}
                self.spawn(self.voice.offer(rid, msg.text, kind=msg.kind, lang=rec.get("lang"), fresh=True))
        elif action == "cx":
            self.pending_edit.pop(rid, None)
        elif action in ("to", "tn"):
            # Principal 2026-11-09: "I pressed Take over, nothing happened" - the tap worked silently. Always confirm.
            if State(req["state"]) in TERMINAL:
                self.card(CardKind.info, f"{rid} is already closed. Nothing to take over.", rid)
            elif State(req["state"]) == State.TAKEN_OVER:
                self.card(CardKind.info, f"{rid} is already yours (taken over). Reply in the group, then /close {rid}.",
                          rid)
            else:
                self.db.update_request(rid, taken_over=1, no_holding=int(action == "tn"))
                self.move(rid, State.TAKEN_OVER, str(uid), "Take over")
                self.card(CardKind.info, f"{rid} taken over: it is yours now. I stop drafting and send nothing on it. "
                          f"Reply in the group yourself, then /close {rid} (closes its ticket too). "
                          f"Hand it back with /give {rid}.", rid)
        elif action == "gb" and req["state"] == State.TAKEN_OVER:
            self.db.update_request(rid, taken_over=0, no_holding=0)
            self.move(rid, State(req["prev_state"] or State.TRIAGED), str(uid), "handed back")
        elif action == "wy":
            await self._why_card(rid)
        elif action == "ck":
            self.move(rid, State.CLOSED_BY_PRINCIPAL, str(uid), "closed by Principal")
        elif action == "sz":
            self._snooze(rid)
        elif action == "kp":
            tm.ensure_timer(self.db, self.policy, rid, self.clock.now())
        self.db.log_event("callback", rid, actor=str(uid), action=tapped)

    def _snooze(self, rid: str) -> None:
        """Snooze 30 min (max 2): moves the reminder only, never the holding floor."""
        s = self.cfg.get("timers", {}).get("snooze", {})
        used = len(self.db.events(rid, "snooze"))
        if used >= int(s.get("max_count", 2)):
            self.card(CardKind.info, f"{rid}: snooze already used {used} times.", rid)
            return
        for t in self.db.future_timers(rid):
            if t["kind"] == TimerKind.reminder:
                self.db.set_timer_due(t["id"], tm.add_minutes(self.clock.now(), s.get("minutes", 30)))
        self.db.log_event("snooze", rid, actor="principal")

    async def on_reply(self, p: dict) -> None:
        """ForceReply text (Edit) waits for [Send]; a plain reply is an instruction, never outgoing text."""
        rid = p.get("request_id")
        if not rid:
            return
        if p.get("force_reply"):
            self.pending_edit[rid] = p.get("text") or ""
            return
        self.db.log_event("instruction", rid, actor=str(p.get("user_id")), text=(p.get("text") or "")[:500])
        ok, _ = self.workers_allowed()
        if ok:
            note = paths.work_dir(rid) / "NOTE.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            # A backup's reply is heard too, but it must never be passed off to the worker as Principal's own words.
            principal = int((self.cfg.get("people") or {}).get("principal_tg_user_id") or 0)
            who = "Principal's" if principal and int(p.get("user_id") or 0) == principal else "Backup colleague's (not Principal)"
            with note.open("a", encoding="utf-8") as f:
                f.write(f"\n{who} instruction: {(p.get('text') or '')[:500]}\n")
            self.spawn(self.spawner.run_worker(rid))

    async def on_command(self, p: dict) -> None:
        cmd, args = p.get("command"), (p.get("args") or "").strip()
        if cmd in ("take", "give") and args:
            rid = args.split()[0].upper()
            await self.on_callback({"request_id": rid, "action": "to" if cmd == "take" else "gb",
                                    "user_id": p.get("user_id")})
        elif cmd == "close" and args:                    # Principal only (bot_io: not a backup command)
            rid = args.split()[0].upper()
            await self.on_callback({"request_id": rid, "action": "ck", "user_id": p.get("user_id")})
        elif cmd == "say" and args:
            await self._say(p.get("user_id"), args)

    async def _say(self, uid, args: str) -> None:
        """`say REQ-x <text>` (Principal 2026-11-15): post the reply Principal approved in the Digital Twin kit's ticket
        session, in the request's own thread, as him (Telegram), or as a Gmail draft (email). It goes through the
        normal sender like his own Edit + Send: same guards, one-time approval bound to this exact text."""
        import re
        from datetime import timedelta
        principal = int((self.cfg.get("people") or {}).get("principal_tg_user_id") or 0)
        if not principal or int(uid or 0) != principal:
            self.db.log_event("say_refused", None, actor=str(uid), why="not Principal")
            return
        parts = re.split(r"\s", args.strip(), maxsplit=1)
        rid, text = parts[0].upper(), (parts[1].strip() if len(parts) > 1 else "")
        req = self.db.get_request(rid)
        if not req or not text:
            self.card(CardKind.info, f"say {rid}: {'no such request' if not req else 'no text'}; nothing was sent.",
                      rid if req else None)
            return
        msg = self.outbound_for(rid, text=text, step=f"say-{draft_hash(text)[:8]}")
        if not msg:
            self.card(CardKind.info, f"say {rid}: no thread to reply in; nothing was sent.", rid)
            return
        msg.draft_made_at = self.clock.now()
        nonce = self.db.issue_approval(rid, 0, "sd", principal, msg.draft_hash, self.clock.now() + timedelta(minutes=30))
        self.pending_close[msg.send_key] = (rid, principal)
        self.db.log_event("say", rid, actor=str(principal), send_key=msg.send_key, channel=str(msg.channel))
        self.bus.publish(Topics.SEND_REQUEST, {"msg": msg, "trigger": SendTrigger.principal_edit, "nonce": nonce,
                                               "user_id": principal})

    async def on_send_request(self, p: dict) -> None:
        if self.sender is None:
            self.db.log_event("send_skipped", p["msg"].request_id, actor="listener", why="no sender loaded",
                              kind=str(p["msg"].kind))
            return
        await self.sender.send(p["msg"], SendTrigger(p["trigger"]), approval_nonce=p.get("nonce"),
                               approver_user_id=p.get("user_id"))

    # ------------------------------------------------------------ kill switch

    async def on_kill(self, p: dict) -> None:
        """On: every open request -> PAUSED (re-card every 2 h, backup escalation kept). Off: back to prev_state."""
        on, by = bool(p.get("on")), str(p.get("by") or "system")
        if on == self.db.kill_on():
            return
        self.db.set_kill(on, by=by)
        for req in self.db.open_requests():
            rid, state = req["id"], State(req["state"])
            if on and state != State.PAUSED:
                self.pause(rid, "kill switch", by)
            elif not on and state == State.PAUSED:
                back = State(req["prev_state"] or State.TRIAGED)
                tm.cancel(self.db, rid, [TimerKind.paused_recard])
                self.move(rid, back, by, "resumed")
        self.bus.publish(Topics.HEALTH, {"component": "kill_switch", "ok": not on, "detail": f"by {by}"})

    def pause(self, rid: str, reason: str, by: str = "listener") -> None:
        """PAUSED: reply timers are cancelled (re-armed on resume, so the hard floor comes back), re-card 2-hourly."""
        tm.cancel(self.db, rid, REPLY_TIMERS)
        self.move(rid, State.PAUSED, by, reason)

    async def on_health(self, p: dict) -> None:
        """One health card per change of a component's state (not one per tick)."""
        comp, ok = str(p.get("component")), bool(p.get("ok"))
        if self.health.get(comp, True) == ok:
            self.health[comp] = ok
            return
        self.health[comp] = ok
        self.db.log_event("health", actor="listener", component=comp, ok=ok, detail=str(p.get("detail"))[:200])
        if comp == "kill_switch":
            return                                               # the bot already answered /stop and /resume
        text = f"{comp}: {'back to normal' if ok else 'problem'}" + (f" ({p.get('detail')})" if p.get("detail") else "")
        self.card(CardKind.health, text)

    def check_stop_file(self) -> None:
        """The STOP file is the other half of the kill switch: notice it appearing or going away."""
        on = paths.stop_file().exists()
        if on != self.db.kill_on():
            self.bus.publish(Topics.KILL_CHANGED, {"on": on, "by": "STOP file"})
