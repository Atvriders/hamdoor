"""FCC ULS whole-database import.

Downloads the weekly amateur radio extract (l_amat.zip, ~200 MB), streams
three of its pipe-delimited member files, and rebuilds the local `hams`
table:

  HD.dat — header:        callsign [4], license_status [5] ('A' = active),
                          radio_service [6] ('HA'/'HV'), grant_date [7],
                          expires [8]
  EN.dat — entity:        callsign [4], name [7]/first [8]/last [10],
                          email [14], street [15], city [16], state [17], zip [18]
  AM.dat — amateur detail: callsign [4], operator_class [5]

Locations come from a GeoNames US ZIP centroid table (no street geocoding),
plus a small deterministic per-callsign offset so hams sharing a ZIP don't
stack into a single map pin (and exact centroids never identify a street).
The table is built as `hams_import` and swapped in atomically, so the site
keeps serving the old data during the multi-minute import.
"""

import hashlib
import logging
import math
import os
import time
import zipfile
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.db import engine

log = logging.getLogger(__name__)

_ACTIVE_STATUS = "A"
_SERVICES = ("HA", "HV")  # amateur, amateur vanity
_CHUNK = 20000

_CREATE_SQL = """
CREATE TABLE hams_import (
    callsign TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    license_class TEXT NOT NULL DEFAULT '',
    expires TEXT NOT NULL DEFAULT '',
    granted TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    loc_source TEXT NOT NULL DEFAULT ''
)"""
_INSERT_SQL = ("INSERT INTO hams_import (callsign, name, street, city, state, zip,"
               " email, license_class, expires, granted, lat, lon, loc_source)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _data_dir() -> str:
    url = get_settings().database_url
    path = url.split("///", 1)[-1] if "///" in url else "/data/hamdoor.db"
    return os.path.dirname(path) or "."


def _marker_path() -> str:
    return os.path.join(_data_dir(), ".uls_last_import")


def last_import() -> datetime | None:
    try:
        with open(_marker_path()) as fh:
            return datetime.fromisoformat(fh.read().strip())
    except (OSError, ValueError):
        return None


def _touch_marker():
    with open(_marker_path(), "w") as fh:
        fh.write(datetime.now(timezone.utc).isoformat())


def _download(url: str, dest: str, max_age_hours: float | None = None):
    if max_age_hours is not None and os.path.exists(dest):
        age_h = (time.time() - os.path.getmtime(dest)) / 3600
        if age_h < max_age_hours:
            log.info("[uls] reusing %s (%.1f h old)", dest, age_h)
            return
    log.info("[uls] downloading %s …", url)
    s = get_settings()
    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True,
                      headers={"User-Agent": s.http_user_agent}) as resp:
        resp.raise_for_status()
        with open(dest + ".partial", "wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    os.replace(dest + ".partial", dest)
    log.info("[uls] saved %s (%.1f MB)", dest, os.path.getsize(dest) / 1e6)


def load_zip_centroids() -> dict[str, tuple[float, float]]:
    """ZIP -> (lat, lon) from the GeoNames US dump (cached in the data dir)."""
    cache = os.path.join(_data_dir(), "uszips.txt")
    if not os.path.exists(cache):
        archive = os.path.join(_data_dir(), "uszips.zip")
        _download(get_settings().geonames_url, archive)
        with zipfile.ZipFile(archive) as zf:
            with zf.open("US.txt") as src, open(cache + ".partial", "wb") as dst:
                dst.write(src.read())
        os.replace(cache + ".partial", cache)
        os.remove(archive)
    table: dict[str, tuple[float, float]] = {}
    with open(cache, encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) > 10 and p[1]:
                try:
                    table[p[1]] = (float(p[9]), float(p[10]))
                except ValueError:
                    continue
    log.info("[uls] %d ZIP centroids loaded", len(table))
    return table


def _rows_from_zip(zip_path: str, name: str):
    """Yield pipe-split fields for each line of a member file (latin-1 safe)."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(name) as fh:
            for raw in fh:
                yield raw.decode("latin-1", errors="replace").rstrip("\r\n").split("|")


def _first_line_fields(row: list[str], idx: int) -> str:
    return row[idx].strip() if len(row) > idx else ""


def jitter_latlon(callsign: str, lat: float, lon: float) -> tuple[float, float]:
    """Deterministic offset within ~4 km of the ZIP centroid, stable per
    callsign across re-imports. Un-stacks same-ZIP pins on the map."""
    h = int.from_bytes(hashlib.sha1(callsign.encode()).digest()[:8], "big")
    frac_r = (h & 0xFFFFFFFF) / 2**32
    frac_a = (h >> 32) / 2**32
    r = 0.035 * math.sqrt(frac_r)          # uniform over the disc, ≤ ~0.035°
    theta = 2 * math.pi * frac_a
    dlat = r * math.cos(theta)
    dlon = r * math.sin(theta) / max(0.2, math.cos(math.radians(lat)))
    return (round(lat + dlat, 5), round(lon + dlon, 5))


def _ensure_geocode_table():
    from app.models import Geocode
    Geocode.__table__.create(engine, checkfirst=True)


def import_hams(zip_path: str | None = None) -> int:
    """Full rebuild of the hams table. Returns the row count imported.

    Locations: Census-geocoded street address when cached, otherwise the
    jittered ZIP centroid. Reuses a recently-downloaded extract (< 20 h)
    so the post-backfill re-import doesn't fetch 200 MB again.
    """
    s = get_settings()
    tmp_zip = zip_path or os.path.join(_data_dir(), "l_amat.zip")
    if zip_path is None:
        _download(s.uls_url, tmp_zip, max_age_hours=20)

    _ensure_geocode_table()
    zips = load_zip_centroids()

    # cached street-level geocodes: address_key -> (lat, lon)
    from app.integrations.census_geocoder import address_key
    geocache: dict[str, tuple[float, float]] = {}
    try:
        with engine.begin() as conn:
            for key, lat, lon in conn.execute(text("SELECT address_key, lat, lon FROM geocodes")):
                geocache[key] = (lat, lon)
    except Exception:
        pass  # geocodes table not created yet
    log.info("[uls] %d cached geocodes", len(geocache))

    # pass 1 — active licenses
    t0 = time.monotonic()
    active: dict[str, tuple[str, str]] = {}  # callsign -> (expires, granted)
    with zipfile.ZipFile(tmp_zip) as zf:
        with zf.open("HD.dat") as fh:
            for raw in fh:
                p = raw.decode("latin-1", errors="replace").rstrip("\r\n").split("|")
                if len(p) > 8 and p[5] == _ACTIVE_STATUS and p[6] in _SERVICES:
                    active[p[4].strip().upper()] = (p[8].strip(), p[7].strip())
    log.info("[uls] %d active licenses (HD pass %.0fs)", len(active), time.monotonic() - t0)

    # pass 2 — entity info
    info: dict[str, list[str]] = {}
    for p in _rows_from_zip(tmp_zip, "EN.dat"):
        cs = _first_line_fields(p, 4).upper()
        if cs not in active:
            continue
        name = " ".join(x for x in (_first_line_fields(p, 8), _first_line_fields(p, 9),
                                    _first_line_fields(p, 10)) if x) or _first_line_fields(p, 7)
        info[cs] = [name.title().strip(", "),
                    _first_line_fields(p, 15), _first_line_fields(p, 16).title(),
                    _first_line_fields(p, 17), _first_line_fields(p, 18)[:5],
                    _first_line_fields(p, 14).lower()]
    log.info("[uls] entity pass done (%.0fs total)", time.monotonic() - t0)

    # pass 3 — operator class
    classes: dict[str, str] = {}
    for p in _rows_from_zip(tmp_zip, "AM.dat"):
        cs = _first_line_fields(p, 4).upper()
        if cs in active and len(p) > 5:
            classes[cs] = p[5].strip()
    log.info("[uls] amateur pass done (%.0fs total)", time.monotonic() - t0)

    # build + swap
    count = 0
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS hams_import"))
        conn.execute(text(_CREATE_SQL))
        chunk = []
        n_addr = n_zip = 0
        for cs, (expires, granted) in active.items():
            e = info.get(cs, ["", "", "", "", "", ""])
            geo = geocache.get(address_key(e[1], e[2], e[3], e[4])) if e[1] else None
            if geo:
                lat, lon, src = geo[0], geo[1], "address"
                n_addr += 1
            else:
                centroid = zips.get(e[4])
                if centroid:
                    lat, lon = jitter_latlon(cs, *centroid)
                    src = "zip"
                    n_zip += 1
                else:
                    lat, lon, src = None, None, ""
            chunk.append((cs, e[0], e[1], e[2], e[3], e[4], e[5],
                          classes.get(cs, ""), expires, granted, lat, lon, src))
            if len(chunk) >= _CHUNK:
                conn.exec_driver_sql(_INSERT_SQL, chunk)
                count += len(chunk)
                chunk = []
        if chunk:
            conn.exec_driver_sql(_INSERT_SQL, chunk)
            count += len(chunk)
        conn.execute(text("DROP TABLE IF EXISTS hams"))
        conn.execute(text("ALTER TABLE hams_import RENAME TO hams"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hams_lat_lon ON hams (lat, lon)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hams_zip ON hams (zip)"))
    log.info("[uls] swapped in %d rows (%d street-level, %d ZIP-level) (%.0fs total)",
             count, n_addr, n_zip, time.monotonic() - t0)

    if zip_path is None and os.path.exists(tmp_zip) and \
            time.time() - os.path.getmtime(tmp_zip) > s.uls_refresh_days * 86400:
        os.remove(tmp_zip)  # stale extract; keep recent ones for re-imports
    _touch_marker()
    return count


def migrate_hams_table():
    """Bring an existing hams table up to the current schema between imports.

    Adding a column this way keeps the site running; the marker file is then
    removed so the scheduler immediately re-imports and populates it.
    """
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(hams)"))]
        if not cols:
            return  # table doesn't exist yet — init_db/create_all handles it
        if "granted" not in cols:
            log.info("[uls] migrating hams table: adding 'granted' column")
            conn.execute(text("ALTER TABLE hams ADD COLUMN granted TEXT NOT NULL DEFAULT ''"))
            try:
                os.remove(_marker_path())  # force re-import to populate it now
            except OSError:
                pass
        if "loc_source" not in cols:
            log.info("[uls] migrating hams table: adding 'loc_source' column")
            conn.execute(text("ALTER TABLE hams ADD COLUMN loc_source TEXT NOT NULL DEFAULT ''"))


def missing_geocode_addresses(limit: int | None = None) -> list[tuple[str, str, str, str, str]]:
    """Distinct addresses from the current hams table that have no cached
    geocode, as (address_key, street, city, state, zip) rows."""
    from app.integrations.census_geocoder import address_key

    _ensure_geocode_table()
    with engine.begin() as conn:
        cached = {r[0] for r in conn.execute(text("SELECT address_key FROM geocodes"))}
        rows = conn.execute(text(
            "SELECT DISTINCT street, city, state, zip FROM hams WHERE street <> ''"
        )).all()
    missing = []
    for street, city, state, zip_ in rows:
        if address_key(street, city, state, zip_) not in cached:
            missing.append((address_key(street, city, state, zip_), street, city, state, zip_))
            if limit and len(missing) >= limit:
                break
    return missing


def geocode_backfill(limit: int | None = None) -> int:
    """Geocode uncached addresses via the Census batch API into the
    persistent geocodes cache. Writes after EVERY batch, so progress is
    visible immediately and survives container restarts."""
    from app.integrations.census_geocoder import geocode_batch

    rows = missing_geocode_addresses(limit)
    if not rows:
        return 0
    log.info("[uls] geocoding %d uncached addresses via Census batch API…", len(rows))

    _ensure_geocode_table()
    added = 0

    def insert_chunk(matched):
        nonlocal added
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT OR REPLACE INTO geocodes (address_key, lat, lon, quality)"
                " VALUES (?, ?, ?, ?)",
                [(k, v[0], v[1], v[2]) for k, v in matched.items()])
        added += len(matched)

    geocode_batch(rows, on_chunk=insert_chunk)
    log.info("[uls] geocode backfill added %d locations", added)
    return added


def ensure_fresh():
    """Keep the hams table current and street-level:

    1. import if the table is empty or stale (uses cached geocodes);
    2. if many addresses lack a geocode, backfill via the Census batch API
       and import again (reusing the just-downloaded extract) so pins move
       from ZIP centroids to street addresses.
    """
    s = get_settings()
    if not s.uls_import_enabled:
        return
    last = last_import()
    if last is not None:
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        if age_days >= s.uls_refresh_days:
            log.info("[uls] data is %.1f days old — refreshing", age_days)
            _run_import()
    else:
        log.info("[uls] no previous import — starting initial import")
        _run_import()

    try:
        missing = len(missing_geocode_addresses(limit=s.geocode_backfill_threshold + 1))
    except Exception:
        missing = 0
    if missing > s.geocode_backfill_threshold:
        try:
            geocode_backfill()
            _run_import()  # re-import with the warm geocode cache
        except Exception:
            log.exception("[uls] geocode backfill failed; will retry at next check")


def _run_import():
    try:
        import_hams()
    except Exception:
        log.exception("[uls] import failed; will retry at next check")
