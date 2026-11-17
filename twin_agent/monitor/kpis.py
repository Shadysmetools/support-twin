"""Outcome KPIs, quality, writes, autopilot share and lane readiness. Every number carries evidence ids.

Evidence format: `REQ-0042#msg-1a2b3c4d` (request id + the alias of the source message), or a record key.
"Twin first reply" counts the twin's first outgoing message for the request, including an observe-mode
"would send" (marked as such in the report), so the replay numbers say what the twin WOULD have done.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from ..clock import elapsed, shift
from ..models import parse_ts
from .alerts import approval_kinds, classify_env, judge_write, target_minutes, wait_minutes

# Historical baselines (Principal's OLD triage-only role, before he also developed and pushed code).
BASELINE = {
    "principal_first_reply_in_hours_min": 18, "principal_first_reply_out_of_hours_h": 13.7, "principal_confirmed_pct": 25,
    "principal_reopened_pct": 11, "helpdesk_first_public_reply_h": 126, "helpdesk_reopen_pct": 23,
    "helpdesk_median_close_d": 12.9,
    "source": "04-PRINCIPAL-HANDOVER-ANALYSIS.md (Telegram, Mar-Nov 2026) and 02h/04b (Odoo helpdesk)",
}
ACCEPT, EDIT, REJECT = {"ap", "as"}, {"ed", "sd"}, {"to", "tn", "dl", "nn"}
OPEN_ABANDON_DAYS = 7


def pct(n, d) -> int | None:
    return round(100 * n / d) if d else None


def perc(xs: list[float], q: float) -> float | None:
    """Nearest-rank percentile."""
    if not xs:
        return None
    s = sorted(xs)
    return round(s[max(0, math.ceil(q / 100 * len(s)) - 1)], 1)


def ev(q: dict) -> str:
    return f"{q['request_id']}#{q['msg']}" if q.get("msg") else q["request_id"]


def _in(ts, since: str, until: str) -> bool:
    t = parse_ts(ts) if isinstance(ts, str) else None
    s, u = parse_ts(since), parse_ts(until)
    return bool(t and s and u and elapsed(s, t) >= timedelta(0) and elapsed(t, u) > timedelta(0))


def _mins(a, b) -> float | None:
    a, b = parse_ts(a), parse_ts(b)
    return round(elapsed(a, b).total_seconds() / 60, 1) if a and b else None


def decisions(store, rids: set[str]) -> dict[str, str]:
    """Principal's decision per request from his taps: accepted / edited / rejected (last decisive tap wins)."""
    out = {}
    for t in store.records(kind="tap"):
        a, rid = t.get("action"), t.get("request_id")
        if rid in rids and a in ACCEPT | EDIT | REJECT:
            out[rid] = "accepted" if a in ACCEPT else "edited" if a in EDIT else "rejected"
    for s in store.records(kind="send"):
        if s.get("request_id") in rids and s.get("trigger") == "principal_edit":
            out[s["request_id"]] = "edited"
    return out


