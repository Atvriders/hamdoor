"""Public FCC ULS directory endpoints — every active US ham on the map.

Street addresses and emails are never exposed here: a ham is a coordinate
(the Census street geocode, or the ZIP anchor shared by every ZIP-level ham
of that ZIP), a name, a city and a state."""

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.geo import EARTH_RADIUS_MILES, cell_degrees, clamp_bbox, haversine_miles
from app.integrations import uls
from app.models import Ham, User
from app.schemas import normalize_callsign
from app.security import get_current_user

router = APIRouter(prefix="/api/hams", tags=["hams"])

DETAIL_ZOOM = 10  # individual points from here in (unless the view is too dense)
CLASS_KEYS = ("E", "A", "G", "T", "N", "U")
SHARED_MIN = 10  # a street point with this many licensees is a shared address
# Cluster cells stay cell_degrees(zoom) up to this many in the snapped bbox —
# a 5K screen spans ~9k at zoom 10–13, ~1.5k at zoom 9 — and a bbox far
# larger than any screen at that zoom gets the next coarser size that fits
# instead (a world bbox at z13 was 431k cells, 18.5 MB, 5 s and +300 MB for
# one request).
MAX_CELLS = 10_000
_CELL_LADDER = tuple(sorted({cell_degrees(z) for z in range(0, 23)}))


def _today() -> date:
    return date.today()


def is_expired(expires: str, today: date | None = None) -> bool:
    """ULS keeps licenses with a past expiration date in 'active' status
    through the 2-year renewal grace period — flag those as expired."""
    try:
        return datetime.strptime(expires, "%m/%d/%Y").date() < (today or _today())
    except (ValueError, TypeError):
        return False


def is_new_ham(granted: str, today: date | None = None) -> bool:
    """Granted within the configured 'new ham' window (default 90 days)."""
    try:
        age = ((today or _today()) - datetime.strptime(granted, "%m/%d/%Y").date()).days
        return 0 <= age <= get_settings().new_ham_days
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class MapFilters:
    classes: frozenset[str] | None  # None = every class
    expired: bool  # include licenses in the post-expiry grace period
    new: bool  # include hams granted within new_ham_days
    approx: bool  # include ZIP-level hams
    today: date
    new_days: int

    @property
    def new_from(self) -> date:
        return self.today - timedelta(days=self.new_days)

    def is_expired(self, expires_iso: str, expires: str) -> bool:
        if expires_iso:
            return expires_iso < self.today.isoformat()
        return is_expired(expires, self.today)

    def is_new(self, granted_iso: str, granted: str) -> bool:
        if granted_iso:
            return self.new_from.isoformat() <= granted_iso <= self.today.isoformat()
        return is_new_ham(granted, self.today)

    def passes(self, expired_now: bool, new_now: bool) -> bool:
        return (self.expired or not expired_now) and (self.new or not new_now)


def parse_classes(classes: str | None) -> frozenset[str] | None:
    """CSV of E,A,G,T,N,U. Absent = all; given but naming no class = none."""
    if classes is None:
        return None
    picked = frozenset(c.strip().upper() for c in classes.split(",")) & set(CLASS_KEYS)
    return None if picked == set(CLASS_KEYS) else picked


def _class_sql(classes: frozenset[str] | None) -> str | None:
    """Row filter on hams.license_class (codes come from a fixed whitelist)."""
    if classes is None:
        return None
    parts = []
    std = [c for c in ("E", "A", "G", "T") if c in classes]
    if std:
        parts.append("license_class IN (%s)" % ", ".join(f"'{c}'" for c in std))
    if "N" in classes:
        parts.append("license_class IN ('N', 'P')")
    if "U" in classes:
        parts.append("license_class NOT IN ('E', 'A', 'G', 'T', 'N', 'P')")
    return "(" + " OR ".join(parts) + ")" if parts else "0"


