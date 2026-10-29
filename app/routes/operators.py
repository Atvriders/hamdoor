from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.geo import bounding_box, haversine_miles
from app.integrations.geocode import shown_location, shows_exact_location
from app.models import User
from app.routes.hams import fcc_badges
from app.schemas import UserPublic
from app.security import get_current_user

router = APIRouter(prefix="/api/operators", tags=["operators"])


def public_location(user: User) -> tuple[float | None, float | None, str]:
    """(lat, lon, grid) other members see — geocode.shown_location, which
    posts share (a 'grid' member's posts sit at the same subsquare centre)."""
    return shown_location(user)


@router.get("/nearby", response_model=list[UserPublic])
def nearby(
    range: float | None = Query(default=None, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Members within range, nearest first. A 'grid' member is placed, and
    judged in range, by their subsquare centre with the distance rounded to
    whole miles: a distance or an in-range test from their exact point would
    leak it (`range` is a float — bisecting it from three spots would
    trilaterate the point). Each carries their map precision and, from
    their FCC row, licence class and new-ham flag."""
    if user.lat is None or user.lon is None:
        raise HTTPException(400, "set your location (grid square or address) in your profile first")

    # never wider than the widest range a member can set (the You slider)
    max_range = min(range if range is not None else user.range_miles, get_settings().max_range_miles)
    min_lat, max_lat, min_lon, max_lon = bounding_box(user.lat, user.lon, max_range)

    located = (User.id != user.id, User.lat.is_not(None), User.lon.is_not(None))
    candidates = db.scalars(select(User).where(
        *located, User.map_precision == "address",
        User.lat.between(min_lat, max_lat), User.lon.between(min_lon, max_lon),
    )).all()
    # 'grid' members: their shown point can lie anywhere in their stored
    # grid, so they're not prefiltered by their stored location (few rows)
    candidates += db.scalars(select(User).where(*located, User.map_precision != "address")).all()

    found = []
    for other in candidates:
        lat, lon, grid = public_location(other)
        exact = shows_exact_location(other)
        ref = (other.lat, other.lon) if exact else (lat, lon)
        if ref[0] is None:
            continue
        dist = haversine_miles(user.lat, user.lon, ref[0], ref[1])
        if dist <= max_range:
            found.append((other, lat, lon, grid, round(dist, 1) if exact else float(round(dist))))
    badges = fcc_badges(db, (f[0].callsign for f in found))
    out = [
        UserPublic(
            callsign=other.callsign,
            name=other.name,
            grid=grid,
            lat=lat,
            lon=lon,
            distance_miles=dist,
            map_precision="address" if shows_exact_location(other) else "grid",
            license_class=badges.get(other.callsign, (None, False))[0],
            new=badges.get(other.callsign, (None, False))[1],
        )
        for other, lat, lon, grid, dist in found
    ]
    out.sort(key=lambda o: o.distance_miles if o.distance_miles is not None else 1e9)
    return out
