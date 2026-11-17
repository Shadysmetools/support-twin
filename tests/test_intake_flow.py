"""Intake v2: the pre-ticket conversation (company, contact save, type buttons, short subject, the one confirmation).
Fakes only: no network, no Odoo."""
from __future__ import annotations

import types

import pytest

from twin_agent.channels import whatsapp_cloud
from twin_agent.hub import fakes, intake_flow
from twin_agent.testing.fake_odoo import FakeOdoo, _seed
from hub_helpers import build_hub, deliver, msg

NEW = "201112223334"                       # nobody knows this number
KNOWN = "201000000001"
STAFF = "201005550001"
BASE_TS = 1762860000
PROBLEM = "Invoice PDF total is wrong for discounted customers"
CFG = {"phone_number_id": "PHONE_NUMBER_ID", "intake_v2": True}
COMPANY_Q = "Hello! Thank you for contacting Acme Support. Which company are you with?"


def transport():
    t = FakeOdoo(data=_seed())
    p = t.data["res.partner"]
    p[4001] = {"id": 4001, "name": "Initech Trading", "is_company": True}
    p[4010] = {"id": 4010, "name": "Delta Works", "is_company": True}
    p[4011] = {"id": 4011, "name": "Delta Works", "is_company": True}          # two companies, one name
    p[4020] = {"id": 4020, "name": "Solo Foods", "is_company": True, "phone": "+201007770001", "user_ids": False}
    p[5001] = {"id": 5001, "name": "Nina", "is_company": False, "parent_id": [4001, "Initech Trading"],
               "phone": "+201000000001", "user_ids": False}
    p[5002] = {"id": 5002, "name": "Acme Test Client – Riley", "is_company": False, "parent_id": False,
               "phone": "+201005550001", "user_ids": False}
    return t


class Seq:
    def __init__(self):
        self.n = 0

    def _p(self, mid, wa, **fields):
        self.n += 1
        p = msg(f"{mid}{self.n}", "x", ts=BASE_TS + self.n * 60, wa_id=wa)
        m = p["entry"][0]["changes"][0]["value"]["messages"][0]
        if fields:
            m.pop("text")
            m.update(fields)
        return p

    def text(self, hub, body, wa=NEW):
        self.n += 1
        return deliver(hub, msg(f"wamid.IN{self.n}", body, ts=BASE_TS + self.n * 60, wa_id=wa))

    def tap(self, hub, kind, wa=NEW, title=None):
        reply = {"id": f"type_{kind}", "title": title or kind}
        return deliver(hub, self._p("wamid.TP", wa, type="interactive",
                                    interactive={"type": "button_reply", "button_reply": reply}))

    def voice(self, hub, wa=NEW, aid="AUD1"):
        return deliver(hub, self._p("wamid.VN", wa, type="audio", audio={"id": aid}))

    def photo(self, hub, wa=NEW, mid="IMG1"):
        return deliver(hub, self._p("wamid.PH", wa, type="image", image={"id": mid, "mime_type": "image/jpeg"}))


def hub_for(t=None, cfg=None, partners=None, knowledge=None):
    know = knowledge or fakes.FakeOdooKnowledge(
        partners=partners if partners is not None else {KNOWN: 5001, STAFF: 5002, "201007770001": 4020})
    return build_hub(transport=t or transport(), cfg={**CFG, **(cfg or {})}, knowledge_odoo=know)


def holds(hub, reason):
    return [h for h in hub.holds() if h["reason"] == reason]


def sent_texts(hub):
    return [s["text"] for s in hub.whatsapp.sent]


def partner_writes(t):
    return [w for w in t.writes if w["model"] == "res.partner"]


def desc(t, st):
    return t.data["helpdesk.ticket"][st.tid]["description"]


