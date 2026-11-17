"""Production helpers kept out of production.py (file size): held-message alerts to Teamhub, and the Cloudflare Access gate.

HOLD ALERTS. A held message (voice note, unknown number, two partners, a failed ack, a dead message, ...) must never be
invisible. ``ProdTeamBus`` is the hub's team bus in production: every triage line (already SAFE: a fixed label, a ticket
id, the last 4 digits, never client text) goes through ``HoldPoster`` to the Teamhub room via the SAME queue code the ticket
watcher uses (``teamhub_threads.TeamhubQueue``). Posting runs on one worker thread, so a slow poster never blocks a webhook.
It never raises. The per-ticket threads stay the watcher's job (``open_thread`` / ``post`` here are no-ops).

ACCESS GATE. The public admin host is served through the tunnel behind Cloudflare Access. ``AccessVerifier`` checks the
``Cf-Access-Jwt-Assertion`` token itself (RS256 against the team's cached JWKS, ``aud``, ``iss``, ``exp``/``nbf``) and the
email claim against an allow-list; ``AccessJwtGate`` answers 403 for any tunnel request that fails. The hub's own login
stays on top of it.
"""
from __future__ import annotations

import logging
import queue
import threading
import uuid
from typing import Any, Callable

from datetime import datetime, timezone
from pathlib import Path

from ..channels import whatsapp_cloud
from ..helpdesk import guard
from ..redact import redact
from .fakes import FakeTelegram
from .model import Owner
from .routing import Candidate, RuleAssigner
from .staging import _digits, _last4

log = logging.getLogger("twin_agent.hub.production")
TEST_CLIENT_PREFIX = guard.TEST_CLIENT_PREFIX


# ---------------------------------------------------------------- adapters

class ProdSTT:
    """Voice notes are not transcribed in this stopgap: hold with a plain reason (a human listens)."""

    def transcribe(self, audio_id: str, *, lang_hint: str = "") -> dict:
        return {"status": "blocked", "why": "voice notes are not transcribed yet; a person will listen", "text": None}


class StaffNumbers:
    """The team's own phones (``{"staff": {name: "digits"}}`` in a secrets file, re-read when it changes). A message from one
    of them is never a client's: no ticket, no ack. A missing file means none (the partner search still skips staff
    partners). The numbers are never logged."""

    def __init__(self, path: str = ""):
        self.path, self._mtime, self._nums, self._keys, self._tests = path, None, frozenset(), {}, {}
        self.exempt = None          # optional ``wa_id -> bool``: an open test-client window makes that number NOT staff

    def _load(self) -> None:
        import json
        import os
        try:
            p = os.path.expanduser(self.path)
            m = os.stat(p).st_mtime
            if m == self._mtime:
                return
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            staff = d.get("staff") if isinstance(d, dict) else None
            self._nums = frozenset(n for n in (_digits(v) for v in (staff or {}).values()) if len(n) >= 8)
            self._keys = {str(k).strip().lower(): n for k, n in ((k, _digits(v)) for k, v in (staff or {}).items())
                          if len(n) >= 8}
            tests = d.get("test_numbers") if isinstance(d, dict) else None      # non-staff numbers for ``test_numbers`` windows
            self._tests = {str(k).strip().lower(): n for k, n in ((k, _digits(v)) for k, v in tests.items())
                           if len(n) >= 8} if isinstance(tests, dict) else {}
            self._mtime = m
        except (OSError, ValueError, AttributeError):
            self._nums, self._keys, self._tests, self._mtime = frozenset(), {}, {}, None

    def phone_of(self, staff_key: str) -> str:
        """The digits of one named team member ("" when absent). Used only by the test-client window."""
        if not self.path:
            return ""
        self._load()
        return self._keys.get(str(staff_key).strip().lower(), "")

    def test_phone_of(self, key: str) -> str:
        """The digits of one named TEST number (the secrets file's ``test_numbers`` map; "" when absent). Never logged."""
        if not self.path:
            return ""
        self._load()
        return self._tests.get(str(key).strip().lower(), "")

    def is_staff_number(self, wa_id) -> bool:
        if not self.path:
            return False
        self._load()
        d = _digits(wa_id)
        return len(d) >= 8 and any(n == d or n[-9:] == d[-9:] for n in self._nums)

    def contains(self, wa_id) -> bool:
        """A staff phone, unless an open test-client window says this number is a test client right now."""
        if not self.is_staff_number(wa_id):
            return False
        return not (self.exempt is not None and self.exempt(wa_id))


