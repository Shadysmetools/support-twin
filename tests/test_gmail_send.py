"""gmail_send: recipient rules in code, threading headers, re-check before send, refuses outside live."""
from __future__ import annotations

import base64
import copy
import email
from pathlib import Path

import pytest
import yaml

from twin_agent.channels import gmail_common as gc
from twin_agent.channels.gmail_send import RealGmailSend, RecipientRefused, build_raw, check_recipients
from twin_agent.models import Channel, OutboundMessage, OutKind
from gmail_fakes import FakeService

REPO = Path(__file__).resolve().parents[1]
HELP = "help@example.com"


@pytest.fixture
def scfg(monkeypatch) -> dict:
    from twin_agent import creds
    monkeypatch.setattr(creds, "has_secret", lambda name, **k: True)
    c = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    c["run_mode"] = "live"
    c["mailboxes"]["ticketmail"]["address"] = "principal@example.org"
    c["mailboxes"]["ticketmail"]["scopes"] = ["gmail.readonly", "gmail.compose"]
    return c


def ticket(**kw) -> OutboundMessage:
    base = dict(request_id="REQ-0150", step="ticket-a", kind=OutKind.ticket_email, channel=Channel.ticket_mail,
                text="Request\n- x\n\nref: REQ-0150", to=[HELP], subject="[Globex EG] Fix X")
    base.update(kw)
    return OutboundMessage(**base)


def test_recipient_rules(scfg):
    assert check_recipients(ticket(), scfg) == []
    assert check_recipients(ticket(to=["someone@example.org"]), scfg)
    assert check_recipients(ticket(cc=["boss@example.org"]), scfg)
    assert check_recipients(ticket(thread_id="t9"), scfg)              # a new ticket = new thread
    upd = ticket(kind=OutKind.ticket_update, step="upd-1", thread_id="t9", in_reply_to="<x@y>")
    assert check_recipients(upd, scfg, {"t9": [HELP]}) == []
    assert check_recipients(upd, scfg, {}) == ["ticket update must stay on a known ticket thread"]
    reply = ticket(kind=OutKind.email_reply, to=["staff@example.org"], thread_id="t1")
    assert check_recipients(reply, scfg, {"t1": ["staff@example.org"]}) == []
    assert check_recipients(ticket(kind=OutKind.email_reply, to=["new@example.org"], thread_id="t1"),
                            scfg, {"t1": ["staff@example.org"]})
    assert check_recipients(ticket(kind=OutKind.holding), scfg)        # telegram kinds never go by Gmail
    assert check_recipients(ticket(), scfg, bcc=["x@example.org"])


def test_build_raw_threading():
    msg = ticket(kind=OutKind.ticket_update, subject="Re: [Globex EG] Fix X (#9001)", in_reply_to="<abc@odoo>")
    em = email.message_from_bytes(base64.urlsafe_b64decode(build_raw(msg, "principal@example.org")))
    assert em["In-Reply-To"] == "<abc@odoo>" and em["References"] == "<abc@odoo>"
    assert em["To"] == HELP and em["X-Principal-Req"] == "REQ-0150" and em["Subject"].startswith("Re:")


def test_refuses_without_live_or_compose(scfg):
    dry = copy.deepcopy(scfg)
    dry["run_mode"] = "dry_run"
    with pytest.raises(gc.AdapterRefused):
        RealGmailSend(dry, service_factory=lambda c, m: FakeService())
    ro = copy.deepcopy(scfg)
    ro["mailboxes"]["ticketmail"]["scopes"] = ["gmail.readonly"]
    with pytest.raises(gc.AdapterRefused):
        RealGmailSend(ro, service_factory=lambda c, m: FakeService())


def test_draft_then_send(scfg):
    svc = FakeService()
    gs = RealGmailSend(scfg, service_factory=lambda c, m: svc)
    did = gs.create_draft(ticket())
    assert svc.drafts_[did]["threadId"] == ""
    assert gs.send_draft(did) == "g1" and svc.sent == [did]


def test_bad_recipient_never_drafted(scfg):
    svc = FakeService()
    gs = RealGmailSend(scfg, service_factory=lambda c, m: svc)
    with pytest.raises(RecipientRefused):
        gs.create_draft(ticket(to=["x@example.org"]))
    assert svc.drafts_ == {}


def test_edited_draft_rechecked_before_send(scfg):
    svc = FakeService()
    gs = RealGmailSend(scfg, service_factory=lambda c, m: svc)
    did = gs.create_draft(ticket())
    svc.draft_headers_override[did] = [{"name": "To", "value": HELP}, {"name": "Cc", "value": "x@example.org"}]
    with pytest.raises(RecipientRefused):
        gs.send_draft(did)
    assert svc.sent == []


def test_unknown_draft_after_restart_held_to_help_only(scfg):
    svc = FakeService()
    gs1 = RealGmailSend(scfg, service_factory=lambda c, m: svc)
    did = gs1.create_draft(ticket())
    gs2 = RealGmailSend(scfg, service_factory=lambda c, m: svc)      # new process, empty memory
    assert gs2.send_draft(did) == "g1"
    did2 = gs1.create_draft(ticket(step="ticket-b"))
    svc.draft_headers_override[did2] = [{"name": "To", "value": "x@example.org"}]
    with pytest.raises(RecipientRefused):
        gs2.send_draft(did2)
