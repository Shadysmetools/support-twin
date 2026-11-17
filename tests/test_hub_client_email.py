"""Client follow-ups go by EMAIL from the helpdesk team alias after owner approval; the guard's new narrow comment rule
refuses everything else. FAKES only: no real send, no real address."""
from __future__ import annotations

import hashlib

import pytest

from twin_agent.helpdesk import guard
from twin_agent.hub import store as store_mod
from hub_helpers import approve, build_hub, deliver, msg, pending
from test_hub_person_odoo import Rig

HELP, INFO = "help@alias.example.org", "info@alias.example.org"
CFG = {"phone_number_id": "PHONE_NUMBER_ID", "client_channel": "email", "email_from_by_team": {2: HELP},
       "email_from_default": INFO}
BODY = "Hello, we are looking into it. Acme Support"


def _sha(b):
    return hashlib.sha256(b.encode()).hexdigest()


def _gate(**over):
    g = {"nonce_consumed": True, "body_sha": _sha(BODY), "aliases": [HELP, INFO], "partner_id": 3001,
         "recipients_verified": True, "plain_body": BODY, "ticket_id": 9001}
    g.update(over)
    return g


def _kw(**over):
    kw = {"body": guard.render_client_email_html(BODY), "body_is_html": True,
          "subject": guard.CLIENT_EMAIL_SUBJECT + "9001", "subtype_xmlid": "mail.mt_comment", "message_type": "comment", "partner_ids": [3001],
          "email_from": HELP, "email_add_signature": False}
    kw.update(over)
    return kw


def _check(kw, gate):
    guard.check("helpdesk", "helpdesk.ticket", "message_post", [[9001]], kw, client_email=gate)


# ---------------------------------------------------------------- the guard rule

def test_guard_allows_the_exact_approved_client_email():
    _check(_kw(), _gate())


@pytest.mark.parametrize("kw,gate,why", [
    (_kw(), _gate(recipients_verified=False), "not verified against the live ticket"),
    (_kw(), {k: v for k, v in _gate().items() if k != "recipients_verified"}, "not verified against the live ticket"),
    (_kw(), _gate(nonce_consumed=False), "no consumed owner approval"),
    (_kw(), {k: v for k, v in _gate().items() if k != "nonce_consumed"}, "no consumed owner approval"),
    (_kw(body="Something else"), _gate(), "not the approved draft"),
    (_kw(email_from="someone.personal@example.org"), _gate(), "not a configured helpdesk alias"),
    (_kw(email_from=""), _gate(), "not a configured helpdesk alias"),
    (_kw(partner_ids=[3001, 777]), _gate(), "own client only"),
    (_kw(partner_ids=[777]), _gate(), "own client only"),
    (_kw(partner_ids=[]), _gate(), "own client only"),
    (_kw(), _gate(partner_id=0), "own client only"),
    (_kw(email_add_signature=True), _gate(), "signature"),
    (_kw(subject="Triage title: Client Co"), _gate(), "client-friendly request subject"),
    (_kw(subject=guard.CLIENT_EMAIL_SUBJECT + "9001 extra"), _gate(), "exactly the client-friendly request subject"),
    (_kw(subject=guard.CLIENT_EMAIL_SUBJECT + "9002"), _gate(), "exactly the client-friendly request subject"),
    (_kw(body_is_html=False), _gate(), "not the approved draft"),
    (_kw(body="<script>x</script>"), _gate(), "not the approved draft"),
    (_kw(email_cc="x@example.org"), _gate(), "not allowed: email_cc"),
    (_kw(author_id=5), _gate(), "not allowed: author_id"),
    (_kw(message_type="notification"), _gate(), "message_type"),
    (_kw(), _gate(aliases=[]), "not a configured helpdesk alias"),
])
def test_guard_refuses_every_other_client_email(kw, gate, why):
    with pytest.raises(guard.WriteRefused, match=why):
        _check(kw, gate)


def test_the_gate_is_opt_in_and_the_principal_comment_rule_is_unchanged():
    with pytest.raises(guard.WriteRefused):                      # no gate: a client email is just a refused comment
        guard.check("helpdesk", "helpdesk.ticket", "message_post", [[9001]], _kw())
    ok = {"body": "x", "subtype_xmlid": "mail.mt_comment", "message_type": "comment", "partner_ids": [55],
          "context": {"mail_notify_author": True}}
    guard.check("helpdesk", "helpdesk.ticket", "message_post", [[9001]], ok, notify_partner=55)
    with pytest.raises(guard.WriteRefused):                      # and the Principal rule never accepts the new kwargs
        guard.check("helpdesk", "helpdesk.ticket", "message_post", [[9001]], _kw(), notify_partner=3001)


