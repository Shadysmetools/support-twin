"""The production runner: rails, one-number webhook, health, Odoo preflight. Fakes only: no network, no real secrets."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from twin_agent.channels import whatsapp_cloud
from twin_agent.helpdesk import guard
from twin_agent.hub import fakes, production, staging, webhook
from twin_agent.hub.admin.app import AdminApp
from twin_agent.hub.admin.config import AdminConfig
from twin_agent.hub.admin.graph import FakeGraphClient
from twin_agent.hub.production import ProductionRefused, ProdOdoo, config_from_dict
from twin_agent.hub.odoo_gateway import GuardedOdoo
from twin_agent.hub.staging import EXECUTIVE_NUMBER, TEST_DISPLAY, TEST_PHONE_NUMBER_ID, Secrets
from twin_agent.hub.store import SqliteStore

PROD_ID = "100200300400500"                      # a made-up production number id (a config value in real life)
CLIENT = "201001112233"
OTHER = "201009998877"
SEC = Secrets("app-id", "app-secret-xyz", "verify-token-xyz", "", "")
MARKER_MAIL = {"tracking_disable": True, "mail_create_nosubscribe": True, "mail_create_nolog": True}


def cfg_dict(**o) -> dict:
    d = dict(phone_number_id=PROD_ID, staff_approver_id=90001, webhook_port=0, admin_port=0, send_enabled=True,
             go_live_ref="test go-live ref", intake_ref="test intake ref")
    d.update(o)
    return d


def make_cfg(**o):
    return config_from_dict(cfg_dict(**o))


def transport(cfg, *, mobile="+20 100 111 2233", extra=()):
    t = production.dry_transport(cfg)
    base = {"phone": False, "email": False, "user_ids": False, "parent_id": False}
    t.data["res.partner"][5001] = {"id": 5001, "name": "Real Client Co", "mobile": mobile, **base}
    for i, m in enumerate(extra):
        t.data["res.partner"][5100 + i] = {"id": 5100 + i, "name": f"Other {i}", "mobile": m, **base}
    return t


def prod_odoo(t, cfg=None):
    cfg = cfg or make_cfg()
    pc = production.people_config(cfg)
    return ProdOdoo(config=pc, open_transport=lambda _n: t, conv=GuardedOdoo(t).conv, store=SqliteStore(":memory:"),
                    team_id=cfg.team_id)


def build(cfg=None, wa=None, **kw):
    cfg = cfg or make_cfg()
    wa = wa or fakes.FakeWhatsApp()
    kw.setdefault("transport", transport(cfg))
    return production.build_hub(cfg, SEC, dry_run=True, whatsapp_inner=wa, **kw), wa


def payload(wa_id=CLIENT, mid="wamid.AAA111", phone_id=PROD_ID, text="Hi, my sales quotation report shows an error.", ts=None):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "100", "phone_number_id": phone_id},
        "contacts": [{"profile": {"name": "Mia"}, "wa_id": wa_id}],
        "messages": [{"from": wa_id, "id": mid, "timestamp": str(int(ts or time.time() + 1)), "type": "text",
                      "text": {"body": text}}]}}]}]}


def fake(hub):
    """The fake Odoo transport behind the hub (the test factory hands the same one to every secret name)."""
    return hub.odoo._open("any")


def tickets(hub):
    return [r for i, r in fake(hub).data["helpdesk.ticket"].items() if i > 20000]


# ---------------------------------------------------------------- config rails

def test_defaults_are_the_safe_ones():
    c = production.ProductionConfig()
    assert (c.webhook_port, c.admin_port, c.bind_host, c.public_host) == (8890, 8891, "127.0.0.1", "app.example.com")
    assert (c.team_id, c.automation_uid, c.send_enabled, c.go_live_ref, c.teamhub_config) == (2, 82, False, "", "")
    assert c.odoo_secret_file.endswith("odoo-riley.json") and "aa-prod" in c.state_db


def test_valid_config_has_no_problems():
    assert production.problems(make_cfg(), have_app_secret=True, have_verify_token=True) == []


@pytest.mark.parametrize("over,needle", [
    ({"phone_number_id": ""}, "phone_number_id"),
    ({"phone_number_id": "12ab"}, "phone_number_id"),
    ({"bind_host": "0.0.0.0"}, "loopback"),
    ({"client_channel": "email"}, "client_channel"),
    ({"staff_approver_id": 0}, "staff_approver_id"),
    ({"go_live_ref": "  "}, "go_live_ref"),
    ({"webhook_port": 8790, "admin_port": 8891}, "staging ports"),
    ({"webhook_port": 8890, "admin_port": 8890}, "two different"),
    ({"public_host": "staging.example.com"}, "public_host"),
    ({"state_db": str(staging.StagingConfig().state_db)}, "state_db"),
    ({"verify_token_file": staging.StagingConfig().verify_token_file}, "verify_token_file"),
    ({"users_file": staging.StagingConfig().users_file}, "users_file"),
    ({"business_dir": staging.StagingConfig().business_dir}, "business_dir"),
    ({"team_id": 0}, "team_id"),
])
def test_config_refusals(over, needle):
    why = production.problems(make_cfg(**over), have_app_secret=True, have_verify_token=True)
    assert any(needle in w for w in why), why


def test_missing_secrets_are_refused():
    why = production.problems(make_cfg(), have_app_secret=False, have_verify_token=False)
    assert any("app secret" in w for w in why) and any("verify token" in w for w in why)


@pytest.mark.parametrize("raw", [{"phone_number_id": TEST_PHONE_NUMBER_ID}, {"display_phone_number": "+1 555-010-0099"},
                                 {"phone_number_id": PROD_ID, "note": [TEST_DISPLAY]}])
def test_the_test_number_is_refused_anywhere_in_the_config(raw):
    with pytest.raises(ProductionRefused, match="TEST number"):
        config_from_dict({**raw})


def test_test_number_by_decoded_value_is_also_a_problem():
    from dataclasses import replace as rep
    c = rep(make_cfg(), phone_number_id=TEST_PHONE_NUMBER_ID)
    assert any("TEST number" in w for w in production.problems(c, have_app_secret=True, have_verify_token=True))


def test_unknown_key_is_refused_and_a_reused_teamhub_section_is_ignored():
    with pytest.raises(ProductionRefused, match="unknown"):
        config_from_dict({"phone_number_id": PROD_ID, "allowed_senders": []})
    assert config_from_dict({"phone_number_id": PROD_ID, "teamhub_tickets": {"x": 1}}).phone_number_id == PROD_ID


def test_staging_still_refuses_executives_number():
    with pytest.raises(staging.StagingRefused):
        staging.config_from_dict({"phone_number_id": "1", "allowed_senders": [EXECUTIVE_NUMBER]})
    why = staging.problems(staging.config_from_dict(dict(phone_number_id=PROD_ID, test_partner_id=1)),
                           have_app_secret=True, have_verify_token=True)
    assert any("phone_number_id" in w for w in why)


# ---------------------------------------------------------------- pipeline: real partner, one ack, guarded Odoo

def test_real_client_gets_a_real_ticket_and_exactly_one_automatic_ack():
    hub, wa = build()
    first = hub.handle_webhook(payload())
    assert first.get("ticket_id")
    (t,) = tickets(hub)
    assert t["partner_id"] == 5001 and t["team_id"] == 2 and not str(t["name"]).startswith(guard.TEST_PREFIX)
    assert "TEST" not in str(t["description"])
    assert [s["automatic"] for s in wa.sent] == [True] and wa.sent[0]["to"] == CLIENT
    again = hub.handle_webhook(payload())                          # the same wamid, redelivered
    assert again.get("duplicate") and len(tickets(hub)) == 1 and len(wa.sent) == 1


def test_create_carries_the_exact_mail_suppress_context_and_no_deletes_exist():
    hub, _ = build()
    seen = []
    orig = fake(hub).execute_kw
    fake(hub).execute_kw = lambda m, me, a, k: (seen.append((m, me, k)), orig(m, me, a, k))[1]
    hub.handle_webhook(payload())
    creates = [k for m, me, k in seen if (m, me) == ("helpdesk.ticket", "create")]
    assert creates and all(k == {"context": MARKER_MAIL} for k in creates)
    assert not [1 for m, me, _ in seen if me == "unlink"]
    with pytest.raises(guard.WriteRefused):
        guard.check("helpdesk", "helpdesk.ticket", "unlink", [[1]])
    with pytest.raises(guard.WriteRefused):
        hub.odoo.create_ticket({"name": "x", "partner_id": 5001}, context={"tracking_disable": True})


def test_team_is_pinned_even_for_an_inquiry():
    hub, _ = build(make_cfg(team_id=2))
    hub.handle_webhook(payload(text="What is the price of the inventory module? Do you offer a quote?"))
    assert all(t["team_id"] == 2 for t in tickets(hub))


def test_unknown_and_ambiguous_senders_are_held_with_no_ticket_and_no_message():
    hub, wa = build()
    r = hub.handle_webhook(payload(wa_id=OTHER, mid="wamid.B"))
    assert r.get("held") and not tickets(hub) and not wa.sent
    cfg = make_cfg()
    hub2, wa2 = build(cfg, transport=transport(cfg, extra=["0100 111 2233"]))       # two partners share the number
    r2 = hub2.handle_webhook(payload(mid="wamid.C"))
    assert r2.get("held") and not tickets(hub2) and not wa2.sent


def test_other_numbers_events_are_ignored_by_the_hub_itself():
    hub, wa = build()
    assert "ignored" in hub.handle_webhook(payload(phone_id="999000111222"))
    assert hub.accept(payload(phone_id="999000111222")) == 0 and not hub.store.scan("messages") and not wa.sent


# ---------------------------------------------------------------- sends: off by default, stop, known recipient only

def test_nothing_is_sent_while_send_enabled_is_false():
    wa = fakes.FakeWhatsApp()
    hub, _ = build(make_cfg(send_enabled=False, go_live_ref=""), wa)
    with pytest.raises(whatsapp_cloud.WhatsAppRefused, match="send_enabled"):
        hub.whatsapp.send_text(CLIENT, "hi", last_inbound_at=hub.clock())
    assert not wa.sent


def test_stop_file_and_unknown_recipient_block_every_send(tmp_path):
    cfg = make_cfg(state_db=str(tmp_path / "state" / "p.db"))
    hub, wa = build(cfg)
    now = hub.clock()
    with pytest.raises(whatsapp_cloud.WhatsAppRefused, match="known client"):
        hub.whatsapp.send_text(OTHER, "hi", last_inbound_at=now)
    stop = tmp_path / "state" / "STOP"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("x")
    with pytest.raises(whatsapp_cloud.WhatsAppRefused, match="STOP"):
        hub.whatsapp.send_text(CLIENT, "hi", last_inbound_at=now)
    assert not wa.sent
    stop.unlink()
    hub.whatsapp.send_text(CLIENT, "hi", last_inbound_at=now)
    assert len(wa.sent) == 1


def test_the_cloud_allowlist_is_opened_for_one_recipient_only_during_the_call():
    class Cloud:
        w = {"allowed_recipients": []}

        def send_text(self, to, text, *, last_inbound_at, now):
            assert self.w["allowed_recipients"] == [to]
            return {"ok": True}

    hub, _ = build(wa=Cloud())
    assert hub.whatsapp.send_text(CLIENT, "hi", last_inbound_at=hub.clock()) == {"ok": True}
    assert hub.whatsapp.inner.w["allowed_recipients"] == []


def test_cloud_sender_needs_go_live_ref_and_is_none_while_off():
    assert production.build_cloud(make_cfg(send_enabled=False, go_live_ref=""), SEC) is None
    with pytest.raises(ProductionRefused, match="go_live_ref"):
        production.check_config(make_cfg(go_live_ref=""), SEC)


# ---------------------------------------------------------------- Odoo preflight

def test_preflight_refuses_a_login_that_is_not_the_automation_uid():
    cfg = make_cfg(automation_uid=999)
    with pytest.raises(ProductionRefused, match="not the expected person"):
        build(cfg, transport=production.dry_transport(make_cfg()))


def test_preflight_refuses_an_opening_stage_with_an_email_template():
    t = transport(make_cfg())
    odoo = prod_odoo(t)                                            # discovery saw stage 14 silent ...
    t.data["helpdesk.stage"][14]["template_id"] = 178              # ... but the live read now shows a template
    with pytest.raises(ProductionRefused, match="email template"):
        odoo.preflight(14)
    assert not odoo._ready


def test_build_refuses_when_every_safe_stage_has_a_template():
    t = transport(make_cfg())
    for sid in (14, 12, 6, 18, 16):
        t.data["helpdesk.stage"][sid]["template_id"] = 178
    with pytest.raises(ProductionRefused, match="safe stage"):
        build(transport=t)


def test_preflight_refuses_a_stage_that_is_not_in_the_team():
    t = transport(make_cfg())
    t.data["helpdesk.stage"][14]["team_ids"] = [11]
    with pytest.raises(ProductionRefused, match="safe stage"):
        build(transport=t)


def test_no_ticket_before_preflight_and_never_without_a_real_partner():
    odoo = prod_odoo(transport(make_cfg()))
    with pytest.raises(guard.WriteRefused, match="preflight"):
        odoo.create_ticket({"name": "x", "partner_id": 5001, "stage_id": 14}, context=MARKER_MAIL)
    hub, _ = build()
    with pytest.raises(guard.WriteRefused, match="real partner"):
        hub.odoo.create_ticket({"name": "x", "partner_id": 0, "stage_id": 14}, context=MARKER_MAIL)
    with pytest.raises(guard.WriteRefused, match="disabled"):
        hub.odoo.send_client_email(1, "x", partner_id=1, email_from="a@b.c", aliases=[], store=None, nonce="n")


def test_preflight_command_reports_not_live_and_touches_no_port(capsys):
    lines = production.preflight(make_cfg(send_enabled=False, go_live_ref=""), dry_run=True)
    assert any("ACK OFF" in x for x in lines) and any("opening stage has no email template" in x for x in lines)
    assert any("NOT ACCEPTING" in x for x in production.preflight(make_cfg(intake_ref="", send_enabled=False, go_live_ref=""), dry_run=True))
    assert any("LIVE-READY" in x for x in production.preflight(make_cfg(), dry_run=True))


def test_the_no_test_rail_refuses_a_test_name_and_the_test_contact():
    hub, _ = build()
    t = hub.odoo
    with pytest.raises(guard.WriteRefused, match="TEST ticket"):
        t.create_ticket({"name": guard.TEST_PREFIX + "x", "partner_id": 5001, "stage_id": 14}, context=MARKER_MAIL)
    fake(hub).data["res.partner"][1187] = {"id": 1187, "name": guard.TEST_PARTNER, "mobile": False, "phone": False, "email": False}
    with pytest.raises(guard.WriteRefused, match="TEST contact"):
        t.create_ticket({"name": "real looking", "partner_id": 1187, "stage_id": 14}, context=MARKER_MAIL)
    with pytest.raises(guard.WriteRefused, match="TEST contact|unknown"):
        t.create_ticket({"name": "ghost", "partner_id": 424242, "stage_id": 14}, context=MARKER_MAIL)
    assert not tickets(hub)


def test_per_person_keys_are_used_as_built_and_a_missing_key_blocks():
    cfg = make_cfg(odoo_people={"81": "odoo-supervisor"}, odoo_tg_people={"90001": 81})
    pc = production.people_config(cfg)
    assert pc.automation_secret == "odoo-riley" and pc.automation_uid == 82
    assert pc.people == {82: "odoo-riley", 81: "odoo-supervisor"} and pc.tg_people == {90001: 81} and not pc.fallback_to_automation
    with pytest.raises(ProductionRefused, match="odoo_people"):
        config_from_dict({"phone_number_id": PROD_ID, "odoo_people": ["x"]})
    from twin_agent.hub.odoo_people import KeyMissing
    hub, _ = build(cfg)
    with pytest.raises(KeyMissing):                                 # a person with no key mapped: blocked, no fallback
        hub.odoo.post_note(9001, "x", owner_uid=77)


def test_real_cloud_class_one_call_for_a_known_client_none_for_unknown_and_its_own_stop_file(tmp_path):
    calls = []

    def http(method, url, *, headers, timeout, json=None, **kw):
        calls.append((url, json["to"]))
        return 200, {"messages": [{"id": "wamid.OUT"}]}

    cfg = make_cfg()
    stop = tmp_path / "cloud-stop"
    wa_cfg = {"run_mode": "live", "send_enabled": True, "consent": {"d1_ref": "r", "channels": {"whatsapp": True}},
              "whatsapp": {"enabled": True, "vault_name": "v", "graph_version": "v27.0", "window_hours": 24,
                           "allowed_recipients": []}}
    cloud = whatsapp_cloud.WhatsAppCloud(wa_cfg, has_secret=lambda n: True, http=http, stop_file=lambda: stop,
                                         get_fields=lambda n: {"token": "tkn", "phone_number_id": cfg.phone_number_id})
    hub, _ = build(cfg, wa=cloud)
    now = hub.clock()
    assert hub.whatsapp.send_text(CLIENT, "hi", last_inbound_at=now) == {"msg_id": "wamid.OUT"}
    assert len(calls) == 1 and calls[0][1] == CLIENT and PROD_ID in calls[0][0]
    with pytest.raises(whatsapp_cloud.WhatsAppRefused):
        hub.whatsapp.send_text(OTHER, "hi", last_inbound_at=now)
    assert len(calls) == 1 and cloud.w["allowed_recipients"] == []   # the allowlist is closed again after every call
    stop.write_text("x")
    with pytest.raises(whatsapp_cloud.WhatsAppRefused, match="STOP"):
        hub.whatsapp.send_text(CLIENT, "hi", last_inbound_at=now)
    assert len(calls) == 1


# ---------------------------------------------------------------- Teamhub: off by default, safe summaries only

def test_teamhub_is_off_by_default():
    assert production.make_teamhub_runner(make_cfg()) is None


def _teamhub_cfg(tmp_path, team=2, tp=1187, state="st", like=""):
    p = tmp_path / "config.yaml"
    p.write_text(f"""teamhub_tickets:
  room_id: r
  workflow_id: w
  teamhub_exe: k
  workdir: {tmp_path.as_posix()}
  state_dir: {tmp_path.as_posix()}/{state}
  test_partner_id: {tp}
  filter: {{team_id: {team}{like}}}
