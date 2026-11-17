"""The ONLY code that sends to a client (CONTRACT 9.8, 05 3.4 and 2.5).

Order inside send():
  1. kill switch (STOP file or db flag)        2. duplicate send key       3. Telegram account blocked flag
  4. lane dial for this trigger (off / too low) 5. approval record (one-time nonce, Principal only, same draft hash)
  6. live thread re-read (fails closed)        7. own checks: stale draft, human-active lock, already answered
  8. guards.check_outbound                     9. shadow mode (observe / dry_run / send disabled) -> would_send
 10. kill switch again                        11. claim the send key      12. adapter send
 13. from_id post-check                       14. finish_send + audit + send.done

A blocked or would_send row never burns the send key, so the hard-floor holding reply can still go later.
A voice note (msg.voice_path) takes the same path; step 7 adds voice.policy.voice_blockers (consent D2, persona
rule, autopilot switch, lane, exact previewed file) and step 12 calls adapter.send_voice instead of send_message.
Client text is never written to the audit log or events: only its sha256.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from . import paths
from .bus import Topics
from .checks import guards
from .clock import CAIRO, elapsed, latest, shift, utc
from .models import (Card, CardKind, Channel, Dial, GuardResult, OutboundMessage, OutKind, SendResult,
                     SendStatus, SendTrigger, parse_ts, utc_iso)
from .redact import redact
from .sender_approval import APPROVAL_ACTIONS, check_approval, text_hash  # noqa: F401 (text_hash re-exported)
from .sender_errors import classify_error
from .voice.policy import VOICE_MODE_GUARDS, voice_blockers, voice_cfg

TELEGRAM = {Channel.telegram_group, Channel.telegram_dm}
EMAIL = {Channel.email, Channel.ticket_mail}
# Guards that only say "not live yet". In shadow mode these turn into would_send, not blocked.
SHADOW_ONLY_GUARDS = guards.MODE_GUARDS            # one list, shared with guards.only_mode_blocks
SHADOW_OK = SHADOW_ONLY_GUARDS | VOICE_MODE_GUARDS  # + "no voice consent yet": observe still records would_send
# Lowest dial at which each trigger may really send.
MIN_DIAL = {SendTrigger.approval: Dial.draft, SendTrigger.principal_edit: Dial.draft,
            SendTrigger.timeout: Dial.auto_on_timeout, SendTrigger.holding_floor: Dial.auto_on_timeout}
AUTOPILOT = {SendTrigger.timeout, SendTrigger.holding_floor}
HOLDING_KINDS = {OutKind.holding, OutKind.email_holding}
KILL_CURSOR, TG_BLOCK_CURSOR = "kill_switch", "tg_send_blocked"
EPOCH = datetime(2000, 1, 1, tzinfo=CAIRO)
ON_VALUES = {"1", "on", "true", "yes"}


class Sender:
    def __init__(self, db, cfg: dict, adapters: dict[Channel, object], clock, bus, audit, *,
                 sleep=asyncio.sleep, max_flood_wait_s: int = 30, vault_hashes=()):
        """vault_hashes: creds.vault_hashes() taken once at listener start, for the exact-leak guard."""
        self.db, self.cfg, self.adapters, self.clock, self.bus, self.audit = db, cfg, adapters, clock, bus, audit
        self.vault_hashes = tuple(vault_hashes)
        self.sleep, self.max_flood_wait_s = sleep, max_flood_wait_s
        self.queue: list[tuple[OutboundMessage, SendTrigger]] = []   # FloodWait: sent later by drain_queue()
        self.flood_until: datetime | None = None

    # ------------------------------------------------------------ flags

    def kill_switch_on(self) -> bool:
        return paths.stop_file().exists() or str(self.db.get_cursor(KILL_CURSOR) or "").lower() in ON_VALUES

    def tg_blocked(self) -> bool:
        return str(self.db.get_cursor(TG_BLOCK_CURSOR) or "").lower() in ON_VALUES

    def _shadow(self, dial: Dial) -> bool:
        return self.cfg.get("run_mode") != "live" or not self.cfg.get("send_enabled") or dial == Dial.observe

    def _adapter(self, channel: Channel):
        if channel in TELEGRAM:
            return self.adapters.get(channel) or self.adapters.get(Channel.telegram_group)
        if channel == Channel.ticket_mail:
            return self.adapters.get(channel) or self.adapters.get(Channel.email)
        return self.adapters.get(channel)

    # ------------------------------------------------------------ main entry

    async def send(self, msg: OutboundMessage, trigger: SendTrigger, *, approval_nonce: str | None = None,
                   approver_user_id: int | None = None) -> SendResult:
        trigger, lane = SendTrigger(trigger), msg.lane
        if self.kill_switch_on():
            return await self._blocked(msg, trigger, ["kill_switch"], "kill switch is on")
        if self._key_used(msg):
            return await self._done(SendResult(SendStatus.duplicate, msg.send_key, "send key already used"), msg, trigger)
        if msg.channel in TELEGRAM and self.tg_blocked():
            return await self._blocked(msg, trigger, ["tg_send_blocked"], "Telegram sends stopped (PEER_FLOOD)")
        if Channel(msg.channel) == Channel.bot or self._adapter(msg.channel) is None:
            return await self._blocked(msg, trigger, ["no_adapter"], f"no send adapter for {msg.channel}")
        if msg.voice_path and not callable(getattr(self._adapter(msg.channel), "send_voice", None)):
            return await self._blocked(msg, trigger, ["no_adapter"], f"no voice adapter for {msg.channel}")
        dial = Dial(self.db.get_dial(lane))
        if dial == Dial.off or (dial != Dial.observe and not dial.at_least(MIN_DIAL[trigger])):
            return await self._blocked(msg, trigger, ["dial"], f"lane {lane} is {dial}; {trigger} needs {MIN_DIAL[trigger]}")
        if msg.channel in TELEGRAM and _int(msg.reply_to) is None:
            # Never post loose in a topic, and never re-read a topic from message 0 (red-team #17).
            return await self._blocked(msg, trigger, ["thread_binding"], "no reply_to message id")
        approval = None
        if trigger in APPROVAL_ACTIONS:
            approval, why = self._use_approval(msg, trigger, approval_nonce, approver_user_id)
            if approval is None:
                return await self._blocked(msg, trigger, ["approval_binding"], why)
        try:
            after_draft, principal_last, answered = await self._live_read(msg)
        except Exception as e:  # noqa: BLE001  (never send blind)
            return await self._blocked(msg, trigger, ["live_read"], f"live thread re-read failed: {type(e).__name__}")
        now = self.clock.now()
        own = self._own_checks(msg, trigger, after_draft, principal_last, answered, now)
        own += voice_blockers(msg, self.cfg, trigger)            # [] for a text message
        request = self.db.get_request(msg.request_id) or {}
        if trigger in AUTOPILOT and request.get("first_reply_by") == "principal" and "already_answered" not in own:
            own.append("already_answered")
        ctx = guards.GuardContext(
            cfg=self.cfg, db=self.db, now=now, trigger=trigger, kill_switch_on=False, approval=approval,
            thread_after_draft=after_draft, principal_last_msg_at=principal_last,
            original_msg_ts=parse_ts(request.get("created_at")))
        if hasattr(ctx, "vault_hashes"):
            ctx.vault_hashes = self.vault_hashes
        # Wire the identity guard: an autonomous reply to Principal / a backup / an owner is card-only (was dead code).
        ctx.requester_is_bot_or_staff_owner = guards.requester_is_owner(self.cfg, request.get("requester_id"))
        try:
            g = guards.check_outbound(msg, ctx)
        except Exception as e:  # noqa: BLE001  (a broken guard blocks; it never lets a send through)
            g = GuardResult(False, [f"guards_error:{type(e).__name__}"], [])
        blocked_by = sorted(set(own) | set(g.blocked_by))
        overridden = []
        if "stale_draft" in blocked_by and principal_tap_overrides_stale(self.cfg, trigger):
            # Principal 2026-11-09: "it should send the message directly after I press Approve". His own tap sends even
            # when the requester wrote after the draft (e.g. a "؟"); he is told what came in, nothing is held back.
            blocked_by = [b for b in blocked_by if b != "stale_draft"]
            overridden = after_draft
            try:
                g.warnings.append("stale_draft overridden by Principal's own tap")
            except AttributeError:
                pass
        shadow = self._shadow(dial)
        if blocked_by and not (shadow and set(blocked_by) <= SHADOW_OK):
            if "stale_draft" in blocked_by:
                self._recard_stale(msg, after_draft)
            return await self._blocked(msg, trigger, blocked_by, "guards blocked", g.warnings)
        if shadow:
            return await self._would_send(msg, trigger, dial, blocked_by, g.warnings)
        if self.kill_switch_on():   # /stop may have landed during the live re-read
            return await self._blocked(msg, trigger, ["kill_switch"], "kill switch is on")
        result = await self._really_send(msg, trigger, g)
        if overridden and result.status == SendStatus.sent:
            said = "; ".join(f"{(r.get('sender_name') or 'someone').split(' Globex')[0]}: "
                             f"{(r.get('text') or '(photo)')[:120]}" for r in overridden[:3])
            self._card(CardKind.info, msg.request_id, f"{msg.request_id}: sent on your Approve. After the draft "
                       f"they also wrote: {said}")
        return result

    # ------------------------------------------------------------ checks

    def _key_used(self, msg: OutboundMessage) -> bool:
        rows = self.db.sends_since(EPOCH, request_id=msg.request_id, statuses=("sent", "maybe_sent", "pending"))
        return any(r.get("send_key") == msg.send_key for r in rows)

    def _use_approval(self, msg, trigger, nonce, user_id) -> tuple[dict | None, str]:
        return check_approval(self.db, self.cfg, msg, trigger, nonce, user_id, self.clock.now())

    async def _live_read(self, msg: OutboundMessage):
        """Re-read the live thread. Returns (messages after the draft, Principal's last message time, answered?)."""
        if msg.channel not in TELEGRAM or msg.chat_id is None:
            return [], None, False
        adapter = self._adapter(msg.channel)
        after = int(msg.reply_to) if str(msg.reply_to or "").lstrip("-").isdigit() else 0
        events = await adapter.read_thread(int(msg.chat_id), msg.topic_id, after)
        # Our own earlier sends post from Principal's account: they are not "Principal is active" and not "new".
        since = utc(self.clock.now()) - timedelta(days=4)
        own_ids = {str(r.get("remote_id")) for r in self.db.sends_since(
            since, chat_id=msg.chat_id, statuses=("sent", "maybe_sent")) if r.get("remote_id")}
        others = [e for e in events if str(e.msg_id) not in own_ids]
        principal_ts = [e.ts for e in others if e.sender.is_principal]          # chat-wide: human-active lock
        # "Answered" only if Principal replied to THIS requester's message (a busy topic has other threads).
        answered = any(e.sender.is_principal and str(e.reply_to) == str(msg.reply_to) for e in others)
        made = msg.draft_made_at
        after_draft = [_row(e) for e in others if made is not None and utc(e.ts) > utc(made)]
        return after_draft, (latest(*principal_ts) if principal_ts else None), answered

    def _own_checks(self, msg, trigger, after_draft, principal_last, answered, now) -> list[str]:
        out = []
        if after_draft and msg.kind not in HOLDING_KINDS:
            out.append("stale_draft")                       # red-team #10
        if trigger in AUTOPILOT:
            lock = float((self.cfg.get("timers") or {}).get("human_active_lock_min", 5))
            if principal_last is not None and elapsed(principal_last, now) < timedelta(minutes=lock):
                out.append("human_active")
            elif answered:
                out.append("already_answered")
        return out

    # ------------------------------------------------------------ outcomes

    async def _really_send(self, msg: OutboundMessage, trigger: SendTrigger, g: GuardResult, *,
                           retried: bool = False) -> SendResult:
        if not self.db.claim_send(msg, trigger):        # stored BEFORE the network call
            return await self._done(SendResult(SendStatus.duplicate, msg.send_key, "send key already claimed"), msg, trigger)
        if self.flood_until and utc(self.clock.now()) < utc(self.flood_until) and msg.channel in TELEGRAM:
            return await self._flood_queue(msg, trigger, "flood wait in force")
        adapter = self._adapter(msg.channel)
        from_id_ok = None
        try:
            if msg.channel in TELEGRAM and msg.voice_path:
                res = await adapter.send_voice(int(msg.chat_id), msg.voice_path, caption=voice_cfg(self.cfg)["caption"],
                                               reply_to=_int(msg.reply_to), topic_id=msg.topic_id)
            elif msg.channel in TELEGRAM:
                res = await adapter.send_message(int(msg.chat_id), msg.text, reply_to=_int(msg.reply_to),
                                                 topic_id=msg.topic_id)
            if msg.channel in TELEGRAM:
                remote_id, from_id = str(res.get("msg_id")), res.get("from_id")
                from_id_ok = res.get("from_id_ok")
                if from_id_ok is None:
                    from_id_ok = from_id is not None and int(from_id) == int(getattr(adapter, "my_user_id", 0) or 0)
            else:
                try:
                    draft_id = await asyncio.to_thread(adapter.create_draft, msg)
                except Exception as e:  # noqa: BLE001  (no draft = nothing went out)
                    self.db.finish_send(msg.send_key, SendStatus.failed, error=f"draft: {type(e).__name__}")
                    return await self._done(SendResult(SendStatus.failed, msg.send_key, type(e).__name__), msg, trigger)
                if msg.channel == Channel.email and (self.cfg.get("sender") or {}).get("email_draft_only", True):
                    # Acme rule: never auto-send a client email, drafts only (Principal 2026-11-11). The draft sits
                    # in the Gmail thread; Principal sends it, and his sent reply closes the request (router).
                    self.db.finish_send(msg.send_key, SendStatus.drafted, sent_at=utc_iso(self.clock.now()),
                                        remote_id=f"draft:{draft_id}")
                    return await self._done(SendResult(SendStatus.drafted, msg.send_key, "saved as a Gmail draft",
                                                       remote_id=f"draft:{draft_id}", guard=g), msg, trigger)
                remote_id = str(await asyncio.to_thread(adapter.send_draft, draft_id))
        except Exception as e:  # noqa: BLE001
            return await self._send_error(msg, trigger, e, g, retried)
        if from_id_ok is False and msg.channel in TELEGRAM and anonymous_ok(self.cfg, msg.chat_id) \
                and is_the_chat(from_id, msg.chat_id):
            # Principal 2026-11-09: he posts in this group as an anonymous admin (the owner controls that switch), so a
            # post shown as THIS group is his own; any other author is still a wrong identity.
            from_id_ok = None
            try:
                g.warnings.append("posted as the group (Principal's anonymous admin; chats[].anonymous_ok)")
            except AttributeError:
                pass
        if from_id_ok is False:                              # posted as the anonymous group, not as Principal
            return await self._wrong_identity(msg, trigger, adapter, remote_id)
        self.db.finish_send(msg.send_key, SendStatus.sent, sent_at=utc_iso(self.clock.now()), remote_id=remote_id,
                            from_id_ok=None if from_id_ok is None else 1,
                            guard_json=json.dumps({"warnings": g.warnings}))
        req = self.db.get_request(msg.request_id)
        if req is not None and not req.get("first_reply_at"):
            self.db.update_request(msg.request_id, first_reply_at=utc_iso(self.clock.now()), first_reply_by="agent")
        self.db.add_kpi("sent", 1, lane=str(msg.lane), request_id=msg.request_id, trigger=str(trigger))
        return await self._done(SendResult(SendStatus.sent, msg.send_key, remote_id=remote_id, guard=g), msg, trigger)

    async def _send_error(self, msg, trigger, e: Exception, g: GuardResult, retried: bool) -> SendResult:
        kind, seconds = classify_error(e)
        if kind == "flood_wait":
            self.flood_until = shift(self.clock.now(), timedelta(seconds=seconds))
            if seconds > self.max_flood_wait_s or retried:
                return await self._flood_queue(msg, trigger, f"flood_wait {seconds}s")
            self.db.finish_send(msg.send_key, SendStatus.failed, error=f"flood_wait {seconds}s, retrying")
            await self.sleep(seconds)                        # respect the wait, never retry faster
            self.flood_until = None
            # The world may have moved while we waited: kill switch and live thread again (approval already used).
            if self.kill_switch_on():
                return await self._blocked(msg, trigger, ["kill_switch"], "kill switch is on")
            try:
                after_draft, principal_last, answered = await self._live_read(msg)
            except Exception as err:  # noqa: BLE001
                return await self._blocked(msg, trigger, ["live_read"], f"live re-read failed: {type(err).__name__}")
            own = self._own_checks(msg, trigger, after_draft, principal_last, answered, self.clock.now())
            if own:
                if "stale_draft" in own:
                    self._recard_stale(msg, after_draft)
                return await self._blocked(msg, trigger, own, "thread changed during flood wait")
            return await self._really_send(msg, trigger, g, retried=True)
        if kind == "refused":
            self.db.finish_send(msg.send_key, SendStatus.failed, error=type(e).__name__)
            return await self._done(SendResult(SendStatus.failed, msg.send_key, type(e).__name__), msg, trigger)
        if kind == "peer_flood":
            self.db.set_cursor(TG_BLOCK_CURSOR, "1")
            self.db.finish_send(msg.send_key, SendStatus.failed, error="peer_flood")
            self._card(CardKind.circuit_breaker, msg.request_id, "Telegram flagged the account (PEER_FLOOD or spam "
                       "notice). All sends from your account are stopped. Check Telegram, then clear the flag.")
            self.bus.publish(Topics.HEALTH, {"component": "telegram_send", "ok": False, "detail": "peer_flood"})
            return await self._done(SendResult(SendStatus.failed, msg.send_key, "peer_flood"), msg, trigger)
        # Anything else: we cannot know if it went out. Key stays burned; read back before any retry.
        self.db.finish_send(msg.send_key, SendStatus.maybe_sent, error=type(e).__name__)
        self._card(CardKind.health, msg.request_id, f"{msg.request_id}: a send may or may not have gone out "
                   f"(network problem). Check the chat before resending.")
        return await self._done(SendResult(SendStatus.maybe_sent, msg.send_key, type(e).__name__), msg, trigger)

    async def _flood_queue(self, msg, trigger, why: str) -> SendResult:
        self.db.finish_send(msg.send_key, SendStatus.failed, error=why)
        self.queue.append((msg, trigger))
        return await self._done(SendResult(SendStatus.failed, msg.send_key, why), msg, trigger)

    async def drain_queue(self) -> list[SendResult]:
        """Send what FloodWait queued, once the wait is over. The listener calls this on each tick."""
        if self.flood_until and utc(self.clock.now()) < utc(self.flood_until):
            return []
        self.flood_until, items, self.queue = None, self.queue, []
        results = []
        for m, t in items:
            if t in AUTOPILOT:
                results.append(await self.send(m, t))       # full checks again
            else:                                           # the one-time approval is used up: ask again
                self._card(CardKind.info, m.request_id, f"{m.request_id}: Telegram asked us to wait, so your "
                           "approved reply was not sent. Please approve it again.")
        return results

    async def _wrong_identity(self, msg, trigger, adapter, remote_id: str) -> SendResult:
        try:
            await adapter.delete_message(int(msg.chat_id), int(remote_id))
        finally:
            dial = Dial(self.db.get_dial(msg.lane))
            lower = list(Dial)[max(dial.rank() - 1, Dial.observe.rank())]
            self.db.set_dial(msg.lane, lower, by="sender:from_id_check")
            self.db.finish_send(msg.send_key, SendStatus.failed, remote_id=remote_id, from_id_ok=0,
                                error="posted under the wrong identity; deleted")
            self._card(CardKind.demotion, msg.request_id, f"{msg.request_id}: a message went out under the wrong "
                       f"identity (not your account). It was deleted and lane {msg.lane} was moved down one level. "
                       "Check 'Remain anonymous' is off for your account.")
        return await self._done(SendResult(SendStatus.failed, msg.send_key, "wrong identity"), msg, trigger)

    async def _would_send(self, msg, trigger, dial: Dial, gates: list[str], warnings: list[str]) -> SendResult:
        if not self.db.claim_send(msg, trigger):
            return await self._done(SendResult(SendStatus.duplicate, msg.send_key, "send key already claimed"), msg, trigger)
        self.db.finish_send(msg.send_key, SendStatus.would_send, guard_json=json.dumps({"gates": gates}))
        self.db.add_kpi("would_have_sent", 1, lane=str(msg.lane), request_id=msg.request_id, trigger=str(trigger))
        voice = f"\n\nAs a voice note. Preview (play this file):\n{msg.voice_path}" if msg.voice_path else ""
        self._card(CardKind.observe_would_send, msg.request_id,
                   f"{msg.request_id} ({msg.lane}): autopilot would have sent:\n\n{redact(msg.text)[:3000]}{voice}")
        why = "observe" if dial == Dial.observe else ("dry_run" if self.cfg.get("run_mode") != "live" else "send disabled")
        g = GuardResult(True, gates, warnings)
        return await self._done(SendResult(SendStatus.would_send, msg.send_key, why, guard=g), msg, trigger)

    async def _blocked(self, msg, trigger, blocked_by: list[str], why: str, warnings=()) -> SendResult:
        g = GuardResult(False, list(blocked_by), list(warnings))
        if self.db.claim_send(msg, trigger):
            self.db.finish_send(msg.send_key, SendStatus.blocked, error=why,
                                guard_json=json.dumps({"blocked_by": g.blocked_by, "warnings": g.warnings}))
        self.db.add_kpi("guard_trip", 1, lane=str(msg.lane), request_id=msg.request_id, blocked_by=g.blocked_by)
        if trigger == SendTrigger.holding_floor:            # never silently queued (red-team #8)
            text = (f"{msg.request_id}: the holding reply could not go out ({', '.join(g.blocked_by)}). "
                    "The requester has no reply yet. Please answer or take over.")
            for who in ("principal", "backup"):
                self._card(CardKind.holding_blocked, msg.request_id, text, to=who)
        return await self._done(SendResult(SendStatus.blocked, msg.send_key, why, guard=g), msg, trigger)

    def _recard_stale(self, msg, after_draft: list[dict]) -> None:
        self._card(CardKind.info, msg.request_id, f"{msg.request_id}: {len(after_draft)} new message(s) in the thread "
                   "after this draft was made. Not sent. A new draft card follows.")
        last = after_draft[-1].get("event_id") if after_draft else None
        self.bus.publish(Topics.REQUEST_UPDATED, {"request_id": msg.request_id, "event_id": last})

    def _card(self, kind: CardKind, request_id: str | None, text: str, *, to: str = "principal") -> None:
        self.bus.publish(Topics.CARD_OUT, Card(kind=kind, text=redact(text, self.vault_hashes)[:3400],
                                               request_id=request_id, to=to))

    async def _done(self, result: SendResult, msg: OutboundMessage, trigger: SendTrigger) -> SendResult:
        record = {"kind": "send", "send_key": msg.send_key, "request_id": msg.request_id, "channel": str(msg.channel),
                  "lane": str(msg.lane), "out_kind": str(msg.kind), "trigger": str(trigger),
                  "status": str(result.status), "reason": result.reason, "text_sha256": text_hash(msg.text),
                  "remote_id": result.remote_id, "voice_sha256": msg.voice_sha256 if msg.voice_path else None,
                  "blocked_by": result.guard.blocked_by if result.guard else []}
        self.audit.append(record)
        self.db.log_event("send", msg.request_id, actor="sender", **{k: v for k, v in record.items() if k not in ("kind", "request_id")})
        self.bus.publish(Topics.SEND_DONE, result)
        return result


def _int(v) -> int | None:
    return int(v) if str(v or "").lstrip("-").isdigit() else None


def _row(e) -> dict:
    """An Event as a state.db messages-shaped dict (what GuardContext.thread_after_draft holds)."""
    return {"event_id": e.event_id, "msg_id": str(e.msg_id), "sender_id": str(e.sender.tg_id),
            "sender_name": e.sender.name, "is_principal": int(e.sender.is_principal), "is_bot": int(e.sender.is_bot),
            "ts": utc_iso(e.ts), "text": e.text}


def anonymous_ok(cfg: dict, chat_id) -> bool:
    """chats[].anonymous_ok: Principal posts in this chat as an anonymous admin (Principal 2026-11-09, Odoo x Globex)."""
    from .config import _same_id
    for c in cfg.get("chats") or []:
        if isinstance(c, dict) and _same_id(c.get("chat_id"), chat_id):
            return c.get("anonymous_ok") is True
    return False


def is_the_chat(from_id, chat_id) -> bool:
    """True when Telegram shows the post's author as this very chat (an anonymous admin post), in either id form:
    the full -100xxxxxxxxxx id or the bare channel id (telegram_send.author_id gives -abs(channel_id))."""
    try:
        a, c = abs(int(from_id or 0)), abs(int(chat_id or 0))
    except (TypeError, ValueError):
        return False
    if not a or not c:
        return False
    bare = int(str(c)[3:]) if str(c).startswith("100") and len(str(c)) > 3 else c
    return a in (c, bare)


def principal_tap_overrides_stale(cfg: dict, trigger) -> bool:
    """sender.principal_tap_overrides_stale (Principal 2026-11-09): HIS Approve / Edit-Send goes out even when the requester
    wrote after the draft. Autonomous sends (timeout, holding floor) keep the stale-draft block."""
    on = ((cfg.get("sender") or {}).get("principal_tap_overrides_stale")) is True
    return on and trigger in APPROVAL_ACTIONS
