"""Amateur repeaters on the map: HearHam (primary), BrandMeister and IRLP,
merged by app/integrations/repeaters.py. Read-only; every route needs a login.

Upstream text (names, notes) comes back as plain text with emails and street
addresses already removed; the browser escapes it before display.
"""

import json
import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.geo import cell_degrees, clamp_bbox, snap_bbox
from app.integrations import repeaters as rpt
from app.models import Repeater, User
from app.security import get_current_user

router = APIRouter(prefix="/api/repeaters", tags=["repeaters"])

DETAIL_ZOOM = 10  # below this the map gets cluster counts, at/above it repeaters
BAND_FILTERS = rpt.FILTER_BANDS + ("other",)
MODE_FILTERS = ("FM", "DMR", "DSTAR", "YSF", "P25", "NXDN", "M17", "ATV", "OTHER")
ATTRIBUTION = [
    {"name": "HearHam", "url": "https://hearham.com/repeaters"},
    {"name": "BrandMeister", "url": "https://brandmeister.network/"},
    {"name": "IRLP", "url": "https://www.irlp.net/"},
]
# RepeaterBook is link-out only (its terms forbid maps built from its data);
# form fields verified against /repeaters/prox2.php (research §4.1)
REPEATERBOOK_NEAR_URL = ("https://www.repeaterbook.com/repeaters/prox2_result.php"
                         "?lat={lat}&long={lon}&distance=25&Dunit=m")
_US_SQL = ", ".join(f"'{c}'" for c in sorted(rpt.US_JURISDICTION))


def repeater_json(r: Repeater) -> dict:
    """The API shape of one repeater (contract `R`)."""
    try:
        also_in = json.loads(r.also_in) if r.also_in else []
    except ValueError:
        also_in = []
    return {
        "id": r.id, "callsign": r.callsign, "name": r.name,
        "output_mhz": r.output_mhz, "input_mhz": r.input_mhz, "offset_mhz": r.offset_mhz,
        "band": r.band, "modes": [m for m in (r.modes or "").split(",") if m],
        "tone_up": r.tone_up, "tone_down": r.tone_down, "dcs": r.dcs, "tone_burst": r.tone_burst,
        "dmr_color_code": r.dmr_color_code, "dmr_id": r.dmr_id, "dmr_network": r.dmr_network,
        "nxdn_ran": r.nxdn_ran, "p25_nac": r.p25_nac, "m17_can": r.m17_can,
        "city": r.city or "", "state": r.state or "", "country": r.country or "",
        "lat": r.lat, "lon": r.lon, "precise": bool(r.precise),
        "location_quality": r.location_quality, "status": r.status, "use": r.use, "kind": r.kind,
        "links": {"echolink": r.echolink, "allstar": r.allstar, "irlp": r.irlp, "wiresx": r.wiresx},
        "notes": r.notes or "", "source": r.source, "source_url": r.source_url,
        "also_in": also_in, "last_update": r.last_update,
    }


def _csv(raw: str | None, allowed: tuple[str, ...], norm) -> list[str] | None:
    """None (parameter absent) = everything; '' or only unknown tokens = nothing."""
    if raw is None:
        return None
    out = []
    for tok in raw.split(","):
        t = norm(tok)
        if t in allowed and t not in out:
            out.append(t)
    return out


def _band_token(t: str) -> str:
    return t.strip().lower().replace(" ", "")


def _mode_token(t: str) -> str:
    return "".join(ch for ch in t.upper() if ch.isalnum())


def _filters(bands: str | None, modes: str | None, onair: bool, simplex: bool,
             approx: bool) -> tuple[list[str], dict]:
    """SQL conditions shared by /map and /near. Rows without coordinates never match."""
    clauses, params = ["lat IS NOT NULL", "lon IS NOT NULL"], {}
    if not get_settings().repeaters_enabled:
        clauses.append("0")
    band_list = _csv(bands, BAND_FILTERS, _band_token)
    if band_list is not None:
        ors = []
        named = [b for b in band_list if b != "other"]
        if named:
            ors.append("band IN (" + ", ".join(f":band{i}" for i in range(len(named))) + ")")
            params.update({f"band{i}": b for i, b in enumerate(named)})
        if "other" in band_list:  # 4m, 13cm, 9cm, 6cm, 3cm, out-of-band listings
            ors.append("band NOT IN (" + ", ".join(f":fb{i}" for i in range(len(rpt.FILTER_BANDS)))
                       + ")")
            params.update({f"fb{i}": b for i, b in enumerate(rpt.FILTER_BANDS)})
        clauses.append("(" + " OR ".join(ors) + ")" if ors else "0")
    mode_list = _csv(modes, MODE_FILTERS, _mode_token)
    if mode_list is not None:
        tokens = [t for m in mode_list for t in (("OTHER", "TETRA") if m == "OTHER" else (m,))]
        ors = [f"(',' || modes || ',') LIKE :mode{i}" for i in range(len(tokens))]
        params.update({f"mode{i}": f"%,{t},%" for i, t in enumerate(tokens)})
        clauses.append("(" + " OR ".join(ors) + ")" if ors else "0")  # any listed mode matches
    if onair:
        clauses.append("status != 'off-air'")
    if not simplex:
        clauses.append("kind != 'simplex_link'")
    if not approx:
        clauses.append("precise = 1")
    return clauses, params


