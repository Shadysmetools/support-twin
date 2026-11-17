"""Review #3 + #4 + #10: owner approvals are asynchronous, bound to approver + draft, single use, expiring; the edited
text is re-scanned and re-judged; and the 24h window is checked against the REAL now at send time."""
from __future__ import annotations

import hashlib

from twin_agent.hub import fakes, routing
from twin_agent.hub.model import Owner
from hub_helpers import approve, build_hub, deliver, pending

SUPERVISOR_TG = 1003


def _creates(hub) -> int:
    return sum(1 for w in hub.odoo.t.writes if w["method"] == "create")


def _confirm_msgs(hub) -> list[dict]:
    return [s for s in hub.whatsapp.sent if not s["automatic"]]


def test_confirm_is_pending_after_the_webhook_and_sent_on_a_later_decision():
    hub = build_hub()
    res = deliver(hub, "webhook_text.json")
    # the webhook call returned WITHOUT blocking on the owner: ticket waits, only the ack went out
    assert res["stage"] == "awaiting_owner" and len(hub.whatsapp.sent) == 1 and hub.whatsapp.sent[0]["automatic"]
    req = pending(hub, "confirm")
    assert req and req["ticket_id"] == res["ticket_id"] and SUPERVISOR_TG in req["allowed_approvers"]
    assert req["draft_hash"] == hashlib.sha256(req["draft"].encode()).hexdigest()
    # a separate call, later: the owner approves
    out = approve(hub, "confirm")
    assert out["ok"] is True and out["stage"] == "awaiting_customer"
    assert len(_confirm_msgs(hub)) == 1 and _confirm_msgs(hub)[0]["text"] == req["draft"]
    assert hub.latest_ticket("201000000001").stage == "awaiting_customer"


def test_nothing_is_sent_without_an_owner_decision():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    assert len(hub.whatsapp.sent) == 1 and hub.whatsapp.automatic_count() == 1


def test_approver_who_is_not_the_owner_is_refused_and_does_not_burn_the_nonce():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    out = approve(hub, "confirm", as_tg=999999)
    assert out["ok"] is False and out["reason"] == "not_authorised" and len(hub.whatsapp.sent) == 1
    assert approve(hub, "confirm")["ok"] is True                    # the real owner can still approve


def test_owner_with_no_tg_id_cannot_be_impersonated_by_id_zero():
    team = [routing.Candidate(Owner(uid=778, name="Supervisor", tg_id=0, role="lead"),
                              skills=("Inventory",), handles_tracks=("functional", "development"))]
    hub = build_hub(assigner=routing.RuleAssigner(team))
    deliver(hub, "webhook_text.json")
    req = pending(hub, "confirm")
    assert 0 not in req["allowed_approvers"]
    assert approve(hub, "confirm", as_tg=0)["ok"] is False and len(hub.whatsapp.sent) == 1


def test_allowlisted_approver_can_approve():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approver_tg_ids": [4242]})
    deliver(hub, "webhook_text.json")
    assert 4242 in pending(hub, "confirm")["allowed_approvers"]
    assert approve(hub, "confirm", as_tg=4242)["ok"] is True


def test_nonce_is_single_use():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    req = pending(hub, "confirm")
    assert approve(hub, nonce=req["nonce"], as_tg=SUPERVISOR_TG)["ok"] is True
    again = approve(hub, nonce=req["nonce"], as_tg=SUPERVISOR_TG)
    assert again["ok"] is False and again["reason"] == "used" and len(_confirm_msgs(hub)) == 1


def test_unknown_nonce_is_refused():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    assert hub.on_owner_decision("nope", SUPERVISOR_TG)["ok"] is False


def test_approval_expires():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 1})
    deliver(hub, "webhook_text.json")
    hub.clock.advance(hours=2)                                      # still inside the 24h window, past the expiry
    out = approve(hub, "confirm")
    assert out["ok"] is False and out["reason"] == "expired" and len(_confirm_msgs(hub)) == 0


def test_approval_is_bound_to_the_draft_hash():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    req = pending(hub, "confirm")
    hub.store.update("approvals", req["nonce"], lambda d: {**d, "draft": "something the owner never saw"})
    out = approve(hub, nonce=req["nonce"], as_tg=SUPERVISOR_TG)
    assert out["ok"] is False and out["reason"] == "draft_mismatch" and _confirm_msgs(hub) == []


def test_rejected_draft_sends_nothing_and_goes_to_the_hold_queue():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    out = approve(hub, "confirm", approved=False)
    assert out["ok"] is True and out["sent"] is False and _confirm_msgs(hub) == []
    assert [h["reason"] for h in hub.holds()] == ["draft_rejected"] and hub.telegram.triage


