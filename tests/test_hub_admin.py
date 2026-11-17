"""The hub admin page (Meta App Review surface): auth, CSRF, the approval path, escaping, fictional data.
All in-process (no sockets, no network, no real sender)."""
from __future__ import annotations

import io
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from admin_helpers import (
    OTHER_PW,
    PW,
    Client,
    approve_form,
    approve_form_safe,
    logged_in,
    make_app,
    pending_for,
)

from twin_agent.hub.admin import auth, seed
from twin_agent.hub.admin.auth import RateLimiter
from twin_agent.hub.admin.config import load_users
from twin_agent.hub.admin.graph import FakeGraphClient, GraphClient

ADMIN_DIR = Path(__file__).parent.parent / "twin_agent" / "hub" / "admin"
TID_A, TID_B, TID_C = 20001, 20004, 20006          # approved+delivered, text awaiting, voice awaiting


def _human_sends(hub):
    return [s for s in hub.whatsapp.sent if not s["automatic"]]


# ------------------------------------------------------------------ auth

@pytest.mark.parametrize("path", ["/", "/tickets", f"/tickets/{TID_B}", "/numbers", "/templates", "/connect"])
def test_unauthenticated_get_is_refused_and_leaks_nothing(path):
    app, _hub, _ = make_app()
    r = Client(app).get(path)
    assert r.status == 303 and r.h["Location"] == "/login" and "Test Customer" not in r.body


def test_unauthenticated_post_cannot_approve():
    app, hub, _ = make_app()
    doc = pending_for(hub, TID_B)
    before = len(hub.whatsapp.sent)
    r = Client(app).post(f"/tickets/{TID_B}/approve", {"nonce": doc["nonce"], "draft_hash": doc["draft_hash"]})
    assert r.status == 303 and len(hub.whatsapp.sent) == before
    assert pending_for(hub, TID_B)["used_at"] is None


def test_password_hashing_roundtrip_and_malformed_hash_fails_closed():
    h = auth.hash_password("pw one")
    assert auth.verify_password("pw one", h) and not auth.verify_password("pw two", h)
    assert not auth.verify_password("x", "plaintext") and not auth.verify_password("x", "scrypt$a$b")
    assert "pw one" not in h


def test_wrong_password_and_unknown_user_are_refused_identically():
    app, _, _ = make_app()
    c = Client(app)
    a = c.login("demo-approver", "nope")
    b = c.login("nobody", "nope")
    assert a.status == b.status == 403
    assert "Wrong username or password" in a.body and "Wrong username or password" in b.body
    assert "__Host-aa_session" not in c.cookies


def test_login_requires_the_pre_login_csrf_token():
    app, _, _ = make_app()
    c = Client(app)
    c.get("/login")
    r = c.post("/login", {"csrf": "forged", "username": "demo-approver", "password": PW})
    assert r.status == 403 and "__Host-aa_session" not in c.cookies


def test_session_cookie_flags_and_new_session_id_per_login():
    app, _, _ = make_app()
    c = Client(app)
    r1 = c.login()
    sess = next(v for v in r1.set_cookies if v.startswith("__Host-aa_session="))
    assert "HttpOnly" in sess and "SameSite=Strict" in sess and "Secure" in sess
    sid1 = c.cookies["__Host-aa_session"]
    c.login()
    assert c.cookies["__Host-aa_session"] != sid1
    stale = Client(app, ip="10.0.0.9").request("GET", "/tickets", cookies={"__Host-aa_session": sid1})
    assert stale.status == 303                                     # the old session id no longer works


def test_login_rate_limit_by_ip_and_user_blocks_even_the_right_password():
    app, _, _ = make_app(limiter=RateLimiter(max_fails=3))
    for _ in range(3):
        Client(app, ip="10.0.0.1").login("demo-approver", "bad")
    assert Client(app, ip="10.0.0.1").login("demo-approver", PW).status == 429