def test_unknown_sender_company_contact_buttons_subject_confirmation():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    assert sent_texts(hub) == [COMPANY_Q]
    assert hub.all_tickets() == []                                        # no ticket yet
    s.text(hub, "Initech Trading")
    w = partner_writes(t)
    assert len(w) == 1 and w[0]["method"] == "create"                      # the contact is saved, under the company
    assert w[0]["vals"]["parent_id"] == 4001 and w[0]["vals"]["phone"] == "+" + NEW
    assert set(w[0]["vals"]) <= {"name", "phone", "parent_id", "category_id"}
    buttons = hub.whatsapp.sent[-1]["interactive"]["action"]["buttons"]
    assert [b["reply"]["title"] for b in buttons] == ["Bugs & issues", "Features & requests", "Question"]
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    row = t.data["helpdesk.ticket"][st.tid]
    assert row["name"] == st.intake_subject and len(row["name"].split()) <= 10    # a short subject only
    assert "Invoice PDF total is wrong" in row["description"]                      # the full text is in the description
    assert row["x_type"] == hub.conv.types["bug"] and row["partner_id"] == w[0]["id"]
    last = sent_texts(hub)[-1]
    assert last.startswith("Thank you.") and f"#{st.tid} – {st.intake_subject}" in last     # no profile name: no "there"
    assert "reach out if we have any questions" in last
    assert sum(1 for x in sent_texts(hub) if x.startswith("Thank you")) == 1       # ONE confirmation, no old ack


def test_known_contact_with_company_skips_the_company_question():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    assert "interactive" in hub.whatsapp.sent[0] and len(hub.whatsapp.sent) == 1
    assert partner_writes(t) == []                                         # nothing to save: already known


def test_a_company_record_as_the_sender_is_its_own_company():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa="201007770001")
    assert "interactive" in hub.whatsapp.sent[0] and partner_writes(t) == []


def test_same_number_is_recognised_next_time():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    s.text(hub, "Initech Trading")
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    st.keep_closed, st.stage = True, "resolved"
    st.resolved_at = hub.clock().isoformat()
    hub._save(st)
    before = len(hub.whatsapp.sent)
    s.text(hub, "Another problem with the stock report")
    assert "Which company" not in " ".join(sent_texts(hub)[before:])
    assert "interactive" in hub.whatsapp.sent[-1]                          # straight to the type buttons


def test_the_saved_contact_is_trusted_only_for_a_short_time():
    """After the TTL only partner_for (which fails closed) is the identity: the number is then 'unknown' again, and the
    exact-phone check REFUSES to create a second contact (no duplicate, no reuse): a human is told, nothing is sent."""
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    s.text(hub, "Initech Trading")
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    st.keep_closed, st.stage = True, "resolved"
    hub._save(st)
    hub.clock.advance(minutes=11)
    before = len(hub.whatsapp.sent)
    s.text(hub, "Another problem")
    s.text(hub, "Initech Trading")
    assert len(partner_writes(t)) == 1 and len(hub.whatsapp.sent) == before + 1
    assert len(holds(hub, "intake_contact_refused")) == 1


@pytest.mark.parametrize("answer", ["Initech Trading Co", "Initech", "Delta Works", "Zeta Unknown Corp"])
def test_only_a_single_exact_name_is_linked_everything_else_is_pending(answer):
    """No near match and no duplicate-name match writes a company; no existing company name is shown to the client."""
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    s.text(hub, answer)
    assert len(holds(hub, "intake_pending_company")) == 1
    assert "parent_id" not in partner_writes(t)[0]["vals"]                 # saved, but with no company
    assert not any(n in " ".join(sent_texts(hub)) for n in ("Initech Trading", "Delta Works"))
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    d = desc(t, st)
    assert "Pending company" in d and f"Company (client said): {answer}" in d and st.intake_company_id == 0


def test_an_answer_with_filler_is_matched_on_the_exact_name_that_is_left():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    s.text(hub, "I work at initech trading")
    assert partner_writes(t)[0]["vals"]["parent_id"] == 4001 and holds(hub, "intake_pending_company") == []


def test_the_request_sent_as_the_company_answer_is_kept_not_taken_as_a_company():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, "Hi")
    s.text(hub, "Invoices are not printing")
    assert any("name of your company" in x for x in sent_texts(hub)) and partner_writes(t) == []
    s.text(hub, "Initech Trading")
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    assert "Invoices are not printing" in desc(t, st) and t.data["helpdesk.ticket"][st.tid]["name"] != "Support request"


def test_company_reask_is_bounded_then_pending_and_the_ticket_is_still_created():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    long = "this is a very long message that is clearly not the name of any company at all"
    for _ in range(2):
        s.text(hub, long)
    assert len([x for x in sent_texts(hub) if "name of your company" in x]) == 1     # ONE re-ask, then it is filed
    assert len(holds(hub, "intake_pending_company")) == 1
    assert "interactive" in hub.whatsapp.sent[-1]                           # the flow moved on
    s.tap(hub, "question")
    assert len(hub.all_tickets()) == 1


