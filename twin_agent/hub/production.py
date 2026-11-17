"""PRODUCTION hub runner (stopgap host): ``python -m twin_agent.hub.production serve|preflight|init-users --config production.yaml``.

Same machinery as the staging runner (``staging.Runtime``, the webhook, the admin page, the Teamhub timer, the redacting
logs), with its OWN config, state file, ports, token store and secrets, so the two can never share state. It serves ONE
number (Executive's, by ``phone_number_id``, a config value) and REFUSES the TEST number.

Rails (code, tested): events for any other number are answered 200 and dropped (webhook); real partners only (an
unknown sender is held, never ticketed); Odoo only through ``GuardedOdoo`` (no deletes, mail suppressed on create, one
configured team, no ``TEST`` prefix); startup refuses if the stage a ticket opens in has an email template or the Odoo
login is not the configured automation uid; the instant ack is the ONLY automatic client message; sends are OFF until
``send_enabled: true`` AND a ``go_live_ref``; a STOP file stops every send; Teamhub is OFF unless ``teamhub_config`` is set
and then it posts SAFE SUMMARIES only (no client text, no approval card); loopback bind only.
FAKE here (as on staging): STT (blocked), judge, learning, Metrics, proof, the approval transport (the admin page approves).

Replies: only the admin page sends a reply to a client (a person approves it); Teamhub shows the safe summary and a link.
Inside the 24h window a WhatsApp reply is the intent; email follow-ups stay the PR #66 policy (a follow-up, not here).
Intake before sends: with ``intake_ref`` set the hub stores, creates the ticket and answers 200 while the ack is off; held
messages post a safe line to Teamhub (``prod_support``); the Cloudflare Access gate guards the public admin host.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from ..channels import whatsapp_cloud
from ..helpdesk import guard
from . import fakes, judge, learning, proof_runner
from .admin.config import DEFAULT_BUSINESS_DIR
from .model import Owner
from .odoo_gateway import GuardedOdoo
from .odoo_people import ActorRefused, DirSecretsBackend, PeopleConfig, PersonOdoo, open_person_transport
from .pipeline import ACK_DONE, Hub
from .prod_support import (AccessJwtGate, AccessVerifier, HoldPoster, ProdApprovalTransport, OwnerAssigner, ProdKnowledge, ProdSTT, StaffNumbers,
                           ProdTeamBus, ProdWhatsApp, RecordingQueue, load_access_file)
from .readonly_api import ApiGate, write_token
from .staging import (SECRETS_DIR, TEST_DISPLAY, TEST_PHONE_NUMBER_ID, Runtime, Secrets, StagingConfig, StagingRefused, _digits,
                      _inside, _int, _last4, _read_json, _same_dir, ensure_verify_token, teamhub_pass, redact_line,
                      setup_logging)
from .staging_support import TeamhubTimer, fake_transport, port_in_use
from .staging_users import deliver, write_staff_users
from .store import SqliteStore
from .autopilot import AutopilotMixin
from .test_clients import ClientTestWindows, international, parse_number_windows, parse_windows, window_problems

log = logging.getLogger("twin_agent.hub.production")

MARKER = ("admin_meta", "production")
from .. import profile as _profile
ACCESS_EMAILS = tuple(_profile.get("access_emails"))   # exact addresses, from the profile
VOICE_PLACEHOLDER = "Voice note, transcription pending"
HOME_DIR = Path.home() / ".claude" / "aa-prod"
STAGING_PORTS = (8790, 8791, 8792)
_STG = StagingConfig()                                   # the staging defaults this config must stay apart from


class ProductionRefused(StagingRefused):
    """A rail said no. The text is a plain reason and never holds a secret."""


# ---------------------------------------------------------------- config + validation

@dataclass(frozen=True)
class ProductionConfig:
    phone_number_id: str = ""                         # Executive's number id (a config value; never the TEST id)
    display_phone_number: str = ""                    # optional, informational only
    team_id: int = 2                                  # Helpdesk Support
    automation_uid: int = 82                          # odoo-riley: the login must be this uid
    default_owner_uid: int = 82                       # Riley: gets a ticket when no better owner is found
    default_owner_name: str = "Riley"
    backup_owner_uid: int = 0                         # Supervisor's Odoo uid (a config value); 0 = no backup
    backup_owner_name: str = "Supervisor"
    unavailable_owner_uids: tuple = ()                # owners to skip right now (the operator's list)
    bind_host: str = "127.0.0.1"
    webhook_port: int = 8890
    admin_port: int = 8891
    public_host: str = "app.example.com"              # only ^/webhook$ and ^/healthz$ go through the tunnel
    state_db: str = str(HOME_DIR / "hub-production.db")
    app_secret_file: str = str(SECRETS_DIR / "meta-acme-app.json")
    system_user_file: str = str(SECRETS_DIR / "meta-acme-system-user.json")
    verify_token_file: str = str(SECRETS_DIR / "aa-prod-verify-token.txt")
    odoo_secret_file: str = str(SECRETS_DIR / "odoo-riley.json")
    users_file: str = str(SECRETS_DIR / "aa-prod-admin-users.json")
    business_dir: str = str(SECRETS_DIR / "aa-prod-meta-businesses")     # the Meta token store: never the default/staging one
    staff_approver_id: int = 0
    client_channel: str = "whatsapp"
    intake_ref: str = ""                              # why storing real client messages is allowed; blank = NOT accepting (503)
    send_enabled: bool = False                        # the instant ack stays OFF until this AND go_live_ref are set
    go_live_ref: str = ""                             # why going live (the ack) is allowed (preflight checks it is not blank)
    expected_phone_number_id: str = ""                # optional typo guard: when set it must equal phone_number_id
    teamhub_config: str = ""                            # "" = Teamhub OFF; else an existing config.yaml (safe summaries only)
    teamhub_interval_s: int = 120
    teamhub_timeout_s: int = 120
    tick_interval_s: int = 60

    public_admin: bool = False                        # serve the admin page on admin_public_host behind Cloudflare Access
    admin_public_host: str = "admin.example.com"
    access_file: str = str(SECRETS_DIR / "aa-prod-access.json")      # {"team_domain", "aud"}, used when the keys below are blank
    access_team_domain: str = ""                      # e.g. acme.cloudflareaccess.com
    access_aud: str = ""                              # the Access application's AUD tag
    access_users: dict = field(default_factory=dict)  # allowed Access email -> its ONE hub username (public_admin needs all)
    access_emails: tuple = ACCESS_EMAILS              # EXACT addresses (never a domain); each needs a matching hub user

    staff_phones_file: str = str(SECRETS_DIR / "twin-team-phones.json")   # {"staff": {name: "digits"}}: the team's own phones
    odoo_people: dict = field(default_factory=dict)       # Odoo uid -> secret name (file <name>.json next to odoo_secret_file)
    odoo_tg_people: dict = field(default_factory=dict)    # Telegram id of an approver -> Odoo uid (for reassign)
    intake_v2: str = "off"                            # "off" (default: the old flow) | "test_only" | "on": company question, type buttons
    test_clients: tuple = ()                          # [{staff_key, until (UTC ISO), ref}]: a staff phone is a TEST client until then
    test_numbers: tuple = ()                          # [{phone (a KEY in the phones file's test_numbers map), until, ref}]: a non-staff test number
    test_client_close_stage: int = 0                  # the Odoo stage (team 2, e.g. 4 Cancelled) a test ticket moves to after One's reply; 0 = off

    foreign_number_policy: ClassVar[str] = "drop"     # another number's event: 200 + ignored (staging answers 503; see webhook.make_server)
    launcher_port: ClassVar[int] = 0                 # no Embedded Signup launcher in production

    @property
    def accepting_events(self) -> bool:               # read by staging.Runtime: intake needs intake_ref (NOT send_enabled)
        return bool(self.intake_ref.strip())


_FIELD_NAMES = {f.name for f in fields(ProductionConfig)}


def _mentions_test_number(node) -> bool:
    if isinstance(node, dict):
        return any(_mentions_test_number(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_mentions_test_number(v) for v in node)
    if isinstance(node, (str, int)):
        d = _digits(node)
        return TEST_PHONE_NUMBER_ID in d or TEST_DISPLAY in d
    return False


def config_from_dict(raw: dict) -> ProductionConfig:
    raw = dict(raw or {})
    raw.pop("teamhub_tickets", None)                    # an existing config.yaml may be reused as-is
    if _mentions_test_number(raw):
        raise ProductionRefused("the config mentions the TEST number: production never serves it")
    unknown = sorted(set(raw) - _FIELD_NAMES)
    if unknown:
        raise ProductionRefused(f"unknown production config key(s): {', '.join(unknown)}")
    kw = {}
    for k, v in raw.items():
        if k in ("odoo_people", "odoo_tg_people"):
            kw[k] = _int_map(k, v)
            continue
        if k == "test_clients":
            try:
                kw[k] = parse_windows(v)
            except ValueError as e:
                raise ProductionRefused(str(e)) from None
            continue
        if k == "intake_v2":
            mode = {False: "off", True: "on", None: "off"}.get(v, str(v).strip().lower())
            if mode not in ("off", "on", "test_only"):
                raise ProductionRefused("intake_v2 must be false, true or test_only")
            kw[k] = mode
            continue
        if k == "test_numbers":
            try:
                kw[k] = parse_number_windows(v)
            except ValueError as e:
                raise ProductionRefused(str(e)) from None
            continue
        if k == "access_users":
            kw[k] = {str(a).strip().lower(): str(b).strip().lower() for a, b in v.items()} if isinstance(v, dict) else {}
            continue
        default = getattr(ProductionConfig, k)
        if k == "unavailable_owner_uids":
            kw[k] = tuple(_int(x) for x in (v or []) if _int(x) > 0) if isinstance(v, (list, tuple)) else ()
        elif isinstance(default, tuple):
            kw[k] = tuple(str(x).strip().lower() for x in (v or []) if str(x).strip()) if isinstance(v, (list, tuple)) else ()
        elif isinstance(default, bool):
            kw[k] = v is True
        elif isinstance(default, int):
            kw[k] = _int(v)
        else:
            kw[k] = str(v if v is not None else "")
    return ProductionConfig(**kw)


def _int_map(name: str, v) -> dict:
    """``odoo_people`` {uid: secret name} / ``odoo_tg_people`` {tg id: uid}. Anything else is refused."""
    try:
        if not isinstance(v, dict):
            raise ValueError
        return {int(a): (str(b) if name == "odoo_people" else int(b)) for a, b in v.items()}
    except (TypeError, ValueError):
        raise ProductionRefused(f"{name} must be a map of whole numbers" + (" to secret names" if name == "odoo_people" else "")) from None


def load_config(path: str) -> ProductionConfig:
    import yaml
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return config_from_dict(yaml.safe_load(f) or {})
    except OSError:
        raise ProductionRefused(f"cannot read the production config {path}") from None


def problems(cfg: ProductionConfig, *, have_app_secret: bool, have_verify_token: bool, now=None) -> list[str]:
    """Every reason this config may not run, in plain words. Empty list = valid. Pure: no files, no network."""
    out: list[str] = []
    pid = _digits(cfg.phone_number_id)
    if cfg.phone_number_id != pid or not (8 <= len(pid) <= 20):
        out.append("phone_number_id is required (the production number's Meta id, digits only)")
    if pid == TEST_PHONE_NUMBER_ID or _digits(cfg.display_phone_number) == TEST_DISPLAY:
        out.append("phone_number_id must not be the TEST number (production never serves it)")
    if cfg.default_owner_uid <= 0 or (cfg.backup_owner_uid and cfg.backup_owner_uid == cfg.default_owner_uid):
        out.append("default_owner_uid is required and the backup owner must be a different person")
    if cfg.team_id <= 0 or cfg.automation_uid <= 0:
        out.append("team_id and automation_uid must be set (Helpdesk Support team, the automation account's Odoo uid)")
    if cfg.bind_host not in ("127.0.0.1", "localhost", "::1"):
        out.append("bind_host must be loopback (127.0.0.1): cloudflared connects to localhost")
    if not have_app_secret:
        out.append("the Meta app secret is missing (app_secret_file)")
    if not have_verify_token:
        out.append("the webhook verify token is missing")
    if cfg.send_enabled and not cfg.go_live_ref.strip():
        out.append("go_live_ref must say why going live is allowed (blank is refused when send_enabled is true)")
    if cfg.send_enabled and not cfg.accepting_events:
        out.append("intake_ref is required before send_enabled (the ack needs intake on first)")
    if cfg.expected_phone_number_id and cfg.expected_phone_number_id != cfg.phone_number_id:
        out.append("phone_number_id does not match expected_phone_number_id (a typo, or the wrong number)")
    out += _access_problems(cfg) + window_problems(cfg.test_clients + cfg.test_numbers, now) + _close_stage_problems(cfg)
    if cfg.client_channel != "whatsapp":
        out.append("client_channel must be whatsapp (an approved reply is sent on WhatsApp, not by Odoo email)")
    if cfg.staff_approver_id <= 0:
        out.append("staff_approver_id is required (the admin user's approver id)")
    return out + _apart_from_staging(cfg) + _port_problems(cfg)


def _access_problems(cfg: ProductionConfig) -> list[str]:
    if not cfg.public_admin:
        return []
    out = []
    host = cfg.admin_public_host.strip().lower()
    if not host or host in (cfg.public_host.lower(), _STG.public_host, _STG.launcher_host):
        out.append("admin_public_host is required and must be its own host (not the webhook or a staging host)")
    users = cfg.access_users
    if set(users) != set(cfg.access_emails) or not all(users.values()) or len(set(users.values())) != len(users):
        out.append("access_users must map EVERY allowed email to its own hub username (one email, one user, no sharing)")
    if not cfg.access_emails or any("@" not in e or e.startswith("@") or e.endswith("@") for e in cfg.access_emails):
        out.append("access_emails must be exact addresses (a domain is never accepted)")
    return out


def _port_problems(cfg: ProductionConfig) -> list[str]:
    ports = (cfg.webhook_port, cfg.admin_port)
    nonzero = [p for p in ports if p]                  # 0 = "any free port" (tests only)
    out = []
    if len(set(nonzero)) != len(nonzero) or any(not (0 <= p <= 65535) for p in ports):
        out.append("webhook_port and admin_port must be two different valid ports")
    if any(p in STAGING_PORTS for p in ports):
        out.append("webhook_port / admin_port may not use the staging ports (8790-8792)")
    if not cfg.public_host.strip() or cfg.public_host == _STG.public_host:
        out.append("public_host is required and must not be the staging host")
    return out


def _apart_from_staging(cfg: ProductionConfig) -> list[str]:
    """Production keeps its own state, token store, verify token and admin users: never staging's or the default admin's."""
    out = []
    stg_dir = str(Path(os.path.expanduser(_STG.state_db)).parent)
    if _inside(cfg.state_db, stg_dir) or _inside(_STG.state_db, str(Path(os.path.expanduser(cfg.state_db)).parent)):
        out.append("state_db must not be in the staging state folder")
    if _same_dir(cfg.verify_token_file, _STG.verify_token_file) or _same_dir(cfg.access_file, _STG.users_file):
        out.append("verify_token_file must not be the staging verify token")
    if _same_dir(cfg.users_file, _STG.users_file):
        out.append("users_file must not be the staging admin users file")
    if any(_inside(cfg.business_dir, d) for d in (DEFAULT_BUSINESS_DIR, _STG.business_dir)):
        out.append("business_dir (the Meta token store) must not be the default admin or staging store")
    return out


# ---------------------------------------------------------------- secrets (files only; never printed)

def load_secrets(cfg: ProductionConfig, *, dry_run: bool) -> Secrets:
    import secrets as _secrets
    if dry_run:                                        # ephemeral, in memory: a dry run never reads or writes the vault
        return Secrets("dry-run-app", _secrets.token_hex(16), _secrets.token_hex(16), "", "")
    app = _read_json(cfg.app_secret_file)
    s = Secrets(str(app.get("app_id") or ""), str(app.get("app_secret") or ""), ensure_verify_token(cfg.verify_token_file),
                str(_read_json(cfg.system_user_file).get("system_user_token") or ""), "",
                str(_read_json(cfg.odoo_secret_file).get("api_key") or ""))
    if cfg.send_enabled and not s.wa_token:
        raise ProductionRefused("send_enabled is on but the system-user token is missing (system_user_file)")
    return s


def check_config(cfg: ProductionConfig, sec: Secrets) -> None:
    why = problems(cfg, have_app_secret=bool(sec.app_secret), have_verify_token=bool(sec.verify_token))
    if why:
        raise ProductionRefused("; ".join(why))


# ---------------------------------------------------------------- adapters

def build_cloud(cfg: ProductionConfig, sec: Secrets) -> whatsapp_cloud.WhatsAppCloud | None:
    """The REAL Cloud API sender for ``phone_number_id``; ``None`` while ``send_enabled`` is false."""
    if not cfg.send_enabled:
        return None
    from .admin.config import GRAPH_VERSION
    wa = {"enabled": True, "vault_name": "production", "graph_version": GRAPH_VERSION, "window_hours": 24,
          "allowed_recipients": []}
    ref = cfg.go_live_ref.strip()
    cloud_cfg = {"run_mode": "live", "send_enabled": True, "whatsapp": wa,
                 "consent": {"d1_ref": ref, "channels": {"whatsapp": bool(ref)}}}
    try:
        return whatsapp_cloud.WhatsAppCloud(cloud_cfg, has_secret=lambda n: True, get_fields=lambda n: {
            "token": sec.wa_token, "phone_number_id": cfg.phone_number_id})
    except whatsapp_cloud.WhatsAppRefused as e:
        raise ProductionRefused(f"the WhatsApp Cloud sender is blocked: {e}") from None


class ProdOdoo(PersonOdoo):
    """Odoo for production: the existing per-person gateway (``PersonOdoo``: each action runs AS the right person's own
    key, identity-checked and audited; intake runs as the automation account) pinned to ONE team, over the plain guarded
    gateway (guard purpose ``helpdesk``: no deletes, mail suppressed on create). Nothing is written until ``preflight``."""

    def __init__(self, *, config: PeopleConfig, open_transport, conv, store, team_id: int, clock=None, close_stage: int = 0):
        super().__init__(config=config, open_transport=open_transport, conv=conv, store=store, writes_on=True, clock=clock)
        self.team_id, self._ready = int(team_id), False
        self.close_stage, self.close_stage_ids = int(close_stage), frozenset()   # the test-client close stage (0 = off)

    def preflight(self, stage_id: int) -> None:
        """Reads only. Fails closed with a plain reason before the first message can arrive."""
        try:
            gw, _ = self._gateway(None)          # logs in as the automation account; its uid must be automation_uid
            rows = gw.search_read("helpdesk.stage", [["team_ids", "in", [self.team_id]]], ["name", "template_id"])
        except ActorRefused as e:
            raise ProductionRefused(f"Odoo preflight: {e}") from None
        except Exception as e:                   # noqa: BLE001 - class name only, never the key
            raise ProductionRefused(f"Odoo preflight: cannot read the team's stages ({type(e).__name__})") from None
        row = next((r for r in rows if int(r["id"]) == int(stage_id)), None)
        if row is None or int(stage_id) not in self.conv.safe_stages:
            raise ProductionRefused(f"Odoo preflight: the opening stage is not a safe stage of team {self.team_id}")
        if row.get("template_id"):
            raise ProductionRefused(f"Odoo preflight: team {self.team_id}'s opening stage has an email template; production "
                                    "needs a silent open stage (a ticket there could email the client)")
        self.close_stage_ids = frozenset(int(r["id"]) for r in rows)
        if self.close_stage and self.close_stage not in self.close_stage_ids:
            raise ProductionRefused(f"Odoo preflight: test_client_close_stage is not a stage of team {self.team_id}")
        self._ready = True

    def close_test_client_ticket(self, tid: int) -> None:
        """Close a TEST client's ticket to the ONE configured stage (guard purpose ``test_client_close``: it re-reads the
        live ticket and partner and requires the stage to be a stage of the team). As automation, audited."""
        if not self._ready or self.close_stage <= 0:
            raise guard.WriteRefused("production: the test-client close stage is not configured")
        self._act(None, "write", int(tid), lambda gw: gw.close_test_client_ticket(int(tid), self.close_stage, self.close_stage_ids),
                  model="helpdesk.ticket")

    def create_ticket(self, vals: dict, *, context: dict) -> int:
        """The NO-TEST rail: a real partner, never the TEST contact, never a ``TEST`` name; the team is pinned."""
        if not self._ready:
            raise guard.WriteRefused("production: preflight has not run")
        v = dict(vals)
        if type(v.get("partner_id")) is not int or v["partner_id"] <= 0:
            raise guard.WriteRefused("production: a ticket needs a real partner")
        if str(v.get("name") or "").startswith(guard.TEST_PREFIX):
            raise guard.WriteRefused("production: a TEST ticket may not be created here")
        who = self.search_read("res.partner", [["id", "=", v["partner_id"]]], ["name"], limit=1)
        if not who or who[0].get("name") == guard.TEST_PARTNER:
            raise guard.WriteRefused("production: the TEST contact (or an unknown partner) may not get a ticket here")
        v["team_id"] = self.team_id
        return super().create_ticket(v, context=context)

    def send_client_email(self, *a, **kw):
        raise guard.WriteRefused("production: client email is disabled (WhatsApp only)")


def people_config(cfg: ProductionConfig) -> PeopleConfig:
    """The automation account (the name of ``odoo_secret_file``, e.g. odoo-riley) acts at intake and for its own tickets;
    the configured people act for their own. A missing key BLOCKS (never a silent fallback to automation)."""
    name = Path(os.path.expanduser(cfg.odoo_secret_file)).stem
    people = {int(cfg.automation_uid): name, **{int(k): str(v) for k, v in cfg.odoo_people.items()}}
    return PeopleConfig(people=people, automation_secret=name, automation_uid=int(cfg.automation_uid),
                        tg_people={int(k): int(v) for k, v in cfg.odoo_tg_people.items()}, fallback_to_automation=False)


def real_open_transport(cfg: ProductionConfig):
    """secret name -> a real JSON-RPC transport. Keys live in files ``<name>.json`` next to ``odoo_secret_file``."""
    f = _read_json(cfg.odoo_secret_file)
    if not (f.get("url") and f.get("db")):
        raise ProductionRefused("the Odoo secret file is missing or has no url/db (odoo_secret_file)")
    backend = DirSecretsBackend(Path(os.path.expanduser(cfg.odoo_secret_file)).parent)
    odoo_sys = {"odoo_sys": {"url": f["url"], "db": f["db"]}}

    def open_(name: str):
        return open_person_transport(odoo_sys, name, backend=backend)
    return open_


HOLD_LABELS = {"staff_number": "message from a team member's own phone (no ticket, no ack)", "unknown_sender": "message from a number that is not a known client",
               "stt_unavailable": "voice note, transcription pending", "voice_note_pending": "voice note, transcription pending",
               "empty_message": "message with no text", "processing_failed": "processing failed, a retry is scheduled",
               "processing_dead": "processing failed for good, needs a person"}


class ProductionHub(AutopilotMixin, TeamhubTimer, Hub):
    """The Hub plus the production marker and rails. ``PRODUCTION`` is the class-level flag the admin page checks (with the
    store marker and a fresh ``problems()`` pass); a plain ``Hub`` can never carry it.

    * INTAKE BEFORE SENDS: with ``send_enabled`` false the hub still stores, creates the ticket and answers 200; only the ack
      is skipped, ONCE per message, as a settled outcome ("7-ACK(skipped)": no retry, no hold). A message that arrived
      before the ack was switched on never gets a late ack (``_ack_allowed``).
    * HOLDS ARE NEVER INVISIBLE: every hold posts a SAFE line (fixed label, last 4 digits, ticket id, link; no client text).
    * A voice note makes a ticket whose description says "Voice note, transcription pending" (STT is off in production)."""

    PRODUCTION = True

    def __init__(self, *, prod: ProductionConfig, have_secrets: tuple[bool, bool] = (True, True), teamhub=None,
                 hold_poster=None, **kw):
        super().__init__(**kw)
        self.staff = getattr(self.k_odoo, "staff", None) or StaffNumbers("")
        self.windows = getattr(self.k_odoo, "windows", None)
        self.prod, self.have_secrets, self.hold_poster = prod, have_secrets, hold_poster
        self._tl = threading.local()
        self.init_teamhub(teamhub, prod.teamhub_interval_s, prod.teamhub_timeout_s)
        self.store.put(*MARKER, {"production": True})
        self._acks_since = self._settle_ack_gate()
        if self.windows is not None:
            self.windows.sweep()

    def tick(self, now=None) -> dict:
        if self.windows is not None:
            self.windows.sweep()                       # the "window closed" audit row, once, after ``until``
        out = super().tick(now)
        if self.windows is not None:
            out["autopilot_approved"] = self._tick_pass("autopilot", self.autopilot_sweep, 0)
        return out

    # ---- the ack gate
    def _settle_ack_gate(self) -> str:
        """When acks were last switched on. Messages that arrived before this never get an ack."""
        prev = self.store.get("admin_meta", "ack_gate") or {}
        now = self.clock().isoformat()
        if not self.prod.send_enabled:
            self.store.put("admin_meta", "ack_gate", {"on": False, "since": prev.get("since", "")})
            return ""
        since = prev["since"] if prev.get("on") and prev.get("since") else now
        self.store.put("admin_meta", "ack_gate", {"on": True, "since": since})
        return since

    def _ack_allowed(self, ts) -> bool:
        if not self.prod.send_enabled or not self._acks_since:
            return False
        try:
            return bool(ts.tzinfo) and ts >= datetime.fromisoformat(self._acks_since)
        except (AttributeError, ValueError):
            return False

    def _ack(self, state, log_: list) -> None:
        if self._ack_allowed(state.inbound.ts):
            return super()._ack(state, log_)
        h = state.history
        if any(x in h for x in ACK_DONE):
            return
        if self._claim_auto(state.wa_id, state.source_message_id, "normal", state.tid):
            self._claim_set(state.wa_id, state.source_message_id, state="skipped")
        h.append("7-ACK(skipped)")
        self._save(state)
        self._note(state.tid, "[Hub] Ack not sent: sends were off when this message arrived.")
        log_.append({"stage": "7-ACK", "status": "skipped", "why": "sends off"})

    def _send_fallback(self, inbound, now) -> int:
        if self._ack_allowed(inbound.ts):
            return super()._send_fallback(inbound, now)
        if self._claim_auto(inbound.wa_id, inbound.message_id, "fallback", 0):
            self._claim_set(inbound.wa_id, inbound.message_id, state="skipped")
        return 0

    def _on_assigned(self, state, choice: dict) -> None:
        """Audit a fallback owner like a reassign (old owner none, new owner, why). Ids and reasons only."""
        if not choice.get("fallback"):
            return
        from .store import audit_append
        audit_append(self.store, {"role": "assignment", "model": "helpdesk.ticket", "method": "owner_fallback",
                                  "ticket_id": int(state.tid), "reason": str(choice["fallback"]), "old_owner_uid": 0,
                                  "new_owner_uid": int(choice["uid"])}, self.clock())

    # ---- holds are visible
    def mark_hold_done(self, hold_id: str, *, hub_user: str, access_email: str = "") -> bool:
        """Close an open hold and audit who did it (the hub user and the verified Access email). Ids only."""
        from .store import audit_append
        h = self.store.get("holds", hold_id)
        if h is None or h.get("status") != "open":
            return False
        self.store.update("holds", hold_id, lambda d: {**d, "status": "closed"})
        audit_append(self.store, {"role": "approver", "model": "hold", "method": "done", "hold_id": hold_id,
                                  "reason": str(h.get("reason") or ""), "ticket_id": int(h.get("ticket_id") or 0),
                                  "hub_user": hub_user, "access_email": access_email}, self.clock())
        return True

    def open_holds_count(self) -> int:
        return len(self.holds())

    def _route(self, inbound) -> dict:
        """A team member's own phone is never a client: held as "staff number", no ticket, no ack, no digits anywhere."""
        w = self.windows.active(inbound.wa_id) if self.windows is not None else None
        if w is not None:
            self.windows.audit_message(w)              # a test-client message: one audit row, then the normal client flow
        if self.staff.contains(inbound.wa_id):
            return self._hold("staff_number", inbound=inbound, detail="a team phone; no ticket")
        return super()._route(inbound)

    def _hold(self, reason: str, *, inbound=None, **kw) -> dict:
        self._tl.mute = reason == "staff_number" and not self._first_staff_alert_today(inbound)
        try:
            return super()._hold(reason, inbound=inbound, **kw)
        finally:
            self._tl.mute = False

    def _first_staff_alert_today(self, inbound) -> bool:
        """A staff phone posts to Teamhub at most once per number per day (the hold itself is always recorded)."""
        key = hashlib.sha256(_digits(inbound.wa_id).encode()).hexdigest()[:16] + self.clock().strftime("-%Y%m%d")
        return self.store.put_if_absent("staff_alert", key, {"at": self.clock().isoformat()})

    def _triage(self, text: str, *, safe: str | None = None) -> None:
        if getattr(self._tl, "mute", False):
            return
        super()._triage(text, safe=self._teamhub_line(safe))

    def _teamhub_line(self, safe: str | None) -> str | None:
        """HOLD lines get a plain label, the last 4 digits and a link; everything else is already a fixed safe line."""
        m = re.match(r"HOLD \[(\w+)\] ticket=(\S+) hold_id=(\w+)", safe or "")
        if not m:
            return safe
        reason, tid, hid = m.groups()
        line = f"HOLD: {HOLD_LABELS.get(reason, 'a message needs a person')} [{reason}]"          # no digits of the number
        line += f", ticket #{tid}" if tid != "-" else ""
        link = f"https://{self.prod.admin_public_host}/" + (f"tickets/{tid}" if tid != "-" else "holds") if self.prod.public_admin else ""
        return f"{line}, hold {hid}" + (f". Open: {link}" if link else ". See the hub admin Holds page.")

    # ---- voice notes
    def _transcribe(self, inbound, log_: list) -> bool:
        if super()._transcribe(inbound, log_) or inbound.kind != "voice":
            return True
        inbound.kind, inbound.text = "text", VOICE_PLACEHOLDER            # a ticket a person can pick up; the audio id stays
        inbound.lang = inbound.lang or "en"
        return True

    def _first_contact(self, inbound, **kw) -> dict:
        res = super()._first_contact(inbound, **kw)
        if VOICE_PLACEHOLDER in (inbound.text or "") and inbound.audio_id and res.get("ticket_id"):
            self._hold("voice_note_pending", inbound=inbound, ticket_id=int(res["ticket_id"]),
                       detail="voice note: a person must listen")
        return res


