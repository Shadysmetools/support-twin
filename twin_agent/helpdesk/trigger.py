"""The twin's REAL trigger: watch Odoo for helpdesk tickets assigned to Principal in the New stage, fire once each.

The readiness review found the gap: nothing watched Odoo for "Supervisor assigns a ticket to Principal in New" — the
only live intakes were Telegram and Gmail. This closes it. It is READ-ONLY (search_read through the guarded
adapter), so it is safe under OBSERVE: it detects and hands off, it never writes.

Usage (the listener calls this on an interval):
    trig = AssignmentTrigger(adapter, principal_uid=adapter.uid, new_stage_id=cfg_new_stage_id)
    fired = trig.drain(lambda ticket: flow.intake_odoo_ticket(ticket))   # hand each fresh ticket to the flow
"""
from __future__ import annotations

from typing import Any, Callable

# Fields the twin needs to open a ticket; all readable on helpdesk.ticket (guard allows the read).
FIELDS = ["id", "name", "stage_id", "user_id", "partner_id", "x_track", "x_type", "x_type_code", "create_date"]


class AssignmentTrigger:
    """Polls for tickets assigned to Principal in the New stage and yields each exactly once (deduped by id)."""

    def __init__(self, adapter, *, principal_uid: int, new_stage_id: int, seen=None):
        self.adapter = adapter
        self.principal_uid = int(principal_uid)
        self.new_stage_id = int(new_stage_id)
        self.seen: set[int] = {int(x) for x in (seen or ())}

    def domain(self) -> list:
        # assigned to Principal (user_id) AND sitting in the New stage
        return [["user_id", "=", self.principal_uid], ["stage_id", "=", self.new_stage_id]]

    def poll(self) -> list[dict]:
        """Tickets newly assigned to Principal in New since the last poll, oldest first, each returned only once."""
        rows = self.adapter.search_read("helpdesk.ticket", self.domain(), FIELDS, order="create_date asc")
        fresh: list[dict] = []
        for r in rows:
            tid = int(r.get("id") or 0)
            if tid and tid not in self.seen:
                self.seen.add(tid)
                fresh.append(r)
        return fresh

    def drain(self, on_ticket: Callable[[dict], Any]) -> int:
        """Poll and hand each fresh ticket to on_ticket (the flow intake). Returns how many fired."""
        fresh = self.poll()
        for t in fresh:
            on_ticket(t)
        return len(fresh)
