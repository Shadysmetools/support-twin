"""Code guards (05 3.4). Never AI. Any failure blocks the send.

check_outbound runs every guard and lists every one that said no. A guard that crashes counts as a "no"
(fail closed). Text rules live in guard_text.py, the fix gate in fixgate.py.

CLI: python -I aa.py guards-scan --scan data\\logs   (exit 1 if a secret-like value is in any log file)
     python -I aa.py guards-scan --selftest
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .. import paths, redact
from ..clock import elapsed, utc
from ..models import Channel, Dial, GuardResult, Lane, OutboundMessage, OutKind, SendTrigger
from . import guard_text as T
from .fixgate import (draft_hash, fix_block_hash, fix_hash_matches,  # noqa: F401  (re-exported)
                      recompute_exemption, recompute_verified)

# Guards that mean "not allowed to go out now" (mode), not "this text is unsafe". See CONTRACT Changes.
MODE_GUARDS = frozenset({"send_disabled", "consent", "dial", "notice_gate"})
AUTONOMOUS = frozenset({SendTrigger.timeout, SendTrigger.holding_floor})
HOLDING_KINDS = frozenset({OutKind.holding, OutKind.email_holding})
TELEGRAM = frozenset({Channel.telegram_group, Channel.telegram_dm})
SEND_ACTIONS = frozenset({"ap", "sd", "as", "ct", "nv"})   # nv = "Send without voice" (voice card)


@dataclass
class GuardContext:
    cfg: dict
    db: object
    now: datetime
    trigger: SendTrigger
    kill_switch_on: bool
    approval: dict | None = None
    thread_after_draft: tuple = ()
    principal_last_msg_at: datetime | None = None
    requester_is_allowed: bool = True
    requester_is_bot_or_staff_owner: bool = False
    original_msg_ts: datetime | None = None
    vault_hashes: tuple = ()


def only_mode_blocks(result: GuardResult) -> bool:
    """True if the send was stopped only by mode guards: record 'would_send', not 'blocked'."""
    return bool(result.blocked_by) and set(result.blocked_by) <= MODE_GUARDS


# ---------------------------------------------------------------- small helpers

def _g(cfg: dict, *keys, default=None):
    cur: Any = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _field(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _is_principal_msg(item) -> bool:
    if isinstance(item, dict):
        return bool(item.get("is_principal"))
    return bool(getattr(getattr(item, "sender", None), "is_principal", False))


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _lane_dial(cfg: dict, db, lane: Lane) -> Dial:
    """The live dial. StateDB.get_dial is authoritative when it knows the config (a /auto row, else config).
    A db without cfg would answer 'off' for every lane, so then we take the stricter of db and config."""
    raw = _g(cfg, "dial", str(lane), default="off")
    cfg_dial = Dial("off" if raw is False else str(raw))
    try:
        db_dial = Dial(db.get_dial(lane))
    except Exception:
        return cfg_dial
    if getattr(db, "cfg", None) is not None:
        return db_dial
    return db_dial if db_dial.rank() < cfg_dial.rank() else cfg_dial


def _consent_channel(ch: Channel) -> str | None:
    return {Channel.telegram_group: "telegram", Channel.telegram_dm: "telegram", Channel.email: "email",
            Channel.ticket_mail: "tickets"}.get(Channel(ch))


def _sent(db, since: datetime, **kw) -> list[dict]:
    return list(db.sends_since(since, statuses=("sent",), **kw))


# ---------------------------------------------------------------- the guards (each returns list of reasons)

def _kill(m, c):
    return ["kill switch on"] if c.kill_switch_on or paths.stop_file().exists() else []


def _send_disabled(m, c):
    if _g(c.cfg, "send_enabled") is not True:
        return ["send_enabled is false"]
    return ["run_mode is not live"] if _g(c.cfg, "run_mode") != "live" else []


def _consent(m, c):
    if not _g(c.cfg, "consent", "d1_ref"):
        return ["no signed D1 reference"]
    name = _consent_channel(m.channel)
    return [] if name and _g(c.cfg, "consent", "channels", name) is True else [f"channel consent off ({name})"]


def _dial(m, c):
    dial = _lane_dial(c.cfg, c.db, m.lane)
    need = Dial.auto_on_timeout if c.trigger in AUTONOMOUS else Dial.draft
    return [] if dial.at_least(need) else [f"lane {m.lane} dial {dial} below {need}"]


def _notice_gate(m, c):
    if c.trigger not in AUTONOMOUS:
        return []
    out = []
    if not (_g(c.cfg, "consent", "notice_gate", "sysadmin_b1_ref") and _g(c.cfg, "consent", "notice_gate", "executive_b2_ref")):
        out.append("Globex notice gate refs missing")
    if _g(c.cfg, "consent", "signatory_must_approve"):
        out.append("Signatory must approve first")
    return out


def _chat_allow(m, c):
    if not c.requester_is_allowed:
        return ["requester not on the allow-list"]
    if m.channel not in TELEGRAM:
        return []
    for chat in _g(c.cfg, "chats", default=[]) or []:
        if chat.get("read") and chat.get("chat_id") and str(chat.get("chat_id")) == str(m.chat_id):
            topics = [t.get("id") for t in chat.get("topics") or [] if t.get("id")]
            if not topics or m.topic_id is None or m.topic_id in topics:
                return []
            return ["topic not on the allow-list"]
    return ["chat not on the allow-list"]


def _thread_binding(m, c):
    if m.channel in TELEGRAM:
        out = [] if m.reply_to else ["no reply_to (must answer the requester's message)"]
        req = c.db.get_request(m.request_id) if hasattr(c.db, "get_request") else None
        if req:
            if req.get("chat_id") is not None and str(req["chat_id"]) != str(m.chat_id):
                out.append("not the request's chat")
            if req.get("topic_id") is not None and m.topic_id is not None and int(req["topic_id"]) != int(m.topic_id):
                out.append("not the request's topic")
        return out
    if m.channel == Channel.email and m.kind != OutKind.ticket_email and not (m.in_reply_to or m.thread_id):
        return ["email is not a reply in the source thread"]
    return []


def requester_is_owner(cfg: dict, requester_id) -> bool:
    """True if the request's requester is Principal, a backup or a configured owner id. An autonomous (holding /
    timeout) reply to one of them must be card-only, never auto-sent. The sender feeds this into the identity
    guard; without it the guard was dead (the field defaulted to False and nothing ever set it)."""
    people = (cfg or {}).get("people") or {}
    owners = {str(people.get("principal_tg_user_id") or "").strip()}
    owners |= {str(x).strip() for x in (people.get("backup_tg_user_ids") or [])}
    owners |= {str(x).strip() for x in (people.get("owner_tg_user_ids") or [])}
    owners -= {"", "0"}
    rid = str(requester_id or "").strip()
    return bool(rid) and rid in owners


def _identity(m, c):
    if c.trigger in AUTONOMOUS and c.requester_is_bot_or_staff_owner:
        return ["requester is a bot, Principal, Executive, Signatory or unknown: card only"]
    return []


def _human_active(m, c):
    if c.trigger not in AUTONOMOUS or c.principal_last_msg_at is None:
        return []
    lock = timedelta(minutes=_g(c.cfg, "timers", "human_active_lock_min", default=5))
    return ["Principal wrote in this chat in the last minutes"] if elapsed(c.principal_last_msg_at, c.now) < lock else []


def _already_answered(m, c):
    if c.trigger in AUTONOMOUS and any(_is_principal_msg(x) for x in c.thread_after_draft or ()):
        return ["Principal already answered in the thread"]
    return []


def _rate_gap(m, c):
    if c.trigger not in AUTONOMOUS:
        return []
    gap = _g(c.cfg, "guards", "rate", "per_request_min_gap_min", default=10)
    return ["less than the per-request gap"] if _sent(c.db, utc(c.now) - timedelta(minutes=gap), request_id=m.request_id) else []


def _rate_req_day(m, c):
    cap = _g(c.cfg, "guards", "rate", "per_request_per_day", default=3)
    return ["per-request daily cap"] if len(_sent(c.db, _day_start(c.now), request_id=m.request_id)) >= cap else []


def _rate_chat_hour(m, c):
    if m.chat_id is None:
        return []
    cap = _g(c.cfg, "guards", "rate", "per_chat_per_hour", default=10)
    return ["per-chat hourly cap"] if len(_sent(c.db, utc(c.now) - timedelta(hours=1), chat_id=m.chat_id)) >= cap else []


def _rate_total(m, c):
    cap = _g(c.cfg, "guards", "rate", "total_per_day", default=40)
    return ["total daily cap"] if len(_sent(c.db, _day_start(c.now))) >= cap else []


def _holding_once(m, c):
    if m.kind not in HOLDING_KINDS:
        return []
    cap = _g(c.cfg, "guards", "rate", "holding_per_request", default=1)
    rows = _sent(c.db, c.now - timedelta(days=3650), request_id=m.request_id)
    return ["holding reply already sent"] if sum(1 for r in rows if r.get("kind") == str(m.kind)) >= cap else []


def _system_text(m, c):
    return T.system_text(m.text, _g(c.cfg, "guards", "system_text"))


def _secrets(m, c):
    return T.secrets(m.text + "\n" + m.subject, c.vault_hashes)


def _personal(m, c):
    allowed = list(_g(c.cfg, "guards", "tickets", "recipients", default=[]) or [])
    return T.personal_data(m.text + "\n" + m.subject, allowed)


def _links(m, c):
    return T.links(m.text, _g(c.cfg, "guards", "link_allow", default=[]) or [])


def _promises(m, c):
    if c.trigger == SendTrigger.principal_edit:
        return []                                   # Principal typed it himself
    filled = m.filled_date if m.is_template else None
    # Principal 2026-11-10: his Approve was blocked by "on 2 Nov", a fact in the reply, not a promise. On his Approve
    # tap (he read the exact text; the button is bound to its hash) dates pass; promise words still block.
    # Autopilot sends keep the full check.
    return T.promises(m.text, _g(c.cfg, "guards", "promise_words"), filled,
                      check_dates=c.trigger != SendTrigger.approval)


def _persona(m, c):
    if m.kind == OutKind.ticket_email:
        want = False                                # client voice to Acme, never the marker
    elif c.trigger in AUTONOMOUS:
        want = True                                 # autopilot always signs with the marker
    else:
        want = None if m.is_template else False     # Principal's tap: his own words (a fixed template may keep it)
    marker = _g(c.cfg, "persona", "marker", default="- Principal's assistant")
    return T.persona(m.text, telegram=m.channel in TELEGRAM, marker=marker, want_marker=want)


def _approval_binding(m, c):
    if c.trigger in AUTONOMOUS:
        return []
    a = c.approval
    if not a:
        return ["no used approval for this send"]
    out = []
    principal = _g(c.cfg, "people", "principal_tg_user_id")
    if not principal or int(a.get("used_by") or 0) != int(principal) or int(a.get("allowed_user_id") or 0) != int(principal):
        out.append("only Principal's own tap can send as Principal")
    if a.get("request_id") != m.request_id:
        out.append("approval is for another request")
    if a.get("action") not in SEND_ACTIONS:
        out.append("approval action is not a send action")
    if c.trigger == SendTrigger.approval:
        if not m.draft_hash or a.get("draft_hash") != m.draft_hash or draft_hash(m.text) != m.draft_hash:
            out.append("text differs from the judged draft")
    return out


def _stale_draft(m, c):
    if m.kind in HOLDING_KINDS or not c.thread_after_draft:
        return []
    return ["new message in the thread after the draft: re-card"]


def _loop_guard(m, c):
    if m.kind in HOLDING_KINDS or c.trigger not in AUTONOMOUS or c.original_msg_ts is None:
        return []
    hours = _g(c.cfg, "guards", "loop_guard_max_age_hours", default=72)
    return ["original message older than the loop-guard window"] if elapsed(c.original_msg_ts, c.now) > timedelta(hours=hours) else []


def _ticket_recipient(m, c):
    if m.kind != OutKind.ticket_email:
        return []
    allowed = {x.lower() for x in _g(c.cfg, "guards", "tickets", "recipients", default=[]) or []}
    to = [x.lower() for x in (m.to or [])] + [x.lower() for x in (m.cc or [])]
    if m.thread_id or m.in_reply_to:
        return [] if set(to) <= allowed else ["ticket mail to a recipient that is not help@"]
    return [] if to and set(to) <= allowed else ["ticket mail to a recipient that is not help@"]


def _ticket_cap(m, c):
    if m.kind != OutKind.ticket_email:
        return []
    out = []
    per_day = _g(c.cfg, "guards", "tickets", "per_day", default=5)
    today = [r for r in _sent(c.db, _day_start(c.now)) if r.get("kind") == str(OutKind.ticket_email)]
    if len(today) >= per_day:
        out.append("ticket emails per day cap")
    mine = _sent(c.db, c.now - timedelta(days=3650), request_id=m.request_id)
    if any(r.get("send_key") == m.send_key for r in mine):
        out.append("ticket for this part already sent")
    return out


GUARDS: list[tuple[str, Callable[[OutboundMessage, GuardContext], list[str]]]] = [
    ("kill_switch", _kill), ("consent", _consent), ("send_disabled", _send_disabled), ("dial", _dial),
    ("notice_gate", _notice_gate), ("chat_allow_list", _chat_allow), ("thread_binding", _thread_binding),
    ("identity", _identity), ("human_active", _human_active), ("already_answered", _already_answered),
    ("rate_per_request_gap", _rate_gap), ("rate_per_request_day", _rate_req_day),
    ("rate_per_chat_hour", _rate_chat_hour), ("rate_total_day", _rate_total), ("holding_once", _holding_once),
    ("system_text", _system_text), ("secrets", _secrets), ("personal_data", _personal), ("links", _links),
    ("ticket_refs", lambda m, c: T.ticket_refs(m.text)), ("promises", _promises), ("persona", _persona),
    ("no_writes", lambda m, c: T.no_writes(m.text)), ("no_stage_moves", lambda m, c: T.no_stage_moves(m.text)),
    ("approval_binding", _approval_binding), ("stale_draft", _stale_draft), ("loop_guard", _loop_guard),
    ("ticket_recipient", _ticket_recipient), ("ticket_cap", _ticket_cap),
]


def check_outbound(msg: OutboundMessage, ctx: GuardContext) -> GuardResult:
    """Run every guard. Blocked if any guard says no or crashes. Persona is a warning on Principal's own edit."""
    blocked, warnings = [], []
    for name, fn in GUARDS:
        try:
            reasons = fn(msg, ctx)
        except Exception as exc:                      # fail closed, never print values
            reasons = [f"guard error: {type(exc).__name__}"]
        if not reasons:
            continue
        if name == "persona" and ctx.trigger == SendTrigger.principal_edit:
            warnings.extend(f"persona: {r}" for r in reasons)
        else:
            blocked.append(name)
            warnings.extend(f"{name}: {r}" for r in reasons)
    return GuardResult(ok=not blocked, blocked_by=blocked, warnings=warnings)


