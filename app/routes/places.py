"""Place search (ZIP codes and city names) for the map search box.

Backed by the `places` table — GeoNames postal codes for the US and its
territories, rebuilt at every ULS import. Coordinates are the ZIP centroid,
or for a city the mean of its ZIP centroids."""

import math
import re

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import get_current_user

router = APIRouter(prefix="/api/places", tags=["places"])

_ZIP_RE = re.compile(r"^(\d{5})(?:-\d{4})?$")
_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "PUERTO RICO": "PR", "GUAM": "GU", "VIRGIN ISLANDS": "VI", "NORTHERN MARIANA ISLANDS": "MP",
    "AMERICAN SAMOA": "AS",
}


def _like_prefix(s: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", s) + "%"


def _state_filter(suffix: str) -> tuple[str, dict]:
    """", ST" narrows the state: a 2-letter code, a state-code prefix while
    typing ("Springfield, I"), or a state name ("Springfield, Illinois")."""
    s = " ".join(suffix.upper().split())
    if not s:
        return "", {}
    if s in _STATE_NAMES:
        return " AND state = :st", {"st": _STATE_NAMES[s]}
    if len(s) <= 2 and s.isalpha():
        return " AND state LIKE :st ESCAPE '\\'", {"st": _like_prefix(s)}
    codes = sorted({code for name, code in _STATE_NAMES.items() if name.startswith(s)})
    if not codes:
        return " AND 0", {}
    return " AND state IN (%s)" % ", ".join(f"'{c}'" for c in codes), {}


@router.get("/search")
def places_search(q: str = "", limit: int = 8, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """A five-digit ZIP, or a city-name prefix with an optional ", ST".
    Cities that are exactly the name typed come first, then the nearest to
    the member (by their stored point), then the bigger (more ZIPs)."""
    q = " ".join(q.split())
    limit = max(1, min(25, limit))
    m = _ZIP_RE.match(q)
    if m:
        r = db.execute(text("SELECT zip, place, state, lat, lon FROM places WHERE zip = :z"),
                       {"z": m.group(1)}).first()
        if r is None:
            return {"results": []}
        return {"results": [{
            "label": f"{r.zip} · {r.place}, {r.state}", "place": r.place, "state": r.state,
            "zip": r.zip, "lat": r.lat, "lon": r.lon, "kind": "zip"}]}

    name, _, suffix = q.partition(",")
    name = name.strip()
    if len(name) < 2:
        return {"results": []}
    state_sql, params = _state_filter(suffix)
    params.update({"pat": _like_prefix(name), "name": name, "lim": limit})
    if user.lat is not None and user.lon is not None:
        # equirectangular distance, squared (only the order matters)
        params.update({"ulat": user.lat, "ulon": user.lon, "kx": math.cos(math.radians(user.lat))})
        near = ("((AVG(lat) - :ulat) * (AVG(lat) - :ulat)"
                " + (AVG(lon) - :ulon) * (AVG(lon) - :ulon) * :kx * :kx)")
    else:
        near = "NULL"  # (a bare 0 would mean "column 0")
    rows = db.execute(text(
        "SELECT place, state, AVG(lat) AS lat, AVG(lon) AS lon, COUNT(*) AS n FROM places"
        f" WHERE place LIKE :pat ESCAPE '\\'{state_sql}"
        f" GROUP BY place, state ORDER BY (lower(place) = lower(:name)) DESC, {near}, n DESC, place, state"
        " LIMIT :lim"), params).all()
    return {"results": [{
        "label": f"{r.place}, {r.state}", "place": r.place, "state": r.state, "zip": None,
        "lat": round(r.lat, 5), "lon": round(r.lon, 5), "kind": "city"} for r in rows]}