""", encoding="utf-8")
    return str(p)


def test_teamhub_forces_safe_summaries_only_and_refuses_staging_state_other_team_and_test_filter(tmp_path):
    k = lambda **o: make_cfg(state_db=str(tmp_path / "state" / "p.db"), **o)          # noqa: E731
    st = production.teamhub_settings(k(teamhub_config=_teamhub_cfg(tmp_path, state="state/teamhub"), public_admin=True))
    assert st.test_partner_id == 0 and st.seed_watermark and st.admin_link_template == "https://admin.example.com/tickets/{tid}"
    with pytest.raises(ProductionRefused, match="team 2"):
        production.teamhub_settings(k(teamhub_config=_teamhub_cfg(tmp_path, team=11, state="state/teamhub")))
    with pytest.raises(ProductionRefused, match="state_dir"):                         # not inside production's folder
        production.teamhub_settings(k(teamhub_config=_teamhub_cfg(tmp_path, state="elsewhere")))
    stg = str(staging.StagingConfig().state_db).replace("\\", "/").rsplit("/", 1)[0]
    with pytest.raises(ProductionRefused, match="state_dir"):                         # staging's folder
        production.teamhub_settings(make_cfg(state_db=str(tmp_path / "x" / "p.db"),
                                           teamhub_config=_teamhub_cfg(tmp_path, state=stg)))
    with pytest.raises(ProductionRefused, match="TEST"):
        production.teamhub_settings(k(teamhub_config=_teamhub_cfg(tmp_path, state="state/teamhub", like=", name_like: 'TEST ·%'")))


# ---------------------------------------------------------------- admin guard

def test_admin_accepts_only_a_validated_production_hub():
    hub, _ = build()
    assert production.is_production_hub(hub) and not staging.is_staging_hub(hub)
    AdminApp(hub, AdminConfig(), FakeGraphClient())
    from hub_helpers import build_hub
    plain = build_hub()
    with pytest.raises(RuntimeError):
        AdminApp(plain, AdminConfig(), FakeGraphClient())
    plain.prod, plain.have_secrets = make_cfg(), (True, True)
    plain.store.put(*production.MARKER, {"production": True})
    plain.cfg["phone_number_id"] = PROD_ID
    with pytest.raises(RuntimeError):                                # the class marker is missing
        AdminApp(plain, AdminConfig(), FakeGraphClient())
    hub.prod = replace(hub.prod, bind_host="0.0.0.0")                # re-validated at construction
    with pytest.raises(RuntimeError):
        AdminApp(hub, AdminConfig(), FakeGraphClient())


# ---------------------------------------------------------------- the real webhook server: one number, healthz

def _sign(body: bytes, secret: str) -> dict:
    return {"Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


def _post(base, body: dict, secret) -> int:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{base}/webhook", data=data, headers=_sign(data, secret))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:                       # noqa: S310 - loopback
            assert r.read() == b""
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _runtime(cfg, tmp_path):
    cfg = replace(cfg, users_file=str(tmp_path / "none" / "users.json"))
    rt, sec = production.open_runtime(cfg, dry_run=True)
    rt.start()
    return rt, sec, f"http://127.0.0.1:{rt.ports()['webhook']}"


def test_production_webhook_drops_other_numbers_and_stores_only_its_own(tmp_path, capsys, caplog):
    rt, sec, base = _runtime(make_cfg(), tmp_path)
    try:
        caplog.set_level("INFO", logger="twin_agent.hub.webhook")
        assert _post(base, payload(phone_id=TEST_PHONE_NUMBER_ID, mid="wamid.X1"), sec.app_secret) == 200
        assert _post(base, payload(phone_id="999000111222", mid="wamid.X2"), sec.app_secret) == 200
        rt.webhook.drain()
        assert rt.hub.store.scan("messages") == [] and rt.hub.store.scan("staging_ignored") == []
        assert "...1222" in caplog.text and "999000111222" not in caplog.text and TEST_PHONE_NUMBER_ID not in caplog.text
        assert rt.webhook.last_event_at is None
        mixed = payload(mid="wamid.X3")
        mixed["entry"][0]["changes"].append(payload(phone_id="999000111222", mid="wamid.X4")["entry"][0]["changes"][0])
        assert _post(base, mixed, sec.app_secret) == 200
        rt.webhook.drain()
        assert [d["inbound"]["message_id"] for d in rt.hub.store.scan("messages")] == ["wamid.X3"]
    finally:
        rt.close()
    capsys.readouterr()


def test_bad_signature_still_401_and_healthz_has_no_pii(tmp_path, capsys):
    rt, sec, base = _runtime(make_cfg(), tmp_path)
    try:
        assert _post(base, payload(), "wrong-secret") == 401
        with urllib.request.urlopen(f"{base}/healthz", timeout=10) as r:        # noqa: S310
            h = json.loads(r.read())
        assert set(h) == {"ok", "accepting", "uptime_s", "last_event_age_s", "open_holds"}
        assert h["ok"] is True and h["accepting"] is True and h["last_event_age_s"] is None and h["open_holds"] == 0
        assert _post(base, payload(mid="wamid.H1"), sec.app_secret) == 200
        with urllib.request.urlopen(f"{base}/healthz", timeout=10) as r:        # noqa: S310
            h2 = json.loads(r.read())
        assert isinstance(h2["last_event_age_s"], int) and PROD_ID not in json.dumps(h2)
    finally:
        rt.close()
    capsys.readouterr()


def test_server_without_intake_ref_answers_the_handshake_and_health_but_refuses_events_with_503(tmp_path, capsys):
    rt, sec, base = _runtime(make_cfg(send_enabled=False, go_live_ref="", intake_ref=""), tmp_path)
    try:
        with urllib.request.urlopen(f"{base}/webhook?hub.mode=subscribe&hub.verify_token={sec.verify_token}"
                                    "&hub.challenge=C1", timeout=10) as r:       # noqa: S310
            assert r.read() == b"C1"
        assert _post(base, payload(mid="wamid.N1"), sec.app_secret) == 503
        assert rt.hub.store.scan("messages") == []
        assert _post(base, payload(phone_id="999000111222", mid="wamid.N2"), sec.app_secret) == 200   # not ours: dropped
    finally:
        rt.close()
    capsys.readouterr()


def _status_payload(phone_id):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "1", "phone_number_id": phone_id},
        "statuses": [{"id": "wamid.ST", "status": "delivered", "timestamp": "1", "recipient_id": CLIENT}]}}]}]}


def test_staging_answers_503_to_any_payload_with_a_foreign_number_and_stores_nothing(tmp_path, capsys, caplog):
    """Meta never resends after a 200, and the app-default callback may carry a REAL client's event for another number."""
    cfg = staging.config_from_dict(dict(phone_number_id=TEST_PHONE_NUMBER_ID, test_partner_id=1187,
                                        allowed_senders=[CLIENT], staff_approver_id=90001, webhook_port=0,
                                        admin_port=0, launcher_port=0, users_file=str(tmp_path / "none" / "u.json")))
    rt, sec = staging.open_runtime(cfg, dry_run=True)
    rt.start()
    base = f"http://127.0.0.1:{rt.ports()['webhook']}"
    try:
        caplog.set_level("INFO", logger="twin_agent.hub.webhook")
        executive_id = "1000000000099"
        assert _post(base, payload(wa_id=OTHER, phone_id=executive_id, mid="wamid.S1"), sec.app_secret) == 503
        assert _post(base, _status_payload(executive_id), sec.app_secret) == 503                  # a foreign status callback too
        mixed = payload(mid="wamid.S2", phone_id=TEST_PHONE_NUMBER_ID)                         # the TEST number's event rides along
        mixed["entry"][0]["changes"].append(payload(wa_id=OTHER, phone_id=executive_id, mid="wamid.S3")["entry"][0]["changes"][0])
        assert _post(base, mixed, sec.app_secret) == 503
        rt.webhook.drain()
        assert rt.hub.store.scan("messages") == [] and rt.hub.store.scan("staging_ignored") == []
        assert "...0099" in caplog.text and executive_id not in caplog.text
        assert _post(base, payload(phone_id=TEST_PHONE_NUMBER_ID, mid="wamid.S4"), sec.app_secret) == 200   # only ours: stored
        rt.webhook.drain()
        assert [d["inbound"]["message_id"] for d in rt.hub.store.scan("messages")] == ["wamid.S4"]
        with urllib.request.urlopen(f"{base}/healthz", timeout=10) as r:        # noqa: S310
            assert json.loads(r.read())["ok"] is True
    finally:
        rt.close()
    capsys.readouterr()


