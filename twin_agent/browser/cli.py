"""aa.py browser status|history

- `status`: is the Digital Twin profile answering on 127.0.0.1:<cdp_port>, and the history gate. Never lists tabs,
  URLs or cookies.
- `history [--day YYYY-MM-DD]`: counts of work-site visits per domain for that Cairo day (needs D4 +
  browser.history.enabled). Counts only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="browser")
    ap.add_argument("cmd", choices=["status", "history"])
    ap.add_argument("--day", default=None)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    from ..config import load_config
    from . import history
    from .attach import CDP
    cfg = load_config(a.config)
    port = int((cfg.get("browser") or {}).get("cdp_port") or 9334)
    if a.cmd == "status":
        try:
            v = CDP(port).version()
            print(f"Digital Twin browser: running ({v.get('Browser')}) on 127.0.0.1:{port}")
        except Exception as e:                                 # noqa: BLE001
            print(f"Digital Twin browser: not answering on 127.0.0.1:{port} ({type(e).__name__}); "
                  "start it with scripts\\digital-twin-browser.ps1")
        print(f"profile folder: {history.profile_dir(cfg)}")
        print(f"history for the Watcher: {'on' if history.enabled(cfg) else 'off (needs D4 + browser.history.enabled)'}")
        return 0
    if not history.enabled(cfg):
        print("browser history: off (needs consent.d4_watcher_ref + browser.history.enabled)")
        return 2
    from ..timesheets.desk import day_window
    since, until = day_window(date.fromisoformat(a.day) if a.day else date.today())
    for dom, n in Counter(v["domain"] for v in history.visits(cfg, since, until)).most_common():
        print(f"{dom}: {n} visits")
    return 0
