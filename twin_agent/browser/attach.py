"""Attach to the Digital Twin browser over CDP: open NEW tabs only, close them, never touch the others.

`CDP(port=9334)` talks to Chrome's HTTP endpoints (`/json/version`, `/json/new`, `/json/close/<id>`, `/json/list`).
- Host is fixed to loopback (127.0.0.1 / localhost / ::1); anything else is refused before any connection.
- `new_tab(url)` remembers the tab id; `close_tab` closes only tabs this object opened; `tab(url)` is a context
  manager that always closes its tab.
- `check_method(m)` is the denylist for raw CDP commands sent by our helpers: no cookie or storage export, no
  attaching to other targets, no closing the browser.
- Playwright users: `playwright_page(url)` connects with `connect_over_cdp`, opens a new page in the existing
  (logged-in) context and closes that page afterwards. Never call `context.cookies()` / `storage_state()`.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable
from urllib.parse import quote, urlsplit

LOOPBACK = ("127.0.0.1", "localhost", "::1")
DENY_METHODS = frozenset({"Network.getAllCookies", "Network.getCookies", "Storage.getCookies",
                          "Network.setCookies", "Storage.setCookies", "Network.clearBrowserCookies",
                          "Storage.clearDataForOrigin", "Target.attachToTarget", "Target.attachToBrowserTarget",
                          "Target.exposeDevToolsProtocol", "Browser.close", "Browser.crash",
                          "Page.captureSnapshot", "Storage.getStorageKeyForFrame"})
DENY_PREFIX = ("Network.getCookie", "Storage.get", "Browser.setDownloadBehavior", "SystemInfo.")
ALLOWED_SCHEMES = ("https", "http", "about")


class CDPRefused(PermissionError):
    pass


ODOO_HOST_SUFFIXES = (".odoo.com",)
from .. import profile as _profile
ODOO_HOSTS = frozenset({_profile.get("helpdesk_host"), "odoo.com"})


def is_flow_tenant(host: str | None) -> bool:
    """The Globex Odoo tenant (production AND stagelatest) shares its LIVE Flow warehouse + Shopify keys, so a
    single click there can push a real fulfilment / order (Odoo safety review 2026-11-04). Hard-coded here and
    NOT clearable by config: the browser may never open it, whatever a config file says."""
    h = (host or "").lower()
    return "odoo" in h and any(k.lower() in h for k in _profile.get("banned_tenant_keywords", []))


def is_odoo_host(host: str | None) -> bool:
    """True for any Odoo host the twin must reach by API / JSON-RPC ONLY, never a browser (S7): Odoo Online
    (`*.odoo.com`) and the Acme production Odoo (`helpdesk.example.com`). Hard-coded and NOT clearable by config -
    the CDP attach layer and the login broker both refuse these, so Odoo-by-API-only is guaranteed in code and does
    not depend on a denylist or on `proof.capture_base` being left empty. (The Globex Odoo.sh tenant is covered
    separately and more strongly by `is_flow_tenant`; the generic Odoo.sh devops console is not banned here.)"""
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return False
    return h in ODOO_HOSTS or h.endswith(ODOO_HOST_SUFFIXES)


def check_method(method: str) -> None:
    if method in DENY_METHODS or method.startswith(DENY_PREFIX):
        raise CDPRefused(f"CDP method {method} is not allowed (no cookie / storage export, no other targets)")


def check_url(url: str) -> str:
    u = urlsplit(url)
    if "\\" in str(url) or "@" in u.netloc:
        # A backslash or a userinfo '@' lets urlsplit and the browser disagree on the real host
        # (e.g. https://helpdesk.example.com\@sandbox.example.org -> urlsplit sees sandbox, Chrome navigates to
        # helpdesk.example.com), which would slip an Odoo host past the ban below. Refuse outright.
        raise CDPRefused("refusing a URL with a backslash or a userinfo '@' (host-spoofing risk)")
    if u.scheme not in ALLOWED_SCHEMES or (u.scheme == "http" and u.hostname not in LOOPBACK):
        raise CDPRefused("only https pages (or about:blank / loopback http) may be opened")
    if is_flow_tenant(u.hostname):
        raise CDPRefused(f"host {u.hostname!r} is hard-banned: the Globex Flow-shared Odoo tenant (a click "
                         "there can push a real warehouse fulfilment / Shopify order)")
    if is_odoo_host(u.hostname):
        raise CDPRefused(f"host {u.hostname!r} is an Odoo host: the twin reaches Odoo by API / JSON-RPC ONLY, "
                         "never a browser (S7). Use the Odoo adapter, not the CDP/Playwright path.")
    return url


def _urlopen(req, timeout):                                    # pragma: no cover - real network, lazy
    import urllib.request
    return urllib.request.urlopen(req, timeout=timeout)       # noqa: S310 - loopback only, checked


class CDP:
    def __init__(self, port: int = 9334, host: str = "127.0.0.1", *, opener: Callable | None = None,
                 timeout: float = 5.0):
        if host not in LOOPBACK:
            raise CDPRefused(f"CDP host must be loopback, not {host!r}")
        if not 1024 <= int(port) <= 65535:
            raise CDPRefused("bad CDP port")
        self.base, self.timeout = f"http://{host}:{int(port)}", timeout
        self.opener = opener or _urlopen
        self.owned: set[str] = set()

    def _http(self, method: str, path: str) -> Any:
        import urllib.request
        req = urllib.request.Request(self.base + path, method=method)
        with self.opener(req, self.timeout) as r:
            body = r.read().decode("utf-8") or "null"
        try:
            return json.loads(body)
        except ValueError:
            return body

    def version(self) -> dict:
        return self._http("GET", "/json/version")

    def endpoint(self) -> str:
        """For Playwright connect_over_cdp / ShipShape: the loopback HTTP endpoint (never a public address)."""
        return self.base

    def new_tab(self, url: str = "about:blank") -> dict:
        tab = self._http("PUT", "/json/new?" + quote(check_url(url), safe=":/?&=#%"))
        self.owned.add(str(tab["id"]))
        return tab

    def close_tab(self, tab_id: str) -> None:
        if str(tab_id) not in self.owned:
            raise CDPRefused("refusing to close a tab this helper did not open")
        self._http("GET", f"/json/close/{tab_id}")
        self.owned.discard(str(tab_id))

    def own_tabs(self) -> list[dict]:
        return [t for t in (self._http("GET", "/json/list") or []) if str(t.get("id")) in self.owned]

    def close_all(self) -> None:
        for tid in list(self.owned):
            try:
                self.close_tab(tid)
            except Exception:                                  # noqa: BLE001 - already gone
                self.owned.discard(tid)

    @contextmanager
    def tab(self, url: str = "about:blank"):
        t = self.new_tab(url)
        try:
            yield t
        finally:
            try:
                self.close_tab(str(t["id"]))
            except Exception:                                  # noqa: BLE001
                pass


@contextmanager
def playwright_page(url: str, *, port: int = 9334):            # pragma: no cover - needs a running Chrome
    """A NEW page in the Digital Twin profile's existing context; closed afterwards."""
    from playwright.sync_api import sync_playwright
    check_url(url)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            page.goto(url)
            yield page
        finally:
            page.close()
