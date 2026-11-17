"""RECEIVE (stage 1): the WhatsApp webhook edge.

``parse_all`` turns a Meta Cloud-API webhook payload into EVERY inbound message it carries (all entries, all
changes, all messages; text, voice and image/video/document). ``make_server`` exposes it over a stdlib
``http.server`` on loopback (cloudflared connects to localhost, so the bind is NOT a trust boundary):

  - POST: bad Content-Length -> 400, over ``max_body`` -> 413 (before reading), then the ``X-Hub-Signature-256``
    HMAC-SHA256 of the RAW body with the app secret (``hmac.compare_digest``) -> 401, and only THEN json parse.
  - An accepted POST is queued for ONE worker thread and answered with an EMPTY 200 (nothing about the pipeline
    result leaks to the caller; Meta only needs the 200). The result is logged server-side, redacted.
  - GET does Meta's verification handshake; the verify token is REQUIRED (no default) and compared in constant time.
  - ``GET /healthz``: a tiny JSON ``{ok, accepting, uptime_s, last_event_age_s, open_holds}`` for an external monitor (no PII;
    ``last_event_age_s`` is left out for requests that came through the tunnel). Any other path is 404.
  - With ``phone_number_id`` set, any change addressed to another number is answered 200 WITHOUT being stored or
    processed (only the count and the last 4 of its id are logged).
"""
from __future__ import annotations

import hmac
import hashlib
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .model import Inbound, detect_lang

log = logging.getLogger("twin_agent.hub.webhook")

_MEDIA_TYPES = ("image", "video", "document")


# ---------------------------------------------------------------- parsing

def parse_all(payload: dict, phone_number_id: str | None = None) -> list[Inbound]:
    """Every inbound message in a Meta webhook payload. ``phone_number_id`` None = no filter; otherwise only
    changes addressed to that number are read (an empty string matches nothing: fail closed)."""
    out: list[Inbound] = []
    for entry in _as_list((payload or {}).get("entry")):
        for change in _as_list((entry or {}).get("changes")):
            value = (change or {}).get("value") or {}
            if phone_number_id is not None and \
                    str((value.get("metadata") or {}).get("phone_number_id") or "") != str(phone_number_id):
                continue
            out.extend(_change_messages(value))
    return out


def parse(payload: dict) -> Inbound | None:
    """First inbound message, or None (status callback / nothing parsable). Kept for single-message callers."""
    got = parse_all(payload)
    return got[0] if got else None


def _as_list(x) -> list:
    return x if isinstance(x, list) else []


def _change_messages(value: dict) -> list[Inbound]:
    contacts = {str((c or {}).get("wa_id") or ""): str(((c or {}).get("profile") or {}).get("name") or "")
                for c in _as_list(value.get("contacts"))}
    out = []
    for msg in _as_list(value.get("messages")):
        try:
            inb = _one(msg, contacts)
        except (AttributeError, TypeError, ValueError):
            inb = None
        if inb is not None:
            out.append(inb)
    return out


def _one(msg: dict, contacts: dict[str, str]) -> Inbound | None:
    wa_id = str(msg.get("from") or "")
    base = dict(message_id=str(msg.get("id") or ""), wa_id=wa_id, name=contacts.get(wa_id, ""),
                ts=_ts(msg.get("timestamp")))
    mtype = msg.get("type")
    if mtype == "text":
        body = str((msg.get("text") or {}).get("body") or "")
        return Inbound(kind="text", text=body, lang=detect_lang(body), **base)
    if mtype == "interactive":                       # a tapped reply button (intake v2): the title is the text
        br = (msg.get("interactive") or {}).get("button_reply") or {}
        title = str(br.get("title") or "")
        return Inbound(kind="text", text=title, lang=detect_lang(title), button_id=str(br.get("id") or "") or None, **base)
    if mtype in ("audio", "voice"):
        audio = msg.get("audio") or msg.get("voice") or {}
        return Inbound(kind="voice", audio_id=str(audio.get("id") or ""), **base)
    if mtype in _MEDIA_TYPES:
        m = msg.get(mtype) or {}
        cap = str(m.get("caption") or "")
        return Inbound(kind="media", text=cap or None, lang=detect_lang(cap), media_id=str(m.get("id") or ""),
                       media_type=mtype, mime=str(m.get("mime_type") or "") or None, **base)
    return None                                     # reaction/location/etc.: not a support message


def _ts(raw) -> datetime:
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------- server

def _safe_summary(result) -> str:
    """A server-side log line: ids/stages only, then run through the shared redactor (no phone/name/token)."""
    from ..redact import redact
    keep = {}
    if isinstance(result, dict):
        for k in ("ticket_id", "stage", "held", "ignored", "duplicate", "error", "resolved", "reopened"):
            if k in result:
                keep[k] = result[k]
        if "batch" in result:
            keep["batch"] = [_summary_dict(r) for r in result["batch"]]
    return redact(json.dumps(keep, default=str))


