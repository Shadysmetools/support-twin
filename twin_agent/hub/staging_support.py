"""Staging helpers kept out of staging.py (file size): the Access gate and the dry-run Odoo transport."""
from __future__ import annotations

import logging

from ..helpdesk import guard

_log = logging.getLogger("twin_agent.hub.staging")


class AccessGate:
    """DEFENCE IN DEPTH ONLY, not an authentication control. With ``public_admin``: a request that came through the
    tunnel (it carries ``CF-Connecting-IP``) must also carry a ``Cf-Access-Jwt-Assertion`` header, else 403, so a tunnel
    route that bypasses Access by mistake is noticed. It checks that the header is PRESENT; it does NOT verify the JWT
    signature, so anyone who can reach the tunnel can forge the header. Cloudflare Access at the edge (team emails only)
    is the real control. Local requests (no tunnel header) pass."""

    def __init__(self, app):
        self._app = app

    def __getattr__(self, name):
        return getattr(self._app, name)

    def __call__(self, environ, start_response):
        if environ.get("HTTP_CF_CONNECTING_IP") and not environ.get("HTTP_CF_ACCESS_JWT_ASSERTION"):
            body = b"Cloudflare Access is required for this page."
            start_response("403 Forbidden", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
            return [body]
        return self._app(environ, start_response)



def port_in_use(host: str, port: int) -> bool:
    """True when something already listens on host:port. A connect probe, because on Windows a second process can BIND a
    port another process has open with SO_REUSEADDR (it then steals or splits the traffic)."""
    import socket
    if not port:
        return False
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, int(port))) == 0


def no_address_reuse() -> None:
    """Windows: SO_REUSEADDR lets two servers share a port. Turn it off for the staging servers."""
    import os
    if os.name == "nt":
        from . import webhook
        from .admin.server import ThreadedWSGIServer
        webhook.HubServer.allow_reuse_address = False
        ThreadedWSGIServer.allow_reuse_address = False


def fake_transport(cfg: StagingConfig):
    from ..testing.fake_odoo import FakeOdoo

    class DryRunOdoo(FakeOdoo):
        """Like the real transport: NOT logged in (uid 0) until ``authenticate()`` is called."""

        def __init__(self):
            super().__init__(uid=0)

        def authenticate(self) -> int:
            self.uid = self.uid or 777
            return self.uid

        def execute_kw(self, model, method, args, kwargs):
            if (model, method) == ("helpdesk.stage", "fields_get"):
                return {"fold": {"type": "boolean"}}
            return super().execute_kw(model, method, args, kwargs)

    t = DryRunOdoo()
    for sid, row in t.data["helpdesk.stage"].items():
        row.update(team_ids=[2], fold=sid in (3, 4, 13))
    t.data["res.partner"][cfg.test_partner_id] = {"id": cfg.test_partner_id, "name": guard.TEST_PARTNER,
                                                  "email": False, "phone": False, "mobile": False}
    return t


class TeamhubTimer:
    """The Teamhub watcher on its OWN timer thread, so a slow Teamhub/Odoo call never delays the hub's tick. Shared by the
    staging and production hubs. ``init_teamhub`` must be called from the hub's ``__init__``."""

    def init_teamhub(self, runner, interval_s: int, timeout_s: int) -> None:
        import threading
        self._teamhub, self.teamhub_last = runner, None
        self._teamhub_interval, self._teamhub_timeout = interval_s, timeout_s
        self._teamhub_stop, self._teamhub_thread = threading.Event(), None

    def teamhub_once(self):
        """One watcher pass in a worker thread, bounded by the timeout. Never raises."""
        import threading
        if self._teamhub is None:
            return "off"
        box: list = []

        def work():
            try:
                box.append(self._teamhub())
            except Exception as e:                              # noqa: BLE001
                _log.error("teamhub pass failed: %s", type(e).__name__)
                box.append({"error": type(e).__name__})

        w = threading.Thread(target=work, name="teamhub-pass", daemon=True)
        w.start()
        w.join(max(1, self._teamhub_timeout))
        self.teamhub_last = box[0] if box else "timeout"
        return self.teamhub_last

    def start_teamhub(self) -> None:
        import threading
        if self._teamhub is None or self._teamhub_thread is not None:
            return
        self._teamhub_thread = threading.Thread(target=self._teamhub_loop, name="teamhub-timer", daemon=True)
        self._teamhub_thread.start()

    def _teamhub_loop(self) -> None:
        while not self._teamhub_stop.wait(max(1, self._teamhub_interval)):
            self.teamhub_once()

    def stop_teamhub(self) -> None:
        self._teamhub_stop.set()