def test_type_reask_is_bounded_then_question_and_alert():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    for _ in range(2):
        s.text(hub, "hmm not sure", wa=KNOWN)
    assert len(holds(hub, "intake_type_defaulted")) == 1
    (st,) = hub.all_tickets()
    assert st.intake_type == "question" and t.data["helpdesk.ticket"][st.tid]["x_type"] == hub.conv.types["inquiries"]
    assert sent_texts(hub)[-1].startswith("Thank you")


def test_message_mid_flow_is_appended_not_a_new_ticket():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    s.text(hub, "The tax line is also wrong on the PDF", wa=KNOWN)
    assert hub.all_tickets() == []
    s.tap(hub, "bug", wa=KNOWN)
    (st,) = hub.all_tickets()
    assert "tax line is also wrong" in desc(t, st) and "Invoice PDF total" in desc(t, st)


def test_voice_and_photo_sent_during_the_questions_are_kept_not_lost():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    s.voice(hub)                                                            # during the company question
    s.photo(hub)
    assert len([x for x in sent_texts(hub) if "name of your company" in x]) == 1      # asked again once, no re-ask spent
    s.text(hub, "Initech Trading")
    s.voice(hub, aid="AUD2")                                                # during the buttons
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    d = desc(t, st)
    assert "[voice note AUD1]" in d and "[image IMG1]" in d and "[voice note AUD2]" in d
    assert len(holds(hub, "voice_note_pending")) == 2                       # a person must listen to each
    assert holds(hub, "intake_type_defaulted") == [] and holds(hub, "intake_pending_company") == []
    assert sent_texts(hub)[-1].startswith("Thank you")


def test_a_photo_with_no_caption_as_the_first_message_still_gets_a_ticket_and_a_confirmation():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.photo(hub, wa=KNOWN)
    assert "interactive" in hub.whatsapp.sent[0]
    s.tap(hub, "bug", wa=KNOWN)
    (st,) = hub.all_tickets()
    row = t.data["helpdesk.ticket"][st.tid]
    assert row["name"] == "Photo from client" and "[image IMG1]" in row["description"]
    assert f"#{st.tid} – Photo from client" in sent_texts(hub)[-1]
    assert holds(hub, "empty_message") == []


def test_silence_on_the_buttons_gets_one_reask_then_a_question_ticket():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    hub.clock.advance(minutes=31)
    hub.tick()
    assert "reply with a number" in sent_texts(hub)[-1].lower() or "1" in sent_texts(hub)[-1]
    n = len(hub.whatsapp.sent)
    hub.clock.advance(minutes=10)
    hub.tick()
    assert len(hub.whatsapp.sent) == n and hub.all_tickets() == []          # only ONE timer re-ask
    hub.clock.advance(hours=2)
    hub.tick()
    (st,) = hub.all_tickets()
    assert st.intake_type == "question" and len(holds(hub, "intake_type_defaulted")) == 1
    assert sent_texts(hub)[-1].startswith("Thank you")                      # and the client is told


def test_silence_on_the_company_question_makes_a_pending_company_ticket():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    hub.clock.advance(minutes=31)
    hub.tick()
    assert "name of your company" in sent_texts(hub)[-1]
    hub.clock.advance(hours=3)
    hub.tick()
    (st,) = hub.all_tickets()
    assert len(holds(hub, "intake_pending_company")) == 1 and "Pending company" in desc(t, st)
    assert "parent_id" not in partner_writes(t)[0]["vals"]


def test_a_timer_never_overwrites_a_flow_that_just_finished():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    stale = hub.store.get("intake", KNOWN)
    s.tap(hub, "bug", wa=KNOWN)
    hub.clock.advance(hours=3)
    assert hub.intake.timer_pass(hub.clock()) == 0 and stale["step"] == "awaiting_type"
    assert hub.store.get("intake", KNOWN)["step"] == "done" and len(hub.all_tickets()) == 1


def test_unknown_button_id_is_an_unrecognised_answer_not_a_crash():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    s.text(hub, "id:zzz", wa=KNOWN)
    assert hub.all_tickets() == [] and len([x for x in sent_texts(hub) if "3" in x]) == 1      # the one re-ask
    res = s.tap(hub, "zzz", wa=KNOWN, title="id:zzz")                       # a forged id: still no answer, no crash
    assert "error" not in res
    (st,) = hub.all_tickets()
    assert st.intake_type == "question" and len(holds(hub, "intake_type_defaulted")) == 1


def test_a_stale_button_tap_is_ignored():
    hub, s = hub_for(), Seq()
    res = s.tap(hub, "bug", wa=KNOWN)
    assert res == {"ignored": "stale_button"} and hub.all_tickets() == [] and hub.whatsapp.sent == []


