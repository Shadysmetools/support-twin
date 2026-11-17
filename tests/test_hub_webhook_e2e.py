"""The mock-webhook integration test: a REAL stdlib HTTP server on loopback drives ALL 11 stages end to end with
fakes, over two POSTs (first contact -> pause -> customer confirmation), plus Meta's GET verification handshake."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.request
from pathlib import Path

from twin_agent.hub import webhook
from datetime import timedelta

from hub_helpers import T0, approve, build_hub

FIX = Path(__file__).parent.parent / "twin_agent" / "hub" / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


PHONE = "201000000001"
SECRET, VERIFY = "test-app-secret", "vt-e2e"


def _post(srv, base: str, payload: dict) -> None:
    """Signed POST; the response is an EMPTY 200 (no leak), so we drain the worker and read hub state instead."""
    data = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), data, hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{base}/webhook", data=data,
                                 headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    with urllib.request.urlopen(req, timeout=10) as r:          # noqa: S310 - loopback only
        assert r.status == 200 and r.read() == b""
    srv.drain()


def test_mock_webhook_drives_all_11_stages_over_two_posts():
    hub = build_hub()
    srv = webhook.make_server(hub, verify_token=VERIFY, app_secret=SECRET)
    host, port = srv.server_address
    base = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # GET verification handshake (Meta subscribe)
        with urllib.request.urlopen(f"{base}/webhook?hub.mode=subscribe&hub.verify_token={VERIFY}"
                                    f"&hub.challenge=CHALLENGE123", timeout=10) as r:   # noqa: S310
            assert r.read().decode() == "CHALLENGE123"

        # POST 1: first contact -> stages 1..7, CONFIRM drafted and PENDING the owner (the POST returned long ago)
        hub.clock.at_least(T0 + timedelta(minutes=2))
        _post(srv, base, _load("webhook_text.json"))
        assert hub.latest_ticket(PHONE).stage == "awaiting_owner"
        approve(hub, "confirm")                                  # owner tap = its own call (Telegram side)
        assert hub.latest_ticket(PHONE).stage == "awaiting_customer"

        # POST 2: the customer replies -> attached; only the OWNER's marks move the ticket on
        hub.clock.at_least(T0 + timedelta(minutes=12))
        _post(srv, base, _load("webhook_confirm.json"))
        assert hub.latest_ticket(PHONE).stage == "awaiting_owner_advance"
        approve(hub, "advance")
        approve(hub, "fix_ready")                                # stages 9
        approve(hub, "resolve")                                  # stages 10..11 + close
        assert hub.latest_ticket(PHONE).stage == "resolved"
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)

    # every stage walked, in order
    assert hub.latest_ticket(PHONE).history == [
        "1-RECEIVE", "2-STT", "3-ANALYSE", "4-CREATE", "5-ASSIGN", "6-TELEGRAM", "7-ACK", "8-CONFIRM(drafted)",
        "8-CONFIRM", "8b-CONFIRMED", "9-PROVE-IT", "10-RESOLVE", "11-METRICS-KPIs"]
    # end-to-end side effects: exactly one auto client message, a KPI push, and a learning record
    assert hub.whatsapp.automatic_count() == 1
    assert len(hub.metrics.pushed) == 1
    assert len(hub.learning.records) == 1


def test_get_verification_rejects_wrong_token():
    hub = build_hub()
    srv = webhook.make_server(hub, verify_token=VERIFY, app_secret=SECRET)
    host, port = srv.server_address
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        import urllib.error
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/webhook?hub.mode=subscribe"
                                   f"&hub.verify_token=WRONG&hub.challenge=X", timeout=10)   # noqa: S310
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.shutdown()
        th.join(timeout=5)
