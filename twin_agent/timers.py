"""Timer policy (Cairo working hours) and the timer table (CONTRACT section 6, 05 2.3).

Two kinds of time maths, on purpose:
- "N minutes later" is real elapsed time: we add it in UTC, so Egypt's summer-time change never shifts it.
- "next working day 10:15" is wall-clock time in Cairo.

Invariant: every open request has at least one unfired, uncancelled timer (schedule_for_state guarantees it).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from .bus import Bus, Topics
from .clock import CAIRO, elapsed, utc
from .models import TERMINAL, Channel, State, TimerKind, parse_ts

K = TimerKind
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Kinds that fire at most once per request. holding is special: "once" means "sent", see holding_done.
ONCE_KINDS = {K.email_holding, K.reminder, K.escalate_backup, K.sla_warning}


def _hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def add_minutes(dt: datetime, minutes: float) -> datetime:
    """Real elapsed minutes (UTC maths), returned in Cairo."""
    return (dt.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(CAIRO)


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=CAIRO)


class TimerPolicy:
    def __init__(self, cfg: dict, holidays: set[date] | None = None, away_until: datetime | None = None):
        """cfg may be the whole config or just its 'timers' section."""
        self.t = cfg.get("timers", cfg) if isinstance(cfg, dict) else {}
        if holidays is None and "timers" in cfg:
            from .config import holidays as cfg_holidays
            holidays = cfg_holidays(cfg)
        self.holidays = set(holidays or ())
        self.away_until = away_until
        self.days = {_DAYS.index(d[:3].lower()) for d in self.t.get("working_days", ["sun", "mon", "tue", "wed", "thu"])}
        wh = self.t.get("working_hours", {"start": "10:00", "end": "18:00"})
        qh = self.t.get("quiet_hours", {"start": "22:00", "end": "08:00"})
        self.work_start, self.work_end = _hhmm(wh["start"]), _hhmm(wh["end"])
        self.quiet_start, self.quiet_end = _hhmm(qh["start"]), _hhmm(qh["end"])

    # ---------------------------------------------------------------- calendar

    def _cfg(self, *keys, default=None):
        node = self.t
        for k in keys:
            node = node.get(k) if isinstance(node, dict) else None
        return default if node is None else node

    def is_working_day(self, d: date) -> bool:
        return d.weekday() in self.days and d not in self.holidays

    def is_working_time(self, dt: datetime) -> bool:
        dt = dt.astimezone(CAIRO)
        if self.away_until and utc(dt) < utc(self.away_until):
            return False                       # /away 3h: treat as out of hours
        return self.is_working_day(dt.date()) and self.work_start <= dt.time() < self.work_end

    def is_quiet_time(self, dt: datetime) -> bool:
        t = dt.astimezone(CAIRO).time()
        if self.quiet_start > self.quiet_end:          # wraps midnight (22:00-08:00)
            return t >= self.quiet_start or t < self.quiet_end
        return self.quiet_start <= t < self.quiet_end

    def next_working_at(self, dt: datetime, hhmm: str) -> datetime:
        """The first hh:mm on a working day that is at or after dt (and after any /away window)."""
        dt = dt.astimezone(CAIRO)
        if self.away_until and utc(dt) < utc(self.away_until):
            dt = self.away_until.astimezone(CAIRO)
        t, d = _hhmm(hhmm), dt.date()
        for _ in range(60):
            cand = _at(d, t)
            if self.is_working_day(d) and utc(cand) >= utc(dt):
                return cand
            d += timedelta(days=1)
        raise ValueError("no working day in the next 60 days: check working_days and holidays")

    def add_working_hours(self, dt: datetime, hours: float) -> datetime:
        """Move forward by working time only (Sun to Thu 10:00 to 18:00, minus holidays)."""
        left = timedelta(hours=hours)
        cur = dt.astimezone(CAIRO)
        for _ in range(400):
            if not self.is_working_time(cur):
                cur = self.next_working_at(cur, self.work_start.strftime("%H:%M"))
                if cur.time() >= self.work_end:           # defensive; cannot happen with sane hours
                    cur = add_minutes(cur, 1)
                    continue
            end = _at(cur.date(), self.work_end)
            room = elapsed(cur, end)
            if left <= room:
                return add_minutes(cur, left.total_seconds() / 60)
            left -= room
            cur = end
        raise ValueError("add_working_hours ran too long")

    def add_working_days(self, dt: datetime, days: int) -> datetime:
        """Same wall-clock time, `days` working days later (or earlier if negative)."""
        dt = dt.astimezone(CAIRO)
        d, step, n = dt.date(), (1 if days >= 0 else -1), abs(days)
        while n:
            d += timedelta(days=step)
            if self.is_working_day(d):
                n -= 1
        return _at(d, dt.time().replace(tzinfo=None))

    def _next_weekly(self, start: datetime, spec: str) -> datetime:
        """'sun 10:30' -> the next such moment after start."""
        day, hhmm = spec.split()
        wd, t = _DAYS.index(day[:3].lower()), _hhmm(hhmm)
        d = start.astimezone(CAIRO).date()
        for i in range(8):
            cand = _at(d + timedelta(days=i), t)
            if cand.weekday() == wd and utc(cand) > utc(start):
                return cand
        raise ValueError(spec)

    def _next_daily(self, start: datetime, hhmm: str) -> datetime:
        cand = _at(start.astimezone(CAIRO).date(), _hhmm(hhmm))
        return cand if utc(cand) > utc(start) else _at(cand.date() + timedelta(days=1), _hhmm(hhmm))

    # ---------------------------------------------------------------- the table (05 2.3)

    def due_at(self, kind: TimerKind, start: datetime, *, fully_blocked: bool = False,
               channel: str | None = None) -> datetime:
        kind, c = TimerKind(kind), self._cfg
        inh = self.is_working_time(start)

        def io(key: str, d_in: int, d_out: int) -> datetime:
            return add_minutes(start, c(key, "in_hours_min", default=d_in) if inh
                               else c(key, "out_of_hours_min", default=d_out))

        if kind == K.burst_close:
            return add_minutes(start, c("burst", "quiet_min", default=3))
        if kind == K.worker_hard_stop:
            return add_minutes(start, c("worker", "hard_stop_min", default=15))
        if kind == K.reminder:
            return io("reminder", 40, 10)
        if kind == K.holding:
            return io("holding", 60, 15)
        if kind == K.verified_answer:
            return io("verified_answer", 60, 30)
        if kind == K.email_holding:
            return io("email_holding", 240, 120)
        if kind == K.escalate_backup:
            if fully_blocked:
                return add_minutes(start, c("escalate_backup", "fully_blocked_min", default=30))
            if inh:
                return add_minutes(start, c("escalate_backup", "in_hours_min", default=180))
            return self.next_working_at(start, c("escalate_backup", "out_of_hours_next_working_at", default="10:15"))
        if kind == K.takeover_backup:
            return add_minutes(start, c("takeover_backup_min", default=180))
        if kind in (K.evidence_chase_1, K.evidence_chase_2):
            key = "first_min" if kind == K.evidence_chase_1 else "second_min"
            return add_minutes(start, c("evidence_chase", key, default=1440 if key == "first_min" else 4320))
        if kind in (K.approval_reping_1, K.approval_reping_2):
            mins = c("approval_card", "reping_min", default=[30, 120])[0 if kind == K.approval_reping_1 else 1]
            if inh:
                return add_minutes(start, mins)
            return self.next_working_at(start, c("approval_card", "out_of_hours_reping_at", default="10:15"))
        if kind == K.approval_expire:
            return add_minutes(start, c("approval_card", "expire_min", default=1440))
        if kind == K.supervisor_review:
            return self.add_working_hours(start, c("supervisor_review_working_hours", default=4))
        if kind in (K.worked_chaser_1, K.worked_chaser_2):
            at = c("worked_chaser", "out_of_hours_at", default="10:30")
            if kind == K.worked_chaser_1:
                return add_minutes(start, c("worked_chaser", "first_min", default=120)) if inh \
                    else self.next_working_at(start, at)
            cand = add_minutes(start, c("worked_chaser", "second_min", default=1440))
            return cand if self.is_working_time(cand) else self.next_working_at(cand, at)
        if kind == K.ticket_readback_1:
            return add_minutes(start, c("ticket_readback", "first_min", default=15))
        if kind == K.ticket_readback_2:
            return add_minutes(start, c("ticket_readback", "second_min", default=30))
        if kind == K.ticket_idle:
            return self.add_working_days(start, c("ticket_idle_working_days", default=3))
        if kind == K.client_question:
            return add_minutes(start, 60 * c("client_question", "hours", default=24))
        if kind == K.client_testing_due:
            return self.add_working_days(start, c("client_testing_proof_working_days", default=1))
        if kind == K.sla_warning:
            frac = c("sla", "card_at_fraction", default=0.75)
            hours = (24 * c("sla", "email_days", default=7)) if channel in (Channel.email, "email") \
                else c("sla", "telegram_hours", default=48)
            return add_minutes(start, 60 * hours * frac)
        if kind == K.paused_recard:
            return add_minutes(start, c("paused_recard_min", default=120))
        if kind == K.answered_close:             # start = Principal's own answer; requester silent since
            return self.add_working_hours(start, c("answered_close", "working_hours", default=48))
        if kind == K.weekly_recheck:
            return self._next_weekly(start, c("sweep", "weekly_recheck", default="sun 10:30"))
        if kind == K.promised_update_card:       # start = the promised date/time
            return self.add_working_days(start, -int(c("promised_update", "card_working_days_before", default=1)))
        if kind == K.promised_update_due:
            return start
        if kind == K.sweep_light:
            return add_minutes(start, c("sweep", "light_min", default=15))
        if kind == K.sweep_full:
            return add_minutes(start, c("sweep", "full_min", default=120))
        if kind == K.digest:
            return self._next_daily(start, c("sweep", "digest_at", default="09:30"))
        if kind == K.scorecard:
            return self._next_weekly(start, c("sweep", "scorecard", default="sun 10:00"))
        if kind == K.deadman_ping:
            return add_minutes(start, c("deadman_ping_min", default=5))
        raise ValueError(f"no rule for timer kind {kind}")


# ---------------------------------------------------------------- table operations

def schedule(db, request_id: str, kind: TimerKind, due_at: datetime, payload: dict | None = None) -> int:
    return db.add_timer(request_id, TimerKind(kind).value, due_at, payload)


def cancel(db, request_id: str, kinds: Iterable[TimerKind]) -> int:
    return db.cancel_timers(request_id, [TimerKind(k).value for k in kinds])


HOLDING_DONE = ("sent", "maybe_sent", "would_send")   # a bare 'pending' row is a send still in flight (or crashed
# mid-send): it is NOT done. boot's sweep_stale_sends turns a crashed 'pending' into 'maybe_sent', which IS done.


def holding_done(db, request_id: str) -> bool:
    """The hard floor is done only when the holding reply went out (or was recorded in observe).
    A holding send that a guard blocked (human-active lock, rate, kill switch) is retried: it never dies."""
    row = db.get_send(f"{request_id}:holding") if hasattr(db, "get_send") else None
    return bool(row and row["status"] in HOLDING_DONE)


def _already(db, request_id: str, kind: TimerKind, pending: set[str]) -> bool:
    """True if this kind is pending, or is a once-only kind that is already done."""
    if kind.value in pending:
        return True
    if kind == K.holding:
        return holding_done(db, request_id)
    return kind in ONCE_KINDS and any(t["fired_at"] for t in db.timers_of(request_id, kind.value))


_CONVERSATION = (State.TRIAGED, State.AWAITING_FIRST_REPLY, State.ACKED, State.DIAGNOSED, State.REOPENED,
                 State.FAILED_WORKER, State.STALLED, State.ESCALATED)


def _wanted(req: dict, state: State) -> list[TimerKind]:
    """Which timers a request in `state` should have (before the fallback)."""
    email = req.get("channel") in (Channel.email, Channel.ticket_mail)
    owed = not req.get("first_reply_at")            # nobody has answered the requester yet
    floor = owed and not (req.get("taken_over") and req.get("no_holding"))
    reply = [K.reminder, K.email_holding if email else K.holding] if owed else []
    if not floor:
        reply = [k for k in reply if k not in (K.holding, K.email_holding)]
    if state == State.NEW:
        return [K.burst_close]
    if req.get("_human_answered") and state in _CONVERSATION:
        # Principal answered himself, the ball is with the requester: no reminder / SLA / escalation cards.
        # Informational answers close after 48 working h of silence; the weekly "worked?" check is the net.
        return ([K.answered_close] if req.get("_answered_close") else []) + [K.weekly_recheck]
    if state in (State.TRIAGED, State.AWAITING_FIRST_REPLY, State.ACKED, State.DIAGNOSED, State.REOPENED,
                 State.FAILED_WORKER, State.STALLED):
        return reply + [K.escalate_backup, K.sla_warning]
    if state == State.NEEDS_INFO:
        return reply + [K.evidence_chase_1, K.evidence_chase_2, K.weekly_recheck]
    if state == State.PROPOSED:
        return reply + [K.approval_reping_1, K.approval_reping_2, K.approval_expire]
    if state == State.FIX_PENDING:
        return reply + [K.sla_warning]
    if state == State.TICKETED:
        return reply + [K.ticket_readback_1, K.ticket_readback_2, K.ticket_idle]
    if state == State.VERIFYING:
        return reply + [K.client_testing_due]
    if state == State.PROOF_SENT:
        return [K.worked_chaser_1, K.worked_chaser_2]
    if state == State.FIXED_UNCONFIRMED:
        return [K.weekly_recheck]
    if state == State.TAKEN_OVER:
        return reply + [K.takeover_backup]
    if state == State.ESCALATED:
        return reply + [K.sla_warning]
    if state == State.PAUSED:
        return [K.paused_recard, K.escalate_backup]   # kill switch wins over the holding floor
    return []


# The client-facing reply clocks (60/15-min holding floor, 40/10-min reminder, 4/2-h email holding) run from the
# requester's FIRST message, not from the burst close 3+ min later (Twin Monitor finding, 2026-11-03).
FIRST_MESSAGE_KINDS = (K.reminder, K.holding, K.email_holding)


def schedule_for_state(db, policy: TimerPolicy, request_id: str, state: State, now: datetime, *,
                       reply_start: datetime | None = None) -> list[int]:
    """Add the timers a request needs in `state` (skipping ones already pending). Terminal: cancel all.

    reply_start: when given (the burst close passes the first message's time), the FIRST_MESSAGE_KINDS are
    measured from it, so the burst-merge window does not delay the holding reply. A due time already in the past
    fires on the next tick.
    Always leaves at least one pending timer on an open request (fallback: weekly_recheck).
    """
    state = State(state)
    req = db.get_request(request_id) or {}
    if state in TERMINAL:
        db.cancel_timers(request_id, None)
        return []
    if req.get("id") and hasattr(db, "events"):
        from .closing import annotate
        req = annotate(db, req)
    pending = {t["kind"] for t in db.future_timers(request_id)}
    fully_blocked = bool(req.get("fully_blocked"))
    ids = []
    for kind in _wanted(req, state):
        if _already(db, request_id, kind, pending):
            continue
        start = now
        if reply_start is not None and kind in FIRST_MESSAGE_KINDS:
            start = reply_start
        if kind == K.sla_warning:
            start = parse_ts(req.get("created_at")) or now
        if kind == K.answered_close:
            from .closing import last_answer_at
            start = last_answer_at(db, request_id) or now
        due = policy.due_at(kind, start, fully_blocked=fully_blocked, channel=req.get("channel"))
        ids.append(schedule(db, request_id, kind, due))
        pending.add(kind.value)
    if not pending:
        ids.append(schedule(db, request_id, K.weekly_recheck, policy.due_at(K.weekly_recheck, now)))
    return ids


def ensure_timer(db, policy: TimerPolicy, request_id: str, now: datetime) -> list[int]:
    """Repair helper: schedule what the request's current state needs."""
    req = db.get_request(request_id)
    if not req:
        return []
    return schedule_for_state(db, policy, request_id, State(req["state"]), now)


def tick(db, policy: TimerPolicy, now: datetime, bus: Bus) -> list[dict]:
    """Fire every due timer: mark fired, publish timer.fired. Returns what fired (oldest first)."""
    import json
    fired = []
    for t in db.due_timers(now):
        db.mark_timer(t["id"], fired=True)
        item = {"timer_id": t["id"], "request_id": t["request_id"], "kind": t["kind"],
                "payload": json.loads(t["payload_json"] or "{}")}
        bus.publish(Topics.TIMER_FIRED, item)
        fired.append(item)
    return fired


def find_stalled(db, now: datetime, missed_after_min: int = 10) -> list[str]:
    """Open requests with no pending timer, or with a timer overdue by more than missed_after_min."""
    limit = add_minutes(now, -missed_after_min)
    out = []
    for req in db.open_requests():
        timers = db.future_timers(req["id"])
        if not timers or any(utc(parse_ts(t["due_at"])) < utc(limit) for t in timers):
            out.append(req["id"])
    return out


def rebase_overdue(db, now: datetime, stagger_s: int = 20) -> int:
    """Boot catch-up: move overdue timers to now, staggered stagger_s apart, oldest first.

    Global timers (sweeps, digest, ping) are moved to now without stagger; duplicates of a global kind are
    cancelled so a long sleep does not fire the same sweep many times.
    """
    count, slot, seen_global = 0, 0, set()
    for t in db.pending_timers():
        if utc(parse_ts(t["due_at"])) > utc(now):
            continue
        if t["request_id"] is None:
            if t["kind"] in seen_global:
                db.mark_timer(t["id"], cancelled=True)
                continue
            seen_global.add(t["kind"])
            db.set_timer_due(t["id"], now)
        else:
            db.set_timer_due(t["id"], add_minutes(now, slot * stagger_s / 60))
            slot += 1
        count += 1
    return count
