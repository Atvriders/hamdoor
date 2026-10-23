"""callook.info callsign lookup (free, no API key, US callsigns).

Response shape (https://callook.info/{call}/json):
{
  "status": "VALID",
  "type": "PERSON",
  "current": {"callsign": "W1AW", "operClass": "E"},
  "name": "Hiram Percy Maxim Memorial Station",
  "address": {"line1": "225 Main St", "line2": "Newington, CT 06111", ...},
  "location": {"latitude": "41.714707", "longitude": "-72.728411", "gridsquare": "FN31pr"},
  "otherInfo": {"expiryDate": "...", ...}
}
"""

import logging
import threading
import time

import httpx

from app.config import get_settings
from app.integrations.provider import CallsignProvider, CallsignRecord

log = logging.getLogger(__name__)

# answers callook gave (a record, or "no such callsign") are reused for a
# while: the sign-up form looks a callsign up and then signs it up, and an
# anonymous caller must not be able to make hamdoor hammer callook.info.
# Failed requests are never cached.
CACHE_SECONDS = 600
CACHE_MAX = 2000
_cache: dict[str, tuple[float, CallsignRecord | None]] = {}
_cache_lock = threading.Lock()


def _cached(callsign: str) -> tuple[bool, CallsignRecord | None]:
    with _cache_lock:
        hit = _cache.get(callsign)
        if hit is None:
            return False, None
        if hit[0] < time.monotonic():
            del _cache[callsign]
            return False, None
        return True, hit[1]


def _remember(callsign: str, record: CallsignRecord | None) -> None:
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            now = time.monotonic()
            for k in [k for k, v in _cache.items() if v[0] < now]:
                del _cache[k]
            while len(_cache) >= CACHE_MAX:
                _cache.pop(next(iter(_cache)))
        _cache[callsign] = (time.monotonic() + CACHE_SECONDS, record)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


class CallookProvider(CallsignProvider):
    name = "callook.info"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def lookup(self, callsign: str) -> CallsignRecord | None:
        key = callsign.upper()
        if self._client is None:
            known, record = _cached(key)
            if known:
                return record
        record, definitive = self._fetch(callsign)
        if definitive and self._client is None:
            _remember(key, record)
        return record

    def _fetch(self, callsign: str) -> tuple[CallsignRecord | None, bool]:
        """(record or None, whether callook really answered)."""
        s = get_settings()
        url = f"{s.callook_base_url}/{callsign}/json"
        try:
            if self._client is not None:
                resp = self._client.get(url)
            else:
                with httpx.Client(
                    timeout=s.http_timeout_seconds,
                    headers={"User-Agent": s.http_user_agent},
                ) as client:
                    resp = client.get(url)
        except httpx.HTTPError as exc:
            log.warning("callook lookup failed for %s: %s", callsign, exc)
            return None, False
        if resp.status_code != 200:
            return None, False
        try:
            data = resp.json()
        except ValueError:
            return None, False
        if data.get("status") != "VALID":
            return None, True
        return self._parse(callsign, data), True

    @staticmethod
    def _parse(callsign: str, data: dict) -> CallsignRecord:
        addr = data.get("address") or {}
        loc = data.get("location") or {}
        current = data.get("current") or {}
        other = data.get("otherInfo") or {}

        # line2 is typically "City, ST ZIP"
        city, state, zip_ = "", "", ""
        line2 = addr.get("line2", "")
        if "," in line2:
            city, _, rest = line2.partition(",")
            parts = rest.strip().split()
            if parts:
                state = parts[0]
                zip_ = parts[1] if len(parts) > 1 else ""

        lat, lon = _coord(loc.get("latitude"), 90), _coord(loc.get("longitude"), 180)
        if lat is None or lon is None:
            lat = lon = None

        return CallsignRecord(
            callsign=callsign.upper(),
            name=(data.get("name") or "").strip(),
            address_line=(addr.get("line1") or "").strip(),
            city=city.strip(),
            state=state.strip(),
            zip=zip_.strip(),
            grid=(loc.get("gridsquare") or "").strip(),
            license_class=(current.get("operClass") or "").strip(),
            expires=(other.get("expiryDate") or "").strip(),
            source=CallookProvider.name,
            lat=lat,
            lon=lon,
        )


def _coord(value, limit: float) -> float | None:
    """callook sends coordinates as strings ("41.714707"), "" when unknown."""
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if v != v or abs(v) > limit or v == 0.0:
        return None
    return v
