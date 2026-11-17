"""`python -I aa.py whatsapp status`: is the WhatsApp Cloud adapter allowed to run, and if not, why. Offline.

The adapter (whatsapp_cloud.py, official Meta Cloud API only) is NOT wired into the sender: there is no WhatsApp
channel type yet, so even with every switch on nothing is sent from the listener. Never prints numbers or tokens.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .whatsapp_cloud import WhatsAppCloud, wa_cfg


def status_lines(cfg: dict, *, has_secret=None) -> list[str]:
    w = wa_cfg(cfg)
    if has_secret is None:
        def has_secret(name):                               # noqa: E306 - keyring missing = "unknown"
            try:
                from .. import creds
                return creds.has_secret(name)
            except Exception:                               # noqa: BLE001
                return None
    why = WhatsAppCloud._config_refusal(cfg, w)
    secret = has_secret(str(w["vault_name"]))
    if why is None and secret is not True:
        why = f"vault entry {w['vault_name']} {'unknown (no keyring)' if secret is None else 'not set'}"
    recipients = len({str(x) for x in (w.get("allowed_recipients") or []) if str(x).strip()})
    return [f"WhatsApp Cloud adapter: {'allowed' if why is None else 'refused'}",
            *([f"- why: {why}"] if why else []),
            f"- allow-listed recipients: {recipients}",
            f"- free-form text / audio only inside the {w['window_hours']} h window; templates outside it",
            "- not wired into the sender (no WhatsApp channel type yet): the listener never sends on WhatsApp"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="whatsapp", description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    args = ap.parse_args(argv)
    from ..config import load_config
    print("\n".join(status_lines(load_config(args.config))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
