"""Shared helpers for the real Telegram adapters (user account and bot).

- `require_live` is the gate every real adapter passes before it may touch the network (CONTRACT rule 3).
- `make_user_client` builds ONE Telethon client "TwinAgent-Laptop" from the vault. telegram_in and
  telegram_send share that one client (two clients on one auth key risk AUTH_KEY_DUPLICATED).
- Telethon is imported lazily, inside functions, so tests and other modules never need it.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

DEVICE_MODEL = "TwinAgent-Laptop"     # what Telegram > Devices shows; Principal revokes it there
SYSTEM_VERSION = "Windows"
APP_VERSION = "twin-agent 0.1"

USER_VAULT_NAMES = ("tg-api", "tg-user-session")
BOT_VAULT_NAMES = ("tg-bot-token",)


class AdapterRefused(RuntimeError):
    """A real adapter was asked to start without live mode or without its vault values."""


def require_live(cfg: dict, names: tuple[str, ...] | list[str], has_secret: Callable[[str], bool]) -> None:
    """Raise AdapterRefused unless run_mode is 'live' and every vault name exists. Never reads a value."""
    if cfg.get("run_mode") != "live":
        raise AdapterRefused("run_mode is not 'live': use the fake / dry-run adapter")
    missing = [n for n in names if not has_secret(n)]
    if missing:
        raise AdapterRefused(f"vault names missing: {', '.join(missing)}")


def parse_tg_api(value: str) -> tuple[int, str]:
    """The 'tg-api' vault value is '<api_id>:<api_hash>'. Also accepts JSON or 'id hash' on two lines/words."""
    value = (value or "").strip()
    if value.startswith("{"):
        import json
        d = json.loads(value)
        return int(d["api_id"]), str(d["api_hash"]).strip()
    parts = [p for p in re.split(r"[:\s]+", value) if p]
    if len(parts) != 2 or not parts[0].isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", parts[1]):
        raise ValueError("tg-api must look like '<api_id>:<32-hex api_hash>'")
    return int(parts[0]), parts[1]


def format_tg_api(api_id: int | str, api_hash: str) -> str:
    api_id_s, api_hash = str(api_id).strip(), api_hash.strip()
    parse_tg_api(f"{api_id_s}:{api_hash}")      # validate shape
    return f"{api_id_s}:{api_hash}"


def quiet_library_logs() -> None:
    """Telethon and httpx can log message bodies or URLs (the bot URL holds the token). Keep them quiet."""
    for name in ("telethon", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def new_telethon_client(api_id: int, api_hash: str, session_string: str = ""):
    """A Telethon client with our device name. Empty session string = a brand new login (tg_login)."""
    from telethon import TelegramClient            # lazy: only real adapters need it
    from telethon.sessions import StringSession
    quiet_library_logs()
    return TelegramClient(StringSession(session_string or None), api_id, api_hash,
                          device_model=DEVICE_MODEL, system_version=SYSTEM_VERSION, app_version=APP_VERSION)


def make_user_client(cfg: dict, *, has_secret: Callable[[str], bool] | None = None,
                     get_secret: Callable[[str], str] | None = None):
    """The shared Telethon client for the listener. Refuses unless live + vault values exist."""
    if has_secret is None or get_secret is None:
        from .. import creds
        has_secret = has_secret or (lambda n: creds.has_secret(n))
        get_secret = get_secret or (lambda n: creds._get(n))
    require_live(cfg, USER_VAULT_NAMES, has_secret)
    api_id, api_hash = parse_tg_api(get_secret("tg-api"))
    return new_telethon_client(api_id, api_hash, get_secret("tg-user-session"))
