"""Timesheet report: hours per ticket, per client, billable vs not (from the local ledger of decided entries).

Counts entries that were written, prepared (observe) or approved. Client and billable come from the ticket as the
writer resolved it (the ticket's partner; the team project's `allow_billable`). 02h 0 #5: today every support hour
on project 107 is non-billable, so "billable" will usually read 0 until Acme changes that in Odoo.
"""
from __future__ import annotations

from collections import defaultdict

COUNTED = ("written", "prepared", "approved")


def summarise(rows: list[dict]) -> dict:
    per_ticket: dict = defaultdict(float)
    per_client: dict = defaultdict(float)
    bill = {"billable": 0.0, "non_billable": 0.0, "unknown": 0.0}
    for r in rows:
        if r.get("status") not in COUNTED:
            continue
        h = float(r.get("hours") or 0)
        per_ticket[f"#{str(r['key'])[2:]}"] += h
        per_client[r.get("client") or "not resolved yet"] += h
        b = r.get("billable")
        bill["unknown" if b is None else "billable" if b else "non_billable"] += h
    return {"tickets": dict(sorted(per_ticket.items(), key=lambda kv: -kv[1])),
            "clients": dict(sorted(per_client.items(), key=lambda kv: -kv[1])),
            "billable": bill, "total": round(sum(per_ticket.values()), 2)}


def render(s: dict, *, title: str = "Timesheet report", label: str = "") -> str:
    out = [f"{title}{' - ' + label if label else ''}", f"Total: {s['total']:.2f} h", "", "Per ticket"]
    out += [f"- {k}: {v:.2f} h" for k, v in s["tickets"].items()] or ["- none"]
    out += ["", "Per client"] + ([f"- {k}: {v:.2f} h" for k, v in s["clients"].items()] or ["- none"])
    b = s["billable"]
    out += ["", f"Billable: {b['billable']:.2f} h | non-billable: {b['non_billable']:.2f} h | not resolved: "
                f"{b['unknown']:.2f} h"]
    return "\n".join(out)


def report(store, *, since: str | None = None, until: str | None = None, label: str = "") -> str:
    rows = [r for r in store.entries() if (since is None or r["day"] >= since) and (until is None or r["day"] <= until)]
    return render(summarise(rows), label=label)
