"""Write APPROVED timesheet entries as account.analytic.line, guarded, with a Monitor write event per line.

- Only rows with status `approved` and a matching approval (the card hash Principal tapped, or autopilot) are written;
  anything else is refused here, whatever the caller says.
- Fields are discovered at runtime (`fields_get`): name, unit_amount, date, employee_id (Principal's, by his user),
  helpdesk_ticket_id, project_id (the ticket team's project), task_id (the ticket's first x_task_ids link).
- A ticket key "T:<n>" is resolved by `ticket_ref`, "U:<id>" by record id. Unresolved = blocked, never guessed.
- OBSERVE / writes off: status `prepared`, nothing reaches Odoo. A call that fails mid-way leaves `maybe_written`
  (never retried automatically: Principal checks Odoo first, so nothing is logged twice).
"""
from __future__ import annotations

from .. import paths
from ..helpdesk import guard
from ..helpdesk.conventions import m2o
from ..helpdesk.ledger import log_write


class TimesheetWriter:
    def __init__(self, db, store, odoo, *, write_on: bool):
        self.db, self.store, self.odoo, self.write_on = db, store, odoo, write_on
        self._fields: set[str] | None = None
        self._employee: int | None = None

    def fields(self) -> set[str]:
        if self._fields is None:
            self._fields = set(self.odoo.fields_get("account.analytic.line")) if self.odoo is not None else set()
        return self._fields

    def employee(self) -> int:
        if self._employee is None:
            rows = self.odoo.search_read("hr.employee", [["user_id", "=", self.odoo.uid]], ["name"], limit=1) \
                if self.odoo is not None else []
            self._employee = int(rows[0]["id"]) if rows else 0
        return self._employee

    def ticket(self, key: str) -> dict | None:
        if self.odoo is None or not key or key[:2] not in ("T:", "U:"):
            return None
        f = ["name", "team_id", "partner_id", "x_task_ids"]
        if key.startswith("T:"):
            rows = self.odoo.search_read("helpdesk.ticket", [["ticket_ref", "=", key[2:]]], f, limit=1)
        else:
            rows = self.odoo.read("helpdesk.ticket", [int(key[2:])], f)
        if not rows:
            return None
        t = rows[0]
        team = m2o(t.get("team_id"))
        proj = None
        billable = False
        if team:
            tm = self.odoo.read("helpdesk.team", [int(team)], ["project_id"])
            proj = m2o(tm[0].get("project_id")) if tm else None
        if proj:
            pr = self.odoo.read("project.project", [int(proj)], ["allow_billable"])
            billable = bool(pr and pr[0].get("allow_billable"))
        partner = t.get("partner_id")
        client = partner[1] if isinstance(partner, (list, tuple)) and len(partner) > 1 else None
        if client is None and m2o(partner):
            p = self.odoo.read("res.partner", [int(m2o(partner))], ["name"])
            client = p[0].get("name") if p else None
        tasks = t.get("x_task_ids") or []
        return {"id": int(t["id"]), "project_id": proj, "task_id": tasks[0] if tasks else None,
                "client": client or "no client", "billable": billable}

    def vals(self, row: dict, t: dict) -> dict:
        f = self.fields()
        v = {"name": row["name"], "unit_amount": float(row["hours"]), "date": row["day"], "employee_id": self.employee()}
        for k, val in (("helpdesk_ticket_id", t["id"]), ("project_id", t["project_id"]), ("task_id", t["task_id"])):
            if val and k in f:
                v[k] = int(val)
        return v

    def write(self, rows: list[dict], approvals: list) -> dict:
        out = {"written": 0, "prepared": 0, "blocked": 0}
        for row in rows:
            status, reason, line_id, fields = self._one(row, approvals)
            out[{"done": "written"}.get(status, status)] += 1
            log_write(self.db, None, op="create", model="account.analytic.line", status=status, approvals=approvals,
                      purpose="timesheet", record_id=line_id, fields=fields, reason=reason,
                      uid=getattr(self.odoo, "uid", None))
        return out

    def _one(self, row: dict, approvals: list):
        if row.get("status") != "approved" or not approvals:
            return "blocked", "not approved", None, None
        try:
            stopped = bool(self.db.kill_on()) or paths.stop_file().exists()
        except Exception:                                   # noqa: BLE001
            stopped = True
        if stopped:
            return "blocked", "kill switch is on", None, None
        try:
            t = self.ticket(row["key"])
        except Exception as e:                              # noqa: BLE001
            t, why = None, type(e).__name__
        else:
            why = "ticket not found"
        if t is None:
            self.store.set(row["id"], status="blocked")
            return "blocked", why, None, None
        vals = self.vals(row, t)
        self.store.set(row["id"], client=t["client"], billable=int(t["billable"]))
        try:
            guard.check("timesheet", "account.analytic.line", "create", [vals])
        except guard.WriteRefused as e:
            self.store.set(row["id"], status="blocked")
            return "blocked", str(e), None, list(vals)
        if not self.write_on:
            self.store.set(row["id"], status="prepared")
            return "prepared", "", None, list(vals)
        self.store.set(row["id"], status="writing")
        try:
            line = int(self.odoo.call("timesheet", "account.analytic.line", "create", [vals]))
        except Exception as e:                              # noqa: BLE001 - maybe written: never retried blindly
            self.store.set(row["id"], status="maybe_written")
            return "blocked", f"maybe written: {type(e).__name__}", None, list(vals)
        self.store.set(row["id"], status="written", line_id=line)
        return "done", "", line, list(vals)
