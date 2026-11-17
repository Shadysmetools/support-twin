"""Gmail intake (read-only scope): poll history.list every 2 minutes, normalise mail into Events.

Owner: CHANNELS (CONTRACT 9.9). Rules:
- Only mail on an allowed label from an allowed sender is kept. Everything else is dropped in memory and never
  logged (the ticket mailbox may be Principal's personal Gmail). Default is deny.
- Acme / auto-reply mail becomes channel `ticket_mail` (it can update a request, never open one).
- The real adapter refuses to start unless run_mode is live and the vault holds the tokens.

Per-mailbox config keys (all optional, defaults in code):
  labels: [INBOX]                 Gmail label ids to read
  allowed_senders: [example.com]  addresses or domains; default for `ticketmail` is Acme only, else nothing
  address: ""                     Principal's own address in this mailbox (his own sent mail to an allowed party is kept)
"""
from __future__ import annotations

import asyncio
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Protocol

from ..models import Channel, Event
from . import gmail_common as gc
from . import tickets

DEFAULT_SENDERS = {"ticketmail": ["example.com"]}


def _cairo():
    from ..clock import CAIRO
    return CAIRO
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
FALLBACK_QUERY = "newer_than:3d"


class GmailIn(Protocol):
    def list_new(self, mailbox: str, history_id: str | None) -> tuple[list[Event], str]: ...


def allowed_senders(cfg: dict, mailbox: str) -> list[str]:
    box = gc.mailbox_cfg(cfg, mailbox)
    rules = box.get("allowed_senders")
    return list(rules) if rules is not None else list(DEFAULT_SENDERS.get(mailbox, []))


def admit(ev: Event, cfg: dict, mailbox: str, label_ids: list[str] | None = None) -> bool:
    """True if this mail may enter the system. Pure; used by the real adapter and by tests."""
    box = gc.mailbox_cfg(cfg, mailbox)
    labels = box.get("labels") or ["INBOX"]
    rules = allowed_senders(cfg, mailbox)
    if label_ids is not None and not set(labels) & set(label_ids) and "SENT" not in label_ids:
        return False
    if ev.sender.is_principal:
        # His own mail only when it goes to an allowed party (for example his hand-filed tickets to help@).
        rcpts = gc.addresses(ev.headers.get("to", "")) + gc.addresses(ev.headers.get("cc", ""))
        return any(gc.domain_match(a, rules) for a in rcpts)
    if label_ids is not None and not set(labels) & set(label_ids):
        return False
    return bool(ev.sender.email) and gc.domain_match(ev.sender.email, rules)


def normalize_gmail(raw: dict, cfg: dict, mailbox: str) -> Event | None:
    """Gmail API message -> Event, or None if not admitted (dropped in memory, never logged)."""
    own = [a for a in [gc.mailbox_cfg(cfg, mailbox).get("address", "")] if a]
    ev = gc.to_event(raw, mailbox, own_addresses=own)
    if not admit(ev, cfg, mailbox, raw.get("labelIds")):
        return None
    if tickets.is_auto_mail(ev):
        # Ticket notifications keep the quoted text: the ticket number often sits there.
        ev = gc.to_event(raw, mailbox, own_addresses=own, channel=Channel.ticket_mail, strip=False)
        ev.text = scrub_portal_tokens(ev.text)   # portal tokens never reach state.db or a worker
    return ev


def scrub_portal_tokens(text: str) -> str:
    """Drop Acme portal access tokens from URLs; the ticket id in the path stays (needed to link)."""
    text = re.sub(r"(/my/(?:ticket|tickets|task|orders?|invoices?)/\d+)/[A-Za-z0-9-]{8,}", r"\1", text)
    return re.sub(r"([?&](?:access_token|token|signup_token|auth)=)[^\s&<>\"')\]]+", r"\1[REDACTED]", text)


def _status(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) or getattr(exc, "status_code", None)


