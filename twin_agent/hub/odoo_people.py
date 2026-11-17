"""Per-person Odoo credentials: each Odoo action runs AS the person who owns the task (Sysadmin/Riley, 15 Nov 2026).

WHO ACTS (the one table; a reviewer should check it):

  | action                                              | acts as                                              |
  |-----------------------------------------------------|------------------------------------------------------|
  | create ticket (incl. a linked "Follows up #N" one)  | automation (intake: no owner exists yet)             |
  | assign at intake (stage 5)                          | automation (same intake step)                               |
  | client EMAIL follow-up (owner-approved, from the  | the person who APPROVED it (tg id -> uid; unmapped = |
  |   team alias; see client_email.py)                | blocked); the visible sender is the team alias       |
  | internal notes (ack, replies, confirm, resolve,     | the ticket OWNER; no owner yet / unknown -> automation|
  |   proof-video link, hold/close notes)               |                                                      |
  | reassign (owner override, has ``actor_tg_id``)      | the person who TAPPED (``hub.odoo_tg_people`` maps   |
  |                                                     | Telegram id -> Odoo uid); NOT mapped -> BLOCKED      |
  |                                                     | (tapper_unmapped) unless the explicit fallback flag  |
  |                                                     | is on (then automation, on_behalf_of = tg hash).     |
  |                                                     | The audit row records the old AND the new owner.     |

SAFETY RULES (each one is tested):
  * People are mapped by Odoo UID (``hub.odoo_people: {uid: secret}``), never by display name. Secrets are read through
    the same ``creds`` backend as every other vault entry (``odoo-riley``, ``odoo-supervisor``, ``odoo-principal``,
    ``odoo-automation``; JSON ``{"login", "api_key"}``). On a PC they may sit in a secrets folder (``hub.secrets_dir``,
    read-only ``DirSecretsBackend``); on the server they are in the vault. No path is hard-coded here.
  * IDENTITY CHECK before the FIRST write of a person's gateway: one guarded read (so the login happens), then the
    authenticated uid MUST equal the expected owner uid (or ``hub.odoo_automation_uid``), else ``IdentityMismatch``
    and nothing is written. A gateway that failed is never cached (a key that appears later is picked up).
  * MISSING KEY = BLOCKED, never a silent fallback. ``KeyMissing`` is raised (so the message is retried by the hub's
    history-based backoff); a blocked NOTE is parked in the store and replayed by ``Hub.tick`` once the key exists.
    ``hub.odoo_fallback_to_automation: true`` (default false) explicitly allows acting as automation instead; the
    audit row then says acting=automation, on_behalf_of=<owner key name | tg hash>, reason=no_key | tapper_unmapped.
    Every audit row has ``role`` (automation | person | none), so the interim 'automation = odoo-riley' is visible.
  * Every per-person ``GuardedOdoo`` shares the SAME conventions (``conv``) and the SAME ``writes_on`` gate, so the guard
    (create/write/message_post only, no unlink, mail suppressed on create) holds for every person unchanged. The guard's
    comment-to-Principal path (``_check_comment_to_principal``) is for the desk's own transport and needs ``notify_partner``,
    which no hub path ever passes: a per-person gateway cannot post it (tested), and nothing here widens the guard.
  * AUDIT (hub store, namespace ``odoo_audit``): timestamp, acting key NAME, on_behalf_of, model, method, ticket id,
    outcome (ok / refused / blocked / error) + a plain reason. Never a key, a login, a phone number or a message id.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import creds
from ..helpdesk import guard
from ..helpdesk.odoo import JsonRpcTransport
from . import store as store_mod
from .odoo_gateway import GuardedOdoo

log = logging.getLogger("twin_agent.hub")
AUTOMATION = "automation"
PENDING_NS = "pending_notes"
PARK_TTL = timedelta(days=14)


class ActorRefused(guard.WriteRefused):
    """The action was not run because of WHO would run it. The message names the rule, never a value."""

    reason = "refused"


class KeyMissing(ActorRefused):
    reason = "no_key"


class IdentityMismatch(ActorRefused):
    reason = "identity_mismatch"


class TapperUnmapped(ActorRefused):
    reason = "tapper_unmapped"


def tg_label(tg_id) -> str:
    """A short hash of a Telegram id for the audit (who tapped) without storing the id itself."""
    import hashlib
    return "tg:" + hashlib.sha256(str(tg_id).encode()).hexdigest()[:8]


class DirSecretsBackend:
    """A read-only, keyring-shaped backend over a folder of ``<name>.json`` files (the PC's secrets folder). The
    folder comes from config (``hub.secrets_dir``); there is no default path. ``creds`` reads through it exactly as
    through the Windows vault, so ``has_secret`` / ``get_fields`` behave the same."""

    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(os.path.expanduser(str(directory)))

    def get_password(self, service: str, name: str) -> str | None:
        p = self.dir / f"{name}.json"
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def set_password(self, *_a, **_k):
        raise creds.VaultError("the secrets-folder backend is read-only")


@dataclass
class PeopleConfig:
    people: dict[int, str] = field(default_factory=dict)        # Odoo uid -> secret name
    automation_secret: str = ""
    automation_uid: int = 0
    tg_people: dict[int, int] = field(default_factory=dict)     # Telegram id -> Odoo uid (who tapped a reassign)
    fallback_to_automation: bool = False

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "PeopleConfig":
        h = (cfg or {}).get("hub") or {}
        pc = cls(people={int(k): str(v) for k, v in (h.get("odoo_people") or {}).items()},
                 automation_secret=str(h.get("odoo_automation") or ""),
                 automation_uid=int(h.get("odoo_automation_uid") or 0),
                 tg_people={int(k): int(v) for k, v in (h.get("odoo_tg_people") or {}).items()},
                 fallback_to_automation=h.get("odoo_fallback_to_automation") is True)
        if not pc.automation_secret or not pc.automation_uid:
            raise ValueError("hub.odoo_automation and hub.odoo_automation_uid must both be set")
        return pc


def open_person_transport(cfg: dict, secret: str, *, backend=None) -> JsonRpcTransport:
    """A transport for one vault secret. Raises ``KeyMissing`` (plain reason, never a value) when it is absent."""
    o = (cfg or {}).get("odoo_sys") or {}
    if not o.get("url") or not o.get("db"):
        raise ActorRefused("odoo_sys.url and odoo_sys.db must be set")
    try:
        present = creds.has_secret(secret, backend=backend)
    except Exception:                                       # noqa: BLE001 - unknown name / vault down: fail closed
        present = False
    if not present:
        raise KeyMissing(f"no Odoo key stored for {secret}")
    try:
        f = creds.get_fields(secret, backend=backend, caller="hub_person_odoo", purpose="person-odoo")
        login, key = str(f["login"]), str(f["api_key"])
    except Exception:                                       # noqa: BLE001 - malformed entry: say so, leak nothing
        raise KeyMissing(f"the Odoo entry {secret} is unreadable (expected login and api_key)") from None
    return JsonRpcTransport(str(o["url"]), str(o["db"]), login, key)


class PersonOdoo:
    """Drop-in for ``GuardedOdoo`` in ``Hub(odoo=...)``: same create/assign/post_note, but each call picks the right
    person's guarded gateway lazily, checks identity, and audits. ``open_transport(secret_name)`` builds a transport
    (real: ``open_person_transport``; tests: a fake factory)."""

    acting = True                                           # the Hub switches on this to pass who is acting

    def __init__(self, *, config: PeopleConfig, open_transport: Callable[[str], object], conv, store,
                 writes_on: bool = False, clock: Callable | None = None):
        self.cfg, self._open, self.conv, self.store = config, open_transport, conv, store
        self.writes_on, self._clock = bool(writes_on), clock
        self._cache: dict[int, GuardedOdoo] = {}
        self._blocked_at: dict[tuple, datetime] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, cfg: dict, *, conv, store, backend=None, writes_on: bool = False, clock=None):
        """Production factory. ``backend`` None = the vault; a ``DirSecretsBackend`` for the PC secrets folder."""
        pc = PeopleConfig.from_cfg(cfg)
        sd = ((cfg or {}).get("hub") or {}).get("secrets_dir")
        be = backend if backend is not None else (DirSecretsBackend(sd) if sd else None)
        return cls(config=pc, open_transport=lambda name: open_person_transport(cfg, name, backend=be), conv=conv,
                   store=store, writes_on=writes_on, clock=clock)

    @property
    def safe_stages(self):
        return self.conv.safe_stages

    def uid_for_tg(self, tg_id) -> int | None:
        return self.cfg.tg_people.get(int(tg_id)) if tg_id is not None else None

    # ---------------------------------------------------------------- gateways (lazy, identity-checked, cached)

    def _secret_and_uid(self, uid: int | None) -> tuple[str, int]:
        if uid is None:
            return self.cfg.automation_secret, self.cfg.automation_uid
        secret = self.cfg.people.get(int(uid))
        if not secret:
            raise KeyMissing("no Odoo key is configured for this person (hub.odoo_people)")
        return secret, int(uid)

    def _gateway(self, uid: int | None) -> tuple[GuardedOdoo, str]:
        secret, expected = self._secret_and_uid(uid)
        with self._lock:
            gw = self._cache.get(expected)
            if gw is None:
                gw = self._verified(self._open(secret), expected)
                self._cache[expected] = gw                  # only a gateway that passed the identity check is cached
            elif int(getattr(gw.t, "uid", 0) or 0) != expected:
                self._cache.pop(expected, None)
                raise IdentityMismatch("the logged-in Odoo user is not the expected person")
        return gw, secret

    def _verified(self, transport, expected: int) -> GuardedOdoo:
        gw = GuardedOdoo(transport, conv=self.conv, writes_on=self.writes_on)    # shared conv + the SAME writes gate
        try:                                                # one guarded READ first: it performs the login
            gw.adapter.search_read("res.users", [["id", "=", expected]], ["id"], limit=1)
        except Exception:                                   # noqa: BLE001
            raise ActorRefused("the Odoo login for this person failed") from None
        uid = int(getattr(transport, "uid", 0) or 0)
        if not uid or uid != expected:
            raise IdentityMismatch("the logged-in Odoo user is not the expected person")
        return gw

    # ---------------------------------------------------------------- one audited action

    def _audit(self, acting: str, on_behalf: str, method: str, tid, outcome: str, reason: str = "", **extra) -> None:
        role = "automation" if acting == self.cfg.automation_secret else ("person" if acting else "none")
        row = {"acting": acting, "role": role, "on_behalf_of": on_behalf, "model": "helpdesk.ticket", "method": method,
               "ticket_id": int(tid or 0), "outcome": outcome, "reason": reason, **extra}
        try:
            store_mod.audit_append(self.store, row, self._clock() if self._clock else None)
        except Exception as e:                              # noqa: BLE001 - the audit must never hide the action
            log.error("odoo audit write failed: %s", type(e).__name__)

    def _resolve(self, as_uid: int | None, method: str, tid, extra: dict) -> tuple[GuardedOdoo, str, str, str]:
        """(gateway, acting key name, on_behalf_of, reason). Blocks (audited) when the person has no key."""
        try:
            gw, acting = self._gateway(as_uid)
            return gw, acting, "", ""
        except KeyMissing as e:
            if as_uid is None or not self.cfg.fallback_to_automation:
                self._audit_blocked(self._guess_acting(as_uid), method, tid, "blocked", str(e), extra)
                raise
            owner_key = self.cfg.people.get(int(as_uid), f"uid:{as_uid}")
            try:                                            # explicit opt-in only: act as automation, and say so
                gw, acting = self._gateway(None)
            except ActorRefused as e2:
                self._audit(self.cfg.automation_secret, owner_key, method, tid, "blocked", str(e2), **extra)
                raise
            return gw, acting, owner_key, "no_key"
        except ActorRefused as e:
            self._audit_blocked(self._guess_acting(as_uid), method, tid, "refused", f"{e.reason}: {e}", extra)
            raise

    def _audit_blocked(self, acting: str, method: str, tid, outcome: str, reason: str, extra: dict) -> None:
        """A replay that is still blocked is audited at most ONCE per person and reason per hour (not once per tick)."""
        if extra.get("replay"):
            k, now = (acting, reason), self._now()
            last = self._blocked_at.get(k)
            if last and now - last < timedelta(hours=1):
                return
            self._blocked_at[k] = now
        self._audit(acting, "", method, tid, outcome, reason, **extra)

    def _act(self, as_uid: int | None, method: str, tid, call: Callable[[GuardedOdoo], object], **extra):
        """Run ``call(gateway)`` as ``as_uid`` (None = automation) and audit the outcome."""
        gw, acting, on_behalf, reason = self._resolve(as_uid, method, tid, extra)
        return self._run(gw, acting, on_behalf, reason, method, tid, call, **extra)

    def _guess_acting(self, as_uid) -> str:
        return self.cfg.people.get(int(as_uid), "") if as_uid is not None else self.cfg.automation_secret

    def _run(self, gw, acting, on_behalf, reason, method, tid, call, **extra):
        try:
            out = call(gw)
        except guard.WriteRefused as e:
            self._audit(acting, on_behalf, method, tid, "refused", str(e), **extra)
            raise
        except Exception as e:                              # noqa: BLE001
            self._audit(acting, on_behalf, method, tid, "error", type(e).__name__, **extra)
            raise
        self._audit(acting, on_behalf, method, tid, "ok", reason, **extra)
        return out

    # ---------------------------------------------------------------- the GuardedOdoo surface

    def create_ticket(self, vals: dict, *, context: dict) -> int:
        return self._act(None, "create", 0, lambda gw: gw.create_ticket(vals, context=context))

    def create_test_client_contact(self, name: str, phone: str, comment: str = "") -> int:
        """The production test-client contact, created as the automation account and audited (model res.partner)."""
        return self._act(None, "create", 0, lambda gw: gw.create_test_client_contact(name, phone, comment),
                         model="res.partner")

    def create_client_contact(self, name: str, phone: str, parent_id=None, category_id=None) -> int:
        return self._act(None, "create", 0, lambda gw: gw.create_client_contact(name, phone, parent_id, category_id),
                         model="res.partner")

    def link_client_contact(self, partner_id: int, parent_id: int) -> None:
        return self._act(None, "write", 0, lambda gw: gw.link_client_contact(partner_id, parent_id), model="res.partner")

    def read_partner(self, partner_id: int):
        rows = self.search_read("res.partner", [["id", "=", int(partner_id)]], ["name", "parent_id", "is_company"], limit=1)
        return rows[0] if rows else None

    def _act_tapper(self, tg_id, method: str, tid, call: Callable[[GuardedOdoo], object], *, allow_fallback: bool = True,
                    **extra):
        """Run as the PERSON WHO TAPPED (Telegram id -> Odoo uid). Unmapped = BLOCKED (audited, tapper_unmapped) unless
        ``odoo_fallback_to_automation`` is on: then it runs as automation, audited on_behalf_of=<tg hash>."""
        uid, label = self.uid_for_tg(tg_id), tg_label(tg_id)
        if uid is not None:
            return self._act(uid, method, tid, call, actor_tg=label, **extra)
        if not (allow_fallback and self.cfg.fallback_to_automation):
            self._audit("", label, method, tid, "blocked", "tapper_unmapped", actor_tg=label, **extra)
            raise TapperUnmapped("the person who tapped has no Odoo mapping (hub.odoo_tg_people)")
        try:
            gw, acting = self._gateway(None)
        except ActorRefused as e:
            self._audit(self.cfg.automation_secret, label, method, tid, "refused", f"{e.reason}: {e}", actor_tg=label, **extra)
            raise
        return self._run(gw, acting, label, "tapper_unmapped", method, tid, call, actor_tg=label, **extra)

    def assign(self, tid: int, user_id: int, *, tapper: bool = False, actor_tg_id=None, old_owner_uid: int = 0) -> None:
        """Intake (default) = automation. A REASSIGN (``tapper=True``) runs as the person who tapped (``actor_tg_id``),
        blocked when unmapped; the audit row has the old and the new owner."""
        extra = {"old_owner_uid": int(old_owner_uid or 0), "new_owner_uid": int(user_id)}
        if tapper:
            self._act_tapper(actor_tg_id, "write", tid, lambda gw: gw.assign(tid, user_id), **extra)
        else:
            self._act(None, "write", tid, lambda gw: gw.assign(tid, user_id), **extra)

    def post_note(self, tid: int, body: str, *, owner_uid: int | None = None) -> None:
        try:
            self._act(owner_uid, "message_post", tid, lambda gw: gw.post_note(tid, body))
        except ActorRefused:                                # no key / wrong identity / login failed: parked, not lost
            self._park_note(tid, body, owner_uid)
            raise

    def send_client_email(self, tid: int, body: str, *, partner_id: int, email_from: str, aliases, store, nonce: str,
                          approver_tg_id=None) -> None:
        """An owner-approved client EMAIL runs as the person who APPROVED it (Telegram id -> Odoo uid). An unmapped
        approver is ALWAYS blocked (the fallback flag never applies to a client email). The sender the client sees is
        the team alias. Never parked or replayed: a blocked client email stays blocked until a human re-approves."""
        self._act_tapper(approver_tg_id, "message_post", tid,
                         lambda gw: gw.send_client_email(tid, body, partner_id=partner_id, email_from=email_from,
                                                         aliases=aliases, store=store, nonce=nonce),
                         allow_fallback=False, channel="email")

    def search_read(self, model, domain, fields, **kw):
        return self._gateway(None)[0].search_read(model, domain, fields, **kw)

    # ---------------------------------------------------------------- parked notes (blocked: no key yet)

    def _park_note(self, tid: int, body: str, owner_uid: int | None) -> None:
        import uuid
        key = uuid.uuid4().hex
        self.store.put(PENDING_NS, key, {"_key": key, "tid": int(tid), "body": body, "owner_uid": owner_uid,
                                         "done": False, "ts": self._now().isoformat()})

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(timezone.utc)

    def replay_notes(self, resolve_owner: Callable[[int], int | None] | None = None, limit: int = 50) -> int:
        """Post the parked notes whose person now has a key (called by ``Hub.tick``).

        FAIR: rows go oldest-attempt first, and once a person is blocked in this pass their other notes are skipped, so a
        person without a key never starves the others (and is audited once per hour, not per note per tick). AT MOST ONCE:
        a row is marked ``posting`` BEFORE the call; if the outcome is unknown (an error after the post may have
        landed) it is parked as ``unknown`` and never re-posted. The owner is RE-RESOLVED now (a reassign may have
        happened); a posted, expired (``PARK_TTL``) or unknown note has its text blanked."""
        sent, attempts, now, blocked = 0, 0, self._now(), set()
        for key, doc in self._pending():
            if doc.get("ts") and now - datetime.fromisoformat(doc["ts"]) > PARK_TTL:
                self.store.put(PENDING_NS, key, {**doc, "body": "", "done": "expired"})
                continue
            owner = resolve_owner(doc["tid"]) if resolve_owner else doc.get("owner_uid")
            if owner in blocked or attempts >= limit:
                continue
            attempts += 1
            stamp = {"last_attempt": now.isoformat()}
            self.store.put(PENDING_NS, key, {**doc, **stamp, "state": "posting"})        # BEFORE the post
            try:
                self._act(owner, "message_post", doc["tid"], lambda gw, d=doc: gw.post_note(d["tid"], d["body"]),
                          replay=True)
            except ActorRefused:                            # still no key / wrong identity: stays parked
                self.store.put(PENDING_NS, key, {**doc, **stamp, "state": "pending"})
                blocked.add(owner)
                continue
            except guard.WriteRefused:                      # the guard will never allow it: drop the text
                self.store.put(PENDING_NS, key, {**doc, "body": "", "done": "refused"})
                continue
            except Exception as e:                          # noqa: BLE001 - it may have posted: NEVER post it again
                log.error("parked note on ticket %s: outcome unknown (%s); not re-posted", doc.get("tid"), type(e).__name__)
                self.store.put(PENDING_NS, key, {**doc, "body": "", "done": "unknown"})
                continue
            self.store.put(PENDING_NS, key, {**doc, "body": "", "done": True})
            sent += 1
        return sent

    def _pending(self) -> list[tuple[str, dict]]:
        """Rows still to replay, oldest attempt first. A row left ``posting`` (a crash between the mark and the result)
        is an unknown outcome: marked ``unknown`` here, never re-posted."""
        rows = []
        for d in self.store.scan(PENDING_NS):
            if d.get("done"):
                continue
            if d.get("state") == "posting":
                self.store.put(PENDING_NS, d["_key"], {**d, "body": "", "done": "unknown"})
                continue
            rows.append((d["_key"], d))
        rows.sort(key=lambda kd: (kd[1].get("last_attempt") or "", kd[1].get("ts") or ""))
        return rows
