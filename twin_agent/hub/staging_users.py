"""Admin staff users for the staging runner (kept apart from staging.py to keep both files small)."""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from pathlib import Path


def _refused():
    from .staging import StagingRefused
    return StagingRefused


def write_staff_users(path: str, username: str, approver_id: int) -> str | None:
    """A staff users file (0600, outside git) with one random password. None when the file already exists."""
    from .admin.auth import hash_password
    p = Path(os.path.expanduser(path))
    if p.exists():
        return None
    password = secrets.token_urlsafe(12)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"users": [{"username": username, "display_name": username, "approver_id": int(approver_id),
                              "password_hash": hash_password(password)}]}, f, indent=2)
    return password


def users_for_run(cfg, *, dry_run: bool) -> str:
    if Path(os.path.expanduser(cfg.users_file)).is_file():
        return os.path.expanduser(cfg.users_file)
    if not dry_run:
        raise _refused()("no admin users file: run  python -m twin_agent.hub.staging init-users --config staging.yaml")
    base = Path.home() / ".claude" / "tmp"                   # profile ACL, not the shared temp folder
    base.mkdir(parents=True, exist_ok=True)
    path = str(Path(tempfile.mkdtemp(prefix="aa-stg-dry-", dir=base)) / "users.json")
    pw = write_staff_users(path, "staging-approver", cfg.staff_approver_id)
    print(deliver("staging-approver", pw, path))
    return path


def deliver(username: str, password: str | None, path: str) -> str:
    if not password:
        return f"users file {path} already exists (kept)"
    if sys.stdout.isatty():
        return f"users file: {path}\nuser: {username}  password (shown once): {password}"
    pf = Path(path).with_suffix(".password.txt")
    pf.unlink(missing_ok=True)                                # a stale file from an older user; then create EXCLUSIVELY
    fd = os.open(pf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    return f"users file: {path}\nuser: {username}; password written to {pf} (not a terminal): read it, then delete it"
