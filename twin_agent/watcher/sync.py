"""Sync the Watcher outbox to Sysadmin's device.

Default adapter: the outbox is its OWN small git repo, pushed to a private remote that Sysadmin configures
(`watcher.sync.remote`, e.g. an ssh path to his machine over Tailscale or a private repo). Rules:
- a real push needs the gate (`sync` capability, consent D4, not paused), `run_mode: live` and a remote;
  otherwise `make_sync` gives the FakeSync (records, never runs git);
- it refuses an outbox that is, or contains, the app repo: this repo itself is never pushed;
- the remote URL is never logged or returned (it may carry a token); git auth is git's own (ssh key / helper).
Sysadmin's side uses `pull` to fetch bundles into his clone.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import gate

APP_REPO = Path(__file__).resolve().parents[2]


@dataclass
class SyncResult:
    ok: bool
    pushed: bool = False
    detail: str = ""


class SyncAdapter(Protocol):
    def push(self, outbox: Path, message: str) -> SyncResult: ...
    def pull(self, dest: Path) -> SyncResult: ...


@dataclass
class FakeSync:
    pushes: list[tuple[str, str]] = field(default_factory=list)
    pulls: list[str] = field(default_factory=list)

    def push(self, outbox: Path, message: str) -> SyncResult:
        self.pushes.append((str(outbox), message))
        return SyncResult(True, False, "fake: nothing pushed")

    def pull(self, dest: Path) -> SyncResult:
        self.pulls.append(str(dest))
        return SyncResult(True, False, "fake: nothing pulled")


def _same_or_parent(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents


class GitSync:
    """git init/add/commit/push on the outbox only. `run` is subprocess.run (injected in tests)."""

    def __init__(self, remote: str, branch: str = "main", *, run: Callable[..., subprocess.CompletedProcess] =
                 subprocess.run, app_repo: Path = APP_REPO):
        if not remote:
            raise ValueError("no remote configured")
        self._remote, self.branch, self.run, self.app_repo = remote, branch or "main", run, app_repo

    def __repr__(self) -> str:                               # never show the remote (it may hold a token)
        return f"GitSync(branch={self.branch!r})"

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return self.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)

    def _check_outbox(self, outbox: Path) -> str | None:
        if _same_or_parent(outbox, self.app_repo):
            return "refused: the outbox is the app repo or holds it"
        if not (outbox / ".git").exists() and self._git(outbox, "init", "-q", "-b", self.branch).returncode != 0:
            return "git init failed"
        top = self._git(outbox, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != outbox.resolve():
            return "refused: the outbox is not its own git repo"
        return None

    def push(self, outbox: Path, message: str) -> SyncResult:
        outbox = Path(outbox)
        outbox.mkdir(parents=True, exist_ok=True)
        why = self._check_outbox(outbox)
        if why:
            return SyncResult(False, False, why)
        self._git(outbox, "add", "-A")
        commit = self._git(outbox, "-c", "user.name=Principal's Watcher", "-c", "user.email=watcher@localhost",
                           "commit", "-q", "-m", message)
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
            return SyncResult(False, False, "git commit failed")
        push = self._git(outbox, "push", "-q", self._remote, f"HEAD:{self.branch}")
        return SyncResult(push.returncode == 0, push.returncode == 0,
                          "pushed" if push.returncode == 0 else "git push failed (remote hidden)")

    def pull(self, dest: Path) -> SyncResult:
        dest = Path(dest)
        if not (dest / ".git").exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            r = self.run(["git", "clone", "-q", "-b", self.branch, self._remote, str(dest)],
                         capture_output=True, text=True, check=False)
        else:
            r = self._git(dest, "pull", "-q", "--ff-only", self._remote, self.branch)
        return SyncResult(r.returncode == 0, False, "pulled" if r.returncode == 0 else "git pull failed (remote hidden)")


def make_sync(cfg: dict, db=None, now=None, *, run=subprocess.run, side: str = "principal") -> SyncAdapter:
    """Real GitSync only when live + remote set (+ the gate on Principal's side). Everything else: FakeSync."""
    s = (cfg.get("watcher") or {}).get("sync") or {}
    remote = str(s.get("remote") or "").strip()
    if cfg.get("run_mode") != "live" or not remote or s.get("enabled") is not True:
        return FakeSync()
    if side == "principal" and (db is None or now is None or not gate.allowed(cfg, db, "sync", now)):
        return FakeSync()
    return GitSync(remote, str(s.get("branch") or "main"), run=run)
