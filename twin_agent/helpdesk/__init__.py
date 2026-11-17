"""Digital Twin's helpdesk writes on the Acme Odoo 18 (helpdesk.example.com).

- `guard.py`       the write allowlist (model, method, fields, stages), enforced in code above every transport
- `odoo.py`        the guarded adapter (Principal's API key from the vault) + JSON-RPC transport
- `conventions.py` field conventions discovered at runtime (fields_get, stages' mail templates, Supervisor's tickets)
- `style.py`       TICKET-STYLE title/description and the descriptive internal note
- `ledger.py`      local request <-> ticket links and drafts (agent state stays off the ticket record)
- `desk.py`        ticket cards, Principal's taps, dedupe, create / link / note / stage moves, Supervisor's gate

Default is OBSERVE: `helpdesk.write_enabled: false`, so every write is recorded as "prepared" and never reaches
Odoo. Every write (done, prepared or blocked) is a Twin Monitor `write` event with its approval evidence.
"""