def test_login_rate_limit_by_ip():
    app, _, _ = make_app(limiter=RateLimiter(max_fails=3, ip_max_fails=3))
    c = Client(app, ip="10.1.1.1")
    for i in range(3):
        c.login(f"user{i}", "bad")
    assert c.login("demo-approver", PW).status == 429


def test_logout_is_post_with_csrf_and_kills_the_session():
    c, _, _ = logged_in()
    assert c.post("/logout", {"csrf": "wrong"}).status == 403
    assert c.get("/tickets").status == 200
    assert c.post("/logout", {"csrf": c.csrf()}).status == 303
    assert c.get("/tickets").status == 303


def test_idle_session_expires():
    t = [1000.0]
    store = auth.SessionStore(idle_seconds=60, clock=lambda: t[0])
    s = store.create("demo-approver")
    t[0] += 61
    assert store.get(s.sid) is None


# ------------------------------------------------------------------ CSRF

def test_every_post_without_a_valid_csrf_token_is_refused_and_sends_nothing():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    before = len(hub.whatsapp.sent)
    for token in ("", "forged"):
        r = c.post(f"/tickets/{TID_B}/approve", {"csrf": token, "nonce": doc["nonce"],
                                                 "draft_hash": doc["draft_hash"], "action": "approve"})
        assert r.status == 403
    assert len(hub.whatsapp.sent) == before and pending_for(hub, TID_B)["used_at"] is None


# ------------------------------------------------------------------ approval = the existing hub path

def test_approve_sends_the_exact_draft_through_the_hub_and_records_the_named_approver():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    before = len(_human_sends(hub))
    r = c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B))
    assert r.status == 303 and r.h["Location"].endswith("?r=sent")
    sent = _human_sends(hub)
    assert len(sent) == before + 1 and sent[-1]["text"] == doc["draft"] and sent[-1]["to"] == "15550100002"
    used = hub.store.get("approvals", doc["nonce"])
    assert used["used_by"] == seed.DEMO_APPROVER_ID and used["approved"] is True and used["used_at"]
    reply = [s for s in hub.store.scan("sends") if s["tid"] == TID_B and s["kind"] == "reply"][0]
    hub.handle_webhook(seed.status_payload(reply["wamid"]))             # Meta's "delivered" callback
    page = c.get(f"/tickets/{TID_B}").body
    assert "Approved by Demo Approver" in page and "Sent to the customer" in page and "delivered" in page


def test_unedited_edit_and_approve_with_browser_crlf_is_not_treated_as_an_edit():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    judged_before = len(hub.judge.scored)
    c.post(f"/tickets/{TID_B}/approve",
           approve_form(c, hub, TID_B, "edit_approve", doc["draft"].replace("\n", "\r\n")))
    assert _human_sends(hub)[-1]["text"] == doc["draft"]
    assert len(hub.judge.scored) == judged_before                  # no edit re-judge: text=None reached the hub
    assert hub.ticket(TID_B).human_edits is False


def test_edited_text_is_exactly_what_gets_sent():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    edited = doc["draft"].replace("Is that correct?", "Is that right?") + " Thank you."
    assert edited != doc["draft"]
    c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B, "edit_approve", edited))
    assert _human_sends(hub)[-1]["text"] == edited.strip()
    assert hub.ticket(TID_B).human_edits is True


def test_plain_approve_ignores_any_text_field():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B, "approve", "TAMPERED TEXT"))
    assert _human_sends(hub)[-1]["text"] == doc["draft"]


def test_approval_is_single_use_replaying_the_same_form_sends_nothing_more():
    c, hub, _ = logged_in()
    form = approve_form(c, hub, TID_B)
    c.post(f"/tickets/{TID_B}/approve", form)
    n = len(hub.whatsapp.sent)
    r = c.post(f"/tickets/{TID_B}/approve", form)
    assert r.h["Location"].endswith("?r=stale") and len(hub.whatsapp.sent) == n


def test_hub_itself_refuses_a_replayed_nonce_even_if_the_page_guard_is_bypassed():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B))
    n = len(hub.whatsapp.sent)
    res = hub.on_owner_decision(doc["nonce"], seed.DEMO_APPROVER_ID)
    assert res["ok"] is False and res["reason"] == "used" and len(hub.whatsapp.sent) == n