def test_clean_edit_is_sent_as_edited_and_recorded():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    edited = "Hello Oscar, we are checking the stock figures in Inventory today and will update you. — Acme Support"
    out = approve(hub, "confirm", text=edited)
    assert out["ok"] is True and _confirm_msgs(hub)[0]["text"] == edited
    assert hub.latest_ticket("201000000001").human_edits is True


def test_edit_that_adds_a_phone_number_or_secret_is_blocked_and_redrafted():
    for bad in ("Hello Oscar, call me on +20 100 123 4567 about the stock issue. — Acme Support",
                "Hello Oscar, use key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123 to log in. — Acme Support"):
        hub = build_hub()
        deliver(hub, "webhook_text.json")
        original = pending(hub, "confirm")
        out = approve(hub, "confirm", text=bad)
        assert out["ok"] is False and out["reason"] == "edit_contains_sensitive_data"
        assert _confirm_msgs(hub) == []
        fresh = pending(hub, "confirm")                             # the owner gets the ORIGINAL draft again
        assert fresh and fresh["nonce"] != original["nonce"] and fresh["draft"] == original["draft"]


def test_edit_the_judge_flags_needs_a_fresh_approval_of_the_exact_text():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    flagged = "Obviously this is guaranteed to work 100% forever. — Acme Support"
    out = approve(hub, "confirm", text=flagged)
    assert out["ok"] is False and out["reason"] == "edited_text_flagged" and _confirm_msgs(hub) == []
    fresh = pending(hub, "confirm")
    assert fresh["draft"] == flagged and fresh["draft_hash"] == hashlib.sha256(flagged.encode()).hexdigest()
    assert approve(hub, "confirm")["ok"] is True and _confirm_msgs(hub)[0]["text"] == flagged


def test_draft_with_an_unfilled_placeholder_is_never_sent():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    out = approve(hub, "confirm", text="Hello Oscar, [[ANSWER]] — Acme Support")
    assert out["ok"] is False and _confirm_msgs(hub) == []


# ---------------------------------------------------------------- #10: the 24h window is real

def test_ack_outside_the_window_is_skipped_not_crashed():
    hub = build_hub()
    hub.clock.advance(hours=25)                                     # processing happens 25h after the message
    res = deliver(hub, "webhook_text.json")
    assert res["ticket_id"] and hub.whatsapp.sent == []
    assert any("window" in p["text"].lower() for p in hub.telegram.posts)


LONG_TTL = {"phone_number_id": "PHONE_NUMBER_ID", "approval_ttl_hours": 72}   # so the WINDOW, not expiry, decides


def test_approved_send_outside_the_window_is_held_not_sent():
    hub = build_hub(cfg=LONG_TTL)
    deliver(hub, "webhook_text.json")
    hub.clock.advance(hours=25)                                     # the owner approves a day later
    out = approve(hub, "confirm")
    assert out["ok"] is False and out["reason"] == "window_closed"
    assert _confirm_msgs(hub) == [] and [h["reason"] for h in hub.holds()] == ["window_closed"]
    # TODO(template): outside the window an approved WhatsApp template is required (Meta); not built in Phase 1.


def test_a_later_customer_message_reopens_the_window_for_sends():
    hub = build_hub(cfg=LONG_TTL)
    deliver(hub, "webhook_text.json")
    hub.clock.advance(hours=23)
    from hub_helpers import msg
    deliver(hub, msg("late1", "any update?", ts=int(hub.clock().timestamp()) - 10))   # a fresh inbound at ~23h
    hub.clock.advance(hours=2)                                      # 25h after the FIRST message, 2h after the last
    assert approve(hub, "confirm")["ok"] is True


# ---------------------------------------------------------------- durable state

def test_pending_approval_survives_a_restart_with_the_sqlite_store(tmp_path):
    from twin_agent.hub.store import SqliteStore
    from twin_agent.testing.fake_odoo import FakeOdoo
    transport = FakeOdoo()
    wa, tg = fakes.FakeWhatsApp(), fakes.FakeTelegram()
    db = str(tmp_path / "hub.db")
    s1 = SqliteStore(db)
    hub1 = build_hub(store=s1, transport=transport, whatsapp=wa, telegram=tg)
    deliver(hub1, "webhook_text.json")
    nonce = pending(hub1, "confirm")["nonce"]
    s1.close()                                                      # "the process died"
    s2 = SqliteStore(db)
    hub2 = build_hub(store=s2, transport=transport, whatsapp=wa, telegram=tg)
    hub2.clock.at_least(hub1.clock())
    try:
        out = hub2.on_owner_decision(nonce, SUPERVISOR_TG)
        assert out["ok"] is True and len(_confirm_msgs(hub2)) == 1
        assert hub2.latest_ticket("201000000001").stage == "awaiting_customer"
    finally:
        s2.close()
