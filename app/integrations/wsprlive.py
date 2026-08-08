"""WSPR spots via wspr.live (ClickHouse HTTP JSON interface — no key needed)."""

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

QUERY = (
    "SELECT time, band, tx_sign, tx_lat, tx_lon, rx_sign, rx_lat, rx_lon, "
    "distance, frequency, power, snr FROM wspr.rx "
    "WHERE (tx_sign = '{cs}' OR rx_sign = '{cs}') "
    "AND time > subtractHours(now(), 24) "
    "ORDER BY time DESC LIMIT {limit} FORMAT JSON"
)


def query_wspr(callsign: str, limit: int = 100) -> list[dict]:
    cs = "".join(ch for ch in callsign.upper() if ch.isalnum() or ch == "/")
    if not cs:
        return []
    s = get_settings()
    sql = QUERY.format(cs=cs, limit=int(limit))
    try:
        with httpx.Client(timeout=s.http_timeout_seconds + 4,
                          headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(s.wspr_live_url, params={"query": sql})
        if resp.status_code != 200:
            log.warning("wspr.live returned %s", resp.status_code)
            return []
        rows = resp.json().get("data", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("wspr.live query failed: %s", exc)
        return []
    out = []
    for r in rows:
        out.append({
            "time": r.get("time", ""),
            "band_m": r.get("band"),
            "tx_callsign": r.get("tx_sign", ""),
            "tx_lat": r.get("tx_lat"),
            "tx_lon": r.get("tx_lon"),
            "rx_callsign": r.get("rx_sign", ""),
            "rx_lat": r.get("rx_lat"),
            "rx_lon": r.get("rx_lon"),
            "distance_km": r.get("distance"),
            "frequency_hz": r.get("frequency"),
            "power_dbm": r.get("power"),
            "snr": r.get("snr"),
        })
    return out
