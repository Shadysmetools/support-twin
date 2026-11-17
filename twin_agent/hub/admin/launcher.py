"""The Embedded Signup launcher: a SEPARATE minimal WSGI app, meant to be served from its OWN origin (its own host name).

Why separate: the Facebook SDK (``connect.facebook.net/sdk.js``) runs with full same-origin power in whatever origin
loads it, and it cannot be pinned with SRI. Loaded in the admin origin it could ride the staff session. Here it runs
in an origin that has NO admin cookie, NO admin page and NO token to read: this app serves exactly three things, sets
no cookie, renders no stored data, and its only write is the per-business record it files in the secrets store.

* ``GET /?s=<state>``    the launcher page. The state is minted only by a logged-in admin (``POST /connect/start``).
* ``POST /callback``     session info + the Embedded Signup code, with the same checks as ``embedded-signup/server.py``:
  server-generated single-use state, ``Origin == scheme://Host``, JSON, 16 KB cap. The code is exchanged at once
  (it lives 30 seconds) and the token is filed per business. Nothing is subscribed, registered or synced here.
* ``GET /static/launcher.js|launcher.css``  exact two-name allowlist.

Nothing in this module logs. Responses are fixed sentences: never a code, token, id or Meta body.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

from . import auth, proxy
from .graph import AmbiguousAssets, GraphError
from .graph_real import exchange_code, make_client, verify_assets
from .meta_store import BusinessStore, ConflictError, SignupStates, valid_id
from .web import STATUS_TEXT, Request, Response, esc

STATIC = Path(__file__).parent / "static"
MAX_CALLBACK = 16 * 1024
LAUNCHER_CSP = ("default-src 'none'; script-src 'self' https://connect.facebook.net; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; connect-src 'self' https://*.facebook.com https://graph.facebook.com; "
                "frame-src https://*.facebook.com; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
ASSETS = {"launcher.js": "text/javascript; charset=utf-8", "launcher.css": "text/css; charset=utf-8"}
EXPIRED_CB = "Session expired. Go back to Support Twin and start again."
EXPIRED = "This link expired or was already used. Go back to Support Twin and press Connect WhatsApp again."


class LauncherApp:
    def __init__(self, *, states: SignupStates, businesses: BusinessStore, app_id: str, config_id: str,
                 graph_version: str, app_secret_file, allowed_hosts=None, trusted_proxies=(), hsts: bool = True,
                 http=None, limiter=None):
        self.states, self.businesses = states, businesses
        self.app_id, self.config_id, self.graph_version = str(app_id), str(config_id), graph_version
        self.app_secret_file = Path(app_secret_file)
        self.allowed_hosts = None if allowed_hosts is None else {proxy.normalize_host(h) for h in allowed_hosts}
        self.trusted, self.hsts, self._http = proxy.parse_networks(trusted_proxies), hsts, http
        self.limiter = limiter or auth.RateLimiter(max_fails=30, ip_max_fails=60)

    # ---------------------------------------------------------------- WSGI

    def __call__(self, environ, start_response):
        req = None
        try:
            req = Request(environ, self.trusted, max_body=MAX_CALLBACK, json_body=True)
            resp = self.handle(req)
        except Exception:                                   # noqa: BLE001 - never leak a traceback
            resp = Response(500, "Something went wrong. Please tell Acme.", "text/plain; charset=utf-8", LAUNCHER_CSP)
        if req is not None and req.https:
            resp.secure_cookies()
            if self.hsts:
                resp.hsts()
        start_response(f"{resp.status} {STATUS_TEXT.get(resp.status, 'Error')}", resp.headers)
        return [resp.body]

    def handle(self, req: Request) -> Response:
        if not proxy.host_allowed(req.host, self.allowed_hosts):
            return self._text(400, "Bad host")
        if req.spoofed_proxy:
            return self._text(400, "Proxy headers from an untrusted peer")
        if req.too_big:
            return self._text(413, "Too large")
        if req.method == "GET" and req.path.startswith("/static/"):
            name = req.path[len("/static/"):]
            return (Response(200, (STATIC / name).read_bytes(), ASSETS[name], LAUNCHER_CSP) if name in ASSETS
                    else self._text(404, "Not found"))
        if req.method == "GET" and req.path == "/":
            return self.page(req)
        if req.method == "POST" and req.path == "/callback":
            return self.callback(req)
        return self._text(404, "Not found")

    @staticmethod
    def _text(status: int, body: str) -> Response:
        return Response(status, body, "text/plain; charset=utf-8", LAUNCHER_CSP)

    # ---------------------------------------------------------------- page

    def page(self, req: Request) -> Response:
        state = req.query.get("s", "")
        business = self.states.peek(state)
        ready = bool(business and self.app_id and self.config_id)
        note = "" if ready else f'<p class="bad">{esc(EXPIRED if not business else "Embedded Signup is not configured.")}</p>'
        body = ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<title>Acme - Connect WhatsApp</title><link rel=\"stylesheet\" href=\"/static/launcher.css\"></head><body>"
                "<h1>Connect your WhatsApp Business number to Support Twin</h1>"
                "<p>Press the button, log in to Facebook, choose <b>Connect your existing WhatsApp Business app</b>, pick "
                "the number, then follow the steps on your phone in the WhatsApp Business app.</p>"
                f"{note}<button id=\"go\" data-app-id=\"{esc(self.app_id)}\" data-config-id=\"{esc(self.config_id)}\" "
                f"data-graph=\"{esc(self.graph_version)}\" data-state=\"{esc(state if ready else '')}\""
                f"{'' if ready else ' disabled'}>Connect WhatsApp</button><p id=\"status\" role=\"status\"></p>"
                "<script src=\"/static/launcher.js\"></script>"
                "<script async defer crossorigin=\"anonymous\" src=\"https://connect.facebook.net/en_US/sdk.js\"></script>"
                "</body></html>")
        return Response(200, body, csp=LAUNCHER_CSP)

    # ---------------------------------------------------------------- callback

    def callback(self, req: Request) -> Response:
        scheme = "https" if req.https else "http"
        if req.origin != f"{scheme}://{req.host}":
            return self._text(403, "Forbidden")
        if not req.ctype.startswith("application/json"):
            return self._text(415, "Unsupported")
        if self.limiter.blocked(req.ip, "callback"):
            return self._text(429, "Too many requests")
        self.limiter.fail(req.ip, "callback")               # every call counts: the callback is not a hot path
        p = req.json
        if req.bad_body or p is None or p.get("kind") not in ("code", "session"):
            return self._text(400, "Bad request")
        state = p.get("state")
        lock = self.states.lock_for(state)
        if lock is None:
            return self._text(403, EXPIRED_CB)
        with lock:                                          # the code and the session info are two racing browser posts
            business = self.states.take(state, p["kind"], str(p.get("event", "")))
            if business is None:
                return self._text(403, EXPIRED_CB)
            if p["kind"] == "session":
                return self._session(state, business, p)
            return self._code(state, business, p.get("code"))

    def _session(self, state: str, business: str, p: dict) -> Response:
        """What Meta's postMessage reports. The ids are the BROWSER's claim: digits only, stored under ``claimed_*`` and
        never used to route a token. May arrive several times (cancel, then try again). If the code's token is waiting in
        memory (it could not be bound without a claim), bind it now."""
        event = str(p.get("event", ""))[:64]
        rec = {"last_event": event}
        for k in ("waba_id", "phone_number_id", "business_id"):
            v = p.get(k)
            if valid_id(str(v) if v else ""):
                rec["claimed_" + k] = str(v)
        drop = () if event.upper() == "CANCEL" else ("cancel_step", "error_code")    # a retry clears the old cancel
        if p.get("current_step"):
            rec["cancel_step"] = str(p["current_step"])[:64]
        if p.get("error_code"):
            rec["error_code"] = str(p["error_code"])[:32]
        self.businesses.save(business, rec, remove=drop if not (p.get("current_step") or p.get("error_code")) else ())
        token = self.states.token(state)
        if token and event.upper() != "CANCEL":
            return self._bind(state, business, token)
        return self._text(200, "OK")

    def _secret(self) -> str:
        try:
            d = json.loads(self.app_secret_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        ok = isinstance(d, dict) and str(d.get("app_id", "")) == self.app_id
        return str(d.get("app_secret", "")) if ok else ""

    def _client(self):
        return contextlib.nullcontext(self._http) if self._http else make_client(20.0)   # a client we make, we close

    def _bind(self, state: str, business: str, token: str, extra: dict | None = None) -> Response:
        """Verify with Graph which WABA / number the token grants, then store ONLY those ids (+ the token), always both
        ids together (the phone is "" until verified). An undecidable WABA keeps the token in memory on the state."""
        secret = self._secret()
        if not (self.app_id and secret):
            return self._text(400, "Something went wrong. Please tell Acme.")
        claims = self.businesses.get(business)
        try:
            with self._client() as http:
                ids = verify_assets(token, app_id=self.app_id, app_secret=secret, client=http, version=self.graph_version,
                                    claimed_waba=claims.get("claimed_waba_id", ""),
                                    claimed_phone=claims.get("claimed_phone_number_id", ""))
        except AmbiguousAssets:
            self.states.set_token(state, token)             # memory only, until the session info arrives or the state expires
            return self._text(200, "Almost done. Finish the steps in the Meta window. If this stays, start again from Support Twin.")
        except GraphError:
            self.states.set_token(state, None)
            return self._text(400, "Something went wrong. Please tell Acme.")
        try:
            self.businesses.save(business, {**(extra or {}), "business_token": token, **ids})
        except ConflictError:
            self.states.set_token(state, None)
            return self._text(409, "That WhatsApp account is already connected under another business name.")
        if ids["phone_number_id"]:
            self.states.finish(state)
            return self._text(200, "Connected. You can close this page.")
        self.states.set_token(state, token)                 # number still unverified: a later claim may settle it
        return self._text(200, "Connected. Open Connected numbers in Support Twin and pick the number.")

    def _code(self, state: str, business: str, code) -> Response:
        if not isinstance(code, str) or not code or len(code) > 4096:
            return self._text(400, "Bad request")
        secret = self._secret()
        if not (self.app_id and secret):
            return self._text(400, "Something went wrong. Please tell Acme.")
        try:
            with self._client() as http:
                token = exchange_code(code, app_id=self.app_id, app_secret=secret, client=http, version=self.graph_version)
        except GraphError:
            return self._text(400, "Something went wrong. Please tell Acme.")
        return self._bind(state, business, token.pop("business_token"), token)