def _row_filter_sql(f: MapFilters) -> tuple[str, dict]:
    """' AND …' clause applying the filters to rows of hams."""
    conds, params = [], {}
    cls = _class_sql(f.classes)
    if cls:
        conds.append(cls)
    if not f.approx:
        conds.append("loc_source = 'address'")
    if not f.expired:
        conds.append("NOT (expires_iso <> '' AND expires_iso < :f_today)")
        params["f_today"] = f.today.isoformat()
    if not f.new:
        conds.append("NOT (granted_iso <> '' AND granted_iso >= :f_nfrom"
                     " AND granted_iso <= :f_today)")
        params["f_today"] = f.today.isoformat()
        params["f_nfrom"] = f.new_from.isoformat()
    return "".join(f" AND {c}" for c in conds), params


def _finite_bbox(min_lat: float, max_lat: float, min_lon: float, max_lon: float):
    """Out-of-range edges are clamped (clamp_bbox); NaN means unbounded and
    ±inf becomes a large finite value for clamp_bbox's world-span test. Every
    edge is then pinned into range: clamp_bbox leaves the inner edge of a box
    lying wholly beyond it (1e300..1e300) untouched, and floor(1e300 / cell)
    is too large for SQLite."""
    def fix(v: float, default: float) -> float:
        if math.isnan(v):
            return default
        return math.copysign(1000.0, v) if math.isinf(v) else v

    def pin(v: float, limit: float) -> float:
        return max(-limit, min(limit, v))

    lat0, lat1, lon0, lon1 = clamp_bbox(fix(min_lat, -90.0), fix(max_lat, 90.0),
                                        fix(min_lon, -180.0), fix(max_lon, 180.0))
    return pin(lat0, 90.0), pin(lat1, 90.0), pin(lon0, 180.0), pin(lon1, 180.0)


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise HTTPException(422, f"expected 1/0/true/false, got {v!r}")


def _json(body: dict) -> Response:
    """Map payloads reach ~2 MB at the point cap: serialize directly and
    compactly (FastAPI's jsonable_encoder walk alone took longer than the
    query)."""
    return Response(json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    media_type="application/json")


def _table_exists(db: Session, name: str) -> bool:
    return db.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"),
                      {"n": name}).first() is not None


# ---------------------------------------------------------------------------
# clusters
# ---------------------------------------------------------------------------

def _cells_from_aggregate(db: Session, cell: float, rng: tuple[int, int, int, int],
                          f: MapFilters, by_cls: dict | None = None) -> dict | None:
    """Cluster sums from the import-time aggregate; None when it can't serve
    this request exactly (missing/stale, or too old to correct). When
    `by_cls` is given it receives the per-class-bucket totals of the cells
    (before the expired/new status correction, which can shift a few)."""
    if not _table_exists(db, "hams_cluster_meta"):
        return None
    meta = db.execute(text("SELECT built_on, new_days, valid FROM hams_cluster_meta"
                           " WHERE id = 1")).first()
    if meta is None or not meta.valid:
        return None
    built_on = date.fromisoformat(meta.built_on)
    corrected = not (f.expired and f.new)
    if corrected:
        age = (f.today - built_on).days
        if age < 0 or age > uls.AGGREGATE_CORRECTION_DAYS or meta.new_days != f.new_days:
            return None
    bl0, bl1, bo0, bo1 = rng
    if f.classes is not None and not f.classes:
        return {}
    where = ["cell = :c", "bl BETWEEN :bl0 AND :bl1", "bo BETWEEN :bo0 AND :bo1"]
    where += _bucket_filters(f)
    acc: dict[tuple[int, int], list] = {}
    for bl, bo, e0, n0, cls, n, slat, slon in db.execute(text(
            "SELECT bl, bo, e0, n0, cls, SUM(n), SUM(slat), SUM(slon) FROM hams_clusters"
            f" WHERE {' AND '.join(where)} GROUP BY bl, bo, e0, n0, cls"),
            {"c": cell, "bl0": bl0, "bl1": bl1, "bo0": bo0, "bo1": bo1}):
        if f.passes(bool(e0), bool(n0)):
            a = acc.setdefault((bl, bo), [0, 0.0, 0.0])
            a[0] += n
            a[1] += slat
            a[2] += slon
            if by_cls is not None:
                by_cls[cls] = by_cls.get(cls, 0) + n
    if corrected and f.today > built_on:
        _correct_for_status_changes(db, cell, rng, f, built_on, acc)
    return acc


