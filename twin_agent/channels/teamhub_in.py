"""Teamhub intake (Principal 2026-11-15: "only send me what is sent to me").

The team moved from the Acme Team Telegram group to Teamhub (Hank, 12 Nov). Every `teamhub.poll_seconds` the
twin asks the signed-in Teamhub CLI, read-only, for `history search --to-me`: @mentions of Principal or his bot, @room
and DMs he did not send. Room traffic (status posts, channel intros) never reaches him.

- One card per new matching root or matched reply, into data/claude_inbox.jsonl (kind "teamhub", no buttons), plus
  a Windows notification. Today (Cairo) only; ids already shown are kept in the cursor `teamhub:seen` for the day.
- No ticket, no holding reply, nothing marked read, no reply: replying posts as Principal and needs his OK each time.
- The CLI's own sign-in file is never read here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Callable

from ..clock import CAIRO

log = logging.getLogger("twin_agent.teamhub_in")
CURSOR = "teamhub:seen"
DEFAULT_CLI = str(Path.home() / ".teamhub" / "bin" / "teamhub.exe")   # teamhub.cmd refuses to forward arguments


class TeamhubError(RuntimeError):
    """The CLI failed or answered something that is not a good result. The message is the error kind only."""


def kcfg(cfg: dict) -> dict:
    return cfg.get("teamhub") or {}


def enabled(cfg: dict) -> bool:
    k = kcfg(cfg)
    return bool(k.get("enabled")) and bool(k.get("workspace"))


def run_cli(cli: str, args: list[str], timeout: float = 90) -> str:
    """Run the Teamhub CLI with no console window (the listener is pythonw). Returns stdout."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        r = subprocess.run([cli, *args], capture_output=True, timeout=timeout, creationflags=flags,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TeamhubError(type(e).__name__) from None
    return r.stdout or ""


