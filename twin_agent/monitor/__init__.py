"""The Twin Monitor: a flight recorder plus an auditor for a digital twin (Digital Twin first).

Two sides, like the Watcher:
- Principal's laptop: export.py reads state.db READ-ONLY (sqlite `mode=ro` + an authorizer that denies every write)
  and the hash-chained audit logs, and writes redacted ops bundles (metadata only, never message text) to its own
  outbox. Gated by the Watcher's consent D4 plus `monitor.enabled`.
- Sysadmin's device: analyse.py (the "Twin Monitor agent") ingests bundles into its OWN SQLite (store.py) and
  produces the daily report, alerts, incident timelines and the weekly scorecard (report.py, alerts.py,
  timeline.py, kpis.py). dev.py adds Principal's git/GitHub dev work from the push-trace outputs (read-only).
  deliver.py posts report and alert text to Sysadmin's bot only.

Read-only guarantee (tests/test_monitor_readonly.py): no module here imports the sender, the channel adapters, the
bot controller, the spawner or anything Odoo; none opens StateDB or calls log_event; nothing posts as Principal.
"""
