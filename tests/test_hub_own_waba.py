"""'Acme (own number)': our own WABA, read-only, in Connected numbers + Templates (Meta App Review video).

NO network (Graph is an ``httpx.MockTransport``) and NO real secrets (a sentinel token in ``tmp_path``)."""
from __future__ import annotations

import json

import httpx
import pytest
from admin_helpers import Client, make_app

from twin_agent.hub import staging
from twin_agent.hub.admin.graph import FakeGraphClient, GraphError, LocalRefusal
from twin_agent.hub.admin.graph_real import RealGraphClient
from twin_agent.hub.admin.meta_store import BusinessStore, SignupStates
from twin_agent.hub.admin.own_waba import EXECUTIVE_DIGITS, OWN_LABEL, OWN_SLUG, OwnWabaReader, WithOwnWaba, read_token

WABA, PHONE, APP = "1000000000012", "1000000000011", "999"
TOKEN = "OWN-SYSTEM-USER-TOKEN-SENTINEL-41e7"
OWN_NUMBER = "+1 555-010-0099"


def token_file(tmp_path, token=TOKEN):
    p = tmp_path / "own-system-user.json"
    p.write_text(json.dumps({"system_user_token": token}), encoding="utf-8")
    return str(p)


def meta(numbers=None, waba_id=WABA, status=200):
    """A fake Graph: records every request and answers the own WABA's reads."""
    seen = []
    nums = numbers if numbers is not None else [{"id": PHONE, "display_phone_number": OWN_NUMBER,
                                                 "verified_name": "Acme", "status": "CONNECTED",
                                                 "quality_rating": "GREEN"}]

    def handler(req):
        seen.append(req)
        p = req.url.path
        if status != 200:
            return httpx.Response(status, json={"error": {"code": 190, "message": f"secret {TOKEN}"}})
        if p.endswith("/phone_numbers"):
            return httpx.Response(200, json={"data": nums})
        if p.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"data": [{"whatsapp_business_api_data": {"id": APP}}]})
        if p.endswith("/message_templates"):
            return httpx.Response(200, json={"data": [
                {"name": "hello_world", "language": "en_US", "category": "UTILITY", "status": "APPROVED",
                 "components": [{"type": "BODY", "text": "Hello World"}]},
                {"name": "draft_one", "language": "en", "category": "MARKETING", "status": "PENDING", "components": []}]})
        return httpx.Response(200, json={"id": waba_id})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def reader(tmp_path, client, waba=WABA):
    return OwnWabaReader(waba, token_file(tmp_path), app_id=APP, client=client)


def combined(tmp_path, client, base=None):
    return WithOwnWaba(base or FakeGraphClient(), reader(tmp_path, client))


def test_own_entry_is_labelled_and_read_with_get_only(tmp_path):
    client, seen = meta()
    w = [x for x in combined(tmp_path, client).list_wabas() if x.get("own")][0]
    assert w["name"] == OWN_LABEL == "Acme (own number)" and w["business"] == OWN_SLUG and w["read_only"]
    n = w["phone_numbers"][0]
    assert n["id"] == PHONE and n["status"] == "CONNECTED" and n["quality_rating"] == "GREEN" and n["webhooks_subscribed"]
    assert {r.method for r in seen} == {"GET"}
    assert {r.url.path.split("/", 2)[2] for r in seen} == {WABA, f"{WABA}/phone_numbers", f"{WABA}/subscribed_apps"}
    assert all(TOKEN not in str(r.url) and r.headers["authorization"] == f"Bearer {TOKEN}" for r in seen)


def test_templates_are_read_for_the_own_waba_with_get_only(tmp_path):
    client, seen = meta()
    ts = combined(tmp_path, client).list_templates(WABA, OWN_SLUG)
    assert [t["name"] for t in ts] == ["hello_world", "draft_one"] and ts[0]["body"] == "Hello World"
    assert {r.method for r in seen} == {"GET"}


