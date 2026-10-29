"""Activity toolbox: live RF activity from PSK Reporter, WSPR, DX clusters,
CW RBN, APRS-IS, POTA, SOTA, plus solar/band conditions.

All endpoints require auth. HTTP sources are cached briefly; live telnet
feeds are sampled for a few seconds per request and cached per grid/call so
the whole neighborhood doesn't hammer upstream services. APRS comes from the
live APRS-IS store when the feed runs (a 15 s sample saw ~5% of an 8-minute
window), else from a short receive-only sample.

When a source fails (no connection, an error status, an unreadable reply)
the endpoint answers 502 with a readable `detail`, and nothing is cached, so
the next request asks again; an empty list means the source answered with
nothing.
"""

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.geo import grid_to_latlon, haversine_miles
from app.integrations import aprs_feed, aprsis, bandcond, pota_sota, pskreporter, telnetfeed, wsprlive
from app.integrations.common import UpstreamError, activity_cache
from app.models import User
from app.routes.aprs import window_seconds
from app.security import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/activity", tags=["activity"])

_KM_PER_MILE = 1.609344
# Everyone in a 0.1-degree cell shares one APRS sample, taken around the
# cell's centre; a cell's half-diagonal is at most 7.9 km, so radius + 8 km
# around the centre covers each sharer's own circle.
_SAMPLE_CELL_MARGIN_KM = 8


def _did_not_answer(source: str, reason) -> HTTPException:
    log.warning("[activity] %s failed: %s", source, reason)
    return HTTPException(502, f"{source} did not answer. Try again in a minute.")


def _answered(source: str, fetch: Callable):
    """fetch(), or a 502 "<source> did not answer" when the upstream failed
    (UpstreamError: activity_cache.wrap has stored nothing for it)."""
    try:
        return fetch()
    except UpstreamError as exc:
        raise _did_not_answer(source, exc) from None


def _dist(user: User, lat, lon):
    if user.lat is None or user.lon is None or lat is None or lon is None:
        return None
    try:
        return round(haversine_miles(user.lat, user.lon, float(lat), float(lon)), 1)
    except (TypeError, ValueError):
        return None


def _dist_grid(user: User, grid: str):
    ll = grid_to_latlon(grid) if grid else None
    return _dist(user, *ll) if ll else None


@router.get("/pskreporter")
def pskreporter_spots(
    direction: str = Query(default="sent", pattern="^(sent|received)$"),
    user: User = Depends(get_current_user),
):
    """PSK Reporter reception reports. `sent` = stations hearing ME,
    `received` = stations I am hearing (by my callsign's receiver reports)."""
    ttl = get_settings().activity_cache_seconds
    key = f"psk:{direction}:{user.callsign}"

    def produce():
        if direction == "sent":
            spots = pskreporter.query_pskreporter(sender=user.callsign)
        else:
            spots = pskreporter.query_pskreporter(receiver=user.callsign)
        for s in spots:
            s["distance_miles"] = _dist_grid(user, s["receiver_grid"] if direction == "sent" else s["sender_grid"])
        return spots

    spots = _answered("PSK Reporter", lambda: activity_cache.wrap(key, ttl, produce))
    return {"source": "pskreporter.info", "direction": direction, "spots": spots}


@router.get("/wspr")
def wspr_spots(user: User = Depends(get_current_user)):
    ttl = get_settings().activity_cache_seconds

    def produce():
        spots = wsprlive.query_wspr(user.callsign)
        for s in spots:
            # distance to the "other end" of each report
            if s["tx_callsign"] == user.callsign:
                s["distance_miles"] = _dist(user, s["rx_lat"], s["rx_lon"])
            else:
                s["distance_miles"] = _dist(user, s["tx_lat"], s["tx_lon"])
        return spots

    spots = _answered("wspr.live", lambda: activity_cache.wrap(f"wspr:{user.callsign}", ttl, produce))
    return {"source": "wspr.live", "spots": spots}


@router.get("/dxcluster")
def dxcluster_spots(user: User = Depends(get_current_user)):
    """DX cluster spots, newest first: the node's latest (SH/DX) and those it
    sends during a few seconds' live sample. `source` is the node that
    answered (DXC_HOST, or a DXC_FALLBACKS node when that one sends no log-in
    prompt)."""
    s = get_settings()
    ttl = max(30.0, s.activity_sample_seconds)
    spots = _answered("The DX cluster",
                      lambda: activity_cache.wrap("dxc", ttl, lambda: telnetfeed.sample_dxcluster(user.callsign)))
    return {"source": telnetfeed.dxc_source(), "sample_seconds": s.activity_sample_seconds, "spots": spots}


@router.get("/rbn")
def rbn_spots(
    mine_only: bool = Query(default=False),
    user: User = Depends(get_current_user),
):
    """CW Reverse Beacon Network spots sampled live. With mine_only=true,
    only reports of YOUR signal heard by the skimmer network are returned."""
    s = get_settings()
    ttl = max(30.0, s.activity_sample_seconds)
    spots = _answered("The Reverse Beacon Network",
                      lambda: activity_cache.wrap("rbn", ttl, lambda: telnetfeed.sample_rbn(user.callsign)))
    if mine_only:
        spots = [sp for sp in spots if sp["dx_callsign"] == user.callsign]
    return {"source": f"{s.rbn_host}:{s.rbn_port}", "sample_seconds": s.activity_sample_seconds, "spots": spots}


