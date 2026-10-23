"""Locations for registered users.

Nominatim (OpenStreetMap) is the network fallback for addresses with no
better source. Be polite: fixed User-Agent, single request, short timeout.
Results are best-effort.
"""

import logging
import re

import httpx
from sqlalchemy import text

from app.config import get_settings

log = logging.getLogger(__name__)

# a stored location this far from both the user's ZIP area and their grid
# square is someone else's (e.g. the previous holder of a recycled callsign)
# — farther than any grid-square or ZIP error, nearer than the 500 km median
# of those mix-ups
_STALE_MILES = 30.0


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


def shows_exact_location(user) -> bool:
    """Is a member shown to other members at their stored location? Members
    keep the default map precision 'address' unless they chose 'grid'; any
    other value counts as 'grid' (never more precise than chosen)."""
    return (user.map_precision or "address") == "address"


def latlon_to_grid(lat: float, lon: float, length: int = 6) -> str:
    """Maidenhead locator of a point ("FN31pr" for 41.7147, -72.7284) — 4, 6
    or 8 characters; the inverse of app.geo.grid_to_latlon, which returns a
    square's centre. Each character is taken from one floor() of the point
    scaled to the finest unit (no repeated subtraction, so float error never
    yields an out-of-range letter); the poles and the antimeridian belong to
    the last square."""
    if length not in (4, 6, 8):
        raise ValueError("a locator has 4, 6 or 8 characters")
    per_square = {4: 1, 6: 24, 8: 240}[length]  # finest units per 2°x1° square
    x = min(int((min(max(lon, -180.0), 180.0) + 180.0) / 2 * per_square), 180 * per_square - 1)
    y = min(int((min(max(lat, -90.0), 90.0) + 90.0) * per_square), 180 * per_square - 1)
    sx, sy = x // per_square, y // per_square  # 2°x1° squares from (-180, -90)
    out = chr(ord("A") + sx // 10) + chr(ord("A") + sy // 10) + f"{sx % 10}{sy % 10}"
    if length >= 6:
        unit = per_square // 24
        out += chr(ord("a") + x // unit % 24) + chr(ord("a") + y // unit % 24)
    if length == 8:
        out += f"{x % 10}{y % 10}"
    return out


def member_subsquare(grid: str, lat: float | None, lon: float | None) -> str | None:
    """The locator a member with map precision 'grid' is shown at: the
    6-character subsquare of their stored grid; for a 4-character grid, the
    subsquare of their stored location when it lies inside that square (else
    the square itself, coarser still); with no usable grid, the subsquare of
    their stored location. None when neither is known."""
    from app.geo import grid_to_latlon

    g = (grid or "").strip()
    if len(g) >= 6 and grid_to_latlon(g[:6]):
        return g[:4].upper() + g[4:6].lower()
    have_point = lat is not None and lon is not None
    if len(g) >= 4 and grid_to_latlon(g[:4]):
        square = g[:4].upper()
        if have_point and latlon_to_grid(lat, lon, 4) == square:
            return latlon_to_grid(lat, lon, 6)
        return square
    return latlon_to_grid(lat, lon, 6) if have_point else None


def shown_location(user) -> tuple[float | None, float | None, str]:
    """(lat, lon, grid) other members see. 'address' members: their stored
    location (their FCC address geocode — the point the public directory
    map already shows), falling back to the grid-square centre, and their
    grid as entered. 'grid' members: the centre of their 6-character
    subsquare (see member_subsquare) and that locator — never their exact
    point. Street text and email are never exposed."""
    from app.geo import grid_to_latlon

    if not shows_exact_location(user):
        g = member_subsquare(user.grid, user.lat, user.lon)
        ll = grid_to_latlon(g) if g else None
        return (ll[0], ll[1], g) if ll else (None, None, "")
    if user.lat is not None and user.lon is not None:
        return (user.lat, user.lon, user.grid)
    ll = grid_to_latlon(user.grid) if user.grid else None
    return (ll[0], ll[1], user.grid) if ll else (None, None, user.grid)


def post_point(user) -> tuple[float | None, float | None]:
    """Where a post by `user` is stored: an 'address' member's stored
    location; a 'grid' member's shown subsquare centre (shown_location) —
    their exact point is never written into a post."""
    if shows_exact_location(user):
        return (user.lat, user.lon)
    lat, lon, _ = shown_location(user)
    return (lat, lon)


def sync_post_locations(db, user, *, restore: bool = False) -> int:
    """Bring the posts by `user` in line with their map precision and
    location. Called whenever either may have changed: profile edits,
    refresh-callsign, the post-import sync for the members it moves, and
    with `restore` when a member has just switched back to 'address'.

    - 'grid': every post sits at post_point(user), the centre of the
      subsquare they are shown at (their exact point is never stored in a
      post), or nowhere (NULL) when they have no shown point.
    - 'address' with `restore`: every post gets their precise stored
      location again. The point a post was written at is not kept while
      they are on 'grid', because that would store it.
    - 'address' otherwise: each post keeps the snapshot taken when it was
      written, except that posts with no location get the member's. Those
      were written while the member had none, or emptied while they were on
      'grid' with no point to show, and a post with no location is in no
      feed, so leaving them would hide the posts for good.
    - 'address' with no location: posts stay as they are. They hold an old
      snapshot of the member's own point or a grid-era subsquare centre,
      neither of them a secret, so nothing is emptied (only 'grid' privacy
      ever needs a NULL).

    Leaves updated_at alone (not an edit) and does not commit. Returns the
    posts moved."""
    from sqlalchemy import or_, update

    from app.models import Post

    lat, lon = post_point(user)
    which = [Post.author_id == user.id]
    if shows_exact_location(user):
        if lat is None or lon is None:
            return 0
        if not restore:
            which.append(or_(Post.lat.is_(None), Post.lon.is_(None)))
    return db.execute(
        update(Post)
        .where(*which, (Post.lat.is_distinct_from(lat)) | (Post.lon.is_distinct_from(lon)))
        .values(lat=lat, lon=lon, updated_at=Post.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount


def _has_table(db, name: str) -> bool:
    return db.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"),
                      {"n": name}).first() is not None


def zip_anchor(db, zip_: str) -> tuple[float, float] | None:
    """Where the directory map puts the ZIP-level hams of this ZIP."""
    z = (zip_ or "").strip()[:5]
    if not z or not _has_table(db, "zip_anchors"):
        return None
    row = db.execute(text("SELECT lat, lon FROM zip_anchors WHERE zip = :z"), {"z": z}).first()
    return (row[0], row[1]) if row else None


def _norm_text(s: str) -> str:
    """Address text compared for identity: case, spacing and punctuation
    don't matter ("P.O. BOX 12" = "PO BOX 12", "5 Old Rd." = "5 OLD RD")."""
    return "".join(re.findall(r"[A-Z0-9]+", (s or "").upper()))


def same_address(a: tuple[str, str, str, str], b: tuple[str, str, str, str]) -> bool:
    """Do two (street, city, state, zip) tuples name the same address?"""
    return all(_norm_text(x) == _norm_text(y) for x, y in zip(a[:3], b[:3])) and \
        _norm_text(a[3])[:5] == _norm_text(b[3])[:5]


def profile_is_fcc_address(user, ham) -> bool:
    """Does a user's profile still carry their FCC licence address (not one
    they edited)? An FCC row without a street line — PO-box licensees: the
    importer keeps EN street_address, and the box is in another field, so
    callook shows "P.O. BOX 132" where the row has '' — matches on city,
    state and ZIP."""
    fcc = (ham.street, ham.city, ham.state, ham.zip)
    mine = (user.address_line, user.city, user.state, user.zip)
    if not _norm_text(ham.street):
        mine, fcc = ("",) + mine[1:], ("",) + fcc[1:]
    return same_address(mine, fcc)


def resolve_user_location_source(db, callsign: str, grid: str, address_line: str,
                                 city: str, state: str, zip_: str,
                                 record=None) -> tuple[float, float, str] | None:
    """Best location for a registered user, most precise source first, as
    (lat, lon, source) — source is what UserSelf.location_source reports:

    1. 'licence': the directory's street geocode — only when the hams row's
       address is the user's address (a recycled callsign or an edited
       profile must not inherit someone else's pin);
    2. 'provider': the provider's coordinates for this callsign (`record`,
       callook: Bing geocode of the license address, ~10 m) — likewise only
       when the user kept the provider's address, and not when they chose a
       grid of their own (an exact point at an address they chose not to
       use would be exposed to other members);
    3. 'grid': the grid-square centre, 4. 'geocoded': Nominatim;
    5. 'zip': the hams row's ZIP anchor, then the user's own ZIP anchor.
    """
    from app.geo import grid_to_latlon
    from app.models import Ham

    mine = (address_line, city, state, zip_)
    ham = db.get(Ham, callsign) if callsign else None
    if ham is not None and ham.lat is not None and ham.loc_source == "address" and \
            same_address((ham.street, ham.city, ham.state, ham.zip), mine):
        return (ham.lat, ham.lon, "licence")
    if record is not None and record.lat is not None and record.lon is not None and \
            same_address((record.address_line, record.city, record.state, record.zip), mine) and \
            (not grid or _norm_text(grid) == _norm_text(record.grid)):
        return (record.lat, record.lon, "provider")
    ll = resolve_location(grid, address_line, city, state, zip_)
    if ll:
        centre = grid_to_latlon(grid) if grid else None
        return (ll[0], ll[1], "grid" if centre is not None and tuple(ll) == tuple(centre) else "geocoded")
    if ham is not None:
        anchor = zip_anchor(db, ham.zip)
        if anchor:
            return (anchor[0], anchor[1], "zip")
        if ham.lat is not None and ham.loc_source == "zip":
            return (ham.lat, ham.lon, "zip")
    anchor = zip_anchor(db, zip_)
    return (anchor[0], anchor[1], "zip") if anchor else None


def resolve_user_location(db, callsign: str, grid: str, address_line: str,
                          city: str, state: str, zip_: str,
                          record=None) -> tuple[float, float] | None:
    """resolve_user_location_source without the source: (lat, lon) or None."""
    hit = resolve_user_location_source(db, callsign, grid, address_line, city, state, zip_,
                                       record=record)
    return (hit[0], hit[1]) if hit else None


# half height / half width (degrees) of a maidenhead square by locator length
_GRID_HALF = {4: (0.5, 1.0), 6: (2.5 / 120, 5 / 120), 8: (2.5 / 1200, 5 / 1200)}


def _near_own_area(lat: float, lon: float, anchor, grid: str) -> bool:
    """Is a stored location within ~30 mi of the user's ZIP anchor or grid
    square (or inside that square)? Then it is the user's own."""
    from app.geo import grid_to_latlon, haversine_miles

    if anchor is not None and haversine_miles(lat, lon, *anchor) <= _STALE_MILES:
        return True
    centre = grid_to_latlon(grid) if grid else None
    if centre is None:
        return False
    half_lat, half_lon = _GRID_HALF[len(grid.strip())]
    return (abs(lat - centre[0]) <= half_lat and abs(lon - centre[1]) <= half_lon) or \
        haversine_miles(lat, lon, *centre) <= _STALE_MILES


def sync_user_locations(db) -> int:
    """Re-place registered users after an import, without network calls.

    Only users whose profile still carries their FCC address are touched (a
    user-edited address is respected), in resolve_user_location's order
    with the sources available offline:
    - street-geocoded in the directory -> that street pin (1);
    - otherwise a user with no location, or a stale one -> their grid-square
      centre (3) wherever it is, else the ZIP anchor (5). Stale = more than
      ~30 mi from both the ZIP anchor and the user's grid square: another
      license holder's pin from before the USI-keyed join. A location near
      either is the user's own (provider coordinates, their grid, Nominatim)
      and is kept — a PO-box ZIP 45 mi from home must not override the grid.
    A moved user's location_source says which ('licence', 'grid', 'zip'),
    and their posts follow (sync_post_locations: a 'grid' member's to their
    new shown point; an 'address' member's posts with no location get the
    new point). Then every 'grid' member's posts are re-placed at their
    shown point (sync_grid_member_posts). Returns users moved.
    """
    from app.geo import grid_to_latlon
    from app.models import Ham, User

    moved = 0
    try:
        for u in db.query(User).all():
            ham = db.get(Ham, u.callsign)
            if ham is None:
                continue
            if not profile_is_fcc_address(u, ham):
                continue  # user edited their address — leave their location alone
            if ham.lat is not None and ham.loc_source == "address":
                target, source = (ham.lat, ham.lon), "licence"
            else:
                anchor = zip_anchor(db, ham.zip)
                if anchor is None and ham.lat is not None:
                    anchor = (ham.lat, ham.lon)
                grid = grid_to_latlon(u.grid) if u.grid else None
                if u.lat is not None and u.lon is not None and (
                        (anchor is None and grid is None)
                        or _near_own_area(u.lat, u.lon, anchor, u.grid if grid else "")):
                    continue  # the user's own location (or nothing to judge it by)
                target, source = (grid, "grid") if grid else (anchor, "zip")
            if target is not None and (u.lat, u.lon) != tuple(target):
                u.lat, u.lon = target
                u.location_source = source
                sync_post_locations(db, u)
                moved += 1
        if moved:
            db.commit()
            log.info("[uls] re-placed %d user location(s) after the import", moved)
    except Exception:
        db.rollback()
        log.warning("[uls] user location sync skipped", exc_info=True)
    sync_grid_member_posts(db)
    return moved


def place_mispinned_members(db, pause_s: float = 1.1) -> int:
    """Members whose profile address is not their licence's but who sit
    exactly on the licence's street pin with no recorded source: older
    versions pinned every street-geocoded member at the licence, whatever
    address they had entered. Re-place them as a profile save would
    (resolve_user_location_source: their grid, a Nominatim geocode of their
    address, else their ZIP area), at most one Nominatim request per
    `pause_s`. Runs once at startup (afterwards they have a source).
    Returns members moved."""
    import time

    from app.models import Ham, User

    moved = 0
    try:
        candidates = db.query(User).filter(User.location_source.is_(None), User.lat.is_not(None),
                                           User.lon.is_not(None)).all()
        for u in candidates:
            ham = db.get(Ham, u.callsign)
            if ham is None or ham.lat is None or ham.loc_source != "address" \
                    or profile_is_fcc_address(u, ham) \
                    or abs(u.lat - ham.lat) > 1e-7 or abs(u.lon - ham.lon) > 1e-7:
                continue
            if moved:
                time.sleep(pause_s)
            placed = resolve_user_location_source(db, u.callsign, u.grid, u.address_line,
                                                  u.city, u.state, u.zip)
            if placed is None or placed[2] == "licence":
                continue
            u.lat, u.lon, u.location_source = placed
            sync_post_locations(db, u)
            db.commit()
            moved += 1
        if moved:
            log.info("[users] re-placed %d member(s) who had been pinned at their licence address"
                     " instead of the address in their profile", moved)
    except Exception:
        db.rollback()
        log.warning("[users] re-placing members at their own address failed", exc_info=True)
    return moved


def sync_grid_member_posts(db) -> int:
    """Re-place the posts of every 'grid' member at their shown subsquare
    centre (sync_post_locations). Runs with every user sync — at startup it
    also repairs posts that grid members wrote before posts honoured map
    precision, which still hold their exact point. Returns posts moved."""
    from app.models import User

    n = 0
    try:
        for u in db.query(User).filter(User.map_precision != "address").all():
            n += sync_post_locations(db, u)
        if n:
            db.commit()
            log.info("[users] re-placed %d post(s) of grid-precision members", n)
    except Exception:
        db.rollback()
        log.warning("[users] grid-precision post sync skipped", exc_info=True)
    return n