def test_a_refused_contact_save_never_loops():
    t, s = transport(), Seq()
    t.data["res.partner"][6001] = {"id": 6001, "name": "Twin", "phone": "+201112223334", "user_ids": False}
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    s.text(hub, "Initech Trading")                                             # the phone already belongs to a partner
    assert len(holds(hub, "intake_contact_refused")) == 1 and hub.all_tickets() == []
    sent = len(hub.whatsapp.sent)
    s.text(hub, "hello?")
    s.text(hub, "anyone there")
    assert len(hub.whatsapp.sent) == sent and len(holds(hub, "intake_contact_refused")) == 1   # no new question, no new alert


def test_a_refused_company_link_still_opens_the_ticket_as_pending_company():
    t, s = transport(), Seq()
    t.data["res.partner"][5009] = {"id": 5009, "name": "NoCo", "is_company": False, "parent_id": False,
                                   "phone": "+201009990001", "user_ids": False}
    hub = hub_for(t, partners={KNOWN: 5001, "201009990001": 5009})
    s.text(hub, PROBLEM, wa="201009990001")
    hub.odoo.link_client_contact = lambda *a: (_ for _ in ()).throw(__import__("twin_agent.helpdesk.guard", fromlist=["x"]).WriteRefused("no"))
    s.text(hub, "Initech Trading", wa="201009990001")
    s.tap(hub, "bug", wa="201009990001")
    (st,) = hub.all_tickets()
    assert len(holds(hub, "intake_contact_refused")) == 1 and "Pending company" in desc(t, st)


def test_flag_off_keeps_the_old_behaviour():
    hub = build_hub(transport=transport(), knowledge_odoo=fakes.FakeOdooKnowledge(partners={KNOWN: 5001}))
    assert hub.cfg.get("intake_v2") is None
    s = Seq()
    s.text(hub, PROBLEM)                                                    # unknown sender: held, nothing sent
    assert hub.whatsapp.sent == [] and len(holds(hub, "unknown_sender")) == 1
    s.text(hub, PROBLEM, wa=KNOWN)                                          # known sender: today's ack, no buttons
    assert sent_texts(hub)[-1].startswith("Thank you, ") and not any("interactive" in x for x in hub.whatsapp.sent)


def windows_for(*numbers):
    """The test-client windows as the production runtime exposes them (test_clients AND test_numbers feed ONE object)."""
    return types.SimpleNamespace(active=lambda d: object() if d in numbers else None)


def test_test_only_mode_applies_to_senders_with_an_open_test_window_only():
    know = fakes.FakeOdooKnowledge(partners={})
    know.windows = windows_for(NEW)
    hub, s = hub_for(cfg={"intake_v2": "test_only"}, knowledge=know), Seq()
    s.text(hub, PROBLEM)
    assert sent_texts(hub) == [COMPANY_Q]
    s.text(hub, PROBLEM, wa="201119998887")                                 # no window: the old flow (held)
    assert len(sent_texts(hub)) == 1 and len(holds(hub, "unknown_sender")) == 1


def test_a_sender_with_a_test_window_is_never_linked_to_a_real_company():
    t, s = transport(), Seq()
    know = fakes.FakeOdooKnowledge(partners={})
    know.windows = windows_for(NEW)
    hub = hub_for(t, cfg={"intake_v2": "test_only"}, knowledge=know)
    s.text(hub, PROBLEM)
    s.text(hub, "Initech Trading")
    assert "parent_id" not in partner_writes(t)[0]["vals"]
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    assert "Company (client said): Initech Trading" in desc(t, st) and st.intake_company_id == 4001


def test_a_test_sender_ticket_stays_in_team_2_whatever_the_type_a_real_client_is_routed():
    t, s = transport(), Seq()
    know = fakes.FakeOdooKnowledge(partners={KNOWN: 5001})
    know.windows = windows_for(NEW)
    hub = hub_for(t, knowledge=know)
    s.text(hub, PROBLEM)
    s.text(hub, "Initech Trading")
    s.tap(hub, "question")                                                  # a Question: team 11 for a real client
    s.text(hub, PROBLEM, wa=KNOWN)
    s.tap(hub, "question", wa=KNOWN)
    test_t, real_t = hub.all_tickets()
    assert t.data["helpdesk.ticket"][test_t.tid]["team_id"] == 2
    assert t.data["helpdesk.ticket"][real_t.tid]["team_id"] == 11


