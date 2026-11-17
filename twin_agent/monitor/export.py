"""Principal's side of the Twin Monitor: periodic, redacted ops bundles of the twin's own work.

Reads state.db through `readonly.ReadOnlyState` (cannot write) and verifies the hash-chained audit logs
(`AuditLog.verify` only reads). Writes ONE file per run to its own outbox: `ops/ops-<twin>-<UTC stamp>.jsonl`.

What goes in (metadata only; never message text, phone numbers, chat ids, Telegram ids or names):
- request   per request: source, channel, lane, tier, requester alias, in/out of hours, timestamps, state,
            first replies (human and twin), close, reopen, repeated asks / negative-wording COUNTS
- routing   request start, state moves, worker runs (session, start vs resume)
- check     judge verdicts per worker run (names of failed checks only)
- output    cards, taps (by role), draft-vs-final similarity, every send attempt (status, trigger, autopilot/tap)
- timer     timer fires, STALLED, snoozes
- health    errors, restarts, worker failures, worker minutes, Claude calls per day
- audit     hash-chain verification of logs/actions.jsonl and logs/vault-audit.jsonl
- write     every write the twin makes or prepares (Odoo / git / PR / environment), with approval evidence
- config    dial changes, kill switch, consent fingerprint (a hash, so a change shows without the refs)

Gate: the Watcher's consent D4 (`consent.d4_watcher_ref`) + `monitor.enabled: true`. Without them only a
`consent` heartbeat is written (no twin data), so Sysadmin's side can alert "consent changed".
/pause: Principal's OWN actions (his replies, taps, instructions) stamped inside a pause window are left out; the
twin's own work is always recorded (it is the agent's activity, not Principal's private time).
Every record passes the Watcher's `clean_record` (redact + pseudonymise) and is dropped if anything is left.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import paths
from ..audit import AuditLog
from ..checks.guards import MODE_GUARDS
from ..clock import SystemClock, elapsed, utc
from ..models import parse_ts, utc_iso
from ..watcher import gate
from ..watcher.bundle import Pseudonymiser, clean_record, known_names, load_salt
from ..watcher.capture import diff_stats
from .readonly import ReadOnlyState

PRINCIPAL_ACTS = {"principal_replied", "callback", "instruction", "command"}
EVENT_CAT = {
    "request.new": ("routing", "request_start"), "state": ("routing", "state"), "packet": ("routing", "worker_run"),
    "worker_failed": ("health", "worker_failed"), "worker_skipped": ("health", "worker_skipped"),
    "handler_error": ("health", "error"), "checks_error": ("health", "error"), "voice_failed": ("health", "error"),
    "health": ("health", "health"), "listener_start": ("health", "restart"),
    "circuit_breaker": ("health", "circuit_breaker"), "catchup": ("health", "catchup"),
    "timer": ("timer", "timer"), "stalled": ("timer", "stalled"), "snooze": ("timer", "snooze"),
    "principal_replied": ("output", "human_reply"), "callback": ("output", "tap"), "instruction": ("output", "instruction"),
    "card_sent": ("output", "card"), "send": ("output", "send"), "send_skipped": ("output", "send_skipped"),
    "approval_refused": ("check", "approval_refused"), "reaction": ("output", "reaction"),
    "dial": ("config", "dial"), "kill": ("config", "kill"), "command": ("config", "command"),
    "watcher_pause": ("config", "pause"), "watcher_unpause": ("config", "unpause"),
    "write": ("write", "write"), "write_blocked": ("write", "write_blocked"), "tool_denied": ("write", "tool_denied"),
}
DROP_KEYS = {"text", "nonce", "text_sha256", "voice_sha256", "remote_id", "detail", "args", "event_id", "to"}
NEGATIVE = re.compile(r"still|same (error|problem)|not working|doesn.?t work|again|urgent|!!|\?\?|لسه|مش شغال|"
                      r"نفس المشكلة|تاني|ضروري|ليه", re.I)
WRITE_KEYS = ("system", "op", "env", "target", "status", "login", "pr", "approvals", "preconditions", "model",
              "record_id", "fields", "stage_to", "emails_client", "auto_close_clock", "reason", "purpose", "uid")


def monitor_on(cfg: dict) -> bool:
    return gate.consent_ok(cfg) and (cfg.get("monitor") or {}).get("enabled") is True


def consent_state(cfg: dict) -> dict:
    """Consent as a fingerprint: shows a change without exporting the reference texts."""
    c = cfg.get("consent") or {}
    keys = {"d4": c.get("d4_watcher_ref"), "d1": c.get("d1_ref"), "b17": c.get("b17_staff_data_to_claude"),
            "monitor": (cfg.get("monitor") or {}).get("enabled"), "watcher": (cfg.get("watcher") or {}).get("enabled")}
    blob = json.dumps(keys, sort_keys=True, default=str).encode()
    return {"d4": gate.consent_ok(cfg), "monitor_on": monitor_on(cfg), "hash": hashlib.sha256(blob).hexdigest()[:12]}


def outbox_dir(cfg: dict) -> Path:
    p = Path(str((cfg.get("monitor") or {}).get("outbox") or "data/monitor/outbox"))
    return p if p.is_absolute() else paths.home() / p


class Roles:
    """Actor -> role. Telegram ids never leave the laptop."""

    def __init__(self, cfg: dict, pseudo: Pseudonymiser):
        people = cfg.get("people") or {}
        self.principal = str(people.get("principal_tg_user_id") or "")
        self.backups = {str(x) for x in people.get("backup_tg_user_ids") or []}
        self.supervisor = str(people.get("supervisor_tg_user_id") or "")
        self.pseudo = pseudo

    def __call__(self, actor) -> str:
        a = str(actor or "system")
        if self.principal and a == self.principal:
            return "principal"
        if self.supervisor and a == self.supervisor:
            return "supervisor"
        if a in self.backups:
            return "backup"
        if a.lstrip("-").isdigit():
            return self.pseudo.alias(a, "person")
        return a if re.fullmatch(r"[a-z_]{2,20}", a) else "system"


def msg_alias(salt: bytes, event_id) -> str | None:
    """Evidence link for a message: stable on both sides, reveals no chat or message id."""
    if not event_id:
        return None
    return "msg-" + hmac.new(salt, str(event_id).encode(), hashlib.sha256).hexdigest()[:8]


def _detail(row: dict) -> dict:
    try:
        d = json.loads(row.get("detail_json") or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _event_record(row: dict, twin: str, roles: Roles, salt: bytes) -> dict | None:
    cat_kind = EVENT_CAT.get(row["kind"])
    if cat_kind is None:
        return None
    d = _detail(row)
    rec = {"key": f"{twin}:ev:{row['id']}", "cat": cat_kind[0], "kind": cat_kind[1], "ts": row["ts"],
           "request_id": row.get("request_id"), "actor": roles(row.get("actor"))}
    if d.get("event_id"):
        rec["msg"] = msg_alias(salt, d["event_id"])
    if rec["kind"] == "send":
        blocked = [g for g in d.get("blocked_by") or [] if g not in MODE_GUARDS]
        rec["guards_blocked"] = blocked
        rec["mode"] = "autopilot" if d.get("trigger") in ("timeout", "holding_floor") else "tap"
    if cat_kind[0] == "write":
        rec.update({k: d.get(k) for k in WRITE_KEYS if k in d})
        for a in rec.get("approvals") or []:
            if isinstance(a, dict):
                a["by"] = roles(a.get("by"))
                a.pop("words", None)                          # exact words stay on the laptop; their hash travels
        return rec
    for k, v in d.items():
        if k in DROP_KEYS or k in rec or isinstance(v, (dict,)):
            continue
        rec[k] = v if not isinstance(v, str) else v[:120]
    return rec


def _request_record(ro: ReadOnlyState, r: dict, twin: str, policy, salt: bytes) -> dict:
    rid = r["id"]
    created = parse_ts(r.get("created_at"))
    twin_first = ro._one("SELECT created_at, status FROM sends WHERE request_id = ? AND status IN "
                         "('sent','would_send','maybe_sent') ORDER BY created_at LIMIT 1", (rid,))
    thread = ro.thread(rid)
    first_reply = parse_ts(r.get("first_reply_at"))
    after = [m for m in thread if not m.get("is_principal") and not m.get("is_bot") and first_reply
             and parse_ts(m.get("ts")) and utc(parse_ts(m["ts"])) > utc(first_reply)]
    root = next((m for m in thread if not m.get("is_principal")), None)
    pk = ro._one("SELECT packet_json, created_at FROM packets WHERE request_id = ? ORDER BY run_no DESC LIMIT 1",
                 (rid,))
    sim = None
    if pk:
        try:
            draft = ((json.loads(pk["packet_json"]) or {}).get("draft_reply") or {}).get("text") or ""
        except ValueError:
            draft = ""
        own = next((m for m in thread if m.get("is_principal") and parse_ts(m.get("ts")) and parse_ts(pk["created_at"])
                    and utc(parse_ts(m["ts"])) > utc(parse_ts(pk["created_at"]))), None)
        if draft and own and own.get("text"):
            st = diff_stats(draft, own["text"])
            sim = {"similarity": st["similarity"], "unchanged": st["unchanged"],
                   "draft_words": st["draft_words"], "final_words": st["final_words"]}
    return {"key": f"{twin}:req:{rid}", "cat": "request", "kind": "request", "request_id": rid,
            "ts": r.get("created_at"), "channel": r.get("channel"), "source": (r.get("channel") or "").split("_")[0],
            "sender_role": "requester", "requester": r.get("requester_id") or r.get("requester_name"),
            "lane": r.get("lane"), "tier": r.get("tier"), "company": r.get("company"), "state": r.get("state"),
            "in_hours": bool(created and policy.is_working_time(created)),
            "first_reply_at": r.get("first_reply_at"), "first_reply_by": r.get("first_reply_by"),
            "twin_first_at": twin_first["created_at"] if twin_first else None,
            "twin_first_status": twin_first["status"] if twin_first else None,
            "closed_at": r.get("closed_at"), "close_reason": close_code(r.get("close_reason")),
            "reopened_from": r.get("reopened_from"), "ticket_id": r.get("ticket_id"),
            "taken_over": bool(r.get("taken_over")), "session_id": r.get("session_id"),
            "worker_runs": r.get("worker_runs") or 0, "agent_minutes": round(r.get("agent_minutes") or 0, 1),
            "principal_minutes": round(r.get("principal_minutes") or 0, 1), "updated_at": r.get("updated_at"),
            "msg": msg_alias(salt, (root or {}).get("event_id")), "requester_msgs_after_reply": len(after),
            "negative_signals": sum(1 for m in after if NEGATIVE.search(m.get("text") or "")),
            "edit": sim}


def collect(ro: ReadOnlyState, cfg: dict, now: datetime, *, twin: str = "principal", after_event_id: int = 0,
            logs_dir: Path | None = None, salt: bytes | None = None) -> tuple[list[dict], int]:
    """(records, last event id). Pure read of state.db + audit logs."""
    from ..timers import TimerPolicy
    salt = salt or load_salt()
    pseudo = Pseudonymiser(salt, known_names(ro, cfg))
    roles, policy = Roles(cfg, pseudo), TimerPolicy(cfg)
    windows = gate.pause_windows(ro, now) or []
    raw: list[dict] = []
    last = after_event_id
    for row in ro.events(after_id=after_event_id):
        last = max(last, int(row["id"]))
        if row["kind"] in PRINCIPAL_ACTS and gate.in_windows(windows, parse_ts(row.get("ts"))):
            continue
        rec = _event_record(row, twin, roles, salt)
        if rec:
            raw.append(rec)
    for r in ro._all("SELECT * FROM requests ORDER BY id"):
        raw.append(_request_record(ro, r, twin, policy, salt))
    for p in ro._all("SELECT request_id, run_no, verified, verdicts_json, created_at FROM packets"):
        try:
            failed = [v.get("check") for v in json.loads(p["verdicts_json"] or "[]") if not v.get("passed")]
        except (ValueError, AttributeError):
            failed = ["unreadable"]
        raw.append({"key": f"{twin}:pk:{p['request_id']}:{p['run_no']}", "cat": "check", "kind": "checks",
                    "request_id": p["request_id"], "ts": p["created_at"], "run_no": p["run_no"],
                    "verified": bool(p["verified"]), "failed": failed})
    for k in ro._all("SELECT day, SUM(value) AS n FROM kpis WHERE name = 'claude_call' GROUP BY day"):
        raw.append({"key": f"{twin}:usage:{k['day']}", "cat": "health", "kind": "usage", "day": k["day"],
                    "ts": f"{k['day']}T12:00:00+00:00", "claude_calls": int(k["n"] or 0)})
    logs = Path(logs_dir) if logs_dir else paths.logs_dir()
    stamp = utc_iso(now)
    for name in ("actions.jsonl", "vault-audit.jsonl"):
        log = AuditLog(logs / name, SystemClock())
        ok, bad = log.verify()
        raw.append({"key": f"{twin}:audit:{name}:{stamp}", "cat": "audit", "kind": "audit_verify", "ts": stamp,
                    "file": name, "ok": ok, "bad_line": bad, "lines": log.count()})
    out = []
    for rec in raw:
        c = clean_record(rec, pseudo)
        if c is not None:
            _restore_roles(rec, c)
            out.append(c)
    return out, last


ROLE = re.compile(r"[a-z_]{2,20}|person-[0-9a-f]{4}")
ROLE_KEYS = ("key", "actor", "first_reply_by", "twin_first_status", "close_reason", "mode", "status")


def _restore_roles(raw: dict, clean: dict) -> None:
    """Role words we produced ourselves ('principal', 'backup', 'agent') must survive the name scrubber."""
    for k in ROLE_KEYS:
        v = raw.get(k)
        if isinstance(v, str) and (k == "key" or ROLE.fullmatch(v)):
            clean[k] = v
    for a, b in zip(raw.get("approvals") or [], clean.get("approvals") or []):
        if isinstance(a, dict) and isinstance(b, dict) and ROLE.fullmatch(str(a.get("by") or "")):
            b["by"] = a["by"]


def close_code(reason) -> str | None:
    r = str(reason or "").lower()
    if not r:
        return None
    if "acked (text)" in r:
        return "ack_text"
    if "acked (reaction)" in r:
        return "ack_reaction"
    if "silen" in r:
        return "silence"
    if "proof" in r or "re-read" in r or "reread" in r:
        return "proof"
    if "/close" in r or "by hand" in r or "button" in r:
        return "by_hand"
    return "other"


@dataclass
class ExportResult:
    path: Path | None
    records: int
    consent_only: bool


def _cursor(outbox: Path) -> dict:
    try:
        return json.loads((outbox / "ops" / "cursor.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def export(cfg: dict, now: datetime, *, state_path: Path | None = None, logs_dir: Path | None = None,
           outbox: Path | None = None, twin: str = "principal", salt: bytes | None = None) -> ExportResult:
    """Write one ops bundle. Never writes to state.db (it is opened read-only)."""
    outbox = Path(outbox or outbox_dir(cfg))
    d = outbox / "ops"
    d.mkdir(parents=True, exist_ok=True)
    head = {"bundle": {"twin": twin, "made_at": utc_iso(now), "version": 1, "consent": consent_state(cfg)}}
    recs: list[dict] = []
    cur = _cursor(outbox)
    last = int(cur.get("event_id") or 0)
    if monitor_on(cfg):
        ro = ReadOnlyState(state_path or paths.state_db())
        try:
            recs, last = collect(ro, cfg, now, twin=twin, after_event_id=last, logs_dir=logs_dir, salt=salt)
        finally:
            ro.close()
    head["bundle"]["id"] = f"ops-{twin}-{utc(now):%Y%m%dT%H%M%SZ}"
    path = d / f"{head['bundle']['id']}.jsonl"
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False, default=str) for x in [head, *recs]) + "\n",
                    encoding="utf-8")
    (d / "cursor.json").write_text(json.dumps({"event_id": last, "at": utc_iso(now)}), encoding="utf-8")
    return ExportResult(path, len(recs), not monitor_on(cfg))


def read_bundle(path: Path) -> tuple[dict, list[dict]]:
    head, recs = {}, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            if "bundle" in obj:
                head = obj["bundle"]
            else:
                recs.append(obj)
    return head, recs


def minutes_between(a, b) -> float | None:
    a, b = parse_ts(a) if isinstance(a, str) else a, parse_ts(b) if isinstance(b, str) else b
    return round(elapsed(a, b).total_seconds() / 60, 1) if a and b else None
