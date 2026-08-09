"""Public FCC ULS directory endpoints — every active US ham, clustered on the
map. Street addresses and emails are never exposed here (ZIP-centroid only)."""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.integrations import uls
from app.models import Ham, User
from app.schemas import normalize_callsign
from app.security import get_current_user

router = APIRouter(prefix="/api/hams", tags=["hams"])

# map zoom -> cluster cell size in degrees; at >= 10 individual hams are returned
_CELL_BY_ZOOM = {4: 5.0, 5: 3.0, 6: 2.0, 7: 1.0, 8: 0.5, 9: 0.25}
_DETAIL_ZOOM = 10


def is_expired(expires: str) -> bool:
    """ULS keeps licenses with a past expiration date in 'active' status
    through the 2-year renewal grace period — flag those as expired."""
    try:
        return datetime.strptime(expires, "%m/%d/%Y").date() < date.today()
    except (ValueError, TypeError):
        return False


@router.get("/count")
def hams_count(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    last = uls.last_import()
    return {
        "count": db.scalar(select(func.count()).select_from(Ham)) or 0,
        "last_import": last.isoformat() if last else None,
        "source": "FCC ULS weekly amateur extract",
    }


@router.get("/map")
def hams_map(
    min_lat: float = Query(ge=-90, le=90),
    max_lat: float = Query(ge=-90, le=90),
    min_lon: float = Query(ge=-180, le=180),
    max_lon: float = Query(ge=-180, le=180),
    zoom: int = Query(ge=0, le=18),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if zoom >= _DETAIL_ZOOM:
        rows = db.execute(
            select(Ham.callsign, Ham.name, Ham.city, Ham.state, Ham.lat, Ham.lon,
                   Ham.license_class, Ham.expires)
            .where(Ham.lat.is_not(None),
                   Ham.lat.between(min_lat, max_lat),
                   Ham.lon.between(min_lon, max_lon))
            .order_by(Ham.callsign)
            .limit(get_settings().hams_map_max_results + 1)
        ).all()
        truncated = len(rows) > get_settings().hams_map_max_results
        rows = rows[: get_settings().hams_map_max_results]
        return {
            "type": "hams",
            "truncated": truncated,
            "hams": [{"callsign": r.callsign, "name": r.name, "city": r.city,
                      "state": r.state, "lat": r.lat, "lon": r.lon,
                      "license_class": r.license_class,
                      "expired": is_expired(r.expires)} for r in rows],
        }

    cell = _CELL_BY_ZOOM.get(zoom, 6.0 if zoom < 4 else 0.25)
    # floor() via CAST + correction (SQLite has no reliable FLOOR)
    sql = text("""
        SELECT CAST(lat / :c AS INTEGER) - (lat / :c < CAST(lat / :c AS INTEGER)) AS bl,
               CAST(lon / :c AS INTEGER) - (lon / :c < CAST(lon / :c AS INTEGER)) AS bo,
               COUNT(*) AS n
        FROM hams
        WHERE lat IS NOT NULL
          AND lat BETWEEN :minla AND :maxla
          AND lon BETWEEN :minlo AND :maxlo
        GROUP BY bl, bo
    """)
    cells = [
        {"lat": (r.bl + 0.5) * cell, "lon": (r.bo + 0.5) * cell, "count": r.n}
        for r in db.execute(sql, {"c": cell, "minla": min_lat, "maxla": max_lat,
                                  "minlo": min_lon, "maxlo": max_lon})
    ]
    return {"type": "clusters", "cell_degrees": cell, "cells": cells}


@router.get("/{callsign}")
def ham_detail(callsign: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    cs = normalize_callsign(callsign)
    ham = db.get(Ham, cs)
    if ham is None:
        raise HTTPException(404, f"{cs} not found in the FCC database")
    return {
        "callsign": ham.callsign,
        "name": ham.name,
        "city": ham.city,
        "state": ham.state,
        "zip": ham.zip,
        "license_class": ham.license_class,
        "expires": ham.expires,
        "expired": is_expired(ham.expires),
        "lat": ham.lat,
        "lon": ham.lon,
        "registered": db.scalar(select(func.count()).select_from(User).where(User.callsign == cs)) > 0,
    }
