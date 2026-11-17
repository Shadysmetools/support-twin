"""Tiny shared WSGI plumbing for the admin app and the launcher app: Request, Response, security headers."""
from __future__ import annotations

import html
import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from . import proxy

MAX_BODY = 64 * 1024
HSTS = "max-age=31536000"
BASE_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
STATUS_TEXT = {200: "OK", 303: "See Other", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
               405: "Method Not Allowed", 413: "Payload Too Large", 415: "Unsupported Media Type",
               429: "Too Many Requests", 500: "Internal Server Error"}


def esc(value) -> str:
    """HTML-escape (quotes included, so attribute values are safe too)."""
    return html.escape(str(value if value is not None else ""), quote=True)


class Request:
    def __init__(self, environ: dict, trusted=(), *, max_body: int = MAX_BODY, json_body: bool = False):
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.query = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        self.ip = proxy.client_ip(environ, trusted)
        self.https = proxy.is_https(environ, trusted)
        self.spoofed_proxy = proxy.untrusted_proxy_headers(environ, trusted)
        self.host = environ.get("HTTP_HOST", "")
        self.access_email = str(environ.get("aa.access_email") or "")   # set by the Cloudflare Access gate, never by a client
        self.origin = environ.get("HTTP_ORIGIN", "")
        self.ctype = environ.get("CONTENT_TYPE", "")
        self.cookies = {k: m.value for k, m in SimpleCookie(environ.get("HTTP_COOKIE", "")).items()}
        self.form: dict[str, str] = {}
        self.json: dict | None = None
        self.too_big = self.bad_body = False
        if self.method == "POST":
            self._read(environ, max_body, json_body)

    def _read(self, environ: dict, max_body: int, json_body: bool) -> None:
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            n = max_body + 1
        if n > max_body or n < 0:
            self.too_big = True
            return
        raw = environ["wsgi.input"].read(n) if n else b""
        if json_body:
            try:
                body = json.loads(raw)
            except (ValueError, RecursionError):        # 15k nested '[' must be a 400, not a traceback
                body = None
            self.json, self.bad_body = (body, False) if isinstance(body, dict) else (None, True)
        else:
            self.form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}


class Response:
    def __init__(self, status: int = 200, body: str | bytes = "", ctype: str = "text/html; charset=utf-8",
                 csp: str = BASE_CSP):
        self.status = status
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = [("Content-Type", ctype), ("Content-Security-Policy", csp), ("X-Frame-Options", "DENY"),
                        ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                        ("Cache-Control", "no-store")]

    def cookie(self, name: str, value: str, *, secure: bool = False, max_age: int | None = None) -> None:
        parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if secure:
            parts.append("Secure")
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        self.headers.append(("Set-Cookie", "; ".join(parts)))

    def secure_cookies(self) -> None:
        """Add ``Secure`` to every cookie that lacks it (HTTPS request, or the config demands it)."""
        self.headers = [(k, v + "; Secure" if k == "Set-Cookie" and "; Secure" not in v else v) for k, v in self.headers]

    def hsts(self) -> None:
        self.headers.append(("Strict-Transport-Security", HSTS))


def redirect(location: str) -> Response:
    r = Response(303, "")
    r.headers.append(("Location", location))
    return r