# ---------------------------------------------------------------- the hub

def _posts(hub):
    return [w for w in hub.odoo.t.writes if w["method"] == "message_post"]


def _emails(hub):
    return [w for w in _posts(hub) if w["kwargs"].get("subtype_xmlid") == "mail.mt_comment"]


def test_approved_confirm_goes_by_email_from_the_team_alias_not_whatsapp():
    hub = build_hub(cfg=dict(CFG))
    deliver(hub, "webhook_text.json")
    res = approve(hub, "confirm")
    assert res["ok"] is True and hub.ticket(20001).stage == "awaiting_customer"
    [mail] = _emails(hub)
    kw = mail["kwargs"]
    assert (kw["email_from"], kw["partner_ids"], kw["email_add_signature"], kw["message_type"]) == \
        (HELP, [3001], False, "comment")
    assert len(hub.whatsapp.sent) == 1 and hub.whatsapp.automatic_count() == 1      # only the instant ack


def test_email_never_goes_out_without_the_owner_tap():
    hub = build_hub(cfg=dict(CFG))
    deliver(hub, "webhook_text.json")
    assert _emails(hub) == []


def test_team_without_an_alias_falls_back_to_the_default():
    hub = build_hub(cfg=dict(CFG))
    deliver(hub, "webhook_text.json")
    st = hub.ticket(20001)
    st.analysis.track = "inquiry"                                  # team 11 has no entry in email_from_by_team
    assert hub._email_from(st) == INFO


def test_a_team_can_only_use_its_own_alias_never_another_teams():
    hub = build_hub(cfg=dict(CFG, email_from_by_team={2: HELP, 11: INFO}))
    deliver(hub, "webhook_text.json")
    st = hub.ticket(20001)                                         # team 2
    hub.odoo.t.data["helpdesk.team"][2].update(use_alias=True, alias_email=INFO)
    assert hub._email_from(st) == HELP                             # team 11's alias on team 2: ignored
    hub.odoo.t.data["helpdesk.team"][2].update(alias_email=HELP.upper())
    assert hub._email_from(st) == HELP.upper()                     # its OWN alias (case-insensitive): used


def test_no_configured_alias_holds_and_sends_nothing():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "client_channel": "email"})
    deliver(hub, "webhook_text.json")
    res = approve(hub, "confirm")
    assert res["ok"] is False and res["reason"] == "no_email_alias"
    assert _emails(hub) == [] and any(h["reason"] == "email_not_configured" for h in hub.holds())


def test_a_sender_the_guard_does_not_know_is_refused_and_held(monkeypatch):
    hub = build_hub(cfg=dict(CFG))
    deliver(hub, "webhook_text.json")
    monkeypatch.setattr(hub, "_email_from", lambda state: "personal.mailbox@example.org")
    res = approve(hub, "confirm")
    assert res["ok"] is False and res["reason"] == "email_blocked"
    assert _emails(hub) == [] and any(h["reason"] == "email_blocked" for h in hub.holds())


def test_email_is_the_default_channel_and_whatsapp_must_be_chosen():
    from twin_agent.hub.pipeline import Hub
    assert Hub._email_channel(type("H", (), {"cfg": {}})()) is True
    hub = build_hub()                                              # the test helper pins the legacy channel
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    assert len(hub.whatsapp.sent) == 2 and _emails(hub) == []


def test_per_person_email_runs_as_the_approving_owner_and_is_audited():
    rig = Rig()
    hub = rig.hub(cfg=dict(CFG))
    deliver(hub, "webhook_text.json")
    assert approve(hub, "confirm")["ok"] is True
    row = [r for r in store_mod.audit_rows(rig.store) if r.get("channel") == "email"][-1]
    assert (row["acting"], row["outcome"], row["method"]) == ("odoo-supervisor", "ok", "message_post")
    assert 778 in rig.authors()


# ---------------------------------------------------------------- HIGH-1: the LIVE ticket is re-read before the post

