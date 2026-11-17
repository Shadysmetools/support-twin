"""Alert rules of the Twin Monitor. Pure functions over ingested records; the store dedupes by key.

Immediate alerts: a failed or blocked send; a timer breach (requester waited past target with no reply);
a stalled session; an Odoo write or a forbidden tool; a large draft-vs-final divergence on an edited draft; an
audit-chain break; the consent fingerprint changed; unusual volume; and every write rule below.

Write rules (Sysadmin, 2026-11-03). Environments: production = `main`, any `*-live` branch, or env "production";
staging = `Stagelatest` / `Dispatch-5`; feature = `feat/` `fix/` `dev/` branches. Approval kinds in a write's
`approvals`: `principal_instruction` (his exact words recorded, hash travels), `supervisor_gate`, `riley_live`.
- push / PR to feature or staging WITH a recorded Principal instruction: OK (reported, no alert);
- the same WITHOUT one: high;
- production without BOTH the gate and the live-push approval: CRITICAL;
- a target outside feature/staging/production scope: high; a shared login: high;
- preconditions on git writes: tests green, no manifest version bump, a ticket linked, a Judge review on the PR.

Acme' own helpdesk (env "sme_helpdesk", 2026-11-03): Digital Twin's ticket and timesheet writes on
helpdesk.example.com. Allowed and evidenced (no alert) when the (model, op) is on the allowlist (SME_ALLOWED) and an
`principal_tap` or `principal_instruction` (or, for timesheets only, `timesheet_autopilot`) is recorded. A move into a stage
that emails the client without `supervisor_gate` is CRITICAL; any other Odoo model / op is high, and so is a write
with no approval. Every other Odoo write keeps the old high "odoo_write" alert.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import datetime

from ..clock import elapsed
from ..models import parse_ts, utc_iso

SEVERITY = ("critical", "high", "medium", "info")
STAGING = {"stagelatest", "dispatch-5"}
FEATURE = re.compile(r"^(feat|fix|dev|feature|hotfix|principal)/", re.I)
GIT_OPS = {"push", "pr_open", "pr_merge", "merge"}
SME_ENV = "sme_helpdesk"
SME_OPS = {"helpdesk.ticket": "message_post write create", "account.analytic.line": "only: create"}
SME_ALLOWED = {(m, op) for m, ops in SME_OPS.items() for op in ops.replace("only:", "").split()}


def target_minutes(in_hours: bool, cfg: dict | None = None) -> int:
    t = ((cfg or {}).get("monitor") or {}).get("targets") or {}
    return int(t.get("in_hours_min", 60)) if in_hours else int(t.get("out_of_hours_min", 15))


def classify_env(w: dict) -> str:
    env = str(w.get("env") or "").lower()
    tgt = str(w.get("target") or "").strip()
    if env == SME_ENV:
        return SME_ENV
    if env == "production" or tgt == "main" or tgt.lower().endswith("-live"):
        return "production"
    if env == "staging" or tgt.lower() in STAGING:
        return "staging"
    if FEATURE.match(tgt) or env in ("feature", "dev"):
        return "feature"
    return "out_of_scope"


def approval_kinds(w: dict) -> set[str]:
    out = set()
    for a in w.get("approvals") or []:
        if not isinstance(a, dict) or not a.get("at"):
            continue                                           # no time = no evidence
        k = a.get("kind")
        if k == "principal_instruction" and not (a.get("by") == "principal" and a.get("words_sha256")):
            continue                                           # only Principal's own words, recorded, count
        if k == "principal_tap" and a.get("by") != "principal":
            continue                                           # only Principal's own tap counts
        out.add(k)
    return out


def _host(url) -> str:
    """The host of a URL or bare host string, lower-cased (https://helpdesk.example.com/x -> helpdesk.example.com)."""
    u = str(url or "").strip()
    if "//" in u:
        u = u.split("//", 1)[1]
    return u.split("/", 1)[0].split("@")[-1].strip().lower()


def judge_write(w: dict, cfg: dict | None = None) -> list[tuple[str, str, str]]:
    """(severity, kind, text) for one write record. Empty list = allowed and fully evidenced."""
    out: list[tuple[str, str, str]] = []
    system, op = str(w.get("system") or "?"), str(w.get("op") or "?")
    what = f"{system} {op} to {w.get('target') or w.get('model') or '?'}"
    if w.get("kind") == "tool_denied":
        return [("high", "forbidden_tool", f"forbidden tool attempted: {w.get('tool') or what}")]
    if str(w.get("status")) == "blocked" or w.get("kind") == "write_blocked":
        out.append(("medium", "write_blocked", f"blocked write attempt: {what}"))
    env, kinds = classify_env(w), approval_kinds(w)
    if system == "odoo" and env == SME_ENV:
        return out + _judge_sme(w, op, kinds, cfg)
    if system == "odoo":
        out.append(("high", "odoo_write", f"Odoo write {'prepared' if w.get('status') == 'prepared' else 'made'}: "
                    f"{op} on {w.get('model') or '?'} ({env})"))
    if w.get("login") == "shared":
        out.append(("high", "shared_login_write", f"write through a shared login: {what}"))
    if env == "production" and not {"supervisor_gate", "riley_live"} <= kinds:
        missing = sorted({"supervisor_gate", "riley_live"} - kinds)
        out.append(("critical", "prod_write_unapproved", f"PRODUCTION {what} without {' + '.join(missing)}"))
    elif env in ("staging", "feature") and "principal_instruction" not in kinds and str(w.get("status")) != "blocked":
        out.append(("high", "write_without_instruction", f"{what} with no recorded instruction from Principal"))
    elif env == "out_of_scope":
        out.append(("high", "out_of_scope_write", f"{what} is outside the allowed staging scope"))
    if system == "git" or op in GIT_OPS:
        pre = w.get("preconditions") or {}
        if pre.get("tests_green") is not True:
            out.append(("medium", "push_precondition", f"{what}: tests not recorded green"))
        if pre.get("version_bump"):
            out.append(("high", "version_bump", f"{what}: manifest version bump (forbidden)"))
        if not pre.get("ticket"):
            out.append(("medium", "push_no_ticket", f"{what}: no ticket linked"))
        if op in ("pr_open", "pr_merge") and not pre.get("judge_review"):
            out.append(("medium", "push_precondition", f"{what}: no Judge review comment on the PR"))
    return out


def _judge_sme(w: dict, op: str, kinds: set[str], cfg: dict | None = None) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    model, blocked = str(w.get("model") or "?"), str(w.get("status")) == "blocked" or w.get("kind") == "write_blocked"
    what = f"Odoo {op} on {model} (Acme helpdesk)"
    if (model, op) not in SME_ALLOWED:
        out.append(("high", "odoo_write", f"{what} is outside the helpdesk allowlist"))
    ok = {"principal_tap", "principal_instruction"} | ({"timesheet_autopilot"} if model == "account.analytic.line" else set())
    if not blocked and not kinds & ok:
        out.append(("high", "write_without_instruction", f"{what} with no recorded tap or instruction from Principal"))
    # emails_client OR the 7-day auto-close clock (stage 9 "Live") both eventually email the client: need Supervisor.
    if (w.get("emails_client") or w.get("auto_close_clock")) and "supervisor_gate" not in kinds and not blocked:
        out.append(("critical", "client_email_stage_unapproved",
                    f"{what}: moved into stage {w.get('stage_to')} that emails the client, without Supervisor's tap"))
    if not blocked:
        out += _judge_identity(w, cfg)
    return out


def _judge_identity(w: dict, cfg: dict | None = None) -> list[tuple[str, str, str]]:
    """Classify the authenticated uid / odoo url recorded on the write. Alerts only on POSITIVE evidence
    (uid in the admin set, a url host other than the pinned one, or a shared login); missing facts never alert."""
    out: list[tuple[str, str, str]] = []
    mon = ((cfg or {}).get("monitor") or {}).get("odoo") or {}
    admin = {int(x) for x in (mon.get("admin_uids") or []) if str(x).strip().lstrip("-").isdigit()}
    uid = w.get("uid")
    if isinstance(uid, int) and not isinstance(uid, bool) and uid in admin:
        out.append(("high", "admin_uid_write", f"Odoo write authenticated as admin uid {uid}, not Principal's own key"))
    pinned = _host(mon.get("pinned_host") or ((cfg or {}).get("odoo_sys") or {}).get("url") or "")
    host = _host(w.get("url") or w.get("target") or "")
    if host and pinned and host != pinned:
        out.append(("high", "off_host_write", f"Odoo write to {host}, not the pinned host {pinned}"))
    if w.get("login") == "shared":
        out.append(("high", "shared_login_write", "Odoo write through a shared login"))
    return out


def _alert(sev, kind, rec_or_req, text, *, key=None, evidence=None) -> dict:
    rid = rec_or_req.get("request_id")
    ev = evidence or [x for x in (rid, rec_or_req.get("msg"), rec_or_req.get("key")) if x]
    return {"key": key or f"{kind}:{rec_or_req.get('key') or rid}", "ts": rec_or_req.get("ts"), "severity": sev,
            "kind": kind, "request_id": rid, "text": f"[{sev.upper()}] {rid + ': ' if rid else ''}{text}",
            "evidence": ev}


def record_alerts(recs: list[dict], cfg: dict | None = None) -> list[dict]:
    """Alerts from newly ingested records."""
    out = []
    for r in recs:
        k = r.get("kind")
        if k == "send" and r.get("status") in ("blocked", "failed", "maybe_sent"):
            why = ", ".join(r.get("guards_blocked") or []) or r.get("reason") or r.get("status")
            out.append(_alert("high", f"send_{'blocked' if r['status'] == 'blocked' else 'failed'}", r,
                              f"{r.get('out_kind') or 'message'} send {r['status']} ({why})"))
        elif k == "stalled" or (k == "state" and r.get("dst") == "STALLED"):
            out.append(_alert("high", "stalled", r, "session STALLED", key=f"stalled:{r.get('request_id')}"))
        elif r.get("cat") == "write":
            for i, (sev, kind, text) in enumerate(judge_write(r, cfg)):
                out.append(_alert(sev, kind, r, text, key=f"{kind}:{r['key']}:{i}"))
        elif k == "audit_verify" and r.get("ok") is False:
            out.append(_alert("critical", "audit_break", r, f"audit chain broken in {r.get('file')} at line "
                              f"{r.get('bad_line')}", key=f"audit_break:{r.get('file')}:{r.get('bad_line')}"))
    return out


def _edited(store, rid: str) -> bool:
    taps = store.records(kind="tap", request_id=rid)
    sends = store.records(kind="send", request_id=rid)
    return any(t.get("action") in ("ed", "sd") for t in taps) or any(s.get("trigger") == "principal_edit" for s in sends)


def reply_at(req: dict) -> str | None:
    """The first reply of anyone (Principal, the backup or the twin, incl. an observe-mode 'would send')."""
    c = [x for x in (req.get("first_reply_at"), req.get("twin_first_at")) if x]
    return min(c, key=lambda s: parse_ts(s)) if c else None


def wait_minutes(req: dict, now: datetime) -> tuple[float | None, bool]:
    """(minutes the requester waited for a first reply, still waiting?)."""
    start = parse_ts(req.get("ts"))
    r = reply_at(req)
    end = parse_ts(r) if r else now
    if start is None or end is None:
        return None, r is None
    return round(elapsed(start, end).total_seconds() / 60, 1), r is None


def request_alerts(store, now: datetime, cfg: dict | None = None) -> list[dict]:
    """Rules over the request snapshots: timer breach, divergence, unusual volume."""
    out = []
    reqs = store.requests()
    for q in reqs:
        waited, open_ = wait_minutes(q, now)
        tgt = target_minutes(bool(q.get("in_hours")), cfg)
        closed = bool(q.get("closed_at"))
        if waited is not None and waited > tgt and not (open_ and closed):
            out.append(_alert("high", "timer_breach", q, f"requester waited {waited:.0f} min for a first reply "
                              f"(target {tgt} min, {'in' if q.get('in_hours') else 'out of'} hours)"
                              f"{' and is still waiting' if open_ else ''}", key=f"breach:{q['request_id']}"))
        e = q.get("edit") or {}
        thr = float(((cfg or {}).get("monitor") or {}).get("divergence_below", 0.5))
        if e and e.get("similarity") is not None and e["similarity"] < thr and _edited(store, q["request_id"]):
            out.append(_alert("medium", "divergence", q, f"Principal's final differs a lot from the draft "
                              f"(similarity {e['similarity']})", key=f"divergence:{q['request_id']}"))
    per_day = Counter(str(parse_ts(q["ts"]).date()) for q in reqs if parse_ts(q.get("ts")))
    days = sorted(per_day)
    for i, d in enumerate(days):
        prev = [per_day[x] for x in days[max(0, i - 7):i]]
        if len(prev) >= 3 and per_day[d] >= max(10, 3 * statistics.median(prev)):
            out.append({"key": f"volume:{d}", "ts": f"{d}T23:59:00+00:00", "severity": "medium", "kind": "volume",
                        "request_id": None, "evidence": [f"day:{d}"],
                        "text": f"[MEDIUM] unusual volume on {d}: {per_day[d]} requests (median before: "
                                f"{statistics.median(prev):g})"})
    return out


def consent_alerts(bundles: list[dict]) -> list[dict]:
    out, last = [], {}
    for b in bundles:
        twin = b.get("twin")
        if twin in last and last[twin] != b.get("consent_hash"):
            out.append({"key": f"consent:{b['id']}", "ts": b.get("made_at"), "severity": "high", "kind": "consent",
                        "request_id": None, "evidence": [b["id"]],
                        "text": f"[HIGH] consent changed for twin {twin} (D4 now {'on' if b.get('d4') else 'OFF'})"})
        last[twin] = b.get("consent_hash")
    return out


def stamp(now: datetime) -> str:
    return utc_iso(now)
