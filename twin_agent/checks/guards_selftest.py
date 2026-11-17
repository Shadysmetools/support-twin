"""Guard self-test: canned cases that must pass before send_enabled may be switched on (06 I4).

Uses an in-memory stand-in for the db, synthetic ids only, and never touches the network.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..clock import CAIRO
from ..models import Channel, OutboundMessage, OutKind, SendTrigger
from .fixgate import draft_hash, recompute_exemption

PRINCIPAL, CHAT, TOPIC = 1001, -100500, 7
NOW = datetime(2026, 11, 1, 14, 40, tzinfo=CAIRO)
MARKER = "- Principal's assistant"


class MiniDB:
    """Just enough of StateDB for the guards: sends_since, get_request, get_dial."""

    def __init__(self, sends: list[dict] | None = None):
        self.sends = sends or []

    def sends_since(self, since, *, chat_id=None, request_id=None, statuses=("sent",)):
        return [s for s in self.sends if s["ts"] >= since and s["status"] in statuses
                and (chat_id is None or str(s.get("chat_id")) == str(chat_id))
                and (request_id is None or s.get("request_id") == request_id)]

    def get_request(self, request_id):
        return {"id": request_id, "chat_id": str(CHAT), "topic_id": TOPIC}

    def get_dial(self, lane):
        raise LookupError("not stored")


def live_cfg() -> dict:
    """A config where everything is switched on, so each case tests one guard."""
    return {
        "run_mode": "live", "send_enabled": True,
        "people": {"principal_tg_user_id": PRINCIPAL, "backup_tg_user_ids": [1002]},
        "chats": [{"name": "G", "chat_id": CHAT, "read": True, "topics": [{"id": TOPIC, "name": "Logistics"}]}],
        "dial": {lane: "auto" for lane in (
            "L1_holding", "L2_evidence_ask", "L3_t1_answer", "L4_t2a_fix_proposal", "L5_t2b_config",
            "L6_ticket_email", "L7_ticket_chase", "L8_approver_question", "L9_worked_followup",
            "L10_proof_video", "L11_internal_diagnosis", "L12_email_reply")},
        "consent": {"d1_ref": "D1-test", "channels": {"telegram": True, "email": True, "tickets": True},
                    "notice_gate": {"sysadmin_b1_ref": "b1", "executive_b2_ref": "b2"}, "signatory_must_approve": False},
        "guards": {"rate": {"per_request_min_gap_min": 10, "per_request_per_day": 3, "per_chat_per_hour": 10,
                            "total_per_day": 40, "holding_per_request": 1},
                   "tickets": {"per_part": 1, "per_day": 5, "recipients": ["help@example.com"]},
                   "loop_guard_max_age_hours": 72,
                   "link_allow": ["https://helpdesk.example.com/", "https://wiki.example.com/share/"]},
        "timers": {"human_active_lock_min": 5},
        "persona": {"marker": MARKER},
    }


def msg(text: str, *, kind=OutKind.holding, step="holding", **kw) -> OutboundMessage:
    base = dict(request_id="REQ-0001", step=step, kind=kind, channel=Channel.telegram_group, text=text,
                chat_id=CHAT, topic_id=TOPIC, reply_to=42)
    base.update(kw)
    return OutboundMessage(**base)


def ctx(cfg=None, *, trigger=SendTrigger.holding_floor, db=None, **kw):
    from .guards import GuardContext
    return GuardContext(cfg=cfg or live_cfg(), db=db or MiniDB(), now=NOW, trigger=trigger,
                        kill_switch_on=kw.pop("kill", False), **kw)


def approval_for(text: str, user=PRINCIPAL, action="ap") -> dict:
    return {"request_id": "REQ-0001", "action": action, "draft_hash": draft_hash(text),
            "allowed_user_id": user, "used_by": user}


HOLD = f"Received, checking it now.\n{MARKER}"


def _cases():
    from .guards import check_outbound as chk
    yield "clean holding passes", chk(msg(HOLD), ctx()).ok
    yield "kill switch blocks", "kill_switch" in chk(msg(HOLD), ctx(kill=True)).blocked_by
    off = live_cfg(); off["send_enabled"] = False
    yield "send flag off blocks", "send_disabled" in chk(msg(HOLD), ctx(off)).blocked_by
    yield "em dash blocks", "persona" in chk(msg("Done — ok\n" + MARKER), ctx()).blocked_by
    yield "missing marker blocks autopilot", "persona" in chk(msg("Received."), ctx()).blocked_by
    yield "Arabic script blocks on Telegram", "persona" in chk(msg("تم\n" + MARKER), ctx()).blocked_by
    yield "secret blocks", "secrets" in chk(msg("key bearer abcdefghijklmnop0123456789\n" + MARKER), ctx()).blocked_by
    yield "unknown chat blocks", "chat_allow_list" in chk(msg(HOLD, chat_id=-999), ctx()).blocked_by
    yield "promise blocks", "promises" in chk(msg("Fixed today.\n" + MARKER), ctx()).blocked_by
    yield "shell block blocks", "no_writes" in chk(msg("run env['stock.move'].unlink()\n" + MARKER), ctx()).blocked_by
    yield "bare ticket number blocks", "ticket_refs" in chk(msg("See #2641\n" + MARKER), ctx()).blocked_by
    text = "Try again now."
    a = approval_for(text, user=1002)
    m = msg(text, kind=OutKind.answer, step="draft-1", draft_hash=draft_hash(text))
    yield "backup cannot approve as Principal", "approval_binding" in chk(m, ctx(trigger=SendTrigger.approval, approval=a)).blocked_by
    a = approval_for(text)
    yield "Principal approval passes", chk(m, ctx(trigger=SendTrigger.approval, approval=a)).ok
    old = NOW - timedelta(hours=80)
    yield "loop guard spares holding", chk(msg(HOLD), ctx(original_msg_ts=old)).ok
    ans = msg(f"Try again now.\n{MARKER}", kind=OutKind.answer, step="auto-1")
    yield "loop guard blocks old answer", "loop_guard" in chk(ans, ctx(trigger=SendTrigger.timeout, original_msg_ts=old)).blocked_by
    later = ({"is_principal": 0, "ts": NOW},)
    yield "stale draft re-cards", "stale_draft" in chk(m, ctx(trigger=SendTrigger.approval, approval=a,
                                                               thread_after_draft=later)).blocked_by
    sent = [{"ts": NOW - timedelta(hours=1), "status": "sent", "request_id": "REQ-0001", "kind": "holding",
             "chat_id": CHAT}]
    yield "holding only once", "holding_once" in chk(msg(HOLD), ctx(db=MiniDB(sent))).blocked_by

    class Broken(MiniDB):
        def sends_since(self, *a, **k):
            raise RuntimeError("db down")
    yield "crashing guard fails closed", not chk(msg(HOLD), ctx(db=Broken())).ok
    packet = {"class": "RB x", "proposed_action": {"type": "run_this", "records": ["a"], "approver": "exempt"}}
    yield "packet cannot exempt itself", recompute_exemption(packet, {}, set(), "x") == "supervisor"


def run_selftest() -> list[tuple[str, bool]]:
    out = []
    try:
        for name, ok in _cases():
            out.append((name, bool(ok)))
    except Exception as exc:
        out.append((f"selftest crashed: {type(exc).__name__}", False))
    return out
