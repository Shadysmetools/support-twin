"""Main process: one listener per laptop (CONTRACT section 9.14, 05 1.1, 2.4, 2.5).

- Single instance: an OS file lock on data/state/listener.lock (a stale file after a crash does not block a restart).
- Heartbeat: data/state/heartbeat holds one line, the last beat as UTC ISO (e.g. 2026-11-02T12:40:00+00:00).
  OPS's watchdog.ps1 restarts the task if it is older than timers.watchdog_stale_min.
- Dead-man ping every 5 min to an off-laptop URL (live mode only, URL from the vault, no data in the ping).
- Kill switch: the STOP file or /stop. Checked every tick; the sender also checks it right before each send.
- Internal watchdog: if a core loop dies, the process exits non-zero so Task Scheduler restarts it.
- Default is OBSERVE: dry_run unless config says live; --mode observe forces send_enabled off.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any

from . import paths
from . import timers as tm
from .bus import Bus, Topics
from .clock import SystemClock, elapsed, utc
from .models import Channel, Dial, parse_ts, utc_iso

log = logging.getLogger("twin_agent.listener")


@dataclass
class Adapters:
    tg_in: Any
    tg_send: Any
    bot: Any
    gmail_in: Any
    gmail_send: Any
    claude: Any


# ------------------------------------------------------------ adapters

def _dry_adapters() -> Adapters:
    from .testing.fakes import FakeBot, FakeClaude, FakeGmail, FakeTelegram
    tg, gm = FakeTelegram(), FakeGmail()
    return Adapters(tg_in=tg, tg_send=tg, bot=FakeBot(), gmail_in=gm, gmail_send=gm, claude=FakeClaude())


def build_adapters(cfg: dict) -> Adapters:
    """Fakes / dry-run unless run_mode == live. In live mode a real adapter that refuses to start (missing vault
    value, missing package) becomes None, never a fake: a fake would invent diagnoses or record fake sends.
    None means: no worker spawns, the sender blocks with no_adapter, no cards. Problems become health cards."""
    if cfg.get("run_mode") != "live":
        return _dry_adapters()
    from . import creds
    has = creds.has_secret
    out = Adapters(tg_in=None, tg_send=None, bot=None, gmail_in=None, gmail_send=None, claude=None)
    problems: list[str] = []

    def attempt(name: str, make):
        try:
            setattr(out, name, make())
        except Exception as e:                       # noqa: BLE001 - never crash on a missing login
            problems.append(f"{name}: {type(e).__name__}")
    client = None
    try:
        from .channels.tg_client import make_user_client
        client = make_user_client(cfg)
    except Exception as e:                           # noqa: BLE001
        problems.append(f"telegram: {type(e).__name__}")
    if client is not None:
        from .channels.telegram_in import TelethonIn
        from .channels.telegram_send import TelethonSend
        attempt("tg_in", lambda: TelethonIn(cfg, client, has_secret=has))
        attempt("tg_send", lambda: TelethonSend(cfg, client, has_secret=has))
    if (cfg.get("bot") or {}).get("transport") == "claude":   # Principal 2026-11-09: cards to his Claude chat
        attempt("bot", lambda: __import__("twin_agent.channels.claude_inbox", fromlist=["x"]).ClaudeInbox(cfg))
    else:
        attempt("bot", lambda: __import__("twin_agent.channels.bot_api", fromlist=["x"]).TelegramBotApi(
            cfg, has_secret=has))
    if (cfg.get("mailboxes") or {}).get("ticketmail", {}).get("address"):
        attempt("gmail_in", lambda: __import__("twin_agent.channels.gmail_in", fromlist=["x"]).RealGmailIn(cfg))
        # gmail_send is only needed when sending is on. With send_enabled false (read-only on purpose), not building
        # it is EXPECTED, not a problem - don't flag the mailbox as broken just because it's deliberately read-only
        # (issue #46). The Sender blocks anyway while send_enabled is false.
        if cfg.get("send_enabled"):
            attempt("gmail_send",
                    lambda: __import__("twin_agent.channels.gmail_send", fromlist=["x"]).RealGmailSend(cfg))
    from .spawner import RealClaudeRunner
    attempt("claude", lambda: RealClaudeRunner(cfg))
    out.problems = problems                          # type: ignore[attr-defined]
    return out


# ------------------------------------------------------------ single instance lock

class InstanceLock:
    """Non-blocking OS lock on one byte of the lock file. Released by the OS if the process dies."""

    def __init__(self, path=None):
        self.path = path or paths.lock_file()
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


# ------------------------------------------------------------ heartbeat and dead-man

def write_heartbeat(db, now) -> None:
    stamp = utc_iso(now)
    path = paths.heartbeat_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(stamp + "\n", encoding="utf-8")
    os.replace(tmp, path)
    db.set_cursor("heartbeat", stamp)


def read_heartbeat(db):
    return parse_ts(db.get_cursor("heartbeat"))


class DeadMan:
    """Pings the off-laptop check (healthchecks-style). Live mode only; URL stays in the vault; no data sent."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pings = 0
        self.enabled = cfg.get("run_mode") == "live"

    async def ping(self) -> bool:
        self.pings += 1
        if not self.enabled:
            return False
        try:
            from . import creds
            name = (self.cfg.get("deadman") or {}).get("vault_name", "deadman-url")
            if not creds.has_secret(name):
                return False
            url = creds._get(name, caller="listener", purpose="deadman ping")
            import urllib.request
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=10).close())
            return True
        except Exception as e:                       # noqa: BLE001 - a failed ping is exactly what the check catches
            log.warning("dead-man ping failed: %s", type(e).__name__)
            return False


