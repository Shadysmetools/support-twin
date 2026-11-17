"""Watcher bundles (redaction + pseudonyms), sync adapters (fake / git with a fake runner), screen recording."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from twin_agent.db import StateDB
from twin_agent.testing.fakes import PRINCIPAL_ID
from twin_agent.watcher import bundle, cli, gate, screen, sync
from watcher_helpers import seed, watcher_on

SALT = b"test-salt"


def test_pseudonymiser_stable_and_scrubs_names():
    p = bundle.Pseudonymiser(SALT, ["Staff A", "Testa Personi"])
    assert p.alias("2001", "requester") == p.alias("2001", "requester") != p.alias("2002", "requester")
    out = p.scrub("thanks testa, ask Personi and Staff A")
    assert "testa" not in out.lower() and "personi" not in out.lower() and "Staff" not in out


def test_clean_record_redacts_and_drops_unsafe():
    p = bundle.Pseudonymiser(SALT, ["Staff A"])
    rec = {"kind": "own_message", "ts": "2026-11-01T12:40:00+00:00", "requester": "2001",
           "text": "call Staff A on +20 100 000 0000, token=abcdefghijkl, mail a@example.org"}
    c = bundle.clean_record(rec, p)
    assert c["requester"].startswith("requester-")
    assert "+20" not in c["text"] and "abcdefghijkl" not in c["text"] and "@example" not in c["text"]
    assert "[REDACTED:" in c["text"] and "Staff" not in c["text"]
    assert bundle.is_clean(c["text"])


def test_export_writes_clean_bundles(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, watcher_on(cfg))
    rid = seed(db, clock)
    db.log_event("instruction", rid, actor=str(PRINCIPAL_ID), text="key sk_live_abcdefghijklmnopqrstuvwx please")
    rep = cli.do_export(cfg, db, clock.now())
    assert rep.cases == 1 and rep.records > 0
    jl = (cli.outbox_dir(cfg) / "cases" / f"{rid}.jsonl").read_text(encoding="utf-8")
    md = (cli.outbox_dir(cfg) / "cases" / f"{rid}.md").read_text(encoding="utf-8")
    for text in (jl, md):
        assert "Staff A" not in text and "sk_live" not in text and "2001" not in text
        assert bundle.is_clean(text)
    back = bundle.read_bundle(cli.outbox_dir(cfg) / "cases" / f"{rid}.jsonl")
    assert back["request_id"] == rid and back["records"]
    assert not (cli.outbox_dir(cfg) / "salt").exists()                       # the salt never sits in the outbox


def test_make_sync_is_fake_unless_live_remote_and_gate(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, watcher_on(cfg))
    assert isinstance(sync.make_sync(cfg, db, clock.now()), sync.FakeSync)  # dry_run
    cfg["run_mode"] = "live"
    cfg["watcher"]["sync"].update(enabled=True, remote="ssh://host/private.git")
    assert gate.capability_on(cfg, "sync")
    real = sync.make_sync(cfg, db, clock.now())
    assert isinstance(real, sync.GitSync) and "private" not in repr(real)
    db.log_event(gate.PAUSE, None, actor=str(PRINCIPAL_ID))
    assert isinstance(sync.make_sync(cfg, db, clock.now()), sync.FakeSync)  # paused: no push


class FakeGit:
    def __init__(self, toplevel: Path | None = None, fail: str = ""):
        self.calls, self.toplevel, self.fail = [], toplevel, fail

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        sub = argv[3] if argv[:2] == ["git", "-C"] else argv[1]
        out = str(self.toplevel or argv[2]) + "\n" if sub == "rev-parse" else ""
        if sub == "init":
            Path(argv[2], ".git").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 1 if sub == self.fail else 0, out, "")


def test_git_sync_pushes_outbox_only(tmp_path):
    git = FakeGit()
    s = sync.GitSync("ssh://h/secret-token@x.git", run=git, app_repo=tmp_path / "app")
    res = s.push(tmp_path / "outbox", "msg")
    assert res.ok and res.pushed
    pushes = [c for c in git.calls if "push" in c]
    assert pushes and pushes[0][2] == str(tmp_path / "outbox")
    assert "secret-token" not in res.detail


def test_git_sync_refuses_app_repo_and_foreign_toplevel(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    git = FakeGit()
    assert not sync.GitSync("r", run=git, app_repo=app).push(app, "m").ok
    assert not sync.GitSync("r", run=git, app_repo=app / "sub").push(app, "m").ok      # outbox holds the repo
    git2 = FakeGit(toplevel=app)
    res = sync.GitSync("r", run=git2, app_repo=app).push(app / "data" / "outbox", "m")
    assert not res.ok and "own git repo" in res.detail
    assert not any("push" in c for c in git2.calls)


def test_git_sync_push_failure_hides_remote(tmp_path):
    res = sync.GitSync("https://user:tok@h/x.git", run=FakeGit(fail="push"), app_repo=tmp_path / "a").push(
        tmp_path / "o", "m")
    assert not res.ok and "tok" not in res.detail


def test_screen_record_gated_and_window_only(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "s.db", clock, cfg)
    with pytest.raises(screen.RecordingRefused):
        screen.record(cfg, db, "REQ-0001", clock.now(), run=lambda *a, **k: None)
    watcher_on(cfg)
    cfg["watcher"]["screen_record"]["enabled"] = True
    with pytest.raises(screen.RecordingRefused):                             # no window: desktop refused
        screen.record(cfg, db, "REQ-0001", clock.now(), run=lambda *a, **k: None)
    cfg["watcher"]["screen_record"]["window_title"] = "Odoo - Browser"
    seen, notes = [], []

    def run(cmd, **kw):
        seen.append(cmd)
        assert gate.recording_file().read_text(encoding="utf-8") == "REQ-0001"   # indicator while recording

    out = screen.record(cfg, db, "REQ-0001", clock.now(), seconds=60, run=run, notify=notes.append)
    cmd = seen[0]
    assert "-show_region" in cmd and "title=Odoo - Browser" in cmd and "desktop" not in cmd
    assert cmd[cmd.index("-t") + 1] == "60" and out.parent.name == "REQ-0001"
    assert not gate.recording_file().exists() and notes
    db.log_event(gate.PAUSE, None, actor=str(PRINCIPAL_ID))
    with pytest.raises(screen.RecordingRefused):
        screen.record(cfg, db, "REQ-0001", clock.now(), run=run)


def test_cli_status_and_export_off(tmp_home, capsys):
    assert cli.main(["status"]) == 0
    assert "Watcher: off" in capsys.readouterr().out
    assert cli.main(["export"]) == 0
    assert "off or paused" in capsys.readouterr().out
