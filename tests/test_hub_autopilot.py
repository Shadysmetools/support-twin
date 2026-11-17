"""Test-client autopilot: One approves a TEST client's draft through the existing approval book, closes the ticket
through the guard purpose ``test_client_close``, and never touches a real client. Fakes only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from twin_agent.helpdesk import guard
from twin_agent.hub import autopilot as ap
from twin_agent.hub import judge as judge_mod
from twin_agent.hub.admin import data as admin_data
from twin_agent.hub.production import ProductionRefused

from test_hub_production import CLIENT, OTHER, build, fake, make_cfg, payload, tickets, transport

RILEY = "201005550001"
TESTNUM = "201007770009"
T0 = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
UNTIL = "2026-11-17T12:00:00Z"
REF = "autopilot check"
CTX = dict(guard.TEST_CONTEXT)
CLOSE = 4


def phones(tmp_path):
    f = tmp_path / "phones.json"
    f.write_text(json.dumps({"staff": {"riley": RILEY, "supervisor": OTHER}, "test_numbers": {"metatest": TESTNUM}}), encoding="utf-8")
    return str(f)


def hub_with(tmp_path, *, windows=(), numbers=(), close=CLOSE):
    cfg = make_cfg(staff_phones_file=phones(tmp_path), test_clients=list(windows), test_numbers=list(numbers),
                   test_client_close_stage=close)
    t = transport(cfg)
    t.data["res.partner"][6001] = {"id": 6001, "name": "Riley Odoo User", "mobile": "+" + RILEY, "phone": False,
                                   "email": "riley@example.org", "user_ids": [82], "parent_id": False}
    now = [T0]
    hub, wa = build(cfg, transport=t, clock=lambda: now[0])
    return hub, wa, now


def win(key="riley"):
    return {"staff_key": key, "until": UNTIL, "ref": REF}


def say(hub, now, mid, wa_id=RILEY, text="Hi, my invoice export shows the wrong total."):
    return hub.handle_webhook(payload(wa_id=wa_id, mid=mid, text=text, ts=now[0].timestamp() + 1))


def approvals(hub):
    return sorted(hub.store.scan("approvals"), key=lambda d: d["issued_at"])


def audit(hub, method):
    return [r for r in hub.store.scan("odoo_audit") if r.get("role") == ap.AUTOPILOT_ROLE and r.get("method") == method]


def texts(wa):
    return [m for m in wa.sent]


# ---------------------------------------------------------------- the autopilot itself

def test_a_test_client_draft_is_approved_by_one_sent_and_closed(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    r = say(hub, now, "wamid.A1")
    [d] = approvals(hub)
    assert d["autopilot"] is True and d["used_at"] is None and len(wa.sent) == 1            # only the ack so far
    hub.tick(now=now[0])
    [d] = approvals(hub)
    assert d["approved"] is True and d["used_by_user"] == "one" and d["used_by_name"] == ap.AUTOPILOT_LABEL
    assert len(wa.sent) == 2 and wa.sent[1]["to"] == RILEY                                    # the approved confirm went out
    [t] = tickets(hub)
    assert t["stage_id"] == CLOSE
    stage_writes = [w for w in fake(hub).writes if w["method"] == "write" and w["vals"] == {"stage_id": CLOSE}]
    assert len(stage_writes) == 1
    assert [x["outcome"] for x in audit(hub, "approve")] == ["ok"] and [x["outcome"] for x in audit(hub, "close")] == ["ok"]
    assert "AUTOPILOT-CLOSE" in hub.ticket(r["ticket_id"]).history


def test_the_attribution_strings(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[win()])
    r = say(hub, now, "wamid.A1")
    hub.tick(now=now[0])
    assert ap.AUTOPILOT_LABEL == "Approved automatically by One (test client)" and ap.AUTOPILOT_ROLE == "autopilot (test client)"
    [d] = approvals(hub)
    assert admin_data.approver_name(None, d) == ap.AUTOPILOT_LABEL
    assert admin_data.approved_line(ap.AUTOPILOT_LABEL) == ap.AUTOPILOT_LABEL and admin_data.approved_line("Riley") == "Approved by Riley"
    from twin_agent.hub import readonly_api_views as v
    st = hub.ticket(r["ticket_id"])
    ev = v._approval_events(st, approvals(hub), None)
    assert any(e.get("text") == ap.AUTOPILOT_LABEL or ap.AUTOPILOT_LABEL in json.dumps(e) for e in ev)


def test_a_real_client_still_needs_a_human(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.R1", wa_id=CLIENT)
    [d] = approvals(hub)
    assert d["autopilot"] is False
    hub.tick(now=now[0])
    [d] = approvals(hub)
    assert d["used_at"] is None and len(wa.sent) == 1 and tickets(hub)[0]["stage_id"] != CLOSE
    assert not any(w["vals"] == {"stage_id": CLOSE} for w in fake(hub).writes if w["method"] == "write")
    assert audit(hub, "approve") == []


def test_no_window_configured_means_no_autopilot_at_all(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[])
    say(hub, now, "wamid.N1", wa_id=CLIENT)
    hub.tick(now=now[0])
    assert approvals(hub)[0]["autopilot"] is False and hub.autopilot_sweep() == 0


def test_a_window_closed_between_request_and_send_is_refused(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.W1")
    assert approvals(hub)[0]["autopilot"] is True
    now[0] = T0 + timedelta(hours=3)                                                          # past ``until``
    hub.tick(now=now[0])
    d = approvals(hub)[0]
    assert d["used_at"] is None and d["autopilot"] is False and len(wa.sent) == 1
    assert [x["outcome"] for x in audit(hub, "refused")] == ["refused"]
    assert not any(w["vals"] == {"stage_id": CLOSE} for w in fake(hub).writes if w["method"] == "write")


def test_the_window_is_rechecked_at_the_send_itself(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    r = say(hub, now, "wamid.W2")
    nonce = approvals(hub)[0]["nonce"]
    orig = hub._autopilot_window
    calls = []

    def closing(state):                                   # open for the approve step, closed by the time of the send
        calls.append(1)
        return orig(state) if len(calls) == 1 else None
    hub._autopilot_window = closing
    res = hub.autopilot_approve(nonce)
    assert res == {"ok": False, "reason": "test_window_closed"} and len(wa.sent) == 1
    assert any(x["reason"].startswith("the test client window closed") for x in audit(hub, "refused"))
    assert not any(w["vals"] == {"stage_id": CLOSE} for w in fake(hub).writes if w["method"] == "write")
    assert hub.ticket(r["ticket_id"]).history.count("8-CONFIRM") == 0


@pytest.mark.parametrize("text", ["Hi", "x" * 3000])
def test_a_draft_outside_the_length_bounds_goes_to_a_human(text):
    assert any("length" in p for p in ap.deterministic_problems(text, "unrelated client message here"))


@pytest.mark.parametrize("draft,why", [
    ("", "empty"),
    ("Thanks. What the client said (English): the invoice export total is wrong, please check it soon.", "label"),
    ("We understand that What the client expects is a fix for the export and we will look at it now.", "label"),
    ("Hi, my invoice export shows the wrong total.", "paste"),
    ("Hello Mia, we will check the [[module]] problem and update you here shortly, thank you.", "placeholder"),
    ("أهلاً، سنراجع المشكلة ونعود إليك قريباً بتحديث كامل ان شاء الله.", "English"),
])
def test_deterministic_gate_failures(draft, why):
    ps = ap.deterministic_problems(draft, "Hi, my invoice export shows the wrong total.")
    assert ps and any(why in p for p in ps)


def test_a_good_draft_passes_the_deterministic_gate():
    d = "Hello Mia, we understand your invoice export shows a different total than expected. We will check it and update you here."
    assert ap.deterministic_problems(d, "Hi, my invoice export shows the wrong total.") == []


def test_a_gate_failure_makes_a_normal_human_request_with_a_hold_reason(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ap, "_lint", lambda text, *, client_text, lang: ["too formal"])
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.G1")
    [d] = approvals(hub)
    assert d["autopilot"] is False and d["used_at"] is None                                   # a person decides
    assert any("wording lint: too formal" in x["reason"] for x in audit(hub, "held"))
    hub.tick(now=now[0])
    assert len(wa.sent) == 1 and tickets(hub)[0]["stage_id"] != CLOSE


def test_a_lint_that_crashes_fails_closed(monkeypatch):
    def boom(text, *, client_text, lang):
        raise RuntimeError("x")
    monkeypatch.setattr(ap, "_lint", boom)
    assert ap.lint_problems("fine text") == ["the wording lint could not run (RuntimeError)"]


def test_real_judge_needs_a_clean_score_a_fake_judge_is_not_trusted():
    class Real:
        real, threshold = True, 0.7
    assert ap.judge_problems(Real(), {"aggregate": 0.9, "flag": False, "unjudged": False}) == []
    assert ap.judge_problems(Real(), {"aggregate": 0.5, "flag": True, "unjudged": False})
    assert ap.judge_problems(Real(), {"aggregate": None, "flag": True, "unjudged": True})
    assert ap.judge_problems(Real(), None)
    assert ap.judge_problems(judge_mod.FakeJudge(), None) == []                                # deterministic checks stand in


def test_a_resolve_always_needs_a_person(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.K1")
    st = hub.all_tickets()[0]
    chk = hub._autopilot_check(st, "resolve", "Your issue is fixed and here is a short video showing it works fine.", None)
    assert chk["ok"] is False and "resolve" in chk["reason"]


# ---------------------------------------------------------------- test_numbers

def test_a_non_staff_test_number_is_a_test_client_inside_its_window(tmp_path):
    hub, wa, now = hub_with(tmp_path, numbers=[{"phone": "metatest", "until": UNTIL, "ref": REF}])
    r = say(hub, now, "wamid.M1", wa_id=TESTNUM)
    assert r.get("ticket_id") and hub.holds() == []
    [t] = tickets(hub)
    assert fake(hub).data["res.partner"][t["partner_id"]]["name"] == "Acme Test Client – Metatest"
    hub.tick(now=now[0])
    assert approvals(hub)[0]["used_by_user"] == "one" and tickets(hub)[0]["stage_id"] == CLOSE
    blob = json.dumps(list(hub.store.scan("odoo_audit")), default=str)
    assert TESTNUM not in blob and TESTNUM[-9:] not in blob


def test_a_test_number_after_the_window_is_unknown_again(tmp_path):
    hub, wa, now = hub_with(tmp_path, numbers=[{"phone": "metatest", "until": UNTIL, "ref": REF}])
    now[0] = T0 + timedelta(hours=3)
    r = say(hub, now, "wamid.M2", wa_id=TESTNUM)
    assert r.get("held") == "unknown_sender" and tickets(hub) == []
    assert hub.k_odoo.partner_for(TESTNUM) is None


def test_test_numbers_config_rails(tmp_path):
    from twin_agent.hub import production
    ok = make_cfg(staff_phones_file=phones(tmp_path), test_numbers=[{"phone": "metatest", "until": "2099-01-01T00:00:00Z", "ref": REF}])
    assert any("24 hours" in p for p in production.problems(ok, have_app_secret=True, have_verify_token=True))
    blank = make_cfg(test_numbers=[{"phone": "metatest", "until": UNTIL, "ref": " "}])
    assert any("ref" in p for p in production.problems(blank, have_app_secret=True, have_verify_token=True))
    for bad in ([{"phone": "201007770009", "until": UNTIL, "ref": REF}], [{"phone": "a", "until": UNTIL, "ref": REF, "x": 1}], "no"):
        with pytest.raises(ProductionRefused):
            make_cfg(test_numbers=bad)
    with pytest.raises(ProductionRefused):                         # an open window whose key is not in the phones file
        hub_with(tmp_path, numbers=[{"phone": "ghost", "until": UNTIL, "ref": REF}])
    assert any("test_client_close_stage" in p for p in production.problems(
        make_cfg(test_client_close_stage=17), have_app_secret=True, have_verify_token=True))


# ---------------------------------------------------------------- the close rail (guard purpose test_client_close)

TEST_NAME = "Acme Test Client – Riley"


def world(**over):
    rows = {
        "helpdesk.ticket": {7: {"id": 7, "partner_id": 50, "team_id": 2}},
        "res.partner": {50: {"id": 50, "name": TEST_NAME, "email": False, "user_ids": False, "parent_id": False}},
    }
    for k, v in over.items():
        tbl, rid = k.split("__")
        rows[tbl.replace("_", ".", 1)][int(rid)].update(v)

    def read(model, ids, fields):
        return [{"id": i, **{f: rows[model][i].get(f, False) for f in fields}} for i in ids if i in rows[model]]
    return read


def close(args=None, kwargs=None, *, stage=CLOSE, read=None, configured=CLOSE):
    guard.check("test_client_close", "helpdesk.ticket", "write", [[7], {"stage_id": stage}] if args is None else args,
                {"context": CTX} if kwargs is None else kwargs, close_stage_id=configured, team_stage_ids=[CLOSE, 5], read_fn=read or world())


def test_close_allows_the_exact_write_on_a_live_test_client_ticket():
    close()


def test_close_allows_partner_as_many2one_pair():
    def read(model, ids, fields):
        r = world()(model, ids, fields)
        return [{**x, "partner_id": [50, "x"], "team_id": [2, "Helpdesk"]} if model == "helpdesk.ticket" else x for x in r]
    close(read=read)


@pytest.mark.parametrize("over", [
    {"res_partner__50": {"name": "Real Client Co"}},                                          # a real partner
    {"res_partner__50": {"name": "Acme Test Client - Riley"}},                              # a hyphen, not the en dash
    {"res_partner__50": {"name": "acme test client – Riley"}},                              # wrong case
    {"res_partner__50": {"name": " Acme Test Client – Riley"}},                             # not at the start
    {"res_partner__50": {"name": "Acme Test Client – "}},                                  # no label
    {"res_partner__50": {"email": "a@b.example.org"}},                                                  # has an email
    {"res_partner__50": {"user_ids": [82]}},                                                   # linked to a user
    {"res_partner__50": {"parent_id": [9, "Real Co"]}},                                        # under a company
    {"helpdesk_ticket__7": {"team_id": 3}},                                                    # the wrong team
    {"helpdesk_ticket__7": {"partner_id": False}},                                             # no client
])
def test_close_refuses_a_live_ticket_that_is_not_a_test_clients(over):
    with pytest.raises(guard.WriteRefused):
        close(read=world(**over))


def test_close_refuses_a_missing_ticket_or_partner_row():
    with pytest.raises(guard.WriteRefused):
        close(args=[[99], {"stage_id": CLOSE}])
    with pytest.raises(guard.WriteRefused):
        close(read=lambda m, i, f: [])
    with pytest.raises(guard.WriteRefused):
        guard.check("test_client_close", "helpdesk.ticket", "write", [[7], {"stage_id": CLOSE}], {"context": CTX},
                    close_stage_id=CLOSE, team_stage_ids=[CLOSE], read_fn=None)


@pytest.mark.parametrize("vals", [
    {"stage_id": CLOSE, "user_id": 1}, {"stage_id": CLOSE, "name": "x"}, {"stage_id": CLOSE, "partner_id": 5},
    {"stage_id": CLOSE, "team_id": 2}, {}, {"user_id": 1}, {"stage_id": CLOSE, "description": "x"},
])
def test_close_refuses_extra_or_missing_fields(vals):
    with pytest.raises(guard.WriteRefused):
        close(args=[[7], vals])


@pytest.mark.parametrize("stage,configured", [(9, 9), (17, 17), (5, CLOSE), (CLOSE, 5), (CLOSE, None), (0, 0), (True, True), ("4", CLOSE)])
def test_close_refuses_any_stage_but_the_configured_one_never_9_or_17(stage, configured):
    with pytest.raises(guard.WriteRefused):
        close(stage=stage, configured=configured)


@pytest.mark.parametrize("args", [[[7, 8], {"stage_id": CLOSE}], [[], {"stage_id": CLOSE}], [[7]], [7, {"stage_id": CLOSE}],
                                  [[7], {"stage_id": CLOSE}, 1]])
def test_close_is_one_record_in_the_exact_shape(args):
    with pytest.raises(guard.WriteRefused):
        close(args=args)


@pytest.mark.parametrize("kwargs", [{}, {"context": {"tracking_disable": True}}, {"context": {**CTX, "force_send": True}},
                                    {"context": CTX, "x": 1}])
def test_close_needs_the_exact_mail_off_context(kwargs):
    with pytest.raises(guard.WriteRefused):
        close(kwargs=kwargs)


@pytest.mark.parametrize("model,method", [("helpdesk.ticket", "create"), ("helpdesk.ticket", "unlink"), ("res.partner", "write"),
                                          ("helpdesk.ticket", "message_post"), ("account.analytic.line", "create"), ("ir.rule", "write")])
def test_close_allows_no_other_model_or_method(model, method):
    with pytest.raises(guard.WriteRefused):
        guard.check("test_client_close", model, method, [[7], {"stage_id": CLOSE}], {"context": CTX}, close_stage_id=CLOSE,
                    team_stage_ids=[CLOSE], read_fn=world())


def test_other_purposes_cannot_close_a_ticket_with_this_rail_and_the_old_ones_still_refuse_it():
    for purpose in ("helpdesk", "test_client_contact", "timesheet"):
        with pytest.raises(guard.WriteRefused):                  # stage 4 is not a silent stage: no purpose may write it
            guard.check(purpose, "helpdesk.ticket", "write", [[7], {"stage_id": CLOSE}], {"context": CTX}, safe_stages=())


def test_the_gateway_close_writes_once_with_the_mail_off_context_and_refuses_a_real_client(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.C1")
    tid = tickets(hub)[0]["id"]
    t = fake(hub)
    t.data["res.partner"][t.data["helpdesk.ticket"][tid]["partner_id"]]["email"] = "someone@example.org"
    with pytest.raises(guard.WriteRefused):                      # the live read sees an email now: refused
        hub.odoo.close_test_client_ticket(tid)
    assert not any(w["method"] == "write" and w["vals"] == {"stage_id": CLOSE} for w in t.writes)
    t.data["res.partner"][t.data["helpdesk.ticket"][tid]["partner_id"]]["email"] = False
    hub.odoo.close_test_client_ticket(tid)
    assert t.data["helpdesk.ticket"][tid]["stage_id"] == CLOSE
    with pytest.raises(guard.WriteRefused):                      # a real client's ticket: refused by the live read
        tk = next(k for k, v in t.data["helpdesk.ticket"].items() if k > 20000)
        t.data["helpdesk.ticket"][tk]["partner_id"] = 5001
        hub.odoo.close_test_client_ticket(tk)


def test_a_close_that_the_guard_refuses_never_breaks_the_sent_reply(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    hub.odoo.close_stage = 17                                      # preflight refuses 17; bypassed here to prove the guard does too
    say(hub, now, "wamid.C2")
    hub.tick(now=now[0])
    assert len(wa.sent) == 2 and [x["outcome"] for x in audit(hub, "close")] == ["refused"]


def test_the_lint_gets_the_clients_message_and_the_label_reaches_the_team_bus(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ap, "_lint", lambda text, *, client_text, lang: seen.append((client_text, lang)) or [])
    hub, _wa, now = hub_with(tmp_path, windows=[win()])
    bus = []
    hub._ensure_thread = lambda state: "thread-ref"
    hub.team_bus.post = lambda ref, text: bus.append(text)
    say(hub, now, "wamid.B1", text="Hi, my invoice export shows the wrong total.")
    hub.tick(now=now[0])
    assert seen and seen[0] == ("Hi, my invoice export shows the wrong total.", "en")
    assert any(ap.AUTOPILOT_LABEL in t for t in bus)


def test_a_close_stage_outside_team_2_is_refused_at_the_guard_and_at_preflight(tmp_path):
    with pytest.raises(guard.WriteRefused):
        guard.check("test_client_close", "helpdesk.ticket", "write", [[7], {"stage_id": 1}], {"context": CTX},
                    close_stage_id=1, team_stage_ids=[2, 3], read_fn=world())
    with pytest.raises(ProductionRefused):
        hub_with(tmp_path, windows=[win()], close=999)


def test_the_real_lint_is_used_and_a_leaking_draft_falls_back_to_a_human(tmp_path):
    from twin_agent.hub import wording_lint
    assert ap._lint is wording_lint.lint
    leak = "Hello Mia, What the client said (English): the invoice export total is wrong. We will check it and update you here."
    assert ap.lint_problems(leak, "the invoice export total is wrong")
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.L1")
    st = hub.all_tickets()[0]
    chk = hub._autopilot_check(st, "confirm", leak, None)
    assert chk["ok"] is False and "leaks" in chk["reason"]


def test_the_note_after_an_autopilot_send_says_so(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.N9")
    hub.tick(now=now[0])
    notes = [m["body"] for m in fake(hub).data["mail.message"].values() if "reply sent to client" in str(m.get("body"))]
    assert notes and all("approved automatically by One (test client)" in n and "owner-approved" not in n for n in notes)


def _notes(hub):
    return [str(m.get("body")) for m in fake(hub).data["mail.message"].values() if "reply sent to client" in str(m.get("body"))]


def test_a_human_logged_in_as_one_on_a_real_client_is_a_normal_send(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.H1", wa_id=CLIENT)
    nonce = approvals(hub)[0]["nonce"]
    res = hub.on_owner_decision(nonce, hub.prod.staff_approver_id, approved=True, by="one", by_name="One Desktop")
    assert res.get("ok") and res.get("sent") and len(wa.sent) == 2
    d = approvals(hub)[0]
    assert d["used_by_user"] == "one" and d["used_by_name"] == "One Desktop" and "autopilot_approved" not in d
    assert not ap.is_autopilot(d) and audit(hub, "refused") == []
    assert _notes(hub) and all("owner-approved" in n for n in _notes(hub))
    assert admin_data.approver_name(None, d) == "One Desktop"


def test_a_human_logged_in_as_one_on_a_real_client_with_no_windows_at_all(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[])
    say(hub, now, "wamid.H5", wa_id=CLIENT)
    [d] = approvals(hub)
    res = hub.on_owner_decision(d["nonce"], hub.prod.staff_approver_id, approved=True, by="one", by_name="one")
    assert res.get("sent") and len(wa.sent) == 2 and audit(hub, "refused") == []


def test_a_human_as_one_on_a_test_client_is_attributed_as_a_human(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.H2")
    nonce = approvals(hub)[0]["nonce"]
    res = hub.on_owner_decision(nonce, hub.prod.staff_approver_id, approved=True, by="one", by_name="One Desktop")
    assert res.get("sent")
    d = approvals(hub)[0]
    assert not ap.is_autopilot(d) and d["used_by_name"] == "One Desktop"
    assert all("owner-approved" in n and "automatically" not in n for n in _notes(hub))
    assert tickets(hub)[0]["stage_id"] != CLOSE and audit(hub, "close") == []                       # the autopilot close is its own


def test_only_the_autopilot_sets_the_flag(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.H3")
    hub.tick(now=now[0])
    d = approvals(hub)[0]
    assert d["autopilot_approved"] is True and ap.is_autopilot(d) and not ap.is_autopilot(None) and not ap.is_autopilot({})
    assert not ap.is_autopilot({"used_by_user": "one"})


def test_a_human_as_one_is_not_refused_when_the_window_has_closed(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.H4", wa_id=CLIENT)
    now[0] = T0 + timedelta(hours=3)
    nonce = approvals(hub)[0]["nonce"]
    assert hub.on_owner_decision(nonce, hub.prod.staff_approver_id, approved=True, by="one", by_name="One Desktop").get("sent")


def test_after_the_autopilot_close_the_next_message_opens_a_new_ticket(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    r = say(hub, now, "wamid.X1")
    hub.tick(now=now[0])
    st = hub.ticket(r["ticket_id"])
    assert st.stage == "resolved" and st.keep_closed and st.pending_nonce == ""
    now[0] = T0 + timedelta(minutes=5)
    r2 = say(hub, now, "wamid.X2", text="Hi again, the PDF of the invoice is blank when printed.")
    assert r2.get("ticket_id") and r2["ticket_id"] != r["ticket_id"]


def test_a_failed_attribution_post_does_not_skip_the_close(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[win()])
    say(hub, now, "wamid.X3")

    def boom(state, text):
        raise RuntimeError("bus down")
    hub._post_static = boom
    hub.tick(now=now[0])
    assert tickets(hub)[0]["stage_id"] == CLOSE


# ---- M1: the close rail also refuses emails and external followers

@pytest.mark.parametrize("over", [{"helpdesk_ticket__7": {"partner_email": "a@b.example.org"}},
                                  {"helpdesk_ticket__7": {"email_cc": "c@d.example.org"}}])
def test_close_refuses_an_email_on_the_ticket(over):
    with pytest.raises(guard.WriteRefused):
        close(read=world(**over))


def _with_followers(followers, share):
    base = world(helpdesk_ticket__7={"message_partner_ids": followers})

    def read(model, ids, fields):
        if model == "res.partner" and fields == ["partner_share"]:
            return [{"id": i, "partner_share": share.get(i, True)} for i in ids if i in share]
        return base(model, ids, fields)
    return read


def test_close_allows_only_the_test_partner_and_internal_followers():
    close(read=_with_followers([50], {}))                                   # just the test partner
    close(read=_with_followers([50, 81], {81: False}))                      # plus an internal user's partner
    for fol, share in (([50, 82], {82: True}),                              # an external follower
                       ([50, 82], {}),                                       # a follower that cannot be read: fail closed
                       ([50, 81, 82], {81: False, 82: True})):
        with pytest.raises(guard.WriteRefused):
            close(read=_with_followers(fol, share))
