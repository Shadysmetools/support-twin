"""Per-business Meta assets in the SECRETS store (a directory outside git, one 0600 JSON per business), and the
single-use Embedded Signup state. The hub DB never holds a token, and nothing here logs.

Record: ``{"waba_id", "phone_number_id", "business_token", "token_type", "expires_in", "last_event", "claimed_*",
"updated"}``. ``waba_id`` / ``phone_number_id`` are the ids the token was VERIFIED to grant (Graph ``debug_token``); the
``claimed_*`` ids are what the browser posted: display only, never used to pick a token. An id belongs to exactly one
business: ``save`` refuses a collision and ``token_by_id`` errors on 0 or more than 1 match. ``public()`` is the only view the admin pages may render: it never contains the token.
On Windows the 0600 mode is ignored; the directory's profile ACL (``~/.claude/secrets``) is the protection."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ROUTING_IDS = ("waba_id", "phone_number_id")        # verified; unique across businesses
TOKEN_FIELD = "business_token"
KEEP = ROUTING_IDS + ("token_type", "expires_in", "last_event", "cancel_step", "error_code", TOKEN_FIELD,
                      "claimed_waba_id", "claimed_phone_number_id", "claimed_business_id")


class ConflictError(ValueError):
    """Another business already holds this WABA or phone number id."""


def valid_slug(slug: str) -> bool:
    return isinstance(slug, str) and bool(SLUG.fullmatch(slug)) and slug != "acme-own"   # reserved: the read-only own-number entry


def valid_id(value) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit() and 0 < len(value) <= 32


class BusinessStore:
    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self._lock = threading.Lock()

    def _path(self, slug: str) -> Path:
        if not valid_slug(slug):
            raise ValueError("bad business name")
        return self.dir / f"{slug}.json"

    def get(self, slug: str) -> dict:
        try:
            return json.loads(self._path(slug).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, slug: str, update: dict, *, remove: tuple = ()) -> None:
        """Merge ``update`` (only known fields) into the record, atomically, mode 0600. ``remove`` drops fields."""
        path = self._path(slug)                              # validates the name BEFORE any path is built from it
        clean = {k: v for k, v in update.items() if k in KEEP and v is not None}
        with self._lock:
            self._make_dir()
            for k in ROUTING_IDS:
                if clean.get(k) and any(s != slug and r.get(k) == clean[k] for s, r in self.records()):
                    raise ConflictError(f"{k} is already held by another business")
            rec = self.get(slug)
            rec.update(clean)
            for k in remove:
                rec.pop(k, None)
            rec["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            tmp = self.dir / f".{slug}.{secrets.token_hex(4)}.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(rec, f, indent=2)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)                 # a failed write must not leave a token in a temp file

    def reset_claims(self, slug: str) -> None:
        """A new sign-up for an existing business: drop the previous attempt's browser claims and events."""
        if self.get(slug):
            self.save(slug, {}, remove=("claimed_waba_id", "claimed_phone_number_id", "claimed_business_id",
                                        "cancel_step", "error_code"))

    def _make_dir(self) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.dir, 0o700)                        # slugs are client names: not world-listable

    def slugs(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.dir.glob("*.json") if valid_slug(p.stem))
        except OSError:
            return []

    def records(self) -> list[tuple[str, dict]]:
        """Internal: includes the token. Only the Graph client may call this."""
        return [(s, self.get(s)) for s in self.slugs()]

    def public(self) -> list[dict]:
        """What the pages may show: ids and whether a token is stored. Never the token."""
        out = []
        for slug, rec in self.records():
            out.append({"business": slug, "waba_id": rec.get("waba_id", ""), "phone_number_id": rec.get("phone_number_id", ""),
                        "token_stored": bool(rec.get(TOKEN_FIELD)), "last_event": rec.get("last_event", ""),
                        "updated": rec.get("updated", "")})
        return out

    def token_for(self, business: str, *, waba_id: str = "", phone_number_id: str = "") -> str:
        """The token of THIS business, and only if the id belongs to it. '' otherwise."""
        rec = self.get(business) if valid_slug(business) else {}
        ok = (waba_id and rec.get("waba_id") == waba_id) or (phone_number_id and rec.get("phone_number_id") == phone_number_id)
        return rec.get(TOKEN_FIELD, "") if ok else ""

    def token_by_id(self, *, waba_id: str = "", phone_number_id: str = "") -> str:
        """For callers that only know an id (a future sender / webhook router). Exactly one business must hold it:
        0 or more than 1 match raises, it never takes 'the first'."""
        hits = [r for _s, r in self.records()
                if (waba_id and r.get("waba_id") == waba_id) or (phone_number_id and r.get("phone_number_id") == phone_number_id)]
        if len(hits) != 1:
            raise LookupError(f"{len(hits)} businesses match this id")
        return hits[0].get(TOKEN_FIELD, "")


class SignupStates:
    """Server-generated Embedded Signup states. Minted ONLY by a logged-in admin (POST /connect/start).

    * The CODE is single use per state. The session info is informational (it never routes a token), so it may be posted
      more than once until a FINISH event (cancel, then try again on the same page works), at most ``MAX_SESSION`` times.
    * Each state has a lock so the code and the session info (two independent browser posts) never race.
    * When the exchanged token cannot be bound yet (the token grants several WABAs and the session info has not arrived),
      the token is kept IN MEMORY on the state until it can be verified or the state expires. It is never written to
      disk unverified, and it is dropped as soon as it is bound or the state is pruned."""

    MAX_SESSION = 20

    def __init__(self, *, ttl: float = 20 * 60, max_pending: int = 100, clock=time.time):
        self.ttl, self.max_pending, self.clock = ttl, max_pending, clock
        self._s: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._s.items() if v["exp"] < now]:
            del self._s[k]

    def new(self, business: str) -> str | None:
        """A fresh state for ``business``, or None when the pending table is full (bounded memory)."""
        if not valid_slug(business):
            return None
        with self._lock:
            self._prune()
            if len(self._s) >= self.max_pending:
                return None
            state = secrets.token_urlsafe(24)
            self._s[state] = {"exp": self.clock() + self.ttl, "business": business, "code": False, "final": False,
                              "n": 0, "token": None, "lock": threading.Lock()}
            return state

    def _live(self, state) -> dict | None:
        self._prune()
        return self._s.get(state) if isinstance(state, str) else None

    def peek(self, state) -> str | None:
        with self._lock:
            st = self._live(state)
            return st["business"] if st else None

    def lock_for(self, state):
        with self._lock:
            st = self._live(state)
            return st["lock"] if st else None

    def take(self, state, kind: str, event: str = "") -> str | None:
        """The business, or None. ``code``: once. ``session``: until a FINISH event, at most MAX_SESSION posts."""
        if kind not in ("code", "session"):
            return None
        with self._lock:
            st = self._live(state)
            if not st:
                return None
            if kind == "code":
                if st["code"]:
                    return None
                st["code"] = True
            else:
                if st["final"] or st["n"] >= self.MAX_SESSION:
                    return None
                st["n"] += 1
                st["final"] = event.upper().startswith("FINISH")
            return st["business"]

    def set_token(self, state, token: str | None) -> None:
        with self._lock:
            st = self._live(state)
            if st:
                st["token"] = token

    def token(self, state) -> str | None:
        with self._lock:
            st = self._live(state)
            return st["token"] if st else None

    def finish(self, state) -> None:
        with self._lock:
            self._s.pop(state, None)