class ProdKnowledge:
    """sender -> the ONE real res.partner whose mobile/phone matches (last 9 digits). No match, or more than one, is
    "no one" (the message is held, nothing is created). An Odoo error propagates: never a silent None."""

    def __init__(self, odoo: GuardedOdoo, staff: StaffNumbers | None = None, windows=None):
        self.odoo, self.staff, self.windows = odoo, staff, windows

    CAP = 200

    @staticmethod
    def _same_number(stored, wa: str) -> bool:
        """A stored number with a country code (+ or 00) must match in full; a local-format one by its last 9 digits."""
        raw = str(stored or "").strip()
        d = _digits(raw)
        if raw.startswith("+") or d.startswith("00"):
            return d.removeprefix("00") == wa
        return len(d) >= 9 and d[-9:] == wa[-9:]

    def partner_for(self, wa_id: str) -> int | None:
        """The ONE matching client partner, else None (held, and the hold is posted). Staff (partners with a user) are not
        clients. A company and its contact sharing the number count as one (the contact). Hitting the row cap = None."""
        d = _digits(wa_id)
        if len(d) < 8:
            return None
        w = self.windows.active(d) if self.windows is not None else None
        if w is not None:                                  # an OPEN test-client window: its ONE contact, never a phone search
            return self.windows.contact_id(self.odoo, w)
        if self.staff is not None and self.staff.contains(d):
            return None
        pat = "%" + "%".join(d[-8:]) + "%"                # tolerant of spaces and dashes inside the stored number
        hits: dict[int, int] = {}                          # partner id -> its parent id (0 = none)
        for fld in ("mobile", "phone"):
            rows = self.odoo.search_read("res.partner", [[fld, "=ilike", pat], ["user_ids", "=", False]],
                                         ["name", "mobile", "phone", "parent_id"], limit=self.CAP)
            if len(rows) >= self.CAP:
                log.warning("partner lookup for %s hit the %d-row cap: held as unknown", _last4(d), self.CAP)
                return None
            for r in rows:
                if str(r.get("name") or "").startswith(TEST_CLIENT_PREFIX):
                    continue                                # a test contact is reachable only through an open window
                if self._same_number(r.get(fld), d):
                    par = r.get("parent_id")
                    hits[int(r["id"])] = int(par[0] if isinstance(par, (list, tuple)) and par else par or 0)
        keep = [i for i in hits if i not in set(hits.values())]          # drop a parent when its contact also matched
        if len(keep) > 1:
            log.warning("sender %s matches %d partners: held as unknown", _last4(d), len(keep))
        return keep[0] if len(keep) == 1 else None

    def client_context(self, partner_id: int) -> dict:
        rows = self.odoo.search_read("res.partner", [["id", "=", int(partner_id)]], ["name"], limit=1)
        return {"source": "odoo", "name": str(rows[0].get("name") or "") if rows else "", "modules": [], "owner_uids": []}


