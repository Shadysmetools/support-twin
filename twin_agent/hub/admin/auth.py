"""Passwords (stdlib scrypt), server-side sessions, CSRF tokens and a login rate limiter. No third-party deps."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

_N, _R, _P = 2 ** 17, 8, 1      # OWASP minimum; hashlib needs maxmem >= 128*N*r (+ overhead)
_MAXMEM = 2 ** 28


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. A malformed stored hash is simply False (fail closed)."""
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, want = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(want), maxmem=_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


_DUMMY = hash_password("not-a-real-password")


def check_login(users: dict, username: str, password: str):
    """Return the StaffUser or None. An unknown user still pays the scrypt cost (no username-probing by timing)."""
    user = users.get((username or "").strip().lower())
    ok = verify_password(password or "", user.pw_hash if user else _DUMMY)
    return user if (user and ok) else None


def tokens_equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


@dataclass
class Session:
    sid: str
    username: str
    csrf: str
    created: float
    last_seen: float


@dataclass
class SessionStore:
    idle_seconds: float = 30 * 60
    max_seconds: float = 8 * 3600
    clock: object = time.time
    _s: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, username: str) -> Session:
        now = self.clock()
        s = Session(secrets.token_urlsafe(32), username, secrets.token_urlsafe(24), now, now)
        with self._lock:
            self._s[s.sid] = s
        return s

    def get(self, sid: str):
        now = self.clock()
        with self._lock:
            s = self._s.get(sid or "")
            if s and (now - s.last_seen > self.idle_seconds or now - s.created > self.max_seconds):
                del self._s[s.sid]
                s = None
            if s:
                s.last_seen = now
            return s

    def destroy(self, sid: str) -> None:
        with self._lock:
            self._s.pop(sid or "", None)


@dataclass
class RateLimiter:
    """Failed-login limiter (sliding window). Two keys per attempt:

    * per IP (``ip_max_fails``): stops one address guessing across many usernames;
    * per (IP, username) pair (``max_fails``): stops guessing one account from one address.

    There is deliberately NO per-username key shared across addresses and NO global counter: either would let a stranger
    lock a real person out with a handful of bad passwords. Distributed guessing is bounded by the per-IP cap, the scrypt
    cost, and the Cloudflare Access / 2FA precondition (README). Memory is bounded: at most ``max_keys`` keys, oldest
    evicted first, empty keys dropped."""
    max_fails: int = 5
    ip_max_fails: int = 15
    window: float = 300.0
    max_keys: int = 4096
    clock: object = time.time
    _f: OrderedDict = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _recent(self, key: str) -> list:
        cut = self.clock() - self.window
        live = [t for t in self._f.get(key, ()) if t > cut]
        if live:
            self._f[key] = live
            self._f.move_to_end(key)
        else:
            self._f.pop(key, None)
        return live

    def _pair(self, ip: str, user: str) -> str:
        return f"pair:{ip[:64]}|{user[:64]}"

    def blocked(self, ip: str, user: str) -> bool:
        with self._lock:
            return (len(self._recent(f"ip:{ip[:64]}")) >= self.ip_max_fails
                    or len(self._recent(self._pair(ip, user))) >= self.max_fails)

    def fail(self, ip: str, user: str) -> None:
        now = self.clock()
        with self._lock:
            for k in (f"ip:{ip[:64]}", self._pair(ip, user)):
                self._f[k] = self._recent(k) + [now]
                self._f.move_to_end(k)
            while len(self._f) > self.max_keys:
                self._f.popitem(last=False)

    def reset(self, ip: str, user: str) -> None:
        with self._lock:
            self._f.pop(self._pair(ip, user), None)