def test_a_changed_draft_cannot_be_approved_with_the_old_hash():
    c, hub, _ = logged_in()
    form = approve_form(c, hub, TID_B)
    form["draft_hash"] = "0" * 64
    n = len(hub.whatsapp.sent)
    r = c.post(f"/tickets/{TID_B}/approve", form)
    assert r.h["Location"].endswith("?r=stale") and len(hub.whatsapp.sent) == n
    assert pending_for(hub, TID_B)["used_at"] is None


def test_a_user_who_is_not_an_approver_is_refused_and_the_nonce_is_not_burned():
    app, hub, _ = make_app()
    c = Client(app)
    assert c.login("outsider", OTHER_PW).status == 303
    n = len(hub.whatsapp.sent)
    r = c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B))
    assert r.h["Location"].endswith("?r=not_authorised") and len(hub.whatsapp.sent) == n
    assert pending_for(hub, TID_B)["used_at"] is None
    assert "You are not an approver" in c.get(f"/tickets/{TID_B}?r=not_authorised").body


def test_an_expired_approval_is_not_sent_and_the_page_says_so():
    c, hub, _ = logged_in()
    form = approve_form(c, hub, TID_B)
    hub.clock = lambda: datetime.now(UTC) + timedelta(hours=25)
    n = len(hub.whatsapp.sent)
    r = c.post(f"/tickets/{TID_B}/approve", form)
    assert len(hub.whatsapp.sent) == n and r.h["Location"].endswith("?r=stale")


def test_window_closed_at_send_time_is_rechecked_by_the_hub_and_reported():
    c, hub, _ = logged_in()
    form = approve_form(c, hub, TID_B)
    st = hub.ticket(TID_B)
    st.last_inbound_at = (datetime.now(UTC) - timedelta(hours=30)).isoformat()   # approval itself still live
    hub._save(st)
    n = len(hub.whatsapp.sent)
    r = c.post(f"/tickets/{TID_B}/approve", form)
    assert len(hub.whatsapp.sent) == n and r.h["Location"].endswith("?r=window_closed")
    assert "NOT sent: the customer's 24-hour window" in c.get(f"/tickets/{TID_B}?r=window_closed").body.replace("&#x27;", "'")


def test_sensitive_data_in_an_edit_is_refused_by_the_hub_path():
    c, hub, _ = logged_in()
    n = len(hub.whatsapp.sent)
    bad = "Call me on 01012345678 or use card 4111 1111 1111 1111"
    r = c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B, "edit_approve", bad))
    assert len(hub.whatsapp.sent) == n and "?r=" in r.h["Location"] and not r.h["Location"].endswith("?r=sent")


def test_admin_never_sends_it_only_calls_the_hub():
    # seed.py is excluded: it holds the DEMO fake sender (DemoWhatsApp), demo scaffolding and not the page itself.
    text = "\n".join(p.read_text(encoding="utf-8") for p in ADMIN_DIR.glob("*.py") if p.name != "seed.py")
    assert "send_text" not in text and ".whatsapp" not in text
    assert "on_owner_decision" in (ADMIN_DIR / "app.py").read_text(encoding="utf-8")


def test_admin_has_no_approvals_store_of_its_own():
    text = (ADMIN_DIR / "app.py").read_text(encoding="utf-8")
    assert re.findall(r"store\.(?:put|update|put_if_absent)\(", text) == []


# ------------------------------------------------------------------ screens

def test_tickets_list_and_detail_show_draft_transcript_status_and_timeline():
    c, _hub, _ = logged_in()
    lst = c.get("/tickets").body
    assert "Awaiting approval" in lst and "Test Customer" in lst
    assert "••••" in lst and "15550100002" not in lst                    # numbers masked
    voice = c.get(f"/tickets/{TID_C}").body
    assert "Voice note transcript" in voice and "my order has not arrived" in voice
    assert "Awaiting approval" in voice and "Edit + Approve" in voice and "Approve as drafted" in voice
    done = c.get(f"/tickets/{TID_A}").body
    for step in ("Customer message received", "Automatic acknowledgement", "AI draft created",
                 "Approved by Demo Approver", "Sent to the customer", "Delivery status: delivered"):
        assert step in done
    assert "Edit + Approve" not in done and "No draft is waiting" in done