def test_test_client_is_asked_every_time_but_never_linked_to_a_company():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=STAFF)
    assert sent_texts(hub)[0] == COMPANY_Q                                  # its contact has no company: always asked
    s.text(hub, "Initech Trading", wa=STAFF)
    assert partner_writes(t) == []                                          # never parent_id on a test client
    s.tap(hub, "bug", wa=STAFF)
    (st,) = hub.all_tickets()
    assert st.intake_company_id == 4001                                     # the match is kept in hub state
    assert "Company (client said): Initech Trading" in desc(t, st)
    assert t.data["res.partner"][5002]["parent_id"] is False


def test_a_staff_number_is_never_asked_anything():
    know = fakes.FakeOdooKnowledge(partners={})
    know.staff = types.SimpleNamespace(contains=lambda d: d == STAFF)
    hub, s = hub_for(knowledge=know), Seq()
    s.text(hub, PROBLEM, wa=STAFF)
    assert hub.whatsapp.sent == [] and len(holds(hub, "unknown_sender")) == 1


def test_the_production_sender_refuses_intake_questions_to_staff():
    from twin_agent.hub.prod_support import ProdWhatsApp
    know = types.SimpleNamespace(partner_for=lambda n: None, staff=types.SimpleNamespace(contains=lambda d: d == STAFF),
                                 windows=None)
    wa = ProdWhatsApp(fakes.FakeWhatsApp(), know, send_enabled=True, takes_automatic=False)
    wa.intake_sender = lambda n: True
    with pytest.raises(whatsapp_cloud.WhatsAppRefused):
        wa.send_text(STAFF, COMPANY_Q, last_inbound_at=BASE_TS, intake=True)
    wa.send_text(NEW, COMPANY_Q, last_inbound_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                 now=None, intake=True)                                     # a stranger with an open flow is allowed


@pytest.mark.parametrize("bad", [lambda *a, **k: ["internal_name"], lambda *a, **k: 1 / 0, lambda *a, **k: None,
                                 lambda *a, **k: {"ok": True}])
def test_the_wording_lint_fails_closed(monkeypatch, bad):
    monkeypatch.setattr(intake_flow, "lint", bad)
    hub, s = hub_for(), Seq()
    s.text(hub, PROBLEM)
    assert hub.whatsapp.sent == [] and len(holds(hub, "intake_send_blocked")) == 1


def test_the_real_lint_is_clean_on_every_intake_text():
    from twin_agent.hub import persona
    for key in ("company_question", "company_reask"):
        assert intake_flow.IntakeFlow._lint_ok(persona.text(key, "en"), "en")
    assert intake_flow.IntakeFlow._lint_ok(persona.text("confirmation", "en", name="Nina", n=7, subject="Invoice total wrong"), "en")
    assert not intake_flow.IntakeFlow._lint_ok("Hello from the Hub, Claude here. Can you? Can you?", "en")


def test_a_closed_window_sends_nothing_and_alerts():
    hub, s = hub_for(), Seq()
    s.text(hub, PROBLEM)
    hub.whatsapp.window_hours = 0.0001                                      # the window is shut for the next question
    s.text(hub, "Initech Trading")
    assert len(holds(hub, "intake_send_blocked")) == 1                      # the buttons were not sent


def test_retry_of_the_message_that_opened_the_ticket_makes_no_second_ticket():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    s.tap(hub, "bug", wa=KNOWN)
    (st,) = hub.all_tickets()
    hub.store.update("intake", KNOWN, lambda d: {**d, "step": "awaiting_type", "handled": d["handled"][:1],
                                                 "first": hub.ticket(st.tid).inbound.to_dict(), "context": [],
                                                 "updated_at": hub.clock().isoformat()})
    s.tap(hub, "bug", wa=KNOWN)                                             # redelivery after a crash: resumes
    assert [x.tid for x in hub.all_tickets()] == [st.tid]


def test_production_config_flag_defaults_off_and_validates():
    from twin_agent.hub import production
    assert production.config_from_dict({}).intake_v2 == "off"
    assert production.config_from_dict({"intake_v2": "test_only", "test_numbers": [{"phone": "qa", "until": "2026-12-01T00:00:00Z", "ref": "qa"}]}).intake_v2 == "test_only"
    assert production.config_from_dict({"intake_v2": False}).intake_v2 == "off"
    with pytest.raises(production.ProductionRefused):
        production.config_from_dict({"intake_v2": "maybe"})


