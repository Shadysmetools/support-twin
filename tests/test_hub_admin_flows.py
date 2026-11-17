"""Embedded Signup flows (code review + security review of PR #65): cross-tenant binding (B1), a one-time token that
must not be lost (code first, cancel then retry, races), phone binding, the verify_assets decision table, log hygiene and
the low-severity fixes. Fakes and ``httpx.MockTransport`` only: no network, no real secrets."""
from __future__ import annotations

import json
import logging
import socket
import threading
import traceback
from urllib.parse import parse_qs

import httpx
import pytest
from admin_helpers import Client, make_app
from test_hub_admin_graph import (
    APP_SECRET,
    CODE,
    GRANTS,
    CODE_TOKENS,
    TOKEN,
    graph_handler,
    make_launcher,
    mock_http,
    post_cb,
    real,
    secrets_file,
    wsgi,
)

from twin_agent.hub.admin import __main__ as cli
from twin_agent.hub.admin.graph import AmbiguousAssets, FakeGraphClient, GraphError, LocalRefusal, MetaRefused
from twin_agent.hub.admin.graph_real import RealGraphClient, verify_assets
from twin_agent.hub.admin.launcher import LauncherApp
from twin_agent.hub.admin.meta_store import BusinessStore, SignupStates


@pytest.fixture(autouse=True)
def _reset_grants():
    GRANTS.clear()
    GRANTS[TOKEN] = {"waba": "111", "phones": ["222"]}
    CODE_TOKENS.clear()


# ------------------------------------------------------------------ B1: cross-tenant (security review of #65)

def attack_setup(tmp_path):
    """bbb connects legitimately (waba 222, phone 333, TOKEN_B). aaa then claims bbb's ids from its own launcher link."""
    tok_a, tok_b = "TOKEN-A-SENTINEL", "TOKEN-B-SENTINEL"
    GRANTS.clear()
    GRANTS.update({TOKEN: {"waba": "111", "phones": ["222"]}, tok_b: {"waba": "222", "phones": ["333"]},
                   tok_a: {"waba": "111", "phones": ["444"]}})
    CODE_TOKENS.clear()
    CODE_TOKENS.update({"code-a": tok_a, "code-b": tok_b})
    app, states, biz, seen = make_launcher(tmp_path)
    sb = states.new("bbb")
    post_cb(app, {"kind": "session", "state": sb, "event": "FINISH", "waba_id": "222", "phone_number_id": "333"})
    assert post_cb(app, {"kind": "code", "state": sb, "code": "code-b"}).status == 200
    return app, states, biz, tok_a, tok_b


def test_b1_a_business_cannot_claim_another_businesss_ids(tmp_path):
    app, states, biz, tok_a, tok_b = attack_setup(tmp_path)
    sa = states.new("aaa")
    post_cb(app, {"kind": "session", "state": sa, "event": "FINISH", "waba_id": "222", "phone_number_id": "333"})
    assert post_cb(app, {"kind": "code", "state": sa, "code": "code-a"}).status == 200
    a, b = biz.get("aaa"), biz.get("bbb")
    assert a["waba_id"] == "111" and a["phone_number_id"] == "444"        # only what A's token really grants
    assert a["claimed_waba_id"] == "222"                                   # the lie is kept as display-only
    assert b["waba_id"] == "222" and b["business_token"] == tok_b          # B untouched
    assert biz.token_by_id(waba_id="222") == tok_b and biz.token_by_id(phone_number_id="333") == tok_b
    # B's write goes out with B's token, by B's slug
    http, seen = mock_http(lambda r: httpx.Response(200, json={"success": True}))
    RealGraphClient(biz, app_id="123", client=http).subscribe_app("222", "bbb")
    assert seen[0].headers["Authorization"] == f"Bearer {tok_b}"
    with pytest.raises(GraphError):                                         # and A's slug cannot be used for B's ids
        RealGraphClient(biz, app_id="123", client=http).subscribe_app("222", "aaa")
    assert len(seen) == 1


