"""POTA and SOTA activator spots (both keyless JSON APIs)."""

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


def query_pota(limit: int = 100) -> list[dict]:
    s = get_settings()
    try:
        with httpx.Client(timeout=s.http_timeout_seconds,
                          headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(s.pota_url)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("pota query failed: %s", exc)
        return []
    out = []
    for r in data[:limit]:
        out.append({
            "activator": r.get("activator", ""),
            "frequency_khz": r.get("frequency"),
            "mode": (r.get("mode") or "").upper(),
            "park_ref": r.get("reference") or r.get("parkReference") or "",
            "park_name": r.get("name") or "",
            "grid": r.get("grid6") or r.get("grid4") or r.get("gridSquare") or "",
            "lat": r.get("latitude"),
            "lon": r.get("longitude"),
            "spotter": r.get("spotter") or "",
            "comments": r.get("comments") or "",
            "time": r.get("spotTime") or "",
            "source": "POTA",
        })
    return out


def query_sota(limit: int = 100) -> list[dict]:
    s = get_settings()
    try:
        with httpx.Client(timeout=s.http_timeout_seconds,
                          headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(s.sota_url)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("sota query failed: %s", exc)
        return []
    out = []
    for r in data[:limit]:
        summit = f"{r.get('associationCode', '')}/{r.get('summitCode', '')}".strip("/")
        out.append({
            "activator": r.get("activatorCallsign", ""),
            "frequency_khz": str(r.get("frequency", "")),
            "mode": (r.get("mode") or "").upper(),
            "summit": summit,
            "summit_name": r.get("summitDetails") or "",
            "lat": r.get("latitude"),
            "lon": r.get("longitude"),
            "spotter": r.get("spotterCallsign") or "",
            "comments": r.get("comments") or "",
            "time": r.get("timeStamp") or "",
            "source": "SOTA",
        })
    return out
