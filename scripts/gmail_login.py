"""One-time Gmail login for one mailbox: `python -I aa.py gmail-login ticketmail` (06 E, 05 section 8).

OAuth "installed app" flow: a browser opens on Google's consent page, Principal signs in to the mailbox and allows the
scopes listed for that mailbox in config.yaml (gmail.readonly first, gmail.compose from day 3). Only the refresh
token is kept, straight into the vault under the mailbox's vault_name (e.g. 'gmail-ticketmail-refresh').
Nothing is printed except the mailbox name and the scope names. No token file is written to disk.

Needs 'google-oauth-client' in the vault first (the OAuth client JSON from Google Cloud):
    python -I aa.py creds set-file google-oauth-client <path to client_secret.json>

Options: --force   replace an existing token for this mailbox.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Callable

CLIENT_VAULT = "google-oauth-client"


def _vault():
    from twin_agent import creds
    return creds.set_secret, creds.has_secret, creds._get


def _flow_factory(client_config: dict, scopes: list[str]) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow  # lazy: only on the laptop
    return InstalledAppFlow.from_client_config(client_config, scopes=scopes)


def login(mailbox: str, cfg: dict, *, store: Callable[[str, str], None], has: Callable[[str], bool],
          get: Callable[[str], str], flow_factory: Callable[[dict, list[str]], Any] = _flow_factory,
          say: Callable[[str], None] = print, force: bool = False) -> int:
    from twin_agent.channels.gmail_common import full_scopes

    box = (cfg.get("mailboxes") or {}).get(mailbox)
    if box is None:
        say(f"No mailbox '{mailbox}' in config.yaml (mailboxes:). Known: {', '.join(cfg.get('mailboxes') or {})}")
        return 2
    vault_name = box.get("vault_name") or f"gmail-{mailbox}-refresh"
    if has(vault_name) and not force:
        say(f"A Gmail token for '{mailbox}' is already in the vault. Use --force to replace it.")
        return 1
    if not has(CLIENT_VAULT):
        say(f"'{CLIENT_VAULT}' is not in the vault. First run: python -I aa.py creds set-file {CLIENT_VAULT} <json>")
        return 2
    scopes = full_scopes(box.get("scopes") or ["gmail.readonly"])
    try:
        client_config = json.loads(get(CLIENT_VAULT))
    except ValueError:
        say(f"'{CLIENT_VAULT}' in the vault is not valid JSON. Nothing was saved.")
        return 2
    say(f"Opening the Google sign-in page for mailbox '{mailbox}'. Scopes: {', '.join(box.get('scopes') or [])}")
    flow = flow_factory(client_config, scopes)
    # offline + consent: Google returns a refresh token even on a second login.
    token = flow.run_local_server(port=0, access_type="offline", prompt="consent", open_browser=True)
    refresh = getattr(token, "refresh_token", None)
    if not refresh:
        say("Google did not return a refresh token. Nothing was saved. Remove the app's access in the Google "
            "account (Security > Third-party access) and run this again.")
        return 3
    store(vault_name, refresh)
    say(f"Saved the Gmail login for '{mailbox}' in the vault as '{vault_name}'.")
    if "gmail.compose" not in (box.get("scopes") or []):
        say("Read-only for now. On day 3 add gmail.compose to the mailbox scopes and run this again with --force.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aa.py gmail-login", description="One-time Gmail login for one mailbox.")
    ap.add_argument("mailbox", nargs="?", default="ticketmail")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    from twin_agent.config import load_config
    store, has, get = _vault()
    return login(args.mailbox, load_config(), store=lambda n, v: store(n, v, caller="gmail_login"),
                 has=has, get=get, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
