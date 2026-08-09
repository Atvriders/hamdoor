"""FCC ULS whole-database import.

Downloads the weekly amateur radio extract (l_amat.zip, ~200 MB), streams
three of its pipe-delimited member files, and rebuilds the local `hams`
table:

  HD.dat — header:        callsign [4], license_status [5] ('A' = active),
                          radio_service [6] ('HA'/'HV'), expires [8]
  EN.dat — entity:        callsign [4], name [7]/first [8]/last [10],
                          email [14], street [15], city [16], state [17], zip [18]
  AM.dat — amateur detail: callsign [4], operator_class [5]

Locations come from a GeoNames US ZIP centroid table (no street geocoding).
The table is built as `hams_import` and swapped in atomically, so the site
keeps serving the old data during the multi-minute import.
"""

import logging
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
    lat REAL,
    lon REAL
)"""
_INSERT_SQL = ("INSERT INTO hams_import (callsign, name, street, city, state, zip,"
               " email, license_class, expires, lat, lon)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


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


def _download(url: str, dest: str):
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


def import_hams(zip_path: str | None = None) -> int:
    """Full rebuild of the hams table. Returns the row count imported."""
    s = get_settings()
    tmp_zip = zip_path or os.path.join(_data_dir(), "l_amat.zip")
    if zip_path is None:
        _download(s.uls_url, tmp_zip)

    zips = load_zip_centroids()

    # pass 1 — active licenses
    t0 = time.monotonic()
    active: dict[str, str] = {}  # callsign -> expires
    with zipfile.ZipFile(tmp_zip) as zf:
        with zf.open("HD.dat") as fh:
            for raw in fh:
                p = raw.decode("latin-1", errors="replace").rstrip("\r\n").split("|")
                if len(p) > 8 and p[5] == _ACTIVE_STATUS and p[6] in _SERVICES:
                    active[p[4].strip().upper()] = p[8].strip()
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
        for cs, expires in active.items():
            e = info.get(cs, ["", "", "", "", "", ""])
            latlon = zips.get(e[4])
            chunk.append((cs, e[0], e[1], e[2], e[3], e[4], e[5],
                          classes.get(cs, ""), expires,
                          latlon[0] if latlon else None, latlon[1] if latlon else None))
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
    log.info("[uls] swapped in %d rows (%.0fs total)", count, time.monotonic() - t0)

    if zip_path is None:
        os.remove(tmp_zip)
    _touch_marker()
    return count


def ensure_fresh():
    """Import if the table is empty or the last import is older than the
    configured refresh period. Called by the scheduler and at startup."""
    s = get_settings()
    if not s.uls_import_enabled:
        return
    last = last_import()
    if last is not None:
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        if age_days < s.uls_refresh_days:
            return
        log.info("[uls] data is %.1f days old — refreshing", age_days)
    else:
        log.info("[uls] no previous import — starting initial import")
    try:
        import_hams()
    except Exception:
        log.exception("[uls] import failed; will retry at next check")
