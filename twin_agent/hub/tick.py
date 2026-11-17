"""Diagnostics for ``Hub.tick``: ``python -m twin_agent.hub.tick --dry-run`` runs one tick against an ALL-FAKES hub on
an IN-MEMORY store (never a real ``hub.db``, no network, no way to point it at real state).

The production tick is NOT a separate process: ``HubServer`` (webhook.py) runs ``hub.tick()`` on a timer thread in the
SAME process as the webhook workers, because the one-ticket / one-ack guarantees are per process (and ``SqliteStore.
lock_exclusive`` refuses a second hub on the same state file). Do not wire this CLI to a systemd timer.
TODO(devops): the production Hub factory (real adapters from the vault) is not wired in Phase 1, so a run without
``--dry-run`` is BLOCKED (exit 2) with a plain reason, never a silent fake.
"""
from __future__ import annotations

import argparse
import json
import sys


def _fake_hub():
    from ..testing.fake_odoo import FakeOdoo
    from . import fakes, judge, learning, proof_runner, routing
    from .odoo_gateway import GuardedOdoo
    from .pipeline import Hub
    from .store import SqliteStore
    store = SqliteStore(":memory:")
    hub = Hub(odoo=GuardedOdoo(FakeOdoo(), writes_on=True), whatsapp=fakes.FakeWhatsApp(), stt=fakes.FakeSTT(),
              telegram=fakes.FakeTelegram(), knowledge_odoo=fakes.FakeOdooKnowledge(),
              knowledge_wiki=fakes.FakeWikiKnowledge(), assigner=routing.RuleAssigner(routing.default_team()),
              proof=proof_runner.FakeProofRunner(), metrics_connector=fakes.FakeMetrics(), judge=judge.FakeJudge(),
              learning=learning.FakeLearningLog(), cfg={"run_mode": "dry_run", "phone_number_id": "dry-run"},
              store=store)
    return hub, store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twin_agent.hub.tick")
    ap.add_argument("--dry-run", action="store_true", help="all-fakes hub (no network)")
    args = ap.parse_args(argv)
    if not args.dry_run:
        print("blocked: real hub adapters are not configured (production Hub factory not wired); "
              "use --dry-run to exercise the tick with fakes", file=sys.stderr)
        return 2
    hub, store = _fake_hub()
    try:
        print(json.dumps(hub.tick()))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
