"""Field conventions of helpdesk.example.com, discovered at runtime and copied from how Supervisor fills her tickets.

Nothing here is hard-coded truth: 02h is the expectation, the live database is the check.
- `fields_get` on helpdesk.ticket and account.analytic.line: which fields exist (x_track, x_type, x_ai_confidence,
  x_task_ids, helpdesk_ticket_id ...). A field that does not exist is never written. We never create fields.
- Stages: the safe set = `helpdesk.safe_stage_ids` (02h section 2: 12, 14, 6, 18, 16, 9) INTERSECTED with "has no
  mail template at runtime". If the stage read fails, nothing is safe (fail closed). Stage 9 starts the auto-close
  clock (cron 66, 7 days), so a move into it is flagged.
- Supervisor's profile: the most common team, priority, x_track, x_type and x_ai_confidence on her recent tickets.
- Principal: his uid (the transport's login) and his employee (hr.employee.user_id).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

DOC_SAFE_STAGES = (12, 14, 6, 18, 16, 9)        # 02h section 2.2 "Silent stages"
AUTO_CLOSE_STAGE = 9                             # Live: cron 66 closes after 7 days and emails "Solved"
TICKET_WANTED = ("x_track", "x_type", "x_ai_confidence", "x_task_ids", "tag_ids", "team_id", "priority",
                 "partner_id", "user_id", "stage_id")


def m2o(v):
    """Odoo many2one value -> id ([id, name] from RPC, an int from the fake, False when empty)."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v or None


@dataclass
class Conventions:
    ticket_fields: set[str] = field(default_factory=set)
    line_fields: set[str] = field(default_factory=set)
    track_values: list[str] = field(default_factory=list)
    stages: dict[int, dict] = field(default_factory=dict)       # id -> {name, template_id, emails_client}
    safe_stages: set[int] = field(default_factory=set)
    teams: dict[int, dict] = field(default_factory=dict)        # id -> {name, project_id}
    tags: dict[str, int] = field(default_factory=dict)          # lower name -> id
    types: dict[str, int] = field(default_factory=dict)         # lower name -> x_ticket_type id
    supervisor: dict = field(default_factory=dict)                   # uid + modes of her tickets
    principal_uid: int = 0
    principal_employee_id: int = 0
    problems: list[str] = field(default_factory=list)

    def emails_client(self, stage_id: int) -> bool:
        st = self.stages.get(int(stage_id))
        return True if st is None else bool(st.get("emails_client"))   # unknown stage = assume it emails

    def has(self, f: str) -> bool:
        return f in self.ticket_fields


def _safe(call, problems: list[str], what: str, default):
    try:
        return call()
    except Exception as e:                                     # noqa: BLE001 - discovery never crashes the desk
        problems.append(f"{what}: {type(e).__name__}")
        return default


def discover(odoo, cfg: dict | None = None) -> Conventions:
    hd = (cfg or {}).get("helpdesk") or {}
    c = Conventions()
    p = c.problems
    tf = _safe(lambda: odoo.fields_get("helpdesk.ticket"), p, "ticket fields", {})
    c.ticket_fields = set(tf)
    c.track_values = [s[0] for s in (tf.get("x_track") or {}).get("selection") or []]
    c.line_fields = set(_safe(lambda: odoo.fields_get("account.analytic.line"), p, "timesheet fields", {}))
    for f in TICKET_WANTED:
        if c.ticket_fields and f not in c.ticket_fields:
            p.append(f"helpdesk.ticket has no field {f}: it will not be written")
    stages = _safe(lambda: odoo.search_read("helpdesk.stage", [], ["name", "template_id", "sequence"]), p,
                   "stages", None)
    wanted = {int(s) for s in hd.get("safe_stage_ids") or DOC_SAFE_STAGES}
    if stages is None:
        c.safe_stages = set()                                  # fail closed: no stage move at all
    else:
        for s in stages:
            sid = int(s["id"])
            # Stage 9 (Live) has no immediate mail template, but cron 66 auto-closes it after 7 days and emails
            # the client "Solved". Treat it as client-emailing so it leaves the safe set and goes to Supervisor's gate.
            emails = bool(m2o(s.get("template_id"))) or sid == AUTO_CLOSE_STAGE
            c.stages[sid] = {"name": s.get("name"), "template_id": m2o(s.get("template_id")),
                             "emails_client": emails}
        c.safe_stages = {sid for sid in wanted if sid in c.stages and not c.stages[sid]["emails_client"]}
        for sid in sorted(wanted - c.safe_stages):
            p.append(f"stage {sid} is not confirmed silent at runtime: moves into it go to Supervisor")
    for t in _safe(lambda: odoo.search_read("helpdesk.team", [["active", "=", True]], ["name", "project_id"]), p,
                   "teams", []):
        c.teams[int(t["id"])] = {"name": t.get("name"), "project_id": m2o(t.get("project_id"))}
    c.tags = {str(t.get("name") or "").lower(): int(t["id"]) for t in
              _safe(lambda: odoo.search_read("helpdesk.tag", [], ["name"]), p, "tags", [])}
    if c.has("x_type"):
        c.types = {str(t.get("x_name") or "").lower(): int(t["id"]) for t in
                   _safe(lambda: odoo.search_read("x_ticket_type", [], ["x_name"]), p, "ticket types", [])}
    c.principal_uid = odoo.uid
    emp = _safe(lambda: odoo.search_read("hr.employee", [["user_id", "=", odoo.uid]], ["name"], limit=1), p,
                "employee", [])
    c.principal_employee_id = int(emp[0]["id"]) if emp else 0
    c.supervisor = supervisor_profile(odoo, c, hd)
    return c


def supervisor_profile(odoo, c: Conventions, hd: dict) -> dict:
    """How Supervisor fills her tickets: the most common value of each convention field on her recent tickets."""
    uid = int(hd.get("supervisor_user_id") or 0)
    if not uid:
        name = str(hd.get("supervisor_name") or "Supervisor")
        users = _safe(lambda: odoo.search_read("res.users", [["name", "ilike", name]], ["name"], limit=1),
                      c.problems, "Supervisor's user", [])
        uid = int(users[0]["id"]) if users else 0
    if not uid:
        return {}
    fields = [f for f in ("team_id", "priority", "x_track", "x_type", "x_ai_confidence", "tag_ids") if c.has(f)]
    rows = _safe(lambda: odoo.search_read("helpdesk.ticket", [["create_uid", "=", uid]], fields, limit=60,
                                          order="id desc"), c.problems, "Supervisor's tickets", [])
    prof: dict = {"uid": uid, "n": len(rows)}
    for f in fields:
        vals = [m2o(r.get(f)) if f in ("team_id", "x_type") else r.get(f) for r in rows]
        if f == "tag_ids":
            prof["tagged_share"] = round(sum(1 for v in vals if v) / len(vals), 2) if vals else 0
            continue
        vals = [v for v in vals if v not in (None, False, "")]
        if vals:
            prof[f] = Counter(vals).most_common(1)[0][0]
    return prof