class ProdWhatsApp:
    """Every send goes through here. Refused when: sending is off, a STOP file exists, or the recipient is not a known
    client. The Cloud adapter's own allowlist is opened for THIS one recipient for the duration of the call (clients are
    dynamic; it still enforces consent, the 24h window and its own STOP file). ``automatic=True`` is the instant ack only."""

    def __init__(self, inner, knowledge, *, send_enabled: bool, takes_automatic: bool, stop_files=()):
        self.inner, self.knowledge, self.send_enabled = inner, knowledge, send_enabled
        self.takes_automatic, self.stop_files = takes_automatic, tuple(stop_files)
        self._lock = threading.Lock()

    def _allow(self, number: str) -> None:
        w = getattr(self.inner, "w", None)
        if isinstance(w, dict):
            w["allowed_recipients"] = [number] if number else []

    intake_sender = None      # set by the hub: n -> True while this number is in an open intake v2 conversation

    def send_text(self, to, text, *, last_inbound_at, now=None, automatic: bool = False, intake: bool = False) -> dict:
        return self._send("send_text", to, text, last_inbound_at, now, automatic, intake)

    def send_interactive(self, to, interactive, *, last_inbound_at, now=None, automatic: bool = False,
                         intake: bool = False) -> dict:
        return self._send("send_interactive", to, interactive, last_inbound_at, now, automatic, intake)

    def _send(self, method: str, to, payload, last_inbound_at, now, automatic: bool, intake: bool) -> dict:
        if not self.send_enabled or self.inner is None:
            raise whatsapp_cloud.WhatsAppRefused("sending is off (send_enabled is false)")
        if any(Path(f).exists() for f in self.stop_files):
            raise whatsapp_cloud.WhatsAppRefused("emergency stop (STOP file)")
        n = _digits(to)
        if intake and n and callable(self.intake_sender) and self.intake_sender(n) and not self.knowledge.partner_for(n):
            # an intake question to a sender we are still identifying. partner_for is None for STAFF too, so the staff
            # check (and the closed-window audit) run here exactly as for every other message: staff never get one.
            staff = getattr(self.knowledge, "staff", None)
            wins = getattr(self.knowledge, "windows", None)
            if wins is not None and wins.audit_closed_refusal(n):
                raise whatsapp_cloud.WhatsAppRefused("the test client window is closed (the number is staff again)")
            if staff is not None and staff.contains(n):
                raise whatsapp_cloud.WhatsAppRefused("a staff number never gets intake questions")
        elif not n or not self.knowledge.partner_for(n):
            wins = getattr(self.knowledge, "windows", None)
            if wins is not None and wins.audit_closed_refusal(n):
                raise whatsapp_cloud.WhatsAppRefused("the test client window is closed (the number is staff again)")
            raise whatsapp_cloud.WhatsAppRefused("recipient is not a known client")
        kw = {"automatic": automatic} if self.takes_automatic else {}
        with self._lock:
            self._allow(n)
            try:
                res = getattr(self.inner, method)(n, payload, last_inbound_at=last_inbound_at,
                                                  now=now or datetime.now(timezone.utc), **(kw if method == "send_text" else {}))
            finally:
                self._allow("")
        log.info("whatsapp sent to %s automatic=%s", _last4(n), automatic)
        return res


# ---------------------------------------------------------------- default owner