def is_production_hub(hub) -> bool:
    """True only for a ``ProductionHub`` with the marker doc, the hub's number equal to the config's, the production
    sender and Odoo types, and a config that passes ``problems()`` again right now."""
    cfg = getattr(hub, "prod", None)
    if not getattr(type(hub), "PRODUCTION", False) or not isinstance(cfg, ProductionConfig):
        return False
    if hub.store.get(*MARKER) is None or str(hub.cfg.get("phone_number_id")) != cfg.phone_number_id:
        return False
    if not (isinstance(hub.odoo, ProdOdoo) and isinstance(hub.whatsapp, ProdWhatsApp)):
        return False
    app_ok, tok_ok = getattr(hub, "have_secrets", (False, False))
    return not problems(cfg, have_app_secret=app_ok, have_verify_token=tok_ok)


# ---------------------------------------------------------------- assembly

def teamhub_settings(cfg: ProductionConfig):
    """The Teamhub settings for production, validated, with production's own overrides; None when ``teamhub_config`` is blank.
    Refused: another team, a ``TEST`` name filter, and a state folder outside production's own (it would share the lock,
    the watermark and the thread store with staging). Real tickets get safe summaries only: ``test_partner_id`` is 0."""
    if not cfg.teamhub_config:
        return None
    from . import teamhub_threads as kt
    try:
        st = kt.load_settings(cfg.teamhub_config)
    except (kt.ConfigError, OSError) as e:
        raise ProductionRefused(f"teamhub_config: {e}") from None
    flt = st.filter or {}
    if int(flt.get("team_id") or 0) != cfg.team_id:
        raise ProductionRefused(f"teamhub_config: the filter must be team {cfg.team_id}")
    if str(flt.get("name_like") or "").startswith(guard.TEST_PREFIX.rstrip()):
        raise ProductionRefused("teamhub_config: a TEST name filter is staging's: production watches real tickets")
    own = str(Path(os.path.expanduser(cfg.state_db)).parent)
    if not _inside(st.state_dir, own) or _inside(st.state_dir, str(Path(os.path.expanduser(_STG.state_db)).parent)):
        raise ProductionRefused("teamhub_config: state_dir must be inside production's own folder (not staging's)")
    link = f"https://{cfg.admin_public_host}/tickets/{{tid}}" if cfg.public_admin else ""
    return replace(st, test_partner_id=0, admin_link_template=link, seed_watermark=True)


