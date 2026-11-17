"""Production watchdog: ``python -m twin_agent.hub.prod_watchdog [--config watchdog.yaml]``.

A SEPARATE process (it must survive the hub dying). Every ``interval_s`` it reads the hub's public health URL. An OUTAGE is:
the request failed, the answer is not JSON, ``ok`` is not true, or ``accepting`` is false. It posts ONE plain-English reply
to a fixed Teamhub thread on an outage and ONE on recovery, at most one post per kind per ``debounce_s`` (a flapping hub
cannot flood the thread), and a recovery is only announced if the outage was. A post is a ``relay-queue-router/*.md`` file
(``ROOM:`` / ``THREAD:`` headers, a ``---`` line, the text) and then one ``teamhub workspace workflow trigger``.

No phone number, IP, token or URL goes into a post. Every path and id is a config value (``WatchdogConfig``).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("twin_agent.hub.prod_watchdog")
TEAMHUB_HOME = Path.home() / ".teamhub"


@dataclass(frozen=True)
class WatchdogConfig:
    health_url: str = "https://app.example.com/healthz"
    interval_s: int = 60
    debounce_s: int = 900
    timeout_s: int = 10
    room_id: str = "a00000000005"
    thread_id: str = "a00000000006"
    queue_dir: str = str(TEAMHUB_HOME / "agents" / "c00000000004" / "workdir" / "relay-queue-router")
    teamhub_exe: str = str(TEAMHUB_HOME / "bin" / "teamhub.exe")
    workflow_id: str = "b00000000003"
    scope_room: str = "a00000000002"
    state_file: str = str(Path.home() / ".claude" / "aa-prod" / "watchdog-state.json")
    log_file: str = str(Path.home() / ".claude" / "aa-prod" / "watchdog.log")


def load_config(path: str | None) -> WatchdogConfig:
    if not path:
        return WatchdogConfig()
    import yaml
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    names = {f.name: f.type for f in fields(WatchdogConfig)}
    unknown = sorted(set(raw) - set(names))
    if unknown:
        raise ValueError(f"unknown watchdog config key(s): {', '.join(unknown)}")
    return WatchdogConfig(**{k: (int(v) if k.endswith("_s") else str(v)) for k, v in raw.items()})


def fetch_health(url: str, timeout: int) -> dict:
    """The health JSON, or an exception. Plain GET, no credentials."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "aa-prod-watchdog"}), timeout=timeout) as r:  # noqa: S310
        if r.status != 200:
            raise OSError(f"http {r.status}")
        d = json.loads(r.read().decode("utf-8"))
    if not isinstance(d, dict):
        raise ValueError("not an object")
    return d


def is_healthy(d: dict) -> bool:
    return d.get("ok") is True and d.get("accepting") is not False


class Watchdog:
    def __init__(self, cfg: WatchdogConfig, *, fetch: Callable[[str, int], dict] = fetch_health,
                 runner: Callable = subprocess.run, now: Callable[[], datetime] | None = None):
        self.cfg, self._fetch, self._run = cfg, fetch, runner
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.state = self._load()

    # ---- state
    def _load(self) -> dict:
        try:
            d = json.loads(Path(self.cfg.state_file).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        p = Path(self.cfg.state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.state), encoding="utf-8")

    def _log(self, line: str) -> None:
        try:
            p = Path(self.cfg.log_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"{self._now().isoformat(timespec='seconds')} {line}\n")
        except OSError:
            log.error("watchdog log not written")

    # ---- one check
    def probe(self) -> bool:
        try:
            return is_healthy(self._fetch(self.cfg.health_url, self.cfg.timeout_s))
        except Exception as e:                                  # noqa: BLE001 - any failure is an outage; the type only
            self._log(f"probe failed: {type(e).__name__}")
            return False

    def _allowed(self, kind: str) -> bool:
        last = self.state.get(f"last_{kind}_post")
        return not last or (self._now() - datetime.fromisoformat(last)).total_seconds() >= self.cfg.debounce_s

    def step(self) -> str:
        """One check. Returns what happened: ok, down, outage-posted, recovered, recovery-posted, still-down, suppressed."""
        healthy, now = self.probe(), self._now()
        was_down = bool(self.state.get("down_since"))
        if healthy and not was_down:
            return "ok"
        if not healthy and was_down:
            if not self.state.get("announced") and self._allowed("outage"):       # began inside the debounce, still down now
                return self._announce_outage(now)
            return "still-down"
        if not healthy:
            self.state["down_since"], self.state["announced"] = now.isoformat(), False
            if self._allowed("outage"):
                return self._announce_outage(now)
            self._log("outage: post suppressed by the debounce")
            self._save()
            return "suppressed"
        mins = int((now - datetime.fromisoformat(self.state["down_since"])).total_seconds() // 60)
        announced = bool(self.state.get("announced"))
        self.state.pop("down_since", None)
        self.state["announced"] = False
        posted = False
        if announced and self._allowed("recovery"):
            self._post("recovery", f"Support Twin production: the hub is healthy again after about {mins} minute(s).")
            self.state["last_recovery_post"], posted = now.isoformat(), True
        self._save()
        return "recovery-posted" if posted else "recovered"

    def _announce_outage(self, now: datetime) -> str:
        since = datetime.fromisoformat(self.state["down_since"])
        self._post("outage", f"Support Twin production: the hub is not answering its health check (since "
                   f"{since:%H:%M} UTC). Real client messages may not be reaching it. A person should check the laptop.")
        self.state["announced"], self.state["last_outage_post"] = True, now.isoformat()
        self._save()
        return "outage-posted"

    # ---- the post
    def _post(self, kind: str, text: str) -> None:
        c, now = self.cfg, self._now()
        q = Path(c.queue_dir)
        q.mkdir(parents=True, exist_ok=True)
        f = q / f"{now:%Y%m%dT%H%M%S}-twin-prod-watchdog-{kind}.md"
        f.write_text(f"ROOM: {c.room_id}\nTHREAD: {c.thread_id}\n---\n{text}\n", encoding="utf-8")
        self._log(f"{kind} post queued")
        try:
            self._run([c.teamhub_exe, "workspace", "workflow", "trigger", c.workflow_id, "--scope-room", c.scope_room],
                      cwd=os.path.expanduser("~"), capture_output=True, text=True, timeout=60)
        except Exception as e:                                  # noqa: BLE001 - the file is queued; a later trigger posts it
            self._log(f"trigger failed: {type(e).__name__}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twin_agent.hub.prod_watchdog")
    ap.add_argument("--config", default="")
    ap.add_argument("--once", action="store_true", help="one check, then exit")
    a = ap.parse_args(argv)
    wd = Watchdog(load_config(a.config or None))
    while True:
        print(wd.step(), flush=True)
        if a.once:
            return 0
        time.sleep(max(5, wd.cfg.interval_s))


if __name__ == "__main__":
    sys.exit(main())
