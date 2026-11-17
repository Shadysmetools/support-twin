"""A time-limited TEST-CLIENT window for the production hub (so a team member can test the live number from her own phone).

Config (``production.yaml``): ``test_clients: [{staff_key: riley, until: "2026-11-16T18:00:00Z", ref: "why"}]``.

While ``now < until`` that staff number is NOT staff: it maps to ONE dedicated Odoo contact, ``Acme Test Client -
<Name>``, with the phone from the staff file and NO email. After ``until`` the number is staff again, by itself (nothing
to switch off). Preflight refuses an ``until`` more than 24h ahead and a blank ``ref``.

Rails: the contact is found by its exact name and reused (never duplicated); it is created only through the guard
purpose ``test_client_contact`` (name, phone, an optional comment, no email, mail-off context, one ``res.partner``
create). The contact is reached ONLY through an open window: the normal phone search skips it, so it can never
match as a real client once the window is closed. Riley's own Odoo USER partner is never matched. Audit rows: window
opened, window closed, and one per message. Never a phone number in a log or an audit row.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..helpdesk import guard
from . import store as store_mod

log = logging.getLogger("twin_agent.hub.production")

PREFIX = guard.TEST_CLIENT_PREFIX
MAX_AHEAD = timedelta(hours=24)
CACHE_S = 300                                     # a found/created contact id is re-checked after this long
NS = "test_client_window"
_KEY = re.compile(r"[A-Za-z][A-Za-z_ ]{0,30}")


@dataclass(frozen=True)
class ClientTestWindow:
    staff_key: str
    until: datetime                               # timezone-aware, UTC
    ref: str
    source: str = "staff"                         # "staff" (a team phone) or "number" (a non-staff test number: ``test_numbers``)


def _when(v) -> datetime:
    """An ISO datetime (a trailing Z is UTC; no zone = UTC) or a datetime YAML already parsed."""
    if isinstance(v, datetime):
        d = v
    elif isinstance(v, str) and v.strip():
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    else:
        raise ValueError("until must be an ISO datetime (UTC)")
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_windows(raw) -> tuple[ClientTestWindow, ...]:
    """The ``test_clients`` list. Raises ValueError with a plain reason (never a value) when the shape is wrong."""
    if raw in (None, "", [], ()):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("test_clients must be a list of {staff_key, until, ref}")
    out = []
    for e in raw:
        if not isinstance(e, dict) or set(e) - {"staff_key", "until", "ref"}:
            raise ValueError("each test_clients entry takes only staff_key, until and ref")
        key = str(e.get("staff_key") or "").strip()
        if not _KEY.fullmatch(key):
            raise ValueError("test_clients: staff_key must be a team member's key (letters only)")
        try:
            until = _when(e.get("until"))
        except (TypeError, ValueError):
            raise ValueError("test_clients: until must be an ISO datetime in UTC, e.g. 2026-11-16T18:00:00Z") from None
        out.append(ClientTestWindow(key.lower(), until, str(e.get("ref") or "").strip()))
    return tuple(out)


def parse_number_windows(raw) -> tuple[ClientTestWindow, ...]:
    """The ``test_numbers`` list: ``[{phone: <key in the secrets file's test_numbers map>, until, ref}]``. The digits live
    only in the secrets file; the config names a KEY. Same shape rules as ``test_clients``."""
    if raw in (None, "", [], ()):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("test_numbers must be a list of {phone, until, ref}")
    out = []
    for e in raw:
        if not isinstance(e, dict) or set(e) - {"phone", "until", "ref"}:
            raise ValueError("each test_numbers entry takes only phone (a key name), until and ref")
        key = str(e.get("phone") or "").strip()
        if not _KEY.fullmatch(key):
            raise ValueError("test_numbers: phone must be a KEY NAME from the secrets file's test_numbers map (letters only), not a number")
        try:
            until = _when(e.get("until"))
        except (TypeError, ValueError):
            raise ValueError("test_numbers: until must be an ISO datetime in UTC, e.g. 2026-11-16T18:00:00Z") from None
        out.append(ClientTestWindow(key.lower(), until, str(e.get("ref") or "").strip(), "number"))
    return tuple(out)


def window_problems(windows, now: datetime | None = None) -> list[str]:
    """Plain reasons a window list may not run (pure: no files, no network)."""
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    if len({w.staff_key for w in windows}) != len(windows):
        out.append("test_clients / test_numbers: one window per key")
    for w in windows:
        if not w.ref.strip():
            out.append(f"test_clients[{w.staff_key}]: ref must say why the window is open (blank is refused)")
        if w.until - now > MAX_AHEAD:
            out.append(f"test_clients[{w.staff_key}]: until is more than 24 hours ahead (the longest window is 24h)")
    return out


def international(digits: str) -> bool:
    """A full international number (country code first, no leading 0): the only form a window may match on."""
    return re.fullmatch(r"[1-9][0-9]{9,14}", str(digits or "")) is not None


def contact_name(staff_key: str) -> str:
    return f"{PREFIX}{str(staff_key).replace('_', ' ').strip().title()}"


class ClientTestWindows:
    """The runtime side: which number is a test client right now, its Odoo contact, and the audit trail."""

    def __init__(self, windows, staff, *, clock=None, store=None):
        self.windows, self.staff, self.store = tuple(windows), staff, store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ids: dict[str, tuple[int, datetime]] = {}
        self._lock = threading.Lock()

    def now(self) -> datetime:
        n = self._clock()
        return n if n.tzinfo else n.replace(tzinfo=timezone.utc)

    # ---- who is a test client right now
    def active(self, wa_id) -> ClientTestWindow | None:
        """The OPEN window for this number, else None. Open = ``now < until`` and the number is that person's own."""
        if not self.windows:
            return None
        now = self.now()
        d = "".join(c for c in str(wa_id or "") if c.isdigit())
        return next((w for w in self.windows if now < w.until and self._mine(w, d)), None)

    def _mine(self, w: ClientTestWindow, d: str) -> bool:
        """A WhatsApp id is always full international, so the match is on ALL digits of the staff phone (and that phone
        must itself be a full international number): a foreign number sharing the last 9 digits never matches."""
        mine = self._phone(w)
        return international(mine) and mine == d

    def _phone(self, w: ClientTestWindow) -> str:
        return self.staff.test_phone_of(w.staff_key) if w.source == "number" else self.staff.phone_of(w.staff_key)

    def expired_for(self, wa_id) -> ClientTestWindow | None:
        """A window for this number that has CLOSED (now >= until), else None."""
        now = self.now()
        d = "".join(c for c in str(wa_id or "") if c.isdigit())
        return next((w for w in self.windows if now >= w.until and self._mine(w, d)), None)

    def is_open(self, wa_id) -> bool:
        return self.active(wa_id) is not None

    # ---- the one Odoo contact (find-or-create)
    def contact_id(self, odoo, w: ClientTestWindow) -> int | None:
        """The test contact's partner id, created once and reused. None = refuse (held): two contacts with the name, one
        with an email, or one linked to a user. An Odoo error propagates (never a silent None)."""
        name, now = contact_name(w.staff_key), self.now()
        with self._lock:
            hit = self._ids.get(name)
            if hit and now - hit[1] < timedelta(seconds=CACHE_S):
                return hit[0]
            pid = self._find_or_create(odoo, name, w)
            if pid:
                self._ids[name] = (pid, now)
            return pid

    def _find_or_create(self, odoo, name: str, w: ClientTestWindow) -> int | None:
        rows = odoo.search_read("res.partner", [["name", "=", name], ["active", "in", [True, False]]],
                                ["name", "email", "user_ids", "parent_id", "active"], limit=5)     # archived ones too
        same = [r for r in rows if r.get("name") == name]
        if len(same) > 1:
            log.warning("test client contact %r exists more than once: held", name)
            return None
        if same:
            r = same[0]
            if r.get("email") or r.get("user_ids") or r.get("parent_id") or r.get("active") is False:
                log.warning("test client contact %r has an email, a user, a company or is archived: held", name)
                return None
            return int(r["id"])
        phone = self._phone(w)
        if not international(phone):
            return None
        try:
            return int(odoo.create_test_client_contact(name, "+" + phone, f"Test client window: {w.ref}"[:300]))
        except guard.WriteRefused as e:                   # Odoo writes are off (or the guard said no): held, not an error
            log.warning("test client contact %r not created: %s", name, e)
            return None

    # ---- audit: opened, closed, and every message
    def _audit(self, w: ClientTestWindow, method: str, once: bool) -> None:
        if self.store is None:
            return
        try:
            if once and not self.store.put_if_absent(NS, f"{w.staff_key}-{w.until.isoformat()}-{method}", {"at": self.now().isoformat()}):
                return
            store_mod.audit_append(self.store, {"acting": "hub", "role": "test_client_window", "on_behalf_of": w.staff_key,
                                                "model": "res.partner", "method": method, "ticket_id": 0, "outcome": "ok",
                                                "reason": w.ref, "until": w.until.isoformat()}, self.now())
        except Exception as e:                                     # noqa: BLE001 - the audit must never break intake
            log.error("test client audit write failed: %s", type(e).__name__)

    def sweep(self) -> None:
        """Once per window: ``window_open`` while it is open, ``window_closed`` after ``until``. Safe to call often."""
        for w in self.windows:
            self._audit(w, "window_open" if self.now() < w.until else "window_closed", once=True)

    def audit_closed_refusal(self, wa_id) -> bool:
        """A reply to a number whose window has closed was refused: say so in the log and the audit. True if it was one."""
        w = self.expired_for(wa_id)
        if w is None:
            return False
        log.warning("test client window for %s is closed: the reply was refused", w.staff_key)
        self._audit(w, "reply_refused_window_closed", once=False)
        return True

    def audit_message(self, w: ClientTestWindow) -> None:
        self._audit(w, "test_client_message", once=False)
