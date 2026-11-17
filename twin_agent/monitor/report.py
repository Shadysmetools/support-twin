"""Plain-English texts of the Twin Monitor: daily report, weekly scorecard, incident timeline.

Short and Telegram-ready (under 4,000 characters; evidence lists show 3 ids then "+N more"). Every outcome
number is followed by its evidence ids (`REQ-0042#msg-1a2b3c4d`). `label` (e.g. "SAMPLE / REPLAY") is printed on
the first line so replay numbers can never be mistaken for live ones.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..clock import CAIRO, utc
from ..models import parse_ts, utc_iso
from . import dev as devmod
from .kpis import BASELINE, activity, lanes, outcome, perc, writes, _mins


def window(day: date, days: int = 1) -> tuple[str, str]:
    start = datetime.combine(day, time(0), tzinfo=CAIRO)
    return utc_iso(start), utc_iso(datetime.combine(day + timedelta(days=days), time(0), tzinfo=CAIRO))


def evs(xs: list, n: int = 3) -> str:
    if not xs:
        return ""
    more = f" +{len(xs) - n} more" if len(xs) > n else ""
    return " [" + ", ".join(map(str, xs[:n])) + more + "]"


def _v(x, unit="") -> str:
    return "-" if x is None else f"{x:g}{unit}" if isinstance(x, (int, float)) else str(x)


def _pct(x) -> str:
    return "-" if x is None else f"{x}%"


def outcome_lines(o: dict) -> list[str]:
    t, h = o["twin_first_min"], o["human_first_min"]
    obs = f", {t['observe']} of them observe-mode 'would send'" if t.get("observe") else ""
    return [
        "Outcome KPIs (evidence in brackets):",
        f"- Requests: {o['received']} in, {o['responded']} responded; within target (60 min in hours, 15 min out): "
        f"{o['within_target']}/{o['received']} ({_pct(o['within_target_pct'])}){evs(o['within_target_ev'])}",
        f"- First reply: twin median {_v(t['median'], ' min')} / p90 {_v(t['p90'], ' min')} (n={t['n']}{obs}); "
        f"humans median {_v(h['median'], ' min')} / p90 {_v(h['p90'], ' min')} (n={h['n']})",
        f"- Closed successfully (CLOSED_PROVEN): {o['closed_proven']}{evs(o['closed_proven_ev'])}; confirmed by the "
        f"requester: {o['closed_confirmed']}{evs(o['closed_confirmed_ev'])}; closed without confirmation: "
        f"{o['closed_unconfirmed']}{evs(o['closed_unconfirmed_ev'])}; reopened: {o['reopened']}"
        f"{evs(o['reopened_ev'])}; escalated to Acme: {o['escalated']}{evs(o['escalated_ev'])}; abandoned: "
        f"{o['abandoned']}{evs(o['abandoned_ev'])}",
        f"- Satisfaction: {o['positive']} positive confirmation(s) / {o['closed']} closed = "
        f"{_pct(o['satisfaction_pct'])}{evs(o['positive_ev'])}; negative signals {o['negative']}{evs(o['negative_ev'])}; "
        f"Odoo helpdesk ratings: {o['odoo_ratings']}{' (none exist in Odoo today)' if not o['odoo_ratings'] else ''}",
        f"- Never on hold: {o['breaches']} timer breach(es) (target 0){evs(o['breaches_ev'])}",
        f"- Drafts: {o['accepted']} accepted unchanged, {o['edited']} edited, {o['rejected']} rejected"
        f"{evs(o['accepted_ev'])}; judge agrees with Principal {o['judge_agree']}/{o['judge_compared']} "
        f"({_pct(o['judge_agree_pct'])}); time saved about {_v(o['time_saved_min'])} min ({o['handling_basis']})",
    ]


def write_lines(w: dict) -> list[str]:
    if not w["total"] and not w["blocked"]:
        return ["Writes: 0 made or prepared, 0 blocked attempts (the v0 twin has no write path)."]
    env = ", ".join(f"{k} {v}" for k, v in sorted(w["by_env"].items())) or "none"
    return [f"Writes: {w['total']} ({env}); with recorded approval {w['approved']}/{w['total']} "
            f"({_pct(w['approved_pct'])}, target 100%); on Principal's instruction {w['instructed']}; "
            f"blocked attempts {w['blocked']}{evs(w['ev'])}"]


def dev_lines(d: dict | None, who: str = "Principal (his own pushes, from git; not the twin)") -> list[str]:
    if d is None:
        return []
    b = d["builds"]
    return [f"Dev work, {who}: {d['pushes']} push(es){evs(d['push_ids'])} on {len(d['branches'])} branch(es); "
            f"PRs {d['prs_opened']} opened / {d['prs_merged']} merged / {d['prs_reviewed']} reviewed{evs(d['pr_ids'])}; "
            f"version bumps {len(d['version_bumps'])}{evs(d['version_bumps'])}; without a ticket "
            f"{len(d['no_ticket'])}{evs(d['no_ticket'])}; reverts/hotfixes after his changes "
            f"{len(d['reverts_hotfixes_after'])}{evs(d['reverts_hotfixes_after'])}; staging to live median "
            f"{_v(d['staging_to_live_h_median'], ' h')}; to production {d['to_production']} (through the gate + "
            f"live-push approval: {d['to_production_gated']}); tests added: not in the push trace; odoo.sh builds: "
            + (f"{b['failed']} failed of {b['seen']}" if b else "not visible")]


def daily_report(store, day: date, now: datetime, cfg: dict | None = None, *, label: str = "",
                 twin: str = "Digital Twin") -> str:
    since, until = window(day)
    a, o, w = activity(store, since, until), outcome(store, since, until, now, cfg), writes(store, since, until)
    al = store.alerts(since, until)
    dev_rows = store.dev_rows()
    d = devmod.dev_kpis(dev_rows, since, until) if dev_rows else None
    head = f"Twin Monitor: {twin}, {day:%a %d %b %Y} (Cairo)" + (f" [{label}]" if label else "")
    lines = [head,
             f"Activity: {a['cards']} cards to Principal; sends {a['sent']} sent, {a['would_send']} would-send (observe), "
             f"{a['autopilot']} on autopilot, {a['blocked']} blocked; sessions {a['sessions_started']} started, "
             f"{a['sessions_resumed']} resumed, {a['stalled']} stalled; errors {a['errors']}, restarts "
             f"{a['restarts']}; Claude calls {a['claude_calls']}; audit chain "
             f"{'ok' if a['audit_ok'] else 'BROKEN' if a['audit_ok'] is False else 'not checked'}.",
             *outcome_lines(o), *write_lines(w), *dev_lines(d)]
    if al:
        sev = sorted(al, key=lambda x: ("critical", "high", "medium", "info").index(x["severity"]))
        lines.append(f"Alerts: {len(al)}. " + " | ".join(x["text"] for x in sev[:3]) +
                     (f" | +{len(al) - 3} more" if len(al) > 3 else ""))
    else:
        lines.append("Alerts: none.")
    if a["top_issues"]:
        lines.append("Top issues: " + ", ".join(f"{k} x{n}" for k, n in a["top_issues"]))
    return "\n".join(lines)


def _split(store, since, until, key) -> tuple[float | None, float | None]:
    """(median minutes in hours, median hours out of hours) for 'first_reply_at' or 'twin_first_at'."""
    from .kpis import _in
    ins, outs = [], []
    for q in store.requests():
        if _in(q.get("ts"), since, until) and (m := _mins(q.get("ts"), q.get(key))) is not None:
            if key == "first_reply_at" and q.get("first_reply_by") != "principal":
                continue
            (ins if q.get("in_hours") else outs).append(m)
    return perc(ins, 50), (round(perc(outs, 50) / 60, 1) if outs else None)


def scorecard(store, week_end: date, now: datetime, cfg: dict | None = None, *, label: str = "",
              twin: str = "Digital Twin", scores: list[float] | None = None) -> str:
    since, until = window(week_end - timedelta(days=6), 7)
    a, o, w = activity(store, since, until), outcome(store, since, until, now, cfg), writes(store, since, until)
    al = store.alerts(since, until)
    from .kpis import _in
    lat = [m for q in store.requests() if _in(q.get("ts"), since, until)
           and (m := _mins(q.get("ts"), q.get("twin_first_at"))) is not None]
    dev_al = [x for x in al if str(x.get("evidence") or "").startswith("dev:")]
    safety = [x for x in al if x["kind"] not in ("timer_breach", "volume") and x not in dev_al]
    dec = o["accepted"] + o["edited"] + o["rejected"]
    fp = f"{sum(scores) / len(scores):.0f}/100 over {len(scores)} drafts" if scores else "no Watcher scores yet"
    t_in, t_out = _split(store, since, until, "twin_first_at")
    h_in, h_out = _split(store, since, until, "first_reply_at")
    conf = round(100 * o["positive"] / o["responded"]) if o["responded"] else None     # baseline's denominator:
    reo = round(100 * o["reopened"] / o["responded"]) if o["responded"] else None      # answered requests
    lines = [f"Weekly scorecard: {twin}, week ending {week_end:%a %d %b %Y}" + (f" [{label}]" if label else ""),
             f"- Fingerprint match: {fp}; acceptance {o['accepted']}/{dec} unchanged "
             f"({_pct(round(100 * o['accepted'] / dec) if dec else None)})",
             f"- Autopilot share: {_pct(a['autopilot_pct'])} of {a['sends_out']} outgoing (sent or would-send)",
             f"- Twin safety events: {len(safety)}" + evs([x["kind"] for x in safety]) +
             f"; human dev alerts (Principal's own pushes): {len(dev_al)}" + evs([x["kind"] for x in dev_al]),
             f"- Twin first-reply latency: p50 {_v(perc(lat, 50), ' min')}, p90 {_v(perc(lat, 90), ' min')}, "
             f"p99 {_v(perc(lat, 99), ' min')} (n={len(lat)})",
             "- Lanes on the dial: " + "; ".join(f"{k}: {v}" for k, v in lanes(store, al).items()),
             *outcome_lines(o), *write_lines(w),
             *dev_lines(devmod.dev_kpis(store.dev_rows(), since, until) if store.dev_rows() else None),
             "Baseline vs this week (baseline = Principal's OLD triage-only role; source " + BASELINE["source"] + "):",
             "| | First reply in hours | Out of hours | Confirmed (of answered) | Reopened (of answered) |",
             "|---|---|---|---|---|",
             f"| Baseline: Principal (Telegram) | {BASELINE['principal_first_reply_in_hours_min']} min | "
             f"{BASELINE['principal_first_reply_out_of_hours_h']} h | {BASELINE['principal_confirmed_pct']}% | "
             f"{BASELINE['principal_reopened_pct']}% |",
             f"| Baseline: Odoo helpdesk | {BASELINE['helpdesk_first_public_reply_h']} h (first public reply) | - | - | "
             f"{BASELINE['helpdesk_reopen_pct']}% (median close {BASELINE['helpdesk_median_close_d']} d) |",
             f"| This week: humans | {_v(h_in, ' min')} | {_v(h_out, ' h')} | {_pct(conf)} | {_pct(reo)} |",
             f"| This week: twin (first reply only) | {_v(t_in, ' min')} | {_v(t_out, ' h')} | - | - |",
             "Confirmed and reopened are shares of every answered request (the baseline's denominator). This week's "
             "closes followed whoever answered, so they sit on the humans' row."]
    return "\n".join(lines)


def _describe(r: dict) -> str:
    k = r.get("kind")
    if k == "request_start":
        return "request opened"
    if k == "state":
        return f"state {r.get('src')} -> {r.get('dst')} ({r.get('reason') or ''})"
    if k == "worker_run":
        return f"worker run {r.get('run_no')} ({'resume' if (r.get('run_no') or 1) > 1 else 'start'}), tier {r.get('tier')}"
    if k == "checks":
        return (f"checks: failed {', '.join(r['failed'])}" if r.get("failed") else "checks: all passed") + \
            f" (verified answer: {'yes' if r.get('verified') else 'no'})"
    if k == "card":
        return f"card to {r.get('to_') or r.get('to') or 'principal'}: {r.get('card_kind')}"
    if k == "tap":
        return f"{r.get('actor')} tapped {r.get('action')}"
    if k == "send":
        return f"send {r.get('out_kind')} via {r.get('trigger')} ({r.get('mode')}): {r.get('status')}" + \
            (f", guards {', '.join(r['guards_blocked'])}" if r.get("guards_blocked") else "")
    if k == "human_reply":
        return f"{r.get('actor')} replied"
    if k == "timer":
        return f"timer {r.get('timer_kind')} fired"
    if r.get("cat") == "write":
        return f"WRITE {r.get('system')} {r.get('op')} -> {r.get('target') or r.get('model')} ({r.get('status')})"
    return k or "?"


def _when(ts) -> str:
    t = parse_ts(ts) if isinstance(ts, str) else None
    return f"{t.astimezone(CAIRO):%Y-%m-%d %H:%M}" if t else "?"


def incident(store, query: str) -> str:
    """Full ordered timeline of one request (by REQ id, ticket id, requester alias or message alias)."""
    q = query.strip()
    reqs = [r for r in store.requests() if q in (r.get("request_id"), str(r.get("ticket_id") or ""),
                                                 r.get("requester"), r.get("msg"))]
    if not reqs:
        return f"No request matches {q!r}."
    out = []
    for req in reqs:
        rid = req["request_id"]
        items = [(req.get("ts"), "request", f"from {req.get('requester')} on {req.get('channel')} "
                  f"({'in' if req.get('in_hours') else 'out of'} hours), lane {req.get('lane')}, evidence {req.get('msg')}")]
        items += [(r.get("ts"), r.get("cat"), _describe(r) + (f" [{r['msg']}]" if r.get("msg") else ""))
                  for r in store.records(request_id=rid)]
        items += [(a.get("ts"), "ALERT", a["text"]) for a in store.alerts() if a.get("request_id") == rid]
        items.sort(key=lambda x: utc(parse_ts(x[0])) if parse_ts(x[0]) else datetime.max.replace(tzinfo=CAIRO))
        out.append(f"Incident timeline {rid} (state now {req.get('state')}; first reply "
                   f"{_v(_mins(req.get('ts'), req.get('first_reply_at')), ' min')} human, "
                   f"{_v(_mins(req.get('ts'), req.get('twin_first_at')), ' min')} twin)")
        out += [f"{_when(ts)}  {cat:<8} {text}" for ts, cat, text in items]
    return "\n".join(out)
