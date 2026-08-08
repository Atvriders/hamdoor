from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.geo import bounding_box, grid_to_latlon, haversine_miles
from app.models import User
from app.schemas import UserPublic
from app.security import get_current_user

router = APIRouter(prefix="/api/operators", tags=["operators"])


def public_location(user: User) -> tuple[float | None, float | None]:
    """Location shown to other users: grid-square center if known, otherwise
    the stored point rounded to ~1 km. Street address is never exposed."""
    if user.grid:
        ll = grid_to_latlon(user.grid)
        if ll:
            return ll
    if user.lat is not None and user.lon is not None:
        return (round(user.lat, 2), round(user.lon, 2))
    return (None, None)


@router.get("/nearby", response_model=list[UserPublic])
def nearby(
    range: float | None = Query(default=None, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.lat is None or user.lon is None:
        raise HTTPException(400, "set your location (grid square or address) in your profile first")

    max_range = range if range is not None else user.range_miles
    min_lat, max_lat, min_lon, max_lon = bounding_box(user.lat, user.lon, max_range)

    candidates = db.scalars(
        select(User).where(
            User.id != user.id,
            User.lat.is_not(None),
            User.lon.is_not(None),
            User.lat.between(min_lat, max_lat),
            User.lon.between(min_lon, max_lon),
        )
    ).all()

    out = []
    for other in candidates:
        dist = haversine_miles(user.lat, user.lon, other.lat, other.lon)
        if dist > max_range:
            continue
        lat, lon = public_location(other)
        out.append(
            UserPublic(
                callsign=other.callsign,
                name=other.name,
                grid=other.grid,
                lat=lat,
                lon=lon,
                distance_miles=round(dist, 1),
            )
        )
    out.sort(key=lambda o: o.distance_miles if o.distance_miles is not None else 1e9)
    return out
