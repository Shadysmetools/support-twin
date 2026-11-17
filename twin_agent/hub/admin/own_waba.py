"""Acme' OWN WhatsApp Business Account as a separate, READ-ONLY source for the admin pages (Meta App Review video).

It is NOT an Embedded Signup business: its system-user token is read from its own file, never written to the
``BusinessStore`` and never mixed with it. Only GET calls are made (the WABA, its numbers, subscribed apps, templates).
The entry is labelled ``Acme (own number)``; the token is never rendered, logged or put in an error message.
Off unless the staging config carries an ``own_waba`` block (see ``docs/STAGING-RUNNER.md``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import GRAPH_VERSION
from .graph import GraphError, LocalRefusal
from .graph_real import _body, _get_with, _rows, make_client
from .meta_store import valid_id

OWN_SLUG = "acme-own"                     # the ``business`` key of the entry: not a valid ES slug choice for writes
OWN_LABEL = "Acme (own number)"
_TOKEN = re.compile(r"[!-~]+")                  # printable ASCII, no whitespace
from ... import profile as _profile
EXECUTIVE_DIGITS = str(_profile.get("protected_numbers")[0])                 # same number the staging rails refuse; filtered out of this view (writes stay refused)


def read_token(path: str) -> str:
    """The system-user token from a JSON file (``system_user_token``) or a one-line text file. Fixed error text only.
    A BOM is tolerated; text that looks like JSON but does not parse, or a token that is not plain ASCII without
    whitespace, is refused (it would otherwise reach the HTTP library as a bearer value)."""
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        raise GraphError("The own-number token file could not be read.") from None
    if raw[:1] in ("{", "["):
        try:
            d = json.loads(raw)
        except ValueError:
            d = None
        raw = str(d.get("system_user_token") or "") if isinstance(d, dict) else ""
    if not raw or not _TOKEN.fullmatch(raw):
        raise GraphError("The own-number token file holds no usable token.")
    return raw


def _digits(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


class OwnWabaReader:
    """GET-only reader for ONE configured WABA. Any other WABA id is refused before a request is made."""

    def __init__(self, waba_id: str, token_file: str, *, app_id: str = "", label: str = OWN_LABEL, client=None,
                 version: str = GRAPH_VERSION):
        if not valid_id(waba_id):
            raise ValueError("own_waba.waba_id is not a valid id")
        self.waba_id, self.token_file, self.app_id = waba_id, token_file, str(app_id)
        self.label, self.version, self._client = label or OWN_LABEL, version, client

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def _get(self, path: str, **params) -> dict:
        return _get_with(self.client, path, read_token(self.token_file), self.version, **params)

    def _check(self, waba_id: str) -> None:
        if waba_id != self.waba_id:
            raise LocalRefusal("That account is not the configured own account.")

    def entry(self) -> dict:
        """The Connected-numbers entry. Never raises: a failure becomes an ``error`` entry (the others stay visible)."""
        base = {"business": OWN_SLUG, "own": True, "read_only": True, "id": self.waba_id, "name": self.label,
                "selected_phone": "", "phone_numbers": []}
        try:
            return {**base, "phone_numbers": self._numbers()}
        except GraphError as e:
            return {**base, "id": "", "error": str(e)}
        except Exception:                             # noqa: BLE001 - never crash the other businesses' pages
            return {**base, "id": "", "error": "The own number could not be read."}

    def _numbers(self) -> list[dict]:
        info = self._get(self.waba_id, fields="id")
        if str(info.get("id", "")) != self.waba_id:
            raise GraphError("Meta answered for a different account: refused.")
        rows = [n for n in _rows(self._get(f"{self.waba_id}/phone_numbers", limit=25,
                                           fields="id,display_phone_number,verified_name,status,quality_rating"))
                if isinstance(n, dict)]
        rows = [n for n in rows if EXECUTIVE_DIGITS not in _digits(n.get("display_phone_number"))]   # hidden, not an error
        subscribed = self._subscribed()
        return [{"id": str(n.get("id", "")), "display_phone_number": str(n.get("display_phone_number", "")),
                 "verified_name": str(n.get("verified_name", "")), "status": str(n.get("status", "")),
                 "quality_rating": str(n.get("quality_rating", "")), "webhooks_subscribed": subscribed} for n in rows]

    def _subscribed(self) -> bool:
        apps = _rows(self._get(f"{self.waba_id}/subscribed_apps"))
        return any(isinstance(a, dict) and str((a.get("whatsapp_business_api_data") or {}).get("id", "")) == self.app_id
                   for a in apps)

    def templates(self, waba_id: str) -> list[dict]:
        self._check(waba_id)
        rows = _rows(self._get(f"{self.waba_id}/message_templates", limit=100,
                               fields="name,language,category,status,components"))
        return [{"name": str(t.get("name", "")), "language": str(t.get("language", "")),
                 "category": str(t.get("category", "")), "status": str(t.get("status", "")), "body": _body(t)}
                for t in rows if isinstance(t, dict)]


class WithOwnWaba:
    """A GraphClient that adds the own-number entry to another client's list. Reads of the own WABA go to the reader;
    every write is refused for it (it is read-only), and the ES businesses' paths are untouched."""

    def __init__(self, inner, own: OwnWabaReader):
        self.inner, self.own = inner, own

    def _is_own(self, waba_id: str, business: str) -> bool:
        return business == OWN_SLUG or waba_id == self.own.waba_id

    def list_wabas(self) -> list[dict]:
        try:
            base = self.inner.list_wabas()
        except GraphError as e:                       # the own entry must not vanish because the others failed
            base = [{"business": "", "id": "", "name": "Embedded Signup businesses", "phone_numbers": [],
                     "error": str(e), "selected_phone": ""}]
        return [*base, self.own.entry()]

    def list_templates(self, waba_id: str, business: str = "") -> list[dict]:
        if business == OWN_SLUG:
            return self.own.templates(waba_id)
        if waba_id == self.own.waba_id:               # an ES business may never read the own WABA (nor the reverse)
            raise LocalRefusal("That account is not valid.")
        return self.inner.list_templates(waba_id, business)

    def subscribe_app(self, waba_id: str, business: str = "") -> None:
        if self._is_own(waba_id, business):
            raise LocalRefusal("The own number is read-only here.")
        self.inner.subscribe_app(waba_id, business)

    def start_sync(self, phone_number_id: str, sync_type: str, business: str = "") -> None:
        if business == OWN_SLUG:
            raise LocalRefusal("The own number is read-only here.")
        self.inner.start_sync(phone_number_id, sync_type, business)
