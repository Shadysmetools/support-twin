"""Issue #63 hardening + the real Graph client + the Embedded Signup launcher.

NO network: Graph is an ``httpx.MockTransport``; the threaded-server test talks only to a loopback socket it opens
itself. NO secrets: tokens, codes and the app secret are sentinel strings in ``tmp_path``. Never the real vault."""
from __future__ import annotations

import io
import json
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from admin_helpers import Client, make_app

from twin_agent.hub.admin import __main__ as cli
from twin_agent.hub.admin import auth, proxy, seed
from twin_agent.hub.admin.auth import RateLimiter
from twin_agent.hub.admin.config import AdminConfig
from twin_agent.hub.admin.graph import FakeGraphClient, GraphError, GraphWriter
from twin_agent.hub.admin.graph_real import RealGraphClient, exchange_code
from twin_agent.hub.admin.launcher import LauncherApp
from twin_agent.hub.admin.meta_store import BusinessStore, SignupStates
from twin_agent.hub.admin.server import make_threaded_server


TOKEN, CODE, APP_SECRET = "TOKEN-SENTINEL-9f3a", "CODE-SENTINEL-77c1", "APPSECRET-SENTINEL-5b2e"
LAUNCHER = "https://launch.example"


# ------------------------------------------------------------------ helpers

def wsgi(app, method="GET", path="/", *, body=b"", query="", host="launch.example", ip="203.0.113.7", headers=None):
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "REMOTE_ADDR": ip, "HTTP_HOST": host,
           "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "wsgi.url_scheme": "http"}
    env.update(headers or {})
    out = {}
    out["body"] = b"".join(app(env, lambda s, h: out.update(status=int(s.split()[0]), headers=h))).decode("utf-8")
    out["h"] = {k: v for k, v in out["headers"] if k != "Set-Cookie"}
    out["cookies"] = [v for k, v in out["headers"] if k == "Set-Cookie"]
    return type("R", (), out)


def secrets_file(tmp_path, app_id="123"):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"app_id": app_id, "app_secret": APP_SECRET}), encoding="utf-8")
    return p


def mock_http(handler):
    seen = []

    def wrapped(req):
        seen.append(req)
        return handler(req)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), seen


GRANTS = {TOKEN: {"waba": "111", "phones": ["222"]}}      # what Meta says each token really grants


def token_ok(req):
    """Mock Meta for the launcher: oauth exchange, debug_token (granular scopes), the WABA's number list."""
    path = req.url.path
    if path.endswith("/oauth/access_token"):
        code = parse_qs(req.content.decode())["code"][0]
        return httpx.Response(200, json={"access_token": CODE_TOKENS.get(code, TOKEN), "token_type": "bearer",
                                         "expires_in": 5000})
    if path.endswith("/debug_token"):
        g = GRANTS.get(req.url.params["input_token"], {})
        return httpx.Response(200, json={"data": {"is_valid": bool(g), "app_id": "123", "granular_scopes": [
            {"scope": "whatsapp_business_management", "target_ids": [g.get("waba", "0")]},
            {"scope": "whatsapp_business_messaging", "target_ids": [g.get("waba", "0")]}]}})
    if path.endswith("/phone_numbers"):
        tok = req.headers["Authorization"].split()[1]
        return httpx.Response(200, json={"data": [{"id": p} for p in GRANTS.get(tok, {}).get("phones", [])]})
    return httpx.Response(404, json={})


CODE_TOKENS: dict = {}


@pytest.fixture(autouse=True)
def _reset_grants():
    GRANTS.clear()
    GRANTS[TOKEN] = {"waba": "111", "phones": ["222"]}
    CODE_TOKENS.clear()


def make_launcher(tmp_path, handler=token_ok, **kw):
    states, biz = SignupStates(), BusinessStore(tmp_path / "biz")
    http, seen = mock_http(handler)
    app = LauncherApp(states=states, businesses=biz, app_id="123", config_id="456", graph_version="v27.0",
                      app_secret_file=(kw.pop("secret", None) or secrets_file(tmp_path)), allowed_hosts=("launch.example",),
                      http=http, **kw)
    return app, states, biz, seen


def post_cb(app, payload, *, origin="http://launch.example", ctype="application/json", **kw):
    return wsgi(app, "POST", "/callback", body=json.dumps(payload).encode(),
                headers={"HTTP_ORIGIN": origin, "CONTENT_TYPE": ctype}, **kw)


# ------------------------------------------------------------------ proxy-aware mode

def test_cf_connecting_ip_is_believed_only_from_a_trusted_peer():
    nets = proxy.parse_networks(["127.0.0.1/32", "::1/128"])
    env = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "198.51.100.9"}
    assert proxy.client_ip(env, nets) == "198.51.100.9"
    assert proxy.client_ip({**env, "REMOTE_ADDR": "203.0.113.5"}, nets) == "203.0.113.5"       # spoof attempt
    assert proxy.client_ip({**env, "HTTP_CF_CONNECTING_IP": "not-an-ip"}, nets) == "127.0.0.1"
    assert proxy.client_ip(env, ()) == "127.0.0.1"                                              # nobody trusted


