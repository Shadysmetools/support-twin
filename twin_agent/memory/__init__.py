"""Principal's Agent memory / learning layer (CONTRACT section: memory; 20 brief section 3).

Local-only, redacted, OBSERVE-safe. Its own SQLite file (data/memory.db, gitignored). Five stores plus a
reflection loop and a recall API:

- cases.py        case memory: structured record per closed request + FTS5/token-overlap similarity.
- episodes.py     episodic memory: append-only timeline of what happened per request.
- system_map.py   how Odoo/modules are wired, learned from investigations, each with evidence + trust.
- corrections.py  one-line rules distilled from Principal's edits/rejects of drafts.
- reflect.py      after N closed cases / on a timer: distil lessons, adjust trust levels (deterministic v0).
- recall.py       recall(request) -> compact context block, injected into the worker's folder (read-only).

Nothing here sends or writes to Odoo/Telegram/Gmail. Every value is run through twin_agent.redact.redact()
before it is stored. Trust levels: GREEN (verified, >=2 independent sources) / AMBER (partial) / RED (single).
"""
