"""APRS network clients besides the live feed:

- a short one-off APRS-IS sample around a point, used by the Activity tab
  only when the live feed (aprs_feed) is disabled;
- an on-demand aprs.fi position lookup for one callsign (deployer's API key).

Both are receive-only. hamdoor never computes or sends a user's APRS-IS
passcode: a verified login makes aprsc disconnect any other verified client
with the same name (the user's own igate or phone app), and passcodes are
handed out by software authors on request only. We use `pass -1` and the
server's own login (aprs_feed.get_login()).
"""

import logging
import math
import re
import socket
import threading
import time
from collections import OrderedDict

import httpx

from app.config import get_settings
from app.integrations import aprs_feed, aprs_parser
from app.integrations.common import UpstreamError

log = logging.getLogger(__name__)

APRSFI_API_URL = "https://api.aprs.fi/api/get"
APRSFI_CACHE_SECONDS = 60.0  # aprs.fi: cache and share answers, never poll
APRSFI_CACHE_MAX = 1000  # names remembered at once (least recently used go first)
# Request budget. aprs.fi rate-limits per key "to protect the database
# contents from harvesting", and the key is the deployer's: a person looks up
# a handful of names, a script would look up thousands.
APRSFI_GLOBAL_BURST = 10  # the whole server: 10 calls back to back...
APRSFI_GLOBAL_REFILL_S = 3.0  # ...then one every 3 s
APRSFI_USER_BURST = 5  # one user: 5 back to back, then one every 12 s, so
APRSFI_USER_REFILL_S = 12.0  # nobody can spend everyone's budget
_APRSFI_BACKOFF_BASE_S = 30.0
_APRSFI_BACKOFF_MAX_S = 1800.0
_SAMPLE_WINDOW_S = 3600.0


# ---------------------------------------------------------------- one-off sample

def sample_aprs(lat: float, lon: float, radius_km: int) -> list[dict]:
    """Collect APRS-IS traffic within radius_km of (lat, lon) for
    activity_sample_seconds; returns station JSON (aprs_feed format) plus
    "distance_km", nearest first.

    Raises UpstreamError when APRS-IS failed us without a station: no
    connection, a refused login, a connection error, the server hanging up
    before the sample ended, or not one byte received. [] means the server
    stayed connected and nothing was heard nearby."""
    s = get_settings()
    store = aprs_feed.StationStore(retention_s=_SAMPLE_WINDOW_S)
    line = aprs_feed.login_line(aprs_feed.get_login(), f"r/{lat:.3f}/{lon:.3f}/{int(radius_km)}")
    deadline = time.monotonic() + s.activity_sample_seconds
    received = hung_up = failed = False
    try:
        with aprs_feed._open_connection(s.aprs_host, s.aprs_port, 8.0) as sock:
            sock.settimeout(2.0)
            sock.sendall(line)
            buf = b""
            while time.monotonic() < deadline and store.positioned_count() < s.activity_max_spots:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    hung_up = True
                    break
                received = True
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop()
                for raw in lines:
                    if raw[:1] == b"#":
                        low = raw.lower()
                        if b"not allowed" in low or b"invalid" in low:
                            log.warning("[aprs] sample login refused: %s", raw[:120].decode("latin-1"))
                            raise UpstreamError("APRS-IS refused the sample login")
                        continue
                    pkt = aprs_parser.parse_line(raw)
                    if pkt is not None:
                        store.ingest(pkt, aprs_feed.now())
    except OSError as exc:
        log.warning("[aprs] APRS-IS sample failed: %s", exc)
        failed = True
    out = []
    _, near = store.near(lat, lon, radius_km, now=aprs_feed.now(), window_s=_SAMPLE_WINDOW_S,
                         limit=s.activity_max_spots)
    for dist_km, st in near:
        st["distance_km"] = round(dist_km, 1)
        out.append(st)
    if not out and (failed or hung_up or not received):
        raise UpstreamError("APRS-IS sample: " + ("connection failed" if failed else
                                                  "server hung up" if hung_up else "nothing received"))
    return out