def outcome(store, since: str, until: str, now: datetime, cfg: dict | None = None) -> dict:
    reqs = [q for q in store.requests() if _in(q.get("ts"), since, until)]
    rids = {q["request_id"] for q in reqs}
    o: dict = {"received": len(reqs), "received_ev": [ev(q) for q in reqs]}
    responded, within, breaches, human, twin, twin_obs = [], [], [], [], [], 0
    for q in reqs:
        waited, still = wait_minutes(q, now)
        if not still:
            responded.append(ev(q))
            if waited is not None and waited <= target_minutes(bool(q.get("in_hours")), cfg):
                within.append(ev(q))
        if waited is not None and waited > target_minutes(bool(q.get("in_hours")), cfg) and not (still and q.get("closed_at")):
            breaches.append(ev(q))
        if q.get("first_reply_by") == "principal" and (m := _mins(q.get("ts"), q.get("first_reply_at"))) is not None:
            human.append(m)
        if (m := _mins(q.get("ts"), q.get("twin_first_at"))) is not None:
            twin.append(m)
            twin_obs += q.get("twin_first_status") == "would_send"
    o.update(responded=len(responded), responded_ev=responded, within_target=len(within), within_target_ev=within,
             within_target_pct=pct(len(within), len(reqs)), breaches=len(breaches), breaches_ev=breaches,
             human_first_min={"median": perc(human, 50), "p90": perc(human, 90), "n": len(human)},
             twin_first_min={"median": perc(twin, 50), "p90": perc(twin, 90), "n": len(twin), "observe": twin_obs})
    closed = [q for q in reqs if q.get("closed_at")]
    proven = [q for q in closed if q.get("state") == "CLOSED_PROVEN"]
    confirmed = [q for q in closed if q not in proven and str(q.get("close_reason") or "").startswith("ack_")]
    unconf = [q for q in closed if q not in proven and q not in confirmed]
    reopened = [q for q in reqs if q.get("reopened_from") or q.get("state") == "REOPENED"]
    escalated = [q for q in reqs if q.get("ticket_id") or q.get("state") == "ESCALATED"]
    old = shift(now, -timedelta(days=OPEN_ABANDON_DAYS))
    abandoned = [q for q in reqs if not q.get("closed_at") and q.get("first_reply_at") is None
                 and q.get("twin_first_at") is None and parse_ts(q.get("updated_at") or q.get("ts"))
                 and elapsed(parse_ts(q.get("updated_at") or q.get("ts")), old) > timedelta(0)]
    for name, xs in (("closed_proven", proven), ("closed_confirmed", confirmed), ("closed_unconfirmed", unconf),
                     ("reopened", reopened), ("escalated", escalated), ("abandoned", abandoned)):
        o[name], o[name + "_ev"] = len(xs), [ev(q) for q in xs]
    reacts = {r.get("request_id") for r in store.records(kind="reaction") if r.get("request_id") in rids}
    positive = {q["request_id"]: ev(q) for q in reqs if str(q.get("close_reason") or "").startswith("ack_")
                or q["request_id"] in reacts}
    negative = [ev(q) for q in reqs if (q.get("negative_signals") or 0) > 0 or (q.get("requester_msgs_after_reply") or 0) >= 3]
    o.update(positive=len(positive), positive_ev=list(positive.values()), negative=len(negative), negative_ev=negative,
             satisfaction_pct=pct(len(positive), len(closed)), closed=len(closed),
             odoo_ratings=int(((cfg or {}).get("monitor") or {}).get("odoo_ratings") or 0))
    dec = decisions(store, rids)
    c = Counter(dec.values())
    handling = [q["principal_minutes"] for q in reqs if (q.get("principal_minutes") or 0) > 0]
    avg = (round(statistics.mean(handling), 1), "measured") if handling else \
        (BASELINE["principal_first_reply_in_hours_min"], "proxy: baseline 18 min")
    o.update(accepted=c["accepted"], edited=c["edited"], rejected=c["rejected"],
             accepted_ev=[r for r, d in dec.items() if d == "accepted"],
             time_saved_min=round(c["accepted"] * avg[0], 1), handling_basis=avg[1])
    passed = {}                       # judges/checks passed = no failed check on the latest run (not `verified`,
    for k in store.records(kind="checks"):   # which is the code-recomputed "verified answer" flag)
        if k.get("request_id") in dec:
            passed[k["request_id"]] = not (k.get("failed") or [])
    agree = sum(1 for r, d in dec.items() if r in passed and passed[r] == (d == "accepted"))
    o.update(judge_agree=agree, judge_compared=sum(1 for r in dec if r in passed),
             judge_agree_pct=pct(agree, sum(1 for r in dec if r in passed)))
    return o


