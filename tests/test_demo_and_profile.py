"""The public quickstart surface: the synthetic demo, the CLI and the persona profile."""
from __future__ import annotations

import io

from twin_agent import cli, demo, profile


def test_demo_runs_end_to_end_and_every_client_reply_was_approved():
    out = io.StringIO()
    assert demo.run(fast=True, out=out) == 0
    text = out.getvalue()
    assert "[9] resolved" in text and "0 messages sent without a human tap" in text
    assert "client messages sent so far: 0" in text            # nothing goes out before the approval tap


def test_profile_defaults_are_neutral_placeholders():
    p = profile.DEFAULTS
    assert p["company_name"] == "Acme Support" and p["helpdesk_email"].endswith("@example.com")
    assert all(e.split("@")[1].endswith(("example.com", "example.net", "example.org")) for e in p["access_emails"])


def test_profile_file_overrides_defaults(tmp_path, monkeypatch):
    f = tmp_path / "me.yaml"
    f.write_text("\n".join(["profile:", "  company_name: Initech Help", "  principal_name: Sam", ""]), encoding="utf-8")
    monkeypatch.setenv("TWIN_PROFILE", str(f))
    profile.load.cache_clear()
    try:
        assert profile.get("company_name") == "Initech Help" and profile.get("principal_name") == "Sam"
        assert profile.get("helpdesk_email") == profile.DEFAULTS["helpdesk_email"]     # untouched keys keep defaults
    finally:
        profile.load.cache_clear()


def test_cli_lists_commands_and_rejects_unknown(capsys, monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)      # never touch the developer's .env here
    assert cli.main([]) == 0 and "demo" in capsys.readouterr().out
    assert cli.main(["no-such-command"]) == 2
