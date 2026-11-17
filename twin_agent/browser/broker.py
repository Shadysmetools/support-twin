"""Login broker: fill a work site's login form without the model ever seeing the values.

`fill_login(page, site, cfg)` returns ONLY a short status ("filled", or why not). The username and password come
from the vault entry `site-logins` ({"<site>": {"username", "password"}}, Windows Credential Manager / DPAPI) or,
when `browser.bitwarden: true` and `bw` is on PATH, from the Bitwarden CLI (`bw get item <id>`, session from the
vault entry `bitwarden-session`). Values live in local variables only: never returned, printed, logged or written.

Site config (`browser.sites`): {name: {url_prefix, user_selector, pass_selector, submit_selector, bitwarden_item}}.
The page must already be on `url_prefix` (same scheme + host), so a look-alike page is never filled.
`page` is anything with `.url`, `.fill(selector, value)` and `.click(selector)` (a Playwright page; a fake in tests).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable
from urllib.parse import urlsplit

from .attach import is_odoo_host


def _same_site(url: str, prefix: str) -> bool:
    a, b = urlsplit(str(url or "")), urlsplit(str(prefix or ""))
    return bool(b.hostname) and a.scheme == "https" and a.scheme == b.scheme and a.hostname == b.hostname \
        and a.path.startswith(b.path or "/")


def _from_bitwarden(item: str, *, backend: Any = None, run: Callable | None = None) -> tuple[str, str] | None:
    if not item or shutil.which("bw") is None and run is None:
        return None
    from .. import creds
    env = creds.child_env({"BW_SESSION": "bitwarden-session"}, purpose="browser login", backend=backend)
    run = run or (lambda cmd, env: subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30, shell=False))
    r = run(["bw", "get", "item", item], env)
    if getattr(r, "returncode", 1) != 0:
        return None
    login = (json.loads(r.stdout or "{}").get("login") or {})
    return (str(login.get("username") or ""), str(login.get("password") or "")) if login.get("password") else None


def _from_vault(site: str, *, backend: Any = None) -> tuple[str, str] | None:
    from .. import creds
    if not creds.has_secret("site-logins", backend=backend):
        return None
    entry = (creds.get_fields("site-logins", backend=backend, caller="browser_broker", purpose=f"login {site}")
             .get(site) or {})
    return (str(entry.get("username") or ""), str(entry.get("password") or "")) if entry.get("password") else None


def fill_login(page, site: str, cfg: dict, *, backend: Any = None, bw_run: Callable | None = None) -> str:
    b = cfg.get("browser") or {}
    sc = (b.get("sites") or {}).get(site)
    if not sc:
        return f"no login config for {site}"
    if is_odoo_host(urlsplit(getattr(page, "url", "")).hostname):
        return "refused: Odoo is reached by API / JSON-RPC only, never a browser login (S7)"
    if not _same_site(getattr(page, "url", ""), sc.get("url_prefix", "")):
        return "refused: the page is not the configured login page"
    try:
        pair = None
        if b.get("bitwarden") is True:
            pair = _from_bitwarden(str(sc.get("bitwarden_item") or ""), backend=backend, run=bw_run)
        pair = pair or _from_vault(site, backend=backend)
        if not pair:
            return f"no stored login for {site}"
        page.fill(sc["user_selector"], pair[0])
        page.fill(sc["pass_selector"], pair[1])
        pair = None                                            # drop the values before anything else runs
        if sc.get("submit_selector"):
            page.click(sc["submit_selector"])
        return "filled"
    except Exception as e:                                     # noqa: BLE001 - the class only, never a value
        return f"failed: {type(e).__name__}"
