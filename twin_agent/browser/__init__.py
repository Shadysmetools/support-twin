"""The dedicated "Digital Twin" Chrome profile (BUILD 3, 2026-11-03).

- `scripts\\digital-twin-browser.ps1` starts it: its own user-data-dir, CDP on 127.0.0.1:9334 only.
- `attach.py`  open NEW tabs only and close them; refuses non-loopback hosts, other tabs and cookie-reading methods.
- `broker.py`  fills a login form with values from the vault (or the Bitwarden CLI) in-process; returns only
  "filled" / a reason. The model never sees a value.
- `history.py` the profile's History DB, copied and read-only, filtered to `browser.history.work_domains`.
Never export cookies or passwords to files.
"""
