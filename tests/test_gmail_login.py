"""scripts/gmail_login: stores only the refresh token, never prints it, asks for offline + consent."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts import gmail_login

REPO = Path(__file__).resolve().parents[1]
TOKEN = "1//fake-refresh-token-value"


def setup():
    cfg = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    vault = {"google-oauth-client": json.dumps({"installed": {"client_id": "c", "client_secret": "s"}})}
    said: list[str] = []
    seen: dict = {}

    class Flow:
        def run_local_server(self, **kw):
            seen.update(kw)
            return type("Cred", (), {"refresh_token": TOKEN})()

    def factory(client, scopes):
        seen["scopes"] = scopes
        return Flow()

    kw = dict(store=lambda n, v: vault.__setitem__(n, v), has=lambda n: n in vault, get=lambda n: vault[n],
              flow_factory=factory, say=said.append)
    return cfg, vault, said, seen, kw


def test_login_stores_refresh_token_only():
    cfg, vault, said, seen, kw = setup()
    assert gmail_login.login("ticketmail", cfg, **kw) == 0
    assert vault["gmail-ticketmail-refresh"] == TOKEN
    assert seen["access_type"] == "offline" and seen["prompt"] == "consent"
    assert seen["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert not any(TOKEN in s for s in said)


def test_login_refuses_overwrite_and_missing_client():
    cfg, vault, said, seen, kw = setup()
    vault["gmail-ticketmail-refresh"] = "old"
    assert gmail_login.login("ticketmail", cfg, **kw) == 1 and vault["gmail-ticketmail-refresh"] == "old"
    assert gmail_login.login("ticketmail", cfg, force=True, **kw) == 0
    del vault["google-oauth-client"]
    assert gmail_login.login("ticketmail", cfg, force=True, **kw) == 2
    assert gmail_login.login("nope", cfg, **kw) == 2
