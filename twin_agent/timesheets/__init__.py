"""Automatic descriptive timesheets from Principal's REAL active time (BUILD 2, 2026-11-03).

Pipeline (every step is a pure function over `Act` records, so tests run on synthetic transcripts):
1. `transcripts.iter_acts`  streams ~/.claude/projects/**/*.jsonl line by line (never loads a file whole);
   `activity.db_acts` adds his card taps / instructions / commands; `activity.history_acts` his work-site visits.
2. `intervals.session_intervals`  splits each session into active intervals. Active time is ANCHORED on Principal's own
   actions (a prompt, a tap, a visit): assistant / tool activity extends an interval only while the session is not
   idle (gap <= idle_gap, default 10 min) and only up to `max_unattended` (10 min) after his last own action. A
   long autonomous run is not Principal's time.
3. `intervals.allocate`  builds one union timeline over all sessions; an overlapping stretch is counted ONCE and
   given to the foreground session (the one with the latest own action of Principal's at that moment).
4. `attribute.attribute`  ticket per interval: an explicit ticket ref in Principal's own actions, a REQ ref (-> its
   ticket), the session's earlier ref, the files touched (`path_map`). The git branch and ticket numbers seen only
   in tool activity never bill time. Unattributed time is shown on the card and never written.
5. `entries.build_entries`  per Cairo day and ticket: minutes -> hours rounded to 0.25 h, with a descriptive name
   ("#2636 diagnosed: ...; traced to x.py:16; pushed branch y; result: ...") made from that time's actions.
6. `desk.TimesheetDesk`  the daily card [Approve all] [Edit] [Reject]; only approved entries become
   account.analytic.line on the ticket for Principal's employee (fields discovered at runtime). Autopilot is off.
"""