# ------------------------------------------------------------ main loop

def _vault_hashes(cfg: dict) -> tuple:
    """Hashes of every vault value, taken once at start (live mode only), for the exact-leak guard and log
    redaction. Values themselves never leave creds.py. In dry_run there is nothing real to leak."""
    if cfg.get("run_mode") != "live":
        return ()
    try:
        from . import creds
        return tuple(creds.vault_hashes())
    except Exception as e:                           # noqa: BLE001 - no keyring: regex redaction still runs
        log.warning("vault hashes unavailable: %s", type(e).__name__)
        return ()


def _optional_sender(db, cfg, adapters: Adapters, clock, bus):
    try:
        from .audit import AuditLog
        from .sender import Sender
        hashes = _vault_hashes(cfg)
        audit = AuditLog(paths.logs_dir() / "actions.jsonl", clock, hashes)
        chans = {Channel.telegram_group: adapters.tg_send, Channel.telegram_dm: adapters.tg_send,
                 Channel.email: adapters.gmail_send, Channel.ticket_mail: adapters.gmail_send}
        return Sender(db, cfg, chans, clock, bus, audit, vault_hashes=hashes)
    except Exception as e:                           # noqa: BLE001
        bus.publish(Topics.HEALTH, {"component": "sender", "ok": False, "detail": type(e).__name__})
        return None


def _optional_bot_ctl(db, cfg, adapters: Adapters, clock, bus):
    if adapters.bot is None:
        return None
    try:
        from .channels.bot_controller import BotController
        return BotController(adapters.bot, db, cfg, bus, clock)
    except Exception as e:                           # noqa: BLE001
        bus.publish(Topics.HEALTH, {"component": "bot", "ok": False, "detail": type(e).__name__})
        return None


def _optional_voice(flow):
    """Voice notes (consent D2): a VoiceDesk only when live + consent + voice.enabled + vault + ffmpeg, else None."""
    from .voice.policy import voice_cfg
    if flow.cfg.get("run_mode") != "live" or voice_cfg(flow.cfg)["enabled"] is not True:
        return None
    try:
        from . import creds
        from .models import CardKind
        from .voice.desk import build_voice_desk
        desk, why = build_voice_desk(flow.cfg, flow.db, flow.clock, flow.post_card,
                                     lambda rid, text: flow.card(CardKind.info, text, rid), has_secret=creds.has_secret)
    except Exception as e:                           # noqa: BLE001
        desk, why = None, type(e).__name__
    if desk is None:
        flow.bus.publish(Topics.HEALTH, {"component": "voice", "ok": False, "detail": why})
    return desk


