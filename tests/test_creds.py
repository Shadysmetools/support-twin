"""creds.py: vault via keyring (FakeKeyring here), DPAPI files, child env only, audit, CLI never echoes."""
import json
import sys
from pathlib import Path

import pytest

from twin_agent import creds, paths
from twin_agent.audit import AuditLog
from twin_agent.redact import value_hash
from twin_agent.testing.fakes import FakeClock

SECRET = "tok-9f8e7d6c5b4a39281706"


def _audit_text():
    p = paths.logs_dir() / "vault-audit.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_set_has_list_and_audit(tmp_home, fake_keyring):
    creds.set_secret("tg-bot-token", SECRET, backend=fake_keyring)
    assert creds.has_secret("tg-bot-token", backend=fake_keyring)
    assert creds.list_names(backend=fake_keyring) == ["tg-bot-token"]
    assert fake_keyring.get_password("twinagent", "tg-bot-token") == SECRET
    text = _audit_text()
    assert '"event":"set"' in text and SECRET not in text
    assert AuditLog(paths.logs_dir() / "vault-audit.jsonl", FakeClock()).verify() == (True, None)


def test_runsheet_name_form_and_unknown_names(tmp_home, fake_keyring):
    creds.set_secret("twinagent/wiki", SECRET, backend=fake_keyring)
    assert creds.has_secret("wiki", backend=fake_keyring)
    with pytest.raises(creds.VaultError):
        creds.set_secret("not-a-name", SECRET, backend=fake_keyring)
    with pytest.raises(creds.VaultError):
        creds.set_secret("wiki", "", backend=fake_keyring)


def test_big_value_goes_to_dpapi_file(tmp_home, fake_keyring):
    big = json.dumps({"installed": {"client_id": "x" * 3000}})
    creds.set_secret("google-oauth-client", big, backend=fake_keyring)
    assert fake_keyring.get_password("twinagent", "google-oauth-client") == creds.FILE_MARKER
    f = paths.vault_file_dir() / "google-oauth-client.bin"
    assert f.exists() and b"client_id" not in f.read_bytes()     # encrypted on disk
    assert creds._get("google-oauth-client", backend=fake_keyring) == big
    creds.set_secret("google-oauth-client", "small-value-1", backend=fake_keyring)
    assert not f.exists()                                          # old big file removed


