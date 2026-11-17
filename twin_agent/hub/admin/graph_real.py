"""The REAL Meta Graph client (v27.0, httpx) and the Embedded Signup code-to-token exchange.

Rules this module keeps:
* The token is read from the secrets store (``BusinessStore``) per call and travels ONLY in the ``Authorization``
  header (never the URL, so it cannot land in a proxy or access log). It is never logged, never put in an error.
* Every error message is a fixed sentence plus, at most, the numeric HTTP status and Meta's numeric error code. A response
  body is never echoed.
* Reads are GET. The two writes are single POSTs and are NEVER retried (a retry after a timeout could act twice).
  Timeouts are short and explicit. Redirects are not followed.
* httpx is an optional dependency (``pip install .[live]``); it is imported lazily, and tests inject a client built on
  ``httpx.MockTransport``, so no test touches the network.
"""
from __future__ import annotations

import json
import logging

from .config import GRAPH_VERSION
from .graph import SYNC_TYPES, AmbiguousAssets, GraphError, LocalRefusal, MetaRefused
from .meta_store import BusinessStore, valid_id, valid_slug

BASE = "https://graph.facebook.com"
MAX_BYTES = 1_000_000
UNREACHABLE = "Could not reach Meta."
TIMED_OUT = "Meta did not answer in time."
WRITE_UNCERTAIN = " The action may or may not have gone through: check in Meta before trying again."


