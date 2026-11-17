"""FakeOdoo: an in-memory helpdesk.example.com shaped like 02h (synthetic names and ids only). No network.

Implements the transport protocol `execute_kw(model, method, args, kwargs)` + `uid` for OdooAdapter.
Every call is kept in `calls`; every create / write / message_post in `writes`. `fail_on` makes one
(model, method) raise, e.g. to test "maybe created".
"""
from __future__ import annotations

import re
import copy
from typing import Any

PRINCIPAL_UID, SUPERVISOR_UID, PRINCIPAL_EMPLOYEE = 777, 778, 901
STAGE_TEMPLATES = {1: 70, 12: False, 2: 178, 14: False, 5: 180, 6: False, 7: 181, 18: False, 11: 161, 16: False,
                   17: 186, 9: False, 3: 71, 13: 170, 4: 171}
STAGE_NAMES = {1: "New", 12: "On Hold", 2: "Functional", 14: "Pending Internal Review", 5: "Customer Feedback",
               6: "Development Requested", 7: "Development Submitted (Backlog)", 18: "Internal Development",
               11: "Acme / Client Testing", 16: "Pending Internal Testing",
               17: "Development Complete - No Response Received", 9: "Live (Deployment)", 3: "Closed",
               13: "No Response Received", 4: "Cancelled"}
TICKET_FIELD_TYPES = {"name": "char", "description": "html", "team_id": "many2one", "stage_id": "many2one",
                      "tag_ids": "many2many", "partner_id": "many2one", "priority": "selection",
                      "user_id": "many2one", "x_track": "selection", "x_type": "many2one", "x_type_code": "char",
                      "x_ai_confidence": "float", "x_task_ids": "one2many", "create_date": "datetime",
                      "write_date": "datetime", "x_studio_dev_ticket_id": "char",
                      "ticket_ref": "char"}
LINE_FIELD_TYPES = {"name": "char", "unit_amount": "float", "date": "date", "employee_id": "many2one",
                    "helpdesk_ticket_id": "many2one", "project_id": "many2one", "task_id": "many2one",
                    "x_studio_ticket": "char"}


def _seed() -> dict[str, dict[int, dict]]:
    tickets = {}
    for i, (track, typ, conf, prio, tags) in enumerate([("functional", 4, 0.6, "3", []),
                                                         ("development", 1, 0.6, "3", [31]),
                                                         ("functional", 5, 0.7, "0", []),
                                                         ("functional", 4, 0.6, "3", [])]):
        tid = 9001 + i
        tickets[tid] = {"id": tid, "name": f"Sample Ticket {i + 1}", "description": "<p>Request</p>", "team_id": 2,
                        "stage_id": 2, "tag_ids": tags, "partner_id": 3001, "priority": prio, "user_id": SUPERVISOR_UID,
                        "x_track": track, "x_type": typ, "x_type_code": str(typ), "x_ai_confidence": conf,
                        "x_task_ids": [], "create_uid": SUPERVISOR_UID, "ticket_ref": str(2600 + i)}
    return {
        "helpdesk.stage": {sid: {"id": sid, "name": n, "template_id": STAGE_TEMPLATES[sid], "sequence": i}
                           for i, (sid, n) in enumerate(STAGE_NAMES.items())},
        "helpdesk.team": {2: {"id": 2, "name": "Helpdesk Support (Sample)", "project_id": 501, "active": True},
                          11: {"id": 11, "name": "Customer Inquiries (Sample)", "project_id": 501, "active": True}},
        "helpdesk.tag": {31: {"id": 31, "name": "Partner Dev"}, 32: {"id": 32, "name": "Client did not respond yet"}},
        "x_ticket_type": {1: {"id": 1, "x_name": "Bug"}, 2: {"id": 2, "x_name": "New Request"},
                          3: {"id": 3, "x_name": "Customization"}, 4: {"id": 4, "x_name": "Configuration"},
                          5: {"id": 5, "x_name": "Inquiries"}},
        "res.users": {PRINCIPAL_UID: {"id": PRINCIPAL_UID, "name": "Principal Sample", "login": "principal@example.org"},
                      SUPERVISOR_UID: {"id": SUPERVISOR_UID, "name": "Supervisor Sample", "login": "supervisor@example.org"}},
        "res.partner": {3001: {"id": 3001, "name": "Client Co A (SAMPLE)", "is_company": True,
                               "email": "client-a@example.org"},
                        3002: {"id": 3002, "name": "Client Co B (SAMPLE)", "is_company": True,
                               "email": "client-b@example.org"}},
        "hr.employee": {PRINCIPAL_EMPLOYEE: {"id": PRINCIPAL_EMPLOYEE, "name": "Principal Sample", "user_id": PRINCIPAL_UID}},
        "project.project": {501: {"id": 501, "name": "Helpdesk Support (Sample)", "allow_billable": False}},
        "project.task": {},
        "helpdesk.ticket": tickets,
        "mail.message": {},
        "account.analytic.line": {},
        "mail.template": {t: {"id": t, "name": f"Template {t}"} for t in STAGE_TEMPLATES.values() if t},
    }