def test_a_bad_trusted_proxy_entry_fails_loudly():
    with pytest.raises(ValueError):
        proxy.parse_networks(["not-a-cidr"])


def test_login_limiter_sees_the_real_client_behind_the_tunnel():
    app, _, _ = make_app(limiter=RateLimiter(max_fails=2), cfg={"trusted_proxies": ("127.0.0.1/32",)})
    a = lambda ip: Client(app, ip="127.0.0.1", env={"HTTP_CF_CONNECTING_IP": ip})   # noqa: E731
    for _ in range(2):
        a("198.51.100.1").login("demo-approver", "bad")
    assert a("198.51.100.1").login("demo-approver", "bad").status == 429               # that visitor is blocked...
    assert a("198.51.100.2").login("demo-approver", "bad").status == 403               # ...not everyone behind the proxy


def test_cloudflare_headers_from_an_untrusted_peer_are_refused_not_believed():
    app, _, _ = make_app()                                                               # no trusted proxies
    for env in ({"HTTP_CF_CONNECTING_IP": "198.51.100.1"}, {"HTTP_CF_VISITOR": '{"scheme":"https"}'}):
        r = Client(app, ip="203.0.113.5", env=env).get("/login")
        assert r.status == 400 and "--trusted-proxy" in r.body
    assert Client(app, ip="203.0.113.5").get("/login").status == 200


def test_a_deeply_nested_callback_body_is_a_400_not_a_traceback(tmp_path):
    app, _, _, _ = make_launcher(tmp_path)
    r = wsgi(app, "POST", "/callback", body=b"[" * 15000,
             headers={"HTTP_ORIGIN": "http://launch.example", "CONTENT_TYPE": "application/json"})
    assert r.status == 400


def test_cookies_get_the_host_prefix_when_secure_and_not_otherwise():
    app, _, _ = make_app(cookie_secure=True)
    r = Client(app).get("/login")
    assert r.set_cookies[0].startswith("__Host-aa_pre=") and "Secure" in r.set_cookies[0] and "Domain" not in r.set_cookies[0]
    app2, _, _ = make_app(cookie_secure=False)
    assert Client(app2).get("/login").set_cookies[0].startswith("aa_pre=")
    c = Client(app)
    assert c.login().status == 303 and c.get("/tickets").status == 200            # the prefixed cookie round-trips


@pytest.mark.parametrize("host,ok", [("127.0.0.1:8780", True), ("LOCALHOST:8780", True), ("[::1]:8780", True),
                                     ("evil.example", False), ("127.0.0.1:8780@evil.example.net", False),
                                     ("", False), ("127.0.0.1:9999", False)])
def test_host_allowlist_from_config_including_ipv6(host, ok):
    hosts = proxy.loopback_hosts(8780)
    app, _, _ = make_app(cfg={"allowed_hosts": hosts})
    assert (Client(app, env={"HTTP_HOST": host}).get("/login").status == 200) is ok


def test_https_via_a_trusted_proxy_gets_secure_cookies_and_hsts():
    app, _, _ = make_app(cookie_secure=False, cfg={"trusted_proxies": ("127.0.0.1/32",)})
    https = {"HTTP_X_FORWARDED_PROTO": "https"}
    r = Client(app, env=https).get("/login")
    assert "Strict-Transport-Security" in r.h and all("Secure" in c for c in r.set_cookies)
    r2 = Client(app, env={"HTTP_CF_VISITOR": '{"scheme":"https"}'}).get("/login")
    assert "Strict-Transport-Security" in r2.h
    # the same headers from a peer that is NOT trusted are just text: no HSTS, and (cookie_secure off) no Secure
    r3 = Client(app, ip="203.0.113.5", env=https).get("/login")
    assert "Strict-Transport-Security" not in r3.h and not any("Secure" in c for c in r3.set_cookies)


def test_plain_http_loopback_has_no_hsts_and_https_login_cookie_is_secure_httponly_strict():
    app, _, _ = make_app(cookie_secure=False, cfg={"trusted_proxies": ("127.0.0.1/32",)})
    assert "Strict-Transport-Security" not in Client(app).get("/login").h
    c = Client(app, env={"HTTP_X_FORWARDED_PROTO": "https"})
    r = c.login()
    session = next(x for x in r.set_cookies if x.startswith("__Host-aa_session=") and "Max-Age=0" not in x)
    assert "Secure" in session and "HttpOnly" in session and "SameSite=Strict" in session


# ------------------------------------------------------------------ login limiter

def test_no_global_lockout_an_attacker_cannot_lock_the_real_user_out():
    app, _, _ = make_app(limiter=RateLimiter(max_fails=3))
    for _ in range(3):
        Client(app, ip="203.0.113.5").login("demo-approver", "bad")
    assert Client(app, ip="203.0.113.5").login("demo-approver", "bad").status == 429
    assert Client(app, ip="198.51.100.20").login("demo-approver", "correct horse battery").status == 303


