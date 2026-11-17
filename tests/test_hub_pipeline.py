"""Acceptance tests — one per pipeline stage (baton: "each stage gets an acceptance test"), driven through the
public ``Hub.handle_webhook`` / ``Hub.on_owner_decision`` with fakes. No network, no creds.

Since the approval rework (review #3/#4) the owner's tap is its OWN call after the webhook returned: stages 8-10 are
driven with ``hub_helpers.approve``. The ack is still the only automatic client message."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from twin_agent.helpdesk import guard
from twin_agent.hub import webhook
from twin_agent.hub.model import Owner
from hub_helpers import approve, build_hub, deliver, msg, run_to_resolved

FIX = Path(__file__).parent.parent / "twin_agent" / "hub" / "fixtures"
PHONE = "201000000001"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _first(hub):
    return deliver(hub, "webhook_text.json")


# ---------------------------------------------------------------- stage 1: RECEIVE

def test_stage1_receive_parses_text_and_voice():
    t = webhook.parse(_load("webhook_text.json"))
    assert t.kind == "text" and t.wa_id == PHONE and t.message_id == "wamid.TEXT0001"
    v = webhook.parse(_load("webhook_voice.json"))
    assert v.kind == "voice" and v.audio_id == "MEDIA_AUDIO_0001"
    assert webhook.parse(_load("webhook_status.json")) is None   # status callback -> ignored


# ---------------------------------------------------------------- stage 2: STT

def test_stage2_stt_transcribes_voice_note_and_keeps_audio():
    hub = build_hub()
    res = deliver(hub, "webhook_voice.json")
    stt = [s for s in res["log"] if s["stage"] == "2-STT"][0]
    assert stt["status"] == "ok" and stt["placeholder"] is True and stt["audio_retained"] is True
    assert res["ticket_id"]                                      # Arabic transcript -> an Arabic ticket got created


def test_stage2_real_stt_blocks_without_key():
    from twin_agent.hub.adapters_real import RealSTT
    assert RealSTT().transcribe("X")["status"] == "blocked"   # conftest drops GROQ_API_KEY


# ---------------------------------------------------------------- stage 3: ANALYSE

def test_stage3_analyse_classifies_issue_type_module():
    hub = build_hub()
    res = _first(hub)
    a = [s for s in res["log"] if s["stage"] == "3-ANALYSE"][0]
    assert a["nature"] == "issue" and a["ticket_type"] == "Bug" and a["module"] == "Inventory"


# ---------------------------------------------------------------- stage 4: CREATE (mail suppressed, guarded)

def test_stage4_create_ticket_mail_suppressed_in_safe_stage():
    transport = _fresh_transport()
    hub = build_hub(transport=transport)
    _first(hub)
    create = [w for w in transport.writes if w["method"] == "create" and w["model"] == "helpdesk.ticket"][0]
    assert create["vals"]["stage_id"] in hub.conv.safe_stages      # never stage 1 "New"
    assert create["vals"]["team_id"] in (2, 11)                    # teams 2 & 11
    assert create["vals"]["partner_id"] == 3001


def test_stage4_create_refuses_without_mail_suppress_context():
    from twin_agent.hub.odoo_gateway import GuardedOdoo
    gw = GuardedOdoo(_fresh_transport(), writes_on=True)
    with pytest.raises(guard.WriteRefused):
        gw.create_ticket({"name": "x", "stage_id": 14, "team_id": 2, "partner_id": 3001}, context={})


# ---------------------------------------------------------------- stage 5: ASSIGN (team-aware)

def test_stage5_assign_sets_user_id_and_rationale():
    transport = _fresh_transport()
    hub = build_hub(transport=transport)
    res = _first(hub)
    a = [s for s in res["log"] if s["stage"] == "5-ASSIGN"][0]
    assert a["owner"] == "Supervisor" and a["uid"] == 778         # owns this client + Inventory module
    assert "because" in hub.latest_ticket(PHONE).rationale
    assert any(w["method"] == "write" and w["vals"] == {"user_id": 778} for w in transport.writes)


# ---------------------------------------------------------------- stage 6: TELEGRAM topic + brief

def test_stage6_opens_topic_with_wiki_context_in_brief():
    hub = build_hub()
    _first(hub)
    assert len(hub.telegram.topics) == 1
    brief = next(iter(hub.telegram.topics.values()))["brief"]
    assert "assigned to Supervisor because" in brief and "Wiki:" in brief


# ---------------------------------------------------------------- stage 7: ACK (only automatic client message)

def test_stage7_ack_is_the_only_automatic_message():
    hub = build_hub()
    _first(hub)
    wa = hub.whatsapp
    assert wa.automatic_count() == 1                         # exactly one auto message after first contact
    assert "ticket #" in wa.sent[0]["text"].lower()
    assert "Acme Support" in wa.sent[0]["text"]


# ---------------------------------------------------------------- stage 8: CONFIRM (owner-approved, then pauses)

def test_stage8_confirm_owner_approved_then_pauses():
    hub = build_hub()
    res = _first(hub)
    assert res["stage"] == "awaiting_owner"                  # drafted, NOT sent: the owner has not tapped yet
    approve(hub, "confirm")
    assert hub.latest_ticket(PHONE).stage == "awaiting_customer"
    # one auto ack + one owner-approved confirm = 2 sends, exactly one automatic
    assert len(hub.whatsapp.sent) == 2 and hub.whatsapp.automatic_count() == 1
    assert not hub.whatsapp.sent[1]["automatic"]


def test_stage8_confirm_not_sent_without_owner_approval():
    hub = build_hub()
    _first(hub)
    assert len(hub.whatsapp.sent) == 1 and hub.whatsapp.sent[0]["automatic"] is True   # only the auto ack went out


# ---------------------------------------------------------------- stage 9: PROVE-IT

def test_stage9_proveit_runs_when_the_owner_says_the_fix_is_ready():
    hub = build_hub()
    _first(hub)
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")                      # the customer confirms (attached, nothing moves yet)
    assert hub.proof.calls == []
    approve(hub, "advance")                                   # the owner marks it confirmed: work starts
    assert hub.proof.calls == []
    approve(hub, "fix_ready")                                 # the owner: fix is ready -> PROVE-IT
    assert len(hub.proof.calls) == 1
    p = hub.latest_ticket(PHONE).proof
    assert p["status"] == "prepared" and p["share_link"]


# ---------------------------------------------------------------- stage 10: RESOLVE (video attached as a note)

def test_stage10_resolve_sends_video_and_attaches_to_ticket():
    transport = _fresh_transport()
    hub = build_hub(transport=transport)
    out = run_to_resolved(hub)
    assert out["sent"] is True and out["stage"] == "resolved"
    assert "wiki.invalid" in hub.whatsapp.sent[-1]["text"]
    notes = [w for w in transport.writes if w["method"] == "message_post"]
    assert any("Proof video attached" in (w["kwargs"].get("body") or "") for w in notes)


def test_resolve_binary_attachment_is_refused_by_guard():
    """The proof video is attached as a NOTE with its share link; a raw binary ir.attachment is OUT of the guard
    allowlist (tracked follow-up: widen guard for binaries)."""
    with pytest.raises(guard.WriteRefused):
        guard.check("helpdesk", "ir.attachment", "create", [{"name": "proof.mp4", "res_id": 1}])


# ---------------------------------------------------------------- stage 11: METRICS-KPIs

def test_stage11_metrics_kpis_pushed_on_resolve():
    hub = build_hub()
    run_to_resolved(hub)
    assert len(hub.metrics.pushed) == 1
    out = hub.metrics.pushed[0]["outcome"]
    assert out["resolved"] is True and out["ticket_id"] and out["proof_outcome"] == "reproduced"


# ---------------------------------------------------------------- idempotency (A2) + 24h window

def test_all_sends_respect_the_24h_window():
    """The ack, an approved CONFIRM and an approved RESOLVE are each checked against the REAL now and the stored
    last-inbound time (the old test only covered the ack, with window_hours=0)."""
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 72})
    hub.clock.advance(hours=25)
    _first(hub)                                               # ack: window already closed
    assert hub.whatsapp.sent == []
    hub2 = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 72})
    _first(hub2)
    hub2.clock.advance(hours=25)
    assert approve(hub2, "confirm")["reason"] == "window_closed"
    assert [s["automatic"] for s in hub2.whatsapp.sent] == [True]       # only the ack went out


def test_idempotent_on_replayed_message_id():
    transport = _fresh_transport()
    hub = build_hub(transport=transport)
    _first(hub)
    again = _first(hub)
    assert again.get("duplicate") is True
    creates = [w for w in transport.writes if w["method"] == "create" and w["model"] == "helpdesk.ticket"]
    assert len(creates) == 1                                   # no second ticket


def test_unknown_sender_is_held_for_a_human():
    from twin_agent.hub import fakes
    hub = build_hub(knowledge_odoo=fakes.FakeOdooKnowledge(partners={}))
    res = _first(hub)
    assert res.get("held") == "unknown_sender"


# ---------------------------------------------------------------- one-tap reassign (§15b override)

def test_one_tap_reassign_writes_back_and_logs_signal():
    transport = _fresh_transport()
    hub = build_hub(transport=transport)
    _first(hub)
    out = hub.reassign(PHONE, Owner(uid=777, name="Principal", tg_id=1001), actor_tg_id=1003)         # by sender id (latest ticket) ...
    assert out["to"] == "Principal"
    assert hub.learning.signals and hub.learning.signals[0]["kind"] == "reassignment_override"
    assert any(w["method"] == "write" and w["vals"] == {"user_id": 777} for w in transport.writes)
    assert hub.reassign(hub.latest_ticket(PHONE).tid, Owner(uid=778, name="Supervisor"), actor_tg_id=1001)["to"] == "Supervisor"  # ... or ticket id


# ---------------------------------------------------------------- mail-suppress context at the transport

class _SpyOdoo:
    """Wraps FakeOdoo to capture the kwargs (incl. the mail-suppress context) that reach execute_kw."""

    def __init__(self):
        from twin_agent.testing.fake_odoo import FakeOdoo
        self._inner = FakeOdoo()
        self.kw_calls = []

    uid = property(lambda self: self._inner.uid)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute_kw(self, model, method, args, kwargs):
        self.kw_calls.append((model, method, dict(kwargs or {})))
        return self._inner.execute_kw(model, method, args, kwargs)


def test_mail_suppress_context_reaches_the_transport():
    spy = _SpyOdoo()
    hub = build_hub(transport=spy)
    _first(hub)
    create = [k for k in spy.kw_calls if k[0] == "helpdesk.ticket" and k[1] == "create"][0]
    ctx = create[2].get("context") or {}
    assert ctx.get("tracking_disable") and ctx.get("mail_create_nosubscribe") and ctx.get("mail_create_nolog")


# ---------------------------------------------------------------- no false resolution without owner approval

def test_awaiting_owner_does_not_resume_on_customer_reply():
    hub = build_hub()
    first = _first(hub)
    assert first["stage"] == "awaiting_owner"            # CONFIRM not sent (owner hasn't approved)
    res = deliver(hub, "webhook_confirm.json")           # a customer reply must NOT be taken as confirmation
    assert res.get("attached") is True and hub.metrics.pushed == [] and hub.learning.records == []
    assert hub.proof.calls == [] and hub.latest_ticket(PHONE).stage == "awaiting_owner"


def test_no_false_resolution_when_resolve_not_approved():
    hub = build_hub()
    _first(hub)
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")
    approve(hub, "advance")
    approve(hub, "fix_ready")                            # PROVE-IT ok, RESOLVE drafted ...
    approve(hub, "resolve", approved=False)              # ... but the owner does not approve it
    assert hub.metrics.pushed == [] and hub.learning.records == []
    assert hub.latest_ticket(PHONE).stage == "awaiting_owner_resolve"
    assert len([s for s in hub.whatsapp.sent if "resolved" in s["text"].lower()]) == 0


# ---------------------------------------------------------------- fixtures/helpers

def _fresh_transport():
    from twin_agent.testing.fake_odoo import FakeOdoo
    return FakeOdoo()