def _hub_with_ticket(**kw):
    hub = build_hub(cfg=dict(CFG), **kw)
    deliver(hub, "webhook_text.json")
    return hub


def _tap(hub):
    return approve(hub, "confirm")


def test_client_changed_in_odoo_after_the_hub_opened_the_ticket_is_refused():
    hub = _hub_with_ticket()
    hub.odoo.t.data["helpdesk.ticket"][20001]["partner_id"] = 3002
    res = _tap(hub)
    assert res["ok"] is False and res["reason"] == "email_blocked" and _emails(hub) == []
    assert any("client changed in Odoo" in h["detail"] for h in hub.holds())


def test_external_follower_refuses_the_email():
    hub = _hub_with_ticket()
    hub.odoo.t.data["res.partner"][5001] = {"id": 5001, "name": "Other Co contact", "partner_share": True}
    hub.odoo.t.data["helpdesk.ticket"][20001]["message_partner_ids"] = [3001, 5001]
    res = _tap(hub)
    assert res["ok"] is False and _emails(hub) == []
    assert any("external follower" in h["detail"] for h in hub.holds())


def test_a_follower_that_cannot_be_read_counts_as_external():
    hub = _hub_with_ticket()
    hub.odoo.t.data["helpdesk.ticket"][20001]["message_partner_ids"] = [3001, 6999]     # no such partner row
    assert _tap(hub)["ok"] is False and _emails(hub) == []


def test_internal_followers_do_not_block_and_may_be_emailed_by_odoo():
    hub = _hub_with_ticket()
    hub.odoo.t.data["res.partner"][5002] = {"id": 5002, "name": "Staff", "partner_share": False}
    hub.odoo.t.data["helpdesk.ticket"][20001]["message_partner_ids"] = [3001, 5002]
    assert _tap(hub)["ok"] is True and len(_emails(hub)) == 1
    assert _emails(hub)[0]["kwargs"]["partner_ids"] == [3001]       # we name only the client; Odoo may notify staff


# ---------------------------------------------------------------- M1: the stored approval is re-checked, one send

def _nonce(hub, kind="confirm"):
    return pending(hub, kind)["nonce"]


def _email_args(hub, nonce):
    return dict(partner_id=3001, email_from=HELP, aliases={HELP, INFO}, store=hub.store, nonce=nonce)


def test_replaying_a_consumed_approval_cannot_send_a_second_email():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    _tap(hub)
    doc = hub.store.get("approvals", n)
    assert doc["emailed_at"] and len(_emails(hub)) == 1
    with pytest.raises(guard.WriteRefused, match="already used"):
        hub.odoo.send_client_email(20001, doc["draft"], **_email_args(hub, n))
    assert len(_emails(hub)) == 1


def test_a_different_text_than_the_approved_draft_is_refused_by_the_gateway():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    _tap(hub)
    hub.store.update("approvals", n, lambda d: {**d, "emailed_at": None})       # even with the marker cleared
    with pytest.raises(guard.WriteRefused, match="not what the owner approved"):
        hub.odoo.send_client_email(20001, "Totally different text", **_email_args(hub, n))
    assert len(_emails(hub)) == 1


def test_an_unconsumed_or_unknown_approval_cannot_send():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    draft = hub.store.get("approvals", n)["draft"]
    with pytest.raises(guard.WriteRefused, match="not given"):                  # pending: never tapped
        hub.odoo.send_client_email(20001, draft, **_email_args(hub, n))
    with pytest.raises(guard.WriteRefused, match="no stored approval"):
        hub.odoo.send_client_email(20001, draft, **_email_args(hub, "nope"))
    assert _emails(hub) == []


def test_an_edited_text_is_sent_when_the_owner_approved_that_edit_and_only_that():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    edited = "Hello, we are on it and will update you today. Acme Support"
    assert approve(hub, "confirm", text=edited)["ok"] is True
    [mail] = _emails(hub)
    assert mail["kwargs"]["body"] == edited
    assert hub.store.get("approvals", n)["edited_hash"] == _sha(edited)


# ---------------------------------------------------------------- L1: runs as the approver