def test_unknown_ticket_is_404_and_nothing_is_reflected_from_the_url():
    c, _, _ = logged_in()
    assert c.get("/tickets/999999").status == 404 and c.get("/tickets/<script>").status == 404


def test_connected_numbers_masked_with_webhook_status():
    c, _, _ = logged_in()
    page = c.get("/numbers").body
    assert "Demo Bakery" in page and "Webhooks: subscribed" in page and "CONNECTED" in page
    assert "4321" in page and "555 010" not in page


def test_templates_lists_only_approved():
    c, _, _ = logged_in()
    page = c.get("/templates").body
    assert "order_update" in page and "support_followup" in page and "seasonal_offer" not in page


def test_graph_failure_is_shown_plainly_and_graph_client_is_a_protocol():
    c, _, _ = logged_in(graph=FakeGraphClient(fail=True))
    assert "Could not read from Meta." in c.get("/numbers").body
    assert isinstance(FakeGraphClient(), GraphClient)


def test_connect_page_disabled_when_not_configured_and_enabled_when_configured():
    c, _, _ = logged_in(app_id="", config_id="")
    page = c.get("/connect").body
    assert "not configured" in page and " disabled" in page
    c2, _, _ = logged_in(app_id="111", config_id="222")
    r = c2.get("/connect")
    assert "connect.facebook.net" not in r.body and "facebook" not in r.h["Content-Security-Policy"]


def test_security_headers_present():
    c, _, _ = logged_in()
    h = c.get("/tickets").h
    assert h["X-Frame-Options"] == "DENY" and "default-src 'none'" in h["Content-Security-Policy"]
    assert h["Cache-Control"] == "no-store"


# ------------------------------------------------------------------ escaping

def test_all_customer_controlled_output_is_escaped():
    c, hub, _ = logged_in()
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    st = hub.ticket(TID_B)
    st.inbound.name, st.inbound.text, st.client = evil, evil, evil
    st.replies.append({"message_id": "m9", "kind": "text", "text": evil, "ts": st.inbound.ts.isoformat()})
    hub._save(st)
    doc = pending_for(hub, TID_B)
    hub.store.update("approvals", doc["nonce"], lambda d: {**d, "draft": evil})
    for path in ("/tickets", f"/tickets/{TID_B}"):
        body = c.get(path).body
        assert "<script>alert" not in body and "<img" not in body
        assert "&lt;script&gt;" in body


def test_flash_code_from_the_query_string_is_never_reflected():
    c, _, _ = logged_in()
    body = c.get(f"/tickets/{TID_B}?r=<script>alert(1)</script>").body
    assert "<script>alert(1)" not in body and "Not completed" in body


# ------------------------------------------------------------------ demo data + seed + CLI

def test_seed_is_fictional_only_and_refuses_a_dirty_db():
    _app, hub, _ = make_app()
    tickets = repr(hub.store.scan("tickets"))
    blob = tickets + repr(hub.store.scan("approvals")) + repr(hub.whatsapp.sent)
    assert "Test Customer" in blob and "Demo Bakery" in tickets
    for real in ("Principal", "Supervisor", "Oscar", "Riley", "Executive", "Sysadmin"):
        assert real not in blob
    assert seed.is_demo_db(hub)
    with pytest.raises(RuntimeError):
        seed.seed(hub)


def test_seed_refuses_any_existing_file_even_one_with_no_tickets(tmp_path, capsys):
    from twin_agent.hub.admin import __main__ as cli
    from twin_agent.hub.store import SqliteStore
    db = tmp_path / "hub.db"
    s = SqliteStore(str(db))
    s.put("other_ns", "k", {"x": 1})                       # no tickets/messages/approvals/holds: the old check passed
    s.close()
    assert cli.main(["seed", "--db", str(db), "--users-file", str(tmp_path / "u.json")]) == 2
    assert "NEW file" in capsys.readouterr().err
    assert not seed.has_demo_marker(str(db)) and not (tmp_path / "u.json").exists()
    assert cli.main(["serve", "--seed", "--db", str(db), "--users-file", str(tmp_path / "u.json")]) == 2


