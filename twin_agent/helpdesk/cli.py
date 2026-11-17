"""aa.py helpdesk discover|status

- `discover`: log in as Principal (vault odoo-sys-principal, run_mode live) and print what the desk would use: fields present,
  the safe stages confirmed at runtime (and the ones refused), teams, ticket types and Supervisor's conventions. Reads
  only; prints no ids of people other than counts and no secrets.
- `status`: is the desk on, are writes on, and every reason they are not. Offline.
"""
from __future__ import annotations

import argparse


def status_lines(cfg: dict) -> list[str]:
    hd, out = cfg.get("helpdesk") or {}, []
    out.append(f"helpdesk.enabled: {hd.get('enabled') is True}")
    out.append(f"writes: {'ON' if cfg.get('run_mode') == 'live' and hd.get('write_enabled') is True else 'off'}"
               f" (run_mode {cfg.get('run_mode')}, write_enabled {hd.get('write_enabled') is True})")
    o = cfg.get("odoo_sys") or {}
    out.append(f"odoo_sys.url set: {bool(o.get('url'))}, db set: {bool(o.get('db'))}")
    out.append(f"Supervisor's gate card: {'on' if (cfg.get('people') or {}).get('supervisor_tg_user_id') else 'people.supervisor_tg_user_id not set'}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="helpdesk")
    ap.add_argument("cmd", choices=["discover", "status"])
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    from ..config import load_config
    cfg = load_config(a.config)
    if a.cmd == "status":
        print("\n".join(status_lines(cfg)))
        return 0
    from .conventions import discover
    from .odoo import OdooUnavailable, open_odoo
    try:
        odoo = open_odoo(cfg)
    except OdooUnavailable as e:
        print(f"helpdesk discover: {e}")
        return 2
    c = discover(odoo, cfg)
    print(f"ticket fields present: {', '.join(f for f in ('x_track', 'x_type', 'x_ai_confidence', 'x_task_ids', 'x_type_code') if c.has(f))}")
    print(f"x_track values: {c.track_values}")
    print(f"safe stages (no mail template at runtime): {sorted(c.safe_stages)}")
    print(f"stages that email the client: {sorted(s for s, v in c.stages.items() if v['emails_client'])}")
    print(f"teams: {len(c.teams)}; tags: {len(c.tags)}; ticket types: {sorted(c.types)}")
    print(f"Supervisor's conventions (from {c.supervisor.get('n', 0)} tickets): "
          + ", ".join(f"{k}={v}" for k, v in c.supervisor.items() if k not in ('uid', 'n')))
    print(f"Principal's employee found: {bool(c.principal_employee_id)}")
    for p in c.problems:
        print(f"note: {p}")
    return 0