def _bucket_filters(f: MapFilters) -> list[str]:
    """Static filters on the derived tables' class bucket / approx columns."""
    where = []
    if f.classes is not None:
        where.append("cls IN (%s)" % ", ".join(f"'{c}'" for c in sorted(f.classes)))
    if not f.approx:
        where.append("approx = 0")
    return where


def _correct_for_status_changes(db: Session, cell: float, rng, f: MapFilters,
                                built_on: date, acc: dict):
    """Exact correction of the aggregate's build-date expired/new snapshot:
    only licenses that expired, aged out of 'new', or reached a future-dated
    grant since then can change status — the import lists those candidates
    in hams_changes (a few % of hams)."""
    d0, d = built_on.isoformat(), f.today.isoformat()
    nfrom0 = (built_on - timedelta(days=f.new_days)).isoformat()
    nfrom = f.new_from.isoformat()
    where = " AND ".join(["((expires_iso >= :d0 AND expires_iso < :d)"
                          " OR (granted_iso >= :nfrom0 AND granted_iso < :nfrom)"
                          " OR (granted_iso > :d0 AND granted_iso <= :d))"] + _bucket_filters(f))
    bl0, bl1, bo0, bo1 = rng
    rows = db.execute(text(f"SELECT lat, lon, expires_iso, granted_iso FROM hams_changes WHERE {where}"),
                      {"d0": d0, "d": d, "nfrom0": nfrom0, "nfrom": nfrom})
    for lat, lon, exp, gr in rows:
        bl, bo = math.floor(lat / cell), math.floor(lon / cell)
        if not (bl0 <= bl <= bl1 and bo0 <= bo <= bo1):
            continue
        was = f.passes(bool(exp) and exp < d0, bool(gr) and nfrom0 <= gr <= d0)
        now = f.passes(bool(exp) and exp < d, bool(gr) and nfrom <= gr <= d)
        if was == now:
            continue
        sign = 1 if now else -1
        a = acc.setdefault((bl, bo), [0, 0.0, 0.0])
        a[0] += sign
        a[1] += sign * lat
        a[2] += sign * lon


def _cells_live(db: Session, cell: float, rng: tuple[int, int, int, int], f: MapFilters,
                by_cls: dict | None = None) -> dict:
    where, params = _row_filter_sql(f)
    bl0, bl1, bo0, bo1 = rng
    eps = cell * 1e-6  # prefilter on degrees; the cell test below is exact
    params.update({"c": cell, "bl0": bl0, "bl1": bl1, "bo0": bo0, "bo1": bo1,
                   "la0": bl0 * cell - eps, "la1": (bl1 + 1) * cell + eps,
                   "lo0": bo0 * cell - eps, "lo1": (bo1 + 1) * cell + eps})
    acc = {}
    for bl, bo, cls, n, slat, slon in db.execute(text(f"""
            SELECT bl, bo, cls, COUNT(*), SUM(lat), SUM(lon) FROM (
                SELECT {uls.floor_sql('lat', 'c')} AS bl, {uls.floor_sql('lon', 'c')} AS bo, lat, lon,
                       {uls.CLASS_BUCKET_SQL} AS cls
                FROM hams
                WHERE lat >= :la0 AND lat <= :la1 AND lon >= :lo0 AND lon <= :lo1{where})
            WHERE bl BETWEEN :bl0 AND :bl1 AND bo BETWEEN :bo0 AND :bo1
            GROUP BY bl, bo, cls"""), params):
        a = acc.setdefault((bl, bo), [0, 0.0, 0.0])
        a[0] += n
        a[1] += slat
        a[2] += slon
        if by_cls is not None:
            by_cls[cls] = by_cls.get(cls, 0) + n
    return acc


def _snapped_cells(bbox, cell: float) -> int:
    """Number of cells in the bbox snapped outward to whole cells."""
    return ((math.floor(bbox[1] / cell) - math.floor(bbox[0] / cell) + 1)
            * (math.floor(bbox[3] / cell) - math.floor(bbox[2] / cell) + 1))