class _NoSecretsInLogs(logging.Filter):
    """httpx logs the full URL of every request at INFO, and ``debug_token`` carries ``input_token=<token>`` in its
    query. Drop any record that mentions it (belt and braces next to the WARNING level below)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "input_token" not in record.getMessage()


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_NoSecretsInLogs())
    logging.getLogger(_name).setLevel(logging.WARNING)


def _rows(body: dict, key: str = "data") -> list:
    """A list from a Graph answer; ``null`` or any non-list becomes []."""
    v = body.get(key) if isinstance(body, dict) else None
    return v if isinstance(v, list) else []


def make_client(timeout: float = 15.0):
    """The real httpx client: no redirects, short timeouts. Imported lazily (optional dependency)."""
    try:
        import httpx
    except ImportError as e:
        raise GraphError("httpx is not installed (pip install .[live]).") from e
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=False)


def _error_code(resp) -> str:
    """'HTTP 400, code 190' from a failed response. Digits only: nothing free-form from Meta is ever shown."""
    code = ""
    try:
        err = json.loads(resp.content[:MAX_BYTES]).get("error", {})
        c = err.get("code")
        code = f", code {int(c)}" if isinstance(c, int) and not isinstance(c, bool) else ""
    except (ValueError, AttributeError, TypeError):
        pass
    return f"Meta refused the request (HTTP {int(resp.status_code)}{code})."


def _send(client, method: str, url: str, *, token: str = "", write: bool = False, **kw) -> dict:
    """One request. No retry, no redirect. Raises GraphError with a fixed message."""
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = client.request(method, url, headers=headers, **kw)
    except httpx.TimeoutException:
        raise GraphError(TIMED_OUT + (WRITE_UNCERTAIN if write else "")) from None
    except httpx.HTTPError:
        raise GraphError(UNREACHABLE + (WRITE_UNCERTAIN if write else "")) from None
    if len(resp.content) > MAX_BYTES:
        raise GraphError("Meta sent an unexpectedly large answer.")
    if resp.status_code >= 300:
        raise MetaRefused(_error_code(resp))
    try:
        body = resp.json()
    except ValueError:
        raise GraphError("Meta sent an answer this page could not read.") from None
    body = body if isinstance(body, dict) else {}
    if write and body.get("success") is False:
        raise MetaRefused("Meta did not accept the request.")
    return body


def exchange_code(code: str, *, app_id: str, app_secret: str, client, version: str = GRAPH_VERSION) -> dict:
    """Embedded Signup code to business token. POST (form body, so the secret and code are not in the URL)."""
    body = _send(client, "POST", f"{BASE}/{version}/oauth/access_token",
                 data={"client_id": app_id, "client_secret": app_secret, "code": code})
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise GraphError("Meta did not return an access token.")
    expires = body.get("expires_in")
    return {"business_token": token, "token_type": str(body.get("token_type") or "")[:32],
            "expires_in": expires if isinstance(expires, int) else None}


def _ids(items, key: str = "id") -> list[str]:
    return [str(i.get(key)) for i in items if isinstance(i, dict) and valid_id(str(i.get(key, "")))]


def verify_assets(token: str, *, app_id: str, app_secret: str, claimed_waba: str, claimed_phone: str, client,
                  version: str = GRAPH_VERSION) -> dict:
    """Which WABA and number does this token REALLY grant? Never trust the ids the browser posted.

    1. ``GET /debug_token`` (app access token in the header): the token must be valid for OUR app. A WABA is granted only
       when BOTH ``whatsapp_business_management`` and ``whatsapp_business_messaging`` target it. A claimed WABA id
       outside that set is ignored; with no usable claim exactly one granted WABA is accepted. Several and no claim raises
       ``AmbiguousAssets`` (the caller keeps the token in memory and waits for the session info); none raises GraphError.
    2. ``GET /{waba}/phone_numbers`` with the business's own token: the number id must be in that list.
    Always returns BOTH ids: ``{"waba_id", "phone_number_id"}``; the phone is "" when it cannot be verified ("pick the
    number" state), so a re-bind to another WABA can never leave an old number behind."""
    body = _send(client, "GET", f"{BASE}/{version}/debug_token", token=f"{app_id}|{app_secret}",
                 params={"input_token": token})
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if data.get("is_valid") is not True or str(data.get("app_id", "")) != str(app_id):
        raise GraphError("Meta says this access is not valid for this app.")
    both = _targets(data, "whatsapp_business_management") & _targets(data, "whatsapp_business_messaging")
    wabas = sorted(both)
    if claimed_waba in both:
        waba = claimed_waba
    elif len(wabas) == 1:
        waba = wabas[0]
    elif wabas:
        raise AmbiguousAssets("Meta did not say which of the shared accounts this sign-up is for.")
    else:
        raise GraphError("Meta did not confirm which WhatsApp Business Account was shared.")
    nums = _get_with(client, f"{waba}/phone_numbers", token, version, limit=25, fields="id")
    phones = _ids(_rows(nums))
    phone = claimed_phone if claimed_phone in phones else (phones[0] if len(phones) == 1 else "")
    return {"waba_id": waba, "phone_number_id": phone}


def _targets(data: dict, scope: str) -> set:
    out = set()
    for sc in data.get("granular_scopes") or []:
        if isinstance(sc, dict) and sc.get("scope") == scope:
            out |= {str(t) for t in sc.get("target_ids") or [] if valid_id(str(t))}
    return out


def _get_with(client, path: str, token: str, version: str, **params) -> dict:
    return _send(client, "GET", f"{BASE}/{version}/{path}", token=token, params=params)


class RealGraphClient:
    def __init__(self, businesses: BusinessStore, *, app_id: str, client=None, version: str = GRAPH_VERSION):
        self.businesses, self.app_id, self.version = businesses, str(app_id), version
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def _url(self, path: str) -> str:
        return f"{BASE}/{self.version}/{path}"

    def _get(self, path: str, token: str, **params) -> dict:
        return _send(self.client, "GET", self._url(path), token=token, params=params)

    # ------------------------------------------------------------------ reads

    def list_wabas(self) -> list[dict]:
        """One entry per business, each read with THAT business's own token. A failing business becomes an error
        entry; it never blanks the others."""
        out = []
        for slug, rec in self.businesses.records():
            waba, token = rec.get("waba_id", ""), rec.get("business_token", "")
            if not (valid_id(waba) and token):
                continue
            try:
                out.append({"business": slug, **self._waba(waba, token, slug)})
            except GraphError as e:
                out.append({"business": slug, "id": "", "name": slug, "phone_numbers": [], "error": str(e),
                            "selected_phone": ""})
        return out

    def _waba(self, waba: str, token: str, business: str = "") -> dict:
        info = self._get(waba, token, fields="id,name")
        nums = _rows(self._get(f"{waba}/phone_numbers", token, limit=25,
                               fields="id,display_phone_number,verified_name,status,quality_rating"))
        subscribed = self._subscribed(waba, token)
        return {"id": waba, "name": str(info.get("name") or ""),
                "selected_phone": self.businesses.get(business).get("phone_number_id", ""),
                "phone_numbers": [{"id": str(n.get("id", "")), "display_phone_number": str(n.get("display_phone_number", "")),
                                   "verified_name": str(n.get("verified_name", "")), "status": str(n.get("status", "")),
                                   "quality_rating": str(n.get("quality_rating", "")), "webhooks_subscribed": subscribed}
                                  for n in nums if isinstance(n, dict)]}

    def _subscribed(self, waba: str, token: str) -> bool:
        """True when OUR app is among the WABA's subscribed apps."""
        apps = _rows(self._get(f"{waba}/subscribed_apps", token))
        return any(isinstance(a, dict) and str((a.get("whatsapp_business_api_data") or {}).get("id", "")) == self.app_id
                   for a in apps)

    def list_templates(self, waba_id: str, business: str = "") -> list[dict]:
        token = self._token(business, waba_id=waba_id)
        rows = self._get(f"{waba_id}/message_templates", token, limit=100,
                         fields="name,language,category,status,components")
        rows = _rows(rows)
        return [{"name": str(t.get("name", "")), "language": str(t.get("language", "")), "category": str(t.get("category", "")),
                 "status": str(t.get("status", "")), "body": _body(t)} for t in rows if isinstance(t, dict)]

    def _token(self, business: str, *, waba_id: str = "", phone_number_id: str = "") -> str:
        """The named business's token, and only when the id is the one verified for THAT business."""
        if not valid_id(waba_id or phone_number_id) or not valid_slug(business):
            raise LocalRefusal("That account is not valid.")
        token = self.businesses.token_for(business, waba_id=waba_id, phone_number_id=phone_number_id)
        if not token:
            raise LocalRefusal("That account is not connected on this server.")
        return token

    # ------------------------------------------------------------------ writes (single attempt, never retried)

    def subscribe_app(self, waba_id: str, business: str = "") -> None:
        token = self._token(business, waba_id=waba_id)
        _send(self.client, "POST", self._url(f"{waba_id}/subscribed_apps"), token=token, write=True)

    def start_sync(self, phone_number_id: str, sync_type: str, business: str = "") -> None:
        if sync_type not in SYNC_TYPES:
            raise LocalRefusal("That sync type is not allowed.")
        token = self._token(business, phone_number_id=phone_number_id)
        _send(self.client, "POST", self._url(f"{phone_number_id}/smb_app_data"), token=token, write=True,
              json={"messaging_product": "whatsapp", "sync_type": sync_type})


def _body(template: dict) -> str:
    for c in template.get("components") or []:
        if isinstance(c, dict) and str(c.get("type", "")).upper() == "BODY":
            return str(c.get("text", ""))
    return ""