def own_number_only(payload, phone_number_id) -> tuple[dict | None, int, str]:
    """(payload with only OUR number's changes or None when nothing of ours is left, FOREIGN count, last 4 of
    the last foreign id). A change naming another ``metadata.phone_number_id`` is foreign; a change that names no number is
    dropped but not counted (nothing to route); an empty own id drops everything."""
    own = str(phone_number_id or "")
    if not isinstance(payload, dict):
        return None, 0, ""
    keep, dropped, last = [], 0, ""
    for entry in _as_list(payload.get("entry")):
        changes = []
        for ch in (_as_list(entry.get("changes")) if isinstance(entry, dict) else [None]):
            meta = ((ch or {}).get("value") or {}).get("metadata") if isinstance(ch, dict) and isinstance(ch.get("value"), dict) else None
            pid = str((meta or {}).get("phone_number_id") or "") if isinstance(meta, dict) else ""
            if own and pid == own:
                changes.append(ch)
            elif pid:                                 # names ANOTHER number: foreign (staging answers 503)
                dropped, last = dropped + 1, pid[-4:]
            # a change that names no number (template status, account or quality update) is ignored: not foreign
        if changes:
            keep.append({**entry, "changes": changes})
    return ({**payload, "entry": keep} if keep else None), dropped, last


def _summary_dict(r) -> dict:
    return {k: r[k] for k in ("ticket_id", "stage", "held", "ignored", "duplicate", "error") if
            isinstance(r, dict) and k in r}