def test_one_ip_cannot_spray_many_usernames():
    app, _, _ = make_app(limiter=RateLimiter(max_fails=3, ip_max_fails=4))
    c = Client(app, ip="203.0.113.5")
    for i in range(4):
        c.login(f"user{i}", "bad")
    assert c.login("user99", "bad").status == 429


def test_limiter_memory_is_bounded():
    lim = RateLimiter(max_keys=50)
    for i in range(500):
        lim.fail(f"10.0.{i // 250}.{i % 250}", f"user{i}")
    assert len(lim._f) <= 50


def test_limiter_drops_a_key_once_its_window_has_passed():
    now = [1000.0]
    lim = RateLimiter(window=10, clock=lambda: now[0])
    lim.fail("1.1.1.1", "u")
    assert "ip:1.1.1.1" in lim._f
    now[0] += 11
    assert lim.blocked("1.1.1.1", "u") is False
    assert "ip:1.1.1.1" not in lim._f and "pair:1.1.1.1|u" not in lim._f


# ------------------------------------------------------------------ AdminApp refuses a non-demo hub

def test_adminapp_refuses_a_hub_without_the_demo_marker_unless_allow_live():
    from twin_agent.hub.admin.app import AdminApp
    plain = seed.build_demo_hub(":memory:")                     # fakes, but NO demo marker (never seeded)
    cfg = AdminConfig()
    with pytest.raises(RuntimeError, match="not the demo hub"):
        AdminApp(plain, cfg, FakeGraphClient())
    assert AdminApp(plain, AdminConfig(allow_live=True), FakeGraphClient())
    assert AdminApp(plain, cfg, FakeGraphClient(), allow_live=True)


# ------------------------------------------------------------------ wording: never claim "nothing was sent" after a send

def test_action_failed_after_a_send_is_not_reported_as_nothing_sent():
    from admin_helpers import approve_form, logged_in
    c, hub, _ = logged_in()
    hub.on_owner_decision = lambda *a, **k: {"ok": False, "reason": "action_failed"}
    form = approve_form(c, hub, 20004)
    r = c.post("/tickets/20004/approve", form)
    assert r.h["Location"].endswith("?r=action_failed")
    page = c.get("/tickets/20004?r=action_failed").body
    assert "Nothing was sent" not in page and "may already have gone out" in page


def test_an_unexpected_error_page_is_neutral(monkeypatch):
    app, _, _ = make_app()
    monkeypatch.setattr(app, "handle", lambda req: (_ for _ in ()).throw(RuntimeError("boom")))
    r = Client(app).get("/login")
    assert r.status == 500 and "Nothing was sent" not in r.body and "boom" not in r.body


# ------------------------------------------------------------------ demo password goes only to a real terminal

def test_password_is_printed_on_a_tty_and_filed_otherwise(tmp_path):
    users = tmp_path / "users.json"
    shown = cli.deliver_password("pw-SENTINEL", users, tty=True)
    assert "pw-SENTINEL" in shown and not list(tmp_path.glob("*.password.txt"))
    hidden = cli.deliver_password("pw-SENTINEL", users, tty=False)
    assert "pw-SENTINEL" not in hidden
    f = users.with_suffix(".password.txt")
    assert f.read_text().strip() == "pw-SENTINEL" and str(f) in hidden
    assert cli.deliver_password(None, users, tty=False).endswith("(kept)")


# ------------------------------------------------------------------ threaded server with timeouts

def test_slow_client_does_not_block_other_requests_and_idle_sockets_time_out():
    srv = make_threaded_server("127.0.0.1", 0, lambda env, sr: (sr("200 OK", [("Content-Type", "text/plain")]), [b"ok"])[1],
                               timeout=0.6)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        idle = socket.create_connection(("127.0.0.1", port))        # opens and says nothing (slowloris shape)
        ok = httpx.get(f"http://127.0.0.1:{port}/", timeout=3)
        assert ok.status_code == 200 and ok.text == "ok"            # served while the idle socket is still open
        idle.settimeout(3)
        try:
            data = idle.recv(10)
        except (ConnectionResetError, ConnectionAbortedError):
            data = b""
        assert data == b""                                          # the server hung up on the idle socket
        idle.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
    assert not t.is_alive()


# ------------------------------------------------------------------ Embedded Signup: admin side

def signup_app(tmp_path, **cfg):
    states, biz = SignupStates(), BusinessStore(tmp_path / "biz")
    base = {"launcher_url": LAUNCHER, **cfg}
    app, hub, _ = make_app(app_id="123", config_id="456", cfg=base, signups=states, businesses=biz)
    c = Client(app)
    assert c.login().status == 303
    return c, states, biz


def test_connect_start_mints_a_single_use_state_and_redirects_to_the_launcher_origin(tmp_path):
    c, states, _ = signup_app(tmp_path)
    page = c.get("/connect")
    assert "connect.facebook.net" not in page.body and "<script" not in page.body       # no SDK in the admin origin
    assert "form-action 'self' https://launch.example" in page.h["Content-Security-Policy"]
    assert "connect-src" not in page.h["Content-Security-Policy"] and "facebook" not in page.h["Content-Security-Policy"]
    assert c.post("/connect/start", {"csrf": "wrong", "business": "bakery"}).status == 403
    r = c.post("/connect/start", {"csrf": c.csrf("/connect"), "business": "Bakery"})
    u = urlsplit(r.h["Location"])
    assert f"{u.scheme}://{u.netloc}" == LAUNCHER and u.path == "/"
    state = parse_qs(u.query)["s"][0]
    assert states.peek(state) == "bakery"


