"""Fallback geocoding via Nominatim (OpenStreetMap).

Only used when a callsign record has no grid square. Be polite: fixed
User-Agent, single request, short timeout. Results are best-effort.
"""

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


def geocode_address(address_line: str, city: str, state: str, zip_: str) -> tuple[float, float] | None:
    query = ", ".join(p for p in (address_line, city, state, zip_) if p)
    if not query:
        return None
    s = get_settings()
    try:
        with httpx.Client(
            timeout=s.http_timeout_seconds,
            headers={"User-Agent": s.http_user_agent},
        ) as client:
            resp = client.get(
                s.nominatim_url,
                params={"q": query, "format": "json", "limit": "1", "countrycodes": "us"},
            )
        if resp.status_code != 200:
            return None
        results = resp.json()
        if not results:
            return None
        return (float(results[0]["lat"]), float(results[0]["lon"]))
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        log.warning("geocode failed for %r: %s", query, exc)
        return None


def resolve_location(grid: str, address_line: str, city: str, state: str, zip_: str) -> tuple[float, float] | None:
    """Grid square first (no network), address geocode as fallback."""
    from app.geo import grid_to_latlon

    if grid:
        ll = grid_to_latlon(grid)
        if ll:
            return ll
    return geocode_address(address_line, city, state, zip_)


def resolve_user_location(db, callsign: str, grid: str, address_line: str,
                          city: str, state: str, zip_: str) -> tuple[float, float] | None:
    """Best location for a registered user.

    Prefers the local FCC street-level geocode (the same pin the directory
    map shows), then grid square, then Nominatim, then the ZIP centroid.
    """
    from app.models import Ham

    ham = db.get(Ham, callsign) if callsign else None
    if ham is not None and ham.lat is not None and ham.loc_source == "address":
        return (ham.lat, ham.lon)
    ll = resolve_location(grid, address_line, city, state, zip_)
    if ll:
        return ll
    if ham is not None and ham.lat is not None:
        return (ham.lat, ham.lon)
    return None
