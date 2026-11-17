"""Wiring for Digital Twin's write desks (helpdesk tickets, daily timesheets). Both are off by default.

- `optional_helpdesk(flow)`: `helpdesk.enabled` -> a HelpdeskDesk on the bus. Live: the real Odoo adapter (Principal's key
  from the vault; a failure is a health event and the desk stays off, never a fake). dry_run: FakeOdoo, writes off.
- `timesheet_tick(flow)`: once a day at `timesheets.card_hour` (Cairo), compute yesterday-to-now's entries and post
  the timesheet card. Needs consent D4 + `timesheets.enabled`; nothing while Principal has /paused the Watcher.
"""
from __future__ import annotations

from .bus import Topics


def _odoo(flow, section: str):
    from .helpdesk.odoo import OdooAdapter, OdooUnavailable, open_odoo
    if flow.cfg.get("run_mode") != "live":
        from .testing.fake_odoo import FakeOdoo
        return OdooAdapter(FakeOdoo(), writes_on=False)
    try:
        return open_odoo(flow.cfg, section=section)
    except (OdooUnavailable, Exception) as e:              # noqa: BLE001 - off, with a health card
        flow.bus.publish(Topics.HEALTH, {"component": f"odoo:{section}", "ok": False, "detail": type(e).__name__})
        return None


def optional_helpdesk(flow):
    if (flow.cfg.get("helpdesk") or {}).get("enabled") is not True:
        return None
    from .helpdesk.desk import HelpdeskDesk
    odoo = _odoo(flow, "helpdesk")
    if odoo is None:
        return None
    desk = HelpdeskDesk(flow.db, flow.cfg, flow.bus, flow.clock, odoo)
    desk.wire()
    return desk


def optional_timesheets(flow):
    if (flow.cfg.get("timesheets") or {}).get("enabled") is not True:
        return None
    from .timesheets.desk import TimesheetDesk
    odoo = _odoo(flow, "timesheets")
    desk = TimesheetDesk(flow.db, flow.cfg, flow.bus, flow.clock, odoo,
                         ledger_of=getattr(getattr(flow, "helpdesk", None), "ledger", None))
    from .browser import history
    if history.enabled(flow.cfg):                          # work-site visits from the Digital Twin profile
        desk.visits = lambda since, until: history.visits(flow.cfg, since, until)
    desk.wire()
    return desk


async def timesheet_tick(flow) -> None:
    desk = getattr(flow, "timesheets", None)
    if desk is not None:
        desk.maybe_daily_card()
