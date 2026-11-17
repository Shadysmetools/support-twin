"""Minimal web admin for the WhatsApp hub (Meta App Review screencast surface).

Screens: login, Connect WhatsApp (Embedded Signup launcher), Connected numbers, Templates, Tickets (+ approvals).

Design rules (all enforced by tests):
  - Approvals go through the EXISTING path, ``Hub.on_owner_decision``: same single-use, approver-bound, draft-bound
    nonce as the Telegram tap. This package has no approvals table of its own and never touches the WhatsApp sender
    (the hub sends; see ``test_admin_never_sends``).
  - Meta Graph reads go through the injectable ``GraphClient`` (``graph.py``); tests use ``FakeGraphClient``.
  - Plain WSGI + stdlib only. Tests call the app in-process: no sockets, no servers.

Run the demo: ``python -m twin_agent.hub.admin --help``.
"""
