"""`support-twin demo`: one synthetic support ticket, end to end, with every external system faked.

Nothing leaves the machine: WhatsApp, Telegram, Odoo, the knowledge sources and the model judge are the same
deterministic fakes the test-suite uses. What runs for real is the hub pipeline itself: intake, ticket creation,
classification, drafting, the human approval gate and the send rules.

    support-twin demo            # narrated run
    support-twin demo --fast     # no pauses (CI / recording)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import resources

from . import profile

T0 = datetime.fromtimestamp(1762857600, tz=timezone.utc)
DIM, BOLD, CYAN, GREEN, YELLOW, MAGENTA, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[33m", "\033[35m", "\033[0m"


class _Clock:
    def __init__(self, now: datetime):
        self.t = now

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)

    def at_least(self, t: datetime) -> None:
        self.t = max(self.t, t)


def _fixture(name: str) -> dict:
    return json.loads(resources.files("twin_agent.hub").joinpath("fixtures", name).read_text(encoding="utf-8"))


def build_hub():
    """A Hub whose every seam is a deterministic fake (the same wiring the tests use)."""
    from .hub import fakes, judge, learning, proof_runner, routing
    from .hub.odoo_gateway import GuardedOdoo
    from .hub.pipeline import Hub
    from .hub.store import MemoryStore
    from .testing.fake_odoo import FakeOdoo

    return Hub(
        odoo=GuardedOdoo(FakeOdoo(), writes_on=True),
        whatsapp=fakes.FakeWhatsApp(),
        stt=fakes.FakeSTT(),
        telegram=fakes.FakeTelegram(),
        knowledge_odoo=fakes.FakeOdooKnowledge(),
        knowledge_wiki=fakes.FakeWikiKnowledge(),
        assigner=routing.RuleAssigner(routing.default_team()),
        proof=proof_runner.FakeProofRunner(),
        metrics_connector=fakes.FakeMetrics(),
        judge=judge.FakeJudge(),
        learning=learning.FakeLearningLog(),
        cfg={"phone_number_id": "PHONE_NUMBER_ID", "client_channel": "whatsapp"},
        clock=_Clock(T0 + timedelta(minutes=1)),
        store=MemoryStore(),
    )


def _deliver(hub, payload: dict) -> dict:
    stamps = [int(m["timestamp"]) for e in payload["entry"] for c in e["changes"]
              for m in c["value"].get("messages", [])]
    hub.clock.at_least(datetime.fromtimestamp(max(stamps), tz=timezone.utc) + timedelta(minutes=1))
    return hub.handle_webhook(payload)


def _pending(hub, kind: str) -> dict:
    for req in reversed(hub.telegram.approvals):
        doc = hub.store.get("approvals", req["nonce"])
        if doc and not doc.get("used_at") and req["kind"] == kind:
            return req
    raise RuntimeError(f"no pending {kind} approval")


def run(fast: bool = False, out=None) -> int:
    out = out or sys.stdout
    color = getattr(out, "isatty", lambda: False)() or fast is None

    def say(text: str = "", pause: float = 0.6) -> None:
        if not color:
            for code in (DIM, BOLD, CYAN, GREEN, YELLOW, MAGENTA, RESET):
                text = text.replace(code, "")
        print(text, file=out, flush=True)
        if not fast:
            time.sleep(pause)

    def wrap(text: str, width: int = 74, indent: str = "      ") -> str:
        words, lines, cur = text.split(), [], ""
        for w in words:
            if len(cur) + len(w) + 1 > width:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        lines.append(cur)
        return "\n".join(indent + ln for ln in lines)

    p = profile.load()
    hub = build_hub()
    say(f"{BOLD}support-twin demo{RESET}  {DIM}dry run - synthetic data - every external system is a fake{RESET}", 0.3)
    say(f"{DIM}persona:{RESET} {p['twin_name']} of {p['principal_name']} @ {p['company_name']}  "
        f"{DIM}client:{RESET} {p['client_name']}", 1.0)

    first = _fixture("webhook_text.json")
    body = first["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"]
    name = first["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"]
    say()
    say(f"{CYAN}[1] incoming{RESET}  WhatsApp from {BOLD}{name}{RESET}:")
    say(wrap(f'"{body}"'), 1.0)

    res = _deliver(hub, first)
    tid = res.get("ticket_id")
    ticket = hub.odoo.transport.tickets.get(tid, {}) if hasattr(hub.odoo, "transport") else {}
    auto = [s for s in hub.whatsapp.sent if s["automatic"]]
    say(f"{CYAN}[2] ticket{RESET}    helpdesk ticket {BOLD}#{tid}{RESET} opened"
        + (f" - {ticket.get('name')}" if ticket.get("name") else ""))
    if auto:
        say(f"      auto-acknowledgement (the only automatic message): {DIM}{auto[0]['text'][:44]}...{RESET}")
    req = _pending(hub, "confirm")
    say(f"{CYAN}[3] draft{RESET}     understanding drafted for {p['principal_name']}:")
    say(wrap(req["draft"]), 1.2)
    sent_before = len([s for s in hub.whatsapp.sent if not s["automatic"]])
    say(f"{YELLOW}[4] gate{RESET}      approval card sent to {p['principal_name']} on Telegram "
        f"- client messages sent so far: {BOLD}{sent_before}{RESET}", 1.2)

    hub.on_owner_decision(req["nonce"], req["allowed_approvers"][0], approved=True)
    sent = [s for s in hub.whatsapp.sent if not s["automatic"]]
    say(f"{GREEN}[5] approve{RESET}   {p['principal_name']} taps Approve -> {BOLD}{len(sent)}{RESET} reply sent "
        f"(exactly the approved text, once)", 1.0)

    _deliver(hub, _fixture("webhook_confirm.json"))
    say(f"{CYAN}[6] client{RESET}    {name} confirms the understanding")
    adv = _pending(hub, "advance")
    hub.on_owner_decision(adv["nonce"], adv["allowed_approvers"][0], approved=True)
    fix = _pending(hub, "fix_ready")
    hub.on_owner_decision(fix["nonce"], fix["allowed_approvers"][0], approved=True)
    say(f"{MAGENTA}[7] proof{RESET}     fix verified with evidence before anyone claims it is done", 0.8)
    rs = _pending(hub, "resolve")
    say(f"{YELLOW}[8] gate{RESET}      resolution drafted, waiting for {p['principal_name']}:")
    say(wrap(rs["draft"]), 1.0)
    final = hub.on_owner_decision(rs["nonce"], rs["allowed_approvers"][0], approved=True)
    total = len([s for s in hub.whatsapp.sent if not s["automatic"]])
    say(f"{GREEN}[9] resolved{RESET}  ticket #{tid} -> {BOLD}{final.get('stage') or final.get('status') or 'resolved'}{RESET}; "
        f"{total} client replies, every one approved by a human", 1.0)
    say()
    say(f"{DIM}audit trail: {len(hub.telegram.approvals)} approval requests, "
        f"{len(hub.telegram.posts)} internal notes, 0 messages sent without a human tap{RESET}", 0.5)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="support-twin demo", description=__doc__.splitlines()[0])
    ap.add_argument("--fast", action="store_true", help="no pauses")
    a = ap.parse_args(argv)
    return run(fast=a.fast)


if __name__ == "__main__":
    raise SystemExit(main())