def make_teamhub_runner(cfg: ProductionConfig):
    """The EXISTING Teamhub watcher on REAL tickets: SAFE SUMMARIES ONLY, no card, the press processor never runs."""
    st = teamhub_settings(cfg)
    return None if st is None else teamhub_pass(st)


def make_hold_poster(cfg: ProductionConfig, store) -> HoldPoster | None:
    """Held messages are posted to the Teamhub room through the watcher's own queue code (same poster, same room)."""
    st = teamhub_settings(cfg)
    if st is None:
        return None
    from .teamhub_threads import TeamhubQueue
    return HoldPoster(TeamhubQueue(st, store=store))


def dry_transport(cfg: ProductionConfig):
    """Fake Odoo for ``--dry-run`` / tests: logged in as the automation uid, the team's stages marked, no network."""
    t = fake_transport(StagingConfig(test_partner_id=1))
    t.uid = cfg.automation_uid
    for row in t.data["helpdesk.stage"].values():
        row["team_ids"] = [cfg.team_id]
    return t


def build_hub(cfg: ProductionConfig, sec: Secrets, *, dry_run: bool, transport=None, whatsapp_inner=None, teamhub=None,
              store=None, clock=None, hold_poster=None) -> ProductionHub:
    """Validate, then wire the production hub. ``transport`` / ``whatsapp_inner`` / ``store`` are injection points."""
    check_config(cfg, sec)
    t = transport or (dry_transport(cfg) if dry_run else None)
    open_t = (lambda _name: t) if t is not None else real_open_transport(cfg)
    pc = people_config(cfg)
    inner = whatsapp_inner if whatsapp_inner is not None else (fakes.FakeWhatsApp() if dry_run else build_cloud(cfg, sec))
    store = store if store is not None else _open_store(cfg, dry_run)
    try:
        odoo = ProdOdoo(config=pc, open_transport=open_t, conv=GuardedOdoo(open_t(pc.automation_secret)).conv, store=store,
                        team_id=cfg.team_id, clock=clock, close_stage=cfg.test_client_close_stage)
    except Exception as e:                                          # noqa: BLE001 - discovery reads failed
        store.close()
        raise ProductionRefused(f"Odoo is not usable ({type(e).__name__})") from None
    staff = StaffNumbers(cfg.staff_phones_file)
    windows = _open_windows(cfg, staff, store, clock)
    know = ProdKnowledge(odoo, staff, windows)
    try:
        poster = hold_poster or (HoldPoster(RecordingQueue()) if dry_run else make_hold_poster(cfg, store))
        if poster is None:
            raise ProductionRefused("teamhub_config is required: held messages must be posted to Teamhub, never lost")
    except BaseException:
        store.close()
        raise
    stops = (str(Path(os.path.expanduser(cfg.state_db)).parent / "STOP"), str(_global_stop()))
    wa = ProdWhatsApp(inner, know, send_enabled=cfg.send_enabled, takes_automatic=hasattr(inner, "automatic_count"),
                      stop_files=stops)
    hub_cfg = {"run_mode": "production", "phone_number_id": cfg.phone_number_id, "client_channel": cfg.client_channel,
               "approver_tg_ids": [cfg.staff_approver_id], "team_bus": "teamhub",
               "intake_v2": {"off": False, "on": True}.get(cfg.intake_v2, cfg.intake_v2)}
    kw = dict(odoo=odoo, whatsapp=wa, stt=ProdSTT(), telegram=ProdApprovalTransport(), team_bus=ProdTeamBus(poster),
              knowledge_odoo=know,
              knowledge_wiki=fakes.FakeWikiKnowledge(context={}), assigner=_assigner(cfg),
              proof=proof_runner.FakeProofRunner(), metrics_connector=fakes.FakeMetrics(), judge=judge.FakeJudge(),
              learning=learning.FakeLearningLog(), cfg=hub_cfg, store=store)
    if clock:
        kw["clock"] = clock
    hub = ProductionHub(prod=cfg, have_secrets=(bool(sec.app_secret), bool(sec.verify_token)), teamhub=teamhub, hold_poster=poster, **kw)
    try:
        odoo.preflight(hub.working_stage)
    except BaseException:
        store.close()
        raise
    return hub


