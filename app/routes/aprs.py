"""Live APRS traffic served from the in-memory APRS-IS store
(app/integrations/aprs_feed.py), plus an optional on-demand aprs.fi lookup.

The map hides APRS below zoom 9 (a count only): a North America window holds
15-25k stations and aprs.fi's own "draw everything" view is unreadable there.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.geo import clamp_bbox
from app.integrations import aprs_feed, aprsis
from app.models import User
from app.security import get_current_user

router = APIRouter(prefix="/api/aprs", tags=["aprs"])

_DETAIL_ZOOM = 9  # stations from here; below it only a count
_TRACK_ZOOM = 10  # track tails from here
_WINDOW_DEFAULT_S = 3600  # aprs.fi's default "show last 1 hour"
_WINDOW_MIN_S = 300


def window_seconds(window: int | float | None) -> float:
    """Clamp a requested window to [5 min, retention] instead of a 422."""
    max_s = max(_WINDOW_MIN_S, int(get_settings().aprs_retention_hours * 3600))
    if window is None:
        window = _WINDOW_DEFAULT_S
    return float(max(_WINDOW_MIN_S, min(int(window), max_s)))


def parse_cats(cats: str | None) -> tuple[str, ...]:
    """CSV of categories; missing/blank = all, unknown names ignored."""
    if cats is None or not cats.strip():
        return aprs_feed.CATEGORIES
    wanted = {c.strip().lower() for c in cats.split(",")}
    return tuple(c for c in aprs_feed.CATEGORIES if c in wanted)


@router.get("/map")
def aprs_map(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    zoom: int,
    window: int = _WINDOW_DEFAULT_S,
    cats: str | None = None,
    tracks: bool = True,
    user: User = Depends(get_current_user),
):
    zoom = max(0, min(22, zoom))
    min_lat, max_lat, min_lon, max_lon = clamp_bbox(min_lat, max_lat, min_lon, max_lon)
    window_s = window_seconds(window)
    wanted = parse_cats(cats)
    store = aprs_feed.get_store()
    now = aprs_feed.now()
    if zoom < _DETAIL_ZOOM:
        return {"type": "hidden", "zoom": zoom,
                "total": store.count(min_lat, max_lat, min_lon, max_lon, now=now,
                                     window_s=window_s, cats=wanted)}
    total, stations = store.query(min_lat, max_lat, min_lon, max_lon, now=now, window_s=window_s,
                                  cats=wanted, limit=get_settings().aprs_map_max_results,
                                  tracks=tracks and zoom >= _TRACK_ZOOM)
    return {
        "type": "stations",
        "zoom": zoom,
        "total": total,
        "truncated": total > len(stations),
        "feed": aprs_feed.feed_info(),
        "stations": stations,
    }


@router.get("/station")
def aprs_station(
    key: str = Query(min_length=1, max_length=64),
    window: int = _WINDOW_DEFAULT_S,
    user: User = Depends(get_current_user),
):
    """One station/object with its whole track in the window and its last
    raw packets (newest first)."""
    st = aprs_feed.get_store().station(key, now=aprs_feed.now(), window_s=window_seconds(window))
    if st is None:
        raise HTTPException(404, f"{key} has not been heard recently")
    return st


@router.get("/search")
def aprs_search(
    q: str = Query(default="", max_length=32),
    limit: int = 10,
    user: User = Depends(get_current_user),
):
    limit = max(1, min(50, limit))
    return {"results": aprs_feed.get_store().search(q, now=aprs_feed.now(), limit=limit)}


@router.get("/lookup/{callsign}")
def aprs_lookup(callsign: str, user: User = Depends(get_current_user)):
    """Last position of one callsign from aprs.fi (deployer's API key,
    server-side cache, on demand only), for stations outside our feed area.
    429 when the per-user or server-wide request budget is spent."""
    if not get_settings().aprsfi_api_key:
        raise HTTPException(404, "aprs.fi lookup is not configured on this server")
    name = aprsis.normalize_lookup_name(callsign)
    if name is None:
        raise HTTPException(422, "not a valid APRS name")
    try:
        return aprsis.aprsfi_lookup(name, user_key=f"user:{user.id}")
    except aprsis.AprsFiRateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)})
    except aprsis.AprsFiUnavailable as exc:
        raise HTTPException(503, str(exc))


@router.get("/status")
def aprs_status(user: User = Depends(get_current_user)):
    return aprs_feed.status()
