"""Throttles for the endpoints anyone can call without logging in (log in,
sign up, callsign lookup).

hamdoor runs a single uvicorn worker (the APRS feed lives in-process too), so
process memory is the right place to count; a restart forgets the counts,
which is harmless. Clients are told when to come back (429 + Retry-After),
which the frontend shows as "That's busy right now. Try again in a minute."

Keys are the client address uvicorn reports (behind a reverse proxy on the
same host every visitor may share one address, so the per-address limits
are generous) and, for log in, the callsign being tried.
"""

import math
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from app.config import get_settings

BUSY_TEXT = "Too many attempts. Try again in a minute."


def _clock() -> float:
    return time.monotonic()


class Throttle:
    """At most `limit` events per `window` seconds for each key (a sliding
    window of event times)."""

    def __init__(self, name: str, limit: int, window: float, max_keys: int = 20000):
        self.name, self.limit, self.window, self.max_keys = name, limit, window, max_keys
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque | None:
        q = self._hits.get(key)
        if q is None:
            return None
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self._hits[key]
            return None
        return q

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds until one more event is allowed for `key` (0 = now)."""
        now = _clock() if now is None else now
        with self._lock:
            q = self._trim(key, now)
            if q is None or len(q) < self.limit:
                return 0
            return max(1, math.ceil(q[0] + self.window - now))

    def add(self, key: str, now: float | None = None) -> None:
        now = _clock() if now is None else now
        with self._lock:
            self._hits.setdefault(key, deque()).append(now)
            if len(self._hits) > self.max_keys:
                for k in list(self._hits):
                    self._trim(k, now)
                while len(self._hits) > self.max_keys:  # still full: forget the oldest keys
                    self._hits.pop(next(iter(self._hits)))

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# every log in attempt from one address (bounds the bcrypt work one client can cause)
LOGIN_PER_ADDRESS = Throttle("login-address", 20, 60)
# wrong passwords for one callsign, from anywhere: 5 in 5 minutes, 20 an hour
LOGIN_FAILS_SHORT = Throttle("login-callsign-5min", 5, 300)
LOGIN_FAILS_LONG = Throttle("login-callsign-hour", 20, 3600)
# account creation attempts from one address (each one hashes a password and
# asks the callsign provider and the geocoder)
SIGNUP_PER_ADDRESS = Throttle("signup-address", 10, 600)
# FCC lookups (the sign-up form) from one address
LOOKUP_PER_ADDRESS = Throttle("lookup-address", 30, 60)

ALL = (LOGIN_PER_ADDRESS, LOGIN_FAILS_SHORT, LOGIN_FAILS_LONG, SIGNUP_PER_ADDRESS, LOOKUP_PER_ADDRESS)


def enabled() -> bool:
    return get_settings().rate_limit_enabled


def client_address(request: Request) -> str:
    """The visitor's address: TRUSTED_IP_HEADER when configured and present
    (its first entry — a proxy chain lists the visitor first), else the
    connecting address. Behind a tunnel without the setting, every visitor
    shares the proxy's address and the per-address limits apply site-wide."""
    header = get_settings().trusted_ip_header.strip()
    if header:
        value = request.headers.get(header, "").split(",")[0].strip()
        if value:
            return value[:64]
    return request.client.host if request.client else "unknown"


def too_many(seconds: int) -> HTTPException:
    return HTTPException(429, BUSY_TEXT, headers={"Retry-After": str(max(1, int(seconds)))})


def check(*pairs: tuple[Throttle, str]) -> None:
    """Raise 429 when any (throttle, key) pair is at its limit."""
    if not enabled():
        return
    wait = max((t.retry_after(k) for t, k in pairs), default=0)
    if wait:
        raise too_many(wait)


def hit(*pairs: tuple[Throttle, str]) -> None:
    """Check, then count one event against each pair."""
    check(*pairs)
    if enabled():
        for t, k in pairs:
            t.add(k)


def reset_all() -> None:
    for t in ALL:
        t.reset()


# ---------------------------------------------------------------- one callsign's log in


def login_callsign_pairs(callsign: str) -> tuple[tuple[Throttle, str], ...]:
    """The (throttle, key) pairs that count wrong passwords for a callsign."""
    return ((LOGIN_FAILS_SHORT, callsign), (LOGIN_FAILS_LONG, callsign))


def login_locked(callsign: str) -> bool:
    """Is log in refused for this callsign right now (too many wrong
    passwords)? Never while the throttles are switched off."""
    return enabled() and any(t.retry_after(k) for t, k in login_callsign_pairs(callsign))


def unlock_login(callsign: str) -> None:
    """Forget the wrong passwords counted against a callsign (an admin's
    Unlock log in and password reset), so it can log in again at once."""
    for t, k in login_callsign_pairs(callsign):
        t.clear(k)