def test_connect_start_rejects_bad_names_and_is_off_without_config(tmp_path):
    c, _, _ = signup_app(tmp_path)
    for bad in ("../x", "a b", "", "x" * 41):
        r = c.post("/connect/start", {"csrf": c.csrf("/connect"), "business": bad})
        assert r.h["Location"].endswith("connect_bad")
    c2, _, _ = signup_app(tmp_path, launcher_url="")
    assert c2.post("/connect/start", {"csrf": c2.csrf("/connect"), "business": "ok"}).h["Location"].endswith("connect_off")


def test_pending_states_are_bounded():
    s = SignupStates(max_pending=3)
    assert all(s.new("b") for _ in range(3)) and s.new("b") is None and s.new("../x") is None


def test_state_expires_and_is_single_use_per_kind():
    now = [100.0]
    s = SignupStates(ttl=10, clock=lambda: now[0])
    st = s.new("b")
    assert s.take(st, "code") == "b" and s.take(st, "code") is None                 # the code is single use
    assert s.take(st, "session", "CANCEL") == "b" and s.take(st, "session", "FINISH") == "b"   # info may repeat...
    assert s.take(st, "session", "FINISH") is None                                  # ...until a FINISH
    st2 = s.new("b")
    now[0] += 11
    assert s.take(st2, "code") is None


# ------------------------------------------------------------------ Meta writes: off by default, explicit when on

def numbers_with(tmp_path, **cfg):
    fake = FakeGraphClient()
    states, biz = SignupStates(), BusinessStore(tmp_path / "biz")
    app, _, _ = make_app(graph=fake, cfg=cfg, signups=states, businesses=biz)
    c = Client(app)
    assert c.login().status == 303
    return c, fake


def test_meta_writes_are_off_by_default_everywhere(tmp_path):
    c, fake = numbers_with(tmp_path)
    page = c.get("/numbers").body
    assert "allow_meta_writes" in page and page.count(" disabled") >= 3
    for path, form in (("/meta/subscribe", {"waba_id": "1000000000001", "business": "demo-bakery", "confirm": "yes"}),
                       ("/meta/sync", {"phone_number_id": "1000000000002", "business": "demo-bakery", "sync_type": "history", "confirm": "yes"})):
        r = c.post(path, {"csrf": c.csrf("/numbers"), **form})
        assert r.h["Location"].endswith("meta_disabled")
    assert not any(x.startswith(("subscribe", "start_sync")) for x in fake.calls)


def test_nothing_in_the_read_paths_or_signup_calls_a_write(tmp_path):
    c, fake = numbers_with(tmp_path)
    for p in ("/numbers", "/templates", "/connect", "/tickets"):
        c.get(p)
    assert not any(x.startswith(("subscribe", "start_sync")) for x in fake.calls)


def test_meta_writes_when_enabled_need_csrf_confirm_and_valid_input(tmp_path):
    c, fake = numbers_with(tmp_path, allow_meta_writes=True)
    assert "allow_meta_writes" not in c.get("/numbers").body
    assert c.post("/meta/subscribe", {"csrf": "x", "waba_id": "1", "confirm": "yes"}).status == 403
    bad = [("/meta/subscribe", {"waba_id": "1000000000001"}),                                   # no confirm
           ("/meta/subscribe", {"waba_id": "../etc", "business": "demo-bakery", "confirm": "yes"}),
           ("/meta/subscribe", {"waba_id": "1000000000001", "business": "../x", "confirm": "yes"}),         # bad business
           ("/meta/sync", {"phone_number_id": "1000000000002", "sync_type": "delete", "confirm": "yes"})]
    for path, form in bad:
        assert c.post(path, {"csrf": c.csrf("/numbers"), **form}).h["Location"].endswith("meta_bad")
    assert not any(x.startswith(("subscribe", "start_sync")) for x in fake.calls)
    ok = c.post("/meta/subscribe", {"csrf": c.csrf("/numbers"), "waba_id": "1000000000001", "business": "demo-bakery", "confirm": "yes"})
    assert ok.h["Location"].endswith("meta_ok") and fake.calls[-1] == "subscribe_app:1000000000001:demo-bakery"
    ok = c.post("/meta/sync", {"csrf": c.csrf("/numbers"), "phone_number_id": "1000000000002", "business": "demo-bakery",
                               "sync_type": "history", "confirm": "yes"})
    assert fake.calls[-1] == "start_sync:1000000000002:history:demo-bakery" and ok.h["Location"].endswith("meta_ok")