def _aprs_row(station: dict, dist_km: float) -> dict:
    """Station JSON (see /api/aprs/map) plus the Activity tab's original keys."""
    row = dict(station)
    row.pop("distance_km", None)
    row.update(
        callsign=station["from"],
        object=station["call"] if station["kind"] != "station" else "",
        distance_miles=round(dist_km / _KM_PER_MILE, 1),
    )
    return row


def _nearest_rows(sample: list[dict], lat: float, lon: float, radius_km: float,
                  limit: int) -> tuple[int, list[dict]]:
    """(stations of a shared sample within radius_km of this user, their
    rows nearest first, capped at limit). Distances, the radius cut and the
    order are the asker's own; the cached sample is never modified."""
    hits = []
    for st in sample or ():
        d = aprs_feed._haversine_km(lat, lon, st["lat"], st["lon"])
        if d <= radius_km:
            hits.append((d, st["key"], st))
    hits.sort(key=lambda h: (h[0], h[1]))
    return len(hits), [_aprs_row(st, d) for d, _, st in hits[:limit]]


@router.get("/aprs")
def aprs_stations(
    radius_km: int = Query(default=150, ge=10, le=2000),
    window: int = 3600,
    user: User = Depends(get_current_user),
):
    """APRS stations near you, nearest first: from the live APRS-IS store when
    the feed runs, else a short receive-only sample (pass -1, server login).
    502 when APRS-IS failed: the sample could not be taken, or the live feed
    is disconnected and its store holds nothing for you in the window (an
    empty answer would claim nothing was heard)."""
    if user.lat is None or user.lon is None:
        raise HTTPException(400, "set your location (grid square or address) in your profile first")
    s = get_settings()
    if aprs_feed.is_running():
        window_s = window_seconds(window)
        total, near = aprs_feed.get_store().near(user.lat, user.lon, radius_km, now=aprs_feed.now(),
                                                 window_s=window_s, limit=s.activity_max_spots)
        if total == 0 and not aprs_feed.is_connected():
            raise _did_not_answer("APRS-IS", "the live feed is disconnected and holds nothing here")
        return {
            "source": f"{s.aprs_host}:{s.aprs_port}",
            "mode": "feed",
            # the store's window is the live sample here; today's Activity tab
            # prints "(<sample_seconds>s live sample)", so never null
            "sample_seconds": int(window_s),
            "window_s": int(window_s),
            "radius_km": radius_km,
            "total": total,
            "stations": [_aprs_row(st, d) for d, st in near],
        }

    ttl = max(30.0, s.activity_sample_seconds)
    clat, clon = round(user.lat, 1), round(user.lon, 1)
    key = f"aprs:{clat}:{clon}:{radius_km}"
    # one receive-only sample per cell (not per user: each sample is an
    # APRS-IS connection), centred on the cell, not on whoever asked first
    sample = _answered("APRS-IS", lambda: activity_cache.wrap(
        key, ttl, lambda: aprsis.sample_aprs(clat, clon, radius_km + _SAMPLE_CELL_MARGIN_KM)))
    total, stations = _nearest_rows(sample, user.lat, user.lon, radius_km, s.activity_max_spots)
    return {
        "source": f"{s.aprs_host}:{s.aprs_port}",
        "mode": "sample",
        "sample_seconds": s.activity_sample_seconds,
        "window_s": None,
        "radius_km": radius_km,
        "total": total,
        "stations": stations,
    }


@router.get("/pota")
def pota_spots(user: User = Depends(get_current_user)):
    ttl = get_settings().activity_cache_seconds
    spots = _answered("pota.app", lambda: activity_cache.wrap(
        "pota", ttl, lambda: pota_sota.query_pota(get_settings().activity_max_spots)))
    out = []
    for sp in spots or ():
        # the cached spots are shared by every user: distances go on copies
        d = _dist(user, sp.get("lat"), sp.get("lon"))
        out.append(dict(sp, distance_miles=d if d is not None else _dist_grid(user, sp.get("grid"))))
    return {"source": "pota.app", "spots": out}


@router.get("/sota")
def sota_spots(user: User = Depends(get_current_user)):
    ttl = get_settings().activity_cache_seconds
    spots = _answered("SOTAwatch", lambda: activity_cache.wrap(
        "sota", ttl, lambda: pota_sota.query_sota(get_settings().activity_max_spots)))
    # shared cache: each user's distances go on copies
    return {"source": "sota.org.uk",
            "spots": [dict(sp, distance_miles=_dist(user, sp.get("lat"), sp.get("lon"))) for sp in spots or ()]}


@router.get("/bands")
def band_conditions(user: User = Depends(get_current_user)):
    """Solar indices and HF band conditions (hamqsl.com)."""
    ttl = max(get_settings().activity_cache_seconds, 900)  # feed updates ~15 min
    data = activity_cache.wrap("bands", ttl, bandcond.query_band_conditions)
    if data is None:
        raise HTTPException(502, "band condition feed unavailable right now")
    return data
