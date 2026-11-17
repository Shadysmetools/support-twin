"""The production test-client window: a staff phone is a TEST client for a short, audited time, then staff again.
Fakes only: no network, no real secrets, no Odoo."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from twin_agent.helpdesk import guard
from twin_agent.hub import production, prod_support, test_clients as tc
from twin_agent.hub.production import ProductionRefused
from twin_agent.hub.team_posts import _test_client_tag

from test_hub_production import OTHER, build, fake, make_cfg, payload, tickets, transport

RILEY = "201005550001"
NAME = "Acme Test Client – Riley"
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
UNTIL = "2026-11-16T12:00:00Z"
REF = "Riley tests the live number from her phone"
CTX = dict(guard.TEST_CONTEXT)


def window(until=UNTIL, ref=REF, key="riley"):
    return {"staff_key": key, "until": until, "ref": ref}


def phones(tmp_path, **staff):
    f = tmp_path / "phones.json"
    f.write_text(json.dumps({"staff": staff or {"riley": RILEY, "supervisor": OTHER}}), encoding="utf-8")
    return str(f)


def hub_with(tmp_path, *, windows=(), clock=None, staff=None):
    cfg = make_cfg(staff_phones_file=phones(tmp_path, **(staff or {"riley": RILEY, "supervisor": OTHER})), test_clients=list(windows))
    t = transport(cfg)
    # Riley's Odoo USER partner: same phone, linked to a user (that is how staff are excluded from the client search)
    t.data["res.partner"][6001] = {"id": 6001, "name": "Riley Odoo User", "mobile": "+" + RILEY, "phone": False,
                                   "email": "riley@example.org", "user_ids": [82], "parent_id": False}
    now = [clock or T0]
    hub, wa = build(cfg, transport=t, clock=lambda: now[0])
    return hub, wa, now


def contacts(hub):
    return [r for r in fake(hub).data["res.partner"].values() if str(r.get("name", "")).startswith(guard.TEST_CLIENT_PREFIX)]


def say(hub, now, mid, wa_id=RILEY, text="Hi, my invoice export shows the wrong total."):
    return hub.handle_webhook(payload(wa_id=wa_id, mid=mid, text=text, ts=now[0].timestamp() + 1))


def audit(hub, method):
    return [r for r in hub.store.scan("odoo_audit") if r.get("method") == method]


# ---------------------------------------------------------------- inside the window

def test_inside_the_window_a_staff_phone_is_a_client_with_its_own_contact(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[window()])
    r = say(hub, now, "wamid.T1")
    assert r.get("ticket_id") and r.get("held") is None and hub.holds() == []
    [c] = contacts(hub)
    assert c["name"] == NAME and c["phone"] == "+" + RILEY and "email" not in c and not c.get("user_ids")
    [t] = tickets(hub)
    assert t["partner_id"] == c["id"] and not t["name"].startswith(guard.TEST_PREFIX)     # a real prod ticket, no TEST prefix
    assert t["name"].startswith("Acme Test Client")
    assert len(wa.sent) == 1 and wa.sent[0]["to"] == RILEY                                   # the one automatic ack


def test_the_contact_is_reused_never_duplicated_and_written_once_through_the_guard(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[window()])
    say(hub, now, "wamid.T1")
    say(hub, now, "wamid.T2", text="Also the PDF is blank.")
    assert len(contacts(hub)) == 1
    writes = [w for w in fake(hub).writes if w["model"] == "res.partner"]
    assert len(writes) == 1 and set(writes[0]["vals"]) == {"name", "phone", "comment"} and "email" not in writes[0]["vals"]


def test_only_the_test_contact_matches_never_the_users_partner_or_a_real_client_with_the_number(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[window()])
    t = fake(hub)
    t.data["res.partner"][6002] = {"id": 6002, "name": "Real Co", "mobile": "+" + RILEY, "phone": False, "email": "a@b.example.org",
                                   "user_ids": False, "parent_id": False}              # a real client sharing the number
    pid = hub.k_odoo.partner_for(RILEY)
    assert pid == contacts(hub)[0]["id"] and pid not in (6001, 6002)
    hub2, _w2, _n2 = hub_with(tmp_path, windows=[])       # no window: staff again, never matched
    assert hub2.k_odoo.partner_for(RILEY) is None


def test_other_staff_stay_staff_during_riley_window(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[window()])
    r = say(hub, now, "wamid.A1", wa_id=OTHER)
    assert r.get("held") == "staff_number" and tickets(hub) == [] and wa.sent == []


def test_the_safe_teamhub_summary_says_test_client_and_never_the_client_text(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[window()])
    r = say(hub, now, "wamid.T1", text="PRIVATE words about invoice")
    brief = hub._brief_safe(hub.ticket(r["ticket_id"]))
    assert f"Ticket #{r['ticket_id']} (test client) -> @Riley" in brief and "PRIVATE" not in brief
    st = hub.ticket(r["ticket_id"])
    assert _test_client_tag(st, f"Ticket #{st.tid}: update") == f"Ticket #{st.tid} (test client): update"
    assert _test_client_tag(st, f"Ticket #{st.tid} (test client): x") == f"Ticket #{st.tid} (test client): x"   # once only
    st.client = "Real Client Co"
    assert "(test client)" not in _test_client_tag(st, f"Ticket #{st.tid}: update")


def test_audit_rows_for_open_each_message_and_close_with_no_phone_number(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[window()])
    assert len(audit(hub, "window_open")) == 1
    say(hub, now, "wamid.T1")
    say(hub, now, "wamid.T2", text="one more thing")
    assert len(audit(hub, "test_client_message")) == 2 and audit(hub, "window_closed") == []
    now[0] = T0 + timedelta(hours=3)
    hub.tick(now=now[0])
    hub.tick(now=now[0] + timedelta(minutes=1))
    assert len(audit(hub, "window_closed")) == 1 and len(audit(hub, "window_open")) == 1      # each exactly once
    blob = json.dumps(list(hub.store.scan("odoo_audit")), default=str)
    assert RILEY not in blob and RILEY[-9:] not in blob and REF in blob
    assert any(r["reason"] == REF and r["on_behalf_of"] == "riley" for r in audit(hub, "window_open"))


# ---------------------------------------------------------------- after the window

def test_after_the_window_the_number_is_staff_again_and_the_contact_never_matches(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[window()])
    first = say(hub, now, "wamid.T1")
    assert first.get("ticket_id")
    now[0] = datetime(2026, 11, 16, 12, 0, 1, tzinfo=timezone.utc)                  # one second past until
    r = say(hub, now, "wamid.T2")
    assert r.get("held") == "staff_number" and len(tickets(hub)) == 1
    assert hub.k_odoo.partner_for(RILEY) is None and len(contacts(hub)) == 1        # the contact stays, but is unreachable
    sent = len(wa.sent)
    hub.tick(now=now[0] + timedelta(minutes=10))
    assert len(wa.sent) == sent                                                     # not even the fallback ack


def test_a_window_that_expired_before_startup_never_opens(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[window(until="2026-11-16T09:00:00Z")])
    r = say(hub, now, "wamid.T1")
    assert r.get("held") == "staff_number" and contacts(hub) == [] and wa.sent == []
    assert audit(hub, "window_open") == [] and len(audit(hub, "window_closed")) == 1


# ---------------------------------------------------------------- find-or-create edge cases

def _row(**o):
    return {"id": 7001, "name": NAME, "phone": "+" + RILEY, "email": False, "user_ids": False, "parent_id": False, **o}


@pytest.mark.parametrize("extra,ok", [({}, True), ({"email": "x@y.example.org"}, False), ({"user_ids": [82]}, False)])
def test_an_existing_contact_is_reused_unless_it_has_an_email_or_a_user(tmp_path, extra, ok):
    hub, _wa, now = hub_with(tmp_path, windows=[window()])
    fake(hub).data["res.partner"][7001] = _row(**extra)
    assert hub.k_odoo.partner_for(RILEY) == (7001 if ok else None)
    assert len(contacts(hub)) == 1 and not [w for w in fake(hub).writes if w["model"] == "res.partner"]


def test_two_contacts_with_the_name_are_refused_not_guessed(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[window()])
    fake(hub).data["res.partner"][7001] = _row()
    fake(hub).data["res.partner"][7002] = _row(id=7002)
    assert hub.k_odoo.partner_for(RILEY) is None


def test_the_phone_search_skips_a_test_contact_without_a_window(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[], staff={"supervisor": OTHER})
    fake(hub).data["res.partner"][7001] = _row(mobile="+" + RILEY, phone=False)
    assert hub.k_odoo.partner_for(RILEY) is None


# ---------------------------------------------------------------- the guard

def _check(vals=None, kwargs=None, model="res.partner", method="create", args=None):
    a = [vals] if args is None else args
    guard.check("test_client_contact", model, method, a, {"context": CTX} if kwargs is None else kwargs)


GOOD = {"name": NAME, "phone": "+" + RILEY}


def test_guard_allows_the_exact_contact_and_the_optional_comment_and_mobile():
    _check(GOOD)
    _check({"name": NAME, "mobile": "+" + RILEY, "comment": "why"})
    _check({"name": guard.TEST_CLIENT_PREFIX + "Supervisor Leo", "phone": RILEY})


@pytest.mark.parametrize("vals", [
    {"name": "Acme Test Client - Riley", "phone": "+201005550001"},                  # a hyphen, not the en dash
    {"name": "acme test client – Riley", "phone": "+201005550001"},             # wrong case
    {"name": "Acme Test Client – ", "phone": "+201005550001"},                 # no name
    {"name": NAME + " <a@b.example.org>", "phone": "+201005550001"},                            # an address smuggled into the name
    {"name": "TEST Client (Support Twin)", "phone": "+201005550001"},
    {"name": "Real Client Co", "phone": "+201005550001"},
    {"name": NAME, "phone": "+201005550001", "email": "a@b.example.org"},                        # no email, ever
    {"name": NAME, "phone": "+201005550001", "email": False},
    {"name": NAME, "phone": "+201005550001", "is_company": True},
    {"name": NAME, "phone": "+201005550001", "user_ids": [[4, 82]]},
    {"name": NAME, "phone": "+201005550001", "parent_id": 5001},
    {"name": NAME},                                                                    # no phone at all
    {"name": NAME, "phone": "call me"},
    {"name": NAME, "phone": "+201005550001", "comment": "x" * 301},
    {"phone": "+201005550001"},
])
def test_guard_refuses_a_bad_contact(vals):
    with pytest.raises(guard.WriteRefused):
        _check(vals)


@pytest.mark.parametrize("kwargs", [{}, {"context": {**CTX, "force_send": True}}, {"context": {"tracking_disable": True}},
                                    {"context": CTX, "email_from": "x@y.example.org"}])
def test_guard_needs_the_exact_mail_off_context(kwargs):
    with pytest.raises(guard.WriteRefused):
        _check(GOOD, kwargs=kwargs)


@pytest.mark.parametrize("model,method,args", [
    ("res.partner", "unlink", [[7001]]), ("res.partner", "write", [[7001], {"email": "a@b.example.org"}]),
    ("res.users", "create", [{"name": "x"}]), ("helpdesk.ticket", "create", [{"name": "x"}]),
    ("helpdesk.ticket", "write", [[1], {"stage_id": 14}]), ("ir.rule", "create", [{"name": "x"}]),
    ("res.partner", "create", [GOOD, GOOD]), ("res.partner", "create", [[GOOD, GOOD]]),
])
def test_guard_allows_no_other_model_or_method_and_one_record(model, method, args):
    with pytest.raises(guard.WriteRefused):
        _check(model=model, method=method, args=args)


def test_the_other_purposes_still_refuse_the_new_contact():
    for purpose in ("helpdesk", "timesheet"):
        with pytest.raises(guard.WriteRefused):
            guard.check(purpose, "res.partner", "create", [GOOD], {"context": CTX})
    with pytest.raises(guard.WriteRefused):                      # the old TEST-ticket scope does not take it either
        guard.check("test_ticket", "res.partner", "create", [GOOD], {"context": CTX})


# ---------------------------------------------------------------- config + preflight

def _why(now=T0, **o):
    return production.problems(make_cfg(**o), have_app_secret=True, have_verify_token=True, now=now)


def test_config_parses_utc_forms_and_the_default_is_no_window():
    assert make_cfg().test_clients == ()
    for u in ("2026-11-16T12:00:00Z", "2026-11-16T12:00:00", "2026-11-16T14:00:00+02:00"):
        [w] = make_cfg(test_clients=[window(until=u)]).test_clients
        assert w.until == datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc) and w.staff_key == "riley"


def test_preflight_limits_24h_and_a_blank_ref():
    assert _why(test_clients=[window()]) == []
    assert _why(test_clients=[window(until="2026-11-17T09:59:00Z")]) == []
    assert any("24 hours" in x for x in _why(test_clients=[window(until="2026-11-17T10:00:01Z")]))
    assert any("ref" in x for x in _why(test_clients=[window(ref="")]))
    assert any("ref" in x for x in _why(test_clients=[window(ref="   ")]))
    assert any("ref" in x for x in _why(test_clients=[{"staff_key": "riley", "until": UNTIL}]))
    assert any("one window" in x for x in _why(test_clients=[window(), window()]))
    with pytest.raises(ProductionRefused, match="24 hours"):
        production.check_config(make_cfg(test_clients=[window(until="2099-01-01T00:00:00Z")]),
                                production.Secrets("a", "s", "v", "", ""))


@pytest.mark.parametrize("until", ["soon", "", None, 12])
def test_config_refuses_a_bad_until(until):
    with pytest.raises(ProductionRefused):
        make_cfg(test_clients=[{"staff_key": "riley", "until": until, "ref": "x"}])


@pytest.mark.parametrize("entry", [{"staff_key": "../etc", "until": UNTIL, "ref": "x"},
                                   {"staff_key": "riley", "until": UNTIL, "ref": "x", "email": "a@b.example.org"}, "riley"])
def test_config_refuses_a_bad_entry(entry):
    with pytest.raises(ProductionRefused):
        make_cfg(test_clients=[entry])
    with pytest.raises(ProductionRefused):
        make_cfg(test_clients={"staff_key": "riley"})


def test_an_open_window_needs_the_key_in_the_staff_file_but_an_expired_one_does_not(tmp_path):
    cfg = make_cfg(staff_phones_file=phones(tmp_path, supervisor=OTHER), test_clients=[window(key="riley")])
    with pytest.raises(ProductionRefused, match="staff phones file"):
        production.build_hub(cfg, production.Secrets("a", "s", "v", "", ""), dry_run=True, clock=lambda: T0,
                             transport=transport(cfg))
    old = make_cfg(staff_phones_file=phones(tmp_path, supervisor=OTHER), test_clients=[window(key="riley", until="2026-11-15T00:00:00Z")])
    hub = production.build_hub(old, production.Secrets("a", "s", "v", "", ""), dry_run=True, clock=lambda: T0,
                               transport=transport(old))
    assert hub.windows is not None and not hub.windows.is_open(RILEY)


def test_the_preflight_report_names_the_window(tmp_path):
    cfg = make_cfg(staff_phones_file=phones(tmp_path), test_clients=[window()])
    assert any("Test client window: riley until 2026-11-16T12:00:00+00:00" in x for x in production.preflight(cfg, dry_run=True))


def test_the_window_is_a_noop_without_config_and_staff_contains_is_unchanged(tmp_path):
    s = prod_support.StaffNumbers(phones(tmp_path))
    assert s.contains(RILEY) and s.phone_of("Riley") == RILEY and s.phone_of("nobody") == ""
    s.exempt = lambda wa: True
    assert not s.contains(RILEY) and s.is_staff_number(RILEY)
    assert tc.parse_windows(None) == () and tc.contact_name("riley") == NAME


# ---------------------------------------------------------------- review round: the LOW fixes

FOREIGN = "441005550001"                      # another country, the SAME last 9 digits as Riley's number


def test_a_foreign_number_sharing_the_last_9_digits_never_becomes_the_test_client(tmp_path):
    hub, wa, now = hub_with(tmp_path, windows=[window()])
    assert hub.windows.active(FOREIGN) is None and hub.windows.active(RILEY) is not None
    assert hub.k_odoo.partner_for(FOREIGN) is None
    assert hub.windows.expired_for(FOREIGN) is None and contacts(hub) == []


def test_a_staff_phone_in_local_format_cannot_open_a_window(tmp_path):
    cfg = make_cfg(staff_phones_file=phones(tmp_path, riley="01005550001"), test_clients=[window()])
    with pytest.raises(ProductionRefused, match="full international"):
        production.build_hub(cfg, production.Secrets("a", "s", "v", "", ""), dry_run=True, clock=lambda: T0,
                             transport=transport(cfg))
    assert tc.international(RILEY) and not tc.international("01005550001") and not tc.international("12345")


def test_the_contact_phone_is_the_normalized_international_number(tmp_path):
    hub, _wa, now = hub_with(tmp_path, windows=[window()], staff={"riley": "+20 100-555 0001"})
    say(hub, now, "wamid.T1")
    assert contacts(hub)[0]["phone"] == "+" + RILEY


@pytest.mark.parametrize("extra", [{"parent_id": [5001, "Real Co"]}, {"active": False}])
def test_a_contact_under_a_company_or_archived_is_held_not_reused_or_duplicated(tmp_path, extra):
    hub, _wa, now = hub_with(tmp_path, windows=[window()])
    fake(hub).data["res.partner"][7001] = _row(**extra)
    assert hub.k_odoo.partner_for(RILEY) is None
    assert len(contacts(hub)) == 1 and not [w for w in fake(hub).writes if w["model"] == "res.partner"]
    r = say(hub, now, "wamid.T1")
    assert r.get("held") and tickets(hub) == []


def test_the_search_for_the_contact_includes_archived_ones():
    seen = []

    class Odoo:
        def search_read(self, model, domain, fields, **kw):
            seen.append(domain)
            return [{"id": 9, "name": NAME, "email": False, "user_ids": False, "parent_id": False, "active": True}]
    w = tc.ClientTestWindow("riley", T0 + timedelta(hours=1), "x")
    staff = type("S", (), {"phone_of": lambda self, k: RILEY})()
    assert tc.ClientTestWindows([w], staff, clock=lambda: T0).contact_id(Odoo(), w) == 9
    assert ["active", "in", [True, False]] in seen[0]


def test_with_odoo_writes_off_the_message_is_held_not_an_error(tmp_path):
    class Odoo:
        def search_read(self, *a, **k):
            return []

        def create_test_client_contact(self, *a, **k):
            raise guard.WriteRefused("observe: Odoo writes are off")
    w = tc.ClientTestWindow("riley", T0 + timedelta(hours=1), "x")
    staff = type("S", (), {"phone_of": lambda self, k: RILEY})()
    assert tc.ClientTestWindows([w], staff, clock=lambda: T0).contact_id(Odoo(), w) is None


def test_a_reply_refused_after_the_window_closed_is_logged_and_audited(tmp_path, caplog):
    from datetime import timezone as _tz
    from twin_agent.channels import whatsapp_cloud
    hub, wa, now = hub_with(tmp_path, windows=[window()])
    say(hub, now, "wamid.T1")
    now[0] = datetime(2026, 11, 16, 12, 5, tzinfo=_tz.utc)
    caplog.set_level("WARNING")
    with pytest.raises(whatsapp_cloud.WhatsAppRefused, match="test client window is closed"):
        hub.whatsapp.send_text(RILEY, "hello", last_inbound_at=now[0], now=now[0])
    [row] = audit(hub, "reply_refused_window_closed")
    assert row["on_behalf_of"] == "riley" and RILEY not in json.dumps(row, default=str)
    assert "window for riley is closed" in caplog.text and RILEY not in caplog.text
    with pytest.raises(whatsapp_cloud.WhatsAppRefused, match="not a known client"):          # an ordinary stranger: unchanged
        hub.whatsapp.send_text(FOREIGN, "hello", last_inbound_at=now[0], now=now[0])
    assert len(audit(hub, "reply_refused_window_closed")) == 1
