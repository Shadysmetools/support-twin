"""The persona / organisation profile: every company, client, person, host and mailbox the engine needs to know.

Nothing organisation-specific is hard-coded in the engine: modules read these values instead. They come from the
``profile:`` section of ``<TWIN_AGENT_HOME>/config/config.yaml`` (or the YAML file named by ``TWIN_PROFILE``), merged
over the neutral defaults below (the fictional "Acme Support" team helping the fictional client "Globex").
The defaults are what the test-suite runs with, so a fresh clone works with no configuration at all.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # who the twin is a twin of (the "principal": the human support agent who approves every send)
    "principal_name": "Principal",
    "twin_name": "Digital Twin",
    # the support organisation running the twin
    "company_name": "Acme Support",
    "our_domains": ["example.com"],                 # a domain also matches its sub-domains (helpdesk.example.com)
    "helpdesk_email": "help@example.com",           # the helpdesk mailbox tickets are filed to
    "helpdesk_host": "helpdesk.example.com",        # the helpdesk (Odoo) host: reached by API only, never a browser
    # the client the principal supports
    "client_name": "Globex",
    "client_companies": ["EG", "KSA"],              # company codes that get their own ticket tag, e.g. [Globex EG]
    # Odoo tenants a browser must never open (a click there can trigger real fulfilment): host keywords
    "banned_tenant_keywords": ["globex"],
    # exact e-mail addresses allowed through the production access gate (each needs a matching hub user)
    "access_emails": ["sysadmin@example.com", "info@example.com", "hsa@example.com", "riley@example.com",
                      "supervisor@example.com", "principal.doe@globex.example.net"],
    # phone numbers (digits only) the staging rails refuse anywhere in a config
    "protected_numbers": ["15550009999"],
}


def _load_file(path: Path) -> dict:
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "profile" in data:
        return data["profile"] if isinstance(data["profile"], dict) else {}
    return {} if path.name == "config.yaml" else data      # a dedicated profile file may be flat


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    """DEFAULTS overlaid with the configured profile (unknown keys are kept, so extensions can read them)."""
    out = dict(DEFAULTS)
    env = os.environ.get("TWIN_PROFILE")
    candidates = [Path(env)] if env else []
    home = os.environ.get("TWIN_AGENT_HOME")
    if home and not env:
        candidates.append(Path(home) / "config" / "config.yaml")
    for c in candidates:
        if c.is_file():
            prof = _load_file(c)
            if isinstance(prof, dict):
                out.update({k: v for k, v in prof.items() if v is not None})
            break
    return out


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)