def test_child_env_puts_value_in_env_only(tmp_home, fake_keyring, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    creds.set_secret("tg-bot-token", SECRET, backend=fake_keyring)
    env = creds.child_env({"BOT_TOKEN": "tg-bot-token"}, purpose="test", request_id="REQ-0001", backend=fake_keyring)
    assert env["BOT_TOKEN"] == SECRET
    import os
    assert "BOT_TOKEN" not in os.environ
    text = _audit_text()
    assert '"purpose":"test"' in text and '"request_id":"REQ-0001"' in text and SECRET not in text


def test_run_passes_value_to_child(tmp_home, fake_keyring):
    creds.set_secret("wiki", SECRET, backend=fake_keyring)
    code = f"import os,sys; sys.exit(0 if os.environ.get('KC')=={SECRET!r} else 5)"
    assert creds.run({"KC": "wiki"}, [sys.executable, "-I", "-c", code], purpose="t", backend=fake_keyring) == 0
    with pytest.raises(creds.VaultError):
        creds.run({"KC": "wiki"}, "echo hi", purpose="t", backend=fake_keyring)


def test_missing_value_is_audited_failure(tmp_home, fake_keyring):
    with pytest.raises(creds.VaultError):
        creds._get("elevenlabs", backend=fake_keyring)
    assert '"success":false' in _audit_text()


def test_vault_hashes_and_redact_values(tmp_home, fake_keyring):
    creds.set_secret("tg-api", "123456:0123456789abcdef0123456789abcdef", backend=fake_keyring)
    hashes = creds.vault_hashes(backend=fake_keyring)
    assert value_hash("0123456789abcdef0123456789abcdef") in hashes
    out = creds.redact_values("hash is 0123456789abcdef0123456789abcdef ok", hashes=hashes)
    assert "0123456789abcdef0123456789abcdef" not in out


def test_cli_set_never_echoes(tmp_home, fake_keyring, monkeypatch, capsys):
    answers = iter([SECRET, SECRET])
    monkeypatch.setattr(creds.getpass, "getpass", lambda prompt="": next(answers))
    assert creds.main(["set", "twinagent/deadman-url"], backend=fake_keyring) == 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert creds._get("deadman-url", backend=fake_keyring) == SECRET


def test_cli_set_tg_api_two_prompts(tmp_home, fake_keyring, monkeypatch):
    answers = iter(["123456", "0123456789abcdef0123456789abcdef"])
    monkeypatch.setattr(creds.getpass, "getpass", lambda prompt="": next(answers))
    assert creds.main(["set", "tg-api"], backend=fake_keyring) == 0
    assert creds._get("tg-api", backend=fake_keyring) == "123456:0123456789abcdef0123456789abcdef"


def test_cli_mismatch_stores_nothing(tmp_home, fake_keyring, monkeypatch):
    answers = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(creds.getpass, "getpass", lambda prompt="": next(answers))
    assert creds.main(["set", "wiki"], backend=fake_keyring) == 2
    assert not creds.has_secret("wiki", backend=fake_keyring)


def test_cli_set_file_deletes_source(tmp_home, fake_keyring, capsys):
    src = tmp_home / "client_secret_download.json"
    src.write_text('{"installed": {"client_secret": "abc123def456"}}', encoding="utf-8")
    assert creds.main(["set-file", "google-oauth-client", str(src)], backend=fake_keyring) == 0
    assert not src.exists()
    assert "abc123def456" not in capsys.readouterr().out


def test_cli_list_names_only(tmp_home, fake_keyring, capsys):
    creds.set_secret("wiki", SECRET, backend=fake_keyring)
    assert creds.main(["list"], backend=fake_keyring) == 0
    out = capsys.readouterr().out
    assert "wiki" in out and SECRET not in out and "missing" in out


def test_cli_has_no_get(tmp_home, fake_keyring):
    with pytest.raises(SystemExit):
        creds.main(["get", "wiki"], backend=fake_keyring)


def test_cli_run(tmp_home, fake_keyring):
    creds.set_secret("wiki", SECRET, backend=fake_keyring)
    code = f"import os,sys; sys.exit(0 if os.environ.get('KC')=={SECRET!r} else 5)"
    assert creds.main(["run", "--env", "KC=twinagent/wiki", "--", sys.executable, "-I", "-c", code],
                      backend=fake_keyring) == 0


def _valid_worker_settings(home):
    (home / "config" / "worker-settings.json").write_text(json.dumps(
        {"permissions": {"deny": list(creds.REQUIRED_WORKER_DENY)}}), encoding="utf-8")


def test_selftest_all_ok(tmp_home, fake_keyring):
    _valid_worker_settings(tmp_home)
    results = creds.selftest(backend=fake_keyring)
    assert results and all(ok for _, ok in results), results
    assert not creds.has_secret(creds.SELFTEST_NAME, backend=fake_keyring)     # probe cleaned up
    assert creds.main(["selftest"], backend=fake_keyring) == 0


def test_missing_keyring_package_is_a_plain_message(tmp_home, monkeypatch, capsys):
    import builtins
    real_import = builtins.__import__

    def no_keyring(name, *a, **k):
        if name == "keyring":
            raise ImportError("no keyring")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_keyring)
    assert creds.main(["list"]) == 2
    assert "keyring" in capsys.readouterr().err


def test_selftest_fails_on_unparseable_worker_settings(tmp_home, fake_keyring):
    (tmp_home / "config" / "worker-settings.json").write_text(r'{"_c": "C:\TwinAgent", "permissions": {"deny": ["Bash", "PowerShell"]}}', encoding="utf-8")
    results = dict(creds.selftest(backend=fake_keyring))
    assert results["worker deny rules present (check the hook by hand too)"] is False


def test_selftest_fails_when_worker_settings_lack_private_folder_denies(tmp_home):
    """Review fix: Bash + PowerShell alone are not enough; the private-folder Read rules must be there too."""
    (tmp_home / "config" / "worker-settings.json").write_text(json.dumps(
        {"permissions": {"deny": ["Bash", "PowerShell", "Write"]}}), encoding="utf-8")
    assert creds._worker_settings_deny_ok() is False


def test_shipped_worker_settings_pass_the_deny_check(tmp_home):
    shipped = Path(creds.__file__).parent / "worker" / "worker-settings.json"
    deny = json.loads(shipped.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert all(rule in deny for rule in creds.REQUIRED_WORKER_DENY)
