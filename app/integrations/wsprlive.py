"""WSPR spots via wspr.live (ClickHouse HTTP JSON interface — no key needed).

ClickHouse's JSON format quotes 64-bit integers ("frequency": "14097081"),
so numbers are coerced here; tx/rx coordinates feed the map's spot layers.
"""

import logging

import httpx

from app.config import get_settings
from app.integrations.common import UpstreamError

log = logging.getLogger(__name__)

QUERY = (
    "SELECT time, band, tx_sign, tx_lat, tx_lon, rx_sign, rx_lat, rx_lon, "
    "distance, frequency, power, snr FROM wspr.rx "
    "WHERE (tx_sign = '{cs}' OR rx_sign = '{cs}') "
    "AND time > subtractHours(now(), 24) "
    "ORDER BY time DESC LIMIT {limit} FORMAT JSON"
)


def _num(value, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _coord(value, limit: float) -> float | None:
    v = _num(value)
    return v if v is not None and -limit <= v <= limit else None


def query_wspr(callsign: str, limit: int = 100) -> list[dict]:
    """Spots of `callsign` (as TX or RX) in the last 24 hours. Raises
    UpstreamError when wspr.live fails (network, HTTP error status, a reply
    without ClickHouse's `data` list); [] means it answered with no spots."""
    cs = "".join(ch for ch in callsign.upper() if ch.isalnum() or ch == "/")
    if not cs:
        return []
    s = get_settings()
    sql = QUERY.format(cs=cs, limit=int(limit))
    try:
        with httpx.Client(timeout=s.http_timeout_seconds + 4,
                          headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(s.wspr_live_url, params={"query": sql})
    except httpx.HTTPError as exc:
        log.warning("[activity] wspr.live query failed: %s", exc)
        raise UpstreamError(f"wspr.live: {type(exc).__name__}") from None
    if resp.status_code != 200:
        log.warning("[activity] wspr.live returned %s", resp.status_code)
        raise UpstreamError(f"wspr.live: HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        data = None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        log.warning("[activity] wspr.live reply has no data list: %r", resp.text[:120])
        raise UpstreamError("wspr.live: unreadable reply")
    return parse_rows(rows)


def parse_rows(rows: list) -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "time": r.get("time", ""),
            "band_m": r.get("band"),
            "tx_callsign": r.get("tx_sign", ""),
            "tx_lat": _coord(r.get("tx_lat"), 90.0),
            "tx_lon": _coord(r.get("tx_lon"), 180.0),
            "rx_callsign": r.get("rx_sign", ""),
            "rx_lat": _coord(r.get("rx_lat"), 90.0),
            "rx_lon": _coord(r.get("rx_lon"), 180.0),
            "distance_km": _num(r.get("distance"), int),
            "frequency_hz": _num(r.get("frequency"), int),
            "power_dbm": _num(r.get("power"), int),
            "snr": _num(r.get("snr"), int),
        })
    return out
