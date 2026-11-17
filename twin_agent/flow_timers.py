"""What each timer does when it fires (05 2.3), plus the sweep, boot catch-up and STALLED check. Owner: CORE.

Every request timer ends with ensure_timer, so an open request is never left without a next timer.
Sends go out only as send.request to the sender (which applies the guards and the dials).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from . import closing
from . import timers as tm
from .bus import Topics
from .clock import elapsed, utc
from .models import (TERMINAL, Button, CardKind, Channel, OutboundMessage, OutKind, SendStatus, SendTrigger,
                     State, TimerKind, parse_ts, utc_iso)

log = logging.getLogger(__name__)
K = TimerKind
GLOBAL_KINDS = (K.sweep_light, K.sweep_full, K.digest, K.scorecard, K.deadman_ping)
CARD_ONLY = {
    K.evidence_chase_1: "evidence chase 1: still waiting on the requester",
    K.evidence_chase_2: "evidence chase 2: parked as 'waiting on you'; weekly re-check stays",
    K.approval_reping_1: "reminder: a card is waiting for your tap",
    K.approval_reping_2: "second reminder: a card is waiting for your tap",
    K.approval_expire: "the approval card expired (nothing was sent)",
    K.supervisor_review: "Supervisor's review is still pending",
    K.worked_chaser_1: "time to ask 'worked?' (chaser 1)",
    K.ticket_readback_1: "no ticket ack yet (15 min)",
    K.ticket_readback_2: "still no ticket ack (30 min): check it, do not resend blind",
    K.ticket_idle: "ticket idle 3 working days: chaser draft needed",
    K.client_question: "a client question is open on the ticket",
    K.client_testing_due: "ticket in Client Testing: proof is due",
    K.promised_update_card: "a promised update is due tomorrow",
    K.promised_update_due: "a promised update is due today",
}


def _mins(flow, req: dict) -> int:
    created = parse_ts(req["created_at"]) or flow.clock.now()
    return int(elapsed(created, flow.clock.now()).total_seconds() // 60)


async def on_timer(flow, item: dict) -> None:
    kind, rid = TimerKind(item["kind"]), item.get("request_id")
    if rid is None:
        await _global(flow, kind)
        return
    req = flow.db.get_request(rid)
    if not req or State(req["state"]) in TERMINAL:
        return
    if kind == K.burst_close:
        await flow.router.close_burst(rid)
        return
    await _request_timer(flow, kind, rid, req)
    tm.ensure_timer(flow.db, flow.policy, rid, flow.clock.now())


async def _request_timer(flow, kind: TimerKind, rid: str, req: dict) -> None:
    owed = not req["first_reply_at"]
    quiet = flow.policy.is_quiet_time(flow.clock.now())
    title = f"{rid} ({req['requester_name'] or 'requester'}): {req['title']}"
    if kind == K.reminder and str(req.get("channel")) == str(Channel.email):
        # 2026-11-11: no "waited 13 h" reminders at night for mail: at most ONE, inside working hours
        if not owed or not flow.policy.is_working_time(flow.clock.now()) or flow.db.events(rid, "email_reminder"):
            return
        flow.db.log_event("email_reminder", rid, actor="timers")
        flow.card(CardKind.reminder, f"{title}\nThis email is still waiting for your reply.", rid,
                  buttons=[[Button("Take over", "to"), Button("Close", "ck")]])
        return
    if kind == K.reminder and owed:
        flow.card(CardKind.reminder, f"{title}\nNo reply yet after {_mins(flow, req)} min."
                  + (" (quiet hours)" if quiet else ""), rid,
                  buttons=[[Button("Take over", "to"), Button("Snooze 30 min", "sz")]])
    elif kind in (K.holding, K.email_holding) and owed and not (req["taken_over"] and req["no_holding"])             and not tm.holding_done(flow.db, rid):
        send_holding(flow, rid, req)
    elif kind == K.verified_answer and owed:
        _verified_answer(flow, rid)
    elif kind == K.takeover_backup or (kind == K.escalate_backup and owed):   # answered = no "no answer" card
        why = "taken over 3 h ago, still open" if kind == K.takeover_backup else "no answer from Principal"
        flow.card(CardKind.escalation, f"{title}\nPlease look: {why}. Waiting {_mins(flow, req)} min.", rid,
                  to="backup", buttons=[[Button("Take over", "to")]])
    elif kind == K.paused_recard:
        n = sum(1 for r in flow.db.open_requests() if not r["first_reply_at"])
        flow.card(CardKind.reminder, f"Agent is stopped (/stop). {n} requesters have no reply yet. /resume to restart.")
    elif kind == K.worked_chaser_2 and req["state"] == State.PROOF_SENT:
        closing.on_worked_chaser_2(flow, rid, req)
    elif kind == K.answered_close:
        closing.on_answered_close(flow, rid)
    elif kind == K.sla_warning:
        # Only while the requester still waits for Principal: after his own answer the ball is with the requester.
        if not closing.human_answered(flow.db, req):
            flow.card(CardKind.reminder, f"{title}\n75% of the promised time has passed", rid)
    elif kind == K.weekly_recheck and closing.human_answered(flow.db, req):
        # Principal answered and the requester never confirmed: no card per request. They are listed together in the
        # weekly scorecard ("worked?" check), so nothing is dropped and Principal gets one card, not one per request.
        flow.db.log_event("weekly_answered_open", rid, actor="timers")
    elif kind == K.weekly_recheck:
        flow.card(CardKind.reminder, f"{title}\nWeekly re-check: close or keep?", rid,
                  buttons=[[Button("Close", "ck"), Button("Keep", "kp")]])
    elif kind in CARD_ONLY:
        flow.card(CardKind.reminder, f"{title}\n{CARD_ONLY[kind]}", rid)
    else:
        flow.db.log_event("timer", rid, actor="timers", timer_kind=str(kind))


def holding_text(flow, req: dict) -> str | None:
    """The fixed hard-floor template (persona.py). None for email (L12 holding is drafted, not sent, today)."""
    if req["channel"] not in (Channel.telegram_group, Channel.telegram_dm):
        return None
    thread = flow.db.thread(req["id"])
    has_shot = any(json.loads(m["media_json"] or "[]") for m in thread)
    from .router import extract_refs
    has_ref = any(extract_refs(m["text"] or "") for m in thread)
    try:
        from . import persona
        marker = (flow.cfg.get("persona") or {}).get("marker") or persona.MARKER
        return persona.holding_telegram(in_hours=flow.policy.is_working_time(flow.clock.now()),
                                        has_screenshot=has_shot, has_ref=has_ref, marker=marker)
    except Exception:                                    # noqa: BLE001 - no template = no send, card instead
        return None


def send_holding(flow, rid: str, req: dict) -> None:
    if req["channel"] == Channel.email:
        # Mail has no holding reply (read-only mailbox): Principal gets the analysis card instead, not a
        # "please answer by hand" card per email (2026-11-10).
        flow.db.log_event("holding_skipped", rid, actor="listener", channel="email")
        return
    from .config import chat_flag
    if not chat_flag(flow.cfg, req.get("chat_id"), "holding", True):
        # chats[].holding: false (Acme Team, 2026-11-11): agents never message people there; Principal gets the
        # card and answers himself or by his Approve.
        flow.db.log_event("holding_skipped", rid, actor="listener", channel=str(req["channel"]), why="chat")
        return
    text = holding_text(flow, req)
    if not text:
        flow.card(CardKind.holding_blocked, f"{rid}: holding reply due but no template for {req['channel']}. "
                  "Please answer by hand.", rid)
        flow.card(CardKind.holding_blocked, f"{rid}: holding reply could not go out; Principal was carded.", rid,
                  to="backup")
        return
    msg = OutboundMessage(request_id=rid, step="holding", kind=OutKind.holding, channel=Channel(req["channel"]),
                          text=text, chat_id=req["chat_id"], topic_id=req["topic_id"], reply_to=req["root_msg_id"],
                          is_template=True, draft_made_at=flow.clock.now())
    flow.bus.publish(Topics.SEND_REQUEST, {"msg": msg, "trigger": SendTrigger.holding_floor})


def _verified_answer(flow, rid: str) -> None:
    """Autopilot answer only if the latest packet is verified in code; the sender checks the dial and guards."""
    row = flow.db.latest_packet(rid)
    if not row or not row["verified"]:
        return
    msg = flow.outbound_for(rid, step=f"draft-{row['run_no']}")
    if msg:
        flow.bus.publish(Topics.SEND_REQUEST, {"msg": msg, "trigger": SendTrigger.timeout})


# ------------------------------------------------------------ global timers

def ensure_global_timers(flow) -> None:
    pending = {t["kind"] for t in flow.db.pending_timers(global_only=True)}
    now = flow.clock.now()
    for kind in GLOBAL_KINDS:
        if kind.value not in pending:
            flow.db.add_timer(None, kind.value, flow.policy.due_at(kind, now))


async def _global(flow, kind: TimerKind) -> None:
    now = flow.clock.now()
    try:
        if kind in (K.sweep_light, K.sweep_full):
            await sweep(flow, full=kind == K.sweep_full)
        elif kind == K.deadman_ping:
            await flow.deadman.ping()
        elif kind == K.digest:
            flow.card(CardKind.digest, digest_text(flow))
        elif kind == K.scorecard:
            flow.card(CardKind.digest, scorecard_text(flow))
    finally:
        flow.db.add_timer(None, kind.value, flow.policy.due_at(kind, now))


def scorecard_text(flow) -> str:
    """Weekly scorecard + the weekly "worked?" check for requests Principal answered that never got a confirmation."""
    reqs = flow.db.open_requests()
    answered = [r for r in reqs if closing.human_answered(flow.db, r)]
    lines = [f"Weekly scorecard: {len(reqs)} open requests."]
    if answered:
        lines.append(f"Weekly 'worked?' check: {len(answered)} you answered, never confirmed by the requester. "
                     "They close on their own on 'تمام' / 'ok thanks' / 👍; close any other with /close REQ-x.")
        lines += [f"- {r['id']}: {r['title']}" for r in answered[:25]]
        if len(answered) > 25:
            lines.append(f"... and {len(answered) - 25} more (see the board)")
    return "\n".join(lines)


def digest_text(flow) -> str:
    from datetime import timedelta
    from .models import AUTO_CLOSED
    reqs = flow.db.open_requests()
    owed = [r for r in reqs if not r["first_reply_at"]]
    since = utc(flow.clock.now()) - timedelta(hours=24)
    closed = [r for r in flow.db.requests_in(AUTO_CLOSED)
              if r["closed_at"] and utc(parse_ts(r["closed_at"])) >= since]
    lines = [f"Morning digest: {len(reqs)} open, {len(owed)} with no reply yet, {len(closed)} closed by the "
             "closing rules in the last 24 h."]
    lines += [f"- {r['id']} {r['state']}: {r['title']}" for r in reqs[:15]]
    lines += [f"- closed {r['id']} {r['state']}: {r['title']}" for r in closed[:10]]
    return "\n".join(lines)


async def sweep(flow, *, full: bool = False) -> int:
    """Re-read allowed chats since the last seen id (min_id; never a fixed 200) and Gmail since the cursor."""
    found = 0
    tg = flow.adapters.tg_in
    for chat in flow.cfg.get("chats") or []:
        if not chat.get("chat_id") or chat.get("read", True) is False or tg is None:
            continue
        topics = [t.get("id") for t in chat.get("topics") or [] if t.get("id")] or [None]
        for topic in topics:
            key = f"tg:{chat['chat_id']}:{topic or 0}"
            raw_cursor = flow.db.get_cursor(key)
            if raw_cursor is None:
                # First time we ever see this chat: baseline to 'now' and open NOTHING, so the twin starts from now
                # instead of re-opening months of already-answered threads (issue #45). After this the cursor
                # exists, so normal and `full` sweeps catch up from it; there is no case where a never-tracked chat
                # should flood its whole backlog (an explicit backfill is a deliberate, separate action).
                try:
                    flow.db.set_cursor(key, str(await tg.latest_id(int(chat["chat_id"]), topic)))
                except Exception as e:                   # noqa: BLE001
                    flow.bus.publish(Topics.HEALTH, {"component": "sweep", "ok": False, "detail": type(e).__name__})
                continue
            min_id = int(raw_cursor or 0)
            try:
                events = await tg.fetch_since(int(chat["chat_id"]), topic, min_id, 200)
            except Exception as e:                       # noqa: BLE001 - one bad chat must not stop the sweep
                flow.bus.publish(Topics.HEALTH, {"component": "sweep", "ok": False, "detail": type(e).__name__})
                continue
            for ev in events:
                if not flow.db.get_message(ev.event_id):
                    ev.found_by_sweep = True
                    found += 1
                await flow.on_event_in(ev)
    for name, box in (flow.cfg.get("mailboxes") or {}).items():
        if flow.adapters.gmail_in is None or not box.get("address") or box.get("enabled") is False:
            continue
        try:
            # In a thread with a limit: a slow Gmail call must not freeze Telegram and timers (2026-11-10).
            events, cursor = await asyncio.wait_for(
                asyncio.to_thread(flow.adapters.gmail_in.list_new, name, flow.db.get_cursor(f"gmail:{name}")),
                timeout=180)
        except Exception as e:                           # noqa: BLE001
            # The 2-minute poller owns Gmail health (it reports after 3 failures in a row); here only log, and
            # drop the client so the next read reconnects. The cursor did not move: nothing is lost.
            reset = getattr(flow.adapters.gmail_in, "reset", None)
            if callable(reset):
                reset(name)
            flow.db.log_event("sweep_gmail_failed", actor="timers", mailbox=name, error=type(e).__name__)
            continue
        for ev in events:
            await flow.on_event_in(ev)
        flow.db.set_cursor(f"gmail:{name}", cursor)
    check_stalled(flow)
    flow.db.log_event("sweep", actor="timers", full=full, found=found)
    return found


def check_stalled(flow) -> list[str]:
    """Open requests with no timer (or a timer > 10 min late): repair, and alarm unless boot catch-up mutes it."""
    now = flow.clock.now()
    late = flow.cfg.get("timers", {}).get("stalled_after_min", 10)
    stalled = tm.find_stalled(flow.db, now, late)
    muted = flow.mute_stalled_until is not None and utc(now) < utc(flow.mute_stalled_until)
    for rid in stalled:
        tm.ensure_timer(flow.db, flow.policy, rid, now)
        flow.db.log_event("stalled", rid, actor="timers", muted=muted)
        if not muted:
            flow.card(CardKind.stalled, f"{rid} was stalled (no timer, or a late one). Repaired.", rid)
    return stalled


def boot_catchup(flow, last_beat) -> int:
    """After sleep or power-off: rebase overdue timers (1 per 20 s, oldest first), mute STALLED, one summary card."""
    now = flow.clock.now()
    t = flow.cfg.get("timers", {})
    if last_beat is None or elapsed(last_beat, now) < timedelta(seconds=3 * int(t.get("heartbeat_seconds", 60))):
        return 0
    stagger = int(t.get("catchup_stagger_seconds", 20))
    n = tm.rebase_overdue(flow.db, now, stagger)
    flow.mute_stalled_until = tm.add_minutes(now, (n * stagger) / 60 + 2)
    gap = int(elapsed(last_beat, now).total_seconds() // 60)
    flow.catchup_note = f"laptop was off {gap // 60} h {gap % 60} min"
    owed = sum(1 for r in flow.db.open_requests() if not r["first_reply_at"])
    flow.card(CardKind.info, f"Back online: the {flow.catchup_note}. {n} timers caught up, sent 1 per {stagger} s, "
              f"oldest first. {owed} requesters have no reply yet.")
    flow.db.log_event("catchup", actor="listener", gap_min=gap, rebased=n, since=utc_iso(last_beat))
    return n


def sweep_stale_sends(flow) -> int:
    """A send stuck at 'pending' is a crash between claim_send and finish_send: the key is burnt (so the holding
    timer counts it done and never fires) but we do not know if it reached the client. At boot, nothing is in
    flight yet, so every 'pending' row is stale. Mark it 'maybe_sent' (never auto-resend: double-send risk) and
    card Principal to check. Returns how many were swept."""
    stale = flow.db.sends_since(closing.EPOCH, statuses=("pending",))
    for s in stale:
        flow.db.finish_send(s["send_key"], SendStatus.maybe_sent,
                            error="was 'pending' at boot (crashed mid-send?); not auto-resent - check the client")
    if stale:
        flow.db.log_event("stale_sends_swept", actor="listener", n=len(stale))
        flow.card(CardKind.info, f"{len(stale)} reply(ies) were mid-send when the agent last stopped. I did NOT "
                  "resend them (a double-send risk). Please check whether they reached the requester.")
    return len(stale)