class OwnerAssigner:
    """Who gets a new ticket. The ownership signals (this client's usual owner, the type's owner) pick a person when there is
    one. Otherwise the DEFAULT owner (Riley) gets it; if she is marked unavailable, the BACKUP (Supervisor). There is no capacity
    signal in Odoo that can be trusted (the open-ticket count assumes fixed closed-stage ids), so "unavailable" is the
    operator's list (``unavailable_owner_uids``). The answer carries ``fallback`` (``default_owner``, ``backup_owner``,
    ``no_one_available`` or "") so the hub can audit it."""

    def __init__(self, default: Owner, backup: Owner | None = None, unavailable=()):
        self.default, self.backup, self.unavailable = default, backup, {int(u) for u in unavailable}
        self._rank = RuleAssigner([Candidate(o) for o in (default, backup) if o is not None])

    def assign(self, analysis, client_context: dict) -> dict:
        pick = self._rank.assign(analysis, client_context or {})
        fb, owner, why = "", pick["owner"], pick["rationale"]
        if pick["score"] <= 0:
            owner, fb, why = self.default, "default_owner", f"assigned to {self.default.name} because: default owner (no better owner found)"
        if int(owner.uid) in self.unavailable:
            alts = [o for o in (self.default, self.backup) if o is not None and o.uid != owner.uid and int(o.uid) not in self.unavailable]
            if alts:
                owner, fb = alts[0], "backup_owner" if alts[0] is self.backup else "default_owner"
                why = f"assigned to {owner.name} because: the first choice is unavailable ({fb.replace('_', ' ')})"
            else:
                fb, why = "no_one_available", f"assigned to {owner.name} because: no one is marked available (kept the first choice)"
        return {"owner": owner, "uid": owner.uid, "score": pick["score"], "rationale": why, "factors": [why],
                "ranking": pick["ranking"], "fallback": fb}


# ---------------------------------------------------------------- hold alerts

class HoldPoster:
    """Posts SAFE lines as new threads in the Teamhub room. ``queue`` is a ``TeamhubQueue`` (or a test double with ``run``)."""

    def __init__(self, kq: Any):
        self._kq = kq
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._guard = threading.Lock()
        self.posted = 0
        self.failed = 0

    def post(self, text: str) -> None:
        """Queue one line. Never raises and never blocks the caller."""
        with self._guard:
            self._q.put(str(text))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="hold-poster", daemon=True)
                self._worker.start()

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything queued so far has been tried (tests and shutdown)."""
        done = threading.Event()
        threading.Thread(target=lambda: (self._q.join(), done.set()), daemon=True).start()
        return done.wait(timeout)

    def _loop(self) -> None:
        while True:
            try:
                text = self._q.get(timeout=30)
            except queue.Empty:
                return
            try:
                self._send(text)
            except Exception as e:                              # noqa: BLE001 - a Teamhub outage must not touch intake
                self.failed += 1
                log.error("hold alert not posted: %s", type(e).__name__)
            finally:
                self._q.task_done()

    def _send(self, text: str) -> None:
        key = f"hold-{uuid.uuid4().hex[:12]}"
        res = self._kq.run([{"key": key, "op": "create_thread", "text": redact(text)}])[0]
        if res.get("refused"):                                  # the scanner disliked the line: say it plainly instead
            key = f"{key}-g"
            res = self._kq.run([{"key": key, "op": "create_thread",
                                 "text": "HOLD: a message needs a person. Open the hub admin Holds page."}])[0]
        if res.get("ok"):
            self.posted += 1
        else:
            self.failed += 1
            log.error("hold alert not posted (poster said no)")


class ProdTeamBus:
    """The production team bus: only triage lines are posted (to Teamhub). Named ``teamhub`` so the hub's config check passes;
    it carries no client text. Never raises."""

    name = "teamhub"
    carries_client_text = False
    owns_approval_topic = False

    def __init__(self, poster: HoldPoster):
        self._poster = poster

    def __repr__(self) -> str:
        return "ProdTeamBus()"

    def open_thread(self, tid: int, owner: Any, brief: str) -> str:
        return f"ticket-{tid}"                                  # the ticket watcher opens the Teamhub thread

    def post(self, ref: str, text: str) -> None:
        return None

    def post_triage(self, text: str) -> None:
        try:
            self._poster.post(text)
        except Exception as e:                                  # noqa: BLE001
            log.error("triage post failed: %s", type(e).__name__)


class ProdApprovalTransport(FakeTelegram):
    """The approval gateway the hub API needs (the admin page approves). It is NOT an alert sink: triage text that may
    hold a client's words goes nowhere."""

    def post_triage(self, text: str) -> None:
        return None


# ---------------------------------------------------------------- Cloudflare Access

