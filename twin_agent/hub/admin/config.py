"""Admin configuration: staff users and the Meta Embedded Signup ids. Nothing secret lives in git: the users file
(scrypt password hashes, never plain passwords) is read from a path outside the repo, by default under
``~/.claude/secrets/``."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SECRETS_DIR = Path.home() / ".claude" / "secrets"
DEFAULT_USERS_FILE = SECRETS_DIR / "aa-admin-users.json"
DEFAULT_APP_SECRET_FILE = SECRETS_DIR / "meta-acme-app.json"      # {"app_id", "app_secret"}; never in git
DEFAULT_BUSINESS_DIR = SECRETS_DIR / "aa-meta-businesses"             # one 0600 JSON per connected business
GRAPH_VERSION = "v27.0"


@dataclass(frozen=True)
class StaffUser:
    username: str
    display_name: str
    approver_id: int        # the id the hub's approval model knows this person by (``approver_tg_ids`` / ticket owner)
    pw_hash: str


@dataclass
class AdminConfig:
    users: dict[str, StaffUser] = field(default_factory=dict)
    app_id: str = ""                 # Meta app id (public) for the Embedded Signup launcher
    config_id: str = ""              # Embedded Signup configuration id
    graph_version: str = GRAPH_VERSION
    cookie_secure: bool = True       # forced on by the app whenever a request arrived over HTTPS (see ``proxy``)
    allowed_hosts: tuple[str, ...] | None = None    # Host allowlist (DNS rebinding / host-header guard); None = off
    banner: str = ""                 # e.g. "DEMO MODE: fake data, nothing leaves this machine"
    trusted_proxies: tuple[str, ...] = ()   # CIDRs whose CF-Connecting-IP / X-Forwarded-Proto we believe
    hsts: bool = True                # send Strict-Transport-Security on HTTPS requests
    allow_live: bool = False         # let AdminApp run on a hub that is NOT a demo hub (PDPL sign-off precondition)
    allow_meta_writes: bool = False  # enable the explicit Meta write actions (subscribe webhooks, history sync)
    launcher_url: str = ""           # public base URL of the Embedded Signup launcher (a DIFFERENT origin)

    def __post_init__(self) -> None:
        if self.allowed_hosts is not None:
            self.allowed_hosts = tuple(sorted({str(h).strip().lower().rstrip(".") for h in self.allowed_hosts}))

    def name_for_approver(self, approver_id) -> str:
        """Only when ONE user has this id (hub users share an id, so it cannot tell them apart; see ``data.approver_name``)."""
        same = [u for u in self.users.values() if u.approver_id == approver_id]
        if len(same) == 1:
            return same[0].display_name
        return "Telegram approver" if not same else "Staff approver"   # never show a raw id or guess between users


def load_users(path: str | os.PathLike) -> dict[str, StaffUser]:
    """Read ``{"users": [{"username", "display_name", "approver_id", "password_hash"}]}``. Fails loudly on a bad file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    users: dict[str, StaffUser] = {}
    for u in data["users"]:
        name = str(u["username"]).strip().lower()
        if not name or name in users:
            raise ValueError("users file: empty or duplicate username")
        users[name] = StaffUser(name, str(u["display_name"]), int(u["approver_id"]), str(u["password_hash"]))
    return users