def _open_windows(cfg: ProductionConfig, staff: StaffNumbers, store, clock) -> ClientTestWindows | None:
    """The test-client windows (None when none are configured). A window still open must name a real staff key."""
    allw = cfg.test_clients + cfg.test_numbers
    if not allw:
        return None
    wins = ClientTestWindows(allw, staff, clock=clock, store=store)
    for w in allw:
        if wins.now() < w.until and not international(wins._phone(w)):
            store.close()
            where = "test_numbers map in the phones file" if w.source == "number" else "staff phones file"
            raise ProductionRefused(f"test window [{w.staff_key}]: that key must be in the {where} "
                                    "with a full international number (country code first)")
    staff.exempt = wins.is_open
    return wins


def _close_stage_problems(cfg: ProductionConfig) -> list[str]:
    s = cfg.test_client_close_stage
    if type(s) is not int or s < 0 or s in guard.NEVER_TEST_STAGES:
        return ["test_client_close_stage must be 0 (off) or a stage id of team 2 (never 9 or 17; checked against Odoo at start-up)"]
    return []


def _assigner(cfg: ProductionConfig) -> OwnerAssigner:
    mk = lambda uid, name: Owner(uid=int(uid), name=name, tg_id=cfg.staff_approver_id, role="support")   # noqa: E731
    backup = mk(cfg.backup_owner_uid, cfg.backup_owner_name) if cfg.backup_owner_uid else None
    return OwnerAssigner(mk(cfg.default_owner_uid, cfg.default_owner_name), backup, cfg.unavailable_owner_uids)


