"""The TEAM channel (ticket threads, briefs, triage lines), swappable by config: ``team_bus: telegram|teamhub``.

Two rules shape everything here:

1. APPROVALS NEVER GO THROUGH THE TEAM BUS. A client-send approval is the hub's bound, actor-checked approval
   (approver id, nonce, draft hash, expiry) on the Telegram approval gateway, whichever bus is chosen. Teamhub has no
   outbound webhook: a card click would be an LLM turn, not a decision the hub can verify (TEAMHUB-RESEARCH §0, §5.3).
2. TEAMHUB IS POST-ONLY AND CARRIES NO CLIENT DATA. ``TeamhubTeamBus.carries_client_text`` is False, so the hub hands it
   only a pre-built SAFE line (ticket id, owner, redacted summary, links), never the raw text. The bus redacts
   phones/emails/tokens again as a second layer. Teamhub data sits in the US (PDPL), so this is not optional.

Teamhub's only documented way in is an inbound webhook: each POST opens its OWN thread (``title``, ``_content``,
``_event_id`` for idempotency). It cannot append to an existing thread; a transport that can (an agent identity on a
dedicated runner) plugs in via ``supports_append``. Until then later posts open a small "update" thread.

The Teamhub webhook URL is the credential (vault name ``teamhub-hub``). It is never logged, never in an exception text.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Protocol, runtime_checkable

from ..config import TEAM_BUSES
from ..redact import redact

log = logging.getLogger(__name__)
TEAMHUB_VAULT = "teamhub-hub"


class TeamBusBlocked(RuntimeError):
    """A real adapter that cannot run (missing token, unsupported operation). The message is a plain reason."""


class TeamhubError(RuntimeError):
    """A Teamhub call failed. The message carries only an exception type or an HTTP status, never a URL."""


@runtime_checkable
class TeamBus(Protocol):
    """Post-only team channel. ``carries_client_text`` False = the hub passes only pre-built SAFE text.
    ``owns_approval_topic`` True = the per-ticket thread doubles as the Telegram approval topic (the Telegram bus)."""

    name: str
    carries_client_text: bool
    owns_approval_topic: bool

    def open_thread(self, tid: int, owner: Any, brief: str) -> str: ...
    def post(self, ref: str, text: str) -> None: ...
    def post_triage(self, text: str) -> None: ...


# ---------------------------------------------------------------- Telegram (the existing behaviour, unchanged)

class TelegramTeamBus:
    """Wraps the existing per-ticket Telegram topic. ``ref`` is the topic id as a string."""

    name = "telegram"
    carries_client_text = True
    owns_approval_topic = True

    def __init__(self, gateway):
        self._gw = gateway
        self._refs: dict[int, str] = {}

    def open_thread(self, tid: int, owner: Any, brief: str) -> str:
        if tid not in self._refs:
            topic = self._gw.open_topic(tid, owner, brief)
            self._gw.post(topic, brief)                      # the brief is the topic's first message (as before)
            self._refs[tid] = str(topic)
        return self._refs[tid]

    def post(self, ref: str, text: str) -> None:
        self._gw.post(int(ref), text)

    def post_triage(self, text: str) -> None:
        self._gw.post_triage(text)


# ---------------------------------------------------------------- Teamhub transports

@runtime_checkable
class TeamhubTransport(Protocol):
    supports_append: bool

    def open(self, title: str, content: str, event_id: str) -> str | None: ...
    def append(self, thread_id: str, content: str, event_id: str) -> None: ...


def _quiet_http_logs() -> None:
    """httpx logs 'HTTP Request: POST <url>' at INFO, and the webhook URL IS the credential."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _httpx_request(method: str, url: str, *, _transport=None, **kw) -> tuple[int, dict]:
    import httpx                                                    # lazy: tests never import it
    _quiet_http_logs()
    with httpx.Client(transport=_transport) as client:
        r = client.request(method, url, **kw)
    return r.status_code, {}


