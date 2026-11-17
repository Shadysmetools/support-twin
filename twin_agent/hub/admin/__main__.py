"""CLI: ``python -m twin_agent.hub.admin {hash-password|seed|serve}``.

Only the all-fakes DEMO hub is wired (the production Hub factory is not, same as ``hub.tick``). ``serve`` therefore
refuses any SQLite file that was not created by ``seed``. Binds 127.0.0.1 unless ``--allow-public-bind`` is given.
One hub process per SQLite file: do not run ``serve`` beside a real HubServer on the same file.

``--graph fake`` (default) shows fictional Demo Bakery data. ``--graph real`` reads the connected business's WABA, numbers
and templates from Meta (Graph v27.0) and runs the Embedded Signup launcher on its own port and origin. It never writes
to Meta unless ``--allow-meta-writes`` is also given, and even then only on an explicit click.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from . import proxy
from . import seed as seed_mod
from .app import AdminApp
from .auth import hash_password
from .config import DEFAULT_APP_SECRET_FILE, DEFAULT_BUSINESS_DIR, DEFAULT_USERS_FILE, AdminConfig, load_users
from .graph import FakeGraphClient
from .graph_real import RealGraphClient
from .launcher import LauncherApp
from .meta_store import BusinessStore, SignupStates
from .server import make_threaded_server

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="twin_agent.hub.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash-password", help="read a password from the terminal, print its scrypt hash")
    for name in ("seed", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True, help="a DEMO SQLite file (created by seed), or :memory: with serve --seed")
        p.add_argument("--users-file", default=str(DEFAULT_USERS_FILE.with_name("aa-admin-demo-users.json")))
    _serve_args(sub.choices["serve"])
    return ap


def _serve_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--seed", action="store_true", help="seed fictional data first if the DB is empty")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8780)
    s.add_argument("--allow-public-bind", action="store_true", help="required for any non-loopback host")
    s.add_argument("--app-id", default=os.environ.get("AA_ADMIN_META_APP_ID", ""))
    s.add_argument("--config-id", default=os.environ.get("AA_ADMIN_META_CONFIG_ID", ""))
    s.add_argument("--cookie-secure", choices=("auto", "on", "off"), default="auto",
                   help="auto = on unless bound to loopback; an HTTPS request (via a trusted proxy) is always Secure")
    s.add_argument("--allowed-host", action="append", default=[], metavar="HOST",
                   help="extra Host header value to accept (repeatable), e.g. the tunnel name")
    s.add_argument("--trusted-proxy", action="append", default=[], metavar="CIDR",
                   help="peer network whose CF-Connecting-IP / X-Forwarded-Proto we believe (repeatable); for a local "
                        "cloudflared use 127.0.0.1/32")
    s.add_argument("--graph", choices=("fake", "real"), default="fake")
    s.add_argument("--launcher-url", default="", help="public base URL of the Embedded Signup launcher (its OWN host name)")
    s.add_argument("--launcher-port", type=int, default=0, help="port for the launcher (0 = launcher off)")
    s.add_argument("--business-dir", default=str(DEFAULT_BUSINESS_DIR), help="secrets directory for per-business tokens")
    s.add_argument("--app-secret-file", default=str(DEFAULT_APP_SECRET_FILE), help='JSON {"app_id","app_secret"}; outside git')
    s.add_argument("--allow-meta-writes", action="store_true",
                   help="enable the explicit Meta write buttons (subscribe webhooks, history sync). Needs the owner's OK")


def deliver_password(password: str | None, users_file: str | os.PathLike, *, tty: bool | None = None) -> str:
    """Show the generated password ONLY on a real terminal. Anywhere else (agent transcripts, logs, pipes) it goes to a
    0600 file next to the users file and only the path is printed."""
    if not password:
        return f"users file: {users_file} (kept)"
    if sys.stdout.isatty() if tty is None else tty:
        return f"users file: {users_file}\nuser: demo-approver  password (shown once): {password}"
    path = Path(users_file).with_suffix(".password.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    return (f"users file: {users_file}\npassword written to {path} (not a terminal, so it is not printed). "
            "Read it yourself and delete the file after the recording.")


def _cmd_seed(args) -> int:
    if not seed_mod.is_new_path(args.db):
        print("refusing: seed only creates a NEW file; this path already exists", file=sys.stderr)
        return 2
    hub = seed_mod.build_demo_hub(args.db)
    try:
        out = seed_mod.seed(hub)
    finally:
        hub.store.close()
    print(f"seeded fictional tickets {out['tickets']} into {args.db}")
    print(deliver_password(seed_mod.write_demo_users(args.users_file), args.users_file))
    return 0


def _prepare_db(args) -> int | None:
    """None when the DB is ready, else the exit code."""
    if args.seed:
        if not seed_mod.is_new_path(args.db) or args.db == ":memory:":
            print("refusing: --seed only creates a NEW file; use a path that does not exist yet", file=sys.stderr)
            return 2
        hub = seed_mod.build_demo_hub(args.db)
        try:
            seed_mod.seed(hub)
        finally:
            hub.store.close()
        print(f"seeded {args.db}")
        print(deliver_password(seed_mod.write_demo_users(args.users_file), args.users_file))
    if not seed_mod.has_demo_marker(args.db):          # checked read-only BEFORE a hub touches the file
        print("refusing: this SQLite file is not a demo DB (create one with the seed command)", file=sys.stderr)
        return 2
    return None


def _hosts(args, loopback: bool, launcher_host: str) -> tuple[str, ...] | None:
    base = list(proxy.loopback_hosts(args.port)) if loopback else []
    hosts = [h for h in base + list(args.allowed_host) if proxy.hostname(h) != launcher_host or h in args.allowed_host]
    return tuple(hosts) if (hosts or loopback) else None


def _build(args, hub, loopback: bool):
    """(admin app, launcher app or None) or raise ValueError with a plain reason."""
    launcher_host = proxy.hostname(urlsplit(args.launcher_url).netloc) if args.launcher_url else ""
    hosts = _hosts(args, loopback, launcher_host)
    if launcher_host and any(proxy.hostname(h) == launcher_host for h in (hosts or ())):
        raise ValueError("the launcher must have its OWN host name, different from the admin's (cookies ignore ports)")
    if args.launcher_url and args.graph != "real":
        raise ValueError("--launcher-url needs --graph real (the launcher does a real code exchange and stores a real token)")
    if args.launcher_port and args.launcher_port == args.port:
        raise ValueError("--launcher-port must differ from --port")
    if args.graph == "real" and not args.app_id:
        raise ValueError("--graph real needs --app-id")
    if bool(args.launcher_port) != bool(args.launcher_url):
        raise ValueError("--launcher-url and --launcher-port go together")
    public_host = any(proxy.hostname(h) not in LOOPBACK for h in args.allowed_host)
    if (public_host or args.launcher_url.startswith("https")) and not args.trusted_proxy:
        raise ValueError("a tunnel / public host name needs --trusted-proxy (e.g. 127.0.0.1/32 for a local cloudflared); "
                         "without it every visitor shares one IP and 15 bad logins lock everyone out")
    secure = {"on": True, "off": False, "auto": not loopback or public_host}[args.cookie_secure]
    cfg = AdminConfig(users=load_users(args.users_file), app_id=args.app_id, config_id=args.config_id, cookie_secure=secure,
                      allowed_hosts=hosts, trusted_proxies=tuple(args.trusted_proxy), launcher_url=args.launcher_url,
                      allow_meta_writes=args.allow_meta_writes,
                      banner="DEMO MODE: fictional data and fake senders. Nothing leaves this machine."
                      if args.graph == "fake" else "TEST MODE: fake hub data. Meta reads are REAL (Graph v27.0). "
                      + ("Meta WRITE actions are ENABLED." if args.allow_meta_writes else "Meta writes are OFF."))
    businesses, states = BusinessStore(args.business_dir), SignupStates()
    graph = RealGraphClient(businesses, app_id=args.app_id) if args.graph == "real" else FakeGraphClient()
    app = AdminApp(hub, cfg, graph, signups=states, businesses=businesses)
    launcher = None
    if args.launcher_url:
        launcher = LauncherApp(states=states, businesses=businesses, app_id=args.app_id, config_id=args.config_id,
                               graph_version=cfg.graph_version, app_secret_file=args.app_secret_file,
                               allowed_hosts=(urlsplit(args.launcher_url).netloc,), trusted_proxies=cfg.trusted_proxies)
    return app, launcher


def _run(args, app, launcher) -> None:
    with contextlib.ExitStack() as stack:                   # whatever fails to bind, anything already bound is closed
        lsrv = stack.enter_context(make_threaded_server(args.host, args.launcher_port, launcher)) if launcher else None
        httpd = stack.enter_context(make_threaded_server(args.host, args.port, app))
        if lsrv:
            threading.Thread(target=lsrv.serve_forever, name="launcher", daemon=True).start()
            stack.callback(lsrv.shutdown)
            print(f"launcher on {args.host}:{args.launcher_port} (public address: {args.launcher_url})")
        print(f"admin on http://{args.host}:{args.port}/  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def _cmd_serve(args) -> int:
    loopback = args.host in LOOPBACK
    if not loopback and not args.allow_public_bind:
        print(f"refusing to bind {args.host}: pass --allow-public-bind (and put TLS in front)", file=sys.stderr)
        return 2
    if not loopback and not args.allowed_host:
        print("refusing: a public bind needs at least one --allowed-host (the Host header allowlist)", file=sys.stderr)
        return 2
    bad = _prepare_db(args)
    if bad is not None:
        return bad
    hub = seed_mod.build_demo_hub(args.db)
    try:
        try:
            app, launcher = _build(args, hub, loopback)
        except (ValueError, OSError) as e:
            print(f"refusing: {e}", file=sys.stderr)
            return 2
        try:
            _run(args, app, launcher)
        except OSError as e:                                # port in use, etc.: a plain refusal, not a traceback
            print(f"refusing: could not bind ({e.__class__.__name__})", file=sys.stderr)
            return 2
    finally:
        hub.store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "hash-password":
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            print("passwords differ", file=sys.stderr)
            return 1
        print(hash_password(pw))
        return 0
    return _cmd_seed(args) if args.cmd == "seed" else _cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())