def test_serve_checks_the_marker_before_opening_the_file(tmp_path, monkeypatch, capsys):
    import sqlite3

    from twin_agent.hub.admin import __main__ as cli
    db = tmp_path / "real.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE docs (ns TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (ns, key))")
    c.commit()
    c.close()
    before = db.read_bytes()
    monkeypatch.setattr(seed, "build_demo_hub", lambda p: pytest.fail("the hub/store must not be opened"))
    assert cli.main(["serve", "--db", str(db), "--users-file", str(tmp_path / "u.json")]) == 2
    assert db.read_bytes() == before and not (tmp_path / "hub.db.lock").exists()
    assert not Path(str(db) + "-wal").exists() and not Path(str(db) + ".lock").exists()


def test_serve_refuses_a_db_that_is_not_a_demo_db(tmp_path, capsys):
    from twin_agent.hub.admin import __main__ as cli
    from twin_agent.hub.store import SqliteStore
    db = tmp_path / "real.db"
    s = SqliteStore(str(db))
    s.put("tickets", "1", {"x": 1})
    s.close()
    users = tmp_path / "u.json"
    seed.write_demo_users(users)
    assert cli.main(["serve", "--db", str(db), "--users-file", str(users)]) == 2
    assert "not a demo DB" in capsys.readouterr().err


def test_serve_refuses_a_public_bind_without_the_flag(tmp_path, capsys):
    from twin_agent.hub.admin import __main__ as cli
    code = cli.main(["serve", "--db", ":memory:", "--host", "0.0.0.0", "--users-file", str(tmp_path / "u.json")])
    assert code == 2 and "allow-public-bind" in capsys.readouterr().err


def test_demo_users_file_has_a_hash_not_a_password_and_is_never_overwritten(tmp_path):
    p = tmp_path / "users.json"
    pw = seed.write_demo_users(p)
    text = p.read_text(encoding="utf-8")
    assert pw and pw not in text and "scrypt$" in text
    assert seed.write_demo_users(p) is None
    assert auth.check_login(load_users(p), "demo-approver", pw) is not None


def test_host_header_allowlist_blocks_dns_rebinding():
    app, _, _ = make_app()
    app.cfg.allowed_hosts = ("127.0.0.1:8780",)
    assert Client(app).get("/login").status == 200
    app.cfg.allowed_hosts = ("evil.example:1",)
    assert Client(app).get("/login").status == 400


def test_oversized_body_is_refused():
    app, _, _ = make_app()
    out = {}
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/login", "CONTENT_LENGTH": str(10 ** 7),
           "wsgi.input": io.BytesIO(b""), "REMOTE_ADDR": "127.0.0.1"}
    app(env, lambda s, h: out.setdefault("s", s))
    assert out["s"].startswith("413")


def test_a_sender_failure_is_reported_as_unknown_never_as_not_sent_and_is_not_retried():
    c, hub, _ = logged_in()
    sender = hub.whatsapp

    def boom(*a, **k):
        raise TimeoutError("sender down or kill switch")

    sender.send_text = boom                                   # the hub's sender refuses / times out
    n = len(sender.sent)
    r = c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B))
    assert r.h["Location"].endswith("?r=send_unknown") and len(sender.sent) == n
    assert "did not confirm" in c.get(f"/tickets/{TID_B}?r=send_unknown").body
    assert "Nothing was sent" not in c.get(f"/tickets/{TID_B}?r=send_unknown").body
    sender.send_text = lambda *a, **k: sender.sent.append({"text": "SHOULD NOT HAPPEN", "automatic": False})
    c.post(f"/tickets/{TID_B}/approve", approve_form_safe(c, hub, TID_B))
    assert len(sender.sent) == n                              # the hub holds the ticket: never auto re-sent