def test_email_runs_as_the_approver_when_that_is_not_the_owner():
    rig = Rig()
    hub = rig.hub(cfg=dict(CFG, approver_tg_ids=[2001]))
    rig.person.cfg.tg_people[2001] = 777                          # Principal taps; the owner is Supervisor (778)
    deliver(hub, "webhook_text.json")
    assert approve(hub, "confirm", as_tg=2001)["ok"] is True
    row = [r for r in store_mod.audit_rows(rig.store) if r.get("channel") == "email"][-1]
    assert (row["acting"], row["outcome"]) == ("odoo-principal", "ok") and row["actor_tg"].startswith("tg:")
    assert 777 in rig.authors()


def test_email_is_blocked_when_the_approver_is_not_mapped():
    rig = Rig()
    hub = rig.hub(cfg=dict(CFG, approver_tg_ids=[2002]))
    deliver(hub, "webhook_text.json")
    res = approve(hub, "confirm", as_tg=2002)
    assert res["ok"] is False
    assert not [m for m in rig.data["mail.message"].values() if m["subtype"] == "mail.mt_comment"]
    row = [r for r in store_mod.audit_rows(rig.store) if r["outcome"] == "blocked"][-1]
    assert row["reason"] == "tapper_unmapped" and row["role"] == "none"


# ---------------------------------------------------------------- review round 2

def test_B1_a_client_with_no_email_is_not_marked_sent_and_nothing_moves():
    hub = _hub_with_ticket()
    hub.odoo.t.data["res.partner"][3001]["email"] = False
    n = _nonce(hub)
    res = _tap(hub)
    st = hub.ticket(20001)
    assert res["ok"] is False and res["reason"] == "email_blocked" and _emails(hub) == []
    assert st.stage == "awaiting_owner" and st.history.count("8-CONFIRM") == 0
    assert not hub.store.get("approvals", n).get("emailed_at") and not hub.metrics.pushed
    assert any("client has no email in Odoo; add it, then re-approve" in h["detail"] for h in hub.holds())
    assert pending(hub, "confirm") is not None and pending(hub, "confirm")["nonce"] != n     # the owner is asked again
    hub.odoo.t.data["res.partner"][3001]["email"] = "client-a@example.org"                  # fixed in Odoo ...
    assert approve(hub, "confirm")["ok"] is True and len(_emails(hub)) == 1                  # ... re-approve sends


@pytest.mark.parametrize("bad", ["", "not-an-email", "a@b", " "])
def test_B1_an_invalid_email_is_refused_too(bad):
    hub = _hub_with_ticket()
    hub.odoo.t.data["res.partner"][3001]["email"] = bad
    assert _tap(hub)["ok"] is False and _emails(hub) == []


def test_M1_the_email_text_tells_the_client_they_can_reply_by_email_or_whatsapp():
    hub = _hub_with_ticket()
    draft = hub.store.get("approvals", _nonce(hub))["draft"]
    assert "You can reply to this email or on WhatsApp." in draft
    _tap(hub)
    assert "reply to this email or on WhatsApp" in _emails(hub)[0]["kwargs"]["body"]


def _client_mail(hub, mid, body, *, author=3001, mtype="email"):
    hub.odoo.t.data["mail.message"][mid] = {"id": mid, "model": "helpdesk.ticket", "res_id": 20001, "author_id": author,
                                            "message_type": mtype, "body": body, "date": "2025-11-11 12:30:00"}


def test_M1_a_client_email_reply_reaches_the_hub_like_a_whatsapp_reply_once():
    hub = _hub_with_ticket()
    _tap(hub)                                                      # confirm sent: stage awaiting_customer
    _client_mail(hub, 900, "<p>Yes, that is correct.<br/>Thanks</p>")
    out = hub.tick()
    st = hub.ticket(20001)
    assert out["email_replies"] == 1 and st.stage == "awaiting_owner_advance"
    assert st.replies[-1]["text"] == "Yes, that is correct.\nThanks"
    assert pending(hub, "advance") is not None                     # the owner is asked to mark it confirmed
    assert hub.tick()["email_replies"] == 0 and len(hub.ticket(20001).replies) == 1      # deduped: never twice


def test_M1_only_the_clients_own_messages_are_read():
    hub = _hub_with_ticket()
    _tap(hub)
    hub.odoo.t.data["res.partner"][777] = {"id": 777, "name": "Staff", "partner_share": False}
    _client_mail(hub, 901, "ours: staff note", author=777)         # not the ticket's partner
    _client_mail(hub, 902, "notification", mtype="notification")
    assert hub.tick()["email_replies"] == 0 and hub.ticket(20001).replies == []


