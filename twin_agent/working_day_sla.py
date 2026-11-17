"""Working-day SLA / auto-close - the counting Odoo's built-in automation cannot do.

WHY THIS EXISTS
    Odoo's team auto-close counts CALENDAR days. The Acme helpdesk runs on WORKING days
    (Sunday to Thursday; Friday and Saturday never count). TICKET-BLUEPRINT.md section 5, worked
    example #2639: it went Live on Wed 21 Oct and Odoo closed it Thu 29 Oct (~7.5 calendar days);
    with 7 WORKING days it should have closed on Sun 1 Nov. This module does that counting itself.

THE RULES (TICKET-BLUEPRINT.md section 5)
    - Live (Deployment)        -> Closed       after 7 working days (no reminders).
    - Customer Feedback        -> No Response   after 5 working days, with 2 reminders first.
    - Client Testing (Review)  -> No Response   after 10 working days, with 2 reminders first.
    - Working days = Sunday..Thursday. Fri/Sat never count. Dates are Africa/Cairo dates.

OBSERVE / PREPARE-ONLY
    scan() returns a list of PREPARED actions (what is due). It holds no Odoo handle and performs
    no side effect: moving a stage, sending a reminder email and closing a ticket are the live
    runner's job, after Supervisor's rules. scan() CRUCIALLY flags - and blocks - any terminal action
    (auto_close / no_response) or client reminder when the last client inbound is still unanswered:
    telling a client "awaiting your response" or closing for "no response" is wrong if they replied.

Everything here is pure: same inputs, same output, no clock unless you omit `today`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .clock import CAIRO

# Python weekday(): Mon=0 .. Sun=6. Working week is Sun..Thu, so Fri(4) and Sat(5) are the only non-working days.
_NON_WORKING = {4, 5}

# --- SLA table (TICKET-BLUEPRINT.md section 5) ----------------------------------------------------------------
# reminders_working_days: the two reminder offsets are NOT pinned in the blueprint - these are a sensible default
# (spread across the window, the last one before the deadline) and are "(Supervisor to confirm)". Edit here, not in code
# scattered elsewhere, when she confirms the cadence.
@dataclass(frozen=True)
class StageSLA:
    label: str
    deadline_working_days: int
    reminders_working_days: tuple[int, ...]
    terminal_action: str                 # "auto_close" | "no_response"


_STAGES: dict[str, StageSLA] = {
    "live": StageSLA("Live (Deployment)", 7, (), "auto_close"),
    "customer feedback": StageSLA("Customer Feedback", 5, (2, 4), "no_response"),
    "client testing": StageSLA("Client Testing (Client Review)", 10, (4, 7), "no_response"),
}

# Reminder cadence is a default pending Supervisor's confirmation (see StageSLA above).
REMINDERS_ARE_PROVISIONAL = True


# --- working-day arithmetic -----------------------------------------------------------------------------------

def is_working_day(d: date) -> bool:
    """True for Sunday..Thursday; False for Friday and Saturday."""
    return d.weekday() not in _NON_WORKING


def add_working_days(start: date, n: int) -> date:
    """`start` plus `n` working days. The start day itself is day 0 (not counted); counting resumes on the next
    working day. So a Friday/Saturday start + 1 lands on the following Sunday, and #2639 (Wed 21 Oct) + 7 = Sun
    1 Nov. `n == 0` returns `start` unchanged. Negative `n` walks backwards over working days."""
    start = _as_date(start)
    if n == 0:
        return start
    step = 1 if n > 0 else -1
    remaining = abs(n)
    d = start
    while remaining:
        d += timedelta(days=step)
        if is_working_day(d):
            remaining -= 1
    return d


def working_days_between(a: date, b: date) -> int:
    """Count of working days in the half-open interval (a, b] - the inverse of add_working_days: for any n >= 0,
    working_days_between(a, add_working_days(a, n)) == n. Returns 0 when b <= a."""
    a, b = _as_date(a), _as_date(b)
    if b <= a:
        return 0
    count = 0
    d = a
    while d < b:
        d += timedelta(days=1)
        if is_working_day(d):
            count += 1
    return count


# --- date normalisation (Odoo stores naive UTC) ---------------------------------------------------------------

def to_cairo_date(value) -> date:
    """The Africa/Cairo calendar date of `value`. A naive datetime is read as UTC (Odoo's convention) then shifted
    to Cairo, so a late-UTC Wednesday is already Thursday here. A date passes through; an ISO string is parsed."""
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return dt.astimezone(CAIRO).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return to_cairo_date(datetime.fromisoformat(s)) if ("T" in s or " " in s) else date.fromisoformat(s)
        except ValueError as exc:
            raise ValueError(f"cannot read a date from {value!r}") from exc
    raise TypeError(f"cannot read a date from {type(value).__name__}")


def _as_date(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else to_cairo_date(value)


# --- the SLA for one stage ------------------------------------------------------------------------------------

def _normalise_stage(stage: str) -> StageSLA | None:
    s = (stage or "").strip().lower()
    # drop a trailing parenthetical like "(Deployment)" / "(Client Review)"
    core = s.split("(")[0].strip()
    for key, sla in _STAGES.items():
        if s == key or core == key or key in s:
            return sla
    if "deployment" in s:
        return _STAGES["live"]
    if "client review" in s or "testing" in s:
        return _STAGES["client testing"]
    return None


def due(stage: str, entered_date) -> dict:
    """The deadline and reminder dates for a ticket sitting in `stage` since `entered_date`.

    Returns a plain dict (JSON-friendly):
      {stage, entered_date, deadline, deadline_working_days, reminders[], reminder_working_days[],
       terminal_action, note}
    A stage with no working-day SLA (New, Functional, internal stages...) returns deadline=None,
    reminders=[], terminal_action=None.
    """
    entered = to_cairo_date(entered_date)
    sla = _normalise_stage(stage)
    if sla is None:
        return {"stage": stage, "entered_date": entered, "deadline": None, "deadline_working_days": None,
                "reminders": [], "reminder_working_days": [], "terminal_action": None,
                "note": "no working-day SLA for this stage"}
    reminders = [add_working_days(entered, wd) for wd in sla.reminders_working_days]
    return {
        "stage": sla.label,
        "entered_date": entered,
        "deadline": add_working_days(entered, sla.deadline_working_days),
        "deadline_working_days": sla.deadline_working_days,
        "reminders": reminders,
        "reminder_working_days": list(sla.reminders_working_days),
        "terminal_action": sla.terminal_action,
        "note": ("reminder cadence is a default pending Supervisor's confirmation"
                 if reminders else "no reminders for this stage"),
    }


# --- the scan (prepared actions, never executed) --------------------------------------------------------------

# Actions that reach the client (an email) or close the ticket. Each is wrong if the client has an unanswered
# message, so each is blocked when last_inbound_unanswered is True.
_CLIENT_FACING = {"reminder", "no_response", "auto_close"}


def scan(tickets, *, today=None, clock=None) -> list[dict]:
    """For each ticket, return the single latest SLA milestone that has come due as of `today` (a prepared action),
    or nothing if none is due yet. OBSERVE: this returns the due list; it executes nothing.

    tickets: iterable of {id, stage, entered_date, last_inbound_unanswered?}.
    today:   a date / datetime / ISO string (Cairo date). Omit to read the system clock.

    Each returned action: {id, stage, action, due_date, reminder_index, blocked, block_reason, to_verify}.
      - action is "reminder", "no_response" or "auto_close".
      - blocked=True (with a block_reason) when the action is client-facing AND last_inbound_unanswered is True.
      - last_inbound_unanswered missing -> not blocked, but a to_verify note asks the runner to confirm it first.
    scan is stateless: it reports the latest milestone passed and cannot know whether reminder #1 already went out;
    de-duplicating against what was already sent is the live runner's job.
    """
    if today is None:
        now = (clock.now() if clock is not None else datetime.now(CAIRO))
        today = now.date() if isinstance(now, datetime) else now
    today = to_cairo_date(today)

    out: list[dict] = []
    for t in tickets or []:
        d = due(t.get("stage", ""), t.get("entered_date"))
        milestones: list[tuple[str, int | None, date]] = [
            ("reminder", i + 1, rd) for i, rd in enumerate(d["reminders"])]
        if d["terminal_action"] and d["deadline"] is not None:
            milestones.append(("terminal", None, d["deadline"]))
        passed = [m for m in milestones if m[2] <= today]
        if not passed:
            continue
        kind, idx, when = passed[-1]
        action = d["terminal_action"] if kind == "terminal" else "reminder"

        blocked = False
        reason = None
        to_verify: list[str] = []
        unanswered = t.get("last_inbound_unanswered")
        if action in _CLIENT_FACING:
            if unanswered is True:
                blocked = True
                reason = (f"last client inbound is unanswered; '{action}' would be wrong until Supervisor "
                          "answers the client")
            elif unanswered is None:
                to_verify.append("last_inbound_unanswered not provided; confirm the client has no unanswered "
                                 "message before this action goes out")
        out.append({
            "id": t.get("id"),
            "stage": d["stage"],
            "action": action,
            "due_date": when,
            "reminder_index": idx,
            "blocked": blocked,
            "block_reason": reason,
            "to_verify": to_verify,
        })
    return out
