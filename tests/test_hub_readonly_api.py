"""The read-only hub API: auth, loopback-only, every endpoint, no PII, no writes. In-process (no sockets)."""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest
from admin_helpers import Client, make_app

from twin_agent.hub import production
from twin_agent.hub.readonly_api import ApiGate, Bucket, TokenFile, write_token
from twin_agent.hub import readonly_api_views as views

TOKEN = "t" * 43
ADMIN_HOST = "admin.example.com"


class Api:
    def __init__(self, gate):
        self.gate = gate

    def call(self, method="GET", path="/api/v1/health", *, token=TOKEN, env=None, headers=None):
        p, _, qs = path.partition("?")
        e = {"REQUEST_METHOD": method, "PATH_INFO": p, "QUERY_STRING": qs, "REMOTE_ADDR": "127.0.0.1",
             "HTTP_HOST": "127.0.0.1:8891", "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")}
        if token is not None:
            e["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        e.update(env or {})
        e.update(headers or {})
        out = {}

        def start(status, hdrs):
            out["status"], out["h"] = int(status.split()[0]), dict(hdrs)

        out["raw"] = b"".join(self.gate(e, start)).decode()
        try:
            out["json"] = json.loads(out["raw"])
        except ValueError:
            out["json"] = None
        return type("R", (), out)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)


@pytest.fixture
def world(tmp_path):
    app, hub, info = make_app()
    tf = tmp_path / "token.txt"
    tf.write_text(TOKEN, encoding="utf-8")
    gate = ApiGate(app, hub, app.cfg, admin_host=ADMIN_HOST, token_file=tf, health=lambda: {"ok": True, "accepting": True,
                                                                                            "uptime_s": 5, "open_holds": 0})
    return Api(gate), hub, app, tf


# ------------------------------------------------------------------ token file + CLI

def test_write_token_is_random_private_and_prints_nothing(tmp_path, capsys):
    p = tmp_path / "sub" / "tok.txt"
    write_token(p)
    write_token(tmp_path / "other.txt")
    a = p.read_text()
    assert len(a) >= 43 and a != (tmp_path / "other.txt").read_text()
    if os.name != "nt":
        assert (p.stat().st_mode & 0o777) == 0o600
    assert capsys.readouterr().out == ""


def test_cli_api_token_command_prints_nothing_and_writes_the_default_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(production, "write_token", lambda: write_token(tmp_path / "t.txt"))
    assert production.main(["api-token"]) == 0
    assert capsys.readouterr().out == "" and len((tmp_path / "t.txt").read_text()) >= 43


def test_default_token_path_is_the_secrets_file():
    from twin_agent.hub.readonly_api import DEFAULT_TOKEN_FILE
    assert DEFAULT_TOKEN_FILE.name == "aa-prod-api-token.txt" and DEFAULT_TOKEN_FILE.parent.name == "secrets"


def test_missing_or_short_token_file_turns_the_api_off(world):
    api, _, _, tf = world
    tf.write_text("short")
    assert api.get("/api/v1/health", token="short").status == 404
    tf.unlink()
    assert api.get("/api/v1/health").status == 404


def test_rotating_the_token_takes_effect_without_restart(world):
    api, _, _, tf = world
    assert api.get("/api/v1/health").status == 200
    tf.write_text("n" * 50)
    os.utime(tf, ns=(1, 2_000_000_000_000_000_000))
    assert api.get("/api/v1/health").status == 401
    assert api.get("/api/v1/health", token="n" * 50).status == 200


# ------------------------------------------------------------------ auth

def test_missing_and_wrong_token_are_401(world):
    api = world[0]
    assert api.get("/api/v1/tickets", token=None).status == 401
    for bad in ("x" * 43, "", TOKEN + "x", TOKEN[:-1]):
        r = api.get("/api/v1/tickets", token=bad)
        assert r.status == 401 and r.json == {"error": "unauthorized"} and r.h["WWW-Authenticate"] == "Bearer"
    assert api.get("/api/v1/tickets", token=None, headers={"HTTP_AUTHORIZATION": f"Basic {TOKEN}"}).status == 401