# ---------------------------------------------------------------- aprs.fi lookup

class AprsFiUnavailable(Exception):
    """aprs.fi failed or we are backing off after repeated failures."""


class AprsFiRateLimited(AprsFiUnavailable):
    """Our own request budget is spent (HTTP 429); aprs.fi was not asked."""

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class _Budget:
    """Token bucket: `burst` calls back to back, then one per `refill_s`.
    Callers hold _budget_lock."""

    def __init__(self, burst: int, refill_s: float):
        self.burst, self.refill_s = float(burst), refill_s
        self.tokens, self.at = float(burst), None

    def _refill(self, now: float) -> None:
        if self.at is not None:
            self.tokens = min(self.burst, self.tokens + max(0.0, now - self.at) / self.refill_s)
        self.at = now

    def take(self, now: float) -> bool:
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def give_back(self) -> None:
        self.tokens = min(self.burst, self.tokens + 1.0)

    def wait_s(self) -> int:
        """Whole seconds until the next token (for Retry-After)."""
        return max(1, math.ceil((1.0 - self.tokens) * self.refill_s))


class _LookupCache:
    """Shared answers for APRSFI_CACHE_SECONDS. Expired entries are dropped on
    every set and at most `max_entries` names are kept (least recently used
    go first): common.TTLCache only forgets a name when it is asked again,
    so every distinct name would stay for the life of the process."""

    def __init__(self, max_entries: int = APRSFI_CACHE_MAX):
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str) -> dict | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if hit[0] <= time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return hit[1]

    def set(self, key: str, value: dict, ttl: float) -> None:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, (expires, _) in self._data.items() if expires <= now]:
                del self._data[k]
            self._data[key] = (now + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)


_lookup_cache = _LookupCache()
_backoff_lock = threading.Lock()
_failures = 0
_paused_until = 0.0
_budget_lock = threading.Lock()
_global_budget = _Budget(APRSFI_GLOBAL_BURST, APRSFI_GLOBAL_REFILL_S)
_user_budgets: dict[str, _Budget] = {}  # one per account: bounded by the users table
_inflight_lock = threading.Lock()
_inflight: dict[str, list] = {}  # name -> [lock, askers]: one upstream call per name at a time
_CALL_RE = re.compile(r"^[A-Za-z0-9]{1,6}(-[A-Za-z0-9]{1,2})?$")
_NAME_RE = re.compile(r"^[\x20-\x7e]{1,9}$")


def normalize_lookup_name(name: str) -> str | None:
    """One APRS name (callsign, object or item; at most 9 characters).
    No commas or wildcards: one target per request, no searching."""
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name) or "," in name or "*" in name:
        return None
    return name.upper() if _CALL_RE.match(name) else name


def _scrub(text: str) -> str:
    key = get_settings().aprsfi_api_key
    return text.replace(key, "***") if key else text


def _record_failure() -> None:
    """aprs.fi: "If multiple requests fail in a row ... slow down the rate of
    attempts using an exponential backoff algorithm"."""
    global _failures, _paused_until
    with _backoff_lock:
        _failures += 1
        if _failures >= 2:
            # bounded exponent: 30.0 * 2**1024 overflows a float (the 1,026th
            # failure in a row, ~3 weeks of a revoked key), which turned every
            # failure into a 500 and stopped the backoff for good
            pause = min(_APRSFI_BACKOFF_MAX_S, _APRSFI_BACKOFF_BASE_S * 2 ** min(_failures - 2, 16))
            _paused_until = time.monotonic() + pause


def _record_success() -> None:
    global _failures, _paused_until
    with _backoff_lock:
        _failures, _paused_until = 0, 0.0


