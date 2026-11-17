"""WhatsApp Business Cloud API adapter (official Meta Graph API only; no Baileys, no whatsapp-web.js). OFF by default.

HANDOFF 2026-11-02: Principal's WhatsApp goes through the official Cloud API (coexistence on his work number, or a
dedicated Acme support number). Unofficial WhatsApp Web libraries break the terms of service: never used here.

What it can do (v0.1, not wired into sender.py yet: there is no `Channel.whatsapp`; wiring it in means the
same guards, dial and approval path as Telegram):
- `send_text(to, text, last_inbound_at=, now=)`  free-form text, only inside the 24 h customer-service window
- `send_audio(to, ogg_path, last_inbound_at=, now=)`  upload the OGG/Opus file (POST /media), then an audio
  message by media id; also inside the window only, and only with voice consent (D1 + D2 + voice.enabled)
- `send_template(to, name, lang, params)`  an approved template: the only thing allowed outside the window

It refuses unless: run_mode live, send_enabled, `whatsapp.enabled`, consent D1 + `consent.channels.whatsapp`,
no STOP file, the vault name exists, `whatsapp.graph_version` is set, and `to` is in `whatsapp.allowed_recipients`
(filled on the laptop only; real numbers never go in git). The vault value is JSON {"token", "phone_number_id"}.
`to` is never logged or put in an exception; HTTP errors carry only the status and Meta's numeric error code.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

GRAPH = "https://graph.facebook.com/{version}/{phone_id}/{edge}"
DEFAULTS = {"enabled": False, "vault_name": "whatsapp-cloud", "graph_version": "", "window_hours": 24,
            "allowed_recipients": []}


class WhatsAppError(RuntimeError):
    """The API said no or the call failed. Never carries the recipient or the message text."""


class WhatsAppRefused(WhatsAppError):
    """The adapter's own safety check said no (nothing went out)."""


class WindowClosed(WhatsAppRefused):
    """Outside the 24 h customer-service window: only an approved template may be sent."""


def wa_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("whatsapp") or {})}


def in_service_window(last_inbound_at: datetime | None, now: datetime, hours: float = 24) -> bool:
    """True if the customer wrote to us less than `hours` ago (Meta's customer-service window)."""
    if last_inbound_at is None:
        return False
    age = now - last_inbound_at
    return timedelta(0) <= age < timedelta(hours=hours)


def _digits(number) -> str:
    return "".join(ch for ch in str(number or "") if ch.isdigit())