def test_production_answers_200_and_ignores_a_foreign_number_status_too(tmp_path, capsys):
    rt, sec, base = _runtime(make_cfg(), tmp_path)
    try:
        assert _post(base, _status_payload("999000111222"), sec.app_secret) == 200
        assert _post(base, payload(wa_id=OTHER, phone_id=TEST_PHONE_NUMBER_ID, mid="wamid.P1"), sec.app_secret) == 200
        rt.webhook.drain()
        assert rt.hub.store.scan("messages") == []
    finally:
        rt.close()
    capsys.readouterr()


def test_only_the_webhook_path_takes_posts_and_handshakes(tmp_path, capsys):
    rt, sec, base = _runtime(make_cfg(), tmp_path)
    try:
        data = json.dumps(payload(mid="wamid.PATH")).encode()
        req = urllib.request.Request(f"{base}/healthz", data=data, headers=_sign(data, sec.app_secret))
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)                              # noqa: S310
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e2:
            urllib.request.urlopen(f"{base}/other", timeout=10)                  # noqa: S310
        assert e2.value.code == 404 and rt.hub.store.scan("messages") == []
    finally:
        rt.close()
    capsys.readouterr()


def test_staging_hub_accept_skips_a_change_for_another_number():
    from test_hub_staging import build as stg_build, payload as stg_payload
    hub = stg_build()
    assert hub.accept(stg_payload(phone_id="999000111222", wa_id=OTHER)) == 0
    assert hub.store.scan("staging_ignored") == []


def test_own_number_only_unit():
    keep, n, last = webhook.own_number_only(payload(phone_id="777"), PROD_ID)
    assert keep is None and n == 1 and last == "777"
    assert webhook.own_number_only(payload(), "") [0] is None           # an empty own id drops everything
    assert webhook.own_number_only({"entry": [None, {"changes": [None, {"value": 3}]}]}, PROD_ID)[0] is None
    assert webhook.own_number_only("junk", PROD_ID) == (None, 0, "")