def _open_store(cfg: ProductionConfig, dry_run: bool) -> SqliteStore:
    if dry_run:
        return SqliteStore(":memory:")
    Path(os.path.expanduser(cfg.state_db)).parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(os.path.expanduser(cfg.state_db))


def _global_stop() -> Path:
    from .. import paths
    return Path(paths.stop_file())


def access_settings(cfg: ProductionConfig) -> tuple[str, str]:
    """(team domain, AUD) from the config keys, else from ``access_file``. Never printed."""
    f = load_access_file(cfg.access_file)
    return (cfg.access_team_domain or f.get("team_domain", ""), cfg.access_aud or f.get("aud", ""))


def need_jwt(cfg: ProductionConfig) -> None:
    """``public_admin`` verifies Access tokens with PyJWT: refuse to start without it (never fall back to trusting a header)."""
    if cfg.public_admin:
        try:
            import jwt  # noqa: F401
        except ImportError:
            raise ProductionRefused("public_admin needs PyJWT (pip install \"pyjwt[crypto]\"); it is not installed") from None


def build_admin(cfg: ProductionConfig, sec: Secrets, hub: ProductionHub, *, dry_run: bool, users_file: str, verifier=None):
    """The admin app, bound to 127.0.0.1 only, no launcher, Meta writes hard-wired OFF. With ``public_admin`` it is also
    reachable as ``admin_public_host`` through the tunnel, but ONLY through the Cloudflare Access gate (the hub verifies
    the token itself); that host is added to the Host allowlist in this mode only."""
    import tempfile
    from .admin import __main__ as admin_cli
    real = not dry_run
    biz = tempfile.mkdtemp(prefix="aa-prod-dry-biz-") if dry_run else cfg.business_dir
    pub = cfg.public_admin
    need_jwt(cfg)
    if pub and verifier is None:
        team, aud = access_settings(cfg)
        try:
            verifier = AccessVerifier(team, aud, cfg.access_emails)
        except ValueError as e:
            raise ProductionRefused(f"public_admin: {e} (access_team_domain / access_aud or access_file)") from None
    ns = argparse.Namespace(users_file=users_file, host=cfg.bind_host, port=cfg.admin_port, graph="real" if real else "fake",
                            app_id=sec.app_id if real else "", config_id="", launcher_url="", launcher_port=0,
                            business_dir=biz, app_secret_file=cfg.app_secret_file,
                            allowed_host=[cfg.admin_public_host] if pub else [],
                            trusted_proxy=["127.0.0.1/32"] if pub else [], cookie_secure="auto", allow_meta_writes=False)
    try:
        app, _ = admin_cli._build(ns, hub, True)
    except ValueError as e:
        raise ProductionRefused(f"admin page: {e}") from None
    if pub:
        missing = sorted({u for u in cfg.access_users.values() if u not in app.cfg.users})
        if missing:
            raise ProductionRefused(f"public_admin: access_users names hub user(s) that do not exist: {', '.join(missing)}")
        app.access_users = dict(cfg.access_users)
    app.cfg.banner = ("PRODUCTION (real clients). Approved replies go to the client on WhatsApp. "
                      + ("Dry run: everything is fake." if dry_run else "Meta writes are OFF."))
    outer = AccessJwtGate(app, verifier, cfg.admin_public_host) if pub else app
    return ApiGate(outer, hub, app.cfg, admin_host=cfg.admin_public_host if pub else "")    # read-only /api/v1 (loopback + bearer token)