def test_production_wrapper_lets_the_company_question_reach_an_unknown_sender():
    from test_hub_production import build, make_cfg, payload
    unknown = "201117776665"
    hub, wa = build(make_cfg(intake_v2="on"))
    hub.handle_webhook(payload(wa_id=unknown))
    assert [s["text"] for s in wa.sent] == [COMPANY_Q]
    assert hub.store.get("intake", unknown)["step"] == "awaiting_company"


def test_send_refusal_is_held_not_a_failed_message():
    hub, s = hub_for(), Seq()
    hub.whatsapp.send_text = lambda *a, **k: (_ for _ in ()).throw(whatsapp_cloud.WhatsAppRefused("sending is off"))
    res = s.text(hub, PROBLEM)
    assert "error" not in res and len(holds(hub, "intake_send_blocked")) == 1


def test_pending_company_tag_is_applied_when_the_tag_exists():
    t, s = transport(), Seq()
    t.data["helpdesk.tag"][77] = {"id": 77, "name": "Pending company"}
    hub = hub_for(t)
    s.text(hub, PROBLEM)
    s.text(hub, "Zeta Unknown Corp")
    s.tap(hub, "bug")
    (st,) = hub.all_tickets()
    assert 77 in t.data["helpdesk.ticket"][st.tid]["tag_ids"]


def test_a_forced_filing_that_fails_in_odoo_alerts_once_not_every_minute():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, PROBLEM, wa=KNOWN)
    hub.clock.advance(hours=3)
    real = hub.odoo.create_ticket
    hub.odoo.create_ticket = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("odoo down"))
    for _ in range(3):
        hub._tick_pass("intake", lambda: hub.intake.timer_pass(hub.clock()), 0)
    assert len(holds(hub, "intake_type_defaulted")) == 1 and hub.all_tickets() == []
    hub.odoo.create_ticket = real
    hub.tick()
    assert len(hub.all_tickets()) == 1 and len(holds(hub, "intake_type_defaulted")) == 1


def test_the_clients_own_braces_cannot_block_their_confirmation():
    t, s = transport(), Seq()
    hub = hub_for(t)
    s.text(hub, "The {discount} label shows [[x]] on the invoice", wa=KNOWN)
    s.tap(hub, "bug", wa=KNOWN)
    (st,) = hub.all_tickets()
    assert "{" not in st.intake_subject and sent_texts(hub)[-1].startswith("Thank you")
    assert holds(hub, "intake_send_blocked") == []


def test_end_to_end_test_client_company_button_team_2_autopilot_reply_close(tmp_path):
    """A test client: company question -> company answer -> type button -> ticket in team 2 (even a Question) -> the
    autopilot approves the reply, it is sent, and the ticket is closed."""
    from test_hub_autopilot import CLOSE, RILEY, approvals, fake, hub_with, say, tickets, win
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    hub.cfg["intake_v2"] = "test_only"
    say(hub, now, "wamid.E1", text="Hi, my invoice export shows the wrong total.")
    assert wa.sent[0]["text"] == COMPANY_Q and hub.all_tickets() == []
    fake(hub).data["res.partner"][7001] = {"id": 7001, "name": "Initech Trading", "is_company": True}
    say(hub, now, "wamid.E2", text="Initech Trading")
    assert "interactive" in wa.sent[-1]
    tap = __import__("test_hub_production").payload(wa_id=RILEY, mid="wamid.E3", ts=now[0].timestamp() + 1)
    m = tap["entry"][0]["changes"][0]["value"]["messages"][0]
    m.pop("text")
    m.update(type="interactive", interactive={"type": "button_reply",
                                              "button_reply": {"id": "type_question", "title": "Question"}})
    hub.handle_webhook(tap)
    [t] = tickets(hub)
    assert t["team_id"] == 2 and len(t["name"].split()) <= 10                  # team 2, short subject
    assert not [w for w in fake(hub).writes if w["model"] == "res.partner" and "parent_id" in w.get("vals", {})]
    assert wa.sent[-1]["text"].startswith("Thank you")                      # the one confirmation
    hub.tick(now=now[0])
    [d] = approvals(hub)
    assert d["approved"] is True and d["used_by_user"] == "one"
    assert wa.sent[-1]["to"] == RILEY and len(wa.sent) >= 4                  # question, buttons, confirmation, approved reply
    assert tickets(hub)[0]["stage_id"] == CLOSE                             # and the autopilot closed it