def test_a_waba_id_other_than_the_configured_one_is_refused_before_any_request(tmp_path):
    client, seen = meta()
    c = combined(tmp_path, client)
    with pytest.raises(LocalRefusal):
        c.list_templates("111222333444555", OWN_SLUG)
    with pytest.raises(LocalRefusal):                       # an ES business may not read the own WABA either
        c.list_templates(WABA, "some-business")
    assert seen == []


def test_meta_answering_for_a_different_waba_is_refused(tmp_path):
    client, _ = meta(waba_id="555")
    w = reader(tmp_path, client).entry()
    assert w["error"] and w["phone_numbers"] == []


def test_executives_number_is_filtered_out_of_the_view_not_the_whole_entry(tmp_path):
    client, _ = meta(numbers=[{"id": "1", "display_phone_number": "+1 555-000-9999", "status": "CONNECTED"},
                              {"id": "2", "display_phone_number": "+1 555-010-0099", "status": "CONNECTED"}])
    w = reader(tmp_path, client).entry()
    assert "error" not in w and [n["id"] for n in w["phone_numbers"]] == ["2"]
    assert "9999" not in json.dumps(w) and EXECUTIVE_DIGITS not in json.dumps(w)
    inner = FakeGraphClient()
    c = combined(tmp_path, client, inner)                       # writes for the own entry stay refused
    with pytest.raises(LocalRefusal):
        c.subscribe_app(WABA, OWN_SLUG)
    with pytest.raises(LocalRefusal):
        c.start_sync("2", "smb_app_state_sync", OWN_SLUG)


def test_only_executives_number_in_the_list_gives_an_empty_entry_without_an_error(tmp_path):
    client, _ = meta(numbers=[{"id": "1", "display_phone_number": "+1 555-000-9999", "status": "CONNECTED"}])
    w = reader(tmp_path, client).entry()
    assert "error" not in w and w["phone_numbers"] == [] and "9999" not in json.dumps(w)


def test_meta_error_and_missing_token_never_leak_the_token(tmp_path):
    client, _ = meta(status=400)
    w = reader(tmp_path, client).entry()
    assert "HTTP 400" in w["error"] and TOKEN not in json.dumps(w)
    gone = OwnWabaReader(WABA, str(tmp_path / "nope.json"), client=client).entry()
    assert gone["error"] and str(tmp_path) not in gone["error"]
    with pytest.raises(GraphError):
        read_token(str(tmp_path / "nope.json"))


