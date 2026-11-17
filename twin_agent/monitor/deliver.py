"""Delivery of monitor texts to SYSADMIN's bot only (the Watcher digest adapters, with the monitor's own settings).

`FakeMonitorSender` keeps texts in memory (tests, dry runs). `make_sender(cfg)` gives the real Bot API sender
only when `run_mode: live`, `monitor.delivery.sysadmin_tg_user_id` is set, it is not Principal's id, and the vault holds
`monitor.delivery.vault_name` (default `monitor-bot-token`, Sysadmin's own bot). This is the Bot API `sendMessage`
to one chat: there is no Telethon user client here, so nothing can ever be posted as Principal.
Both refuse text with a secret or personal-data finding.
"""
from __future__ import annotations

from typing import Callable

from ..watcher.digest import BotDigestSender, DigestRefused, FakeDigestSender

FakeMonitorSender = FakeDigestSender
MonitorRefused = DigestRefused


def _delivery(cfg: dict) -> dict:
    return ((cfg or {}).get("monitor") or {}).get("delivery") or {}


def make_sender(cfg: dict, *, has_secret: Callable[[str], bool] | None = None, get_secret=None, post=None):
    d = _delivery(cfg)
    sysadmin = int(d.get("sysadmin_tg_user_id") or 0)
    principal = int(((cfg or {}).get("people") or {}).get("principal_tg_user_id") or 0)
    if sysadmin and sysadmin == principal:
        raise MonitorRefused("monitor delivery must go to Sysadmin, never to Principal's chat")
    view = {"run_mode": cfg.get("run_mode"),
            "watcher": {"digest": {"sysadmin_tg_user_id": sysadmin, "vault_name": d.get("vault_name") or "monitor-bot-token"}}}
    if has_secret is None:
        from .. import creds
        has_secret = creds.has_secret
    return BotDigestSender(view, has_secret=has_secret, get_secret=get_secret, post=post)