def test_b1_if_a_token_really_grants_a_waba_another_business_holds_it_is_refused(tmp_path):
    app, states, biz, tok_a, tok_b = attack_setup(tmp_path)
    GRANTS[tok_a] = {"waba": "222", "phones": ["333"]}                    # Meta says A's token grants B's WABA too
    sa = states.new("aaa")
    r = post_cb(app, {"kind": "code", "state": sa, "code": "code-a"})
    assert r.status == 409 and "another business" in r.body
    assert "business_token" not in biz.get("aaa") and biz.get("bbb")["business_token"] == tok_b


def test_b1_a_token_meta_calls_invalid_stores_nothing(tmp_path):
    app, states, biz, tok_a, _ = attack_setup(tmp_path)
    GRANTS.pop(tok_a)
    assert post_cb(app, {"kind": "code", "state": states.new("aaa"), "code": "code-a"}).status == 400
    assert "business_token" not in biz.get("aaa")


def test_b1_session_after_code_binds_too(tmp_path):
    app, states, biz, _, _ = attack_setup(tmp_path)
    s = states.new("aaa")
    assert post_cb(app, {"kind": "code", "state": s, "code": "code-a"}).status == 200      # code first, no claim yet
    assert biz.get("aaa")["waba_id"] == "111"                                              # the sole granted WABA
    post_cb(app, {"kind": "session", "state": s, "waba_id": "222", "phone_number_id": "333"})
    assert biz.get("aaa")["waba_id"] == "111"                                              # a late lie changes nothing


def test_b1_store_refuses_collisions_and_token_by_id_errors_on_zero_or_many(tmp_path):
    from twin_agent.hub.admin.meta_store import ConflictError
    biz = BusinessStore(tmp_path / "biz")
    biz.save("aaa", {"waba_id": "1", "phone_number_id": "2", "business_token": "A"})
    with pytest.raises(ConflictError):
        biz.save("bbb", {"waba_id": "1", "business_token": "B"})
    with pytest.raises(ConflictError):
        biz.save("bbb", {"phone_number_id": "2"})
    assert biz.get("bbb") == {}                                                            # nothing was written
    with pytest.raises(LookupError):
        biz.token_by_id(waba_id="9")                                                       # 0 matches
    (tmp_path / "biz" / "bbb.json").write_text(json.dumps({"waba_id": "1", "business_token": "B"}))   # forced duplicate
    with pytest.raises(LookupError):
        biz.token_by_id(waba_id="1")                                                       # >1 matches: no "first"


def test_b1_one_failing_business_does_not_blank_the_list_or_the_pages(tmp_path):
    biz = BusinessStore(tmp_path / "biz")
    biz.save("bad", {"waba_id": "999", "business_token": "TOKEN-BAD"})
    biz.save("good", {"waba_id": "111", "phone_number_id": "222", "business_token": TOKEN})

    def h(req):
        if req.headers["Authorization"] == "Bearer TOKEN-BAD":
            return httpx.Response(400, json={"error": {"code": 200}})
        return graph_handler(req)

    http, _ = mock_http(h)
    g = RealGraphClient(biz, app_id="123", client=http)
    out = {w["business"]: w for w in g.list_wabas()}
    assert out["good"]["name"] == "Demo Bakery" and "HTTP 400" in out["bad"]["error"] and out["bad"]["phone_numbers"] == []
    app, _, _ = make_app(graph=g, businesses=biz, signups=SignupStates())
    c = Client(app)
    c.login()
    nums, tpl = c.get("/numbers").body, c.get("/templates").body
    assert "Demo Bakery" in nums and "Could not read this business" in nums
    assert "order_update" in tpl and "HTTP 400" in tpl and "TOKEN-BAD" not in nums + tpl


# ------------------------------------------------------------------ a richer fake Meta (multi-WABA / multi-number / bad tokens)

