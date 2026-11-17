"""STAGING hub runner: ``python -m twin_agent.hub.staging serve --config staging.yaml``.

One process on 127.0.0.1 only (cloudflared connects to localhost) that runs, around ONE ``StagingHub``:
  - the WhatsApp webhook (``webhook.make_server``, HMAC + verify handshake, hub tick thread),
  - the admin page (``--graph real`` views + the approval flow) and the Embedded Signup launcher (its own port/origin),
  - the existing Teamhub ticket watcher (``teamhub_threads`` / ``odoo_ticket_watch``) on the tick, TEST tickets only.
The webhook and the admin page MUST share one process: ``SqliteStore`` allows one hub per file.

REAL here: the Cloud API send (our own TEST number only) and Odoo through the guard (purpose ``test_ticket``).
FAKE here: STT (blocked, never canned text), judge, learning, Metrics, proof, the Telegram/approval transport (the admin
page approves). A missing real adapter refuses to start with a plain reason; nothing silently falls back to a fake.

Rails (all in code, all tested): the TEST number only (Executive's refused); ``allowed_senders`` (default EMPTY) gates both
what is processed and every send; every ticket is a ``TEST · `` ticket of team 2 for the pinned TEST contact, owned by
our own uid, mail off, no deletes; loopback bind only; no history sync and no webhook-subscription or callback writes from
code; Teamhub card presses are NOT acted on (this module never runs the press processor); logs are redacted.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..channels import whatsapp_cloud
from ..helpdesk import guard
from . import fakes, judge, learning, proof_runner, routing
from . import teamhub_test_ticket as ktt
from .admin.config import DEFAULT_BUSINESS_DIR
from .admin.own_waba import OWN_LABEL, OwnWabaReader, WithOwnWaba
from .model import Owner
from .odoo_gateway import MAIL_SUPPRESS, GuardedOdoo
from .pipeline import Hub, _ref
from .staging_support import AccessGate, TeamhubTimer, fake_transport, no_address_reuse, port_in_use
from .staging_users import deliver, users_for_run, write_staff_users
from .store import SqliteStore

log = logging.getLogger("twin_agent.hub.staging")

TEST_PHONE_NUMBER_ID = "1000000000011"          # Acme TEST number +1 555-010-0099 (our own WABA)
TEST_WABA_ID = "1000000000012"
TEST_DISPLAY = "15550100099"
from .. import profile as _profile
EXECUTIVE_NUMBER = str(_profile.get("protected_numbers")[0])                       # never allowed anywhere in a staging config
LOOPBACK = ("127.0.0.1", "localhost", "::1")
MARKER = ("admin_meta", "staging")
IGNORED = "ignored (not on the staging allowlist)"
SECRETS_DIR = Path.home() / ".claude" / "secrets"


class StagingRefused(Exception):
    """A rail said no. The text is a plain reason and never holds a secret."""


def _digits(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def _last4(v) -> str:
    return "..." + _digits(v)[-4:]


# ---------------------------------------------------------------- config + validation

@dataclass(frozen=True)
class OwnWabaConfig:
    """Opt-in: show OUR OWN WABA (read-only, own token file) on the admin Connected numbers / Templates pages."""
    waba_id: str = ""
    label: str = OWN_LABEL
    token_file: str = ""                              # "" = the system-user file; never the Embedded Signup store


@dataclass(frozen=True)
class StagingConfig:
    phone_number_id: str = ""
    waba_id: str = TEST_WABA_ID
    display_phone_number: str = TEST_DISPLAY
    test_partner_id: int = 0
    allowed_senders: tuple = ()
    bind_host: str = "127.0.0.1"
    webhook_port: int = 8790
    admin_port: int = 8791
    launcher_port: int = 8792
    public_host: str = "staging.example.com"          # the webhook (/webhook); the admin page too ONLY if public_admin
    public_admin: bool = False                        # False: the admin page is local (127.0.0.1) only, not on the tunnel
    cloudflare_access: bool = False                   # operator attestation: Access protects the admin host (required for public_admin)
    launcher_host: str = "signup.staging.example.com"  # its OWN origin (cookies ignore ports)
    state_db: str = str(Path.home() / ".claude" / "aa-staging" / "hub-staging.db")
    app_secret_file: str = str(SECRETS_DIR / "meta-acme-app.json")
    system_user_file: str = str(SECRETS_DIR / "meta-acme-system-user.json")
    signup_file: str = str(SECRETS_DIR / "meta-acme-embedded-signup.json")
    verify_token_file: str = str(SECRETS_DIR / "aa-staging-verify-token.txt")
    odoo_secret_file: str = str(SECRETS_DIR / "odoo.json")
    users_file: str = str(SECRETS_DIR / "aa-staging-admin-users.json")
    business_dir: str = str(SECRETS_DIR / "aa-staging-meta-businesses")   # NEVER the default admin dir
    staff_approver_id: int = 0
    client_channel: str = "whatsapp"
    send_enabled: bool = False                        # explicit: the Cloud API send is OFF until staging.yaml says so
    consent_ref: str = ""                             # explicit: why sending to the allowlisted testers is allowed
    teamhub_config: str = ""                            # an existing config.yaml with teamhub_tickets; "" = watcher off
    teamhub_interval_s: int = 120
    teamhub_timeout_s: int = 120                        # one watcher pass may not run longer than this
    tick_interval_s: int = 60
    own_waba: OwnWabaConfig | None = None             # default OFF; see docs/STAGING-RUNNER.md "Own number in the admin pages"


_FIELD_NAMES = {f.name for f in fields(StagingConfig)}


def _mentions_executive(node) -> bool:
    if isinstance(node, dict):
        return any(_mentions_executive(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_mentions_executive(v) for v in node)
    return EXECUTIVE_NUMBER in _digits(node) if isinstance(node, (str, int)) else False


def config_from_dict(raw: dict) -> StagingConfig:
    raw = dict(raw or {})
    raw.pop("teamhub_tickets", None)                    # an existing config.yaml may be reused as-is
    unknown = sorted(set(raw) - _FIELD_NAMES)
    if unknown:
        raise StagingRefused(f"unknown staging config key(s): {', '.join(unknown)}")
    if _mentions_executive(raw):
        raise StagingRefused("the config mentions Executive's number: it is never allowed on staging")
    own = raw.pop("own_waba", None)
    kw = {"own_waba": _own_waba(own)} if own is not None else {}
    for k, v in raw.items():
        default = getattr(StagingConfig, k)
        if k == "allowed_senders":
            kw[k] = tuple(_digits(x) for x in (v or []))
        elif isinstance(default, bool):
            kw[k] = v is True
        elif isinstance(default, int):
            kw[k] = _int(v)
        else:
            kw[k] = str(v if v is not None else "")
    return StagingConfig(**kw)


def _own_waba(raw) -> OwnWabaConfig:
    if not isinstance(raw, dict) or not raw or set(raw) - {"waba_id", "label", "token_file"}:
        raise StagingRefused("own_waba must be a block with waba_id, label and token_file only")
    return OwnWabaConfig(str(raw.get("waba_id") or ""), str(raw.get("label") or OWN_LABEL), str(raw.get("token_file") or ""))


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def load_config(path: str) -> StagingConfig:
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            return config_from_dict(yaml.safe_load(f) or {})
    except OSError:
        raise StagingRefused(f"cannot read the staging config {path}") from None


def problems(cfg: StagingConfig, *, have_app_secret: bool, have_verify_token: bool) -> list[str]:
    """Every reason this config may not run, in plain words. Empty list = valid. Pure: no files, no network."""
    out: list[str] = []
    if cfg.phone_number_id != TEST_PHONE_NUMBER_ID:
        out.append("phone_number_id must be the Acme TEST number's id (any other number, including Executive's, is refused)")
    if cfg.waba_id != TEST_WABA_ID:
        out.append("waba_id must be our own WABA")
    if _digits(cfg.display_phone_number) != TEST_DISPLAY:
        out.append("display_phone_number must be the Acme TEST number (+1 555-010-0099)")
    if cfg.test_partner_id <= 0:
        out.append("test_partner_id is required (the pinned TEST contact's res.partner id)")
    if cfg.bind_host not in LOOPBACK:
        out.append("bind_host must be loopback (127.0.0.1): cloudflared connects to localhost")
    if not have_app_secret:
        out.append("the Meta app secret is missing (meta-acme-app.json)")
    if not have_verify_token:
        out.append("the webhook verify token is missing")
    if cfg.send_enabled and not cfg.consent_ref.strip():
        out.append("consent_ref must say why sending to the allowlisted testers is allowed (blank is refused)")
    if cfg.public_admin and not cfg.cloudflare_access:
        out.append("public_admin needs cloudflare_access: true (Cloudflare Access in front of the admin page; a password alone is not enough)")
    if _same_dir(cfg.business_dir, DEFAULT_BUSINESS_DIR):
        out.append("business_dir must not be the default admin token store (staging keeps its own)")
    if cfg.client_channel != "whatsapp":
        out.append("client_channel must be whatsapp (an approved reply is sent on WhatsApp, not by Odoo email)")
    if cfg.staff_approver_id <= 0:
        out.append("staff_approver_id is required (the admin user's approver id)")
    out += _sender_problems(cfg) + _port_problems(cfg) + _own_waba_problems(cfg)
    return out


def _own_waba_problems(cfg: StagingConfig) -> list[str]:
    o = cfg.own_waba
    if o is None:
        return []
    out = []
    if o.waba_id != TEST_WABA_ID:
        out.append("own_waba.waba_id must be our own WABA (any other account is refused)")
    if o.label != OWN_LABEL:
        out.append(f"own_waba.label must be exactly '{OWN_LABEL}'")
    tf = o.token_file or cfg.system_user_file
    for d in (cfg.business_dir, DEFAULT_BUSINESS_DIR):
        if _inside(tf, d):
            out.append("own_waba.token_file must not live in an Embedded Signup token store")
    return out


def _inside(path, folder) -> bool:
    a, b = (os.path.normcase(os.path.realpath(os.path.expanduser(str(x)))) for x in (path, folder))
    return a == b or a.startswith(b.rstrip("/" + os.sep) + os.sep)


def _same_dir(a, b) -> bool:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(a)))) == os.path.normcase(os.path.abspath(os.path.expanduser(str(b))))


def _sender_problems(cfg: StagingConfig) -> list[str]:
    out = []
    for n in cfg.allowed_senders:
        if not (8 <= len(n) <= 15):
            out.append("an allowed_senders entry is not a wa_id (digits only, 8-15)")
        if n in (EXECUTIVE_NUMBER, TEST_DISPLAY):
            out.append("allowed_senders may not hold Executive's number or the TEST number itself")
    return out


def _port_problems(cfg: StagingConfig) -> list[str]:
    ports = (cfg.webhook_port, cfg.admin_port, cfg.launcher_port)
    out = []
    nonzero = [p for p in ports if p]                  # 0 = "any free port" (tests only)
    if len(set(nonzero)) != len(nonzero) or any(not (0 <= p <= 65535) for p in ports):
        out.append("webhook_port, admin_port and launcher_port must be three different valid ports")
    if cfg.public_host == cfg.launcher_host or not cfg.launcher_host or not cfg.public_host:
        out.append("launcher_host must be a different host name from public_host (its own origin)")
    return out


# ---------------------------------------------------------------- secrets (files only; never printed)

@dataclass
class Secrets:
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    verify_token: str = field(default="", repr=False)
    wa_token: str = field(default="", repr=False)
    config_id: str = ""
    odoo_key: str = field(default="", repr=False)

    def values(self) -> list[str]:
        return [v for v in (self.app_secret, self.verify_token, self.wa_token, self.odoo_key) if v]


def _read_json(path: str) -> dict:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def ensure_verify_token(path: str) -> str:
    """Read the verify token, or create it (random, mode 0600, O_EXCL) when the file is absent. Never printed."""
    p = Path(os.path.expanduser(path))
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def load_secrets(cfg: StagingConfig, *, dry_run: bool) -> Secrets:
    if dry_run:                                        # ephemeral, in memory: a dry run never reads or writes the vault
        return Secrets("dry-run-app", secrets.token_hex(16), secrets.token_hex(16), "", "")
    app = _read_json(cfg.app_secret_file)
    s = Secrets(str(app.get("app_id") or ""), str(app.get("app_secret") or ""), ensure_verify_token(cfg.verify_token_file),
                str(_read_json(cfg.system_user_file).get("system_user_token") or ""),
                str(_read_json(cfg.signup_file).get("config_id") or ""),
                str(_read_json(cfg.odoo_secret_file).get("api_key") or ""))
    if cfg.send_enabled and not s.wa_token:
        raise StagingRefused("send_enabled is on but the system-user token is missing (meta-acme-system-user.json)")
    return s


def check_config(cfg: StagingConfig, sec: Secrets) -> None:
    why = problems(cfg, have_app_secret=bool(sec.app_secret), have_verify_token=bool(sec.verify_token))
    if why:
        raise StagingRefused("; ".join(why))


# ---------------------------------------------------------------- logs: no tokens, last 4 digits only

_LONG_DIGITS = re.compile(r"\d{8,}")


def redact_line(text: str, secret_values=()) -> str:
    from ..redact import redact
    for v in secret_values:
        text = text.replace(v, "[REDACTED:secret]")
    return redact(_LONG_DIGITS.sub(lambda m: "***" + m.group(0)[-4:], text))


class RedactFilter(logging.Filter):
    def __init__(self, secret_values=()):
        super().__init__()
        self.secret_values = list(secret_values)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact_line(record.getMessage(), self.secret_values), ()
        return True


def setup_logging(secret_values=()) -> None:
    h = logging.StreamHandler()
    h.addFilter(RedactFilter(secret_values))
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
    logging.getLogger("twin_agent.hub").setLevel(logging.INFO)     # the per-event lines (hub.events) must never be filtered out


# ---------------------------------------------------------------- adapters

class BlockedSTT:
    """Voice notes are not transcribed on staging (the only STT here would be a fixture): hold with a plain reason."""

    def transcribe(self, audio_id: str, *, lang_hint: str = "") -> dict:
        return {"status": "blocked", "why": "voice notes are not enabled on staging; send a text message", "text": None}


class StagingKnowledge:
    """Allowlisted sender -> the ONE pinned TEST contact. Any other sender is no one. No real partner record is read."""

    def __init__(self, cfg: StagingConfig):
        self.pid, self.allowed = int(cfg.test_partner_id), set(cfg.allowed_senders)

    def partner_for(self, wa_id: str) -> int | None:
        return self.pid if _digits(wa_id) in self.allowed else None

    def client_context(self, partner_id: int) -> dict:
        return {"source": "odoo", "name": guard.TEST_PARTNER, "modules": [], "owner_uids": []}


class StagingWhatsApp:
    """Every send goes through here: only to ``allowed_senders`` (empty = nothing is ever sent), then the inner sender
    (the real ``WhatsAppCloud``, or ``FakeWhatsApp`` in a dry run). The inner Cloud adapter has its own allowlist,
    24h window, consent and STOP-file checks on top."""

    def __init__(self, inner, allowed, *, takes_automatic: bool):
        self.inner, self.allowed, self.takes_automatic = inner, set(allowed), takes_automatic

    def send_interactive(self, to, interactive, *, last_inbound_at, now=None, automatic: bool = False,
                         intake: bool = False) -> dict:
        if _digits(to) not in self.allowed:
            raise whatsapp_cloud.WhatsAppRefused("recipient is not on the staging allowlist")
        return self.inner.send_interactive(to, interactive, last_inbound_at=last_inbound_at,
                                           now=now or datetime.now(timezone.utc))

    def send_text(self, to, text, *, last_inbound_at, now=None, automatic: bool = False, intake: bool = False) -> dict:
        if _digits(to) not in self.allowed:
            raise whatsapp_cloud.WhatsAppRefused("recipient is not on the staging allowlist")
        now = now or datetime.now(timezone.utc)
        kw = {"automatic": automatic} if self.takes_automatic else {}
        res = self.inner.send_text(to, text, last_inbound_at=last_inbound_at, now=now, **kw)
        log.info("whatsapp sent to %s automatic=%s", _last4(to), automatic)
        return res


def build_cloud(cfg: StagingConfig, sec: Secrets) -> whatsapp_cloud.WhatsAppCloud:
    """The REAL Cloud API sender. Its gates come from the explicit staging.yaml values, never faked in code."""
    from .admin.config import GRAPH_VERSION
    wa = {"enabled": cfg.send_enabled, "vault_name": "staging", "graph_version": GRAPH_VERSION, "window_hours": 24,
          "allowed_recipients": list(cfg.allowed_senders)}
    cloud_cfg = {"run_mode": "live", "send_enabled": cfg.send_enabled, "whatsapp": wa,
                 "consent": {"d1_ref": cfg.consent_ref.strip(), "channels": {"whatsapp": bool(cfg.consent_ref.strip())}}}
    try:
        return whatsapp_cloud.WhatsAppCloud(cloud_cfg, has_secret=lambda n: True, get_fields=lambda n: {
            "token": sec.wa_token, "phone_number_id": TEST_PHONE_NUMBER_ID})
    except whatsapp_cloud.WhatsAppRefused as e:
        raise StagingRefused(f"the WhatsApp Cloud sender is blocked: {e}. Set send_enabled and consent_ref in "
                             "staging.yaml (own TEST number, allowlisted testers only)") from None


class StagingOdoo(GuardedOdoo):
    """Odoo for staging. Built with writes OFF so any inherited ``helpdesk``-purpose write is refused; the ONE write is a
    TEST ticket through ``teamhub_test_ticket.write`` (guard purpose ``test_ticket``: ``TEST · `` name, team 2, the pinned
    TEST contact re-read live, our own uid, the exact mail-off context). Assign and notes are local no-ops."""

    def __init__(self, transport, *, test_partner_id: int):
        super().__init__(transport, writes_on=False)
        self.test_partner_id = int(test_partner_id)
        self.stage_id, self.team_stage_ids = 0, ()
        self._local = threading.local()

    @property
    def uid(self) -> int:
        return int(getattr(self.t, "uid", 0) or 0)

    def preflight(self) -> None:
        """Reads only. Fails closed with a plain reason before the first message can arrive."""
        login = getattr(self.t, "authenticate", None)       # the real transport logs in lazily: uid is 0 until it does
        if login:
            try:
                login()
            except Exception as e:                           # noqa: BLE001 - class name only, never the key
                raise StagingRefused(f"Odoo login failed ({type(e).__name__}): check odoo_secret_file") from None
        if self.uid <= 0:
            raise StagingRefused("our own Odoo uid is unknown (login refused?)")
        try:
            stages = ktt.pick_stages(self.t, need_close=False)
            ktt.verify_test_contact(self.t, pid=self.test_partner_id)
        except (ktt.Refuse, guard.WriteRefused, PermissionError) as e:
            raise StagingRefused(f"Odoo preflight: {e}") from None
        if not stages["open"]:
            raise StagingRefused("Odoo preflight: team 2 has no open stage")
        if stages["needs_contact_check"]["open"]:
            raise StagingRefused("Odoo preflight: team 2's opening stage has an email template; staging needs a silent "
                                 "open stage (a ticket there could email someone)")
        self.stage_id, self.team_stage_ids = int(stages["open"]), tuple(stages["team_stage_ids"])

    @contextlib.contextmanager
    def sender_scope(self, name: str, wa_id: str):
        self._local.sender = (name, wa_id)
        try:
            yield
        finally:
            self._local.sender = None

    def _description(self, vals: dict) -> str:
        who = getattr(self._local, "sender", None)
        base = str(vals.get("description") or "")
        if not who:
            return base
        return base + f"<p><b>[Staging]</b> WhatsApp sender: allowlisted tester ({_last4(who[1])})</p>"

    def create_ticket(self, vals: dict, *, context: dict) -> int:
        if context != MAIL_SUPPRESS:
            raise guard.WriteRefused("create refused: context must equal the mail-suppress context exactly")
        if not self.stage_id or self.uid <= 0:
            raise guard.WriteRefused("staging: preflight has not run")
        name = str(vals.get("name") or "WhatsApp request")
        clean = {"name": name if name.startswith(guard.TEST_PREFIX) else guard.TEST_PREFIX + name,
                 "description": self._description(vals), "team_id": guard.TEST_TEAM, "stage_id": self.stage_id,
                 "partner_id": self.test_partner_id, "user_id": self.uid}
        try:
            ktt.verify_test_contact(self.t, pid=self.test_partner_id)
        except ktt.Refuse as e:
            raise guard.WriteRefused(str(e)) from None
        return int(ktt.write(self.t, "helpdesk.ticket", "create", [clean], safe=(self.stage_id,), self_uid=self.uid,
                             partner_id=self.test_partner_id, team_stage_ids=self.team_stage_ids))

    def assign(self, tid: int, user_id: int) -> None:
        if int(user_id) != self.uid:
            raise guard.WriteRefused("staging: tickets are owned by our own account only")
        # already the owner from create (user_id = our uid): nothing to write

    def post_note(self, tid: int, body: str) -> None:
        log.debug("staging: Odoo note skipped (no extra Odoo writes on staging)")

    def send_client_email(self, *a, **kw):
        raise guard.WriteRefused("staging: client email is disabled")


class StagingHub(TeamhubTimer, Hub):
    """The Hub plus the staging rails. ``STAGING`` is the class-level marker the admin page checks (with the store marker
    and a fresh ``problems()`` pass); a plain ``Hub`` can never carry it."""

    STAGING = True

    def __init__(self, *, staging: StagingConfig, have_secrets: tuple[bool, bool] = (True, True), teamhub=None, **kw):
        super().__init__(**kw)
        self.staging, self.have_secrets = staging, have_secrets
        self._allowed_set = set(staging.allowed_senders)
        self.init_teamhub(teamhub, staging.teamhub_interval_s, staging.teamhub_timeout_s)
        self.store.put(*MARKER, {"staging": True})

    def _allowed(self, wa_id) -> bool:
        return _digits(wa_id) in self._allowed_set

    def _ignore(self, wa_id, message_id) -> None:
        key = _ref(message_id or f"nomid-{wa_id}-{time.time()}")
        self.store.put_if_absent("staging_ignored", key, {"reason": IGNORED, "sender": _last4(wa_id),
                                                          "at": self.clock().isoformat()})
        log.info("message %s %s (sender %s)", key, IGNORED, _last4(wa_id))

    def accept(self, payload: dict) -> int:
        """Persist ONLY allowlisted senders' messages: a stranger's message is never queued, so ``retry_failed`` and the
        fallback-ack pass can never reach it."""
        p = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        for entry in (p.get("entry") if isinstance(p.get("entry"), list) else []):
            for ch in ((entry or {}).get("changes") if isinstance((entry or {}).get("changes"), list) else []):
                val = (ch or {}).get("value") or {}
                if str((val.get("metadata") or {}).get("phone_number_id") or "") != self.staging.phone_number_id:
                    continue                          # another number's change: never touched here (parse_all drops it too)
                msgs = val.get("messages") if isinstance(val.get("messages"), list) else []
                for m in msgs:
                    if not self._allowed((m or {}).get("from")):
                        self._ignore((m or {}).get("from"), (m or {}).get("id"))
                val["messages"] = [m for m in msgs if self._allowed((m or {}).get("from"))]
        return super().accept(p)

    def _handle_one(self, inbound) -> dict:
        if not self._allowed(inbound.wa_id):
            self._ignore(inbound.wa_id, inbound.message_id)
            return {"ignored": IGNORED}
        return super()._handle_one(inbound)

    def _create(self, analysis, inbound, partner_id, previous=0, *, client="", ctx=None, **more):
        with self.odoo.sender_scope(inbound.name, inbound.wa_id):
            return super()._create(analysis, inbound, partner_id, previous, client=client, ctx=ctx, **more)


def is_staging_hub(hub) -> bool:
    """True only for a hub that (1) is a ``StagingHub``, (2) carries the staging marker doc, and (3) whose config passes
    ``problems()`` again right now. The admin page uses this as its second allowed kind (besides the demo hub)."""
    cfg = getattr(hub, "staging", None)
    if not getattr(type(hub), "STAGING", False) or not isinstance(cfg, StagingConfig):
        return False
    if hub.store.get(*MARKER) is None or str(hub.cfg.get("phone_number_id")) != cfg.phone_number_id:
        return False
    if not (isinstance(hub.odoo, StagingOdoo) and not hub.odoo.adapter.writes_on
            and isinstance(hub.whatsapp, StagingWhatsApp) and hub.whatsapp.allowed == set(cfg.allowed_senders)):
        return False                                    # the sender and Odoo gateway must be the staging-safe types
    app_ok, tok_ok = getattr(hub, "have_secrets", (False, False))
    return not problems(cfg, have_app_secret=app_ok, have_verify_token=tok_ok)


# ---------------------------------------------------------------- the dry-run Odoo (same wiring, no network)

def real_transport(cfg: StagingConfig):
    from ..helpdesk.odoo import JsonRpcTransport
    s = _read_json(cfg.odoo_secret_file)
    if not all(s.get(k) for k in ("url", "db", "login", "api_key")):
        raise StagingRefused("the Odoo secret file is missing or incomplete (odoo_secret_file)")
    return JsonRpcTransport(str(s["url"]), str(s["db"]), str(s["login"]), str(s["api_key"]))


# ---------------------------------------------------------------- assembly

def make_teamhub_runner(cfg: StagingConfig) -> Callable[[], dict] | None:
    """The EXISTING Teamhub watcher (``watch_once``), TEST tickets only. It never calls ``process_submissions``, so a card
    press is inert on staging. ``""`` = off."""
    if not cfg.teamhub_config:
        return None
    from . import teamhub_threads as kt
    from .odoo_ticket_watch import watch_once
    try:
        st = kt.load_settings(cfg.teamhub_config)
    except (kt.ConfigError, OSError) as e:
        raise StagingRefused(f"teamhub_config: {e}") from None
    flt = st.filter or {}
    if int(flt.get("team_id") or 0) != guard.TEST_TEAM or not str(flt.get("name_like") or "").startswith(guard.TEST_PREFIX):
        raise StagingRefused("teamhub_config: the filter must be team 2 and name_like 'TEST · %' (TEST tickets only)")
    if st.test_partner_id and st.test_partner_id != cfg.test_partner_id:
        raise StagingRefused("teamhub_config: test_partner_id differs from the staging test_partner_id")
    return teamhub_pass(st)


def teamhub_pass(st) -> Callable[[], dict]:
    """One watcher pass for settings ``st`` (shared by staging and production; neither runs the press processor)."""
    from . import teamhub_threads as kt
    from .odoo_ticket_watch import watch_once

    def run() -> dict:
        try:
            with kt.single_instance(st.state_dir):
                ctx = kt.build_ctx(st, dry_run=False, need_odoo=True)
                try:
                    ctx.queue.check_gateway()
                    return {k: (len(v) if isinstance(v, list) else v) for k, v in watch_once(ctx).items()}
                finally:
                    ctx.store.close()
        except kt.Refused as e:
            return {"skipped": str(e)}
    return run


def _team(own_uid: int, approver_id: int):
    agent = Owner(uid=own_uid, name="Acme Support", tg_id=approver_id, role="support")
    return routing.RuleAssigner([routing.Candidate(agent, skills=("Inventory", "Sales", "Accounting", "Helpdesk"),
                                                   handles_tracks=("inquiry", "functional", "development"))])


def build_hub(cfg: StagingConfig, sec: Secrets, *, dry_run: bool, transport=None, whatsapp_inner=None, teamhub=None,
              store=None, clock=None) -> StagingHub:
    """Validate, then wire the staging hub. ``transport`` / ``whatsapp_inner`` / ``store`` are injection points for tests."""
    check_config(cfg, sec)
    t = transport or (fake_transport(cfg) if dry_run else real_transport(cfg))
    try:
        odoo = StagingOdoo(t, test_partner_id=cfg.test_partner_id)
    except Exception as e:                                          # noqa: BLE001 - discovery reads failed
        raise StagingRefused(f"Odoo is not usable ({type(e).__name__})") from None
    odoo.preflight()
    if whatsapp_inner is not None:
        inner, auto = whatsapp_inner, hasattr(whatsapp_inner, "automatic_count")
    elif dry_run:
        inner, auto = fakes.FakeWhatsApp(), True
    else:
        inner, auto = build_cloud(cfg, sec), False
    if store is None:
        if dry_run:
            store = SqliteStore(":memory:")
        else:
            Path(os.path.expanduser(cfg.state_db)).parent.mkdir(parents=True, exist_ok=True)
            store = SqliteStore(os.path.expanduser(cfg.state_db))
    hub_cfg = {"run_mode": "staging", "phone_number_id": cfg.phone_number_id, "client_channel": cfg.client_channel,
               "approver_tg_ids": [cfg.staff_approver_id], "team_bus": "telegram"}
    kw = dict(odoo=odoo, whatsapp=StagingWhatsApp(inner, cfg.allowed_senders, takes_automatic=auto), stt=BlockedSTT(),
              telegram=fakes.FakeTelegram(), knowledge_odoo=StagingKnowledge(cfg),
              knowledge_wiki=fakes.FakeWikiKnowledge(context={}), assigner=_team(odoo.uid, cfg.staff_approver_id),
              proof=proof_runner.FakeProofRunner(), metrics_connector=fakes.FakeMetrics(), judge=judge.FakeJudge(),
              learning=learning.FakeLearningLog(), cfg=hub_cfg, store=store)
    if clock:
        kw["clock"] = clock
    return StagingHub(staging=cfg, have_secrets=(bool(sec.app_secret), bool(sec.verify_token)), teamhub=teamhub, **kw)


def build_admin(cfg: StagingConfig, sec: Secrets, hub: StagingHub, *, dry_run: bool, users_file: str):
    """(admin app, launcher app or None). Reuses the admin CLI's own ``_build`` so the Host allowlist, proxy trust and
    launcher-origin rules are the tested ones. Meta writes are hard-wired OFF (no flag)."""
    import argparse as ap
    from .admin import __main__ as admin_cli
    real = not dry_run
    biz_dir = tempfile.mkdtemp(prefix="aa-stg-dry-biz-") if dry_run else cfg.business_dir   # dry run never touches the real store
    proxies = ["127.0.0.1/32"] if (cfg.public_admin or real) else []        # the launcher host (https) needs a trusted proxy
    ns = ap.Namespace(users_file=users_file, host=cfg.bind_host, port=cfg.admin_port, graph="real" if real else "fake",
                      app_id=sec.app_id if real else "", config_id=sec.config_id if real else "",
                      launcher_url=f"https://{cfg.launcher_host}" if real else "",
                      launcher_port=cfg.launcher_port if real else 0, business_dir=biz_dir,
                      app_secret_file=cfg.app_secret_file, allowed_host=[cfg.public_host] if cfg.public_admin else [],
                      trusted_proxy=proxies, cookie_secure="auto", allow_meta_writes=False)
    try:
        app, launcher = admin_cli._build(ns, hub, True)
    except ValueError as e:
        raise StagingRefused(f"admin page: {e}") from None
    if real and cfg.own_waba:                       # opt-in, read-only, its own token file: not in the ES store
        o = cfg.own_waba
        app.graph = WithOwnWaba(app.graph, OwnWabaReader(o.waba_id, o.token_file or cfg.system_user_file,
                                                         app_id=sec.app_id, label=o.label))
    app.cfg.banner = ("STAGING (TEST number, TEST tickets only). Approved replies go to allowlisted testers on WhatsApp. "
                      + ("Dry run: everything is fake." if dry_run else "Meta reads are REAL; Meta writes are OFF."))
    return (AccessGate(app) if cfg.public_admin else app), launcher


# ---------------------------------------------------------------- run

class Runtime:
    """The three servers around one hub. ``open`` binds them; ``run`` serves until stopped; ``close`` shuts down only what
    started (``shutdown()`` on a server that never ran ``serve_forever`` would block forever)."""

    def __init__(self, cfg, sec, hub, app, launcher):
        from . import webhook
        from .admin.server import make_threaded_server
        self.hub, self._running = hub, []
        self.webhook = self.admin = self.launcher = None
        busy = [n for n, p in (("webhook", cfg.webhook_port), ("admin", cfg.admin_port),
                               ("launcher", cfg.launcher_port if launcher else 0)) if port_in_use(cfg.bind_host, p)]
        if busy:                                         # e.g. an older serve (or a dry run) is still running
            raise StagingRefused(f"port already in use ({', '.join(busy)}): another serve is running? stop it first")
        no_address_reuse()
        try:                                            # a later bind failure must close what is already bound
            self.webhook = webhook.make_server(hub, verify_token=sec.verify_token, app_secret=sec.app_secret,
                                               host=cfg.bind_host, port=cfg.webhook_port,
                                               tick_interval=cfg.tick_interval_s,
                                               phone_number_id=cfg.phone_number_id,
                                               accept_events=getattr(cfg, "accepting_events", True),
                                               foreign_policy=getattr(cfg, "foreign_number_policy", "reject"))
            self.admin = make_threaded_server(cfg.bind_host, cfg.admin_port, app)
            self.launcher = make_threaded_server(cfg.bind_host, cfg.launcher_port, launcher) if launcher else None
        except BaseException:
            self._close_sockets()
            raise

    def start(self) -> "Runtime":
        for name, srv in (("webhook", self.webhook), ("admin", self.admin), ("launcher", self.launcher)):
            if srv is not None:
                threading.Thread(target=srv.serve_forever, name=name, daemon=True).start()
                self._running.append(srv)
        self.hub.start_teamhub()
        return self

    def ports(self) -> dict:
        return {n: s.server_address[1] for n, s in (("webhook", self.webhook), ("admin", self.admin),
                                                    ("launcher", self.launcher)) if s is not None}

    def _close_sockets(self) -> None:
        for srv in (self.webhook, self.admin, self.launcher):
            if srv is not None:
                if hasattr(srv, "_stop"):                # HubServer: stop its tick thread and workers too
                    srv._stop.set()
                    srv._pool.shutdown(wait=False)
                srv.server_close()

    def close(self) -> None:
        self.hub.stop_teamhub()
        for srv in self._running:
            srv.shutdown()
        self._close_sockets()
        self.hub.store.close()


def banner(cfg: StagingConfig, rt_ports: dict, *, dry_run: bool, teamhub: bool) -> str:
    tail = ", ".join(_last4(n) for n in cfg.allowed_senders) or "(none: nothing is processed or sent)"
    mode = "DRY RUN (fake WhatsApp + fake Odoo)" if dry_run else "STAGING (REAL Cloud API + live Acme Odoo, TEST tickets)"
    return "\n".join([
        f"mode: {mode}",
        f"webhook  http://{cfg.bind_host}:{rt_ports['webhook']}  -> https://{cfg.public_host}/webhook",
        f"admin    http://{cfg.bind_host}:{rt_ports['admin']}  -> " + (f"https://{cfg.public_host}/ (behind Cloudflare Access)"
                                                                       if cfg.public_admin else "LOCAL ONLY (not on the tunnel)"),
        f"launcher " + (f"http://{cfg.bind_host}:{rt_ports['launcher']}  -> https://{cfg.launcher_host}/" if "launcher" in rt_ports else "off"),
        f"allowed senders: {tail}",
        f"WhatsApp send: {'fake (dry run)' if dry_run else 'REAL Cloud API, allowlisted testers only'};  Teamhub watcher: "
        f"{'ON' if teamhub else 'OFF'};  history sync: OFF;  Meta writes from code: none",
        "fakes: STT (blocked), judge, learning, Metrics, proof, approval transport (the admin page approves)"])


def open_runtime(cfg: StagingConfig, *, dry_run: bool, log_setup: bool = False, **inject) -> tuple[Runtime, Secrets]:
    sec = load_secrets(cfg, dry_run=dry_run)
    if log_setup:                                      # redaction is ON before anything else can log
        setup_logging(sec.values())
    check_config(cfg, sec)
    users = users_for_run(cfg, dry_run=dry_run)
    teamhub = inject.pop("teamhub", None) or (None if dry_run else make_teamhub_runner(cfg))
    hub = build_hub(cfg, sec, dry_run=dry_run, teamhub=teamhub, **inject)
    try:
        app, launcher = build_admin(cfg, sec, hub, dry_run=dry_run, users_file=users)
        return Runtime(cfg, sec, hub, app, launcher), sec
    except (StagingRefused, OSError) as e:
        hub.store.close()
        raise StagingRefused(str(e) if isinstance(e, StagingRefused) else f"could not bind ({e.__class__.__name__})") from None


def cmd_serve(a) -> int:
    cfg = load_config(a.config)
    rt, sec = open_runtime(cfg, dry_run=a.dry_run, log_setup=True)
    rt.start()
    print(banner(cfg, rt.ports(), dry_run=a.dry_run, teamhub=rt.hub._teamhub is not None), flush=True)
    pw = Path(os.path.expanduser(cfg.users_file)).with_suffix(".password.txt")
    if pw.exists():
        print(f"NOTE: {pw} still holds the admin password in plain text: read it, then delete it.", flush=True)
    print("serving. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
    return 0


def cmd_check(a) -> int:
    cfg = load_config(a.config)
    sec = load_secrets(cfg, dry_run=a.dry_run)
    check_config(cfg, sec)
    print("config OK (no network, nothing started). allowed senders: " + (", ".join(_last4(n) for n in cfg.allowed_senders) or "none"))
    return 0


def cmd_init_users(a) -> int:
    cfg = load_config(a.config)
    if cfg.staff_approver_id <= 0:
        raise StagingRefused("staff_approver_id is required")
    print(deliver(a.username, write_staff_users(cfg.users_file, a.username, cfg.staff_approver_id), cfg.users_file))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twin_agent.hub.staging")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "check", "init-users"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="staging.yaml")
        if name != "init-users":
            p.add_argument("--dry-run", action="store_true", help="fake WhatsApp + fake Odoo, same wiring (rehearsal)")
        else:
            p.add_argument("--username", default="staging-approver")
    a = ap.parse_args(argv)
    try:
        return {"serve": cmd_serve, "check": cmd_check, "init-users": cmd_init_users}[a.cmd](a)
    except StagingRefused as e:
        print(f"REFUSED: {redact_line(str(e))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # run the canonical module, not this __main__ copy: the admin page imports ``twin_agent.hub.staging`` by name and
    # must see the SAME classes (otherwise is_staging_hub's isinstance check fails).
    from twin_agent.hub.staging import main as _canonical_main
    sys.exit(_canonical_main())
