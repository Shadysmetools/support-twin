"""Webhook edge (review #1, #2, #13 edge parts): Meta signature, required verify token, size caps, hub-number filter,
and a response that never leaks pipeline data. Real stdlib server on loopback; no network beyond that."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from twin_agent.hub import webhook
from hub_helpers import build_hub

FIX = Path(__file__).parent.parent / "twin_agent" / "hub" / "fixtures"
SECRET = "test-app-secret"
VERIFY = "vt-123-verify"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@contextmanager
def serve(hub, **kw):
    kw.setdefault("verify_token", VERIFY)
    kw.setdefault("app_secret", SECRET)
    srv = webhook.make_server(hub, **kw)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)


def post(base: str, body: bytes, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{base}/webhook", data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:            # noqa: S310 - loopback only
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {"Content-Type": "application/json", "X-Hub-Signature-256": _sig(body)}


def creates(hub) -> int:
    return sum(1 for w in hub.odoo.t.writes if w["method"] == "create")


def test_unsigned_post_is_401_and_never_reaches_the_hub():
    hub = build_hub()
    with serve(hub) as (srv, base):
        body = json.dumps(_load("webhook_text.json")).encode()
        status, _ = post(base, body, {"Content-Type": "application/json"})
        srv.drain()
    assert status == 401 and creates(hub) == 0 and hub.whatsapp.sent == []


def test_bad_signature_is_401():
    hub = build_hub()
    with serve(hub) as (srv, base):
        body = json.dumps(_load("webhook_text.json")).encode()
        status, _ = post(base, body, {"X-Hub-Signature-256": _sig(body, "wrong-secret")})
        status2, _ = post(base, body, {"X-Hub-Signature-256": "sha256=" + "0" * 64})
        status3, _ = post(base, body, {"X-Hub-Signature-256": "md5=abc"})
        srv.drain()
    assert (status, status2, status3) == (401, 401, 401) and creates(hub) == 0


def test_signature_is_checked_before_json_parse():
    hub = build_hub()
    with serve(hub) as (srv, base):
        status, _ = post(base, b"{not json", {"X-Hub-Signature-256": _sig(b"something else")})
    assert status == 401                                   # not 400: nothing is parsed until the HMAC passes


def test_valid_signature_bad_json_is_400():
    with serve(build_hub()) as (srv, base):
        body = b"{not json"
        status, _ = post(base, body, {"X-Hub-Signature-256": _sig(body)})
    assert status == 400


def test_signed_post_is_accepted_with_an_empty_200_and_processed():
    hub = build_hub()
    with serve(hub) as (srv, base):
        body, headers = signed(_load("webhook_text.json"))
        status, resp = post(base, body, headers)
        srv.drain()
    assert status == 200 and resp == b""                   # #2: nothing leaks (no ticket id, owner, rationale)
    assert creates(hub) == 1 and hub.whatsapp.automatic_count() == 1


def test_unknown_sender_gets_the_same_empty_200_as_a_known_one():
    from twin_agent.hub import fakes
    hub = build_hub(knowledge_odoo=fakes.FakeOdooKnowledge(partners={}))
    with serve(hub) as (srv, base):
        body, headers = signed(_load("webhook_text.json"))
        status, resp = post(base, body, headers)
        srv.drain()
    assert status == 200 and resp == b"" and creates(hub) == 0


def test_result_is_logged_server_side_and_redacted(caplog):
    hub = build_hub()
    with caplog.at_level(logging.INFO, logger="twin_agent.hub.webhook"):
        with serve(hub) as (srv, base):
            body, headers = signed(_load("webhook_text.json"))
            post(base, body, headers)
            srv.drain()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "ticket_id" in text                              # an operator can see what happened ...
    assert "201000000001" not in text and "Oscar" not in text and "Supervisor" not in text   # ... without PII


def test_oversized_body_is_413_before_it_is_read():
    with serve(build_hub(), max_body=1024) as (srv, base):
        host, port = srv.server_address
        c = http.client.HTTPConnection(host, port, timeout=10)
        c.putrequest("POST", "/webhook")
        c.putheader("Content-Length", str(5 * 1024 * 1024))
        c.putheader("X-Hub-Signature-256", "sha256=" + "0" * 64)
        c.endheaders()
        assert c.getresponse().status == 413
        c.close()


@pytest.mark.parametrize("bad", ["abc", "-5", "1e3", ""])
def test_bad_content_length_is_400(bad):
    with serve(build_hub()) as (srv, base):
        host, port = srv.server_address
        c = http.client.HTTPConnection(host, port, timeout=10)
        c.putrequest("POST", "/webhook")
        c.putheader("Content-Length", bad)
        c.endheaders()
        assert c.getresponse().status == 400
        c.close()


@pytest.mark.parametrize("kw", [{"verify_token": ""}, {"verify_token": None}, {"app_secret": ""},
                                {"app_secret": None}])
def test_verify_token_and_app_secret_are_required_no_dev_default(kw):
    args = {"verify_token": VERIFY, "app_secret": SECRET, **kw}
    with pytest.raises(ValueError):
        webhook.make_server(build_hub(), **args)


def test_verify_token_has_no_dev_default():
    with pytest.raises((ValueError, TypeError)):
        webhook.make_server(build_hub(), app_secret=SECRET)


def test_verify_handshake_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(webhook.hmac, "compare_digest", spy)
    with serve(build_hub()) as (srv, base):
        ok = urllib.request.urlopen(f"{base}/webhook?hub.mode=subscribe&hub.verify_token={VERIFY}"
                                    f"&hub.challenge=C1", timeout=10).read()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/webhook?hub.mode=subscribe&hub.verify_token=vt-123&hub.challenge=C2",
                                   timeout=10)
    assert ok == b"C1" and e.value.code == 403 and calls


def test_signature_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(webhook.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    with serve(build_hub()) as (srv, base):
        body, headers = signed(_load("webhook_text.json"))
        post(base, body, headers)
        srv.drain()
    assert calls


def test_changes_for_another_phone_number_id_are_ignored():
    hub = build_hub()
    payload = _load("webhook_text.json")
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "SOMEONE_ELSES_NUMBER"
    res = hub.handle_webhook(payload)
    assert "ignored" in res and creates(hub) == 0 and hub.whatsapp.sent == []


def test_hub_with_no_configured_number_fails_closed():
    hub = build_hub(cfg={})
    assert "ignored" in hub.handle_webhook(_load("webhook_text.json")) and creates(hub) == 0


def test_post_is_persisted_before_the_200_and_before_processing():
    hub = build_hub()
    gate, seen = threading.Event(), {}
    real = hub.handle_webhook

    def slow(payload):
        seen["status"] = hub.store.get("messages", "wamid.TEXT0001")["status"]      # what the worker finds on pickup
        gate.wait(5)
        return real(payload)

    hub.handle_webhook = slow
    with serve(hub) as (srv, base):
        body, headers = signed(_load("webhook_text.json"))
        status, _ = post(base, body, headers)
        assert status == 200 and creates(hub) == 0                       # answered while NOTHING was processed yet
        gate.set()
        srv.drain()
    assert seen["status"] == "queued" and creates(hub) == 1


def test_if_persisting_fails_the_post_is_a_500_so_meta_retries():
    hub = build_hub()

    def boom(payload):
        raise RuntimeError("disk full")

    hub.accept = boom
    with serve(hub) as (srv, base):
        body, headers = signed(_load("webhook_text.json"))
        status, resp = post(base, body, headers)
    assert status == 500 and resp == b""