def test_meta_write_failure_is_reported_not_retried(tmp_path):
    c, fake = numbers_with(tmp_path, allow_meta_writes=True)
    fake.fail = True
    r = c.post("/meta/subscribe", {"csrf": c.csrf("/numbers"), "waba_id": "1000000000001", "business": "demo-bakery", "confirm": "yes"})
    assert r.h["Location"].endswith("meta_failed") and fake.calls.count("subscribe_app:1000000000001:demo-bakery") == 1


def test_the_fake_and_real_clients_both_satisfy_the_write_seam(tmp_path):
    assert isinstance(FakeGraphClient(), GraphWriter)
    assert isinstance(RealGraphClient(BusinessStore(tmp_path), app_id="1"), GraphWriter)


# ------------------------------------------------------------------ the launcher (its own origin)

def test_launcher_page_needs_a_live_state_sets_no_cookie_and_has_a_strict_csp(tmp_path):
    app, states, _, _ = make_launcher(tmp_path)
    dead = wsgi(app, query="s=nope")
    assert "disabled" in dead.body and "expired" in dead.body and 'data-state=""' in dead.body
    live = wsgi(app, query=f"s={states.new('bakery')}")
    assert "disabled" not in live.body and "connect.facebook.net/en_US/sdk.js" in live.body
    assert live.cookies == [] and "Set-Cookie" not in live.h
    csp = live.h["Content-Security-Policy"]
    assert "script-src 'self' https://connect.facebook.net" in csp and "form-action 'none'" in csp
    assert "'unsafe-eval'" not in csp and "script-src 'self' https://connect.facebook.net;" in csp
    assert "frame-ancestors 'none'" in csp                   # (style-src allows inline: this origin holds nothing)


def test_launcher_has_no_admin_pages_and_a_two_name_static_allowlist(tmp_path):
    app, _, _, _ = make_launcher(tmp_path)
    for path in ("/tickets", "/login", "/numbers", "/static/admin.css", "/static/../app.py", "/static/signup.js"):
        assert wsgi(app, path=path).status == 404
    assert wsgi(app, path="/static/launcher.js").status == 200 and wsgi(app, path="/static/launcher.css").status == 200
    assert wsgi(app, "GET", "/callback").status == 404


def test_launcher_host_allowlist(tmp_path):
    app, _, _, _ = make_launcher(tmp_path)
    assert wsgi(app, host="evil.example").status == 400


def test_callback_checks_origin_content_type_size_and_state(tmp_path):
    app, states, biz, seen = make_launcher(tmp_path)
    good = {"kind": "code", "code": CODE, "state": states.new("bakery")}
    assert post_cb(app, good, origin="https://evil.example").status == 403
    assert post_cb(app, good, origin="").status == 403
    assert post_cb(app, good, ctype="text/plain").status == 415
    assert wsgi(app, "POST", "/callback", body=b"x" * 20000,
                headers={"HTTP_ORIGIN": "http://launch.example", "CONTENT_TYPE": "application/json"}).status == 413
    assert post_cb(app, {**good, "state": "forged"}).status == 403
    assert post_cb(app, {"kind": "bogus", "state": good["state"]}).status == 400
    assert wsgi(app, "POST", "/callback", body=b"[1,2]",
                headers={"HTTP_ORIGIN": "http://launch.example", "CONTENT_TYPE": "application/json"}).status == 400
    assert not seen and biz.slugs() == []                          # nothing reached Meta, nothing was stored


def test_https_origin_is_expected_behind_a_trusted_proxy(tmp_path):
    app, states, _, _ = make_launcher(tmp_path, trusted_proxies=("127.0.0.1/32",))
    h = {"HTTP_X_FORWARDED_PROTO": "https"}
    st = states.new("bakery")
    body = {"kind": "session", "event": "FINISH", "state": st}
    r = wsgi(app, "POST", "/callback", ip="127.0.0.1", body=json.dumps({**body, "state": st}).encode(),
             headers={**h, "HTTP_ORIGIN": "https://launch.example", "CONTENT_TYPE": "application/json"})
    assert r.status == 200 and "Strict-Transport-Security" in r.h


def test_code_exchange_stores_the_token_per_business_and_never_echoes_it(tmp_path, capsys, caplog):
    app, states, biz, seen = make_launcher(tmp_path)
    st = states.new("bakery")
    s = post_cb(app, {"kind": "session", "state": st, "event": "FINISH", "waba_id": "111", "phone_number_id": "222",
                      "business_id": "333"})
    assert s.status == 200
    r = post_cb(app, {"kind": "code", "state": st, "code": CODE})
    assert r.status == 200 and TOKEN not in r.body and TOKEN not in str(r.headers)
    rec = biz.get("bakery")
    assert rec["business_token"] == TOKEN and rec["waba_id"] == "111" and rec["phone_number_id"] == "222"
    assert rec["claimed_business_id"] == "333"
    req = seen[0]                                                 # the exchange; verification calls follow it
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == ["access_token", "debug_token", "phone_numbers"]
    assert seen[1].headers["Authorization"] == f"Bearer 123|{APP_SECRET}"          # app token, in a header
    assert req.method == "POST" and str(req.url) == "https://graph.facebook.com/v27.0/oauth/access_token"
    form = parse_qs(req.content.decode())
    assert form["client_id"] == ["123"] and form["code"] == [CODE] and form["client_secret"] == [APP_SECRET]
    assert CODE not in str(req.url) and APP_SECRET not in str(req.url)       # secret and code travel in the body only
    assert post_cb(app, {"kind": "code", "state": st, "code": CODE}).status == 403      # single use: replay refused
    out = capsys.readouterr()
    for blob in (out.out, out.err, caplog.text):
        assert TOKEN not in blob and CODE not in blob and APP_SECRET not in blob


