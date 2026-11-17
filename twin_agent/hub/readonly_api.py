"""A READ-ONLY JSON API on the production admin server (127.0.0.1:8891), under ``/api/v1/``.

For the One desktop app: live tickets and pending approvals. GET only; approving stays on the admin page and in Teamhub.

* Auth: ``Authorization: Bearer <token>``, the token read from ``~/.claude/secrets/aa-prod-api-token.txt``, compared in
  constant time. The API never looks at the admin cookie; the admin pages never look at the token (this gate hands them
  every non-``/api`` request untouched).
* Loopback only: a request that came through the tunnel (``CF-Connecting-IP`` / ``CF-Ray`` / ``X-Forwarded-For``), a
  non-loopback peer, or a Host other than 127.0.0.1 / localhost gets the same 404 as an unknown page. Remote laptops come
  later via a Cloudflare Access service token.
* No token file (or a short one) = the API is off (404). Fail closed.
* About 20 requests per second per process (burst 30), then 429. The token is checked FIRST, so a caller without it
  can never use up the bucket.
* Data: the hub's own store through ``admin.data``; no Odoo call per request. No phone numbers, emails or message ids;
  the customer's display name is included for the approver (internal, same as on the admin pages).
* The token file is written atomically. Its 0600 mode does nothing on Windows: the ACL on the secrets folder is the real
  protection there.
* Port 8891 must only ever be reached through cloudflared over plain HTTP from this machine; never expose it directly.

``python -m twin_agent.hub.production api-token`` writes a fresh random token (and prints nothing).
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from . import readonly_api_views as views
from .staging import SECRETS_DIR

DEFAULT_TOKEN_FILE = SECRETS_DIR / "aa-prod-api-token.txt"
MIN_TOKEN_LEN = 32
RATE_PER_SECOND, BURST = 20, 30
LOOPBACK_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
TUNNEL_HEADERS = ("HTTP_CF_CONNECTING_IP", "HTTP_CF_RAY", "HTTP_X_FORWARDED_FOR")
STATUS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
          429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}


def write_token(path: str | os.PathLike = DEFAULT_TOKEN_FILE) -> None:
    """A fresh random 32-byte token, 0600, replacing any old one. Prints nothing and returns nothing: the value is never shown."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secrets.token_urlsafe(32))
    os.replace(tmp, p)                           # atomic: a reader sees the old token or the new one, never a half file


class TokenFile:
    """The token, re-read when the file changes (so ``api-token`` rotates it without a restart). Empty = API off."""

    def __init__(self, path: str | os.PathLike):
        self.path, self._mtime, self._value = Path(path), None, b""

    def value(self) -> bytes:
        try:
            m = self.path.stat().st_mtime_ns
            if m != self._mtime:
                tok = self.path.read_text(encoding="utf-8").strip()
                self._mtime, self._value = m, (tok.encode() if len(tok) >= MIN_TOKEN_LEN else b"")
        except OSError:
            self._mtime, self._value = None, b""
        return self._value


class Bucket:
    """A token bucket: ``rate`` requests per second, up to ``burst`` at once. One per process."""

    def __init__(self, rate: float = RATE_PER_SECOND, burst: float = BURST, clock=time.monotonic):
        self.rate, self.burst, self.clock, self._lock = float(rate), float(burst), clock, threading.Lock()
        self._tokens, self._at = self.burst, clock()

    def take(self) -> bool:
        with self._lock:
            now = self.clock()
            self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
            self._at = now
            if self._tokens < 1:
                return False
            self._tokens -= 1
            return True


def _json(status: int, body: dict, extra=()) -> tuple[int, list, bytes]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = [("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
               ("Content-Length", str(len(raw))), *extra]
    return status, headers, raw


def _err(status: int, code: str, extra=()):
    return _json(status, {"error": code}, extra)


def _is_local(environ: dict) -> bool:
    """Loopback peer, no tunnel header, and Host 127.0.0.1 / localhost (any port)."""
    if any(environ.get(h) for h in TUNNEL_HEADERS) or environ.get("REMOTE_ADDR", "") not in LOOPBACK_PEERS:
        return False
    host = str(environ.get("HTTP_HOST") or "").strip().lower().rstrip(".")
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in LOOPBACK_HOSTS


class ApiGate:
    """WSGI wrapper: ``/api`` and ``/api/...`` are served here, everything else goes to the admin app unchanged."""

    def __init__(self, app, hub, admin_cfg, *, admin_host: str, token_file=None, health=None, bucket=None):
        self._app, self.hub, self.cfg = app, hub, admin_cfg
        self.admin_base = f"https://{admin_host}" if admin_host else ""     # "" = admin not public: no approve links
        self.token, self.bucket, self.health = TokenFile(token_file or DEFAULT_TOKEN_FILE), bucket or Bucket(), health

    def __getattr__(self, name):                 # build_admin's callers read ``.cfg`` etc. through the wrapper chain
        return getattr(self._app, name)

    def set_health(self, fn) -> None:
        """``fn()`` returns what ``/healthz`` returns (the webhook server's ``health``). Wired by ``cmd_serve``."""
        self.health = fn

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/") or "/"
        if path != "/api" and not path.startswith("/api/"):
            return self._app(environ, start_response)
        try:
            status, headers, body = self._serve(environ, path)
        except Exception:                        # noqa: BLE001 - never leak a traceback
            status, headers, body = _err(500, "internal_error")
        start_response(f"{status} {STATUS.get(status, 'Error')}", headers)
        return [body]

    def _serve(self, environ: dict, path: str):
        tok = self.token.value()
        if not tok or not _is_local(environ):
            return _err(404, "not_found")
        sent = str(environ.get("HTTP_AUTHORIZATION") or "")
        given = sent[7:].strip().encode() if sent[:7].lower() == "bearer " else b""
        if not hmac.compare_digest(given, tok):
            return _err(401, "unauthorized", [("WWW-Authenticate", "Bearer")])
        if not self.bucket.take():
            return _err(429, "rate_limited", [("Retry-After", "1")])
        if environ.get("REQUEST_METHOD", "GET").upper() != "GET":
            return _err(405, "method_not_allowed", [("Allow", "GET")])
        return self._route(path, parse_qs(environ.get("QUERY_STRING", "")))

    def _route(self, path: str, q: dict):
        if path == "/api/v1/health":
            return self._health()
        if path == "/api/v1/tickets":
            return self._tickets(q)
        if path == "/api/v1/approvals/pending":
            return _json(200, views.approvals_pending(self.hub, self.cfg, self.admin_base))
        if path.startswith("/api/v1/tickets/"):
            tail = path[len("/api/v1/tickets/"):]
            state = self.hub.ticket(int(tail)) if tail.isascii() and tail.isdigit() else None
            if state is None:
                return _err(404, "not_found")
            return _json(200, views.ticket_detail(self.hub, state, self.cfg, self.admin_base))
        return _err(404, "not_found")

    def _health(self):
        if self.health is None:
            return _err(503, "health_unavailable")
        return _json(200, self.health())

    def _tickets(self, q: dict):
        status = (q.get("status") or ["open"])[0]
        raw = (q.get("limit") or [str(views.DEFAULT_LIMIT)])[0]
        if status not in ("open", "all") or not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= views.MAX_LIMIT:
            return _err(400, "bad_query")
        return _json(200, views.ticket_list(self.hub, self.cfg, open_only=status == "open", limit=int(raw)))