class HubServer(ThreadingHTTPServer):
    """Loopback server. The POST only persists + answers; processing runs on a SMALL WORKER POOL inside this one
    process (the hub's per-sender locks keep one customer's messages in order, so a slow PROVE-IT for one customer
    never queues the others behind it), and a timer thread runs ``hub.tick()`` (retries, backoff, approval expiry)
    in the SAME process, so the one-ticket / one-ack guarantees hold. ``drain()`` waits for queued work."""

    daemon_threads = True

    def __init__(self, addr, handler, hub, *, workers: int = 4, tick_interval: float | None = 60.0):
        super().__init__(addr, handler)
        self.hub = hub
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix="hub-worker")
        self._pending = 0
        self._cv = threading.Condition()
        self._queues: dict[str, deque] = {}           # one serial queue per sender: at most ONE in-flight job each
        self._busy: set[str] = set()
        self._stop = threading.Event()
        self.started_at = time.monotonic()
        self.last_event_at: float | None = None        # monotonic time of the last event accepted for our number
        self._ticker = None
        if tick_interval:
            self._ticker = threading.Thread(target=self._tick_loop, args=(float(tick_interval),), daemon=True)
            self._ticker.start()

    accepting = True                                   # False: signed events are answered 503 (set by make_server)

    def health(self, *, public: bool = False) -> dict:
        """Small, PII-free status for an external monitor. ``ok`` is false when the server is stopping, is not accepting
        events, or its tick thread died. ``public`` (a request that came through the tunnel) omits the last-event age,
        which would tell anyone when real clients write. ``open_holds`` is a count from the hub, when it has one."""
        now = time.monotonic()
        last = self.last_event_at
        alive = self._ticker is None or self._ticker.is_alive()
        out = {"ok": bool(not self._stop.is_set() and self.accepting and alive), "accepting": bool(self.accepting),
               "uptime_s": int(now - self.started_at)}
        if not public:
            out["last_event_age_s"] = None if last is None else int(now - last)
        counter = getattr(self.hub, "open_holds_count", None)
        if callable(counter):
            try:
                out["open_holds"] = int(counter())
            except Exception:                           # noqa: BLE001 - health must answer even if the store hiccups
                out["open_holds"] = -1
        return out

    @staticmethod
    def _sender(payload: dict) -> str:
        try:
            return str(payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"])
        except (KeyError, IndexError, TypeError):
            return ""

    def submit(self, payload: dict) -> None:
        key = self._sender(payload)
        with self._cv:
            self._pending += 1
            self._queues.setdefault(key, deque()).append(payload)
            if key in self._busy:                     # the sender already has a worker: wait in ITS queue, not on a worker
                return
            self._busy.add(key)
        self._pool.submit(self._drain_sender, key)

    def _drain_sender(self, key: str) -> None:
        while True:
            with self._cv:
                q = self._queues.get(key)
                if not q:
                    self._busy.discard(key)
                    self._queues.pop(key, None)
                    return
                payload = q.popleft()
            self._run(payload)

    def _run(self, payload: dict) -> None:
        try:
            result = self.hub.handle_webhook(payload)
            log.info("webhook processed: %s", _safe_summary(result))
        except Exception as e:                      # noqa: BLE001 - never let one payload kill a worker
            log.error("webhook processing failed: %s", type(e).__name__)
        finally:
            with self._cv:
                self._pending -= 1
                self._cv.notify_all()

    def _tick_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.hub.tick()
            except Exception as e:                  # noqa: BLE001
                log.error("tick failed: %s", type(e).__name__)

    def drain(self, timeout: float = 15.0) -> None:
        with self._cv:
            self._cv.wait_for(lambda: self._pending == 0, timeout)

    def shutdown(self) -> None:
        super().shutdown()
        self._stop.set()
        self._pool.shutdown(wait=False)


def _require(name: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required (no default); read it from the vault")
    return value


def _sig_ok(app_secret: str, raw: bytes, header: str) -> bool:
    if not header.startswith("sha256="):
        return False
    want = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.encode(), header[len("sha256="):].strip().encode())


def make_server(hub, *, verify_token: str, app_secret: str, host: str = "127.0.0.1", port: int = 0,
                max_body: int = 1_048_576, workers: int = 4, tick_interval: float | None = 60.0,
                phone_number_id: str | None = None, accept_events: bool = True, foreign_policy: str = "drop") -> HubServer:
    """A loopback webhook server bound to ``hub``. Use ``server.server_address`` for the chosen port.
    ``phone_number_id`` (None = no filter) keeps only that number's events. ``foreign_policy`` is what happens to a payload
    carrying ANY change (message or status) for another number: ``drop`` = answer 200 and ignore those changes (production: the
    number is ours alone, Meta only sends what it should); ``reject`` = answer 503 and store/process NOTHING from the payload
    (staging: the app-default callback may receive a REAL client's event for another number, and Meta never resends after a
    200, so 503 makes Meta retry it until it is routed to the right hub). ``accept_events=False`` answers a validly
    signed POST 503 without storing it (Meta retries later): used while sending is off, so nothing is half-processed."""
    verify_token = _require("verify_token", verify_token)
    app_secret = _require("app_secret", app_secret)

    class Handler(BaseHTTPRequestHandler):
        hub_ = hub
        timeout = 10                                 # a short/stalled body can't pin a thread forever

        def log_message(self, *a):                  # keep tests quiet
            return

        def do_GET(self):                            # /healthz, else Meta's verification handshake (/webhook only)
            path = urlsplit(self.path).path
            if path == "/healthz":
                public = bool(self.headers.get("CF-Connecting-IP"))      # came through the tunnel
                self._send(200, json.dumps(self.server.health(public=public)).encode(), "application/json")
                return
            if path != "/webhook":
                self._send(404, b"")
                return
            q = parse_qs(urlsplit(self.path).query)
            mode = (q.get("hub.mode") or [""])[0]
            token = (q.get("hub.verify_token") or [""])[0]
            challenge = (q.get("hub.challenge") or [""])[0]
            if mode == "subscribe" and hmac.compare_digest(token.encode(), verify_token.encode()):
                self._send(200, challenge.encode(), "text/plain")
            else:
                self._send(403, b"forbidden", "text/plain")

        def _length(self) -> int | None:
            raw = (self.headers.get("Content-Length") or "").strip()
            if not (raw.isascii() and raw.isdigit() and len(raw) < 10):
                return None
            return int(raw)

        def do_POST(self):
            if urlsplit(self.path).path != "/webhook":      # a signed POST to /healthz (or anywhere else) is not an event
                self._send(404, b"")
                return
            length = self._length()
            if length is None:
                self._send(400, b"")
                return
            if length > max_body:
                self._send(413, b"")
                self.close_connection = True
                return
            raw = self.rfile.read(length)
            if not _sig_ok(app_secret, raw, self.headers.get("X-Hub-Signature-256") or ""):
                self._send(401, b"")
                return
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self._send(400, b"")
                return
            if phone_number_id is not None:
                payload, dropped, last = own_number_only(payload, phone_number_id)
                if dropped and foreign_policy == "reject":
                    log.info("webhook: %d event(s) for another number (id ...%s): answered 503, nothing stored", dropped, last)
                    self._send(503, b"")
                    return
                if dropped:
                    log.info("webhook: dropped %d event(s) for another number (id ...%s)", dropped, last)
                if payload is None:
                    self._send(200, b"")                  # nothing of ours: answered, never stored or processed
                    return
            if not accept_events:
                log.info("webhook: events are not accepted yet (sending is off); answered 503")
                self._send(503, b"")
                return
            try:
                self.hub_.accept(payload)                 # durable BEFORE the 200: Meta does not resend after a 200
            except Exception as e:                  # noqa: BLE001 - nothing persisted -> let Meta retry
                log.error("could not persist webhook: %s", type(e).__name__)
                self._send(500, b"")
                return
            self.server.last_event_at = time.monotonic()
            self.server.submit(payload)
            self._send(200, b"")                    # empty: the pipeline result is never returned to the caller

        def _send(self, code: int, body: bytes, ctype: str = "text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HubServer((host, port), Handler, hub, workers=workers, tick_interval=tick_interval)
    srv.accepting = bool(accept_events)
    return srv