def test_token_is_compared_in_constant_time():
    src = (Path(views.__file__).parent / "readonly_api.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest(given, tok)" in src


def test_the_admin_cookie_never_opens_the_api(world):
    api, hub, app, _ = world
    c = Client(app)
    assert c.login().status == 303
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    assert api.get("/api/v1/tickets", token=None, headers={"HTTP_COOKIE": cookie}).status == 401


def test_the_admin_pages_never_accept_the_token(world):
    api, *_ = world
    r = api.get("/tickets", headers={"HTTP_HOST": "127.0.0.1:8780"})
    assert r.status == 303 and r.h["Location"] == "/login"
    r = api.get("/tickets", token=None, headers={"HTTP_HOST": "127.0.0.1:8780", "HTTP_X_API_KEY": TOKEN})
    assert r.status == 303


def test_the_admin_still_works_through_the_gate(world):
    api, _, app, _ = world
    c = Client(app)
    assert c.login().status == 303 and c.get("/tickets").status == 200


# ------------------------------------------------------------------ loopback only

@pytest.mark.parametrize("env", [
    {"HTTP_CF_CONNECTING_IP": "203.0.113.9"}, {"HTTP_CF_RAY": "abc-CAI"}, {"HTTP_X_FORWARDED_FOR": "203.0.113.9"},
    {"HTTP_HOST": ADMIN_HOST}, {"HTTP_HOST": "app.example.com"}, {"HTTP_HOST": ""}, {"HTTP_HOST": "127.0.0.1.evil.com"},
    {"REMOTE_ADDR": "10.0.0.5"}, {"REMOTE_ADDR": "203.0.113.9"}])
def test_tunnel_or_non_loopback_gets_404_even_with_the_right_token(world, env):
    api = world[0]
    for path in ("/api/v1/tickets", "/api/v1/health", "/api", "/api/nothing"):
        r = api.get(path, env=env)
        assert r.status == 404 and r.json == {"error": "not_found"}


def test_loopback_host_forms(world):
    api = world[0]
    for host in ("127.0.0.1:8891", "localhost:8891", "localhost", "127.0.0.1"):
        assert api.get("/api/v1/health", env={"HTTP_HOST": host}).status == 200
    assert api.get("/api/v1/health", env={"REMOTE_ADDR": "::1"}).status == 200


# ------------------------------------------------------------------ read-only

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_every_write_method_is_405(world, method):
    api = world[0]
    for path in ("/api/v1/tickets", "/api/v1/tickets/20001", "/api/v1/approvals/pending", "/api/v1/health"):
        r = api.call(method, path)
        assert r.status == 405 and r.h["Allow"] == "GET"


def test_a_write_attempt_changes_nothing(world):
    api, hub, *_ = world
    before = json.dumps([hub.store.scan(ns) for ns in ("tickets", "approvals")], sort_keys=True, default=str)
    for m in ("POST", "PUT", "DELETE"):
        api.call(m, "/api/v1/approvals/pending")
    assert json.dumps([hub.store.scan(ns) for ns in ("tickets", "approvals")], sort_keys=True, default=str) == before


def test_no_write_verbs_in_the_api_source():
    src = (Path(views.__file__).parent / "readonly_api_views.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(put|update|delete|on_owner_decision|mark_hold_done)\(", src)


# ------------------------------------------------------------------ endpoints

LIST_KEYS = {"id", "odoo_ticket_id", "created_at", "updated_at", "customer", "owner", "stage", "status", "waiting_on",
             "has_pending_approval", "channel", "summary", "lifecycle", "lifecycle_at", "acked", "approved_by", "last_delivery"}


def test_health_is_the_healthz_content(world):
    r = world[0].get("/api/v1/health")
    assert r.status == 200 and r.json == {"ok": True, "accepting": True, "uptime_s": 5, "open_holds": 0}


def test_health_without_a_server_is_503(world):
    api, hub, app, tf = world
    bare = Api(ApiGate(app, hub, app.cfg, admin_host=ADMIN_HOST, token_file=tf))
    assert bare.get("/api/v1/health").status == 503
    bare.gate.set_health(lambda: {"ok": True})
    assert bare.get("/api/v1/health").json == {"ok": True}


def test_ticket_list_shape_and_open_filter(world):
    api, hub, *_ = world
    r = api.get("/api/v1/tickets?status=all&limit=200")
    assert r.status == 200 and r.json["status"] == "all" and r.json["count"] == len(hub.all_tickets())
    for t in r.json["tickets"]:
        assert set(t) == LIST_KEYS and t["id"] == t["odoo_ticket_id"]
        assert t["waiting_on"] in ("approval", "customer", "owner", None)
        assert t["waiting_on"] == "approval" if t["has_pending_approval"] else True
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+\+00:00", t["created_at"])
    ids = [t["id"] for t in r.json["tickets"]]
    assert ids == sorted(ids, reverse=True)
    open_ = api.get("/api/v1/tickets").json
    assert open_["status"] == "open" and open_["limit"] == 50
    assert all(t["stage"] != "resolved" or t["lifecycle"] == "closed" for t in open_["tickets"])
    assert open_["count"] <= r.json["count"]


def test_limit_and_bad_queries(world):
    api = world[0]
    assert api.get("/api/v1/tickets?status=all&limit=2").json["count"] == 2
    for q in ("limit=0", "limit=201", "limit=abc", "limit=-1", "status=closed"):
        assert api.get(f"/api/v1/tickets?{q}").status == 400


def test_pending_approvals_have_a_deep_link_and_no_nonce(world):
    api, hub, *_ = world
    r = api.get("/api/v1/approvals/pending")
    assert r.status == 200 and r.json["count"] == len(r.json["approvals"]) > 0
    for a in r.json["approvals"]:
        assert a["approve_url"] == f"https://{ADMIN_HOST}/tickets/{a['ticket_id']}"
        assert set(a) == {"ticket_id", "kind", "kind_label", "client_facing", "fingerprint", "issued_at", "expires_at",
                          "approvers", "approve_url", "customer", "owner", "summary"}
        doc = hub.store.get("approvals", hub.ticket(a["ticket_id"]).pending_nonce)
        assert a["fingerprint"] == doc["draft_hash"]
    assert not any(h.pending_nonce in r.raw for h in hub.all_tickets() if h.pending_nonce)
    assert all("draft_text" not in a for a in r.json["approvals"])


def test_ticket_detail_with_a_pending_approval(world):
    api, hub, *_ = world
    tid = next(t.tid for t in hub.all_tickets() if t.pending_nonce)
    r = api.get(f"/api/v1/tickets/{tid}")
    t = r.json["ticket"]
    assert r.status == 200 and set(t) == LIST_KEYS | {"pending_approval", "timeline"} and t["has_pending_approval"]
    pa = t["pending_approval"]
    assert set(pa) == {"ticket_id", "kind", "kind_label", "client_facing", "fingerprint", "issued_at", "expires_at",
                       "approvers", "approve_url", "draft_text"}
    doc = hub.store.get("approvals", hub.ticket(tid).pending_nonce)
    assert pa["fingerprint"] == doc["draft_hash"] and pa["draft_text"] and pa["expires_at"].endswith("+00:00")
    assert t["timeline"] and t["timeline"][0]["type"] == "customer_message_received"
    for e in t["timeline"]:
        assert set(e) == {"type", "actor", "at", "label"}


def test_ticket_detail_without_approval_and_timeline_after_approval(world):
    api, hub, *_ = world
    done = next(t for t in hub.all_tickets() if not t.pending_nonce)
    t = api.get(f"/api/v1/tickets/{done.tid}").json["ticket"]
    assert t["pending_approval"] is None and not t["has_pending_approval"]
    types = {e["type"] for e in t["timeline"]}
    assert "draft_created" in types or "customer_message_received" in types


def test_unknown_and_malformed_ticket_ids_are_404(world):
    api = world[0]
    for p in ("/api/v1/tickets/999999", "/api/v1/tickets/abc", "/api/v1/tickets/-1", "/api/v1/tickets/1.5",
              "/api/v1/tickets/", "/api/v1/nope", "/api/v2/tickets"):
        assert api.get(p).status == 404


def test_server_errors_do_not_leak(world):
    api, hub, *_ = world
    hub.store.scan = lambda ns: (_ for _ in ()).throw(RuntimeError("secret detail"))
    r = api.get("/api/v1/tickets")
    assert r.status == 500 and r.json == {"error": "internal_error"} and "secret" not in r.raw


# ------------------------------------------------------------------ no PII

def test_no_phone_email_wamid_or_client_text_in_any_response(world):
    api, hub, *_ = world
    st = hub.all_tickets()[0]
    st.inbound.name = "Mia +20 100 111 2233 mia@example.com"
    st.inbound.text = "Please call me about invoice SECRETWORD-77"
    hub.store.put("tickets", st.tid, st.to_dict())
    bodies = [api.get("/api/v1/tickets?status=all&limit=200").raw, api.get("/api/v1/approvals/pending").raw]
    bodies += [api.get(f"/api/v1/tickets/{t.tid}").raw for t in hub.all_tickets()]
    blob = "\n".join(bodies)
    assert "@" not in blob and "wamid" not in blob.lower() and "SECRETWORD" not in blob
    assert "2233" not in blob and "100 111" not in blob
    for t in hub.all_tickets():
        assert t.wa_id not in blob and t.inbound.message_id not in blob
        assert not t.pending_nonce or t.pending_nonce not in blob
    plain = re.sub(r'"fingerprint":"[0-9a-f]+"', "", blob)                 # sha256 hex is not a phone number
    plain = re.sub(r"\d{4}-\d\d-\d\dT[\d:.+]+", "", plain)
    assert not re.search(r"(?<![\w])\+?\d{7,}(?![\w])", plain)


def test_customer_name_is_scrubbed_short_and_never_empty(world):
    _, hub, *_ = world
    st = hub.all_tickets()[0]
    cases = {"Mia +20 100 111 2233": "Mia", "+201001112233": "Customer", "a@b.example.org": "Customer", "": "Customer",
             "x" * 90: "x" * 40, "Oscar  Leo": "Oscar Leo"}
    for raw, want in cases.items():
        st.inbound.name = raw
        assert views.display_name(st) == want


def test_summary_is_structured_fields_only(world):
    _, hub, *_ = world
    st = hub.all_tickets()[0]
    st.analysis.summary = "CLIENT WORDS"
    st.analysis.module = "free text from the client"
    s = views.safe_summary(st)
    assert "CLIENT WORDS" not in s and "free text" not in s and "module Other" in s


def test_draft_text_is_scrubbed(world):
    _, hub, *_ = world
    out = views.scrub("Call +20 100 111 2233 or x@y.example.org about wamid.HBgLMjAx")
    assert "2233" not in out and "@" not in out and "wamid.H" not in out


# ------------------------------------------------------------------ rate limit

def _limited(world, bucket):
    _, hub, app, tf = world
    return Api(ApiGate(app, hub, app.cfg, admin_host=ADMIN_HOST, token_file=tf, health=lambda: {"ok": True}, bucket=bucket))


def test_rate_limit_is_20_per_second_with_a_burst_of_30(world):
    t = [100.0]
    limited = _limited(world, Bucket(clock=lambda: t[0]))
    codes = [limited.get("/api/v1/health").status for _ in range(34)]
    assert codes[:30] == [200] * 30 and codes[30:] == [429] * 4
    r = limited.get("/api/v1/health")
    assert r.status == 429 and r.h["Retry-After"] == "1"
    t[0] += 1.0
    assert sum(limited.get("/api/v1/health").status == 200 for _ in range(25)) == 20


def test_calls_without_the_token_never_use_up_the_bucket(world):
    t = [100.0]
    limited = _limited(world, Bucket(clock=lambda: t[0]))
    assert {limited.get("/api/v1/health", token="wrong").status for _ in range(100)} == {401}
    assert {limited.get("/api/v1/health", token=None).status for _ in range(100)} == {401}
    assert limited.get("/api/v1/health").status == 200


def test_the_bucket_refills_at_the_rate():
    t = [0.0]
    b = Bucket(10, 10, clock=lambda: t[0])
    assert sum(b.take() for _ in range(20)) == 10
    t[0] += 0.5
    assert sum(b.take() for _ in range(20)) == 5


def test_token_file_class_reads_changes(tmp_path):
    f = tmp_path / "t"
    tf = TokenFile(f)
    assert tf.value() == b""
    f.write_text("z" * 40)
    assert tf.value() == b"z" * 40


# ------------------------------------------------------------------ review round 2

def test_scrub_removes_phones_but_keeps_dates_amounts_versions_and_ids():
    out = views.scrub("Call +20 100 111 2233, or 01001112233, or 0100-111-2233, or +971 4 123 4567.")
    assert not re.search(r"\d{4}", out.replace("[number removed]", ""))
    keep = "On 2026-11-16 we billed 1,500.00 EGP and 12.50, upgrade 17.0.1.2 at 10:30, ticket #20004, ref IN/10001/26, 2026-11-16 10:30."
    assert views.scrub(keep) == keep
    assert views.scrub("number 2026111612345 here") == "number [number removed] here"     # 13 digits
    assert views.scrub("see 123456789") == "see 123456789"                                   # 9 digits: not a phone


def test_atomic_token_write_leaves_no_temp_file(tmp_path):
    for _ in range(3):
        write_token(tmp_path / "t.txt")
    assert [p.name for p in tmp_path.iterdir()] == ["t.txt"]


def test_approve_url_is_null_when_the_admin_is_not_public(world):
    _, hub, app, tf = world
    off = Api(ApiGate(app, hub, app.cfg, admin_host="", token_file=tf))
    a = off.get("/api/v1/approvals/pending").json["approvals"]
    assert a and all(x["approve_url"] is None for x in a)
    tid = a[0]["ticket_id"]
    assert off.get(f"/api/v1/tickets/{tid}").json["ticket"]["pending_approval"]["approve_url"] is None


def test_an_internal_owner_tap_is_waiting_on_owner_not_approval(world):
    api, hub, *_ = world
    st = next(t for t in hub.all_tickets() if t.pending_nonce)
    hub.store.update("approvals", st.pending_nonce, lambda d: {**d, "kind": "fix_ready"})
    t = api.get(f"/api/v1/tickets/{st.tid}").json["ticket"]
    assert t["waiting_on"] == "owner" and t["has_pending_approval"] and t["pending_approval"]["client_facing"] is False


def test_one_bad_record_is_skipped_not_a_500(world, caplog):
    api, hub, *_ = world
    good = len(api.get("/api/v1/tickets?status=all&limit=200").json["tickets"])
    victim = hub.all_tickets()[0]
    hub.store.put("tickets", 77777, {"tid": 77777, "garbage": True})
    hub.store.put("approvals", "bad", {"nonce": "bad", "ticket_id": victim.tid})          # no kind / issued_at
    with caplog.at_level("WARNING", logger="twin_agent.hub"):
        r = api.get("/api/v1/tickets?status=all&limit=200")
        a = api.get("/api/v1/approvals/pending")
    assert r.status == 200 and a.status == 200 and len(r.json["tickets"]) >= good - 1
    assert "77777" in caplog.text


def test_naive_and_aware_timestamps_do_not_break_updated_at(world):
    api, hub, *_ = world
    st = hub.all_tickets()[0]
    st.last_inbound_at = "2026-01-01T00:00:00"          # naive next to aware stamps
    hub.store.put("tickets", st.tid, st.to_dict())
    assert api.get(f"/api/v1/tickets/{st.tid}").status == 200


def test_through_the_real_production_wrapper_chain(tmp_path, monkeypatch):
    """ApiGate must stay OUTSIDE the Cloudflare Access gate: a tunnel request is 404 (not 403), a local one is served."""
    from test_hub_production import SEC, build, make_cfg
    from test_hub_production_round2 import ACCESS_USERS, _call, _users_file, token, verifier
    from twin_agent.hub import readonly_api
    tf = tmp_path / "tok"
    tf.write_text(TOKEN)
    monkeypatch.setattr(readonly_api, "DEFAULT_TOKEN_FILE", tf)
    cfg = make_cfg(public_admin=True, access_users=ACCESS_USERS)
    hub, _ = build(cfg)
    app = production.build_admin(cfg, SEC, hub, dry_run=True, users_file=_users_file(tmp_path, ACCESS_USERS.values()),
                                 verifier=verifier())
    assert isinstance(app, ApiGate)
    bearer = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
    tunnel = {"HTTP_CF_CONNECTING_IP": "203.0.113.9", "HTTP_CF_ACCESS_JWT_ASSERTION": token(), **bearer}
    assert _call(app, path="/api/v1/tickets", host="admin.example.com", headers=tunnel)[0] == 404
    assert _call(app, path="/api/v1/tickets", host="127.0.0.1:8891", headers={"HTTP_CF_CONNECTING_IP": "1.2.3.4", **bearer})[0] == 404
    code, body, _ = _call(app, path="/api/v1/tickets", host="127.0.0.1:8891", headers=bearer)
    assert code == 200 and json.loads(body)["status"] == "open"
    assert _call(app, path="/api/v1/tickets", host="127.0.0.1:8891")[0] == 401
    assert _call(app, path="/tickets", host="admin.example.com", headers={"HTTP_CF_CONNECTING_IP": "1.2.3.4"})[0] == 403   # admin still gated
