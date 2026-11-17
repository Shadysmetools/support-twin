"""The Twin Monitor agent (Sysadmin's device): ingest ops bundles, raise alerts, deliver them.

`analyse(...)` reads every `ops/ops-*.jsonl` under the bundle folder (a clone of the monitor outbox), stores it in
the monitor's own SQLite (idempotent), optionally loads Principal's dev work from the push-trace folder, runs the alert
rules and sends each NEW alert to Sysadmin (at most `monitor.max_alerts_per_run`, default 10, one by one; the rest go
in one summary message). Reports are made on demand by report.py (`aa.py monitor report|scorecard|incident`).
It has no path to Principal's state, the sender, Odoo or Telegram-as-Principal (see readonly tests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..models import utc_iso
from . import dev as devmod
from .alerts import consent_alerts, record_alerts, request_alerts
from .export import read_bundle
from .store import MonitorStore


@dataclass
class AnalyseResult:
    bundles: int = 0
    new_records: int = 0
    dev_rows: int = 0
    alerts: list[dict] = field(default_factory=list)
    delivered: int = 0


def ingest_folder(store: MonitorStore, bundles: Path) -> tuple[int, list[dict]]:
    seen = {b["id"] for b in store.bundles()}
    n, new = 0, []
    for p in sorted(Path(bundles).glob("**/ops-*.jsonl")):
        head, recs = read_bundle(p)
        if not head or head.get("id") in seen:
            continue
        new += store.ingest(head, recs)
        n += 1
    return n, new


def deliver(store: MonitorStore, sender, now: datetime, cap: int = 10) -> int:
    if sender is None:
        return 0
    pending = store.unsent_alerts()
    order = ("critical", "high", "medium", "info")
    pending.sort(key=lambda a: (order.index(a["severity"]) if a["severity"] in order else 9, a["ts"] or ""))
    sent = 0
    for a in pending[:cap]:
        text = a["text"] + (f"\nEvidence: {a['evidence']}" if a.get("evidence") else "")
        if sender.send(text[:3500]):
            store.mark_sent(a["key"], utc_iso(now))
            sent += 1
    rest = pending[cap:]
    if rest:
        kinds: dict[str, int] = {}
        for a in rest:
            kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
        if sender.send(f"Twin Monitor: {len(rest)} more alert(s): " +
                       ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())) + ". Run: aa.py monitor report"):
            for a in rest:
                store.mark_sent(a["key"], utc_iso(now))
    return sent


def analyse(bundles: Path, store: MonitorStore, now: datetime, cfg: dict | None = None, *, sender=None,
            dev_dir: Path | None = None) -> AnalyseResult:
    cfg = cfg or {}
    res = AnalyseResult()
    res.bundles, new = ingest_folder(store, bundles)
    res.new_records = len(new)
    alerts = record_alerts(new, cfg) + consent_alerts(store.bundles()) + request_alerts(store, now, cfg)
    if dev_dir:
        rows = devmod.load(dev_dir, cfg)
        fresh = store.add_dev(rows)
        res.dev_rows = len(fresh)
        alerts += devmod.dev_alerts(fresh)
    res.alerts = [a for a in alerts if store.raise_alert(a)]
    cap = int((cfg.get("monitor") or {}).get("max_alerts_per_run", 10))
    res.delivered = deliver(store, sender, now, cap)
    return res