def _spend_budget(user_key: str | None) -> None:
    """One upstream call from this user's budget and the server's, or
    AprsFiRateLimited (nothing is spent then)."""
    now = time.monotonic()
    with _budget_lock:
        user = None
        if user_key is not None:
            user = _user_budgets.get(user_key)
            if user is None:
                user = _user_budgets[user_key] = _Budget(APRSFI_USER_BURST, APRSFI_USER_REFILL_S)
            if not user.take(now):
                raise AprsFiRateLimited("too many aprs.fi lookups; try again shortly", user.wait_s())
        if not _global_budget.take(now):
            if user is not None:
                user.give_back()
            raise AprsFiRateLimited("this server's aprs.fi lookups are busy; try again shortly",
                                    _global_budget.wait_s())


def aprsfi_lookup(name: str, user_key: str | None = None) -> dict:
    """Last known position of `name` from the aprs.fi API (what=loc), cached
    for APRSFI_CACHE_SECONDS and shared by every user. Cache misses spend the
    asking user's and the server's request budget; concurrent askers for one
    name share a single upstream call."""
    cached = _lookup_cache.get(name)
    if cached is not None:
        return cached
    with _inflight_lock:
        slot = _inflight.get(name)
        if slot is None:
            slot = _inflight[name] = [threading.Lock(), 0]
        slot[1] += 1
    try:
        with slot[0]:
            cached = _lookup_cache.get(name)  # answered while we waited
            if cached is not None:
                return cached
            return _fetch(name, user_key)
    finally:
        with _inflight_lock:
            slot[1] -= 1
            if slot[1] == 0 and _inflight.get(name) is slot:
                del _inflight[name]


def _fetch(name: str, user_key: str | None) -> dict:
    with _backoff_lock:
        paused = time.monotonic() < _paused_until
    if paused:
        raise AprsFiUnavailable("aprs.fi lookups are paused after repeated failures; try again later")
    _spend_budget(user_key)
    s = get_settings()
    params = {"name": name, "what": "loc", "apikey": s.aprsfi_api_key, "format": "json"}
    try:
        # aprs.fi terms: the User-Agent names the app, its version and home page
        with httpx.Client(timeout=s.http_timeout_seconds, headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(APRSFI_API_URL, params=params)
        if resp.status_code != 200:
            raise AprsFiUnavailable(f"aprs.fi answered HTTP {resp.status_code}")
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _record_failure()
        log.warning("[aprs] aprs.fi lookup failed: %s", _scrub(f"{type(exc).__name__}: {exc}"))
        raise AprsFiUnavailable("aprs.fi is not answering right now") from None
    except AprsFiUnavailable:
        _record_failure()
        raise
    if not isinstance(data, dict) or data.get("result") != "ok":
        _record_failure()
        desc = data.get("description") if isinstance(data, dict) else None
        log.warning("[aprs] aprs.fi lookup error: %s", _scrub(str(desc)))
        raise AprsFiUnavailable(f"aprs.fi error: {_scrub(str(desc or 'unknown'))}")
    _record_success()
    entries = data.get("entries") or []
    entry = entries[0] if entries and isinstance(entries[0], dict) else None
    result = {"found": False, "call": name, "lat": None, "lon": None, "last": None, "comment": "",
              "aprsfi_url": aprs_feed.aprsfi_url(name), "source": "aprs.fi"}
    if entry is not None:
        try:
            lat, lon = round(float(entry["lat"]), 6), round(float(entry["lng"]), 6)
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):  # also NaN/inf: JSON has neither
                raise ValueError("position out of range")
            result.update(found=True,
                          call=str(entry.get("name") or name),
                          lat=lat,
                          lon=lon,
                          last=int(entry.get("lasttime") or entry.get("time") or 0) or None,
                          comment=str(entry.get("comment") or ""))
            result["aprsfi_url"] = aprs_feed.aprsfi_url(result["call"])
        except (KeyError, TypeError, ValueError, OverflowError):
            result.update(found=False, lat=None, lon=None)
    _lookup_cache.set(name, result, APRSFI_CACHE_SECONDS)
    return result