class Meta:
    """token -> {mgmt: [waba ids] | None, msg: same default, valid, app_id, phones: {waba: [ids]}, phone_null}."""

    def __init__(self):
        self.tokens: dict = {}
        self.codes: dict = {}
        self.seen: list = []
        self.write_answer: dict = {"success": True}

    def __call__(self, req):
        self.seen.append(req)
        p = req.url.path
        if p.endswith("/oauth/access_token"):
            code = parse_qs(req.content.decode())["code"][0]
            return httpx.Response(200, json={"access_token": self.codes[code], "token_type": "bearer"})
        if p.endswith("/debug_token"):
            return self._debug(self.tokens.get(req.url.params["input_token"]))
        if p.endswith("/phone_numbers"):
            t = self.tokens.get(req.headers["Authorization"].split()[1], {})
            if t.get("phone_null"):
                return httpx.Response(200, json={"data": None})
            return httpx.Response(200, json={"data": [{"id": x} for x in t.get("phones", {}).get(p.split("/")[-2], [])]})
        if p.endswith("/subscribed_apps") and req.method == "GET":
            return httpx.Response(200, json={"data": []})
        if p.count("/") == 2 and p.rsplit("/", 1)[-1].isdigit():
            return httpx.Response(200, json={"id": p.rsplit("/", 1)[-1], "name": "Initech"})
        if p.endswith("/subscribed_apps") and req.method == "POST" or p.endswith("/smb_app_data"):
            return httpx.Response(200, json=self.write_answer)
        return httpx.Response(404, json={})

    @staticmethod
    def _debug(t):
        if t is None or t.get("valid") is False:
            return httpx.Response(200, json={"data": {"is_valid": False, "app_id": "123"}})
        d = {"is_valid": True, "app_id": t.get("app_id", "123")}
        mgmt = t.get("mgmt")
        if mgmt is not None:
            msg = t.get("msg", mgmt)
            d["granular_scopes"] = [
                {"scope": "whatsapp_business_management", **({} if t.get("no_targets") else {"target_ids": mgmt})},
                {"scope": "whatsapp_business_messaging", "target_ids": msg}]
        return httpx.Response(200, json={"data": d})


def launcher_world(tmp_path, **kw):
    meta = Meta()
    app, states, biz, _ = make_launcher(tmp_path, meta, **kw)
    return app, states, biz, meta


def two_waba_token(meta, tok="tok-a", code="code-a"):
    meta.tokens[tok] = {"mgmt": ["10", "20"], "phones": {"10": ["11"], "20": ["21", "22"]}}
    meta.codes[code] = tok
    return tok


# ------------------------------------------------------------------ CR-M1: the one-time token must not be lost

def test_p1_code_first_with_two_wabas_keeps_the_token_in_memory_then_binds_on_the_session(tmp_path):
    app, states, biz, meta = launcher_world(tmp_path)
    tok = two_waba_token(meta)
    s = states.new("initech")
    r1 = post_cb(app, {"kind": "code", "state": s, "code": "code-a"})
    assert r1.status == 200 and "Almost done" in r1.body and tok not in r1.body
    assert biz.get("initech").get("business_token") is None                    # never written to disk unverified
    assert states.token(s) == tok                                           # kept in memory on the state
    r2 = post_cb(app, {"kind": "session", "state": s, "event": "FINISH", "waba_id": "20", "phone_number_id": "21"})
    assert r2.status == 200 and "Connected" in r2.body
    rec = biz.get("initech")
    assert (rec["waba_id"], rec["phone_number_id"], rec["business_token"]) == ("20", "21", tok)
    assert states.token(s) is None                                          # gone from memory once bound


def test_p2_cancel_then_retry_on_the_same_page_works(tmp_path):
    app, states, biz, meta = launcher_world(tmp_path)
    tok = two_waba_token(meta)
    s = states.new("initech")
    assert post_cb(app, {"kind": "session", "state": s, "event": "CANCEL", "current_step": "PHONE_NUMBER_SETUP"}).status == 200
    assert biz.get("initech")["cancel_step"] == "PHONE_NUMBER_SETUP"
    assert post_cb(app, {"kind": "session", "state": s, "event": "FINISH", "waba_id": "20",
                         "phone_number_id": "22"}).status == 200
    assert post_cb(app, {"kind": "code", "state": s, "code": "code-a"}).status == 200
    rec = biz.get("initech")
    assert (rec["waba_id"], rec["phone_number_id"], rec["business_token"]) == ("20", "22", tok)
    assert "cancel_step" not in rec                                          # the stale cancel was cleared by the retry
    assert post_cb(app, {"kind": "session", "state": s, "event": "FINISH"}).status == 403   # the state is done


