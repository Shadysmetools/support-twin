"""Where everything lives on disk.

Home is C:\\TwinAgent on Principal's laptop. Tests point TWIN_AGENT_HOME at a temp folder.
Runtime data (databases, logs, work folders) sits under home\\data, which git ignores.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path(r"C:\TwinAgent") if os.name == "nt" else Path.home() / ".twin-agent"


def home() -> Path:
    """The install folder (env TWIN_AGENT_HOME, else C:\\TwinAgent)."""
    return Path(os.environ.get("TWIN_AGENT_HOME") or DEFAULT_HOME)


def config_dir() -> Path:
    return home() / "config"


def data_dir() -> Path:
    return home() / "data"


def state_db() -> Path:
    return data_dir() / "state" / "state.db"


def kb_db() -> Path:
    return data_dir() / "kb" / "kb.db"


def logs_dir() -> Path:
    return data_dir() / "logs"


def work_dir(request_id: str | None = None) -> Path:
    base = data_dir() / "work"
    return base / request_id if request_id else base


def stop_file() -> Path:
    """Kill switch: if this file exists, nothing goes out."""
    return home() / "STOP"


def lock_file() -> Path:
    return data_dir() / "state" / "listener.lock"


def heartbeat_file() -> Path:
    return data_dir() / "state" / "heartbeat"


def vault_file_dir() -> Path:
    """DPAPI files for vault values too big for Credential Manager (outside home on purpose)."""
    override = os.environ.get("TWIN_AGENT_VAULT_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "TwinAgent" / "vault"


def ensure_dirs() -> None:
    """Create every runtime folder (not the vault folder; creds.py owns that)."""
    for p in (config_dir(), state_db().parent, kb_db().parent, logs_dir(), work_dir(),
              data_dir() / "proof", data_dir() / "kb" / "persona", data_dir() / "kb" / "lanes"):
        p.mkdir(parents=True, exist_ok=True)
