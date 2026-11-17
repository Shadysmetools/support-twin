"""Guarded Odoo adapter for helpdesk.example.com (Odoo 18), as Principal (his own API key from the vault).

`OdooAdapter(transport, writes_on=...)` runs every call through `guard.check` first, then the transport.
- `transport.execute_kw(model, method, args, kwargs)` and `transport.uid` (the logged-in user id). Real:
  `JsonRpcTransport` (stdlib urllib, JSON-RPC /jsonrpc). Tests: `twin_agent.testing.fake_odoo.FakeOdoo`.
- `writes_on=False` (the default) refuses every write even after the guard passed: defence in depth under
  OBSERVE. The desks record "prepared" writes themselves and never call the adapter for them.
- `open_odoo(cfg)` refuses unless `run_mode: live`, `odoo_sys.url` / `odoo_sys.db` are set and the vault holds
  `odoo-sys-principal` ({"login", "api_key"}). The key stays inside the transport object; nothing prints or logs it.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from . import guard


class OdooUnavailable(RuntimeError):
    """The adapter cannot start (not live, no config, no vault entry). The message never holds a value."""


class Transport(Protocol):
    uid: int

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict) -> Any: ...


class OdooAdapter:
    def __init__(self, transport: Transport, *, writes_on: bool = False):
        self.t, self.writes_on = transport, bool(writes_on)

    @property
    def uid(self) -> int:
        return int(getattr(self.t, "uid", 0) or 0)

    def call(self, purpose: str, model: str, method: str, args: list | None = None, kwargs: dict | None = None, *,
             safe_stages=(), gated_stage: int | None = None, notify_partner: int | None = None) -> Any:
        guard.check(purpose, model, method, args, kwargs, safe_stages=safe_stages, gated_stage=gated_stage,
                    notify_partner=notify_partner)
        if guard.is_write(model, method) and not self.writes_on:
            raise guard.WriteRefused("observe: Odoo writes are off (write_enabled false or not live)")
        return self.t.execute_kw(model, method, list(args or []), dict(kwargs or {}))

    # ---------------------------------------------------------------- reads (purpose "read")

    def search_read(self, model: str, domain: list, fields: list[str], *, limit: int = 0, order: str = "") -> list:
        kw: dict = {"fields": fields}
        if limit:
            kw["limit"] = limit
        if order:
            kw["order"] = order
        return self.call("read", model, "search_read", [domain], kw) or []

    def read(self, model: str, ids: list[int], fields: list[str]) -> list:
        return self.call("read", model, "read", [ids], {"fields": fields}) or []

    def fields_get(self, model: str) -> dict:
        return self.call("read", model, "fields_get", [], {"attributes": ["type", "relation", "selection",
                                                                          "string", "readonly"]}) or {}


class JsonRpcTransport:
    """Odoo external API over JSON-RPC (POST <url>/jsonrpc). Lazy: nothing connects until the first call."""

    def __init__(self, url: str, db: str, login: str, api_key: str, *, timeout: float = 30.0):
        self.url, self.db, self._login, self._key, self.timeout = url.rstrip("/"), db, login, api_key, timeout
        self.uid = 0

    def __repr__(self) -> str:                         # never show the key
        return f"JsonRpcTransport({self.url!r}, db={self.db!r}, uid={self.uid})"

    def _rpc(self, service: str, method: str, args: list) -> Any:
        import urllib.request
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "id": 1,
                           "params": {"service": service, "method": method, "args": args}}).encode()
        req = urllib.request.Request(self.url + "/jsonrpc", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:   # noqa: S310 - fixed https URL from config
            data = json.loads(r.read().decode("utf-8"))
        if data.get("error"):
            err = data["error"].get("data", {}).get("name") or data["error"].get("message") or "error"
            raise RuntimeError(f"odoo {service}.{method}: {err}")      # the error class only, no payload
        return data.get("result")

    def authenticate(self) -> int:
        """Log in now (idempotent) and return the uid. ``execute_kw`` calls this lazily, so ``uid`` is 0 until the first
        call; code that needs our own uid up front must call this first."""
        if not self.uid:
            self.uid = int(self._rpc("common", "authenticate", [self.db, self._login, self._key, {}]) or 0)
            if not self.uid:
                raise OdooUnavailable("Odoo login refused (check the odoo-sys-principal vault entry)")
        return self.uid

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict) -> Any:
        self.authenticate()
        return self._rpc("object", "execute_kw", [self.db, self.uid, self._key, model, method, args, kwargs])


def writes_allowed(cfg: dict, section: str) -> bool:
    """Writes need run_mode live AND `<section>.write_enabled: true`. `--mode observe` forces the flag off."""
    return cfg.get("run_mode") == "live" and ((cfg.get(section) or {}).get("write_enabled") is True)


def open_odoo(cfg: dict, *, section: str = "helpdesk", backend: Any = None) -> OdooAdapter:
    if cfg.get("run_mode") != "live":
        raise OdooUnavailable("Odoo adapter is off unless run_mode is live")
    o = cfg.get("odoo_sys") or {}
    if not o.get("url") or not o.get("db"):
        raise OdooUnavailable("odoo_sys.url and odoo_sys.db must be set")
    from .. import creds
    if not creds.has_secret("odoo-sys-principal", backend=backend):
        raise OdooUnavailable("vault entry odoo-sys-principal is missing (aa.py creds set odoo-sys-principal)")
    f = creds.get_fields("odoo-sys-principal", backend=backend, caller="odoo_sys", purpose=section)
    t = JsonRpcTransport(str(o["url"]), str(o["db"]), str(f.get("login") or ""), str(f.get("api_key") or ""))
    return OdooAdapter(t, writes_on=writes_allowed(cfg, section))