_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($env:AA_TOAST_TITLE)) > $null
$t.Item(1).AppendChild($x.CreateTextNode($env:AA_TOAST_BODY)) > $null
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def windows_toast(title: str, body: str) -> None:
    """A Windows notification. The text goes in environment variables, never into the script itself."""
    if sys.platform != "win32":
        return
    env = dict(os.environ, AA_TOAST_TITLE=title[:120], AA_TOAST_BODY=body[:300])
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TOAST], env=env,
                   capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _parse_at(s) -> datetime | None:
    """Teamhub's '2026-11-14 12:51:56.057601+00' (or ISO) as an aware datetime."""
    if not s:
        return None
    t = str(s).strip().replace(" ", "T", 1)
    if t.endswith("+00"):
        t += ":00"
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class TeamhubPoller:
    def __init__(self, cfg: dict, db, clock, *, run: Callable[[list[str]], str] | None = None,
                 notify: Callable[[str, str], None] | None = None, inbox_dir: Path | None = None,
                 on_error: Callable[[str], None] | None = None, on_ok: Callable[[], None] | None = None):
        self.cfg, self.db, self.clock = cfg, db, clock
        k = kcfg(cfg)
        self.workspace = str(k.get("workspace") or "")
        self.cli = str(k.get("cli") or DEFAULT_CLI)
        self.poll_seconds = float(k.get("poll_seconds", 60))
        self.notify_on = bool(k.get("notify", True))
        self.run_cli = run or (lambda args: run_cli(self.cli, args))
        self.notify = notify or windows_toast
        self.on_error, self.on_ok = on_error, on_ok
        from .claude_inbox import ClaudeInbox
        self.inbox = ClaudeInbox(cfg, data_dir=inbox_dir)
        self._running = False

    # ------------------------------------------------------------ one check

    def _day_start(self) -> tuple[str, datetime]:
        now = self.clock.now().astimezone(CAIRO)
        start = datetime.combine(now.date(), dtime(0, 0), tzinfo=CAIRO)
        return now.date().isoformat(), start

    def _seen(self, day: str) -> set[str]:
        try:
            v = json.loads(self.db.get_cursor(CURSOR) or "{}")
        except ValueError:
            v = {}
        return set(v.get("seen") or []) if v.get("day") == day else set()

    def _save(self, day: str, seen: set[str]) -> None:
        self.db.set_cursor(CURSOR, json.dumps({"day": day, "seen": sorted(seen)}))

    def fetch(self, since: datetime) -> list[dict]:
        args = ["workspace", "history", "search", "--to-me", "--room", "all",
                "--since", since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "--limit", "100", "--json", "--scope-workspace", self.workspace]
        raw = self.run_cli(args)
        try:
            data = json.loads(raw)
        except ValueError:
            raise TeamhubError("bad_output") from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise TeamhubError(str((data or {}).get("kind") or "not_ok") if isinstance(data, dict) else "not_ok")
        return list((data.get("details") or {}).get("results") or [])

    def poll_once(self) -> list[dict]:
        _, start = self._day_start()
        return self.process(self.fetch(start))

    def process(self, results: list[dict]) -> list[dict]:
        """Cards for the results not shown yet today. Runs on the listener's own thread (state.db)."""
        day, start = self._day_start()
        seen = self._seen(day)
        new: list[dict] = []
        for r in results:
            rid = str(r.get("rootMessageId") or "")
            if not rid:
                continue
            room = str(r.get("roomName") or r.get("roomKind") or "Teamhub")
            sender = str((r.get("rootSenderProfile") or {}).get("name") or "someone")
            at = _parse_at(r.get("rootSentAt"))
            if r.get("rootMatched") and rid not in seen and at is not None and at >= start:
                new.append({"root": rid, "message_id": rid, "room": room, "sender": sender,
                            "text": str(r.get("rootContent") or r.get("rootSnippet") or ""), "reply": False})
            for m in r.get("matchedReplies") or []:
                mid = str(m.get("messageId") or "")
                if mid and mid not in seen:
                    new.append({"root": rid, "message_id": mid, "room": room, "sender": None,
                                "text": str(m.get("snippet") or ""), "reply": True})
        for n in new:
            self._card(n)
            seen.add(n["message_id"])
        self._save(day, seen)
        return new

    def _card(self, n: dict) -> None:
        who = n["sender"] or "a reply in the thread"
        head = f"[Teamhub] {n['room']} - {'reply' if n['reply'] else 'from ' + who}"
        text = "\n".join([head, n["text"].strip()[:1500],
                          f"Full thread: teamhub workspace thread get --root {n['root']} "
                          f"--scope-workspace '{self.workspace}'",
                          "Nothing is marked read and nothing is replied without Principal's OK."])
        self.inbox._write("card", kind="teamhub", source="teamhub", request_id=None, text=text, photo=None,
                          buttons=[], chat=f"Teamhub: {n['room']}", root=n["root"], message_id=n["message_id"])
        if self.db is not None:
            try:
                self.db.log_event("teamhub_card", actor="teamhub", root=n["root"], message_id=n["message_id"],
                                  room=n["room"])
            except Exception:                                # noqa: BLE001 - the card is already out
                pass
        if self.notify_on:
            try:
                self.notify(f"Teamhub: {who if not n['reply'] else 'reply'} ({n['room']})", n["text"].strip()[:300])
            except Exception as e:                           # noqa: BLE001 - the card is the record
                log.warning("teamhub toast failed: %s", type(e).__name__)

    # ------------------------------------------------------------ loop

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                _, start = self._day_start()
                results = await asyncio.to_thread(self.fetch, start)    # only the CLI call leaves this thread
                self.process(results)
                if self.on_ok:
                    self.on_ok()
            except Exception as e:                           # noqa: BLE001 - one bad check never stops the loop
                kind = str(e) if isinstance(e, TeamhubError) else type(e).__name__
                log.warning("teamhub check failed: %s", kind)
                if self.on_error:
                    self.on_error(kind)
            if not self._running:
                break
            await asyncio.sleep(max(self.poll_seconds, 0.01))

    def stop(self) -> None:
        self._running = False