# ---------------------------------------------------------------- self-test and CLI

def selftest() -> list[tuple[str, bool]]:
    from .guards_selftest import run_selftest
    return run_selftest()


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")  # session ids


def scan_dir(root: Path) -> list[tuple[str, int, str]]:
    """(file, line number, kind) for every secret-like value in text files under root. Never the value."""
    hits = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file() or p.suffix.lower() not in {".log", ".jsonl", ".json", ".txt", ".md", ".csv", ""}:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for f in redact.scan(line):
                if f.kind not in ("email", "phone") and not _UUID.fullmatch(line[f.start:f.end]):
                    hits.append((str(p), i, f.kind))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="guards-scan", description="Scan logs for secrets, or run the guard self-test.")
    ap.add_argument("--scan", type=Path, help="folder to scan (e.g. data\\logs)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    rc = 0
    if args.selftest:
        for name, ok in selftest():
            print(f"{name}: {'ok' if ok else 'FAIL'}")
            rc |= 0 if ok else 1
    if args.scan:
        hits = scan_dir(args.scan)
        for f, line, kind in hits:
            print(f"{f}:{line}: {kind}")
        print(f"{len(hits)} finding(s) in {args.scan}")
        rc |= 1 if hits else 0
    if not args.scan and not args.selftest:
        ap.print_help()
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