def _httpx_request(method: str, url: str, *, headers: dict, json: dict | None = None, data: dict | None = None,
                   files: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
    import httpx                       # lazy: tests and dry runs never import it
    r = httpx.request(method, url, headers=headers, json=json, data=data, files=files, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


class WhatsAppCloud:
    def __init__(self, cfg: dict, *, has_secret: Callable[[str], bool], get_fields: Callable[[str], dict] | None = None,
                 http: Callable[..., tuple[int, dict]] | None = None, stop_file: Callable[[], Path] | None = None):
        w = wa_cfg(cfg)
        why = self._config_refusal(cfg, w)
        if why is None and not has_secret(w["vault_name"]):
            why = f"vault name {w['vault_name']} is not set"
        if why:
            raise WhatsAppRefused(why)
        if get_fields is None:
            from .. import creds
            get_fields = lambda n: creds.get_fields(n, caller="whatsapp_cloud", purpose="whatsapp send")  # noqa: E731
        fields = get_fields(w["vault_name"])
        self.__token, self.phone_id = str(fields["token"]), str(fields["phone_number_id"])
        self.cfg, self.w = cfg, w
        self._http = http or _httpx_request
        if stop_file is None:
            from .. import paths
            stop_file = paths.stop_file
        self._stop_file = stop_file

    def __repr__(self) -> str:
        return "WhatsAppCloud()"

    @staticmethod
    def _config_refusal(cfg: dict, w: dict) -> str | None:
        consent = cfg.get("consent") or {}
        if cfg.get("run_mode") != "live":
            return "run_mode is not live"
        if cfg.get("send_enabled") is not True:
            return "send_enabled is false"
        if w["enabled"] is not True:
            return "whatsapp.enabled is false"
        if not consent.get("d1_ref") or (consent.get("channels") or {}).get("whatsapp") is not True:
            return "no WhatsApp consent (D1 + consent.channels.whatsapp)"
        if not str(w["graph_version"]).strip():
            return "whatsapp.graph_version is not set"
        return None

    # ------------------------------------------------------------ checks

    def _check(self, to, *, last_inbound_at: datetime | None, now: datetime, template: bool = False) -> str:
        why = self._config_refusal(self.cfg, self.w)
        if why:
            raise WhatsAppRefused(why)
        if self._stop_file().exists():
            raise WhatsAppRefused("kill switch (STOP file)")
        number = _digits(to)
        allowed = {_digits(x) for x in self.w["allowed_recipients"] or []} - {""}
        if not number or number not in allowed:
            raise WhatsAppRefused("recipient is not on whatsapp.allowed_recipients")
        if not template and not in_service_window(last_inbound_at, now, float(self.w["window_hours"])):
            raise WindowClosed("outside the 24 h customer-service window: approved template only")
        return number

    def _url(self, edge: str) -> str:
        return GRAPH.format(version=str(self.w["graph_version"]).strip(), phone_id=self.phone_id, edge=edge)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.__token}"}

    def _call(self, edge: str, **kw) -> dict:
        try:
            status, body = self._http("POST", self._url(edge), headers=self._headers(), timeout=30, **kw)
        except Exception as e:  # noqa: BLE001 - network: we cannot know if it went out (maybe_sent)
            raise WhatsAppError(f"network: {type(e).__name__}") from None
        if status != 200:
            code = ((body or {}).get("error") or {}).get("code")
            raise WhatsAppError(f"http {status}" + (f" code {code}" if code is not None else ""))
        return body or {}

    def _message(self, number: str, payload: dict) -> dict:
        body = self._call("messages", json={"messaging_product": "whatsapp", "recipient_type": "individual",
                                            "to": number, **payload})
        ids = [m.get("id") for m in body.get("messages") or [] if isinstance(m, dict)]
        if not ids or not ids[0]:
            raise WhatsAppError("no message id in the answer")
        return {"msg_id": str(ids[0])}

    # ------------------------------------------------------------ sends

    def send_text(self, to, text: str, *, last_inbound_at: datetime | None, now: datetime) -> dict:
        number = self._check(to, last_inbound_at=last_inbound_at, now=now)
        return self._message(number, {"type": "text", "text": {"body": text, "preview_url": False}})

    def send_interactive(self, to, interactive: dict, *, last_inbound_at: datetime | None, now: datetime) -> dict:
        """An interactive message (reply buttons). Free-form, so the same checks as ``send_text`` (config, STOP file,
        allow-list, 24 h window)."""
        number = self._check(to, last_inbound_at=last_inbound_at, now=now)
        return self._message(number, {"type": "interactive", "interactive": interactive})

    def upload_audio(self, ogg_path) -> str:
        path = Path(ogg_path)
        if path.suffix.lower() != ".ogg" or not path.is_file():
            raise WhatsAppRefused("audio must be an existing .ogg (Opus) file")
        with path.open("rb") as f:
            body = self._call("media", data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                              files={"file": (path.name, f.read(), "audio/ogg")})
        media_id = body.get("id")
        if not media_id:
            raise WhatsAppError("no media id in the answer")
        return str(media_id)

    def send_audio(self, to, ogg_path, *, last_inbound_at: datetime | None, now: datetime) -> dict:
        from ..voice.policy import voice_consent_ok
        if not voice_consent_ok(self.cfg):
            raise WhatsAppRefused("no voice consent (D1 + D2 + voice.enabled)")
        number = self._check(to, last_inbound_at=last_inbound_at, now=now)
        media_id = self.upload_audio(ogg_path)
        return self._message(number, {"type": "audio", "audio": {"id": media_id}})

    def send_template(self, to, name: str, lang: str, params: list[str] | None = None, *, now: datetime) -> dict:
        """An approved template (any time, also outside the window). Only body text parameters in v0.1."""
        number = self._check(to, last_inbound_at=None, now=now, template=True)
        template = {"name": name, "language": {"code": lang}}
        if params:
            template["components"] = [{"type": "body",
                                       "parameters": [{"type": "text", "text": str(p)} for p in params]}]
        return self._message(number, {"type": "template", "template": template})
