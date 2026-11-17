"""One-time Telegram login for the agent: `python -I aa.py tg-login` (06 E step 3).

Creates a NEW Telethon login named "TwinAgent-Laptop" (shown in Telegram > Settings > Devices, where Principal can
end it at any time). Principal types his phone number, the login code and his 2FA password into hidden prompts.
Nothing he types is printed or logged. The session string goes straight into the vault as 'tg-user-session'
(Windows Credential Manager, DPAPI, his Windows user only). No .session file is written to disk.

If 'tg-api' is not in the vault yet, it asks for api_id and api_hash (from my.telegram.org) and stores them as
'<api_id>:<api_hash>'.

Options: --force   replace an existing 'tg-user-session' (end the old one in Telegram > Devices too).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
from typing import Callable

TG_API, TG_SESSION = "tg-api", "tg-user-session"


def _vault():
    from twin_agent import creds
    return creds.set_secret, creds.has_secret, creds._get


async def login(*, store: Callable[[str, str], None], has: Callable[[str], bool], get: Callable[[str], str],
                client_factory: Callable[[int, str], object], ask_secret: Callable[[str], str],
                say: Callable[[str], None], force: bool = False) -> int:
    from twin_agent.channels.tg_client import format_tg_api, parse_tg_api

    if has(TG_SESSION) and not force:
        say("A Telegram login is already in the vault. Use --force to replace it.")
        return 1
    if not has(TG_API):
        api_id = ask_secret("api_id from my.telegram.org (hidden): ").strip()
        api_hash = ask_secret("api_hash from my.telegram.org (hidden): ").strip()
        try:
            store(TG_API, format_tg_api(api_id, api_hash))
        except ValueError:
            say("That api_id / api_hash does not look right (api_id is digits, api_hash is 32 letters/digits).")
            return 2
    api_id, api_hash = parse_tg_api(get(TG_API))

    client = client_factory(api_id, api_hash)
    await client.connect()
    try:
        phone = ask_secret("Your phone number with country code, e.g. +20... (hidden): ").strip()
        await client.send_code_request(phone)
        code = ask_secret("The login code Telegram just sent you (hidden): ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except Exception as e:                       # noqa: BLE001
            if type(e).__name__ != "SessionPasswordNeededError":
                say(f"Login failed ({type(e).__name__}). Nothing was saved. Run it again.")
                return 3
            await client.sign_in(password=ask_secret("Your Telegram 2FA password (hidden): "))
        me = await client.get_me()
        store(TG_SESSION, client.session.save())
        say("Done. The login 'TwinAgent-Laptop' is saved in the vault (not printed, not on disk).")
        say(f"Your Telegram user id is {int(me.id)}. Put it in config.yaml as people.principal_tg_user_id.")
        return 0
    except Exception as e:                           # noqa: BLE001 - never echo what Telegram said back
        say(f"Login failed ({type(e).__name__}). Nothing was saved. Run it again.")
        return 3
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None, *, store=None, has=None, get=None, client_factory=None,
         ask_secret: Callable[[str], str] = getpass.getpass, say: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(prog="aa.py tg-login", description="One-time Telegram login for Principal's Agent.")
    ap.add_argument("--force", action="store_true", help="replace an existing login in the vault")
    args = ap.parse_args(argv or [])
    if store is None or has is None or get is None:
        v_store, v_has, v_get = _vault()
        store = store or (lambda n, v: v_store(n, v, caller="tg_login"))
        has, get = has or v_has, get or v_get
    if client_factory is None:
        from twin_agent.channels.tg_client import new_telethon_client
        client_factory = new_telethon_client
    return asyncio.run(login(store=store, has=has, get=get, client_factory=client_factory,
                             ask_secret=ask_secret, say=say, force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