async def watcher_tick(flow) -> None:
    """Watcher (consent D4): export redacted case bundles + sync, every `watcher.sync.interval_minutes`.

    0 (the default) = manual only (`aa.py watcher sync`). Off, unconsented or /paused = nothing at all, not even
    the outbox folder. The push is a FakeSync unless live + `watcher.sync.enabled` + a remote (sync.make_sync);
    it runs in a thread so a slow git push never holds the bus.
    """
    from .watcher import gate
    every = float(((flow.cfg.get("watcher") or {}).get("sync") or {}).get("interval_minutes") or 0)
    now = flow.clock.now()
    last = getattr(flow, "watcher_last", None)
    if every <= 0 or (last is not None and elapsed(last, now).total_seconds() < every * 60):
        return
    flow.watcher_last = now
    if not any(gate.allowed(flow.cfg, flow.db, c, now) for c in gate.CAPTURE):
        return
    from .watcher.cli import do_export, outbox_dir
    from .watcher.sync import make_sync
    do_export(flow.cfg, flow.db, now)
    res = await asyncio.to_thread(make_sync(flow.cfg, flow.db, now).push, outbox_dir(flow.cfg),
                                  f"watcher bundles {now:%Y-%m-%d %H:%M}")
    if not res.ok:
        flow.bus.publish(Topics.HEALTH, {"component": "watcher_sync", "ok": False, "detail": res.detail})


async def monitor_tick(flow) -> None:
    """Twin Monitor (consent D4 + monitor.enabled): a read-only ops-bundle export every `monitor.interval_minutes`.

    0 (the default) = manual only (`aa.py monitor export`). The export opens state.db read-only through its own
    connection (never through flow.db) and runs in a thread; `monitor.sync` pushes the outbox when configured.
    """
    from .monitor.export import monitor_on
    every = float((flow.cfg.get("monitor") or {}).get("interval_minutes") or 0)
    now = flow.clock.now()
    last = getattr(flow, "monitor_last", None)
    if every <= 0 or (last is not None and elapsed(last, now).total_seconds() < every * 60):
        return
    flow.monitor_last = now
    if not monitor_on(flow.cfg):
        return
    from .monitor.cli import sync_outbox
    from .monitor.export import export, outbox_dir
    await asyncio.to_thread(export, flow.cfg, now)
    detail = await asyncio.to_thread(sync_outbox, flow.cfg, outbox_dir(flow.cfg), now)
    if "failed" in detail or "refused" in detail:
        flow.bus.publish(Topics.HEALTH, {"component": "monitor_sync", "ok": False, "detail": detail})


def wire_desks(flow) -> None:
    """Digital Twin's write desks (off by default): helpdesk tickets, then timesheets (which read its links)."""
    from .desks import optional_helpdesk, optional_timesheets
    flow.helpdesk = optional_helpdesk(flow)
    flow.timesheets = optional_timesheets(flow)


async def tick_extras(flow) -> None:
    """Per-tick work owned by other components: FloodWait queue (sender), cards held by the 10/h cap (bot) and the
    Watcher's timed export/sync. Each is optional and a failure is a health event, never a crash."""
    jobs = []
    if flow.sender is not None:
        jobs.append(("sender_queue", flow.sender.drain_queue))
    if flow.bot_ctl is not None:
        jobs.append(("card_digest", flow.bot_ctl.flush_digest))
    jobs.append(("watcher", lambda: watcher_tick(flow)))
    jobs.append(("monitor", lambda: monitor_tick(flow)))
    from .desks import timesheet_tick
    jobs.append(("timesheets", lambda: timesheet_tick(flow)))
    for name, job in jobs:
        try:
            await job()
        except Exception as e:                       # noqa: BLE001
            flow.bus.publish(Topics.HEALTH, {"component": name, "ok": False, "detail": type(e).__name__})


async def _tick_loop(flow, stop: asyncio.Event, tick_s: float, beat_s: float) -> None:
    from .flow_timers import check_stalled
    last_beat = None
    fails = 0
    while not stop.is_set():
        now = flow.clock.now()
        try:
            flow.check_stop_file()
            tm.tick(flow.db, flow.policy, now, flow.bus)
            await tick_extras(flow)
            if int(now.timestamp()) % 300 < tick_s:          # every ~5 min, between sweeps
                check_stalled(flow)
        except Exception as e:        # noqa: BLE001 - one bad tick must not kill the loop (queues would be lost)
            fails += 1
            flow.bus.publish(Topics.HEALTH, {"component": "tick", "ok": False, "detail": type(e).__name__})
            try:
                flow.db.log_event("tick_error", actor="listener", error=type(e).__name__, consecutive=fails)
            except Exception:         # noqa: BLE001
                pass
            if fails >= 5:
                raise                 # persistent failure: let the watchdog restart us rather than spin silently
        else:
            fails = 0
            if last_beat is None or elapsed(last_beat, now).total_seconds() >= beat_s:
                write_heartbeat(flow.db, now)     # heartbeat only after a clean tick, so a dead loop looks dead
                last_beat = now
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_s)
        except asyncio.TimeoutError:
            pass