def open_runtime(cfg: ProductionConfig, *, dry_run: bool, log_setup: bool = False, **inject) -> tuple[Runtime, Secrets]:
    sec = load_secrets(cfg, dry_run=dry_run)
    if log_setup:                                      # redaction is ON before anything else can log
        setup_logging(sec.values())
    check_config(cfg, sec)
    users = _users_path(cfg, dry_run)
    teamhub = inject.pop("teamhub", None) or (None if dry_run else make_teamhub_runner(cfg))
    verifier = inject.pop("verifier", None)
    hub = build_hub(cfg, sec, dry_run=dry_run, teamhub=teamhub, **inject)
    try:
        return Runtime(cfg, sec, hub, build_admin(cfg, sec, hub, dry_run=dry_run, users_file=users, verifier=verifier), None), sec
    except (StagingRefused, OSError) as e:
        hub.store.close()
        raise ProductionRefused(str(e) if isinstance(e, StagingRefused) else f"could not bind ({e.__class__.__name__})") from None


def _users_path(cfg: ProductionConfig, dry_run: bool) -> str:
    p = Path(os.path.expanduser(cfg.users_file))
    if p.is_file():
        return str(p)
    if not dry_run:
        raise ProductionRefused("no admin users file: run  python -m twin_agent.hub.production init-users --config production.yaml")
    import tempfile
    base = Path.home() / ".claude" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = str(Path(tempfile.mkdtemp(prefix="aa-prod-dry-", dir=base)) / "users.json")
    print(deliver("prod-approver", write_staff_users(path, "prod-approver", cfg.staff_approver_id), path))
    return path