def _cluster_cell(bbox, zoom: int) -> float:
    """Cell size for a cluster response: cell_degrees(zoom), or the finest
    coarser size on the ladder that keeps the snapped bbox within MAX_CELLS
    — bounds the work and the payload whatever bbox a client sends. Below
    the detail zoom every candidate size is in the aggregate."""
    for cell in _CELL_LADDER:
        if cell >= cell_degrees(zoom) and _snapped_cells(bbox, cell) <= MAX_CELLS:
            return cell
    return _CELL_LADDER[-1]


def _clusters(db: Session, bbox, zoom: int, f: MapFilters, reason: str, total: int) -> dict:
    """§1 cluster response. Bubbles sit at the mean position of their hams
    (never the cell centre); the bbox is snapped outward to whole cells so
    edge bubbles count their whole cell and stay put while panning, so the
    cells may sum to more than `total` (the hams inside the requested bbox)."""
    cell = _cluster_cell(bbox, zoom)
    rng = (math.floor(bbox[0] / cell), math.floor(bbox[1] / cell),
           math.floor(bbox[2] / cell), math.floor(bbox[3] / cell))
    acc = None
    by_cls: dict[str, int] = {}
    if cell in uls.CLUSTER_CELLS:
        acc = _cells_from_aggregate(db, cell, rng, f, by_cls)
    if acc is None:
        by_cls = {}
        acc = _cells_live(db, cell, rng, f, by_cls)
    cells = [{"lat": round(slat / n, 5), "lon": round(slon / n, 5), "count": n}
             for _, (n, slat, slon) in sorted(acc.items()) if n > 0]
    return {"type": "clusters", "zoom": zoom, "cell_degrees": cell,
            "total": total, "reason": reason, "cells": cells,
            "by_class": _by_class(by_cls)}


def _by_class(counts: dict) -> dict:
    """Per-class-bucket counts (E, A, G, T, N, U) for the legend chips. For
    clusters they cover the cells (the view snapped outward), so they are
    "about" figures; for points they are exact."""
    return {k: int(counts.get(k, 0)) for k in ("E", "A", "G", "T", "N", "U")}


def fcc_badges(db: Session, callsigns) -> dict[str, tuple[str, bool]]:
    """{callsign: (license_class, new)} from the FCC directory — the class
    badge and new-ham star of members and post authors. Callsigns without a
    directory row are left out (callers show "" / false)."""
    calls = sorted({c for c in callsigns if c})
    out: dict[str, tuple[str, bool]] = {}
    for i in range(0, len(calls), 500):  # stay far below SQLite's parameter limit
        for cs, cls, granted in db.execute(select(Ham.callsign, Ham.license_class, Ham.granted)
                                           .where(Ham.callsign.in_(calls[i:i + 500]))):
            out[cs] = (cls or "", is_new_ham(granted or ""))
    return out


# ---------------------------------------------------------------------------
# licences within a radius (GET /api/hams/near-count)
# ---------------------------------------------------------------------------

# The haversine test of app.geo.haversine_miles in plain SQL arithmetic (no
# reliance on SQLite's optional math functions). With u = Δφ/2 and v = Δλ/2
# in radians (row minus centre):
#   h = sin²u + cos φ1 · cos φ2 · sin²v,  cos φ2 = cos φ1 · cos 2u − sin φ1 · sin 2u,
# and a point is within r exactly when h <= sin²(r / 2R). sin² and sin are
# Taylor series in Horner form; for |u| <= 0.064 (r <= 500 mi) and a cap at
# most 45° of longitude wide (|v| <= 0.393) their truncation error is below
# 1e-7 of h — under a millimetre at 500 mi.
_U = "((lat - :la) * :hp)"
_V = "((lon - :lo) * :hp)"
_U2 = f"({_U} * {_U})"
_V2 = f"({_V} * {_V})"
_SIN2_U = f"({_U2} * (1 - {_U2} * (0.3333333333333333 - {_U2} * 0.044444444444444446)))"
_SIN2_V = (f"({_V2} * (1 - {_V2} * (0.3333333333333333 - {_V2} * (0.044444444444444446"
           f" - {_V2} * 0.0031746031746031746))))")