def test_M1_polling_is_off_in_whatsapp_mode_and_read_only():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    assert "email_replies" not in hub.tick()
    hub2 = _hub_with_ticket()
    before = len(hub2.odoo.t.writes)
    hub2.tick()
    assert len(hub2.odoo.t.writes) == before


def test_minor_a_refused_edit_is_never_emailed_even_if_the_gateway_is_asked():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    leaked = "Hi, your password is hunter2 and the key is sk-live-AbCdEfGhIjKlMnOpQrStUvWx123456 Acme Support"
    res = approve(hub, "confirm", text=leaked)
    assert res["ok"] is False and _emails(hub) == []
    assert not hub.store.get("approvals", n).get("edited_hash")
    with pytest.raises(guard.WriteRefused):
        hub.odoo.send_client_email(20001, leaked, **_email_args(hub, n))


def test_minor_after_an_edit_only_the_edited_text_passes_not_the_original_draft():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    draft = hub.store.get("approvals", n)["draft"]
    approve(hub, "confirm", text="Hello, we are on it today. Acme Support")
    hub.store.update("approvals", n, lambda d: {**d, "emailed_at": None})
    with pytest.raises(guard.WriteRefused, match="not what the owner approved"):
        hub.odoo.send_client_email(20001, draft, **_email_args(hub, n))


def test_R_LOW_2_an_approval_of_another_ticket_cannot_send_here():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    _tap(hub)
    hub.store.update("approvals", n, lambda d: {**d, "emailed_at": None})
    doc = hub.store.get("approvals", n)
    with pytest.raises(guard.WriteRefused, match="another ticket"):
        hub.odoo.send_client_email(9001, doc["draft"], **_email_args(hub, n))


def test_minor_the_client_sees_a_friendly_subject_and_line_breaks_survive():
    hub = _hub_with_ticket()
    n = _nonce(hub)
    approve(hub, "confirm", text="Line one\nLine <two> & more. Acme Support")
    kw = _emails(hub)[0]["kwargs"]
    assert kw["subject"] == "Your Acme support request #20001" and kw["body_is_html"] is True
    assert kw["body"] == "Line one<br/>Line &lt;two&gt; &amp; more. Acme Support"
    assert hub.odoo.t.data["helpdesk.ticket"][20001]["name"] not in kw["subject"] and n


def test_minor_a_pre_post_read_failure_says_not_sent_and_is_not_unknown():
    hub = _hub_with_ticket()
    hub.odoo.t.fail_on.add(("res.partner", "search_read"))
    res = _tap(hub)
    st = hub.ticket(20001)
    assert res["reason"] == "email_blocked" and not st.send_unknown and _emails(hub) == []
    assert any("nothing was sent" in h["detail"] for h in hub.holds())


def test_minor_the_fallback_flag_never_lets_an_unmapped_approver_send_a_client_email():
    rig = Rig(fallback_to_automation=True)
    hub = rig.hub(cfg=dict(CFG, approver_tg_ids=[2002]))
    deliver(hub, "webhook_text.json")
    res = approve(hub, "confirm", as_tg=2002)
    assert res["ok"] is False
    assert not [m for m in rig.data["mail.message"].values() if m["subtype"] == "mail.mt_comment"]


# ---------------------------------------------------------------- B1 option B: WhatsApp only as the owner's explicit choice

def _no_email_hub():
    hub = _hub_with_ticket()
    hub.odoo.t.data["res.partner"][3001]["email"] = False
    _tap(hub)                                                      # held; the owner is asked again
    return hub


def test_no_email_holds_and_sends_nothing_by_default():
    hub = _no_email_hub()
    assert _emails(hub) == [] and len(hub.whatsapp.sent) == 1 and hub.whatsapp.automatic_count() == 1
    req = pending(hub, "confirm")
    assert req["offer_whatsapp"] is True and "reply to this email" not in req["draft"]
    assert hub.store.get("approvals", req["nonce"])["whatsapp_offer"] is True
    assert hub.ticket(20001).stage == "awaiting_owner"