def test_session_ids_must_be_digits_and_events_are_clipped(tmp_path):
    app, states, biz, _ = make_launcher(tmp_path)
    st = states.new("bakery")
    post_cb(app, {"kind": "session", "state": st, "event": "E" * 500, "waba_id": "12a", "phone_number_id": "../x",
                  "business_id": "999"})
    rec = biz.get("bakery")
    assert not any(k in rec for k in ("waba_id", "phone_number_id", "claimed_waba_id", "claimed_phone_number_id"))
    assert rec["claimed_business_id"] == "999"
    assert len(rec["last_event"]) == 64


def test_exchange_failures_are_fixed_sentences_and_store_nothing(tmp_path):
    def refuse(_req):
        return httpx.Response(400, json={"error": {"code": 100, "message": f"bad code {CODE} secret {APP_SECRET}"}})

    app, states, biz, _ = make_launcher(tmp_path, refuse)
    r = post_cb(app, {"kind": "code", "state": states.new("bakery"), "code": CODE})
    assert r.status == 400 and CODE not in r.body and APP_SECRET not in r.body and biz.slugs() == []


def test_a_mismatched_or_missing_app_secret_file_blocks_the_exchange(tmp_path):
    app, states, _biz, seen = make_launcher(tmp_path, secret=secrets_file(tmp_path, app_id="999"))
    assert post_cb(app, {"kind": "code", "state": states.new("b"), "code": CODE}).status == 400
    app2, states2, _, seen2 = make_launcher(tmp_path, secret=tmp_path / "missing.json")
    assert post_cb(app2, {"kind": "code", "state": states2.new("b"), "code": CODE}).status == 400
    assert not seen and not seen2                                    # never reached Meta without a valid secret


def test_callback_is_rate_limited_per_ip(tmp_path):
    app, _states, _, _ = make_launcher(tmp_path, limiter=RateLimiter(max_fails=3, ip_max_fails=3))
    codes = [post_cb(app, {"kind": "session", "state": "forged"}).status for _ in range(5)]
    assert codes[:3] == [403] * 3 and codes[3:] == [429, 429]


# ------------------------------------------------------------------ tokens never reach the admin pages

def test_admin_pages_never_show_the_stored_token(tmp_path):
    biz = BusinessStore(tmp_path / "biz")
    biz.save("bakery", {"business_token": TOKEN, "waba_id": "111", "phone_number_id": "222", "last_event": "FINISH"})
    states = SignupStates()
    app, _, _ = make_app(cfg={"launcher_url": LAUNCHER}, signups=states, businesses=biz)
    c = Client(app)
    c.login()
    for path in ("/connect", "/numbers", "/templates", "/tickets"):
        r = c.get(path)
        assert TOKEN not in r.body and TOKEN not in str(r.headers)
    assert "bakery" in c.get("/connect").body and "yes" in c.get("/connect").body
    assert all("business_token" not in b for b in biz.public())


# ------------------------------------------------------------------ BusinessStore

def test_business_store_merges_files_atomically_and_refuses_odd_names(tmp_path):
    biz = BusinessStore(tmp_path / "biz")
    biz.save("bakery", {"waba_id": "1", "ignored_field": "x"})
    biz.save("bakery", {"business_token": TOKEN})
    biz.save("ok-two", {"waba_id": "2"})
    rec = biz.get("bakery")
    assert rec["waba_id"] == "1" and rec["business_token"] == TOKEN and "ignored_field" not in rec
    assert biz.slugs() == ["bakery", "ok-two"] and not list((tmp_path / "biz").glob(".*.tmp"))
    assert biz.token_for("bakery", waba_id="1") == TOKEN and biz.token_for("bakery", waba_id="2") == ""
    assert biz.token_for("ok-two", waba_id="1") == ""                             # an id only works for its own business
    for bad in ("../x", "A", "", "a/b", "-a"):
        with pytest.raises(ValueError):
            biz.save(bad, {"waba_id": "1"})


# ------------------------------------------------------------------ the real Graph client (mock transport)

def graph_handler(req):
    path, q = req.url.path, dict(req.url.params)
    if path.endswith("/subscribed_apps") and req.method == "GET":
        return httpx.Response(200, json={"data": [{"whatsapp_business_api_data": {"id": "123", "name": "us"}}]})
    if path.endswith("/phone_numbers"):
        return httpx.Response(200, json={"data": [{"id": "222", "display_phone_number": "+20 100 000 4444",
                                                   "verified_name": "Demo Bakery", "status": "CONNECTED",
                                                   "quality_rating": "GREEN"}]})
    if path.endswith("/message_templates"):
        return httpx.Response(200, json={"data": [{"name": "order_update", "language": "en", "category": "UTILITY",
                                                   "status": "APPROVED",
                                                   "components": [{"type": "HEADER", "text": "h"},
                                                                  {"type": "BODY", "text": "Hello {{1}}"}]}]})
    if q.get("fields") == "id,name":
        return httpx.Response(200, json={"id": "111", "name": "Demo Bakery"})
    return httpx.Response(404, json={})