_SIN_2U = f"(2 * {_U} * (1 - {_U2} * (0.6666666666666666 - {_U2} * 0.13333333333333333)))"
_HAV_SQL = f"({_SIN2_U} + :c1 * (:c1 * (1 - 2 * {_SIN2_U}) - :s1 * {_SIN_2U}) * {_SIN2_V})"
# a cheap stand-in for h (cos φ2 ≈ cos φ1, sin x ≈ x): rows it places clearly
# inside or outside skip the exact test (~90 % of them at 500 mi on the replica)
_EST_SQL = f"({_U2} + :c1sq * {_V2})"
_NEAR_COUNT_SQL = (
    "SELECT COUNT(*) FROM hams WHERE lat BETWEEN :lat0 AND :lat1 AND lon BETWEEN :lon0 AND :lon1"
    f" AND CASE WHEN {_EST_SQL} <= :est_in THEN 1 WHEN {_EST_SQL} > :est_out THEN 0"
    f" ELSE {_HAV_SQL} <= :hav END")
_NEAR_MAX_DLON = 45.0  # widest cap (degrees of longitude either side) the series cover


def count_hams_within(db: Session, lat: float, lon: float, radius_miles: float) -> int:
    """Licensees whose map point (street geocode or ZIP anchor) lies within
    radius_miles of (lat, lon): exactly haversine_miles(...) <= radius_miles,
    the test the Feed and Nearby use. Index range scan on the cap's bounding
    box; the cheap estimate decides most rows, provably (bounds below), and
    the exact series decides the rest. ~170 ms for 500 mi around New York on
    the 823k-row replica. A cap reaching past the series' range (within ~10°
    of a pole) is counted row by row in Python — no US licensee lives there."""
    theta = radius_miles / EARTH_RADIUS_MILES  # angular radius
    hav = math.sin(theta / 2) ** 2
    dlat = math.degrees(theta) * (1 + 1e-9) + 1e-9
    lat0, lat1 = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    c1, s1, sin_t = math.cos(math.radians(lat)), math.sin(math.radians(lat)), math.sin(theta)
    if c1 <= sin_t or math.degrees(math.asin(sin_t / c1)) > _NEAR_MAX_DLON:
        rows = db.execute(text("SELECT lat, lon FROM hams WHERE lat BETWEEN :a AND :b"),
                          {"a": lat0, "b": lat1})
        return sum(1 for la, lo in rows if haversine_miles(lat, lon, la, lo) <= radius_miles)
    # the cap's true longitude half-width (wider than r / cos φ1 away from the equator)
    dlon = math.degrees(math.asin(sin_t / c1)) * (1 + 1e-9) + 1e-9
    # est = u² + cos²φ1·v² brackets h on the cap: sin²x lies in [x²(1 − x²/3), x²]
    # and cos φ2 between its extremes on [lat0, lat1], so k_lo·est <= h <= k_hi·est
    umax, vmax = math.radians(dlat) / 2, math.radians(dlon) / 2
    c2_max = math.cos(math.radians(min(max(0.0, lat0), lat1)))
    c2_min = min(math.cos(math.radians(lat0)), math.cos(math.radians(lat1)))
    k_hi = max(1.0, c2_max / c1)
    k_lo = min(1 - umax * umax / 3, c2_min / c1 * (1 - vmax * vmax / 3))
    params = {"lat0": lat0, "lat1": lat1, "la": lat, "hp": math.pi / 360, "c1": c1, "s1": s1,
              "c1sq": c1 * c1, "est_in": hav / k_hi, "est_out": hav / k_lo, "hav": hav}
    # the cap may cross the antimeridian: count the far side with a shifted centre
    ranges = [(max(-180.0, lon - dlon), min(180.0, lon + dlon), lon)]
    if lon - dlon < -180.0:
        ranges.append((lon - dlon + 360.0, 180.0, lon + 360.0))
    if lon + dlon > 180.0:
        ranges.append((-180.0, lon + dlon - 360.0, lon - 360.0))
    return sum(db.scalar(text(_NEAR_COUNT_SQL), dict(params, lon0=lo0, lon1=lo1, lo=ref)) or 0
               for lo0, lo1, ref in ranges)