_BBOX = "lat BETWEEN :min_lat AND :max_lat AND lon BETWEEN :min_lon AND :max_lon"


@router.get("/status")
def repeaters_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    s = get_settings()
    try:
        count, with_coords, us = db.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(lat IS NOT NULL), 0),"
            f" COALESCE(SUM(lat IS NOT NULL AND country IN ({_US_SQL})), 0) FROM repeaters")).one()
    except Exception:
        count = with_coords = us = 0
    st = rpt.source_status()
    keys = ("fetched_at", "ok", "count", "error", "last_ok_at", "next_due")
    return {
        "enabled": s.repeaters_enabled,
        "count": count,
        "with_coords": with_coords,
        "us": us,  # mappable repeaters in the US and its territories
        "sources": {name: {k: st[name][k] for k in keys} for name in rpt.SOURCES},
        "geonames": {k: st[rpt.PLACES][k] for k in keys},
        "built_at": st["_build"]["built_at"],
        "build_error": st["_build"]["error"],  # why the last rebuild failed (None when it didn't)
        "attribution": ATTRIBUTION,
        "repeaterbook_near_url": REPEATERBOOK_NEAR_URL,
    }


@router.get("/map")
def repeaters_map(
    min_lat: float = Query(allow_inf_nan=False),
    max_lat: float = Query(allow_inf_nan=False),
    min_lon: float = Query(allow_inf_nan=False),
    max_lon: float = Query(allow_inf_nan=False),
    zoom: int = Query(),
    bands: str | None = Query(None, description="CSV of 10m,6m,2m,1.25m,70cm,33cm,23cm,other"),
    modes: str | None = Query(None, description="CSV of FM,DMR,DSTAR,YSF,P25,NXDN,M17,ATV,OTHER"),
    onair: bool = Query(True, description="hide repeaters listed as off the air"),
    simplex: bool = Query(False, description="include linked simplex nodes"),
    approx: bool = Query(True, description="include approximately placed repeaters"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    zoom = max(0, min(22, zoom))
    min_lat, max_lat, min_lon, max_lon = clamp_bbox(min_lat, max_lat, min_lon, max_lon)
    clauses, params = _filters(bands, modes, onair, simplex, approx)
    where = " AND ".join(clauses)
    box = {"min_lat": min_lat, "max_lat": max_lat, "min_lon": min_lon, "max_lon": max_lon}

    if zoom < DETAIL_ZOOM:
        cell = cell_degrees(zoom)
        s_min_lat, s_max_lat, s_min_lon, s_max_lon = snap_bbox(min_lat, max_lat, min_lon, max_lon,
                                                               cell)
        # One scan of the snapped box: bubbles count whole cells and sit at
        # their members' mean position (never the cell centre); `total` counts
        # only what is inside the requested view. floor() via CAST + correction
        # (SQLite has no reliable FLOOR).
        rows = db.execute(text(f"""
            SELECT CAST(lat / :c AS INTEGER) - (lat / :c < CAST(lat / :c AS INTEGER)) AS bl,
                   CAST(lon / :c AS INTEGER) - (lon / :c < CAST(lon / :c AS INTEGER)) AS bo,
                   AVG(lat) AS alat, AVG(lon) AS alon, COUNT(*) AS n,
                   SUM(lat BETWEEN :v_min_lat AND :v_max_lat
                       AND lon BETWEEN :v_min_lon AND :v_max_lon) AS in_view
            FROM repeaters WHERE {where} AND {_BBOX}
            GROUP BY bl, bo ORDER BY bl, bo"""),
            {**params, "c": cell, "min_lat": s_min_lat, "max_lat": s_max_lat,
             "min_lon": s_min_lon, "max_lon": s_max_lon, **{f"v_{k}": v for k, v in box.items()}}
        ).all()
        return {
            "type": "clusters", "zoom": zoom, "cell_degrees": cell,
            "total": sum(r.in_view for r in rows), "reason": "zoom",
            "cells": [{"lat": round(r.alat, 5), "lon": round(r.alon, 5), "count": r.n} for r in rows],
        }

    # total = what matches inside the requested (clamped) view, before the cap
    total = db.execute(text(f"SELECT COUNT(*) FROM repeaters WHERE {where} AND {_BBOX}"),
                       {**params, **box}).scalar() or 0
    cap = get_settings().repeaters_map_max_results
    rows = db.execute(
        select(Repeater)
        .where(text(f"{where} AND {_BBOX}").bindparams(**params, **box))
        .order_by(Repeater.precise.desc(), text("(status = 'on-air') DESC"), Repeater.id)
        .limit(cap)
    ).scalars().all()
    return {"type": "repeaters", "zoom": zoom, "total": total, "truncated": total > cap,
            "repeaters": [repeater_json(r) for r in rows]}


def _bearing(la1: float, lo1: float, la2: float, lo2: float) -> float:
    """Initial great-circle bearing, degrees clockwise from true north."""
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _lon_ranges(lat: float, lon: float, radius_km: float) -> list[tuple[float, float]] | None:
    """Longitude ranges covering a radius circle (split at the antimeridian);
    None when the circle reaches a pole and every longitude qualifies."""
    delta = radius_km / 6371.0088
    cos_lat = math.cos(math.radians(lat))
    if math.sin(delta) >= cos_lat:
        return None
    d = math.degrees(math.asin(math.sin(delta) / cos_lat))
    lo, hi = lon - d, lon + d
    if lo < -180:
        return [(-180.0, hi), (lo + 360.0, 180.0)]
    if hi > 180:
        return [(lo, 180.0), (-180.0, hi - 360.0)]
    return [(lo, hi)]


# up to 805 km: the widest range a member can choose (500 miles, You > Range),
# so `total` counts the range Activity and the Feed print
NEAR_RADIUS_KM = (0.1, 805.0)
NEAR_MAX_LIMIT = 200


@router.get("/near")
def repeaters_near(
    lat: float | None = Query(None, ge=-90, le=90, allow_inf_nan=False),
    lon: float | None = Query(None, ge=-180, le=180, allow_inf_nan=False),
    radius_km: float = Query(80.0, allow_inf_nan=False, description="clamped to 0.1-805"),
    limit: int = Query(50, description="clamped to 1-200"),
    bands: str | None = Query(None),
    modes: str | None = Query(None),
    onair: bool = Query(True),
    simplex: bool = Query(False),
    approx: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Repeaters within radius_km of a point (default: the signed-in user's
    location), nearest first, each with distance_km and bearing_deg, at most
    `limit` of them; `total` counts every one within the radius. Radius and
    limit are clamped like every other map/search limit, never a 422; the
    radius reaches 805 km, a member's widest range (500 miles)."""
    radius_km = min(max(radius_km, NEAR_RADIUS_KM[0]), NEAR_RADIUS_KM[1])
    limit = max(1, min(NEAR_MAX_LIMIT, limit))
    if (lat is None) != (lon is None):
        raise HTTPException(400, "pass both lat and lon (or neither to use your location)")
    if lat is None:
        lat, lon = user.lat, user.lon
    if lat is None or lon is None:
        raise HTTPException(400, "no location: pass lat and lon, or set your location in your profile")
    clauses, params = _filters(bands, modes, onair, simplex, approx)
    dlat = math.degrees(radius_km / 6371.0088)
    clauses.append("lat BETWEEN :near_min_lat AND :near_max_lat")
    params.update(near_min_lat=lat - dlat, near_max_lat=lat + dlat)
    ranges = _lon_ranges(lat, lon, radius_km)
    if ranges is not None:
        ors = []
        for i, (lo, hi) in enumerate(ranges):
            ors.append(f"lon BETWEEN :lon_lo{i} AND :lon_hi{i}")
            params.update({f"lon_lo{i}": lo, f"lon_hi{i}": hi})
        clauses.append("(" + " OR ".join(ors) + ")")
    # every candidate's point to count and sort (a 500-mile range holds a few
    # thousand), then the whole rows of only the nearest `limit`
    candidates = db.execute(
        select(Repeater.id, Repeater.lat, Repeater.lon).where(text(" AND ".join(clauses)).bindparams(**params))
    ).all()
    hits = []
    for rid, rlat, rlon in candidates:
        d = rpt.haversine_km(lat, lon, rlat, rlon)
        if d <= radius_km:
            hits.append((d, rid))
    hits.sort()
    nearest = hits[:limit]
    rows = {r.id: r for r in db.execute(
        select(Repeater).where(Repeater.id.in_([rid for _, rid in nearest]))).scalars()}
    return {"repeaters": [
        {**repeater_json(rows[rid]), "distance_km": round(d, 2),
         "bearing_deg": round(_bearing(lat, lon, rows[rid].lat, rows[rid].lon), 1)}
        for d, rid in nearest
    ], "total": len(hits)}


@router.get("/{repeater_id}")
def repeater_detail(repeater_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """One repeater plus its talkgroups (BrandMeister repeaters only, fetched
    on demand and cached in the row for 24 h; [] when unavailable)."""
    if not get_settings().repeaters_enabled:
        raise HTTPException(404, "repeaters are not enabled on this server")
    r = db.get(Repeater, repeater_id)
    if r is None:
        raise HTTPException(404, f"repeater {repeater_id} not found")
    out = repeater_json(r)
    out["talkgroups"] = (rpt.talkgroups_for(r.id, r.dmr_id, r.talkgroups)
                         if r.dmr_id and r.dmr_network == "BrandMeister" else [])
    return out