class WebhookTeamhubTransport:
    """Teamhub's inbound webhook. The vault value IS the URL (shown to a person once; rotate = replace the secret)."""

    supports_append = False

    def __init__(self, get_url: Callable[[], str], *, http: Callable[..., tuple[int, dict]] | None = None):
        self._get_url = get_url
        self._url: str | None = None
        self._http = http or _httpx_request
        _quiet_http_logs()

    def __repr__(self) -> str:
        return "WebhookTeamhubTransport(<url hidden>)"

    def open(self, title: str, content: str, event_id: str) -> str | None:
        if self._url is None:
            self._url = str(self._get_url())
        if not self._url.startswith("https://"):
            raise TeamhubError("the Teamhub webhook URL is not https")
        try:
            status, _ = self._http("POST", self._url, json={"title": title, "_content": content,
                                                            "_event_id": event_id}, timeout=5)
        except Exception as e:                                      # noqa: BLE001 - str(e) can hold the URL
            raise TeamhubError(f"network: {type(e).__name__}") from None
        if not 200 <= int(status) < 300:
            raise TeamhubError(f"http {status}")
        return None

    def append(self, thread_id: str, content: str, event_id: str) -> None:
        raise TeamBusBlocked("the Teamhub webhook cannot append to a thread: replies need a Teamhub agent identity on a "
                             "dedicated runner (TEAMHUB-RESEARCH §6.3)")


# ---------------------------------------------------------------- Teamhub

class TeamhubTeamBus:
    """POST-ONLY. Never raises: a Teamhub outage must not block the ack or the approval, so a failure is logged by
    type only and the (already safe) line goes to the ``fallback`` bus's triage instead."""

    name = "teamhub"
    carries_client_text = False
    owns_approval_topic = False

    def __init__(self, transport: TeamhubTransport, *, fallback: TeamBus | None = None):
        self._t = transport
        self._fallback = fallback
        self._threads: dict[str, str] = {}

    def __repr__(self) -> str:
        return "TeamhubTeamBus()"

    @staticmethod
    def _tid(ref: str) -> str:
        return ref.removeprefix("ticket-")

    def open_thread(self, tid: int, owner: Any, brief: str) -> str:
        ref = f"ticket-{tid}"
        content = redact(brief)
        self._send(lambda: self._remember(ref, self._t.open(f"Ticket #{tid}", content, ref)), content)
        return ref

    def _remember(self, ref: str, thread_id: str | None) -> None:
        if thread_id:
            self._threads[ref] = thread_id

    def post(self, ref: str, text: str) -> None:
        content = redact(text)
        event_id = f"{ref}-{uuid.uuid4().hex[:10]}"                  # every update is its own event (never deduped away)
        thread = self._threads.get(ref)
        if self._t.supports_append and thread:
            self._send(lambda: self._t.append(thread, content, event_id), content)
        else:
            self._send(lambda: self._t.open(f"Ticket #{self._tid(ref)} · update", content, event_id), content)

    def post_triage(self, text: str) -> None:
        content = redact(text)
        event_id = f"triage-{uuid.uuid4().hex[:12]}"
        self._send(lambda: self._t.open("Triage", content, event_id), content)

    def _send(self, call: Callable[[], Any], content: str) -> None:
        try:
            call()
        except Exception as e:                                      # noqa: BLE001
            log.error("teamhub post failed: %s", type(e).__name__)
            if self._fallback is not None:
                try:
                    self._fallback.post_triage(f"[teamhub unavailable: {type(e).__name__}] {content}")
                except Exception as e2:                             # noqa: BLE001
                    log.error("teamhub fallback post failed: %s", type(e2).__name__)


# ---------------------------------------------------------------- config -> bus

def resolve_team_bus(cfg: dict, telegram, *, has_secret: Callable[[str], bool] | None = None,
                     get_secret: Callable[[str], str] | None = None, http=None) -> TeamBus:
    """Build the bus the config names. ``teamhub`` blocks (plain reason) without the ``teamhub-hub`` vault entry; it
    never silently falls back to Telegram, and unknown values are an error."""
    mode = (cfg or {}).get("team_bus", "telegram")
    if mode == "telegram":
        return TelegramTeamBus(telegram)
    if mode != "teamhub":
        raise ValueError(f"team_bus must be one of {TEAM_BUSES}, got {mode!r}")
    if has_secret is None or get_secret is None:
        from .. import creds
        has_secret = has_secret or creds.has_secret
        get_secret = get_secret or (lambda n: creds._get(n, caller="teamhub_team_bus", purpose="team post"))
    if not has_secret(TEAMHUB_VAULT):
        raise TeamBusBlocked(f"no Teamhub token in the vault ('{TEAMHUB_VAULT}'); team_bus teamhub is blocked")
    transport = WebhookTeamhubTransport(lambda: get_secret(TEAMHUB_VAULT), http=http)
    return TeamhubTeamBus(transport, fallback=TelegramTeamBus(telegram))