def test_owner_picks_whatsapp_for_this_follow_up_gives_exactly_one_whatsapp_send():
    hub = _no_email_hub()
    nonce = pending(hub, "confirm")["nonce"]
    res = hub.on_owner_decision(nonce, hub.ticket(20001).owner.tg_id, approved=True, channel="whatsapp")
    assert res["ok"] is True and hub.ticket(20001).stage == "awaiting_customer"
    assert len(hub.whatsapp.sent) == 2 and hub.whatsapp.automatic_count() == 1      # the ack + this ONE owner-approved send
    assert hub.whatsapp.sent[-1]["automatic"] is False and _emails(hub) == []
    doc = hub.store.get("approvals", nonce)
    assert doc["channel"] == "whatsapp" and doc["channel_by"] and doc["used_at"]
    assert any("chose WhatsApp" in w["kwargs"]["body"] for w in _posts(hub))        # audited in Odoo


def test_a_plain_approve_without_the_choice_still_sends_nothing():
    hub = _no_email_hub()
    assert approve(hub, "confirm")["ok"] is False
    assert len(hub.whatsapp.sent) == 1 and _emails(hub) == []


def test_whatsapp_choice_is_refused_when_it_was_not_offered_and_does_not_burn_the_nonce():
    hub = _hub_with_ticket()                                       # the client HAS an email
    nonce = pending(hub, "confirm")["nonce"]
    res = hub.on_owner_decision(nonce, hub.ticket(20001).owner.tg_id, approved=True, channel="whatsapp")
    assert res == {"ok": False, "reason": "whatsapp_not_offered"} and len(hub.whatsapp.sent) == 1
    assert not hub.store.get("approvals", nonce)["used_at"]
    assert hub.on_owner_decision(nonce, hub.ticket(20001).owner.tg_id, approved=True, channel="sms")["reason"] == "unknown_channel"


def test_the_choice_is_per_follow_up_the_next_one_tries_email_again():
    hub = _no_email_hub()
    hub.on_owner_decision(pending(hub, "confirm")["nonce"], hub.ticket(20001).owner.tg_id, approved=True, channel="whatsapp")
    hub.odoo.t.data["res.partner"][3001]["email"] = "client-a@example.org"                  # the email was added in Odoo
    from hub_helpers import msg
    deliver(hub, msg("wamid.R1", "yes that is correct"))
    approve(hub, "advance")
    approve(hub, "fix_ready")
    approve(hub, "resolve")
    assert len(_emails(hub)) == 1 and hub.whatsapp.automatic_count() == 1


# ---------------------------------------------------------------- round 3: the poll routes by TICKET, never by number

def test_D_HIGH_1_a_reassigned_number_never_sends_client_As_email_to_client_Bs_ticket():
    hub = _hub_with_ticket()                                       # ticket 20001, partner 3001
    _tap(hub)
    hub.k_odoo.partners["201000000001"] = 3002                     # the number now belongs to client 3002
    deliver(hub, msg("wamid.B1", "Hello from the new owner of this number about the Sales module"))
    tickets = {t.tid: t for t in hub.all_tickets()}
    other = [t for t in tickets.values() if t.partner_id == 3002]
    assert len(other) == 1
    acks_before = hub.whatsapp.automatic_count()
    _client_mail(hub, 950, "CONFIDENTIAL reply from client A")
    hub.tick()
    assert [r["text"] for r in hub.ticket(20001).replies] == ["CONFIDENTIAL reply from client A"]
    assert all("CONFIDENTIAL" not in r["text"] for t in hub.all_tickets() if t.tid != 20001 for r in t.replies)
    assert len(hub.all_tickets()) == 2 and hub.whatsapp.automatic_count() == acks_before


def test_D_HIGH_1_an_email_never_opens_a_ticket_and_never_acks_even_on_a_kept_closed_ticket():
    hub = _hub_with_ticket()
    st = hub.ticket(20001)
    st.stage, st.keep_closed, st.resolved_at = "resolved", True, hub.clock().isoformat()
    hub._save(st)
    _client_mail(hub, 951, "one more thing")
    hub.tick()
    hub.clock.advance(minutes=30)
    hub.tick()
    assert len(hub.all_tickets()) == 1 and hub.whatsapp.automatic_count() == 1 and hub.ticket(20001).replies == []
    assert hub.store.scan("fcclaim") == [] or all("odoo-mail" not in str(d) for d in hub.store.scan("fcclaim"))
    assert all("odoo-mail" not in d.get("mid", "") for d in hub.store.scan("messages") if isinstance(d, dict))