class AccessDenied(Exception):
    """The token or the person is not acceptable. The text is fixed and says nothing about why a token failed."""


class AccessVerifier:
    """Verifies a Cloudflare Access JWT. ``key_resolver(token) -> public key`` is injectable (tests use a local RSA key);
    the default fetches the team's JWKS (``/cdn-cgi/access/certs``) through a cached PyJWKClient."""

    def __init__(self, team_domain: str, aud: str, emails, *, key_resolver: Callable[[str], Any] | None = None,
                 leeway: int = 30):
        team = str(team_domain or "").strip().lower().removeprefix("https://").strip("/")
        if not team.endswith(".cloudflareaccess.com") or not str(aud or "").strip() or not emails:
            raise ValueError("access verifier needs a *.cloudflareaccess.com team domain, an AUD and an email allow-list")
        self.team, self.aud, self.leeway = team, str(aud).strip(), int(leeway)
        self.issuer = f"https://{team}"
        self.emails = {str(e).strip().lower() for e in emails if str(e).strip()}
        self._resolver, self._jwk = key_resolver, None

    def _key(self, token: str):
        if self._resolver is not None:
            return self._resolver(token)
        import jwt
        if self._jwk is None:
            self._jwk = jwt.PyJWKClient(f"https://{self.team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600, timeout=5)
        return self._jwk.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> str:
        """The verified, lower-cased email. Raises ``AccessDenied``."""
        import jwt
        if not token or len(token) > 8192:
            raise AccessDenied("no valid Access token")
        try:
            claims = jwt.decode(token, self._key(token), algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
                                leeway=self.leeway, options={"require": ["exp", "iss", "aud"]})
        except Exception:                                       # noqa: BLE001 - every failure looks the same from outside
            raise AccessDenied("no valid Access token") from None
        email = str(claims.get("email") or "").strip().lower()
        if email not in self.emails:
            raise AccessDenied("this person is not allowed")
        return email


class AccessJwtGate:
    """WSGI wrapper. A request that came through the tunnel (it carries ``CF-Connecting-IP`` or the public Host) needs a
    valid token from an allowed person, else 403. A direct local request passes (the hub's own login still applies). The
    verified email goes to the app in ``environ['aa.access_email']``; any client-sent value is overwritten or removed."""

    def __init__(self, app, verifier: AccessVerifier, public_host: str):
        self._app, self._v, self._host = app, verifier, str(public_host).strip().lower()

    def __getattr__(self, name):
        return getattr(self._app, name)

    def _via_tunnel(self, environ: dict) -> bool:
        host = str(environ.get("HTTP_HOST") or "").split(":")[0].strip().lower().rstrip(".")
        return bool(environ.get("HTTP_CF_CONNECTING_IP")) or host == self._host

    def __call__(self, environ, start_response):
        environ.pop("aa.access_email", None)
        if self._via_tunnel(environ):
            try:
                environ["aa.access_email"] = self._v.verify(str(environ.get("HTTP_CF_ACCESS_JWT_ASSERTION") or ""))
            except AccessDenied:
                body = b"Cloudflare Access: not allowed."
                start_response("403 Forbidden", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
                return [body]
        return self._app(environ, start_response)


def load_access_file(path: str) -> dict:
    """``{"team_domain", "aud"}`` from the secrets file (never printed). Missing or unreadable = {}."""
    import json
    import os
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return {"team_domain": str(d.get("team_domain") or d.get("access_team_domain") or ""),
            "aud": str(d.get("aud") or d.get("access_aud") or "")} if isinstance(d, dict) else {}


class RecordingQueue:
    """A stand-in for ``TeamhubQueue`` in dry runs and tests: remembers the ops, posts nothing."""

    def __init__(self):
        self.ops: list[dict] = []

    def run(self, ops: list[dict]) -> list[dict]:
        self.ops.extend(ops)
        return [{"ok": True, "key": o["key"]} for o in ops]