def _match(rec: dict, domain: list) -> bool:
    for term in domain or []:
        if not isinstance(term, (list, tuple)):
            continue                                       # '&' / '|' prefixes: the fake treats all as AND
        f, op, v = term
        x = rec.get(f, True if f == "active" else None)
        x = x[0] if isinstance(x, (list, tuple)) and op in ("=", "!=") and len(x) == 2 and f.endswith("_id") else x
        if op == "=" and x != v or op == "!=" and x == v:
            return False
        if op == "in" and (x not in v if not isinstance(x, list) else not set(x) & set(v)):
            return False
        if op == "not in" and x in v:
            return False
        if op == "ilike" and str(v).lower() not in str(x or "").lower():
            return False
        if op == "=ilike" and not re.fullmatch("".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in str(v)),
                                                str(x or ""), re.IGNORECASE | re.DOTALL):
            return False
        if op in (">=", "<=") and not (x is not None and (x >= v if op == ">=" else x <= v)):
            return False
    return True


class FakeOdoo:
    def __init__(self, *, uid: int = PRINCIPAL_UID, data: dict | None = None):
        self.uid = uid
        self.data = data or _seed()
        self.calls: list[tuple[str, str]] = []
        self.writes: list[dict] = []
        self.fail_on: set[tuple[str, str]] = set()
        self._next = 20000

    def _id(self) -> int:
        self._next += 1
        return self._next

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict) -> Any:
        self.calls.append((model, method))
        if (model, method) in self.fail_on:
            raise TimeoutError(f"fake timeout on {model}.{method}")
        table = self.data.setdefault(model, {})
        if method == "fields_get":
            types = TICKET_FIELD_TYPES if model == "helpdesk.ticket" else \
                LINE_FIELD_TYPES if model == "account.analytic.line" else {"name": "char"}
            out = {k: {"type": t, "string": k} for k, t in types.items()}
            if model == "helpdesk.ticket":
                out["x_track"]["selection"] = [["functional", "Functional"], ["development", "Development"]]
            return out
        if method in ("search_read", "search", "search_count"):
            rows = [r for r in table.values() if _match(r, args[0] if args else [])]
            if kwargs.get("order", "").endswith("desc"):
                rows = sorted(rows, key=lambda r: r["id"], reverse=True)
            if kwargs.get("limit"):
                rows = rows[: kwargs["limit"]]
            if method == "search_count":
                return len(rows)
            if method == "search":
                return [r["id"] for r in rows]
            return [self._pick(r, kwargs.get("fields")) for r in rows]
        if method == "read":
            return [self._pick(table[i], kwargs.get("fields")) for i in args[0] if i in table]
        if method == "create":
            vals = args[0][0] if isinstance(args[0], list) else args[0]
            rid = self._id()
            table[rid] = {"id": rid, **self._apply({}, vals), "create_uid": self.uid}
            self.writes.append({"model": model, "method": "create", "id": rid, "vals": copy.deepcopy(vals)})
            return rid
        if method == "write":
            for i in args[0]:
                table[i] = self._apply(table[i], args[1])
            self.writes.append({"model": model, "method": "write", "ids": list(args[0]), "vals": copy.deepcopy(args[1])})
            return True
        if method == "message_post":
            mid = self._id()
            self.data["mail.message"][mid] = {"id": mid, "model": model, "res_id": args[0][0],
                                              "body": kwargs.get("body"), "subtype": kwargs.get("subtype_xmlid"),
                                              "author_uid": self.uid}
            self.writes.append({"model": model, "method": "message_post", "ids": list(args[0]), "kwargs": dict(kwargs)})
            return mid
        raise NotImplementedError(f"FakeOdoo: {model}.{method}")

    @staticmethod
    def _apply(rec: dict, vals: dict) -> dict:
        out = dict(rec)
        for k, v in vals.items():
            if isinstance(v, list) and v and isinstance(v[0], (list, tuple)):     # x2many commands
                cur = list(out.get(k) or [])
                for c in v:
                    if c[0] == 4 and c[1] not in cur:
                        cur.append(c[1])
                    elif c[0] == 6:
                        cur = list(c[2])
                out[k] = cur
            else:
                out[k] = v
        return out

    @staticmethod
    def _pick(rec: dict, fields: list | None) -> dict:
        if not fields:
            return dict(rec)
        return {"id": rec["id"], **{f: rec.get(f, True if f == "active" else False) for f in fields}}

    def real_writes(self) -> int:
        return len(self.writes)
