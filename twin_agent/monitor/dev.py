"""Principal's dev work (he now develops and pushes code): read-only, from the push-trace outputs.

Source: `support-automation/scripts/globex_push_trace.py` (run by Sysadmin, read-only git + `gh api` GETs) writes
`research/globexfinal-gh/rows.json` (one row per commit: branches, PRs, manifest version set, tickets, revert /
hotfix flags) and `pr_details.json` (per PR: base, head, merged_at, merged_by, reviews, commit_shas). This module
only READS those files (`monitor.dev.trace_dir`); it never runs git or gh itself and never pushes. Optional
`builds.json` ([{branch, sha, status, at}]) adds odoo.sh build status where someone exported it; without it the
report says "not visible".

Dev records are aggregated by week; alerts: a direct push to main, an unreviewed PR merged, a PR merged to
production without the gate + live-push evidence, a failed build on his branch, a manifest version bump, a push
without a ticket. Names never leave this module: rows are matched on `monitor.dev.author_match` and emitted as
"principal"; other people become roles ("gate", "live_push", "other").
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAIRO_STD = timezone(timedelta(hours=2))      # the push trace stores Cairo wall time (its own approximation)
STAGING = {"Stagelatest", "Dispatch-5"}


def _cfg(cfg: dict) -> dict:
    return ((cfg or {}).get("monitor") or {}).get("dev") or {}


def _iso(cairo_text: str) -> str:
    try:
        return datetime.strptime(cairo_text[:16], "%Y-%m-%d %H:%M").replace(tzinfo=CAIRO_STD) \
            .astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def _role(login: str, cfg: dict) -> str:
    d = _cfg(cfg)
    login = str(login or "").lower()
    if login in {x.lower() for x in d.get("github_logins") or []}:
        return "principal"
    if login in {x.lower() for x in d.get("gate_logins") or []}:
        return "gate"
    if login in {x.lower() for x in d.get("live_logins") or []}:
        return "live_push"
    return "other"


def load(trace_dir: Path, cfg: dict) -> list[dict]:
    """Dev records for the monitor store (keys are stable, so re-loading adds nothing twice)."""
    trace_dir = Path(trace_dir)
    match = str(_cfg(cfg).get("author_match") or "principal").lower()
    rows = json.loads((trace_dir / "rows.json").read_text(encoding="utf-8"))
    prs = json.loads((trace_dir / "pr_details.json").read_text(encoding="utf-8")) \
        if (trace_dir / "pr_details.json").exists() else {}
    mine = {r["sha"]: r for r in rows if match in str(r.get("author") or "").lower()}
    out: list[dict] = []
    for sha, r in mine.items():
        out.append({"key": f"dev:commit:{sha}", "kind": "commit", "ts": _iso(r.get("authored_cairo")), "sha": sha,
                    "branches": sorted({*filter(None, str(r.get("first_parent_of") or "").split(";")),
                                        *filter(None, str(r.get("other_live_branches") or "").split(";"))}),
                    "first_parent_of": [b for b in str(r.get("first_parent_of") or "").split(";") if b],
                    "in_main": r.get("in_main") == "Y", "is_merge": r.get("is_merge") == "Y",
                    "is_revert": r.get("is_revert") == "Y", "is_hotfix": r.get("is_hotfix") == "Y",
                    "prs": [int(x) for x in str(r.get("prs") or "").split(";") if x.isdigit()],
                    "modules": [m for m in str(r.get("modules") or "").split(";") if m],
                    "version_bump": bool(r.get("manifest_version_set")),
                    "tickets": [t for t in str(r.get("tickets") or "").split(";") if t]})
    others = [r for r in rows if r["sha"] not in mine and (r.get("is_revert") == "Y" or r.get("is_hotfix") == "Y")]
    for r in others:                                       # reverts / hotfixes by anyone: matched to his modules
        out.append({"key": f"dev:fixup:{r['sha']}", "kind": "fixup", "ts": _iso(r.get("authored_cairo")),
                    "sha": r["sha"], "modules": [m for m in str(r.get("modules") or "").split(";") if m],
                    "is_revert": r.get("is_revert") == "Y"})
    for n, p in prs.items():
        shas = [str(s)[:10] for s in p.get("commit_shas") or []]
        if _role(p.get("user"), cfg) != "principal" and not any(s in mine for s in shas):
            continue
        roles = sorted({_role((rv or {}).get("user") if isinstance(rv, dict) else rv, cfg)
                        for rv in p.get("reviews") or []})
        out.append({"key": f"dev:pr:{n}", "kind": "pr", "ts": p.get("created_at"), "number": int(n),
                    "base": p.get("base"), "head": p.get("head"), "merged_at": p.get("merged_at") or None,
                    "merged_by": _role(p.get("merged_by"), cfg) if p.get("merged_by") else None,
                    "reviewed": bool(p.get("reviews")), "review_roles": roles, "commits": shas})
    bf = trace_dir / "builds.json"
    if bf.exists():
        branches = {b for c in out if c["kind"] == "commit" for b in c["branches"]}
        for b in json.loads(bf.read_text(encoding="utf-8")):
            if b.get("branch") in branches:
                out.append({"key": f"dev:build:{b.get('branch')}:{b.get('sha')}", "kind": "build", "ts": b.get("at"),
                            "branch": b.get("branch"), "status": b.get("status")})
    return out


def dev_alerts(rows: list[dict]) -> list[dict]:
    out = []

    def add(sev, kind, r, text):
        out.append({"key": f"{kind}:{r['key']}", "ts": r.get("ts"), "severity": sev, "kind": kind, "request_id": None,
                    "evidence": [r["key"]], "text": f"[{sev.upper()}] dev: {text}"})
    for r in rows:
        if r["kind"] == "commit":
            if "main" in r["first_parent_of"] and not r["is_merge"] and not r["prs"]:
                add("high", "direct_push_main", r, f"direct push to main (commit {r['sha'][:7]})")
            if r["version_bump"]:
                add("high", "version_bump", r, f"manifest version bump in {r['sha'][:7]}")
            if not r["tickets"] and not r["is_merge"]:
                add("medium", "push_no_ticket", r, f"push without a ticket ({r['sha'][:7]})")
        elif r["kind"] == "pr" and r.get("merged_at"):
            if not r["reviewed"]:
                add("high", "unreviewed_merge", r, f"PR #{r['number']} merged into {r['base']} with no review")
            if (r["base"] == "main" or str(r["head"]).endswith("-live")) and not (
                    "gate" in r["review_roles"] and (r.get("merged_by") == "live_push" or "live_push" in r["review_roles"])):
                add("critical", "prod_write_unapproved", r, f"PR #{r['number']} reached production without the gate "
                    "review + live-push approval on record")
        elif r["kind"] == "build" and str(r.get("status")).lower() in ("failed", "failure", "error", "red"):
            add("high", "build_failed", r, f"build failed on {r['branch']}")
    return out


def _median(xs):
    return round(statistics.median(xs), 1) if xs else None


def dev_kpis(rows: list[dict], since: str, until: str) -> dict:
    """Dev KPIs for [since, until) (UTC ISO). Evidence = commit shas / PR numbers."""
    def inside(r):
        return since <= (r.get("ts") or "") < until
    commits = [r for r in rows if r["kind"] == "commit" and inside(r)]
    pushes = [c for c in commits if not c["is_merge"]]
    prs = [r for r in rows if r["kind"] == "pr" and inside(r)]
    merged = [p for p in prs if p.get("merged_at")]
    by_sha = {}
    for p in (r for r in rows if r["kind"] == "pr" and r.get("merged_at")):
        for s in p["commits"]:
            by_sha.setdefault(s, []).append(p)
    lead = []
    for c in pushes:
        stg = [p["merged_at"] for p in by_sha.get(c["sha"], []) if p["base"] in STAGING]
        live = [p["merged_at"] for p in by_sha.get(c["sha"], []) if p["base"] == "main"]
        if stg and live:
            d = (datetime.fromisoformat(min(live).replace("Z", "+00:00"))
                 - datetime.fromisoformat(min(stg).replace("Z", "+00:00"))).total_seconds() / 3600
            if d >= 0:
                lead.append(d)
    mods = {m for c in pushes for m in c["modules"]}
    fixups = [r for r in rows if r["kind"] == "fixup" and inside(r) and set(r["modules"]) & mods]
    builds = [r for r in rows if r["kind"] == "build" and inside(r)]
    to_prod = [p for p in merged if p["base"] == "main" or str(p["head"]).endswith("-live")]
    gated = [p for p in to_prod if "gate" in p["review_roles"] and
             (p.get("merged_by") == "live_push" or "live_push" in p["review_roles"])]
    return {
        "pushes": len(pushes), "push_ids": [c["sha"][:7] for c in pushes],
        "branches": sorted({b for c in pushes for b in c["branches"]}),
        "prs_opened": len(prs), "prs_merged": len(merged), "prs_reviewed": sum(1 for p in prs if p["reviewed"]),
        "pr_ids": [p["number"] for p in prs],
        "version_bumps": [c["sha"][:7] for c in pushes if c["version_bump"]],
        "no_ticket": [c["sha"][:7] for c in pushes if not c["tickets"]],
        "tickets": sorted({t for c in pushes for t in c["tickets"]}),
        "reverts_hotfixes_after": [f["sha"][:7] for f in fixups],
        "staging_to_live_h_median": _median(lead),
        "to_production": len(to_prod), "to_production_gated": len(gated),
        "builds": {"seen": len(builds), "failed": sum(1 for b in builds if str(b.get("status")).lower()
                                                      in ("failed", "failure", "error", "red"))} if builds else None,
    }
