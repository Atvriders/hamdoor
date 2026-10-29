"""Place search (ZIP codes and city names) for the map search box.

Backed by the `places` table — GeoNames postal codes for the US and its
territories, rebuilt at every ULS import. Coordinates are the ZIP centroid,
or for a city the mean of its ZIP centroids; a city's ZIP count stands in
for its size."""

import itertools
import re

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.geo import haversine_miles
from app.models import User
from app.security import get_current_user
from app.textcase import place_key

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


# GeoNames spells most of these out ("Saint Louis", "Fort Worth", "Mount
# Vernon") and a few the short way ("St Thomas" and "St John" in the Virgin
# Islands): a word typed either way finds both
_SHORT = {"saint": "st", "sainte": "ste", "fort": "ft", "mount": "mt", "point": "pt"}
_LONG = {short: long for long, short in _SHORT.items()}
_MAX_SPELLINGS = 8
# places of one name are ranked by miles / ZIPs ** _SIZE_WEIGHT: a place with
# 10 times the ZIP codes counts as 5.6 times nearer, so a far city beats a
# village unless the village is close ("Topeka" from Connecticut: Kansas, 31
# ZIPs at 1,220 mi, before Indiana, 1 ZIP at 661 mi), while two cities of
# about the same size keep the nearer first (Springfield, Missouri before
# Springfield, Illinois from Kansas City)
_SIZE_WEIGHT = 0.75


def _spellings(name: str) -> list[str]:
    """The typed name in lower case without periods, in each spelling of its
    abbreviated words: "St. Louis" -> ["st louis", "saint louis"]. A word
    changes form only once it is complete (another word follows, or it ends
    in a period), so "St" alone still finds Stamford. The search compares
    their place_key, so apostrophes, hyphens and spaces never matter."""
    words = name.lower().replace(".", ". ").split()
    choices = []
    for i, word in enumerate(words):
        bare = word.replace(".", "")
        if not bare:
            continue
        forms = [bare]
        other = _SHORT.get(bare) or _LONG.get(bare)
        if other and (i < len(words) - 1 or word.endswith(".")):
            forms.append(other)
        choices.append(forms)
    if not choices:
        return []
    spelled = (" ".join(c) for c in itertools.product(*choices))
    return [s for s in itertools.islice(spelled, _MAX_SPELLINGS) if len(s) >= 2]


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
    "St." and "Saint", "Ft" and "Fort", "Mt" and "Mount" (and "Ste", "Pt")
    find either spelling, and so do names with and without their
    apostrophes, hyphens and spaces or accents ("Lee's Summit" finds GeoNames'
    "Lees Summit", "Winston Salem" "Winston-Salem", "McLean" "Mc Lean": the
    places' key, textcase.place_key). Cities that are exactly the name typed
    come first.
    Then, for a member with a location, the nearest — but a much bigger place
    beats a far smaller one unless that one is close (_SIZE_WEIGHT), so a
    Kansas member's "Lawrence" stays Kansas and a Connecticut member's
    "Portland" is Portland CT, then Maine and Oregon. Without a location, the
    biggest first."""
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
    spellings = _spellings(name) if len(name) >= 2 else []
    # keys hold letters and digits only, so a GLOB prefix needs no escaping
    keys = list(dict.fromkeys(k for k in map(place_key, spellings) if len(k) >= 2))
    if not keys:
        return {"results": []}
    state_sql, params = _state_filter(suffix)
    params.update({f"p{i}": k + "*" for i, k in enumerate(keys)})
    match = " OR ".join(f"key GLOB :p{i}" for i in range(len(keys)))
    rows = db.execute(text(
        "SELECT place, state, AVG(lat) AS lat, AVG(lon) AS lon, COUNT(*) AS n FROM places"
        f" WHERE ({match}){state_sql} GROUP BY place, state"), params).all()
    exact = set(keys)
    if user.lat is not None and user.lon is not None:
        def rank(r):
            far = haversine_miles(user.lat, user.lon, r.lat, r.lon) / r.n ** _SIZE_WEIGHT
            return (place_key(r.place) not in exact, far, -r.n, r.place, r.state)
    else:
        def rank(r):
            return (place_key(r.place) not in exact, -r.n, r.place, r.state)
    rows = sorted(rows, key=rank)[:limit]
    return {"results": [{
        "label": f"{r.place}, {r.state}", "place": r.place, "state": r.state, "zip": None,
        "lat": round(r.lat, 5), "lon": round(r.lon, 5), "kind": "city"} for r in rows]}