def known_mail_threads(db) -> dict[str, list[str]]:
    """{gmail thread id: addresses already on it} for the reply-draft recipient check (gmail_send): the requester
    and everyone who wrote in that request's mail thread, from the twin's own records (2026-11-11)."""
    out: dict[str, list[str]] = {}
    for row in db._all("SELECT source, value FROM cursors WHERE source LIKE 'gmreq:%'"):
        rid = row["source"].split(":", 1)[1]
        try:
            thread = json.loads(row["value"] or "{}").get("thread_id")
        except ValueError:
            continue
        req = db.get_request(rid)
        if not thread or not req:
            continue
        people = {str(req.get("requester_id") or "").lower()}
        for m in db.thread(rid):
            if m.get("channel") == "email" and m.get("sender_id"):
                people.add(str(m["sender_id"]).lower())
        out[thread] = sorted(p for p in people if "@" in p)
    return out


async def _tg_keepalive(client, flow, stop: asyncio.Event, check_s: float = 30.0, max_wait_s: float = 300.0,
                        start_intake=None) -> None:
    """Keep the Telegram user client connected (Principal, 2026-11-10: "a stable channel").

    Telethon gives up after 5 failed reconnects and then stays offline without a word: on 10 Nov the listener ran
    blind from 14:29 to 14:50 until a manual restart. Every check_s: if the client is offline, reconnect (backoff up
    to max_wait_s while the network is down). The health card says "problem" once when it drops and "back to normal"
    once when it returns; a sweep then re-reads the chats so nothing sent meanwhile is missed.
    """
    from .flow_timers import sweep
    down_since, wait = None, check_s
    while not stop.is_set():
        try:
            ok = bool(client.is_connected())
            if not ok:
                if down_since is None:
                    down_since = flow.clock.now()
                    flow.bus.publish(Topics.HEALTH, {"component": "telegram", "ok": False,
                                                     "detail": "disconnected, reconnecting by itself"})
                try:
                    await asyncio.wait_for(client.connect(), timeout=60)
                    ok = bool(client.is_connected())
                except Exception as e:                # noqa: BLE001 - network still down: try again later
                    log.warning("telegram reconnect failed: %s", type(e).__name__)
                    ok = False
            if ok and down_since is not None:
                mins = max(1, round(elapsed(down_since, flow.clock.now()).total_seconds() / 60))
                down_since, wait = None, check_s
                try:
                    await client.catch_up()           # replay the updates Telegram kept while we were away
                except Exception:                     # noqa: BLE001 - the sweep below reads them anyway
                    pass
                flow.bus.publish(Topics.HEALTH, {"component": "telegram", "ok": True,
                                                 "detail": f"reconnected after about {mins} min"})
                flow.db.log_event("telegram_reconnected", actor="listener", minutes=mins)
                await sweep(flow)
            elif not ok:
                wait = min(wait * 2, max_wait_s)
            if ok and start_intake is not None:
                await start_intake()                  # started while Telegram was down: hook the chats up now
        except Exception as e:                        # noqa: BLE001 - the keepalive itself must never die
            log.warning("telegram keepalive error: %s", type(e).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


async def main_async(cfg: dict, adapters: Adapters, clock, *, stop_event: asyncio.Event | None = None,
                     db=None, once: bool = False) -> Any:
    """Wire everything and run until stop_event is set (or one round if once=True). Returns the Flow."""
    from .db import StateDB
    from .flow import Flow
    from .flow_timers import boot_catchup, ensure_global_timers, sweep, sweep_stale_sends
    paths.ensure_dirs()
    stop = stop_event or asyncio.Event()
    own_db = db is None
    db = db or StateDB(paths.state_db(), clock, cfg)
    if getattr(adapters, "gmail_send", None) is not None and hasattr(adapters.gmail_send, "_known"):
        adapters.gmail_send._known = lambda: known_mail_threads(db)   # who may get a reply draft, per thread
    bus = Bus(on_error=lambda topic, e: db.log_event("handler_error", actor="bus", topic=topic,
                                                     error=type(e).__name__))
    sender = _optional_sender(db, cfg, adapters, clock, bus)
    bot_ctl = _optional_bot_ctl(db, cfg, adapters, clock, bus)
    flow = Flow(db, cfg, bus, clock, adapters, sender=sender, bot_ctl=bot_ctl)
    flow.deadman = DeadMan(cfg)
    flow.voice = _optional_voice(flow)
    wire_desks(flow)
    away = parse_ts(db.get_cursor("away_until"))
    if away and utc(away) > utc(clock.now()):
        flow.policy.away_until = away
    flow.wire()
    from .comms_backbone import wire_comms
    wire_comms(flow)                                 # comms backbone (off unless comms.enabled; observe-safe)
    for p in getattr(adapters, "problems", []):
        bus.publish(Topics.HEALTH, {"component": "adapters", "ok": False, "detail": p})
    db.log_event("listener_start", actor="listener", run_mode=cfg.get("run_mode"),
                 send_enabled=bool(cfg.get("send_enabled")))
    last = read_heartbeat(db)
    ensure_global_timers(flow)
    boot_catchup(flow, last)
    sweep_stale_sends(flow)      # crash between claim_send and finish_send left a 'pending' reply: alert Principal

    async def on_event(ev):
        bus.publish(Topics.EVENT_IN, ev)
    tasks: list[asyncio.Task] = []
    try:
        await _start_telegram(adapters, on_event, bus)
        if bot_ctl is not None and not once:              # the bot long-polls forever: run it as a watched task
            tasks.append(asyncio.create_task(bot_ctl.start(), name="bot"))
        tg_client = getattr(adapters.tg_in, "client", None)
        if tg_client is not None and hasattr(tg_client, "is_connected") and not once:
            async def start_intake():
                # A start while the network was down failed before the chat handlers were added (2026-11-10):
                # the keepalive brings the connection back, this makes sure messages are heard again too.
                if not getattr(adapters.tg_in, "_handlers", None):
                    await adapters.tg_in.start(on_event)
                    db.log_event("telegram_intake_started", actor="listener", late=True)
            tasks.append(asyncio.create_task(_tg_keepalive(tg_client, flow, stop, start_intake=start_intake),
                                             name="tg_keepalive"))
        if adapters.gmail_in is not None and not once:    # mail intake: its own 2-minute loop, never blocks
            tasks.append(asyncio.create_task(_gmail_poller(adapters, db, cfg, on_event, bus).run(), name="gmail"))
        from .channels import teamhub_in
        if teamhub_in.enabled(cfg) and cfg.get("run_mode") == "live" and not once:   # Principal 2026-11-15
            kp = teamhub_in.TeamhubPoller(
                cfg, db, clock,
                on_error=lambda kind: bus.publish(Topics.HEALTH, {"component": "teamhub", "ok": False, "detail": kind}),
                on_ok=lambda: bus.publish(Topics.HEALTH, {"component": "teamhub", "ok": True}))
            tasks.append(asyncio.create_task(kp.run(), name="teamhub"))
        an = (cfg.get("helpdesk") or {}).get("analyst") or {}
        if an.get("enabled") and not once:               # the email's "Open as a session in Claude Code" link
            from .helpdesk import open_server
            h = open_server.start(paths.state_db(), int(an.get("open_port") or 8766))
            db.log_event("open_server", actor="listener", ok=h.server is not None, port=int(an.get("open_port") or 8766))
        await sweep(flow)                                 # re-read threads first: someone may have answered
        await bus.drain()
        write_heartbeat(db, clock.now())
        if once:
            tm.tick(db, flow.policy, clock.now(), bus)
            await bus.drain()
            await tick_extras(flow)
            await bus.drain()
            if flow.tasks:
                await asyncio.gather(*list(flow.tasks), return_exceptions=True)
                await bus.drain()
            return flow
        t = cfg.get("timers", {})
        tasks += [asyncio.create_task(bus.run(), name="bus"),
                 asyncio.create_task(_tick_loop(flow, stop, float(t.get("tick_seconds", 30)),
                                                float(t.get("heartbeat_seconds", 60))), name="tick")]
        waiter = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait(tasks + [waiter], return_when=asyncio.FIRST_COMPLETED)
        for task in done:                                 # watchdog: a core loop ended -> crash -> restart
            if task is not waiter and not stop.is_set():
                raise RuntimeError(f"core loop {task.get_name()} ended") from task.exception()
        return flow
    finally:
        stop.set()
        bus.stop()
        for task in tasks:
            task.cancel()
        for comp in (adapters.tg_in, adapters.bot):
            try:
                if comp is not None:
                    await comp.stop()
            except Exception:                             # noqa: BLE001
                pass
        if own_db:
            db.close()


def _gmail_poller(adapters: Adapters, db, cfg: dict, on_event, bus: Bus):
    """Gmail intake (allow-listed senders only). Errors become health events with the error class only."""
    from .channels.gmail_in import GmailPoller
    return GmailPoller(adapters.gmail_in, db, cfg, on_event,
                       on_error=lambda box, kind: bus.publish(Topics.HEALTH, {"component": f"gmail:{box}",
                                                                              "ok": False, "detail": kind}),
                       on_ok=lambda box: bus.publish(Topics.HEALTH, {"component": f"gmail:{box}", "ok": True}))


async def _start_telegram(adapters: Adapters, on_event, bus: Bus) -> None:
    """Connect Telegram: the sender learns Principal's real user id; intake registers its handlers (returns at once)."""
    for name, call in (("tg_send", lambda: adapters.tg_send.connect()), ("tg_in", lambda: adapters.tg_in.start(on_event))):
        comp = getattr(adapters, name)
        if comp is None or (name == "tg_send" and not hasattr(comp, "connect")):
            continue
        try:
            await call()
        except Exception as e:                        # noqa: BLE001 - e.g. session revoked: card, keep running
            bus.publish(Topics.HEALTH, {"component": name, "ok": False, "detail": f"{type(e).__name__}; re-login?"})


# ------------------------------------------------------------ command line

def _setup_logging() -> None:
    from .redact import redact

    class Redacting(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.msg, record.args = redact(record.getMessage()), ()
            return True
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(paths.logs_dir() / "listener.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.addFilter(Redacting())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)


def apply_mode(cfg: dict, mode: str | None, dry_run: bool) -> dict:
    """--dry-run forces fakes; --mode observe forces send off and caps every lane at observe (L11 stays auto)."""
    if dry_run:
        cfg["run_mode"] = "dry_run"
    if mode == "observe":
        cfg["send_enabled"] = False
        for section in ("helpdesk", "timesheets"):       # observe covers Odoo writes too (prepared only)
            if isinstance(cfg.get(section), dict):
                cfg[section]["write_enabled"] = False
        if isinstance(cfg.get("comms"), dict):            # observe never opens a real visible session
            cfg["comms"]["live_launch"] = False
        for lane, dial in list((cfg.get("dial") or {}).items()):
            if lane != "L11_internal_diagnosis" and Dial(dial).rank() > Dial.observe.rank():
                cfg["dial"][lane] = Dial.observe.value
    return cfg


def main(argv: list[str] | None = None) -> int:
    """--mode observe|config  --dry-run  --once  --config PATH. Exit 3 if another listener is running."""
    ap = argparse.ArgumentParser(prog="aa.py listener")
    ap.add_argument("--mode", choices=["observe", "config"], default="config")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="one sweep + tick, then exit (install self-check)")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    from .config import ConfigError, load_config
    paths.ensure_dirs()
    lock = InstanceLock()
    if not lock.acquire():
        print("Another listener is already running; exiting.", file=sys.stderr)
        return 3
    try:
        _setup_logging()
        try:
            cfg = apply_mode(load_config(args.config), args.mode, args.dry_run)
        except (ConfigError, FileNotFoundError, ValueError) as e:
            log.error("config problem, not starting: %s", e)
            print(f"config problem, not starting: {e}", file=sys.stderr)
            return 2
        log.info("listener starting: run_mode=%s send_enabled=%s", cfg.get("run_mode"), cfg.get("send_enabled"))
        adapters = build_adapters(cfg)
        asyncio.run(main_async(cfg, adapters, SystemClock(), once=args.once))
        problems = getattr(adapters, "problems", [])
        if args.once and problems:                    # install self-check: say what did not start
            print("adapters that did not start: " + ", ".join(problems), file=sys.stderr)
            return 4
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:                            # noqa: BLE001 - Task Scheduler restarts us
        log.error("listener crashed: %s", type(e).__name__, exc_info=True)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
