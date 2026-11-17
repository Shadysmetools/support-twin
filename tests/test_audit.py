"""audit.py: hash-chained append-only log."""
import json

from twin_agent.audit import GENESIS, AuditLog
from twin_agent.testing.fakes import FakeClock


def _log(tmp_path):
    return AuditLog(tmp_path / "logs" / "actions.jsonl", FakeClock())


def test_chain_links_and_verifies(tmp_path):
    log = _log(tmp_path)
    h1 = log.append({"kind": "send", "status": "sent"})
    h2 = log.append({"kind": "send", "status": "blocked"})
    lines = [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["prev_hash"] == GENESIS and lines[0]["hash"] == h1
    assert lines[1]["prev_hash"] == h1 and lines[1]["hash"] == h2
    assert lines[0]["ts"].endswith("+00:00")
    assert log.verify() == (True, None)


def test_edit_is_detected_with_line_number(tmp_path):
    log = _log(tmp_path)
    for i in range(3):
        log.append({"n": i})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"n":1', '"n":9')
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log.verify() == (False, 2)


def test_deleted_line_and_garbage_detected(tmp_path):
    log = _log(tmp_path)
    for i in range(3):
        log.append({"n": i})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
    assert log.verify() == (False, 2)
    log.path.write_text(lines[0] + "\n{not json\n", encoding="utf-8")
    assert log.verify() == (False, 2)


def test_secrets_are_redacted_before_hashing(tmp_path):
    log = _log(tmp_path)
    log.append({"detail": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "nested": {"t": "token=s3cr3tvalue"}})
    text = log.path.read_text(encoding="utf-8")
    assert "abcdefghijklmnopqrstuvwxyz123456" not in text and "s3cr3tvalue" not in text
    assert "[REDACTED:" in text
    assert log.verify() == (True, None)


def test_vault_hash_redaction(tmp_path):
    from twin_agent.redact import value_hash
    log = AuditLog(tmp_path / "a.jsonl", FakeClock(), vault_hashes=[value_hash("plainvalue42")])
    log.append({"x": "leaked plainvalue42 here"})
    assert "plainvalue42" not in log.path.read_text(encoding="utf-8")


def test_new_instance_continues_chain(tmp_path):
    _log(tmp_path).append({"a": 1})
    log2 = _log(tmp_path)          # e.g. the creds CLI and the listener in two processes
    log2.append({"a": 2})
    assert log2.verify() == (True, None)
    assert log2.count() == 2


def test_missing_file_is_ok(tmp_path):
    assert _log(tmp_path).verify() == (True, None)
