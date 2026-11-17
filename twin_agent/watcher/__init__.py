"""The Watcher: learns how Principal works on requests (his "fingerprint") so the agent can work like him.

Two sides:
- Principal's laptop: gate.py (consent D4 + /pause), capture.py + desk.py (read what already exists for a request),
  screen.py (opt-in per-ticket recording), bundle.py (redact + pseudonymise into an outbox), sync.py (push the
  outbox to Sysadmin's private remote).
- Sysadmin's device: analyse.py ("Sysadmin's Watcher agent"), fingerprint.py, similarity.py, digest.py.
bootstrap.py builds a first fingerprint offline from the handover export. Every capability is off by default.
"""