def real(tmp_path, handler=graph_handler):
    biz = BusinessStore(tmp_path / "biz")
    biz.save("bakery", {"business_token": TOKEN, "waba_id": "111", "phone_number_id": "222"})
    http, seen = mock_http(handler)
    return RealGraphClient(biz, app_id="123", client=http), seen


def test_real_client_reads_waba_numbers_status_subscription_and_templates(tmp_path):
    g, seen = real(tmp_path)
    (w,) = g.list_wabas()
    assert w["id"] == "111" and w["name"] == "Demo Bakery"
    (n,) = w["phone_numbers"]
    assert n["status"] == "CONNECTED" and n["quality_rating"] == "GREEN" and n["webhooks_subscribed"] is True
    (t,) = g.list_templates("111", "bakery")
    assert t["name"] == "order_update" and t["body"] == "Hello {{1}}" and t["status"] == "APPROVED"
    assert {r.method for r in seen} == {"GET"}                                  # reads never POST
    assert all(str(r.url).startswith("https://graph.facebook.com/v27.0/") for r in seen)
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen)
    assert all(TOKEN not in str(r.url) for r in seen)                           # the token is never in a URL


def test_real_client_marks_webhooks_not_subscribed_when_our_app_is_absent(tmp_path):
    def other_app(req):
        if req.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"data": [{"whatsapp_business_api_data": {"id": "999"}}]})
        return graph_handler(req)

    g, _ = real(tmp_path, other_app)
    assert g.list_wabas()[0]["phone_numbers"][0]["webhooks_subscribed"] is False


def test_real_client_with_no_connected_business_reads_nothing(tmp_path):
    http, seen = mock_http(graph_handler)
    g = RealGraphClient(BusinessStore(tmp_path / "empty"), app_id="123", client=http)
    assert g.list_wabas() == [] and not seen
    with pytest.raises(GraphError):
        g.list_templates("111", "bakery")
    assert not seen


def test_real_client_errors_are_fixed_sentences_with_numeric_codes_only(tmp_path):
    def err(_req):
        return httpx.Response(401, json={"error": {"code": 190, "message": f"token {TOKEN} expired", "fbtrace_id": "zz"}})

    g, _ = real(tmp_path, err)
    (w,) = g.list_wabas()                                     # a failing business is an error entry, not an exception
    assert w["error"] == "Meta refused the request (HTTP 401, code 190)." and TOKEN not in w["error"]

    def junk(_req):
        return httpx.Response(200, content=b"<html>not json " + TOKEN.encode())

    g2, _ = real(tmp_path, junk)
    (w2,) = g2.list_wabas()
    assert w2["error"] and TOKEN not in w2["error"]


def test_real_client_timeout_and_network_errors_map_to_plain_errors(tmp_path):
    def slow(_req):
        raise httpx.ReadTimeout("t", request=_req)

    def down(_req):
        raise httpx.ConnectError("c", request=_req)

    for h, text in ((slow, "did not answer in time"), (down, "Could not reach Meta")):
        g, seen = real(tmp_path, h)
        assert text in g.list_wabas()[0]["error"]
        assert len(seen) == 1                                          # one attempt: reads are not retried either


def test_real_client_writes_are_single_posts_with_the_right_shape_and_never_retried(tmp_path):
    def boom(_req):
        return httpx.Response(500, json={"error": {"code": 2}})

    g, seen = real(tmp_path, boom)
    with pytest.raises(GraphError):
        g.subscribe_app("111", "bakery")
    with pytest.raises(GraphError):
        g.start_sync("222", "history", "bakery")
    assert [(r.method, r.url.path) for r in seen] == [("POST", "/v27.0/111/subscribed_apps"),
                                                       ("POST", "/v27.0/222/smb_app_data")]
    assert json.loads(seen[1].content) == {"messaging_product": "whatsapp", "sync_type": "history"}

    def late(_req):
        raise httpx.ReadTimeout("t", request=_req)

    g2, seen2 = real(tmp_path, late)
    with pytest.raises(GraphError, match="may or may not have gone through"):
        g2.subscribe_app("111", "bakery")
    assert len(seen2) == 1


def test_real_client_write_guards_refuse_before_any_request(tmp_path):
    g, seen = real(tmp_path)
    for fn, args in ((g.subscribe_app, ("999", "bakery")), (g.subscribe_app, ("../x", "bakery")),
                     (g.start_sync, ("222", "delete", "bakery")), (g.start_sync, ("999", "history", "bakery")),
                     (g.subscribe_app, ("111", "")), (g.subscribe_app, ("111", "other")), (g.start_sync, ("222", "history", "other"))):
        with pytest.raises(GraphError):
            fn(*args)
    assert not seen