class RealGmailIn:
    """Gmail API reader. `service_factory(cfg, mailbox)` is injectable for tests (fake service object)."""

    def __init__(self, cfg: dict, service_factory: Callable[[dict, str], Any] | None = None):
        self.cfg = cfg
        self._factory = service_factory or (lambda c, m: gc.build_service(c, m))
        # mailbox -> (service, its lock). 11 Nov 2026: one httplib2 connection is not thread-safe. The startup
        # sweep and the poller used it at the same moment and pythonw died inside libssl/libcrypto (0xC0000005).
        # Every call holds the lock of the client it uses; reset() drops the pair, so a call that hung keeps the
        # old client and lock and the next call gets fresh ones instead of waiting on it.
        self._services: dict[str, tuple[Any, threading.Lock]] = {}
        self._build_lock = threading.Lock()

    def reset(self, mailbox: str) -> None:
        with self._build_lock:
            self._services.pop(mailbox, None)

    @contextmanager
    def _svc(self, mailbox: str) -> Iterator[Any]:
        with self._build_lock:
            if mailbox not in self._services:
                gc.require_live(self.cfg, mailbox)      # refuse unless live + vault names exist
                self._services[mailbox] = (self._factory(self.cfg, mailbox), threading.Lock())
            svc, lock = self._services[mailbox]
        with lock:
            yield svc

    def _profile_history(self, svc: Any) -> str:
        return str(svc.users().getProfile(userId="me").execute()["historyId"])

    def _changed_ids(self, svc: Any, history_id: str) -> tuple[list[str], str]:
        ids: list[str] = []
        token = None
        latest = history_id
        while True:
            resp = svc.users().history().list(userId="me", startHistoryId=history_id,
                                              historyTypes=["messageAdded"], pageToken=token).execute()
            for h in resp.get("history", []):
                for added in h.get("messagesAdded", []):
                    mid = added.get("message", {}).get("id")
                    if mid and mid not in ids:
                        ids.append(mid)
            latest = str(resp.get("historyId", latest))
            token = resp.get("nextPageToken")
            if not token:
                return ids, latest

    def _query_ids(self, svc: Any, query: str, limit: int = 200) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            resp = svc.users().messages().list(userId="me", q=query, pageToken=token, maxResults=100).execute()
            ids += [m["id"] for m in resp.get("messages", []) if m["id"] not in ids]
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def _events(self, svc: Any, mailbox: str, ids: list[str]) -> list[Event]:
        out: list[Event] = []
        for mid in ids:
            try:
                raw = svc.users().messages().get(userId="me", id=mid, format="full").execute()
            except Exception as exc:  # deleted between list and get
                if _status(exc) == 404:
                    continue
                raise
            ev = normalize_gmail(raw, self.cfg, mailbox)
            if ev is not None:
                out.append(ev)
        out.sort(key=lambda e: e.ts)
        return out

    def list_new(self, mailbox: str, history_id: str | None) -> tuple[list[Event], str]:
        with self._svc(mailbox) as svc:
            return self._list_new(svc, mailbox, history_id)

    def _list_new(self, svc: Any, mailbox: str, history_id: str | None) -> tuple[list[Event], str]:
        if not history_id:
            return [], self._profile_history(svc)       # first run: baseline only, no backlog flood
        try:
            ids, latest = self._changed_ids(svc, history_id)
        except Exception as exc:
            if _status(exc) != 404:
                raise
            # History id expired (laptop off for days): re-read recent mail, reset the cursor.
            ids, latest = self._query_ids(svc, FALLBACK_QUERY), self._profile_history(svc)
        return self._events(svc, mailbox, ids), latest

    def list_today(self, mailbox: str, day: str) -> list[Event]:
        """Every admitted inbox mail received on `day` (YYYY-MM-DD, Cairo). Principal 2026-11-10: "extract emails sent
        today when the listener is on, when I open my laptop". The router skips any it already has (event_id)."""
        y, m, d = day.split("-")
        nxt = __import__("datetime").date(int(y), int(m), int(d)) + __import__("datetime").timedelta(days=1)
        query = f"in:inbox after:{y}/{m}/{d} before:{nxt.year}/{nxt.month:02d}/{nxt.day:02d}"
        with self._svc(mailbox) as svc:
            return self._events(svc, mailbox, self._query_ids(svc, query, 200))

    def search(self, mailbox: str, query: str, limit: int = 20) -> list[Event]:
        """Read-back helper, e.g. 'from:example.com newer_than:1d' or 'in:sent rfc822msgid:<id>'."""
        with self._svc(mailbox) as svc:
            return self._events(svc, mailbox, self._query_ids(svc, query, limit))

    def download_attachment(self, mailbox: str, media_ref: str, dest_dir: Path) -> Path | None:
        """Save an image attachment (screenshots only; never video). Returns the path or None if refused."""
        _, message_id, attachment_id, filename = media_ref.split(":", 3)
        if Path(filename).suffix.lower() not in IMAGE_EXT:
            return None
        with self._svc(mailbox) as svc:
            data = svc.users().messages().attachments().get(userId="me", messageId=message_id,
                                                            id=attachment_id).execute()["data"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{message_id}_{filename}"
        path.write_bytes(gc._b64(data))
        return path


class GmailPoller:
    """Polls each enabled mailbox every poll_seconds; the cursor is the Gmail history id in state.db."""

    def __init__(self, gmail_in: GmailIn, cursors: Any, cfg: dict,
                 on_event: Callable[[Event], Awaitable[None]], on_error: Callable[[str, str], None] | None = None,
                 on_ok: Callable[[str], None] | None = None):
        self.gmail_in, self.cursors, self.cfg, self.on_event = gmail_in, cursors, cfg, on_event
        self.on_error = on_error or (lambda mailbox, kind: None)
        self.on_ok = on_ok or (lambda mailbox: None)       # a clean poll: health goes back to normal
        self._fails: dict[str, int] = {}                   # consecutive failed polls per mailbox
        self._today_done: dict[str, str] = {}              # mailbox -> Cairo day whose mail was fetched in full
        self._stop = asyncio.Event()
        self.today = lambda: __import__("datetime").datetime.now(_cairo()).date().isoformat()

    def mailboxes(self) -> list[str]:
        return [m for m, box in (self.cfg.get("mailboxes") or {}).items() if box.get("enabled", True)]

    POLL_TIMEOUT_S = 180
    REPORT_AFTER = 3

    def _reset(self, mailbox: str) -> None:
        """Drop the cached Google client so the next poll builds a fresh connection (10 Nov: after an SSLEOFError
        the old one kept failing, and one poll hung from 18:46 until a restart the next day)."""
        reset = getattr(self.gmail_in, "reset", None)
        if callable(reset):
            reset(mailbox)

    async def poll_once(self, mailbox: str) -> int:
        source = f"gmail:{mailbox}"
        # The Google client is blocking: run it in a thread so Telegram, timers and /stop never wait on Gmail.
        # The cursor is the Gmail history id, so a failed or timed-out poll loses no mail: the next one reads on
        # from the same point.
        try:
            events, latest = await asyncio.wait_for(
                asyncio.to_thread(self.gmail_in.list_new, mailbox, self.cursors.get_cursor(source)),
                timeout=self.POLL_TIMEOUT_S)
        except BaseException:
            self._reset(mailbox)
            raise
        for ev in events:
            await self.on_event(ev)
        self.cursors.set_cursor(source, latest)        # only after every event was handed over
        return len(events)

    async def catch_up_today(self, mailbox: str) -> int:
        """Principal 2026-11-10: when the listener starts (he opens his laptop) and when the day changes, hand over
        ALL of today's admitted inbox mail, not only what arrived since the last cursor. The router skips mail it
        already has, so a mail is never carded twice. Off with mailboxes.<box>.today_catchup: false."""
        box = (self.cfg.get("mailboxes") or {}).get(mailbox) or {}
        lister = getattr(self.gmail_in, "list_today", None)
        day = self.today()
        if box.get("today_catchup", True) is False or not callable(lister) or self._today_done.get(mailbox) == day:
            return 0
        try:
            events = await asyncio.wait_for(asyncio.to_thread(lister, mailbox, day), timeout=self.POLL_TIMEOUT_S)
        except BaseException:
            self._reset(mailbox)
            raise
        for ev in events:
            await self.on_event(ev)
        self._today_done[mailbox] = day                # only after every mail was handed over
        return len(events)

    async def run(self) -> None:
        while not self._stop.is_set():
            for mailbox in self.mailboxes():
                try:
                    await self.catch_up_today(mailbox)
                    await self.poll_once(mailbox)
                except Exception as exc:  # report the error class only, never message text (may hold data)
                    # 2026-11-10: on this PC's slow line single polls time out now and then and the next one works.
                    # Warn only after REPORT_AFTER failures in a row (~6 min): no "problem / back to normal" flapping.
                    self._fails[mailbox] = self._fails.get(mailbox, 0) + 1
                    if self._fails[mailbox] >= self.REPORT_AFTER:
                        self.on_error(mailbox, type(exc).__name__)
                else:
                    self._fails[mailbox] = 0
                    self.on_ok(mailbox)
            secs = min(int(b.get("poll_seconds", 120)) for b in (self.cfg.get("mailboxes") or {"x": {}}).values())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=secs)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