# ---------------------------------------------------------------- run

def status_line(cfg: ProductionConfig) -> str:
    if not cfg.accepting_events:
        return "NOT ACCEPTING: intake_ref is blank, signed events get 503"
    return "LIVE: intake ON, ack ON" if cfg.send_enabled else "INTAKE ON, ACK OFF: messages become tickets, no ack is sent"


def banner(cfg: ProductionConfig, ports: dict, *, dry_run: bool, teamhub: bool) -> str:
    live = status_line(cfg)
    mode = "DRY RUN (fake WhatsApp + fake Odoo)" if dry_run else f"PRODUCTION (REAL Cloud API + live Odoo, team {cfg.team_id})"
    return "\n".join([
        f"mode: {mode};  {live}",
        f"number ...{_digits(cfg.phone_number_id)[-4:]} only; every other number's events are dropped",
        f"webhook  http://{cfg.bind_host}:{ports['webhook']}  -> https://{cfg.public_host}/webhook  (+ /healthz)",
        f"admin    http://{cfg.bind_host}:{ports['admin']}  -> " + (f"https://{cfg.admin_public_host}/ (behind Cloudflare Access)"
                                                                  if cfg.public_admin else "LOCAL ONLY (not on the tunnel)"),
        f"WhatsApp: the instant ack is the only automatic message;  Teamhub: {'ON (safe summaries only)' if teamhub else 'OFF'}",
        "fakes: STT (blocked), judge, learning, Metrics, proof, approval transport (the admin page approves)"])


