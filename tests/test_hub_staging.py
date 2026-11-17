"""The staging runner: rails, signed-webhook E2E, admin guard. Fakes only: no network, no real secrets, no real Odoo."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from twin_agent.channels import whatsapp_cloud
from twin_agent.helpdesk import guard
from twin_agent.hub import fakes, staging, webhook
from twin_agent.hub.admin import seed
from twin_agent.hub.admin.app import AdminApp
from twin_agent.hub.admin.config import AdminConfig, StaffUser
from twin_agent.hub.admin.graph import FakeGraphClient
from twin_agent.hub.staging import (EXECUTIVE_NUMBER, TEST_PHONE_NUMBER_ID, Secrets, StagingRefused, config_from_dict)

from admin_helpers import PW, Client, approve_form, fast_hash

ALLOWED, STRANGER = "201001112233", "201009998877"
APPROVER = 90001
SEC = Secrets("app-id", "app-secret-xyz", "verify-token-xyz", "", "")


def cfg_dict(**o) -> dict:
    d = dict(phone_number_id=TEST_PHONE_NUMBER_ID, test_partner_id=1187, allowed_senders=[ALLOWED],
             staff_approver_id=APPROVER, webhook_port=0, admin_port=0, launcher_port=0)
    d.update(o)
    return d


def make_cfg(**o):
    return config_from_dict(cfg_dict(**o))


def build(cfg=None, **kw):
    return staging.build_hub(cfg or make_cfg(), SEC, dry_run=True, whatsapp_inner=fakes.FakeWhatsApp(), **kw)


def payload(wa_id=ALLOWED, mid="wamid.AAA111", text="Hi, my sales quotation report shows an error when I print it.",
            phone_id=TEST_PHONE_NUMBER_ID, name="Mia Tester") -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550100099", "phone_number_id": phone_id},
        "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
        "messages": [{"from": wa_id, "id": mid, "timestamp": str(int(time.time())), "type": "text",
                      "text": {"body": text}}]}}]}]}


def spy(hub):
    """Record every Odoo call the transport sees (model, method, kwargs)."""
    t, seen, orig = hub.odoo.t, [], hub.odoo.t.execute_kw

    def wrapped(model, method, args, kwargs):
        seen.append((model, method, kwargs))
        return orig(model, method, args, kwargs)

    t.execute_kw = wrapped
    return seen


# ---------------------------------------------------------------- validation rails

@pytest.mark.parametrize("over,needle", [
    ({"display_phone_number": "+1 555 000 9999"}, "Executive"),
    ({"phone_number_id": "999000111222"}, "phone_number_id"),
    ({"phone_number_id": ""}, "phone_number_id"),
    ({"test_partner_id": 0}, "test_partner_id"),
    ({"bind_host": "0.0.0.0"}, "loopback"),
    ({"bind_host": "192.168.1.5"}, "loopback"),
    ({"client_channel": "email"}, "client_channel"),
    ({"staff_approver_id": 0}, "staff_approver_id"),
    ({"allowed_senders": ["12345"]}, "wa_id"),
    ({"launcher_host": "staging.example.com"}, "launcher_host"),
])
def test_config_validation_refuses(over, needle):
    try:
        cfg = make_cfg(**over)
    except StagingRefused as e:
        assert needle in str(e)
        return
    with pytest.raises(StagingRefused) as e:
        staging.check_config(cfg, SEC)
    assert needle in str(e.value)


def test_executives_number_is_refused_anywhere_in_the_config():
    for over in ({"allowed_senders": [EXECUTIVE_NUMBER]}, {"phone_number_id": EXECUTIVE_NUMBER},
                 {"display_phone_number": "+1 (555) 000-9999"}):
        with pytest.raises(StagingRefused, match="Executive"):
            make_cfg(**over)


def test_missing_app_secret_or_verify_token_is_refused():
    for sec in (Secrets("a", "", "tok"), Secrets("a", "sec", "")):
        with pytest.raises(StagingRefused):
            staging.build_hub(make_cfg(), sec, dry_run=True)


def test_unknown_key_refused_and_empty_allowlist_is_valid():
    with pytest.raises(StagingRefused, match="unknown"):
        make_cfg(history_sync=True)
    staging.check_config(make_cfg(allowed_senders=[]), SEC)


def test_check_command_generates_a_private_verify_token_and_never_prints_it(tmp_path, capsys):
    (tmp_path / "app.json").write_text(json.dumps({"app_id": "1", "app_secret": "s3cr3t-app"}))
    vt = tmp_path / "vt.txt"
    y = tmp_path / "staging.yaml"
    y.write_text(json.dumps({**cfg_dict(), "app_secret_file": str(tmp_path / "app.json"), "verify_token_file": str(vt),
                             "system_user_file": str(tmp_path / "none.json"), "webhook_port": 8790, "admin_port": 8791,
                             "launcher_port": 8792}))
    assert staging.main(["check", "--config", str(y)]) == 0
    token = vt.read_text().strip()
    assert len(token) >= 32
    out = capsys.readouterr()
    assert token not in out.out + out.err and "s3cr3t-app" not in out.out + out.err
    if os.name != "nt":
        assert oct(vt.stat().st_mode & 0o777) == "0o600"
    assert staging.ensure_verify_token(str(vt)) == token                      # an existing token is kept
    (tmp_path / "app.json").write_text("{}")
    assert staging.main(["check", "--config", str(y)]) == 2                   # no app secret -> refused


def test_send_enabled_without_the_system_user_token_is_refused(tmp_path):
    (tmp_path / "app.json").write_text(json.dumps({"app_id": "1", "app_secret": "x"}))
    cfg = make_cfg(send_enabled=True, app_secret_file=str(tmp_path / "app.json"),
                   verify_token_file=str(tmp_path / "v.txt"), system_user_file=str(tmp_path / "missing.json"))
    with pytest.raises(StagingRefused, match="system-user token"):
        staging.load_secrets(cfg, dry_run=False)


def test_real_cloud_sender_is_blocked_until_staging_yaml_enables_it():
    sec = Secrets("a", "b", "c", "wa-token")
    with pytest.raises(StagingRefused, match="blocked"):
        staging.build_cloud(make_cfg(), sec)                                  # send_enabled false: no silent fake
    cloud = staging.build_cloud(make_cfg(send_enabled=True, consent_ref="own TEST number, testers agreed"), sec)
    assert "wa-token" not in repr(cloud)


# ---------------------------------------------------------------- the pipeline rails (signed webhook E2E)

def _server(hub):
    srv = webhook.make_server(hub, verify_token=SEC.verify_token, app_secret=SEC.app_secret, tick_interval=None)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(srv, base, body: dict, secret=SEC.app_secret):
    data = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{base}/webhook", data=data,
                                 headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    with urllib.request.urlopen(req, timeout=10) as r:                         # noqa: S310 - loopback
        assert r.status == 200 and r.read() == b""
    srv.drain()


def test_signed_webhook_allowlisted_sender_makes_a_test_ticket_and_one_ack():
    hub = build()
    seen = spy(hub)
    srv, base = _server(hub)
    try:
        with urllib.request.urlopen(f"{base}/webhook?hub.mode=subscribe&hub.verify_token={SEC.verify_token}"
                                    "&hub.challenge=C123", timeout=10) as r:   # noqa: S310
            assert r.read() == b"C123"
        _post(srv, base, payload())
    finally:
        srv.shutdown()
        srv.server_close()
    t = hub.odoo.t
    creates = [w for w in t.writes if w["method"] == "create"]
    assert len(creates) == 1 and [w["model"] for w in t.writes] == ["helpdesk.ticket"]    # nothing else was written
    vals = creates[0]["vals"]
    assert vals["name"].startswith("TEST · ") and vals["team_id"] == 2 and vals["partner_id"] == 1187
    assert vals["user_id"] == t.uid and vals["stage_id"] == hub.odoo.stage_id
    assert set(vals) <= guard.TEST_TICKET_FIELDS
    ctx = [kw for m, meth, kw in seen if (m, meth) == ("helpdesk.ticket", "create")]
    assert ctx == [{"context": guard.TEST_CONTEXT}]                           # the exact mail-off context
    assert not any(meth == "unlink" for _, meth, _ in seen)
    desc = t.data["helpdesk.ticket"][creates[0]["id"]]["description"]
    assert ALLOWED not in desc and f"+{ALLOWED}" not in desc                   # never the full number, even here
    assert "Mia Tester" not in desc and "allowlisted tester (...2233)" in desc   # no profile name, last 4 digits only
    assert all("Mia" not in str(p) and ALLOWED not in str(p) for p in t.data["res.partner"].values())
    inner = hub.whatsapp.inner
    assert [(s["to"], s["automatic"]) for s in inner.sent] == [(ALLOWED, True)]   # the single instant ack
    st = hub.latest_ticket(ALLOWED)
    assert st.partner_id == 1187 and st.stage == "awaiting_owner"
    hub.store.close()


def test_non_allowlisted_sender_is_ignored_no_ticket_no_ack():
    hub = build()
    srv, base = _server(hub)
    try:
        _post(srv, base, payload(wa_id=STRANGER, mid="wamid.STRANGER1"))
        res = hub.handle_webhook(payload(wa_id=STRANGER, mid="wamid.STRANGER1"))      # also the retry/worker path
    finally:
        srv.shutdown()
        srv.server_close()
    assert res == {"ignored": staging.IGNORED}
    assert hub.odoo.t.writes == [] and hub.whatsapp.inner.sent == []
    assert hub.all_tickets() == [] and hub.store.scan("messages") == []           # never even queued
    docs = hub.store.scan("staging_ignored")
    assert len(docs) == 1 and docs[0]["reason"] == "ignored (not on the staging allowlist)"
    assert docs[0]["sender"] == "...8877" and STRANGER not in json.dumps(docs)
    hub.store.close()


def test_empty_allowlist_processes_and_sends_nothing():
    hub = build(make_cfg(allowed_senders=[]))
    hub.accept(payload())
    assert hub.handle_webhook(payload()) == {"ignored": staging.IGNORED}
    assert hub.odoo.t.writes == [] and hub.whatsapp.inner.sent == [] and hub.store.scan("messages") == []
    with pytest.raises(whatsapp_cloud.WhatsAppRefused):
        hub.whatsapp.send_text(ALLOWED, "x", last_inbound_at=hub.clock(), now=hub.clock())
    hub.store.close()


def test_every_outbound_send_is_limited_to_the_allowlist():
    hub = build()
    now = hub.clock()
    with pytest.raises(whatsapp_cloud.WhatsAppRefused, match="allowlist"):
        hub.whatsapp.send_text(STRANGER, "hi", last_inbound_at=now, now=now)
    assert hub.whatsapp.inner.sent == []
    hub.store.close()


def test_odoo_gateway_refuses_everything_outside_the_test_ticket():
    hub = build()
    odoo = hub.odoo
    with pytest.raises(guard.WriteRefused):
        odoo.create_ticket({"name": "x"}, context={"tracking_disable": True})            # not the exact context
    with pytest.raises(guard.WriteRefused):
        odoo.assign(1, odoo.uid + 1)                                                     # someone else's ownership
    with pytest.raises(guard.WriteRefused):
        odoo.send_client_email(1, "hi")
    odoo.post_note(1, "x")
    assert odoo.t.writes == []
    for mutate in (lambda p: p.update(email="a@b.example.org"), lambda p: p.update(name="A Real Client")):
        t = staging.fake_transport(make_cfg())                                           # the pinned id is a real client
        mutate(t.data["res.partner"][1187])
        with pytest.raises(StagingRefused, match="Odoo preflight"):
            staging.build_hub(make_cfg(), SEC, dry_run=True, transport=t, whatsapp_inner=fakes.FakeWhatsApp())
    hub.store.close()


def test_voice_notes_are_blocked_not_faked():
    assert staging.BlockedSTT().transcribe("any")["status"] == "blocked"


# ---------------------------------------------------------------- admin page: guard + approved reply from the page

def _admin(hub):
    users = {"approver": StaffUser("approver", "Approver", APPROVER, fast_hash(PW))}
    return AdminApp(hub, AdminConfig(users=users, cookie_secure=False), FakeGraphClient())


def test_admin_accepts_a_validated_staging_hub_and_sends_an_approved_reply_from_the_page():
    hub = build()
    app = _admin(hub)
    hub.handle_webhook(payload())
    st = hub.latest_ticket(ALLOWED)
    c = Client(app)
    assert c.login("approver", PW).status == 303
    r = c.post(f"/tickets/{st.tid}/approve", approve_form(c, hub, st.tid))
    assert r.status == 303
    sent = hub.whatsapp.inner.sent
    assert [s["automatic"] for s in sent] == [True, False] and sent[1]["to"] == ALLOWED   # ack, then the approved reply
    hub.store.close()


def test_admin_refuses_unmarked_real_and_invalid_staging_hubs():
    from hub_helpers import build_hub
    plain = build_hub()                                                                  # an unmarked (real-shaped) hub
    with pytest.raises(RuntimeError, match="demo hub or a validated staging hub"):
        AdminApp(plain, AdminConfig(), FakeGraphClient())
    plain.staging, plain.have_secrets = make_cfg(), (True, True)                         # faking the pieces is not enough:
    plain.store.put(*staging.MARKER, {"staging": True})                                  # the class marker is missing
    plain.cfg["phone_number_id"] = TEST_PHONE_NUMBER_ID
    with pytest.raises(RuntimeError):
        AdminApp(plain, AdminConfig(), FakeGraphClient())
    bad = build()
    bad.staging = replace(bad.staging, phone_number_id="1555000999900")                  # config no longer valid
    with pytest.raises(RuntimeError):
        AdminApp(bad, AdminConfig(), FakeGraphClient())
    weak = build()
    weak.staging = replace(weak.staging, bind_host="0.0.0.0")                            # re-validated at construction
    with pytest.raises(RuntimeError):
        AdminApp(weak, AdminConfig(), FakeGraphClient())
    from twin_agent.hub.odoo_gateway import GuardedOdoo
    swapped = build()
    swapped.whatsapp = fakes.FakeWhatsApp()                                              # not the allowlisting sender
    with pytest.raises(RuntimeError):
        AdminApp(swapped, AdminConfig(), FakeGraphClient())
    wide = build()
    wide.whatsapp.allowed.add(STRANGER)                                                  # allowlist differs from config
    with pytest.raises(RuntimeError):
        AdminApp(wide, AdminConfig(), FakeGraphClient())
    raw = build()
    raw.odoo = GuardedOdoo(raw.odoo.t, writes_on=True)                                   # not the staging gateway
    with pytest.raises(RuntimeError):
        AdminApp(raw, AdminConfig(), FakeGraphClient())
    demo = seed.build_demo_hub(":memory:")
    seed.seed(demo)
    AdminApp(demo, AdminConfig(), FakeGraphClient())                                     # the demo hub still works
    for h in (plain, bad, weak, swapped, wide, raw, demo):
        h.store.close()


# ---------------------------------------------------------------- redaction, source rails, the whole runtime

def test_logs_are_redacted_tokens_and_phone_numbers():
    line = staging.redact_line("sent to 201001112233 with Bearer abcdefghijklmnopqrstuv1234 and app-secret-xyz",
                               ["app-secret-xyz"])
    assert "201001112233" not in line and "2233" in line
    assert "abcdefghijklmnopqrstuv1234" not in line and "app-secret-xyz" not in line


def test_log_filter_applies_to_records(caplog):
    f = staging.RedactFilter(["s3cret-value-1"])
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "to %s key %s", ("201001112233", "s3cret-value-1"), None)
    assert f.filter(rec)
    assert "201001112233" not in rec.getMessage() and "s3cret-value-1" not in rec.getMessage()


def test_source_has_no_press_processor_no_meta_writes_no_delete():
    src = Path(staging.__file__).read_text(encoding="utf-8")
    for needle in ("process_submissions(", "subscribed_apps", "unlink(", "allow_meta_writes=True", "overrideCallback",
                   "history_sync=True"):
        assert needle not in src


def test_teamhub_pass_runs_on_its_own_thread_and_never_touches_the_tick():
    calls = []
    hub = build(teamhub=lambda: calls.append(1) or {"opened": 1})
    assert "teamhub" not in hub.tick() and calls == []                          # the tick does not run the watcher
    assert hub.teamhub_once() == {"opened": 1} and calls == [1]
    hub.store.close()
    off = build()
    assert off.teamhub_once() == "off"
    off.store.close()


def test_a_slow_or_failing_teamhub_pass_is_cut_off_and_never_raises():
    gate = threading.Event()
    hub = build(replace(make_cfg(), teamhub_timeout_s=1), teamhub=lambda: gate.wait(5) or {"late": 1})
    t0 = time.monotonic()
    assert hub.teamhub_once() == "timeout" and time.monotonic() - t0 < 4
    gate.set()
    boom = build(teamhub=lambda: 1 / 0)
    assert boom.teamhub_once() == {"error": "ZeroDivisionError"}
    hub.store.close()
    boom.store.close()


def test_a_staging_ticket_gets_a_teamhub_thread_no_pii_refusal(tmp_path):
    from twin_agent.hub import odoo_ticket_watch as w
    from teamhub_helpers import make_ctx, ticket
    hub = build()
    hub.handle_webhook(payload())
    row = next(r for r in hub.odoo.t.data["helpdesk.ticket"].values() if str(r["name"]).startswith("TEST · "))
    tk = ticket(5, name=row["name"])
    tk["description"] = row["description"]                                      # the real staging description
    ctx, agent, odoo, clock = make_ctx(tmp_path, [tk])
    summary = w.watch_once(ctx)
    assert summary["opened"] == [5] and summary["refused"] == [] and ctx.threads.get(5)
    hub.store.close()


def test_blank_consent_ref_is_refused_when_sending_is_on():
    for blank in ("", "   "):
        with pytest.raises(StagingRefused, match="consent_ref"):
            staging.check_config(make_cfg(send_enabled=True, consent_ref=blank), SEC)
    staging.check_config(make_cfg(send_enabled=True, consent_ref="own TEST number"), SEC)


def test_an_opening_stage_with_an_email_template_refuses_to_start():
    t = staging.fake_transport(make_cfg())
    for row in t.data["helpdesk.stage"].values():
        row["template_id"] = 99                                                 # every team-2 stage would email
    with pytest.raises(StagingRefused, match="email template"):
        staging.build_hub(make_cfg(), SEC, dry_run=True, transport=t, whatsapp_inner=fakes.FakeWhatsApp())


def test_a_bind_failure_closes_what_was_already_bound(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(staging, "port_in_use", lambda host, port: False)     # skip the probe: test the bind itself
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        cfg = make_cfg(admin_port=port, webhook_port=0, launcher_port=0, users_file=str(tmp_path / "u" / "users.json"))
        with pytest.raises(StagingRefused, match="could not bind"):
            staging.open_runtime(cfg, dry_run=True)
    finally:
        blocker.close()


def test_the_odoo_key_is_redacted_from_logs(tmp_path):
    (tmp_path / "odoo.json").write_text(json.dumps({"url": "u", "db": "d", "login": "l", "api_key": "odoo-key-1234567890"}))
    cfg = make_cfg(odoo_secret_file=str(tmp_path / "odoo.json"), app_secret_file=str(tmp_path / "a.json"),
                   verify_token_file=str(tmp_path / "v.txt"))
    (tmp_path / "a.json").write_text(json.dumps({"app_id": "1", "app_secret": "x"}))
    sec = staging.load_secrets(cfg, dry_run=False)
    assert "odoo-key-1234567890" in sec.values() and "odoo-key-1234567890" not in repr(sec)


def test_teamhub_runner_refuses_a_filter_that_is_not_test_only(tmp_path, monkeypatch):
    from twin_agent.hub import teamhub_threads as kt

    class St:
        filter = {"team_id": 2, "name_like": "%"}
        test_partner_id = 0
        state_dir = str(tmp_path)

    monkeypatch.setattr(kt, "load_settings", lambda p: St)
    with pytest.raises(StagingRefused, match="TEST tickets only"):
        staging.make_teamhub_runner(make_cfg(teamhub_config="x.yaml"))
    assert staging.make_teamhub_runner(make_cfg()) is None


def test_dry_run_runtime_serves_the_webhook_on_loopback_and_stops_cleanly(tmp_path, capsys):
    cfg = make_cfg(users_file=str(tmp_path / "none" / "users.json"))
    rt, sec = staging.open_runtime(cfg, dry_run=True)
    rt.start()
    try:
        ports = rt.ports()
        assert set(ports) == {"webhook", "admin"} and rt.webhook.server_address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{ports['webhook']}/webhook"
        with urllib.request.urlopen(f"{base}?hub.mode=subscribe&hub.verify_token={sec.verify_token}&hub.challenge=ok",
                                    timeout=10) as r:                              # noqa: S310
            assert r.read() == b"ok"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=ok", timeout=10)  # noqa: S310
        assert e.value.code == 403
        assert staging.is_staging_hub(rt.hub)
        text = staging.banner(cfg, ports, dry_run=True, teamhub=False)
        assert ALLOWED not in text and "...2233" in text
    finally:
        rt.close()
    capsys.readouterr()


# ---------------------------------------------------------------- security round: public admin, token store, state paths

def test_public_admin_is_off_by_default_and_needs_the_access_attestation():
    assert make_cfg().public_admin is False
    staging.check_config(make_cfg(), SEC)
    with pytest.raises(StagingRefused, match="cloudflare_access"):
        staging.check_config(make_cfg(public_admin=True), SEC)
    staging.check_config(make_cfg(public_admin=True, cloudflare_access=True), SEC)


def test_admin_hosts_and_proxy_follow_public_admin(tmp_path):
    for public in (False, True):
        cfg = make_cfg(public_admin=public, cloudflare_access=public)
        hub = build(cfg)
        app, _ = staging.build_admin(cfg, SEC, hub, dry_run=True, users_file=_users(tmp_path, cfg))
        hosts = set(app.cfg.allowed_hosts or ())
        assert ("staging.example.com" in hosts) is public
        assert ("127.0.0.1:0" in hosts) or any(h.startswith("127.0.0.1") for h in hosts)
        assert bool(app.cfg.trusted_proxies) is public                        # dry run has no launcher
        hub.store.close()


def _users(tmp_path, cfg) -> str:
    from twin_agent.hub.staging_users import write_staff_users
    path = str(tmp_path / f"u{cfg.public_admin}.json")
    write_staff_users(path, "approver", cfg.staff_approver_id)
    return path


def test_staging_never_shares_the_default_admin_token_store(tmp_path):
    from twin_agent.hub.admin.config import DEFAULT_BUSINESS_DIR
    cfg = make_cfg()
    assert os.path.normcase(cfg.business_dir) != os.path.normcase(str(DEFAULT_BUSINESS_DIR))
    with pytest.raises(StagingRefused, match="token store"):
        staging.check_config(make_cfg(business_dir=str(DEFAULT_BUSINESS_DIR)), SEC)
    hub = build(cfg)
    app, _ = staging.build_admin(cfg, SEC, hub, dry_run=True, users_file=_users(tmp_path, cfg))
    assert os.path.normcase(str(app.businesses.dir)) not in (os.path.normcase(cfg.business_dir),
                                                             os.path.normcase(str(DEFAULT_BUSINESS_DIR)))
    hub.store.close()


def test_default_state_lives_under_the_user_profile_not_the_worktree():
    home = os.path.normcase(str(Path.home() / ".claude"))
    assert os.path.normcase(make_cfg().state_db).startswith(home)


def test_the_password_file_is_created_exclusively_and_replaces_a_stale_one(tmp_path, monkeypatch):
    from twin_agent.hub import staging_users as su
    monkeypatch.setattr(su.sys.stdout, "isatty", lambda: False, raising=False)
    users = str(tmp_path / "users.json")
    pf = tmp_path / "users.password.txt"
    pf.write_text("stale")
    msg = su.deliver("approver", "pw-123", users)
    assert pf.read_text().strip() == "pw-123" and "delete it" in msg and "pw-123" not in msg


# ---------------------------------------------------------------- the lazy-login bug (uid unknown before the first call)

def test_the_fake_transport_is_not_logged_in_until_preflight_authenticates():
    t = staging.fake_transport(make_cfg())
    assert t.uid == 0                                                          # like the real transport before login
    hub = build(transport=t)
    assert hub.odoo.uid == t.uid == 777
    hub.store.close()


def test_a_failed_odoo_login_is_a_plain_refusal_not_a_silent_zero():
    t = staging.fake_transport(make_cfg())

    def boom():
        raise RuntimeError("odoo common.authenticate: AccessDenied")

    t.authenticate = boom
    with pytest.raises(StagingRefused, match="Odoo login failed"):
        staging.build_hub(make_cfg(), SEC, dry_run=True, transport=t, whatsapp_inner=fakes.FakeWhatsApp())


def test_json_rpc_transport_authenticate_logs_in_once_and_returns_the_uid():
    from twin_agent.helpdesk.odoo import JsonRpcTransport
    calls = []
    t = JsonRpcTransport("https://x.test", "db", "login", "key")
    t._rpc = lambda service, method, args: calls.append((service, method)) or (82 if method == "authenticate" else [])
    assert t.uid == 0 and t.authenticate() == 82 and t.uid == 82
    t.execute_kw("res.partner", "search", [[]], {})
    assert calls == [("common", "authenticate"), ("object", "execute_kw")]      # no second login


def test_teamhub_test_ticket_create_logs_in_before_reading_its_uid():
    from test_hub_teamhub_test_ticket import FakeT
    from twin_agent.hub import teamhub_test_ticket as kt

    class LazyT(FakeT):
        def __init__(self):
            super().__init__(assign="auto")
            self.uid = 0

        def authenticate(self):
            self.uid = 82
            return 82

    t = LazyT()
    kt.create(t, live=True, out=lambda *_: None)
    assert t.writes[-1][2][0]["user_id"] == 82


def test_access_gate_blocks_tunnel_requests_without_the_access_header():
    seen = []

    def app(environ, start_response):
        seen.append(1)
        start_response("200 OK", [])
        return [b"ok"]

    gate = staging.AccessGate(app)

    def call(env):
        out = {}
        body = b"".join(gate(env, lambda status, headers: out.update(status=status)))
        return out["status"], body

    assert call({"HTTP_CF_CONNECTING_IP": "1.2.3.4"})[0].startswith("403") and seen == []
    assert call({"HTTP_CF_CONNECTING_IP": "1.2.3.4", "HTTP_CF_ACCESS_JWT_ASSERTION": "jwt"})[0].startswith("200")
    assert call({})[0].startswith("200")                                          # a local request has no tunnel header


def test_public_admin_wraps_the_admin_app_in_the_access_gate(tmp_path):
    cfg = make_cfg(public_admin=True, cloudflare_access=True)
    hub = build(cfg)
    app, _ = staging.build_admin(cfg, SEC, hub, dry_run=True, users_file=_users(tmp_path, cfg))
    assert isinstance(app, staging.AccessGate) and app.cfg is not None
    hub.store.close()


def test_serve_refuses_when_a_port_is_already_in_use(tmp_path):
    import socket
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        cfg = make_cfg(admin_port=blocker.getsockname()[1], webhook_port=0, launcher_port=0,
                       users_file=str(tmp_path / "u" / "users.json"))
        with pytest.raises(StagingRefused, match="port already in use"):
            staging.open_runtime(cfg, dry_run=True)
    finally:
        blocker.close()
    from twin_agent.hub.staging_support import port_in_use
    assert port_in_use("127.0.0.1", 0) is False
