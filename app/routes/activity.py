"""Activity toolbox: live RF activity from PSK Reporter, WSPR, DX clusters,
CW RBN, APRS-IS, POTA, SOTA, plus solar/band conditions.

All endpoints require auth. HTTP sources are cached briefly; live telnet/APRS
feeds are sampled for a few seconds per request and cached per grid/call so
the whole neighborhood doesn't hammer upstream services.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.geo import grid_to_latlon, haversine_miles
from app.integrations import aprsis, bandcond, pota_sota, pskreporter, telnetfeed, wsprlive
from app.integrations.common import activity_cache
from app.models import User
from app.security import get_current_user

router = APIRouter(prefix="/api/activity", tags=["activity"])


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

    return {"source": "pskreporter.info", "direction": direction, "spots": activity_cache.wrap(key, ttl, produce)}


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

    return {"source": "wspr.live", "spots": activity_cache.wrap(f"wspr:{user.callsign}", ttl, produce)}


@router.get("/dxcluster")
def dxcluster_spots(user: User = Depends(get_current_user)):
    """Live DX cluster spots sampled for a few seconds."""
    s = get_settings()
    ttl = max(30.0, s.activity_sample_seconds)
    spots = activity_cache.wrap("dxc", ttl, lambda: telnetfeed.sample_dxcluster(user.callsign))
    return {"source": f"{s.dxc_host}:{s.dxc_port}", "sample_seconds": s.activity_sample_seconds, "spots": spots}


@router.get("/rbn")
def rbn_spots(
    mine_only: bool = Query(default=False),
    user: User = Depends(get_current_user),
):
    """CW Reverse Beacon Network spots sampled live. With mine_only=true,
    only reports of YOUR signal heard by the skimmer network are returned."""
    s = get_settings()
    ttl = max(30.0, s.activity_sample_seconds)
    spots = activity_cache.wrap("rbn", ttl, lambda: telnetfeed.sample_rbn(user.callsign))
    if mine_only:
        spots = [sp for sp in spots if sp["dx_callsign"] == user.callsign]
    return {"source": f"{s.rbn_host}:{s.rbn_port}", "sample_seconds": s.activity_sample_seconds, "spots": spots}


@router.get("/aprs")
def aprs_stations(
    radius_km: int = Query(default=150, ge=10, le=2000),
    user: User = Depends(get_current_user),
):
    """Live APRS stations near you (position packets sampled from APRS-IS)."""
    if user.lat is None or user.lon is None:
        raise HTTPException(400, "set your location (grid square or address) in your profile first")
    s = get_settings()
    ttl = max(30.0, s.activity_sample_seconds)
    key = f"aprs:{round(user.lat, 1)}:{round(user.lon, 1)}:{radius_km}"

    def produce():
        stations = aprsis.sample_aprs(user.callsign, user.lat, user.lon, radius_km)
        for st in stations:
            st["distance_miles"] = _dist(user, st["lat"], st["lon"])
        stations.sort(key=lambda st: st["distance_miles"] if st["distance_miles"] is not None else 1e9)
        return stations

    return {
        "source": f"{s.aprs_host}:{s.aprs_port}",
        "sample_seconds": s.activity_sample_seconds,
        "radius_km": radius_km,
        "stations": activity_cache.wrap(key, ttl, produce),
    }


@router.get("/pota")
def pota_spots(user: User = Depends(get_current_user)):
    ttl = get_settings().activity_cache_seconds

    def produce():
        spots = pota_sota.query_pota(get_settings().activity_max_spots)
        for sp in spots:
            d = _dist(user, sp.get("lat"), sp.get("lon"))
            sp["distance_miles"] = d if d is not None else _dist_grid(user, sp["grid"])
        return spots

    return {"source": "pota.app", "spots": activity_cache.wrap("pota", ttl, produce)}


@router.get("/sota")
def sota_spots(user: User = Depends(get_current_user)):
    ttl = get_settings().activity_cache_seconds

    def produce():
        spots = pota_sota.query_sota(get_settings().activity_max_spots)
        for sp in spots:
            sp["distance_miles"] = _dist(user, sp["lat"], sp["lon"])
        return spots

    return {"source": "sota.org.uk", "spots": activity_cache.wrap("sota", ttl, produce)}


@router.get("/bands")
def band_conditions(user: User = Depends(get_current_user)):
    """Solar indices and HF band conditions (hamqsl.com)."""
    ttl = max(get_settings().activity_cache_seconds, 900)  # feed updates ~15 min
    data = activity_cache.wrap("bands", ttl, bandcond.query_band_conditions)
    if data is None:
        raise HTTPException(502, "band condition feed unavailable right now")
    return data
