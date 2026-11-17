"""PR #77 fix round: intake before sends, visible holds, voice notes, the watchdog, the Cloudflare Access gate, Teamhub links."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from wsgiref.util import setup_testing_defaults

from twin_agent.hub import fakes, teamhub_threads as kt, odoo_ticket_watch as otw, production, prod_support, prod_watchdog
from twin_agent.hub.admin import data, views
from twin_agent.hub.production import ACCESS_EMAILS, ProductionRefused, config_from_dict
from twin_agent.hub.prod_support import AccessDenied, AccessJwtGate, AccessVerifier
from twin_agent.hub.store import MemoryStore, SqliteStore
from twin_agent.hub.staging_users import write_staff_users

from test_hub_production import (CLIENT, OTHER, PROD_ID, SEC, build, fake, make_cfg, payload, tickets, transport)

PRIVATE = "my private invoice words"
ACCESS_USERS = {e: e.split("@")[0].replace(".", "-") for e in ACCESS_EMAILS}      # one email, one hub user


def posted(hub) -> list[str]:
    assert hub.hold_poster.flush()
    return [o["text"] for o in hub.hold_poster._kq.ops]


# ---------------------------------------------------------------- intake before sends

def test_intake_with_sends_off_stores_creates_the_ticket_and_settles_the_ack_once_without_a_hold():
    hub, wa = build(make_cfg(send_enabled=False, go_live_ref=""))
    r = hub.handle_webhook(payload())
    assert r["ticket_id"] and len(tickets(hub)) == 1 and wa.sent == []
    assert "7-ACK(skipped)" in hub.ticket(r["ticket_id"]).history
    assert hub.holds() == [] and hub.store.get("messages", "wamid.AAA111")["status"] == "done"
    notes = [w for w in fake(hub).writes if "Ack not sent" in json.dumps(w, default=str)]
    assert len(notes) == 1                                           # recorded ONCE
    hub.tick(now=hub.clock() + timedelta(minutes=10))
    assert wa.sent == [] and hub.holds() == [] and posted(hub) == []


def test_turning_sends_on_later_sends_zero_late_acks(tmp_path):
    db = str(tmp_path / "s.db")
    cfg_off = make_cfg(send_enabled=False, go_live_ref="")
    a, wa_a = build(cfg_off, store=SqliteStore(db))
    a.handle_webhook(payload(mid="wamid.DONE"))                      # processed while off: ticket, ack settled as skipped
    a.accept(payload(mid="wamid.QUEUED", wa_id=OTHER))               # stored, not processed yet
    a.store.close()
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    cfg_on = make_cfg()
    t = transport(cfg_on, extra=["0100 999 8877", "0111 222 3344"])
    b, wa_b = build(cfg_on, store=SqliteStore(db), transport=t, clock=lambda: later)
    b.tick(now=later + timedelta(minutes=5))                         # retries the queued one and runs the fallback pass
    b.tick(now=later + timedelta(minutes=10))
    assert wa_b.sent == []                                           # ZERO late acks
    third = "201112223344"
    fresh = payload(mid="wamid.NEW", wa_id=third, ts=later.timestamp() + 60)
    b.handle_webhook(fresh)                                          # a message that arrives AFTER the switch is acked
    assert [s["to"] for s in wa_b.sent] == [third] and wa_b.sent[0]["automatic"] is True


def test_intake_ref_is_required_and_blank_means_not_accepting():
    assert not make_cfg(intake_ref="").accepting_events and make_cfg().accepting_events
    why = production.problems(make_cfg(intake_ref=""), have_app_secret=True, have_verify_token=True)
    assert any("intake_ref" in w for w in why)                      # send_enabled needs intake first
    assert production.problems(make_cfg(intake_ref="", send_enabled=False, go_live_ref=""), have_app_secret=True,
                               have_verify_token=True) == []


# ---------------------------------------------------------------- holds are never invisible

def _assert_safe(lines: list[str]) -> None:
    for t in lines:
        assert kt.refusal_reason({"op": "create_thread", "text": t}) is None, t
        assert PRIVATE not in t and CLIENT not in t and "Hi" != t[:2]


def test_voice_note_makes_a_ticket_a_hold_and_a_safe_teamhub_line():
    hub, wa = build(make_cfg(public_admin=True, access_users=ACCESS_USERS))
    p = payload(mid="wamid.V1")
    m = p["entry"][0]["changes"][0]["value"]["messages"][0]
    m.update(type="audio", audio={"id": "A1"})
    m.pop("text")
    r = hub.handle_webhook(p)
    (t,) = tickets(hub)
    assert r["ticket_id"] and production.VOICE_PLACEHOLDER in str(t["description"])
    (h,) = hub.holds()
    assert h["reason"] == "voice_note_pending" and h["ticket_id"] == r["ticket_id"]
    lines = posted(hub)
    assert any("voice note, transcription pending" in x and f"#{r['ticket_id']}" in x for x in lines)
    assert not any("2233" in x for x in lines)                      # the Teamhub alert shows no digits of the number
    assert any(f"https://admin.example.com/tickets/{r['ticket_id']}" in x for x in lines)
    _assert_safe(lines)


def test_unknown_number_two_partners_and_a_bad_mobile_each_post_a_safe_line():
    hub, wa = build(make_cfg(), transport=transport(make_cfg(), extra=["0100 111 2233"]))     # two partners share CLIENT's number
    hub.handle_webhook(payload(mid="wamid.T1", text=PRIVATE))
    hub.handle_webhook(payload(mid="wamid.T2", wa_id=OTHER, text=PRIVATE))                     # nobody has this number
    cfg = make_cfg()
    bad, _ = build(cfg, transport=transport(cfg, mobile="0100 11"))                            # a mobile too short to match
    bad.handle_webhook(payload(mid="wamid.T3", text=PRIVATE))
    assert wa.sent == [] and len(hub.holds()) == 2 and len(bad.holds()) == 1
    lines = posted(hub) + posted(bad)
    assert len(lines) == 3 and all("not a known client" in x for x in lines)
    assert not any(d in x for x in lines for d in ("2233", "8877", "0100"))              # no digits in Teamhub
    assert sorted(r["sender"][-4:] for r in data.hold_rows(hub)) == ["2233", "8877"]      # the admin page keeps the last 4
    _assert_safe(lines)


def test_a_failed_ack_is_a_hold_with_a_safe_line():
    class Boom(fakes.FakeWhatsApp):
        def send_text(self, *a, **k):
            raise RuntimeError("network down")

    hub, _ = build(wa=Boom())
    r = hub.handle_webhook(payload(text=PRIVATE))
    assert r.get("error") and [h["reason"] for h in hub.holds()] == ["processing_failed"]
    lines = posted(hub)
    assert any("processing failed" in x for x in lines)
    _assert_safe(lines)


def test_open_holds_in_health_and_the_admin_holds_list_have_no_client_text():
    hub, _ = build()
    hub.handle_webhook(payload(wa_id=OTHER, mid="wamid.H1", text=PRIVATE))
    srv = SimpleNamespace(hub=hub)
    from twin_agent.hub.webhook import HubServer
    h = HubServer.health(SimpleNamespace(_stop=SimpleNamespace(is_set=lambda: False), accepting=True, _ticker=None,
                                         started_at=time.monotonic(), last_event_at=None, hub=hub))
    assert h["open_holds"] == 1 and h["ok"] is True
    rows = data.hold_rows(hub)
    page = views.holds_page(SimpleNamespace(banner="", users={}), SimpleNamespace(display_name="x"), "c", rows)
    assert "8877" in page and "known client" in page and PRIVATE not in page and OTHER not in page
    assert srv is not None


def test_the_hub_never_uses_a_fake_telegram_for_alerts():
    hub, _ = build()
    assert hub.team_bus.name == "teamhub" and hub.team_bus.carries_client_text is False
    hub.telegram.post_triage("full text with PRIVATE")
    assert hub.telegram.triage == []                                  # the approval transport swallows triage text


# ---------------------------------------------------------------- partner matching

@pytest.mark.parametrize("mobile,found", [("+20 100 111 22 33", True), ("0100-111-2233", True), ("+44 100 111 2233", False),
                                          ("0044 100 111 2233", False), ("0020 100 111 2233", True)])
def test_partner_phone_matching_is_tolerant_of_spaces_and_strict_about_country(mobile, found):
    cfg = make_cfg()
    hub, _ = build(cfg, transport=transport(cfg, mobile=mobile))
    assert (hub.k_odoo.partner_for(CLIENT) == 5001) is found


def test_a_company_and_its_contact_count_as_one_staff_are_not_clients_and_the_cap_holds():
    cfg = make_cfg()
    t = transport(cfg)
    t.data["res.partner"][5002] = {"id": 5002, "name": "Contact", "mobile": "0100 111 2233", "phone": False, "email": False,
                                   "user_ids": False, "parent_id": [5001, "Real Client Co"]}
    hub, _ = build(cfg, transport=t)
    assert hub.k_odoo.partner_for(CLIENT) == 5002                      # the contact, not the company
    t.data["res.partner"][5002]["user_ids"] = [7]                      # now a staff member: not a client
    assert hub.k_odoo.partner_for(CLIENT) == 5001
    t.data["res.partner"][5002]["user_ids"] = False
    t.data["res.partner"][5003] = {"id": 5003, "name": "Other", "mobile": "0100 111 2233", "phone": False, "email": False,
                                   "user_ids": False, "parent_id": False}
    assert hub.k_odoo.partner_for(CLIENT) is None                      # two different clients: held
    hub.k_odoo.CAP = 2
    assert hub.k_odoo.partner_for(CLIENT) is None                      # the cap was hit: never guess


# ---------------------------------------------------------------- config

def test_new_config_rails():
    why = lambda **o: production.problems(make_cfg(**o), have_app_secret=True, have_verify_token=True)   # noqa: E731
    assert any("expected_phone_number_id" in w for w in why(expected_phone_number_id="111111111111"))
    assert why(expected_phone_number_id=PROD_ID) == []
    assert any("admin_public_host" in w for w in why(public_admin=True, admin_public_host="app.example.com"))
    assert any("exact" in w for w in why(public_admin=True, access_emails=("@globex.example.net",)))
    assert any("access_users" in w for w in why(public_admin=True))
    assert any("access_users" in w for w in why(public_admin=True, access_users={**ACCESS_USERS, "info@example.com": "sysadmin"}))
    assert why(public_admin=True, access_users=ACCESS_USERS) == []
    assert "principal.doe@globex.example.net" in ACCESS_EMAILS and all("@" in e and not e.startswith("@") for e in ACCESS_EMAILS)
    assert make_cfg(access_emails=["A@b.example.org"]).access_emails == ("a@b.example.org",)


# ---------------------------------------------------------------- Cloudflare Access

TEAM, AUD, ISS = "acme.cloudflareaccess.com", "aud-tag-123", "https://acme.cloudflareaccess.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(email="sysadmin@example.com", *, key=KEY, aud=AUD, iss=ISS, exp=300, nbf=None, alg="RS256"):
    now = int(time.time())
    claims = {"aud": [aud], "iss": iss, "email": email, "iat": now, "exp": now + exp}
    if nbf is not None:
        claims["nbf"] = now + nbf
    return jwt.encode(claims, key, algorithm=alg)


def verifier(**kw) -> AccessVerifier:
    return AccessVerifier(TEAM, AUD, ACCESS_EMAILS, key_resolver=lambda t: KEY.public_key(), **kw)


def test_access_good_token_and_case_insensitive_exact_email():
    assert verifier().verify(token()) == "sysadmin@example.com"
    assert verifier().verify(token("Principal.doe@Globex.example.net")) == "principal.doe@globex.example.net"


@pytest.mark.parametrize("bad", [
    lambda: token(aud="other-aud"), lambda: token(exp=-3600), lambda: token(nbf=3600), lambda: token(iss="https://evil.cloudflareaccess.com"),
    lambda: token("someone@globex.example.net"), lambda: token("x@example.com"), lambda: token("sysadmin@example.com.evil.example.net"),
    lambda: token(key=OTHER_KEY), lambda: "", lambda: "not-a-jwt"])
def test_access_refuses_bad_tokens_and_people(bad):
    with pytest.raises(AccessDenied):
        verifier().verify(bad())


def test_access_refuses_an_hs256_token_even_when_the_claims_are_perfect():
    forged = jwt.encode({"aud": [AUD], "iss": ISS, "email": "sysadmin@example.com", "exp": int(time.time()) + 300},
                        "secret" * 8, algorithm="HS256")
    with pytest.raises(AccessDenied):                                  # only RS256 is ever accepted
        verifier().verify(forged)


def _call(app, *, host="admin.example.com", headers=None, path="/login"):
    env = {}
    setup_testing_defaults(env)
    env.update({"HTTP_HOST": host, "PATH_INFO": path, "REMOTE_ADDR": "127.0.0.1", "wsgi.url_scheme": "http"})
    env.update(headers or {})
    out = {}
    body = b"".join(app(env, lambda status, hdrs, *a: out.update(status=status)))
    return int(out["status"].split()[0]), body, env


def test_gate_403_without_a_header_a_forged_header_or_a_wrong_person_and_local_passes():
    seen = []

    def app(environ, start_response):
        seen.append(environ.get("aa.access_email"))
        start_response("200 OK", [])
        return [b"ok"]

    gate = AccessJwtGate(app, verifier(), "admin.example.com")
    tunnel = {"HTTP_CF_CONNECTING_IP": "203.0.113.9"}
    assert _call(gate, headers=tunnel)[0] == 403                                                    # missing header
    assert _call(gate, headers={**tunnel, "HTTP_CF_ACCESS_JWT_ASSERTION": "x.y.z"})[0] == 403        # junk
    assert _call(gate, headers={**tunnel, "HTTP_CF_ACCESS_JWT_ASSERTION": token(key=OTHER_KEY)})[0] == 403   # forged
    assert _call(gate, headers={**tunnel, "HTTP_CF_ACCESS_JWT_ASSERTION": token("someone@globex.example.net")})[0] == 403
    assert _call(gate, host="admin.example.com", headers={"HTTP_CF_ACCESS_JWT_ASSERTION": ""})[0] == 403   # public Host, no tunnel header
    assert seen == []
    code, _, env = _call(gate, headers={**tunnel, "HTTP_CF_ACCESS_JWT_ASSERTION": token("supervisor@example.com")})
    assert code == 200 and seen == ["supervisor@example.com"]
    assert _call(gate, host="127.0.0.1:8891", headers={"aa.access_email": "forged@example.com"})[0] == 200
    assert seen[-1] is None                                                                           # a client-set value is dropped


def _users_file(tmp_path, names) -> str:
    from admin_helpers import fast_hash
    p = tmp_path / "users.json"
    p.write_text(json.dumps({"users": [{"username": n, "display_name": n, "approver_id": 90001, "password_hash": fast_hash("pw")}
                                       for n in names]}), encoding="utf-8")
    return str(p)


def test_public_admin_adds_the_host_only_in_that_mode_and_records_the_verified_email(tmp_path):
    users = _users_file(tmp_path, ACCESS_USERS.values())
    off_cfg, on_cfg = make_cfg(), make_cfg(public_admin=True, access_users=ACCESS_USERS)
    hub, _ = build(off_cfg)
    off = production.build_admin(off_cfg, SEC, hub, dry_run=True, users_file=users)
    assert _call(off, host="admin.example.com", headers={"HTTP_CF_CONNECTING_IP": "203.0.113.9"})[0] == 400   # Bad host
    hub2, _ = build(on_cfg)
    on = production.build_admin(on_cfg, SEC, hub2, dry_run=True, users_file=users, verifier=verifier())
    tunnel = {"HTTP_CF_CONNECTING_IP": "203.0.113.9"}
    assert _call(on, headers=tunnel)[0] == 403
    code, _, _ = _call(on, headers={**tunnel, "HTTP_CF_ACCESS_JWT_ASSERTION": token()})
    assert code in (200, 302)                                          # past Access: the hub's own login page
    assert _call(on, host="evil.example", headers={**tunnel, "HTTP_CF_ACCESS_JWT_ASSERTION": token()})[0] == 400
    with pytest.raises(ProductionRefused, match="public_admin"):
        production.build_admin(make_cfg(public_admin=True, access_users=ACCESS_USERS, access_file=str(tmp_path / "none.json")),
                               SEC, hub2, dry_run=True, users_file=users)
    with pytest.raises(ProductionRefused, match="do not exist"):                  # a mapped hub user that has no account
        production.build_admin(on_cfg, SEC, hub2, dry_run=True, users_file=_users_file(tmp_path, ["only-one"]),
                               verifier=verifier())


def test_access_file_supplies_team_and_aud(tmp_path):
    f = tmp_path / "access.json"
    f.write_text(json.dumps({"team_domain": TEAM, "aud": AUD}), encoding="utf-8")
    assert production.access_settings(make_cfg(access_file=str(f))) == (TEAM, AUD)
    assert production.access_settings(make_cfg(access_file=str(f), access_aud="mine")) == (TEAM, "mine")


# ---------------------------------------------------------------- Teamhub links, state, watermark

def _settings(link="https://admin.example.com/tickets/{tid}"):
    return kt.Settings(room_id="r", workflow_id="w", teamhub_exe="k", workdir="w", state_dir="s", admin_link_template=link)


def test_teamhub_post_for_a_real_ticket_links_to_the_approve_page_without_client_text():
    v = {"id": 7, "client": "Initech Ltd", "text": PRIVATE, "is_test": False, "owner": None, "name": "n"}
    steps = otw.build_steps(_settings(), v, "https://odoo.example/ticket/7")
    blob = json.dumps(steps, ensure_ascii=False)
    assert "https://admin.example.com/tickets/7" in steps["root"] and "approved on the hub admin page" in steps["functional"]
    assert PRIVATE not in blob and "Telegram" not in steps["coordinator"]
    for op in (kt.create_op(7, steps["root"]), kt.reply_op(7, "coordinator", "r", steps["coordinator"])):
        assert kt.refusal_reason(op) is None
    plain = otw.build_steps(_settings(""), v, "x")
    assert "Telegram" in plain["coordinator"]                          # staging wording is unchanged


def test_first_run_seeds_the_watermark_instead_of_opening_the_backlog():
    ctx = SimpleNamespace(store=MemoryStore(), st=_settings(), dry_run=False,
                          odoo=SimpleNamespace(search_read=lambda *a, **k: [{"id": 99}]))
    ctx.st = kt.dataclasses.replace(ctx.st, seed_watermark=True) if hasattr(kt, "dataclasses") else ctx.st
    summary = {"opened": [], "refused": [], "skipped": [], "closed": [], "failed": [], "retry": []}
    otw._new_tickets(ctx, [["team_id", "=", 2]], {}, summary)
    assert ctx.store.get("teamhub_state", "watch") == {"last_id": 99} and summary["opened"] == []


# ---------------------------------------------------------------- watchdog

class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def adv(self, minutes):
        self.t += timedelta(minutes=minutes)


def make_wd(tmp_path, answers):
    cfg = prod_watchdog.WatchdogConfig(queue_dir=str(tmp_path / "q"), state_file=str(tmp_path / "st.json"),
                                       log_file=str(tmp_path / "wd.log"), teamhub_exe="teamhub-x")
    runs, clock = [], Clock()

    def fetch(url, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    return prod_watchdog.Watchdog(cfg, fetch=fetch, runner=lambda cmd, **k: runs.append(cmd), now=clock), runs, clock, cfg


def test_watchdog_posts_one_outage_and_one_recovery_with_the_exact_format(tmp_path):
    good, bad = {"ok": True, "accepting": True}, {"ok": False, "accepting": True}
    wd, runs, clock, cfg = make_wd(tmp_path, [good, OSError("down"), bad, {"ok": True, "accepting": False}, good, good])
    assert wd.step() == "ok"
    assert wd.step() == "outage-posted"
    assert wd.step() == "still-down" and wd.step() == "still-down"           # not ok, then accepting false: no more posts
    clock.adv(7)
    assert wd.step() == "recovery-posted" and wd.step() == "ok"
    files = sorted((tmp_path / "q").glob("*.md"))
    assert len(files) == 2 and len(runs) == 2
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("ROOM: a00000000005\nTHREAD: a00000000006\n---\n") and "not answering" in text
    assert "about 7 minute" in files[1].read_text(encoding="utf-8")
    assert runs[0] == ["teamhub-x", "workspace", "workflow", "trigger", "b00000000003", "--scope-room", "a00000000002"]
    blob = text + files[1].read_text(encoding="utf-8") + (tmp_path / "wd.log").read_text(encoding="utf-8")
    assert "http" not in blob and "twin.acme" not in blob and "127." not in blob


def test_watchdog_debounces_a_flapping_hub_and_only_announces_a_recovery_after_an_announced_outage(tmp_path):
    good, bad = {"ok": True, "accepting": True}, {"ok": False}
    wd, runs, clock, _ = make_wd(tmp_path, [bad, good, bad, good, bad, good])
    assert wd.step() == "outage-posted"
    clock.adv(1)
    assert wd.step() == "recovery-posted"
    clock.adv(1)
    assert wd.step() == "suppressed"                                         # second outage inside 15 minutes: no post
    clock.adv(1)
    assert wd.step() == "recovered"                                          # it was never announced, so no recovery post
    clock.adv(20)
    assert wd.step() == "outage-posted"
    clock.adv(1)
    assert wd.step() == "recovery-posted"                                    # 20+ minutes since the last recovery post
    assert len(runs) == 4


def test_watchdog_state_survives_a_restart_and_config_keys_are_all_values(tmp_path):
    wd, _, clock, cfg = make_wd(tmp_path, [{"ok": False}])
    wd.step()
    wd2 = prod_watchdog.Watchdog(cfg, fetch=lambda u, t: {"ok": False}, runner=lambda *a, **k: None, now=clock)
    assert wd2.step() == "still-down"                                        # no second outage post after a restart
    y = tmp_path / "w.yaml"
    y.write_text("interval_s: 30\nroom_id: abc\n", encoding="utf-8")
    c = prod_watchdog.load_config(str(y))
    assert (c.interval_s, c.room_id, c.debounce_s) == (30, "abc", 900)
    y.write_text("nope: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        prod_watchdog.load_config(str(y))


# ---------------------------------------------------------------- default owner: Riley, backup Supervisor

def _audit(hub, method):
    from twin_agent.hub.store import audit_rows
    return [r for r in audit_rows(hub.store) if r.get("method") == method]


def test_no_better_owner_means_the_default_owner_and_the_fallback_is_audited():
    hub, _ = build(make_cfg(backup_owner_uid=81))
    r = hub.handle_webhook(payload())
    assert r["owner"] == "Riley" and tickets(hub)[0]["user_id"] == 82
    (row,) = _audit(hub, "owner_fallback")
    assert row["reason"] == "default_owner" and row["new_owner_uid"] == 82 and row["ticket_id"] == r["ticket_id"]
    assert "private" not in json.dumps(row)


def test_an_unavailable_default_owner_falls_back_to_the_backup_and_it_is_audited():
    hub, _ = build(make_cfg(backup_owner_uid=81, unavailable_owner_uids=[82]))
    r = hub.handle_webhook(payload())
    assert r["owner"] == "Supervisor" and tickets(hub)[0]["user_id"] == 81
    (row,) = _audit(hub, "owner_fallback")
    assert row["reason"] == "backup_owner" and row["new_owner_uid"] == 81
    assert any("Supervisor" in x for x in [hub.ticket(r["ticket_id"]).rationale])


def test_nobody_available_keeps_the_default_and_says_so_and_a_real_owner_signal_wins():
    cfg = make_cfg(backup_owner_uid=81, unavailable_owner_uids=[82, 81])
    hub, _ = build(cfg)
    hub.handle_webhook(payload())
    assert _audit(hub, "owner_fallback")[0]["reason"] == "no_one_available" and tickets(hub)[0]["user_id"] == 82
    a = production._assigner(make_cfg(backup_owner_uid=81))
    from twin_agent.hub.analyse import analyse
    got = a.assign(analyse("my report is broken"), {"owner_uids": [81]})
    assert got["uid"] == 81 and got["fallback"] == ""                  # the client's usual owner: no fallback, no audit


def test_owner_82_is_the_automation_account_but_a_person_without_a_key_is_still_blocked():
    from twin_agent.hub.odoo_people import KeyMissing
    hub, _ = build(make_cfg(backup_owner_uid=81))
    r = hub.handle_webhook(payload())
    assert any(w for w in hub.store.scan("odoo_audit") if w.get("acting") == "odoo-riley")      # notes for owner 82: her own key
    with pytest.raises(KeyMissing):                                                             # Supervisor has no key mapped: blocked
        hub.odoo.post_note(r["ticket_id"], "x", owner_uid=81)
    assert not [w for w in hub.store.scan("odoo_audit") if w.get("on_behalf_of") and w.get("reason") == "no_key"]
    assert hub.store.scan("pending_notes")                                                      # parked, never run as automation
    hub2, _ = build(make_cfg(backup_owner_uid=81, odoo_people={"81": "odoo-supervisor"}))
    assert hub2.odoo.cfg.people[81] == "odoo-supervisor" and hub2.odoo.cfg.people[82] == "odoo-riley"


def test_owner_config_rails():
    why = lambda **o: production.problems(make_cfg(**o), have_app_secret=True, have_verify_token=True)   # noqa: E731
    assert any("backup owner" in w for w in why(backup_owner_uid=82))
    assert why(backup_owner_uid=81, unavailable_owner_uids=["82"]) == []
    assert make_cfg(unavailable_owner_uids=["82", "x"]).unavailable_owner_uids == (82,)
    assert any("NO BACKUP" in x for x in production.preflight(make_cfg(), dry_run=True))


# ---------------------------------------------------------------- staff numbers

def test_a_team_phone_gets_no_ticket_no_ack_and_a_safe_line_with_no_digits(tmp_path, caplog):
    f = tmp_path / "phones.json"
    f.write_text(json.dumps({"staff": {"riley": "+20 100 111 2233", "x": "201555000111"}}), encoding="utf-8")
    cfg = make_cfg(staff_phones_file=str(f))
    caplog.set_level("DEBUG")
    hub, wa = build(cfg)                      # CLIENT is also a partner in Odoo, but it is a team phone
    r = hub.handle_webhook(payload(text=PRIVATE))
    assert r.get("held") == "staff_number" and tickets(hub) == [] and wa.sent == []
    lines = posted(hub)
    assert len(lines) == 1 and "team member" in lines[0] and "2233" not in lines[0] and CLIENT not in lines[0]
    _assert_safe(lines)
    assert "2233" not in caplog.text and CLIENT not in caplog.text
    row = data.hold_rows(hub)[0]
    assert row["sender"] == "-" and "team member" in row["label"]
    hub.tick(now=hub.clock() + timedelta(minutes=10))
    assert wa.sent == []                                                  # not even the fallback ack
    assert hub.k_odoo.partner_for(CLIENT) is None


def test_staff_file_is_reread_when_it_changes_and_a_missing_file_means_none(tmp_path):
    f = tmp_path / "phones.json"
    s = prod_support.StaffNumbers(str(f))
    assert not s.contains(CLIENT)                                         # no file
    f.write_text(json.dumps({"staff": {"a": CLIENT}}), encoding="utf-8")
    assert s.contains(CLIENT) and s.contains("0100 111 2233") and not s.contains(OTHER)
    f.write_text(json.dumps({"staff": {}}), encoding="utf-8")
    import os
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert not s.contains(CLIENT)
    assert not prod_support.StaffNumbers("").contains(CLIENT)


# ---------------------------------------------------------------- round 3

def _change(field, pid=None):
    value = {"messaging_product": "whatsapp"}
    if pid:
        value["metadata"] = {"display_phone_number": "1", "phone_number_id": pid}
    return {"field": field, "value": value}


def _wrap(*changes):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": list(changes)}]}


def test_staging_ignores_changes_that_name_no_number_but_503s_when_any_change_names_a_foreign_one(tmp_path, capsys):
    from test_hub_production import _post
    from twin_agent.hub import staging
    from twin_agent.hub.staging import TEST_PHONE_NUMBER_ID
    cfg = staging.config_from_dict(dict(phone_number_id=TEST_PHONE_NUMBER_ID, test_partner_id=1187, allowed_senders=[CLIENT],
                                        staff_approver_id=90001, webhook_port=0, admin_port=0, launcher_port=0,
                                        users_file=str(tmp_path / "none" / "u.json")))
    rt, sec = staging.open_runtime(cfg, dry_run=True)
    rt.start()
    base = f"http://127.0.0.1:{rt.ports()['webhook']}"
    try:
        for f in ("message_template_status_update", "account_update", "phone_number_quality_update"):
            assert _post(base, _wrap(_change(f)), sec.app_secret) == 200, f           # no number named: ignored, answered
        mixed = _wrap(_change("account_update"), _change("messages", "999000111222"))
        assert _post(base, mixed, sec.app_secret) == 503                              # one foreign change: the whole payload
        own_plus_none = payload(mid="wamid.N1", phone_id=TEST_PHONE_NUMBER_ID)
        own_plus_none["entry"][0]["changes"].append(_change("account_update"))
        assert _post(base, own_plus_none, sec.app_secret) == 200
        rt.webhook.drain()
        assert [d["inbound"]["message_id"] for d in rt.hub.store.scan("messages")] == ["wamid.N1"]
    finally:
        rt.close()
    capsys.readouterr()


def test_watchdog_outage_that_began_inside_the_debounce_is_posted_once_it_has_passed(tmp_path):
    good, bad = {"ok": True, "accepting": True}, {"ok": False}
    wd, runs, clock, _ = make_wd(tmp_path, [bad, good] + [bad] * 19 + [good])
    clock.t = clock.t.replace(hour=10, minute=0)
    assert wd.step() == "outage-posted"                                      # 10:00 earlier alert
    clock.adv(5)
    assert wd.step() == "recovery-posted"                                    # 10:05
    clock.adv(5)
    assert wd.step() == "suppressed"                                         # 10:10: a new outage, inside the 15 minutes
    posts, t = [], 10 * 60 + 10
    while t < 13 * 60 + 10:
        clock.adv(10)
        t += 10
        out = wd.step()
        if out != "still-down":
            posts.append((f"{t // 60}:{t % 60:02d}", out))
    assert posts == [("10:20", "outage-posted")]                             # once the debounce passed, and only once
    clock.adv(1)
    assert wd.step() == "recovery-posted" and len(runs) == 4
    texts = [f.read_text(encoding="utf-8") for f in sorted((tmp_path / "q").glob("*.md"))]
    assert any("since 10:10 UTC" in x for x in texts)                        # the real start of the outage


def test_done_button_closes_the_hold_audits_who_and_open_holds_goes_down(tmp_path):
    import io
    users = _users_file(tmp_path, ["supervisor"])
    cfg = make_cfg()
    hub, _ = build(cfg)
    hub.handle_webhook(payload(wa_id=OTHER, mid="wamid.D1"))
    (h,) = hub.holds()
    assert hub.open_holds_count() == 1
    app = production.build_admin(cfg, SEC, hub, dry_run=True, users_file=users)
    sess = app.sessions.create("supervisor")
    cookie = {"HTTP_COOKIE": f"aa_session={sess.sid}"}
    code, page, _ = _call(app, host=app.cfg.allowed_hosts[0], headers=cookie, path="/holds")
    assert code == 200 and f"/holds/{h['id']}/done".encode() in page and b"Done" in page
    body = f"csrf={sess.csrf}".encode()
    post = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
            "CONTENT_TYPE": "application/x-www-form-urlencoded", **cookie}
    code, _, _ = _call(app, host=app.cfg.allowed_hosts[0], path=f"/holds/{h['id']}/done", headers=post)
    assert code in (302, 303) and hub.open_holds_count() == 0
    (row,) = _audit(hub, "done")
    assert row["hub_user"] == "supervisor" and row["hold_id"] == h["id"] and "access_email" in row


def test_each_access_email_may_only_use_its_own_hub_user(tmp_path):
    import io
    from twin_agent.hub.admin.web import Request
    users = _users_file(tmp_path, ACCESS_USERS.values())
    cfg = make_cfg(public_admin=True, access_users=ACCESS_USERS)
    hub, _ = build(cfg)
    gate = production.build_admin(cfg, SEC, hub, dry_run=True, users_file=users, verifier=verifier())
    app = gate._app
    tunnel = {"HTTP_CF_CONNECTING_IP": "203.0.113.9", "HTTP_X_FORWARDED_PROTO": "https"}
    supervisor_tok, sysadmin_tok = token("supervisor@example.com"), token("sysadmin@example.com")
    probe = Request({"REQUEST_METHOD": "GET", "HTTP_HOST": "admin.example.com", "REMOTE_ADDR": "127.0.0.1", **tunnel}, app.trusted)
    sid = app.sessions.create("supervisor").sid
    h = {**tunnel, "HTTP_COOKIE": f"{app._cn(probe, 'aa_session')}={sid}"}
    assert _call(gate, headers={**h, "HTTP_CF_ACCESS_JWT_ASSERTION": supervisor_tok}, path="/holds")[0] == 200
    assert _call(gate, headers={**h, "HTTP_CF_ACCESS_JWT_ASSERTION": sysadmin_tok}, path="/holds")[0] == 403   # Sysadmin on Supervisor's session
    pre = "x" * 24
    body = f"csrf={pre}&username=supervisor&password=pw".encode()

    def login(tok):
        env = {**tunnel, "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
               "CONTENT_TYPE": "application/x-www-form-urlencoded", "HTTP_COOKIE": f"{app._cn(probe, 'aa_pre')}={pre}",
               "HTTP_CF_ACCESS_JWT_ASSERTION": tok}
        return _call(gate, headers=env, path="/login")

    code, text, _ = login(sysadmin_tok)
    assert code == 403 and b"may not log in" in text                                         # verified email != hub user
    assert login(supervisor_tok)[0] in (302, 303)                                                        # her own user logs in


def test_public_admin_refuses_to_start_without_pyjwt(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "jwt", None)
    with pytest.raises(ProductionRefused, match="PyJWT"):
        production.preflight(make_cfg(public_admin=True, access_users=ACCESS_USERS), dry_run=True)
    production.need_jwt(make_cfg())                                                          # not needed when the mode is off


def test_a_staff_phone_posts_to_teamhub_once_per_number_per_day_but_every_hold_is_recorded(tmp_path):
    f = tmp_path / "phones.json"
    f.write_text(json.dumps({"staff": {"riley": CLIENT, "supervisor": OTHER}}), encoding="utf-8")
    day = [datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)]
    hub, wa = build(make_cfg(staff_phones_file=str(f)), clock=lambda: day[0])
    for i in range(3):
        hub.handle_webhook(payload(mid=f"wamid.S{i}", ts=day[0].timestamp() + 1))
    hub.handle_webhook(payload(mid="wamid.O1", wa_id=OTHER, ts=day[0].timestamp() + 1))
    assert len(hub.holds()) == 4 and len(posted(hub)) == 2                   # one line per staff number
    day[0] += timedelta(days=1)
    hub.handle_webhook(payload(mid="wamid.S9", ts=day[0].timestamp() + 1))
    assert len(posted(hub)) == 3 and wa.sent == []                            # the next day: one more