def cmd_serve(a) -> int:
    cfg = load_config(a.config)
    rt, _ = open_runtime(cfg, dry_run=a.dry_run, log_setup=True)
    rt.start()
    rt.admin.get_app().set_health(lambda: rt.webhook.health(public=False))
    print(banner(cfg, rt.ports(), dry_run=a.dry_run, teamhub=rt.hub._teamhub is not None), flush=True)
    print("serving. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
    return 0


def preflight(cfg: ProductionConfig, *, dry_run: bool) -> list[str]:
    """Everything ``serve`` checks, without binding a port or touching the real state file. Returns the report lines.
    Reads Odoo (login, stages) unless ``dry_run``; sends nothing; writes nothing."""
    sec = load_secrets(cfg, dry_run=dry_run)
    check_config(cfg, sec)
    need_jwt(cfg)
    busy = [n for n, p in (("webhook", cfg.webhook_port), ("admin", cfg.admin_port)) if port_in_use(cfg.bind_host, p)]
    if busy:
        raise ProductionRefused(f"port already in use ({', '.join(busy)}): another serve is running?")
    if not dry_run:
        _users_path(cfg, False)
        Path(os.path.expanduser(cfg.state_db)).parent.mkdir(parents=True, exist_ok=True)
    teamhub = None if dry_run else make_teamhub_runner(cfg)
    hub = build_hub(cfg, sec, dry_run=dry_run, teamhub=teamhub, store=SqliteStore(":memory:"))
    hub.store.close()
    live = ("LIVE-READY: intake_ref, send_enabled and go_live_ref are set" if cfg.send_enabled and cfg.accepting_events
            else status_line(cfg))
    return [f"production preflight OK ({'dry run, fakes' if dry_run else 'Odoo read only'}). number ...{_digits(cfg.phone_number_id)[-4:]}",
            f"Odoo: automation uid {cfg.automation_uid}, team {cfg.team_id}, opening stage has no email template",
            f"Owner: default uid {cfg.default_owner_uid}, " + (f"backup uid {cfg.backup_owner_uid}" if cfg.backup_owner_uid
                                                                else "NO BACKUP owner configured (backup_owner_uid)"),
            *_window_lines(cfg),
            f"Teamhub: {'ON (safe summaries and hold alerts)' if teamhub else 'OFF'}", live]


def _window_lines(cfg: ProductionConfig) -> list[str]:
    return [f"Test client window: {w.staff_key} until {w.until.isoformat()} ({w.ref})" for w in cfg.test_clients + cfg.test_numbers]


def cmd_preflight(a) -> int:
    for line in preflight(load_config(a.config), dry_run=a.dry_run):
        print(line)
    return 0


def cmd_init_users(a) -> int:
    cfg = load_config(a.config)
    if cfg.staff_approver_id <= 0:
        raise ProductionRefused("staff_approver_id is required")
    print(deliver(a.username, write_staff_users(cfg.users_file, a.username, cfg.staff_approver_id), cfg.users_file))
    return 0


def cmd_api_token(a) -> int:
    write_token()                                   # prints nothing: the value is never shown
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twin_agent.hub.production")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "preflight", "init-users"):
        p = sub.add_parser(name)
        p.add_argument("--config", default=str(HOME_DIR / "production.yaml"))
        if name == "init-users":
            p.add_argument("--username", default="prod-approver")
        else:
            p.add_argument("--dry-run", action="store_true", help="fake WhatsApp + fake Odoo, same wiring (rehearsal)")
    sub.add_parser("api-token", help="write a fresh random read-only API token to the secrets file (prints nothing)")
    a = ap.parse_args(argv)
    try:
        return {"serve": cmd_serve, "preflight": cmd_preflight, "init-users": cmd_init_users,
                "api-token": cmd_api_token}[a.cmd](a)
    except StagingRefused as e:
        print(f"REFUSED: {redact_line(str(e))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # run the canonical module, not this __main__ copy: the admin page imports ``twin_agent.hub.production`` by name
    from twin_agent.hub.production import main as _canonical_main
    sys.exit(_canonical_main())