def test_D_HIGH_1_a_reply_when_the_ticket_client_changed_in_odoo_is_held_not_attached():
    hub = _hub_with_ticket()
    _tap(hub)
    hub.odoo.t.data["helpdesk.ticket"][20001]["partner_id"] = 3002
    _client_mail(hub, 952, "late reply")
    hub.tick()
    assert hub.ticket(20001).replies == [] and any(h["reason"] == "email_reply_held" for h in hub.holds())


def test_MINOR_3_a_reply_from_another_address_on_the_ticket_is_held_for_the_owner():
    hub = _hub_with_ticket()
    _tap(hub)
    hub.odoo.t.data["res.partner"][5100] = {"id": 5100, "name": "Colleague", "partner_share": True}
    _client_mail(hub, 953, "I am a colleague", author=5100)
    _client_mail(hub, 954, "unknown sender", author=False)
    hub.tick()
    holds = [h for h in hub.holds() if h["reason"] == "email_reply_held"]
    assert len(holds) == 2 and hub.ticket(20001).replies == []
    hub.tick()
    assert len([h for h in hub.holds() if h["reason"] == "email_reply_held"]) == 2         # once each, never repeated


def test_an_image_only_email_is_held_and_not_silently_swallowed():
    hub = _hub_with_ticket()
    _tap(hub)
    _client_mail(hub, 955, '<p><img src="cid:abc"/></p>')
    hub.tick()
    assert hub.ticket(20001).replies == []
    assert any("no readable text" in h["detail"] for h in hub.holds())


@pytest.mark.parametrize("subject,sender", [("Out of Office: back Monday", ""), ("Automatic reply: thanks", ""),
                                            ("Undelivered Mail Returned to Sender", ""), ("hi", "MAILER-DAEMON@x.example.org"),
                                            ("Re: ticket", "no-reply@x.example.org")])
def test_auto_replies_are_ignored(subject, sender):
    hub = _hub_with_ticket()
    _tap(hub)
    hub.odoo.t.data["mail.message"][960] = {"id": 960, "model": "helpdesk.ticket", "res_id": 20001, "author_id": 3001,
                                            "message_type": "email", "body": "I am away", "subject": subject,
                                            "email_from": sender, "date": "2025-11-11 12:30:00"}
    hub.tick()
    assert hub.ticket(20001).replies == [] and not [h for h in hub.holds() if h["reason"] == "email_reply_held"]


def test_the_poll_does_not_wait_for_a_busy_sender_and_picks_the_mail_up_next_tick():
    import threading
    hub = _hub_with_ticket()
    _tap(hub)
    _client_mail(hub, 970, "hello again")
    lock = hub._wa_lock("201000000001")
    done = threading.Event()

    def other():
        with lock:
            done.wait(5)
    t = threading.Thread(target=other)
    t.start()
    try:
        assert hub.tick()["email_replies"] == 0 and hub.ticket(20001).replies == []          # skipped, not blocked
    finally:
        done.set()
        t.join()
    assert hub.tick()["email_replies"] == 1


def test_D_MED_1_an_email_reply_never_reopens_the_whatsapp_window():
    hub = build_hub(cfg=dict(CFG, window_hours=0.5))
    deliver(hub, "webhook_text.json")
    hub.odoo.t.data["res.partner"][3001]["email"] = False
    _tap(hub)                                                      # held: WhatsApp is offered for this follow-up
    last = hub.ticket(20001).last_inbound_at
    hub.clock.advance(minutes=40)                                  # the 30 minute window is now closed
    hub.odoo.t.data["mail.message"][980] = {"id": 980, "model": "helpdesk.ticket", "res_id": 20001, "author_id": 3001,
                                            "message_type": "email", "body": "still waiting",
                                            "date": hub.clock().strftime("%Y-%m-%d %H:%M:%S")}
    hub.tick()
    st = hub.ticket(20001)
    assert st.last_inbound_at == last and st.last_email_at and len(st.replies) == 1
    sent = len(hub.whatsapp.sent)
    res = hub.on_owner_decision(pending(hub, "confirm")["nonce"], st.owner.tg_id, approved=True, channel="whatsapp")
    assert res["ok"] is False and res["reason"] == "window_closed" and len(hub.whatsapp.sent) == sent
    assert any(h["reason"] == "window_closed" for h in hub.holds())