# ------------------------------------------------------------------ review fixes (PR #62 review)

def _blank_flow(c, hub, blank):
    """The reviewer's path: clear the box -> Edit + Approve -> then Approve as drafted on whatever is now pending."""
    n = len(hub.whatsapp.sent)
    r1 = c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B, "edit_approve", blank))
    r2 = c.post(f"/tickets/{TID_B}/approve", approve_form(c, hub, TID_B, "approve"))
    return n, r1, r2


@pytest.mark.parametrize("blank", ["", "   ", "\r\n \n"])
def test_empty_edit_is_refused_and_a_blank_message_is_never_sent(blank):
    c, hub, _ = logged_in()
    n, r1, _r2 = _blank_flow(c, hub, blank)
    assert r1.h["Location"].endswith("?r=empty_edit")
    assert len(hub.whatsapp.sent) == n + 1                 # only the real drafted text went out, never ''
    assert hub.whatsapp.sent[-1]["text"].strip()
    assert all(s["text"].strip() for s in hub.whatsapp.sent)


def test_hub_refuses_empty_text_itself_and_never_reissues_an_empty_draft():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    n = len(hub.whatsapp.sent)
    res = hub.on_owner_decision(doc["nonce"], seed.DEMO_APPROVER_ID, text="   ")
    assert res["ok"] is False and res["reason"] == "empty_text" and len(hub.whatsapp.sent) == n
    new = pending_for(hub, TID_B)
    assert new["nonce"] != doc["nonce"] and new["draft"] == doc["draft"] and new["draft"].strip()


def test_hub_refuses_an_approval_whose_stored_draft_is_empty():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    from twin_agent.hub.owner_flow import sha
    hub.store.update("approvals", doc["nonce"], lambda d: {**d, "draft": "", "draft_hash": sha("")})
    n = len(hub.whatsapp.sent)
    res = hub.on_owner_decision(doc["nonce"], seed.DEMO_APPROVER_ID)
    assert res == {"ok": False, "reason": "empty_text"} and len(hub.whatsapp.sent) == n


def test_the_edit_box_is_required_in_the_form():
    c, _, _ = logged_in()
    assert "<textarea name=\"text\" rows=\"7\" required>" in c.get(f"/tickets/{TID_B}").body


def test_judge_score_and_low_score_warning_are_shown_next_to_approve():
    c, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    assert f"judge {doc['judge']}" in c.get(f"/tickets/{TID_B}").body
    hub.store.update("approvals", doc["nonce"], lambda d: {**d, "judge": 0.41, "judge_flag": True})
    page = c.get(f"/tickets/{TID_B}").body
    assert "LOW SCORE: owner-review" in page and "judge 0.41" in page
    hub.store.update("approvals", doc["nonce"], lambda d: {**d, "judge": None, "judge_flag": True,
                                                           "judge_unjudged": True})
    assert "UNJUDGED: owner-review" in c.get(f"/tickets/{TID_B}").body


def test_the_hub_stores_the_flag_that_telegram_shows_and_the_page_reads():
    _, hub, _ = logged_in()
    doc = pending_for(hub, TID_B)
    assert doc["judge_flag"] is False and doc["judge_unjudged"] is False and doc["judge"] is not None


def test_non_ascii_digits_in_a_ticket_url_are_404_not_500():
    c, _, _ = logged_in()
    assert c.get("/tickets/²").status == 404
    assert c.post("/tickets/²/approve", {"csrf": c.csrf()}).status == 404


def test_scrypt_cost_is_raised_and_old_hashes_still_verify():
    import base64
    import hashlib
    h = auth.hash_password("pw")
    assert h.split("$")[1] == str(2 ** 17)
    salt = b"0123456789abcdef"
    old = "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(
        hashlib.scrypt(b"pw", salt=salt, n=2 ** 14, r=8, p=1, dklen=32)).decode()
    assert auth.verify_password("pw", old) and auth.verify_password("pw", h)
