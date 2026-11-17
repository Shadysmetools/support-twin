# Contributing to support-twin

Thanks for helping! Bug reports, new helpdesk adapters, docs and tests are all welcome.

## Dev setup

```bash
git clone https://github.com/<you>/support-twin && cd support-twin
./scripts/install.sh          # or: powershell -File scripts\install.ps1
source .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
python -m pytest -q
```

## Ground rules

1. **Safety first.** Nothing may reach a client without a human approval. A change that weakens a guard, the
   approval gate, the kill switch or the redaction rules needs a test that proves the old behaviour still holds.
2. **No network in tests.** `conftest.py` blocks every non-loopback connection. Use the fakes in
   `twin_agent/testing` and `twin_agent/hub/fakes.py`. Tests that truly need a live service or an optional SDK
   use `pytest.importorskip(...)` or the `live` marker.
3. **No real data.** Fixtures use synthetic people, `example.com`/`example.org` addresses and fake ids only.
4. **Organisation-specific values go in the profile** (`twin_agent/profile.py` and the `profile:` section of the
   config), never in code.
5. Keep pull requests small and focused, with a clear description and tests.

## Adding a helpdesk / channel adapter

Look at `twin_agent/hub/interfaces.py` (the seams the hub depends on), copy the matching fake from
`twin_agent/hub/fakes.py`, and implement the real adapter next to `twin_agent/hub/adapters_real.py`.
Wire it behind a config switch that is **off by default**.

## Code of conduct

Be kind and constructive. Harassment of any kind is not tolerated.
