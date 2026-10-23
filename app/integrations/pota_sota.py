"""POTA and SOTA activator spots (both keyless JSON APIs).

Field names checked against the live APIs (2026-10-20): POTA spots carry
`latitude`/`longitude` (park), `name` (park name), `grid4`/`grid6` and a
kHz string `frequency`; SOTA spots carry `latitude`/`longitude` (summit),
`summitCode` already prefixed with the association ("W7W/CW-085"),
`summitName`, `frequency` as a float in MHz (0.0 = none given),
`activatorCallsign`, and `callsign` = the SPOTTER (there is no
`spotterCallsign`; self-spots repeat the activator, chasers and RBNHOLE
do not).
"""

import logging
import math

import httpx

from app.config import get_settings
from app.integrations.common import UpstreamError

log = logging.getLogger(__name__)


def _coord(value, limit: float) -> float | None:
    """A usable coordinate or None (missing, non-numeric, 0 = unknown)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v == 0 or not -limit <= v <= limit:
        return None
    return round(v, 6)


def _latlon(r: dict) -> tuple[float | None, float | None]:
    lat, lon = _coord(r.get("latitude"), 90.0), _coord(r.get("longitude"), 180.0)
    if lat is None or lon is None:
        return None, None
    return lat, lon


def _khz_from_mhz(value) -> str:
    """SOTA reports MHz (14.286); the API's frequency_khz field is kHz text in
    fixed point to the hertz: "%g" printed 1296.2 MHz as "1.2962e+06" and
    rounded 432.0655 MHz to "432066". 0, negative, NaN, inf or above 1 THz:
    no frequency ("")."""
    try:
        mhz = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(mhz) or not 0.0 < mhz <= 1_000_000.0:
        return ""
    text = f"{mhz * 1000:.3f}".rstrip("0")
    return text[:-1] if text.endswith(".") else text


def _fetch(url: str, label: str) -> list:
    """The spot list a keyless JSON API returns. Raises UpstreamError when it
    fails (network, HTTP error status, a reply that isn't a JSON list)."""
    s = get_settings()
    try:
        with httpx.Client(timeout=s.http_timeout_seconds,
                          headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        log.warning("[activity] %s query failed: %s", label, exc)
        raise UpstreamError(f"{label}: {type(exc).__name__}") from None
    if resp.status_code != 200:
        log.warning("[activity] %s returned %s", label, resp.status_code)
        raise UpstreamError(f"{label}: HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, list):
        log.warning("[activity] %s reply is not a JSON list: %r", label, resp.text[:120])
        raise UpstreamError(f"{label}: unreadable reply")
    return data


def parse_pota(data: list, limit: int = 100) -> list[dict]:
    out = []
    for r in data[:limit]:
        if not isinstance(r, dict):
            continue
        lat, lon = _latlon(r)
        out.append({
            "activator": r.get("activator", ""),
            "frequency_khz": r.get("frequency"),
            "mode": (r.get("mode") or "").upper(),
            "park_ref": r.get("reference") or r.get("parkReference") or "",
            "park_name": r.get("name") or r.get("parkName") or "",
            "grid": r.get("grid6") or r.get("grid4") or r.get("gridSquare") or "",
            "lat": lat,
            "lon": lon,
            "spotter": r.get("spotter") or "",
            "comments": r.get("comments") or "",
            "time": r.get("spotTime") or "",
            "source": "POTA",
        })
    return out


def parse_sota(data: list, limit: int = 100) -> list[dict]:
    out = []
    for r in data[:limit]:
        if not isinstance(r, dict):
            continue
        code = r.get("summitCode") or ""
        assoc = r.get("associationCode") or ""
        summit = code if "/" in code or not assoc else f"{assoc}/{code}"
        lat, lon = _latlon(r)
        out.append({
            "activator": r.get("activatorCallsign") or "",  # never `callsign`: that is the spotter
            "frequency_khz": _khz_from_mhz(r.get("frequency")),
            "mode": (r.get("mode") or "").upper(),
            "summit": summit,
            "summit_name": r.get("summitName") or r.get("summitDetails") or "",
            "lat": lat,
            "lon": lon,
            "spotter": r.get("callsign") or r.get("spotterCallsign") or "",
            "comments": r.get("comments") or "",
            "time": r.get("timeStamp") or "",
            "source": "SOTA",
        })
    return out


def query_pota(limit: int = 100) -> list[dict]:
    """Current POTA activator spots; UpstreamError when pota.app fails."""
    return parse_pota(_fetch(get_settings().pota_url, "pota"), limit)


def query_sota(limit: int = 100) -> list[dict]:
    """SOTAwatch spots of the last 2 hours; UpstreamError when it fails."""
    return parse_sota(_fetch(get_settings().sota_url, "sota"), limit)