def writes(store, since: str, until: str) -> dict:
    ws = [w for w in store.records(cat="write") if _in(w.get("ts"), since, until)]
    by_env = Counter(classify_env(w) for w in ws if w.get("kind") == "write")
    done = [w for w in ws if w.get("kind") == "write" and str(w.get("status")) != "blocked"]
    approved = [w for w in done if not any(k in ("prod_write_unapproved", "write_without_instruction",
                                                 "out_of_scope_write", "client_email_stage_unapproved")
                                           for _, k, _t in judge_write(w))]
    blocked = [w for w in ws if w.get("kind") in ("write_blocked", "tool_denied") or str(w.get("status")) == "blocked"]
    return {"total": len(done), "by_env": dict(by_env), "approved": len(approved),
            "approved_pct": pct(len(approved), len(done)), "blocked": len(blocked),
            "ev": [w["key"] for w in ws], "instructed": sum(1 for w in done if "principal_instruction" in approval_kinds(w)),
            "tapped": sum(1 for w in done if "principal_tap" in approval_kinds(w)),
            "prepared": sum(1 for w in done if str(w.get("status")) == "prepared")}


def activity(store, since: str, until: str) -> dict:
    recs = [r for r in store.records(since=None) if _in(r.get("ts"), since, until)]
    k = Counter(r.get("kind") for r in recs)
    sends = [r for r in recs if r.get("kind") == "send"]
    out_ok = [s for s in sends if s.get("status") in ("sent", "would_send", "maybe_sent")]
    starts = [r for r in recs if r.get("kind") == "worker_run"]
    resumes = sum(1 for r in starts if (r.get("run_no") or 1) > 1)
    errors = [r for r in recs if r.get("kind") in ("error", "worker_failed")]
    stalls = {r.get("request_id") for r in recs if r.get("kind") == "stalled" or r.get("dst") == "STALLED"}
    usage = sum(r.get("claude_calls") or 0 for r in recs if r.get("kind") == "usage")
    audits = [r for r in store.records(kind="audit_verify")]
    return {"cards": k["card"], "sends": len(sends), "sends_out": len(out_ok),
            "would_send": sum(1 for s in sends if s.get("status") == "would_send"),
            "sent": sum(1 for s in sends if s.get("status") == "sent"),
            "autopilot": sum(1 for s in out_ok if s.get("mode") == "autopilot"),
            "autopilot_pct": pct(sum(1 for s in out_ok if s.get("mode") == "autopilot"), len(out_ok)),
            "blocked": sum(1 for s in sends if s.get("status") == "blocked"),
            "sessions_started": len(starts) - resumes, "sessions_resumed": resumes, "stalled": len(stalls),
            "errors": len(errors), "restarts": k["restart"], "claude_calls": usage,
            "audit_ok": all(a.get("ok") for a in audits[-2:]) if audits else None,
            "top_issues": Counter(r.get("kind") for r in errors).most_common(3)}


def lanes(store, alerts: list[dict]) -> dict[str, str]:
    """Per lane: promote / hold / demote on the dial, from Principal's decisions and safety alerts."""
    reqs = {q["request_id"]: q for q in store.requests()}
    dec = decisions(store, set(reqs))
    per = defaultdict(Counter)
    for rid, d in dec.items():
        per[reqs[rid].get("lane") or "?"][d] += 1
    unsafe = Counter(reqs.get(a.get("request_id"), {}).get("lane") for a in alerts
                     if a.get("severity") in ("critical", "high") and a.get("kind") != "timer_breach")
    out = {}
    for lane in sorted({q.get("lane") or "?" for q in reqs.values()}):
        c, n = per[lane], sum(per[lane].values())
        share = c["accepted"] / n if n else 0
        if unsafe[lane] or (n >= 5 and share < 0.5):
            out[lane] = f"demote ({unsafe[lane]} safety, {c['accepted']}/{n} accepted)"
        elif n >= 10 and share >= 0.9:
            out[lane] = f"promote ({c['accepted']}/{n} accepted unchanged, 0 safety)"
        else:
            out[lane] = f"hold ({c['accepted']}/{n} accepted; needs 10 decisions at 90%)"
    return out