def test_the_code_is_still_single_use_and_session_posts_are_capped(tmp_path):
    app, states, biz, meta = launcher_world(tmp_path)
    two_waba_token(meta)
    s = states.new("initech")
    assert post_cb(app, {"kind": "code", "state": s, "code": "code-a"}).status == 200
    assert post_cb(app, {"kind": "code", "state": s, "code": "code-a"}).status == 403
    codes = [post_cb(app, {"kind": "session", "state": s, "event": "CANCEL"}).status
             for _ in range(SignupStates.MAX_SESSION + 2)]
    assert codes[0] == 200 and codes[-1] == 403


def test_a_pending_token_dies_with_its_state(tmp_path):
    now = [1000.0]
    meta = Meta()
    states = SignupStates(ttl=60, clock=lambda: now[0])
    http, _ = mock_http(meta)
    app = LauncherApp(states=states, businesses=BusinessStore(tmp_path / "biz"), app_id="123", config_id="456",
                      graph_version="v27.0", app_secret_file=secrets_file(tmp_path), allowed_hosts=("launch.example",),
                      http=http)
    two_waba_token(meta)
    s = states.new("initech")
    post_cb(app, {"kind": "code", "state": s, "code": "code-a"})
    assert states.token(s)
    now[0] += 61
    assert states.token(s) is None
    assert post_cb(app, {"kind": "session", "state": s, "event": "FINISH", "waba_id": "20"}).status == 403