def _members_shown_on_map(db: Session) -> set[str]:
    """Callsigns whose FCC directory entry may say "registered": members
    who keep the default 'address' map precision. A 'grid' member's public
    FCC dot must not reveal that they have an account."""
    return {r[0] for r in db.execute(text(
        "SELECT callsign FROM users WHERE map_precision = 'address'"))}


# ---------------------------------------------------------------------------
# endpoints (static paths before /{callsign})
# ---------------------------------------------------------------------------

@router.get("/count")
def hams_count(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    last = uls.last_import()
    by_source = dict(
        db.execute(text("SELECT loc_source, COUNT(*) FROM hams GROUP BY loc_source")).all()
    )
    geocodes = db.scalar(text("SELECT COUNT(*) FROM geocodes")) if _table_exists(db, "geocodes") else 0
    misses = db.scalar(text("SELECT COUNT(*) FROM geocode_misses")) \
        if _table_exists(db, "geocode_misses") else 0
    return {
        "count": db.scalar(select(func.count()).select_from(Ham)) or 0,
        "street_level": by_source.get("address", 0),
        "zip_level": by_source.get("zip", 0),
        "unplaced": db.scalar(text("SELECT COUNT(*) FROM hams WHERE lat IS NULL")) or 0,
        "geocodes_cached": geocodes or 0,
        "geocode_misses": misses or 0,
        "last_import": last.isoformat() if last else None,
        "source": "FCC ULS weekly amateur extract",
    }


@router.get("/near-count")
def hams_near_count(
    radius_miles: float | None = Query(default=None, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """How many licensees' map points lie within radius_miles (default: the
    member's range) of the member's stored location — every placed licence,
    expired or new ones included, the member's own too. Exact: the
    great-circle test the Feed and Nearby use (count_hams_within)."""
    if user.lat is None or user.lon is None or not (math.isfinite(user.lat) and math.isfinite(user.lon)):
        raise HTTPException(400, "set your location (grid square or address) in your profile first")
    r = float(radius_miles) if radius_miles is not None else float(min(500, max(1, user.range_miles or 1)))
    return {"count": count_hams_within(db, user.lat, user.lon, r),
            "radius_miles": int(r) if r.is_integer() else r}


@router.get("/map")
def hams_map(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    zoom: int,
    classes: str | None = None,
    expired: str | None = None,
    new: str | None = None,
    approx: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Clusters below zoom 10 (or when a view holds more than
    HAMS_MAP_MAX_RESULTS hams), otherwise one point per distinct location
    with every ham there. Out-of-range bounds are clamped, never rejected.
    `total` is always the exact number of filtered hams inside the
    requested bbox (an index-only count: ~120 ms for all of CONUS)."""
    s = get_settings()
    zoom = max(0, min(22, zoom))
    bbox = _finite_bbox(min_lat, max_lat, min_lon, max_lon)
    f = MapFilters(classes=parse_classes(classes), expired=_bool(expired, True),
                   new=_bool(new, True), approx=_bool(approx, True),
                   today=_today(), new_days=s.new_ham_days)
    where, params = _row_filter_sql(f)
    params.update({"a": bbox[0], "b": bbox[1], "c": bbox[2], "d": bbox[3]})
    in_bbox = "lat >= :a AND lat <= :b AND lon >= :c AND lon <= :d"
    total = db.scalar(text(f"SELECT COUNT(*) FROM hams WHERE {in_bbox}{where}"), params) or 0
    if zoom < DETAIL_ZOOM:
        return _json(_clusters(db, bbox, zoom, f, "zoom", total))
    if total > s.hams_map_max_results:
        return _json(_clusters(db, bbox, zoom, f, "dense", total))

    registered = _members_shown_on_map(db)
    today_iso, nfrom_iso = f.today.isoformat(), f.new_from.isoformat()
    points: dict[tuple, dict] = {}
    by_cls: dict[str, int] = {}
    for (callsign, name, city, state, zip_, cls, expires, granted, exp_iso, gr_iso,
         lat, lon, loc_source) in db.execute(text(
            "SELECT callsign, name, city, state, zip, license_class, expires, granted,"
            " expires_iso, granted_iso, lat, lon, loc_source"
            f" FROM hams WHERE {in_bbox}{where} ORDER BY callsign"), params):
        loc = "address" if loc_source == "address" else "zip"
        key = (round(lat, 6), round(lon, 6), loc, (zip_ or None) if loc == "zip" else None)
        p = points.get(key)
        if p is None:
            p = points[key] = {"lat": key[0], "lon": key[1], "loc": loc, "zip": key[3],
                               "count": 0, "shared": False, "hams": []}
        p["count"] += 1
        bucket = cls if cls in ("E", "A", "G", "T") else ("N" if cls in ("N", "P") else "U")
        by_cls[bucket] = by_cls.get(bucket, 0) + 1
        p["hams"].append({
            "callsign": callsign, "name": name, "city": city, "state": state,
            "license_class": cls,
            "expired": exp_iso < today_iso if exp_iso else is_expired(expires, f.today),
            "new": nfrom_iso <= gr_iso <= today_iso if gr_iso else is_new_ham(granted, f.today),
            "granted": granted,
            "registered": callsign in registered,
        })
    out = sorted(points.values(), key=lambda p: (p["lat"], p["lon"], p["loc"], p["zip"] or ""))
    for p in out:
        p["shared"] = p["loc"] == "address" and p["count"] >= SHARED_MIN
    return _json({"type": "points", "zoom": zoom, "total": total, "points": out,
                  "by_class": _by_class(by_cls)})


@router.get("/search")
def hams_search(q: str = "", limit: int = 10, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Callsign prefix search: exact match first, then shortest, then A–Z."""
    qn = re.sub(r"[^A-Za-z0-9]", "", q).upper()
    if len(qn) < 2:
        return {"results": []}
    limit = max(1, min(25, limit))
    f = MapFilters(classes=None, expired=True, new=True, approx=True, today=_today(),
                   new_days=get_settings().new_ham_days)
    rows = db.execute(text(
        "SELECT callsign, name, city, state, lat, lon, loc_source, license_class,"
        " expires, granted, expires_iso, granted_iso FROM hams"
        " WHERE callsign >= :q AND callsign < :q_end"
        " ORDER BY callsign = :q DESC, length(callsign), callsign LIMIT :lim"),
        {"q": qn, "q_end": qn + "\x7f", "lim": limit}).all()
    calls = [r.callsign for r in rows]
    registered = set()
    if calls:
        registered = set(db.scalars(select(User.callsign).where(
            User.callsign.in_(calls), User.map_precision == "address")))
    return {"results": [{
        "callsign": r.callsign, "name": r.name, "city": r.city, "state": r.state,
        "lat": r.lat, "lon": r.lon,
        "loc": None if r.lat is None else ("address" if r.loc_source == "address" else "zip"),
        "license_class": r.license_class,
        "expired": f.is_expired(r.expires_iso, r.expires),
        "new": f.is_new(r.granted_iso, r.granted),
        "registered": r.callsign in registered,
    } for r in rows]}


@router.get("/{callsign}")
def ham_detail(callsign: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        cs = normalize_callsign(callsign)
    except ValueError:
        raise HTTPException(404, f"{callsign.upper()} not found in the FCC database")
    ham = db.get(Ham, cs)
    if ham is None:
        raise HTTPException(404, f"{cs} not found in the FCC database")
    loc = None if ham.lat is None else ("address" if ham.loc_source == "address" else "zip")
    return {
        "callsign": ham.callsign,
        "name": ham.name,
        "city": ham.city,
        "state": ham.state,
        "zip": ham.zip,
        "license_class": ham.license_class,
        "expires": ham.expires,
        "expired": is_expired(ham.expires),
        "granted": ham.granted,
        "new": is_new_ham(ham.granted),
        "lat": ham.lat,
        "lon": ham.lon,
        "loc": loc,
        "registered": db.scalar(select(func.count()).select_from(User).where(
            User.callsign == cs, User.map_precision == "address")) > 0,
    }