def test_token_file_may_be_plain_text(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text(TOKEN + "\n", encoding="utf-8")
    assert read_token(str(p)) == TOKEN


def test_writes_are_refused_for_the_own_entry_and_never_reach_the_inner_client(tmp_path):
    client, seen = meta()
    inner = FakeGraphClient()
    c = combined(tmp_path, client, inner)
    with pytest.raises(LocalRefusal):
        c.subscribe_app(WABA, OWN_SLUG)
    with pytest.raises(LocalRefusal):
        c.subscribe_app(WABA, "some-business")
    with pytest.raises(LocalRefusal):
        c.start_sync(PHONE, "history", OWN_SLUG)
    assert seen == [] and inner.calls == []


def test_each_side_survives_the_other_failing(tmp_path):
    client, _ = meta()
    both = combined(tmp_path, client, FakeGraphClient(fail=True)).list_wabas()
    assert both[0]["error"] and both[1]["own"] and not both[1].get("error")
    bad, _ = meta(status=500)
    both = combined(tmp_path, bad).list_wabas()
    assert both[0]["name"] == "Demo Bakery" and both[1]["error"]


def test_pages_show_the_label_the_template_and_no_token_or_write_forms(tmp_path):
    client, _ = meta()
    c = Client(make_app(graph=combined(tmp_path, client))[0])
    assert c.login().status == 303
    nums = c.get("/numbers").body
    assert OWN_LABEL in nums and "Demo Bakery" in nums and "Read-only (our own number)" in nums
    assert TOKEN not in nums and f'value="{OWN_SLUG}"' not in nums
    tpl = c.get("/templates").body
    assert OWN_LABEL in tpl and "hello_world" in tpl and "draft_one" not in tpl and TOKEN not in tpl


def test_own_slug_cannot_be_connected_or_picked_as_an_es_business(tmp_path):
    client, _ = meta()
    store = BusinessStore(str(tmp_path / "biz"))
    c = Client(make_app(graph=combined(tmp_path, client), businesses=store, signups=SignupStates(),
                        cfg={"launcher_url": "https://launch.example"})[0])
    c.login()
    r = c.post("/connect/start", {"csrf": c.csrf("/connect"), "business": OWN_SLUG})
    assert r.h["Location"].endswith("connect_bad")
    r = c.post("/meta/pick", {"csrf": c.csrf("/numbers"), "business": OWN_SLUG, "phone_number_id": PHONE})
    assert r.h["Location"].endswith("pick_bad") and list(store.records()) == []


def test_the_system_user_token_is_never_written_to_the_signup_store(tmp_path):
    client, _ = meta()
    store = BusinessStore(str(tmp_path / "biz"))
    c = combined(tmp_path, client, RealGraphClient(store, app_id=APP, client=client))
    c.list_wabas()
    c.list_templates(WABA, OWN_SLUG)
    assert list(store.records()) == []
    biz = tmp_path / "biz"
    assert not any(TOKEN in f.read_text(errors="ignore") for f in biz.rglob("*") if f.is_file())


# ------------------------------------------------------------------ staging config

def cfg(**own):
    d = dict(phone_number_id=staging.TEST_PHONE_NUMBER_ID, test_partner_id=1187, staff_approver_id=90001,
             webhook_port=0, admin_port=0, launcher_port=0, own_waba=own)
    return staging.config_from_dict(d)


def why(c):
    return staging.problems(c, have_app_secret=True, have_verify_token=True)


def test_own_waba_is_off_by_default():
    d = dict(phone_number_id=staging.TEST_PHONE_NUMBER_ID, test_partner_id=1187, staff_approver_id=90001)
    assert staging.config_from_dict(d).own_waba is None


def test_own_waba_block_validates():
    assert why(cfg(waba_id=WABA, label=OWN_LABEL)) == []
    assert any("own WABA" in p for p in why(cfg(waba_id="111222333444555")))
    assert any("label" in p for p in why(cfg(waba_id=WABA, label="Executive")))
    store = cfg(waba_id=WABA).business_dir
    assert any("token store" in p for p in why(cfg(waba_id=WABA, token_file=store + "/x.json")))
    with pytest.raises(staging.StagingRefused):
        cfg(waba_id=WABA, extra="x")


def test_executives_number_in_the_own_block_is_refused():
    with pytest.raises(staging.StagingRefused):
        cfg(waba_id=WABA, label="+1 555 000 9999")


# ------------------------------------------------------------------ review fixes (M1 token file, M2 wiring, L1-L3)

@pytest.mark.parametrize("content", [
    "\ufeff" + json.dumps({"system_user_token": TOKEN}),          # BOM + JSON (Windows editors)
    json.dumps({"system_user_token": TOKEN}),
    "\ufeff" + TOKEN + "\n",
])
def test_token_file_with_a_bom_is_read(tmp_path, content):
    p = tmp_path / "t.json"
    p.write_bytes(content.encode("utf-8"))
    assert read_token(str(p)) == TOKEN


@pytest.mark.parametrize("content", ['{"system_user_token": ', "{broken", "two words", "tok\u00e9n", '{"other": "x"}', "",
                                      "[1]"])
def test_junk_token_files_are_refused_with_a_fixed_error_and_never_crash_the_page(tmp_path, content):
    p = tmp_path / "t.json"
    p.write_bytes(content.encode("utf-8"))
    with pytest.raises(GraphError) as e:
        read_token(str(p))
    assert content.strip() not in str(e.value) or not content.strip()
    client, seen = meta()
    w = OwnWabaReader(WABA, str(p), client=client).entry()
    assert w["error"] and seen == []
    page = Client(make_app(graph=WithOwnWaba(FakeGraphClient(), OwnWabaReader(WABA, str(p), client=client)))[0])
    page.login()
    assert page.get("/numbers").status == 200 and "Demo Bakery" in page.get("/templates").body


def test_an_unexpected_error_in_the_own_read_becomes_a_fixed_error_line(tmp_path):
    def boom(req):
        raise RuntimeError(f"secret {TOKEN}")
    client = httpx.Client(transport=httpx.MockTransport(boom))
    w = OwnWabaReader(WABA, token_file(tmp_path), client=client).entry()
    assert w["error"] and TOKEN not in json.dumps(w)


def _real_cfg(tmp_path, **own):
    from dataclasses import replace
    c = cfg(**own) if own else cfg(waba_id=WABA)
    return replace(c, admin_port=18791, launcher_port=18792, business_dir=str(tmp_path / "biz"),
                   app_secret_file=str(tmp_path / "app.json"))


def _admin(tmp_path, c, dry_run):
    from test_hub_staging import SEC, _users, build
    hub = build(c)
    sec = SEC if dry_run else staging.Secrets("123", "s", "v", "", "456")
    app, _ = staging.build_admin(c, sec, hub, dry_run=dry_run, users_file=_users(tmp_path, c))
    return app, hub


def test_dry_run_ignores_own_waba_and_makes_no_graph_reads(tmp_path):
    app, hub = _admin(tmp_path, _real_cfg(tmp_path), True)
    assert not isinstance(app.graph, WithOwnWaba) and isinstance(app.graph, FakeGraphClient)
    hub.store.close()


def test_real_run_wires_own_waba_read_only(tmp_path):
    c = _real_cfg(tmp_path)
    app, hub = _admin(tmp_path, c, False)
    assert isinstance(app.graph, WithOwnWaba) and app.graph.own.waba_id == WABA
    assert app.graph.own.token_file == c.system_user_file and app.cfg.allow_meta_writes is False
    hub.store.close()


def test_off_by_default_real_run_has_no_own_entry(tmp_path):
    from dataclasses import replace
    app, hub = _admin(tmp_path, replace(_real_cfg(tmp_path), own_waba=None), False)
    assert not isinstance(app.graph, WithOwnWaba)
    hub.store.close()


def test_a_symlinked_token_file_into_the_store_is_refused(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "x.json").write_text("{}")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(store / "x.json")
    except OSError:
        pytest.skip("symlinks not permitted here")
    c = staging.config_from_dict(dict(phone_number_id=staging.TEST_PHONE_NUMBER_ID, test_partner_id=1187,
                                      staff_approver_id=90001, business_dir=str(store),
                                      own_waba={"waba_id": WABA, "token_file": str(link)}))
    assert any("token store" in p for p in why(c))


def test_an_empty_own_waba_block_is_refused():
    with pytest.raises(staging.StagingRefused):
        cfg()


def test_own_slug_is_reserved_in_the_business_store(tmp_path):
    from twin_agent.hub.admin.meta_store import valid_slug
    assert not valid_slug(OWN_SLUG) and valid_slug("bakery")
    with pytest.raises(Exception):
        BusinessStore(str(tmp_path / "b")).save(OWN_SLUG, {"waba_id": "1"})


def test_a_wrong_password_rotates_the_pre_auth_token():
    c = Client(make_app()[0])
    import re
    first = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").body).group(1)
    r = c.post("/login", {"csrf": first, "username": "demo-approver", "password": "wrong"})
    second = re.search(r'name="csrf" value="([^"]+)"', r.body).group(1)
    assert r.status == 403 and second != first