def test_code_and_session_racing_never_lose_the_token(tmp_path):
    for i in range(15):
        d = tmp_path / f"r{i}"
        d.mkdir()
        app, states, biz, meta = launcher_world(d)
        tok = two_waba_token(meta)
        s = states.new("race")
        out = {}

        def send_code():
            out["c"] = post_cb(app, {"kind": "code", "state": s, "code": "code-a"})

        def send_session():
            out["s"] = post_cb(app, {"kind": "session", "state": s, "event": "FINISH", "waba_id": "20",
                                     "phone_number_id": "21"})

        ts = [threading.Thread(target=send_code), threading.Thread(target=send_session)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        rec = biz.get("race")
        assert (rec.get("waba_id"), rec.get("phone_number_id"), rec.get("business_token")) == ("20", "21", tok)


# ------------------------------------------------------------------ CR-M2: phone binding

def test_an_unverifiable_number_is_saved_as_empty_and_a_late_claim_settles_it(tmp_path):
    app, states, biz, meta = launcher_world(tmp_path)
    meta.tokens["tok-a"] = {"mgmt": ["10"], "phones": {"10": ["11", "12"]}}
    meta.codes["code-a"] = "tok-a"
    s = states.new("initech")
    r = post_cb(app, {"kind": "code", "state": s, "code": "code-a"})
    assert "pick the number" in r.body
    assert biz.get("initech")["waba_id"] == "10" and biz.get("initech")["phone_number_id"] == ""
    assert states.token(s) == "tok-a"                                       # still waiting for a claim
    post_cb(app, {"kind": "session", "state": s, "event": "FINISH", "waba_id": "10", "phone_number_id": "12"})
    assert biz.get("initech")["phone_number_id"] == "12" and states.token(s) is None


def test_reconnecting_to_another_waba_clears_the_old_number(tmp_path):
    app, states, biz, meta = launcher_world(tmp_path)
    meta.tokens["tok-1"] = {"mgmt": ["10"], "phones": {"10": ["11"]}}
    meta.tokens["tok-2"] = {"mgmt": ["30"], "phones": {"30": ["31", "32"]}}
    meta.codes.update({"c1": "tok-1", "c2": "tok-2"})
    post_cb(app, {"kind": "code", "state": states.new("initech"), "code": "c1"})
    assert biz.get("initech")["phone_number_id"] == "11"
    post_cb(app, {"kind": "code", "state": states.new("initech"), "code": "c2"})
    rec = biz.get("initech")
    assert (rec["waba_id"], rec["phone_number_id"], rec["business_token"]) == ("30", "", "tok-2")
    assert biz.token_for("initech", phone_number_id="11") == ""               # the old number no longer routes anything


def test_connect_start_for_an_existing_business_drops_the_old_claims(tmp_path):
    biz = BusinessStore(tmp_path / "biz")
    biz.save("initech", {"claimed_waba_id": "9", "cancel_step": "X", "error_code": "1", "waba_id": "10",
                      "business_token": "T"})
    app, _, _ = make_app(cfg={"launcher_url": "https://launch.example"}, signups=SignupStates(), businesses=biz)
    c = Client(app)
    c.login()
    c.post("/connect/start", {"csrf": c.csrf("/connect"), "business": "initech"})
    rec = biz.get("initech")
    assert not {"claimed_waba_id", "cancel_step", "error_code"} & set(rec) and rec["waba_id"] == "10"


def numbers_app(tmp_path, handler, **cfg):
    biz = BusinessStore(tmp_path / "biz")
    http, _ = mock_http(handler)
    g = RealGraphClient(biz, app_id="123", client=http)
    app, _, _ = make_app(graph=g, businesses=biz, signups=SignupStates(), cfg=cfg)
    c = Client(app)
    c.login()
    return c, biz


def test_history_sync_works_for_a_multi_number_account_once_a_number_is_picked(tmp_path):
    meta = Meta()
    meta.tokens["tok-a"] = {"mgmt": ["10"], "phones": {"10": ["11", "12"]}}
    c, biz = numbers_app(tmp_path, meta, allow_meta_writes=True)
    biz.save("initech", {"waba_id": "10", "phone_number_id": "", "business_token": "tok-a"})
    page = c.get("/numbers").body
    assert page.count("Use this number for the sync") == 2 and "Start history sync" not in page
    sync = {"business": "initech", "sync_type": "history", "confirm": "yes", "phone_number_id": "12"}
    r = c.post("/meta/sync", {"csrf": c.csrf("/numbers"), **sync})
    assert r.h["Location"].endswith("meta_not_connected")                  # refused locally: no number picked yet
    assert not [q for q in meta.seen if q.method == "POST"]
    bad = c.post("/meta/pick", {"csrf": c.csrf("/numbers"), "business": "initech", "phone_number_id": "99"})
    assert bad.h["Location"].endswith("pick_bad") and biz.get("initech")["phone_number_id"] == ""
    other = c.post("/meta/pick", {"csrf": c.csrf("/numbers"), "business": "zzz", "phone_number_id": "12"})
    assert other.h["Location"].endswith("pick_bad")
    ok = c.post("/meta/pick", {"csrf": c.csrf("/numbers"), "business": "initech", "phone_number_id": "12"})
    assert ok.h["Location"].endswith("picked") and biz.get("initech")["phone_number_id"] == "12"
    page = c.get("/numbers").body
    assert page.count("Start history sync") == 1 and page.count("Use this number for the sync") == 1
    r = c.post("/meta/sync", {"csrf": c.csrf("/numbers"), **sync})
    assert r.h["Location"].endswith("meta_ok")
    (post,) = [q for q in meta.seen if q.method == "POST"]
    assert post.url.path == "/v27.0/12/smb_app_data" and post.headers["Authorization"] == "Bearer tok-a"


def test_picking_a_number_needs_no_write_permission_and_writes_nothing_to_meta(tmp_path):
    meta = Meta()
    meta.tokens["tok-a"] = {"mgmt": ["10"], "phones": {"10": ["11", "12"]}}
    c, biz = numbers_app(tmp_path, meta)                                      # writes OFF
    biz.save("initech", {"waba_id": "10", "phone_number_id": "", "business_token": "tok-a"})
    r = c.post("/meta/pick", {"csrf": c.csrf("/numbers"), "business": "initech", "phone_number_id": "11"})
    assert r.h["Location"].endswith("picked")
    assert not [q for q in meta.seen if q.method != "GET"]


# ------------------------------------------------------------------ CR-M3: the verify_assets decision table

def run_verify(world, claim=("", ""), app_id="123"):
    meta = Meta()
    meta.tokens["T"] = world
    http, _ = mock_http(meta)
    return verify_assets("T", app_id=app_id, app_secret="S", claimed_waba=claim[0], claimed_phone=claim[1], client=http)


PH = {"10": ["11"], "20": ["21", "22"]}
CASES = [
    ("claim picks among many wabas and numbers", {"mgmt": ["10", "20"], "phones": PH}, ("20", "22"),
     {"waba_id": "20", "phone_number_id": "22"}),
    ("many wabas, no claim", {"mgmt": ["10", "20"], "phones": PH}, ("", ""), AmbiguousAssets),
    ("many wabas, claim outside the grant", {"mgmt": ["10", "20"], "phones": PH}, ("99", ""), AmbiguousAssets),
    ("one waba, a lying claim is ignored", {"mgmt": ["10"], "phones": PH}, ("99", "98"),
     {"waba_id": "10", "phone_number_id": "11"}),
    ("many numbers, no claim", {"mgmt": ["20"], "phones": PH}, ("", ""), {"waba_id": "20", "phone_number_id": ""}),
    ("many numbers, claim not listed", {"mgmt": ["20"], "phones": PH}, ("20", "77"),
     {"waba_id": "20", "phone_number_id": ""}),
    ("no granular scopes at all", {"mgmt": None, "phones": PH}, ("10", "11"), GraphError),
    ("management scope without target ids", {"mgmt": ["10"], "no_targets": True, "phones": PH}, ("10", "11"), GraphError),
    ("valid token for a DIFFERENT app", {"mgmt": ["10"], "phones": PH, "app_id": "999"}, ("10", "11"), GraphError),
    ("expired / invalid token", {"mgmt": ["10"], "phones": PH, "valid": False}, ("10", "11"), GraphError),
    ("management but no messaging grant", {"mgmt": ["10"], "msg": [], "phones": PH}, ("10", "11"), GraphError),
    ("messaging grants a different waba", {"mgmt": ["10"], "msg": ["20"], "phones": PH}, ("10", "11"), GraphError),
    ("integer-looking ids", {"mgmt": [10, 20], "phones": PH}, ("20", "21"), {"waba_id": "20", "phone_number_id": "21"}),
    ("null phone list", {"mgmt": ["10"], "phones": PH, "phone_null": True}, ("10", "11"),
     {"waba_id": "10", "phone_number_id": ""}),
]


@pytest.mark.parametrize("name,world,claim,want", CASES, ids=[c[0] for c in CASES])
def test_verify_assets_decision_table(name, world, claim, want):
    if isinstance(want, dict):
        assert run_verify(world, claim) == want
    else:
        with pytest.raises(want) as e:
            run_verify(world, claim)
        assert (type(e.value) is AmbiguousAssets) == (want is AmbiguousAssets)      # "ambiguous" is its own outcome


def test_a_wrong_app_or_expired_token_through_the_launcher_stores_nothing(tmp_path):
    app, states, biz, meta = launcher_world(tmp_path)
    meta.tokens["bad"] = {"mgmt": ["10"], "phones": PH, "valid": False}
    meta.codes["c"] = "bad"
    assert post_cb(app, {"kind": "code", "state": states.new("initech"), "code": "c"}).status == 400
    assert biz.get("initech") == {}


# ------------------------------------------------------------------ CR-M3: the token never reaches httpx logs or tracebacks

def test_httpx_logs_never_contain_the_token_at_debug_and_the_filter_is_what_prevents_it(caplog):
    meta = Meta()
    meta.tokens["TOKEN-LOGSENTINEL-1"] = {"mgmt": ["10"], "phones": PH}
    loggers = [logging.getLogger(n) for n in ("httpx", "httpcore")]

    def call():
        with httpx.Client(transport=httpx.MockTransport(meta)) as http:
            verify_assets("TOKEN-LOGSENTINEL-1", app_id="123", app_secret="S", claimed_waba="10", claimed_phone="11",
                          client=http)

    caplog.set_level(logging.DEBUG)
    for lg in loggers:
        caplog.set_level(logging.DEBUG, logger=lg.name)
    call()
    assert "TOKEN-LOGSENTINEL-1" not in caplog.text and "input_token" not in caplog.text
    saved = [(lg, list(lg.filters)) for lg in loggers]                      # control: without our filter the URL IS logged
    try:
        for lg in loggers:
            lg.filters.clear()
        caplog.clear()
        call()
        assert "TOKEN-LOGSENTINEL-1" in caplog.text
    finally:
        for lg, f in saved:
            lg.filters[:] = f


def test_error_output_never_contains_the_token():
    tok = "TOKEN-" + "ERRSENTINEL"                                            # not a literal: tracebacks print source lines
    http, _ = mock_http(lambda r: httpx.Response(401, json={"error": {"code": 190, "message": f"{tok} bad"}}))
    with pytest.raises(MetaRefused) as e:
        verify_assets(tok, app_id="123", app_secret="S", claimed_waba="", claimed_phone="", client=http)
    assert tok not in "".join(traceback.format_exception(e.value))


# ------------------------------------------------------------------ lows

def test_success_false_is_a_refusal_and_local_refusals_are_worded_differently(tmp_path):
    meta = Meta()
    meta.write_answer = {"success": False}
    c, biz = numbers_app(tmp_path, meta, allow_meta_writes=True)
    biz.save("initech", {"waba_id": "10", "phone_number_id": "11", "business_token": "tok-a"})
    sub = {"business": "initech", "waba_id": "10", "confirm": "yes"}
    assert c.post("/meta/subscribe", {"csrf": c.csrf("/numbers"), **sub}).h["Location"].endswith("meta_refused")
    other = c.post("/meta/subscribe", {"csrf": c.csrf("/numbers"), **sub, "business": "nobody"})
    assert other.h["Location"].endswith("meta_not_connected")
    meta.write_answer = {"success": True}
    assert c.post("/meta/subscribe", {"csrf": c.csrf("/numbers"), **sub}).h["Location"].endswith("meta_ok")


def test_a_timeout_on_a_write_keeps_the_uncertain_wording(tmp_path):
    def late(_req):
        raise httpx.ReadTimeout("t", request=_req)

    c, biz = numbers_app(tmp_path, late, allow_meta_writes=True)
    biz.save("initech", {"waba_id": "10", "phone_number_id": "11", "business_token": "tok-a"})
    r = c.post("/meta/subscribe", {"csrf": c.csrf("/numbers"), "business": "initech", "waba_id": "10", "confirm": "yes"})
    assert r.h["Location"].endswith("meta_failed")


def test_null_data_lists_do_not_crash_the_pages(tmp_path):
    def nulls(req):
        if req.url.path.endswith(("/subscribed_apps", "/phone_numbers", "/message_templates")):
            return httpx.Response(200, json={"data": None})
        return graph_handler(req)

    biz = BusinessStore(tmp_path / "biz")
    biz.save("initech", {"waba_id": "111", "phone_number_id": "", "business_token": TOKEN})
    http, _ = mock_http(nulls)
    g = RealGraphClient(biz, app_id="123", client=http)
    (w,) = g.list_wabas()
    assert "error" not in w and w["phone_numbers"] == [] and g.list_templates("111", "initech") == []
    app, _, _ = make_app(graph=g, businesses=biz, signups=SignupStates())
    c = Client(app)
    c.login()
    assert c.get("/numbers").status == 200 and c.get("/templates").status == 200


def args_for(**kw):
    base = {"host": "127.0.0.1", "port": 0, "launcher_port": 0, "launcher_url": "http://localhost:1"}
    return type("A", (), {**base, **kw})


def test_ports_must_differ_and_the_launcher_needs_the_real_graph(tmp_path):
    db, users = str(tmp_path / "d.db"), str(tmp_path / "u.json")
    assert cli.main(["seed", "--db", db, "--users-file", users]) == 0
    same = ["serve", "--db", db, "--users-file", users, "--graph", "real", "--app-id", "1", "--port", "8790",
            "--launcher-url", "http://localhost:8790", "--launcher-port", "8790"]
    assert cli.main(same) == 2
    fake = ["serve", "--db", db, "--users-file", users, "--launcher-url", "http://localhost:8791",
            "--launcher-port", "8791"]
    assert cli.main(fake) == 2


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_if_the_admin_bind_fails_the_launcher_socket_is_closed():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    lport = free_port()

    def ok(env, start_response):
        start_response("200 OK", [])
        return [b""]

    try:
        with pytest.raises(OSError):
            cli._run(args_for(port=busy.getsockname()[1], launcher_port=lport), ok, ok)
        again = socket.socket()
        again.bind(("127.0.0.1", lport))                                        # the launcher's port is free again
        again.close()
    finally:
        busy.close()