def test_exchange_code_needs_an_access_token_in_the_answer():
    http, _ = mock_http(lambda r: httpx.Response(200, json={"token_type": "bearer"}))
    with pytest.raises(GraphError):
        exchange_code("c", app_id="1", app_secret="s", client=http)


# ------------------------------------------------------------------ CLI wiring

@pytest.fixture
def seeded(tmp_path):
    db, users = str(tmp_path / "demo.db"), str(tmp_path / "users.json")
    assert cli.main(["seed", "--db", db, "--users-file", users]) == 0
    return db, users


def test_serve_refuses_a_public_bind_without_a_host_allowlist(seeded):
    db, users = seeded
    assert cli.main(["serve", "--db", db, "--users-file", users, "--host", "0.0.0.0", "--allow-public-bind"]) == 2


def test_serve_refuses_a_launcher_on_the_admin_host_name(seeded, tmp_path):
    db, users = seeded
    base = ["serve", "--db", db, "--users-file", users, "--allowed-host", "admin.example", "--trusted-proxy", "127.0.0.1/32"]
    same = base + ["--launcher-url", "https://admin.example", "--launcher-port", "8781"]
    assert cli.main(same) == 2
    assert cli.main(base + ["--launcher-url", "https://launch.example"]) == 2             # url without a port
    local = ["serve", "--db", db, "--users-file", users, "--launcher-url", "http://localhost:8781",
             "--launcher-port", "8781", "--graph", "real", "--app-id", "123", "--business-dir", str(tmp_path / "b")]
    ns = cli._parser().parse_args(local)
    hub = seed.build_demo_hub(db)
    try:
        app, launcher = cli._build(ns, hub, True)
    finally:
        hub.store.close()
    assert "localhost:8780" not in (app.cfg.allowed_hosts or ()) and "127.0.0.1:8780" in app.cfg.allowed_hosts
    assert launcher is not None and app.cfg.allow_meta_writes is False


def test_default_graph_is_fake_and_real_needs_an_app_id(seeded, tmp_path):
    db, users = seeded
    ns = cli._parser().parse_args(["serve", "--db", db, "--users-file", users])
    assert ns.graph == "fake" and ns.allow_meta_writes is False and ns.trusted_proxy == []
    hub = seed.build_demo_hub(db)
    try:
        assert isinstance(cli._build(ns, hub, True)[0].graph, FakeGraphClient)
        bad = cli._parser().parse_args(["serve", "--db", db, "--users-file", users, "--graph", "real"])
        with pytest.raises(ValueError, match="app-id"):
            cli._build(bad, hub, True)
    finally:
        hub.store.close()


def test_scrypt_cost_is_unchanged_in_production():
    assert auth.hash_password("x").split("$")[1] == str(2 ** 17)


def test_the_removed_signup_script_is_a_404_in_the_admin_app_not_a_500():
    app, _, _ = make_app()
    r = Client(app).get("/static/signup.js")
    assert r.status == 404


def test_serve_refuses_a_tunnel_without_trusted_proxy_and_forces_secure_cookies(seeded):
    db, users = seeded
    no_proxy = ["serve", "--db", db, "--users-file", users, "--allowed-host", "admin.example"]
    assert cli.main(no_proxy) == 2
    assert cli.main(["serve", "--db", db, "--users-file", users, "--launcher-url", "https://l.example",
                     "--launcher-port", "8781"]) == 2
    hub = seed.build_demo_hub(db)
    try:
        ns = cli._parser().parse_args(no_proxy + ["--trusted-proxy", "127.0.0.1/32"])
        app, _ = cli._build(ns, hub, True)
    finally:
        hub.store.close()
    assert app.cfg.cookie_secure is True and app.cfg.trusted_proxies == ("127.0.0.1/32",)


def test_slug_regex_does_not_accept_a_trailing_newline():
    from twin_agent.hub.admin.meta_store import valid_slug
    assert valid_slug("abc") and not valid_slug("abc\n") and not valid_slug("abc ")


def test_the_launcher_closes_the_http_client_it_creates(tmp_path, monkeypatch):
    from twin_agent.hub.admin import launcher as L
    closed = []

    class C(httpx.Client):
        def __exit__(self, *a):
            closed.append(1)
            return super().__exit__(*a)

    monkeypatch.setattr(L, "make_client", lambda t=0: C(transport=httpx.MockTransport(token_ok)))
    states, biz = SignupStates(), BusinessStore(tmp_path / "biz")
    app = LauncherApp(states=states, businesses=biz, app_id="123", config_id="456", graph_version="v27.0",
                      app_secret_file=secrets_file(tmp_path), allowed_hosts=("launch.example",))
    post_cb(app, {"kind": "code", "state": states.new("bakery"), "code": CODE})
    assert len(closed) == 2 and biz.get("bakery")["business_token"] == TOKEN        # exchange client + verify client


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX modes")
def test_token_dir_is_0700(tmp_path):
    BusinessStore(tmp_path / "biz").save("a", {"waba_id": "1"})
    assert (tmp_path / "biz").stat().st_mode & 0o777 == 0o700
