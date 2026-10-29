"""FCC ULS whole-database import.

Downloads the weekly amateur radio extract (l_amat.zip, ~200 MB), streams
three of its pipe-delimited member files, and rebuilds the local `hams`
table:

  HD.dat — header:        unique_system_identifier [1], callsign [4],
                          license_status [5] ('A' = active), radio_service [6]
                          ('HA'/'HV'), grant_date [7], expires [8]
  EN.dat — entity:        usi [1], callsign [4], name [7]/first [8]/last [10],
                          email [14], street [15], city [16], state [17], zip [18]
  AM.dat — amateur detail: usi [1], callsign [4], operator_class [5]

The extract carries every license ever issued, so a recycled callsign has
EN/AM rows from its previous holders. Rows are joined on the active HD row's
unique_system_identifier, never on the callsign alone.

Locations: the Census street geocode of the FCC address when one is cached
and trustworthy, otherwise the ZIP anchor — the median of the ZIP's
street-geocoded hams (≥ 5) or the GeoNames centroid. Every ZIP-level ham of a
ZIP sits on the same anchor; the map groups them into one marker.

The table is built as `hams_import` and swapped in atomically together with
the derived tables (places, zip_anchors, hams_clusters), so the site keeps
serving the old data during the import.
"""

import logging
import math
import os
import sqlite3
import statistics
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.geo import cell_degrees
from app.textcase import from_capitals, place_key, tidy_case

log = logging.getLogger(__name__)

_ACTIVE_STATUS = "A"
_SERVICES = ("HA", "HV")  # amateur, amateur vanity
_CHUNK = 50000  # rows per staging / merge transaction (~1 s of write lock each)
_VET_PAGE = 50000  # geocodes read per statement when vetting Non_Exact matches
# pause between the importer's transactions: SQLite's busy handler polls
# every 100 ms, so without a gap the importer re-takes the lock first and a
# waiting signup times out after 5 s
_LOCK_GAP_SECONDS = 0.25
# GeoNames ships the territories outside US.zip (US.txt has no 006xx–009xx)
GEONAMES_COUNTRIES = ("US", "PR", "GU", "VI", "MP", "AS")
# a ZIP's anchor is the median of its street-geocoded hams when it has this many
ZIP_ANCHOR_MIN_STREET = 5
# cluster cell sizes served from the aggregate table (zoom 0–9)
CLUSTER_CELLS = tuple(sorted({cell_degrees(z) for z in range(0, 10)}))
# the map corrects the aggregate's build-date expired/new snapshot for this
# many days; older aggregates fall back to the live query for those filters
AGGREGATE_CORRECTION_DAYS = 60

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
    loc_source TEXT NOT NULL DEFAULT '',
    expires_iso TEXT NOT NULL DEFAULT '',
    granted_iso TEXT NOT NULL DEFAULT ''
)"""
_INSERT_SQL = ("INSERT INTO hams_import (callsign, name, street, city, state, zip,"
               " email, license_class, expires, granted, lat, lon, loc_source,"
               " expires_iso, granted_iso)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
# indexes of the hams table (map bbox queries are index-only with every
# filter). They are built on the staging table before the swap, so their
# names alternate between imports ("ix_hams_map" / "ix_hams_map_b").
_HAMS_INDEXES = (
    ("ix_hams_map", "(lat, lon, license_class, loc_source, expires_iso, granted_iso)"),
    ("ix_hams_zip", "(zip)"),
)
_CLUSTERS_TABLE_SQL = (
    "CREATE TABLE {name} (cell REAL NOT NULL, bl INTEGER NOT NULL, bo INTEGER NOT NULL,"
    " cls TEXT NOT NULL, approx INTEGER NOT NULL, e0 INTEGER NOT NULL, n0 INTEGER NOT NULL,"
    " n INTEGER NOT NULL, slat REAL NOT NULL, slon REAL NOT NULL,"
    " PRIMARY KEY (cell, bl, bo, cls, approx, e0, n0)) WITHOUT ROWID")
# placed licenses whose expired/new status can change within the correction
# window: the map's correction of the aggregate reads these, not all of hams
_CHANGES_TABLE_SQL = (
    "CREATE TABLE {name} (lat REAL NOT NULL, lon REAL NOT NULL, cls TEXT NOT NULL,"
    " approx INTEGER NOT NULL, expires_iso TEXT NOT NULL, granted_iso TEXT NOT NULL)")
# any write to hams outside an import makes the cluster aggregate stale
_AGG_TRIGGERS = tuple(
    f"CREATE TRIGGER IF NOT EXISTS hams_agg_{op.lower()} AFTER {op} ON hams"
    " BEGIN UPDATE hams_cluster_meta SET valid = 0 WHERE valid = 1; END"
    for op in ("INSERT", "UPDATE", "DELETE"))

# license class -> map filter bucket (N = Novice incl. old P; U = club/unknown)
CLASS_BUCKET_SQL = ("CASE WHEN license_class IN ('E','A','G','T') THEN license_class"
                    " WHEN license_class IN ('N','P') THEN 'N' ELSE 'U' END")


def floor_sql(col: str, param: str) -> str:
    """floor(col / :param) for SQLite builds without math functions."""
    return (f"(CAST({col} / :{param} AS INTEGER)"
            f" - ({col} / :{param} < CAST({col} / :{param} AS INTEGER)))")


def _data_dir() -> str:
    url = get_settings().database_url
    path = url.split("///", 1)[-1] if "///" in url else "/data/hamdoor.db"
    return os.path.dirname(path) or "."


def _marker_path() -> str:
    return os.path.join(_data_dir(), ".uls_last_import")


def _extract_marker_path() -> str:
    """The FCC's Last-Modified date of the extract the last import read."""
    return os.path.join(_data_dir(), ".uls_extract")


def _release_marker_path() -> str:
    return os.path.join(_data_dir(), ".data_release_fetched")


def _release_matched_marker_path() -> str:
    """Written once a release carrying Census matched addresses was merged
    (older releases lack them, so their Non_Exact rows can't be vetted)."""
    return os.path.join(_data_dir(), ".data_release_matched")


def _read_marker(path: str) -> datetime | None:
    try:
        with open(path) as fh:
            dt = datetime.fromisoformat(fh.read().strip())
    except (OSError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _write_marker(path: str, when: datetime | None = None):
    with open(path, "w") as fh:
        fh.write((when or datetime.now(timezone.utc)).isoformat())


def last_import() -> datetime | None:
    return _read_marker(_marker_path())


def _touch_marker():
    _write_marker(_marker_path())


def _utc_stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_date(value: str | None) -> datetime | None:
    """An HTTP date header ("Sun, 25 Oct 2026 13:10:40 GMT") as an aware
    datetime; None when missing or unreadable."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _download(url: str, dest: str, max_age_hours: float | None = None) -> datetime | None:
    """Fetch url to dest. Returns the reply's Last-Modified date; None when
    the server sent none, or when a recent copy was reused instead."""
    if max_age_hours is not None and os.path.exists(dest):
        age_h = (time.time() - os.path.getmtime(dest)) / 3600
        if age_h < max_age_hours:
            log.info("[uls] reusing %s (%.1f h old)", dest, age_h)
            return None
    log.info("[uls] downloading %s …", url)
    s = get_settings()
    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True,
                      headers={"User-Agent": s.http_user_agent}) as resp:
        resp.raise_for_status()
        modified = _http_date(resp.headers.get("last-modified"))
        with open(dest + ".partial", "wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    os.replace(dest + ".partial", dest)
    log.info("[uls] saved %s (%.1f MB)", dest, os.path.getsize(dest) / 1e6)
    return modified


def published_extract() -> datetime | None:
    """When the FCC last published the weekly extract (it does on Sundays):
    the Last-Modified date of a HEAD request for ULS_URL. None when the
    request fails or the header is missing or unreadable."""
    s = get_settings()
    try:
        resp = httpx.head(s.uls_url, timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": s.http_user_agent})
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        log.warning("[uls] could not ask the FCC when its extract was published: %s", exc)
        return None
    published = _http_date(resp.headers.get("last-modified"))
    if published is None:
        log.info("[uls] the FCC's extract has no Last-Modified date")
    return published


# ---------------------------------------------------------------------------
# GeoNames postal codes (US + territories)
# ---------------------------------------------------------------------------

def _geonames_cache(cc: str) -> str:
    # US keeps its historical cache name so existing data dirs reuse it
    return os.path.join(_data_dir(), "uszips.txt" if cc == "US" else f"zips_{cc}.txt")


def _geonames_url(cc: str) -> str:
    base = get_settings().geonames_url
    return base if cc == "US" else f"{base.rsplit('/', 1)[0]}/{cc}.zip"


def _fetch_geonames(cc: str) -> str:
    cache = _geonames_cache(cc)
    if not os.path.exists(cache):
        archive = cache + ".zip"
        _download(_geonames_url(cc), archive)
        try:
            with zipfile.ZipFile(archive) as zf:
                with zf.open(f"{cc}.txt") as src, open(cache + ".partial", "wb") as dst:
                    dst.write(src.read())
            os.replace(cache + ".partial", cache)
        finally:
            if os.path.exists(archive):
                os.remove(archive)
    return cache


def load_gazetteer() -> list[tuple[str, str, str, float, float, int | None]]:
    """GeoNames postal codes as (zip, place, state, lat, lon, accuracy) rows,
    US.txt plus the PR/GU/VI/MP/AS files (cached in the data dir).

    Territory files put a numeric code in the admin1 column, so their
    "state" is the country code itself (PR, GU, ...). A territory that
    can't be fetched is skipped (retried at the next import); US is required.
    """
    rows: list[tuple[str, str, str, float, float, int | None]] = []
    for cc in GEONAMES_COUNTRIES:
        try:
            path = _fetch_geonames(cc)
        except Exception:
            if cc == "US":
                raise
            log.warning("[uls] GeoNames %s unavailable — its ZIPs stay unplaced until the next import",
                        cc, exc_info=True)
            continue
        n = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                p = line.rstrip("\n").split("\t")
                if len(p) < 11 or not p[1]:
                    continue
                try:
                    lat, lon = float(p[9]), float(p[10])
                except ValueError:
                    continue
                state = (p[4] if p[0] == "US" else p[0]).strip().upper()
                acc = p[11].strip() if len(p) > 11 else ""
                rows.append((p[1].strip(), p[2].strip(), state, lat, lon,
                             int(acc) if acc.isdigit() else None))
                n += 1
        log.info("[uls] GeoNames %s: %d postal codes", cc, n)
    return rows


def load_zip_centroids() -> dict[str, tuple[float, float]]:
    """ZIP -> (lat, lon) GeoNames centroid (first entry wins)."""
    table: dict[str, tuple[float, float]] = {}
    for z, _, _, lat, lon, _ in load_gazetteer():
        table.setdefault(z, (lat, lon))
    return table


# ---------------------------------------------------------------------------
# schema (raw SQL: these tables are rebuilt or migrated outside the ORM)
# ---------------------------------------------------------------------------

def _ensure_aux_tables(conn):
    """geocodes (+ matched_address, source), geocode_misses, places,
    zip_anchors and the cluster aggregate. Idempotent."""
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS geocodes (address_key VARCHAR(400) NOT NULL PRIMARY KEY,"
        " lat FLOAT NOT NULL, lon FLOAT NOT NULL, quality VARCHAR(16) NOT NULL DEFAULT '',"
        " matched_address TEXT, source TEXT)"))
    gcols = {r[1] for r in conn.execute(text("PRAGMA table_info(geocodes)"))}
    for col in ("matched_address", "source"):
        if col not in gcols:
            log.info("[uls] migrating geocodes table: adding '%s' column", col)
            conn.execute(text(f"ALTER TABLE geocodes ADD COLUMN {col} TEXT"))
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS geocode_misses (address_key TEXT PRIMARY KEY,"
        " status TEXT NOT NULL DEFAULT '', tries INTEGER NOT NULL DEFAULT 0,"
        " last_try TEXT NOT NULL DEFAULT '')"))
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS places (zip TEXT PRIMARY KEY, place TEXT NOT NULL,"
        " state TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, accuracy INTEGER,"
        " key TEXT NOT NULL DEFAULT '')"))
    if "key" not in {r[1] for r in conn.execute(text("PRAGMA table_info(places)"))}:
        # place search matches this spelling-blind key (textcase.place_key)
        log.info("[uls] migrating places table: adding 'key' column")
        conn.execute(text("ALTER TABLE places ADD COLUMN key TEXT NOT NULL DEFAULT ''"))
        rows = conn.execute(text("SELECT zip, place FROM places")).all()
        if rows:
            conn.execute(text("UPDATE places SET key = :key WHERE zip = :zip"),
                         [{"zip": z, "key": place_key(p)} for z, p in rows])
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_places_key ON places (key)"))
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS zip_anchors (zip TEXT PRIMARY KEY, lat REAL NOT NULL,"
        " lon REAL NOT NULL, source TEXT NOT NULL, n_street INTEGER NOT NULL DEFAULT 0)"))
    conn.execute(text(_CLUSTERS_TABLE_SQL.format(name="IF NOT EXISTS hams_clusters")))
    conn.execute(text(_CHANGES_TABLE_SQL.format(name="IF NOT EXISTS hams_changes")))
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS hams_cluster_meta (id INTEGER PRIMARY KEY CHECK (id = 1),"
        " built_on TEXT NOT NULL, new_days INTEGER NOT NULL, valid INTEGER NOT NULL)"))


def _ensure_geocode_table():
    with engine.begin() as conn:
        _ensure_aux_tables(conn)


def fill_iso_dates(conn=None):
    """Derive expires_iso/granted_iso from the FCC MM/DD/YYYY columns where
    missing (tables from older versions, rows written outside an import)."""
    def run(c):
        for col in ("expires", "granted"):
            c.execute(text(
                f"UPDATE hams SET {col}_iso = substr({col},7,4)||'-'||substr({col},1,2)||'-'||substr({col},4,2)"
                f" WHERE {col}_iso = '' AND {col} GLOB '[01][0-9]/[0-3][0-9]/[12][0-9][0-9][0-9]'"))
    if conn is not None:
        run(conn)
    else:
        with engine.begin() as c:
            run(c)


def migrate_users_table(bind=None):
    """Add users.map_precision, users.location_source and
    users.must_change_password to databases created before they existed
    (create_all only adds missing tables). Idempotent; every existing member
    keeps the default 'address' (where they are shown today), gets a
    best-effort location_source once, when the column is added
    (guess_location_sources), and is not asked to change their password.
    Once per database (PRAGMA user_version 1), names and addresses stored in
    the callsign provider's capitals are put in normal case
    (recase_member_capitals)."""
    with (bind or engine).begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)"))]
        if cols and "map_precision" not in cols:
            log.info("[users] migrating users table: adding 'map_precision' column")
            conn.execute(text("ALTER TABLE users ADD COLUMN map_precision TEXT NOT NULL"
                              " DEFAULT 'address'"))
        if cols and "location_source" not in cols:
            log.info("[users] migrating users table: adding 'location_source' column")
            conn.execute(text("ALTER TABLE users ADD COLUMN location_source TEXT"))
            n = guess_location_sources(conn)
            log.info("[users] location_source guessed for %d existing member(s)", n)
        if cols and "must_change_password" not in cols:
            log.info("[users] migrating users table: adding 'must_change_password' column")
            conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL"
                              " DEFAULT 0"))
        # one-off data fixes, counted in SQLite's user_version
        done = conn.execute(text("PRAGMA user_version")).scalar() or 0
        if cols and done < 1:
            if {"name", "address_line", "city"} <= set(cols):
                n = recase_member_capitals(conn)
                if n:
                    log.info("[users] %d member(s): name, street or city from the callsign provider's"
                             " capitals now in normal case", n)
            conn.execute(text("PRAGMA user_version = 1"))


def recase_member_capitals(conn) -> int:
    """Members' name, street and city as sign-up and Update from FCC stored
    them from the callsign provider, in its capitals ("NEWINGTON", "225 MAIN
    STREET", "ARRL HQ OPERATORS CLUB"), in normal case like the FCC
    directory's (acronyms kept). Text with a lower-case letter in it was typed
    by the member and is left alone. Returns the members changed."""
    changed = []
    for uid, name, street, city in conn.execute(text("SELECT id, name, address_line, city FROM users")).all():
        values = [name, street, city]
        fixed = [from_capitals(name or ""), from_capitals(street or ""), from_capitals(city or "", place=True)]
        if fixed != [v or "" for v in values]:
            changed.append({"id": uid, "name": fixed[0], "street": fixed[1], "city": fixed[2]})
    if changed:
        conn.execute(text("UPDATE users SET name = :name, address_line = :street, city = :city"
                          " WHERE id = :id"), changed)
    return len(changed)


# a stored point "equals" a source's point within this many degrees (~1 cm):
# both were copied from the same float, this only absorbs formatting
_SAME_POINT_DEG = 1e-7


def guess_location_sources(conn) -> int:
    """One-off fill for members placed before location_source existed: a
    stored point equal to their hams row's street geocode -> 'licence';
    equal to their grid-square centre -> 'grid'; anything else stays NULL
    (provider, Nominatim and ZIP points can't be told apart afterwards).
    Returns members attributed."""
    from app.geo import grid_to_latlon
    from app.integrations.geocode import same_address, _norm_text

    ucols = {r[1] for r in conn.execute(text("PRAGMA table_info(users)"))}
    if not {"callsign", "lat", "lon", "grid", "location_source"} <= ucols:
        return 0
    hcols = {r[1] for r in conn.execute(text("PRAGMA table_info(hams)"))}
    has_point = {"lat", "lon", "loc_source"} <= hcols
    # both tables have address columns: a member is 'licence' only when their
    # profile is the licence's address (otherwise the point can't tell)
    has_addr = has_point and {"street", "city", "state", "zip"} <= hcols \
        and {"address_line", "city", "state", "zip"} <= ucols
    street = ("h.lat, h.lon" if has_point else "NULL, NULL") + \
        (", h.street, h.city, h.state, h.zip" if has_addr else ", '', '', '', ''")
    join = "LEFT JOIN hams AS h ON h.callsign = u.callsign AND h.loc_source = 'address'" if has_point else ""
    profile = ", ".join(f"COALESCE(u.{c}, '')" for c in ("address_line", "city", "state", "zip")) \
        if has_addr else "'', '', '', ''"

    def same(a_lat, a_lon, b) -> bool:
        return b is not None and None not in b and \
            abs(a_lat - b[0]) <= _SAME_POINT_DEG and abs(a_lon - b[1]) <= _SAME_POINT_DEG

    def fcc_profile(mine, fcc) -> bool:
        # geocode.profile_is_fcc_address on raw rows: a street-less FCC row
        # (PO box) matches on city, state and ZIP
        if not _norm_text(fcc[0]):
            mine, fcc = ("",) + tuple(mine[1:]), ("",) + tuple(fcc[1:])
        return same_address(tuple(mine), tuple(fcc))

    guessed = []
    for row in conn.execute(text(
            f"SELECT u.id, u.lat, u.lon, u.grid, {street}, {profile} FROM users AS u {join}"
            " WHERE u.lat IS NOT NULL AND u.lon IS NOT NULL AND u.location_source IS NULL")).all():
        uid, lat, lon, grid, h_lat, h_lon = row[:6]
        fcc, mine = row[6:10], row[10:14]
        if same(lat, lon, (h_lat, h_lon)):
            # a member who had edited their address was still pinned at the
            # licence by older versions: not 'licence' — left unattributed
            # for place_mispinned_members to re-place at their own address
            if not has_addr or fcc_profile(mine, fcc):
                guessed.append({"id": uid, "src": "licence"})
        elif grid and same(lat, lon, grid_to_latlon(grid)):
            guessed.append({"id": uid, "src": "grid"})
    if guessed:
        conn.execute(text("UPDATE users SET location_source = :src WHERE id = :id"), guessed)
    return len(guessed)


def migrate_posts_table(bind=None):
    """Add the event columns (posts.event_at, posts.repeats_weekly) to
    databases created before they existed. Idempotent; existing posts have
    no event."""
    with (bind or engine).begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(posts)"))]
        if cols and "event_at" not in cols:
            log.info("[posts] migrating posts table: adding 'event_at' column")
            conn.execute(text("ALTER TABLE posts ADD COLUMN event_at TEXT"))
        if cols and "repeats_weekly" not in cols:
            log.info("[posts] migrating posts table: adding 'repeats_weekly' column")
            conn.execute(text("ALTER TABLE posts ADD COLUMN repeats_weekly INTEGER NOT NULL DEFAULT 0"))


def migrate_hams_table():
    """Bring existing tables up to the current schema between imports — the
    one schema hook the app runs at startup, so it also migrates the users
    and posts tables (migrate_users_table, migrate_posts_table) before
    anything reads them.

    Adding a column this way keeps the site running. A hams table without
    the ISO date columns predates the USI-keyed join and ZIP anchors, so the
    import marker is removed and the scheduler re-imports right away.
    """
    migrate_users_table()
    migrate_posts_table()
    with engine.begin() as conn:
        _ensure_aux_tables(conn)
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(hams)"))]
        if not cols:
            return  # table doesn't exist yet — init_db/create_all handles it
        force_reimport = False
        if "granted" not in cols:
            log.info("[uls] migrating hams table: adding 'granted' column")
            conn.execute(text("ALTER TABLE hams ADD COLUMN granted TEXT NOT NULL DEFAULT ''"))
            force_reimport = True
        if "loc_source" not in cols:
            log.info("[uls] migrating hams table: adding 'loc_source' column")
            conn.execute(text("ALTER TABLE hams ADD COLUMN loc_source TEXT NOT NULL DEFAULT ''"))
        for col in ("expires_iso", "granted_iso"):
            if col not in cols:
                log.info("[uls] migrating hams table: adding '%s' column", col)
                conn.execute(text(f"ALTER TABLE hams ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"))
                force_reimport = True
        if force_reimport:
            fill_iso_dates(conn)
            n = conn.execute(text("SELECT COUNT(*) FROM hams")).scalar() or 0
            if n:
                try:
                    os.remove(_marker_path())  # re-import now to rebuild locations
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def _first_line_fields(row: list[str], idx: int) -> str:
    return row[idx].strip() if len(row) > idx else ""


def _rows_from_zip(zip_path: str, name: str):
    """Yield pipe-split fields for each line of a member file (latin-1 safe)."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(name) as fh:
            for raw in fh:
                yield raw.decode("latin-1", errors="replace").rstrip("\r\n").split("|")


_ISO_CACHE: dict[str, str] = {}
_CASE_CACHE: dict[tuple, str] = {}


def _recase(s: str, kind: str = "name", region: str = "", inner: bool = False) -> str:
    """A name or city field in normal case (textcase.tidy_case): the FCC's
    capitals ("ARRL HQ OPERATORS CLUB" -> "ARRL HQ Operators Club", "PINE GRV
    MLS" -> "Pine Grv Mls"), and what a licensee typed all in lower case or in
    odd case ("taylorville", "ChARLES"); the rest of what a licensee wrote in
    mixed case is kept. First names and cities repeat: memoised."""
    key = (s, kind, region, inner)
    out = _CASE_CACHE.get(key)
    if out is None:
        out = tidy_case(s, kind, region, inner)
        if len(_CASE_CACHE) < 200_000:
            _CASE_CACHE[key] = out
    return out


def iso_date(mdy: str) -> str:
    """FCC 'MM/DD/YYYY' -> 'YYYY-MM-DD'; '' when unparsable."""
    iso = _ISO_CACHE.get(mdy)
    if iso is None:
        try:
            iso = datetime.strptime(mdy, "%m/%d/%Y").date().isoformat()
        except ValueError:
            iso = ""
        if len(_ISO_CACHE) < 100_000:
            _ISO_CACHE[mdy] = iso
    return iso


def read_licenses(zip_path: str):
    """Active licenses joined on unique_system_identifier.

    Returns (active, info, classes, stats): active = {callsign: (usi, expires,
    granted)}, info = {callsign: (name, street, city, state, zip, email)},
    classes = {callsign: operator class}. EN/AM rows of other licenses that
    once held the same callsign are skipped (stats counts them).
    """
    t0 = time.monotonic()
    active: dict[str, tuple[str, str, str]] = {}
    for p in _rows_from_zip(zip_path, "HD.dat"):
        if len(p) > 8 and p[5] == _ACTIVE_STATUS and p[6] in _SERVICES:
            active[p[4].strip().upper()] = (p[1].strip(), p[8].strip(), p[7].strip())
    log.info("[uls] %d active licenses (HD pass %.0fs)", len(active), time.monotonic() - t0)

    info: dict[str, tuple[str, str, str, str, str, str]] = {}
    stale_en = 0
    for p in _rows_from_zip(zip_path, "EN.dat"):
        cs = _first_line_fields(p, 4).upper()
        lic = active.get(cs)
        if lic is None:
            continue
        if _first_line_fields(p, 1) != lic[0]:
            stale_en += 1  # a previous holder's license of this callsign
            continue
        state = _first_line_fields(p, 17).upper()
        # first name, middle initial and surname (which may start "de", "van"), else the club's name
        name = " ".join(x for x in (_recase(_first_line_fields(p, 8)), _recase(_first_line_fields(p, 9), inner=True),
                                    _recase(_first_line_fields(p, 10), inner=True)) if x) \
            or _recase(_first_line_fields(p, 7), "org")
        info[cs] = (name.strip(", "),
                    _first_line_fields(p, 15), _recase(_first_line_fields(p, 16), "place", state),
                    state, _first_line_fields(p, 18)[:5],
                    _first_line_fields(p, 14).lower())
    log.info("[uls] entity pass done (%.0fs total, %d rows of previous licenses skipped)",
             time.monotonic() - t0, stale_en)

    classes: dict[str, str] = {}
    stale_am = 0
    for p in _rows_from_zip(zip_path, "AM.dat"):
        cs = _first_line_fields(p, 4).upper()
        lic = active.get(cs)
        if lic is None or len(p) <= 5:
            continue
        if _first_line_fields(p, 1) != lic[0]:
            stale_am += 1
            continue
        classes[cs] = p[5].strip()
    log.info("[uls] amateur pass done (%.0fs total)", time.monotonic() - t0)
    return active, info, classes, {"stale_en_rows": stale_en, "stale_am_rows": stale_am}


def load_accepted_geocodes() -> tuple[dict[str, tuple[float, float]], dict[str, int]]:
    """Street geocodes trusted for map pins: address_key -> (lat, lon).

    Exact matches and rows without a stored matched address (old releases)
    are accepted as before. A Non_Exact match is accepted only when Census's
    matched address agrees with the input (the key holds the normalized
    input) on state, house number and street name.

    Rows are read a page at a time (rowid order, the table's own) and vetted
    with no statement open: an open read holds SQLite's shared lock, and
    writers (signups, posts) can't commit until it ends — vetting 100k
    Non_Exact rows takes ~5 s.
    """
    from app.integrations.census_geocoder import matched_address_agrees, split_address_key

    accepted: dict[str, tuple[float, float]] = {}
    stats = {"geocodes": 0, "non_exact_checked": 0, "non_exact_rejected": 0}
    _ensure_geocode_table()
    conn = _connect()
    try:
        last = 0
        while True:
            rows = conn.execute(
                "SELECT rowid, address_key, lat, lon, quality, matched_address FROM geocodes"
                " WHERE rowid > ? ORDER BY rowid LIMIT ?", (last, _VET_PAGE)).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            for _, key, lat, lon, quality, matched in rows:
                stats["geocodes"] += 1
                if quality == "Non_Exact" and matched:
                    stats["non_exact_checked"] += 1
                    street, city, state, zip_ = split_address_key(key)
                    if not matched_address_agrees(street, city, state, zip_, matched):
                        stats["non_exact_rejected"] += 1
                        continue
                accepted[key] = (lat, lon)
    finally:
        conn.close()
    return accepted, stats


# a street point shared by this many licensees is a mail drop, a club or a
# PO box service: it says nothing about where the ZIP's hams live
SHARED_POINT_MIN = 10
# a ZIP anchor keeps this far from every street-level licence, so its dashed
# group never sits on (and takes the clicks of) someone's home
ANCHOR_CLEARANCE_M = 25.0
_ANCHOR_NUDGES_M = (30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 240.0)
_M_PER_DEG = 111_320.0


def _shared_points(points) -> set[tuple[float, float]]:
    """Points that SHARED_POINT_MIN or more licensees share."""
    return {p for p, n in Counter(points).items() if n >= SHARED_POINT_MIN}


def _state_agrees(licence_state: str, zip_info) -> bool:
    """False when the ZIP's GeoNames state (zip_info[2]) and the licence's
    state are both known and differ: an FCC ZIP typo (Stonington ME with a
    Virgin Islands ZIP) must not move a licence to another state."""
    if not zip_info or len(zip_info) < 3:
        return True
    ls, zs = (licence_state or "").strip().upper(), (zip_info[2] or "").strip().upper()
    return not ls or not zs or ls == zs


class _PointIndex:
    """Street points on a ~110 m grid, for "is anything within d metres"."""

    _CELL = 0.001

    def __init__(self, points):
        self._cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for lat, lon in set(points):
            self._cells.setdefault(self._key(lat, lon), []).append((lat, lon))

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self._CELL), math.floor(lon / self._CELL)

    def near(self, lat: float, lon: float, metres: float) -> bool:
        kx = math.cos(math.radians(lat))
        cy, cx = self._key(lat, lon)
        reach = 1 + int(metres / (self._CELL * _M_PER_DEG * max(kx, 0.05)))
        for i in range(cy - 1, cy + 2):
            for j in range(cx - reach, cx + reach + 1):
                for plat, plon in self._cells.get((i, j), ()):
                    dy = (plat - lat) * _M_PER_DEG
                    dx = (plon - lon) * _M_PER_DEG * kx
                    if dx * dx + dy * dy < metres * metres:
                        return True
        return False

    def clear_spot(self, lat: float, lon: float) -> tuple[float, float]:
        """(lat, lon) itself when no street point is within
        ANCHOR_CLEARANCE_M, else the nearest of a few nudged spots (N, NE,
        E, ... at growing distances) that is; unchanged if none is."""
        if not self.near(lat, lon, ANCHOR_CLEARANCE_M):
            return lat, lon
        kx = max(math.cos(math.radians(lat)), 0.05)
        for d in _ANCHOR_NUDGES_M:
            for b in range(0, 360, 45):
                nlat = lat + d * math.cos(math.radians(b)) / _M_PER_DEG
                nlon = lon + d * math.sin(math.radians(b)) / (_M_PER_DEG * kx)
                if not self.near(nlat, nlon, ANCHOR_CLEARANCE_M):
                    return nlat, nlon
        return lat, lon


def compute_zip_anchors(street_points: dict[str, tuple[list[float], list[float]]],
                        centroids: dict[str, tuple[float, float]],
                        all_points=None) -> dict[str, tuple[float, float, str, int]]:
    """ZIP -> (lat, lon, source, n_street). Coordinate-wise median of the
    ZIP's distinct street-geocoded points when it has ≥ ZIP_ANCHOR_MIN_STREET
    of them (robust to the odd typo'd ZIP; a point many licensees share
    counts once), else the GeoNames centroid. With `all_points` (every
    street-level licence point) the anchor is then kept ANCHOR_CLEARANCE_M
    from all of them. Rounded to 5 decimals (~1 m): an area marker, never
    mistaken for a street pin."""
    index = _PointIndex(all_points) if all_points else None

    def placed(lat: float, lon: float) -> tuple[float, float]:
        if index is not None:
            lat, lon = index.clear_spot(lat, lon)
        return round(lat, 5), round(lon, 5)

    anchors: dict[str, tuple[float, float, str, int]] = {}
    for z, (lats, lons) in street_points.items():
        distinct = set(zip(lats, lons))
        if len(distinct) >= ZIP_ANCHOR_MIN_STREET:
            lat, lon = placed(statistics.median(p[0] for p in distinct),
                              statistics.median(p[1] for p in distinct))
            anchors[z] = (lat, lon, "median", len(lats))
    for z, (lat, lon) in centroids.items():
        if z not in anchors:
            anchors[z] = (*placed(lat, lon), "geonames", len(street_points.get(z, ((), ()))[0]))
    return anchors


def _db_path() -> str:
    return get_settings().database_url.split("///", 1)[-1]


@contextmanager
def _txn(conn: sqlite3.Connection, fast_free: bool = False):
    """One explicit write transaction on the importer's own connection.

    pysqlite only opens transactions before DML, so DDL (DROP/RENAME) needs
    an explicit BEGIN to be atomic. Every step of the import is kept short:
    other writers (signups, posts) wait at most SQLite's 5 s busy timeout.
    fast_free: freed pages are not zero-filled — SQLITE_SECURE_DELETE builds
    (Debian/Ubuntu) otherwise rewrite ~200 MB when the old hams table is
    dropped, 20 s of database lock for public FCC data.
    """
    conn.execute("BEGIN IMMEDIATE")
    prev = None
    if fast_free:
        prev = conn.execute("PRAGMA secure_delete").fetchone()[0]
        conn.execute("PRAGMA secure_delete = OFF")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:  # SQLite may already have rolled back (e.g. disk full)
            conn.execute("ROLLBACK")
        raise
    finally:
        if prev is not None:
            conn.execute(f"PRAGMA secure_delete = {int(prev)}")


def _connect() -> sqlite3.Connection:
    """The importer's own connection: explicit transactions (see _txn) and
    a longer wait for request handlers' short writes."""
    return sqlite3.connect(_db_path(), timeout=30, isolation_level=None)


def _let_writers_in():
    time.sleep(_LOCK_GAP_SECONDS)


def _aggregate_sql(source: str, target: str) -> str:
    """Per-cell sums keyed by class bucket, approx (ZIP-level) and the
    expired / new status *as of the build date*; the map endpoint corrects
    for licenses whose status changed since then."""
    return f"""
        INSERT INTO {target} (cell, bl, bo, cls, approx, e0, n0, n, slat, slon)
        SELECT :c, bl, bo, cls, approx, e0, n0, COUNT(*), SUM(lat), SUM(lon) FROM (
            SELECT {floor_sql('lat', 'c')} AS bl, {floor_sql('lon', 'c')} AS bo,
                   {CLASS_BUCKET_SQL} AS cls,
                   (loc_source <> 'address') AS approx,
                   (expires_iso <> '' AND expires_iso < :d0) AS e0,
                   (granted_iso <> '' AND granted_iso >= :nfrom AND granted_iso <= :d0) AS n0,
                   lat, lon
            FROM {source} WHERE lat IS NOT NULL)
        GROUP BY bl, bo, cls, approx, e0, n0"""


def _aggregate_params(built_on: date, new_days: int, cell: float) -> dict:
    return {"c": cell, "d0": built_on.isoformat(),
            "nfrom": (built_on - timedelta(days=new_days)).isoformat()}


def _changes_sql(source: str, target: str) -> str:
    """Licenses that expire, age out of 'new', or reach a future-dated grant
    within AGGREGATE_CORRECTION_DAYS of the build date (a few % of hams)."""
    return f"""
        INSERT INTO {target} (lat, lon, cls, approx, expires_iso, granted_iso)
        SELECT lat, lon, {CLASS_BUCKET_SQL}, (loc_source <> 'address'), expires_iso, granted_iso
        FROM {source}
        WHERE lat IS NOT NULL AND ((expires_iso >= :d0 AND expires_iso <= :d_end)
              OR (granted_iso >= :nfrom AND granted_iso <= :nfrom_end) OR granted_iso > :d0)"""


def _changes_params(built_on: date, new_days: int) -> dict:
    window = timedelta(days=AGGREGATE_CORRECTION_DAYS + 1)
    nfrom = built_on - timedelta(days=new_days)
    return {"d0": built_on.isoformat(), "d_end": (built_on + window).isoformat(),
            "nfrom": nfrom.isoformat(), "nfrom_end": (nfrom + window).isoformat()}


def _activate_aggregate(conn: sqlite3.Connection, built_on: date, new_days: int):
    conn.execute("INSERT OR REPLACE INTO hams_cluster_meta (id, built_on, new_days, valid)"
                 " VALUES (1, ?, ?, 1)", (built_on.isoformat(), new_days))
    for sql in _AGG_TRIGGERS:
        conn.execute(sql)


def rebuild_cluster_aggregate(built_on: date | None = None):
    """Rebuild the cluster aggregate from the current hams table (normally
    built by import_hams from the staging table, before the swap).

    Staged like an import: each cell size is summed into a side table in its
    own short transaction (~2 s on the full table) and swapped in by rename,
    so requests that write keep getting in. Nothing but an import writes the
    hams table, so it can't change between the sums and the swap."""
    built_on = built_on or date.today()
    new_days = get_settings().new_ham_days
    _ensure_geocode_table()
    conn = _connect()
    try:
        with _txn(conn, fast_free=True):
            for t in ("hams_clusters_rebuild", "hams_changes_rebuild"):
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute(_CLUSTERS_TABLE_SQL.format(name="hams_clusters_rebuild"))
            conn.execute(_CHANGES_TABLE_SQL.format(name="hams_changes_rebuild"))
        with _txn(conn):
            conn.execute(_changes_sql("hams", "hams_changes_rebuild"), _changes_params(built_on, new_days))
        for cell in CLUSTER_CELLS:
            _let_writers_in()
            with _txn(conn):
                conn.execute(_aggregate_sql("hams", "hams_clusters_rebuild"),
                             _aggregate_params(built_on, new_days, cell))
        _let_writers_in()
        with _txn(conn, fast_free=True):
            conn.execute("DROP TABLE IF EXISTS hams_clusters")
            conn.execute("ALTER TABLE hams_clusters_rebuild RENAME TO hams_clusters")
            conn.execute("DROP TABLE IF EXISTS hams_changes")
            conn.execute("ALTER TABLE hams_changes_rebuild RENAME TO hams_changes")
            _activate_aggregate(conn, built_on, new_days)
    finally:
        conn.close()


def derived_tables_due() -> tuple[bool, bool, bool]:
    """(cluster aggregate, places, zip_anchors) that need building for a
    hams table that has rows: a database upgraded from a version without
    them serves the zoomed-out map from a ~5 s live scan, and an empty place
    search, until its first import — forever when imports are switched off.
    The aggregate is also due when the map could no longer correct it (older
    than AGGREGATE_CORRECTION_DAYS, or another NEW_HAM_DAYS)."""
    _ensure_geocode_table()
    with engine.begin() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        if "hams" not in tables or conn.execute(
                text("SELECT 1 FROM hams WHERE lat IS NOT NULL LIMIT 1")).first() is None:
            return False, False, False
        meta = conn.execute(text("SELECT built_on, new_days, valid FROM hams_cluster_meta"
                                 " WHERE id = 1")).first()
        places_due = conn.execute(text("SELECT 1 FROM places LIMIT 1")).first() is None
        anchors_due = conn.execute(text("SELECT 1 FROM zip_anchors LIMIT 1")).first() is None
    agg_due = meta is None or not meta.valid
    if not agg_due:
        try:
            age = (date.today() - date.fromisoformat(meta.built_on)).days
        except (TypeError, ValueError):
            age = None
        agg_due = age is None or age < 0 or age > AGGREGATE_CORRECTION_DAYS - 7 \
            or meta.new_days != get_settings().new_ham_days
    return agg_due, places_due, anchors_due


PLACES_INSERT = ("INSERT OR IGNORE INTO places (zip, place, state, lat, lon, accuracy, key)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)")


def places_rows(gazetteer) -> list[tuple]:
    """GeoNames rows (zip, place, state, lat, lon, accuracy) with the key
    place search matches on (textcase.place_key)."""
    return [(*row, place_key(row[1])) for row in gazetteer]


def _replace_places(conn: sqlite3.Connection, gazetteer) -> None:
    conn.execute("DELETE FROM places")
    conn.executemany(PLACES_INSERT, places_rows(gazetteer))


def _fill_places_and_anchors(places: bool, anchors: bool) -> None:
    """places from the GeoNames files (downloaded once, kept in the data
    dir) and ZIP anchors from the street-level rows already in hams."""
    gazetteer = load_gazetteer()
    zip_anchors: dict[str, tuple[float, float, str, int]] = {}
    if anchors:
        centroids: dict[str, tuple[float, float, str]] = {}
        for z, _, st, lat, lon, _ in gazetteer:
            centroids.setdefault(z, (lat, lon, st))
        street_points: dict[str, tuple[list[float], list[float]]] = {}
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT zip, lat, lon, state FROM hams WHERE loc_source = 'address' AND zip <> ''"
                " AND lat IS NOT NULL")).all()
        shared = _shared_points((r[1], r[2]) for r in rows)
        for z, lat, lon, st in rows:
            if (lat, lon) in shared or not _state_agrees(st, centroids.get(z)):
                continue
            lats, lons = street_points.setdefault(z, ([], []))
            lats.append(lat)
            lons.append(lon)
        zip_anchors = compute_zip_anchors(street_points, {z: c[:2] for z, c in centroids.items()},
                                          [(r[1], r[2]) for r in rows])
    conn = _connect()
    try:
        with _txn(conn):
            if places:
                _replace_places(conn, gazetteer)
            if anchors:
                conn.execute("DELETE FROM zip_anchors")
                conn.executemany("INSERT INTO zip_anchors (zip, lat, lon, source, n_street)"
                                 " VALUES (?, ?, ?, ?, ?)", [(z, *a) for z, a in zip_anchors.items()])
    finally:
        conn.close()


def ensure_derived_tables() -> bool:
    """Build whatever derived_tables_due() names. Returns True when it did
    anything. Failures are logged (the next start tries again)."""
    try:
        agg, places, anchors = derived_tables_due()
    except Exception:
        log.warning("[uls] could not check the derived tables", exc_info=True)
        return False
    did = False
    if agg:
        t0 = time.monotonic()
        try:
            rebuild_cluster_aggregate()
            did = True
            log.info("[uls] cluster aggregate built from the existing hams table (%.0fs)",
                     time.monotonic() - t0)
        except Exception:
            log.warning("[uls] could not build the cluster aggregate", exc_info=True)
    if places or anchors:
        try:
            _fill_places_and_anchors(places, anchors)
            did = True
            log.info("[uls] filled %s from the GeoNames files and the hams table",
                     " and ".join(n for n, due in (("places", places), ("ZIP anchors", anchors)) if due))
        except Exception:
            log.warning("[uls] could not fill places / ZIP anchors (next start retries)", exc_info=True)
    return did


def _index_suffix(conn: sqlite3.Connection) -> str:
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    for suffix in ("", "_b"):
        if not any(name + suffix in existing for name, _ in _HAMS_INDEXES):
            return suffix
    raise RuntimeError("[uls] both index name sets are in use")


def import_hams(zip_path: str | None = None) -> int:
    """Full rebuild of the hams table and its derived tables. Returns the row
    count imported. Reuses a recently-downloaded extract (< 20 h) so the
    post-backfill re-import doesn't fetch 200 MB again.

    Everything is staged (hams_import, its indexes, hams_clusters_import) in
    short transactions while the site keeps serving the old tables, then
    swapped in by rename in one brief transaction.
    """
    from app.integrations.census_geocoder import address_key

    s = get_settings()
    tmp_zip = zip_path or os.path.join(_data_dir(), "l_amat.zip")
    extract_date = None
    if zip_path is None:
        extract_date = _download(s.uls_url, tmp_zip, max_age_hours=20)

    _ensure_geocode_table()
    gazetteer = load_gazetteer()
    zip_info: dict[str, tuple[float, float, str]] = {}
    for z, _, st, lat, lon, _ in gazetteer:
        zip_info.setdefault(z, (lat, lon, st))
    centroids = {z: c[:2] for z, c in zip_info.items()}
    geocache, gstats = load_accepted_geocodes()
    log.info("[uls] %d cached geocodes, %d trusted (%d Non_Exact rejected: matched another street)",
             gstats["geocodes"], len(geocache), gstats["non_exact_rejected"])

    t0 = time.monotonic()
    active, info, classes, jstats = read_licenses(tmp_zip)

    def street_point(e) -> tuple[float, float] | None:
        return geocache.get(address_key(e[1], e[2], e[3], e[4])) if e[1] else None

    # ZIP anchors from the street-level hams of each ZIP — leaving out mail
    # drops (points many licensees share) and ZIPs typed for another state
    located = []
    for cs in active:
        e = info.get(cs)
        if e is None:
            continue
        pt = street_point(e)
        if pt and e[4]:
            located.append((e[4], e[3], pt))
    shared = _shared_points(pt for _, _, pt in located)
    street_points: dict[str, tuple[list[float], list[float]]] = {}
    for z, st, pt in located:
        if pt in shared or not _state_agrees(st, zip_info.get(z)):
            continue
        lats, lons = street_points.setdefault(z, ([], []))
        lats.append(pt[0])
        lons.append(pt[1])
    anchors = compute_zip_anchors(street_points, centroids, [pt for _, _, pt in located])
    del street_points, located, shared

    counts = {"address": 0, "zip": 0}
    blank = ("", "", "", "", "", "")

    def rows():
        for cs, (_, expires, granted) in active.items():
            e = info.get(cs, blank)
            pt = street_point(e)
            if pt:
                lat, lon, src = pt[0], pt[1], "address"
            else:
                # a ZIP from another state is a typo: unplaced, not 4,000 km off
                a = anchors.get(e[4]) if _state_agrees(e[3], zip_info.get(e[4])) else None
                lat, lon, src = (a[0], a[1], "zip") if a else (None, None, "")
            if src:
                counts[src] += 1
            yield (cs, e[0], e[1], e[2], e[3], e[4], e[5], classes.get(cs, ""), expires, granted,
                   lat, lon, src, iso_date(expires), iso_date(granted))

    built_on, new_days = date.today(), s.new_ham_days
    conn = _connect()
    try:
        with _txn(conn, fast_free=True):
            for t in ("hams_import", "hams_clusters_import", "hams_changes_import"):
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute(_CREATE_SQL)
        count = 0
        chunk = []
        for row in rows():
            chunk.append(row)
            if len(chunk) >= _CHUNK:
                _let_writers_in()
                with _txn(conn):
                    conn.executemany(_INSERT_SQL, chunk)
                count += len(chunk)
                chunk = []
        if chunk:
            _let_writers_in()
            with _txn(conn):
                conn.executemany(_INSERT_SQL, chunk)
            count += len(chunk)
        t_rows = time.monotonic()
        suffix = _index_suffix(conn)
        for name, cols in _HAMS_INDEXES:
            _let_writers_in()
            with _txn(conn):
                conn.execute(f"CREATE INDEX {name}{suffix} ON hams_import {cols}")
        with _txn(conn):
            conn.execute(_CLUSTERS_TABLE_SQL.format(name="hams_clusters_import"))
            conn.execute(_CHANGES_TABLE_SQL.format(name="hams_changes_import"))
            conn.execute(_changes_sql("hams_import", "hams_changes_import"),
                         _changes_params(built_on, new_days))
        for cell in CLUSTER_CELLS:
            _let_writers_in()
            with _txn(conn):
                conn.execute(_aggregate_sql("hams_import", "hams_clusters_import"),
                             _aggregate_params(built_on, new_days, cell))
        t_swap = time.monotonic()
        _let_writers_in()
        with _txn(conn, fast_free=True):
            conn.execute("DROP TABLE IF EXISTS hams")
            conn.execute("ALTER TABLE hams_import RENAME TO hams")
            conn.execute("DROP TABLE IF EXISTS hams_clusters")
            conn.execute("ALTER TABLE hams_clusters_import RENAME TO hams_clusters")
            conn.execute("DROP TABLE IF EXISTS hams_changes")
            conn.execute("ALTER TABLE hams_changes_import RENAME TO hams_changes")
            _replace_places(conn, gazetteer)
            conn.execute("DELETE FROM zip_anchors")
            conn.executemany("INSERT INTO zip_anchors (zip, lat, lon, source, n_street)"
                             " VALUES (?, ?, ?, ?, ?)", [(z, *a) for z, a in anchors.items()])
            _activate_aggregate(conn, built_on, new_days)
        t_done = time.monotonic()
    finally:
        conn.close()
    log.info("[uls] swapped in %d rows (%d street-level, %d ZIP-level, %d unplaced) (%.0fs total:"
             " rows staged in %.0fs, indexes+aggregate %.0fs, swap %.1fs)",
             count, counts["address"], counts["zip"], count - counts["address"] - counts["zip"],
             time.monotonic() - t0, t_rows - t0, t_swap - t_rows, t_done - t_swap)

    if zip_path is None and os.path.exists(tmp_zip) and \
            time.time() - os.path.getmtime(tmp_zip) > s.uls_refresh_days * 86400:
        os.remove(tmp_zip)  # stale extract; keep recent ones for re-imports
    _touch_marker()
    if extract_date is not None:
        # a reused copy (extract_date None) is the one the marker already names
        _write_marker(_extract_marker_path(), extract_date)
    _sync_users()
    return count


def _sync_users():
    """Registered users follow every import (new street geocodes, fixed joins)."""
    try:
        from app.db import SessionLocal
        from app.integrations.geocode import sync_user_locations

        with SessionLocal() as db:
            sync_user_locations(db)
    except Exception:
        log.warning("[uls] user location sync after import failed", exc_info=True)


# ---------------------------------------------------------------------------
# geocode backfill
# ---------------------------------------------------------------------------

def missing_geocode_addresses(limit: int | None = None) -> list[tuple[str, str, str, str, str]]:
    """Distinct addresses from the current hams table that are due for a
    geocode attempt — never tried, or last tried (and unmatched) more than
    GEOCODE_MISS_RETRY_DAYS ago — as (address_key, street, city, state, zip)."""
    from app.integrations.census_geocoder import address_key

    s = get_settings()
    _ensure_geocode_table()
    cutoff = _utc_stamp(datetime.now(timezone.utc) - timedelta(days=s.geocode_miss_retry_days))
    with engine.begin() as conn:
        skip = {r[0] for r in conn.execute(text("SELECT address_key FROM geocodes"))}
        skip.update(r[0] for r in conn.execute(
            text("SELECT address_key FROM geocode_misses WHERE last_try >= :cut"), {"cut": cutoff}))
        rows = conn.execute(text(
            "SELECT DISTINCT street, city, state, zip FROM hams WHERE street <> ''"
        )).all()
    missing = []
    for street, city, state, zip_ in rows:
        key = address_key(street, city, state, zip_)
        if key in skip:
            continue
        skip.add(key)  # one row per key
        missing.append((key, street, city, state, zip_))
        if limit and len(missing) >= limit:
            break
    return missing


def _control_addresses(n: int = 20) -> list[tuple[str, str, str, str]]:
    """A few addresses Census matched exactly before — the throttling probe
    that rides along in every batch."""
    from app.integrations.census_geocoder import split_address_key

    with engine.begin() as conn:
        keys = [r[0] for r in conn.execute(text(
            "SELECT address_key FROM geocodes WHERE quality = 'Exact'"
            " ORDER BY random() LIMIT :n"), {"n": n})]
    return [split_address_key(k) for k in keys] if len(keys) >= 5 else []


def geocode_backfill(limit: int | None = None) -> int:
    """Geocode due addresses via the Census batch API. Writes after EVERY
    batch, so progress survives restarts: matches go to the geocodes cache,
    No_Match/Tie rows to geocode_misses (retried after a month)."""
    from app.integrations import census_geocoder

    rows = missing_geocode_addresses(limit)
    if not rows:
        return 0
    log.info("[uls] geocoding %d due addresses via Census batch API…", len(rows))
    added = 0

    def persist(hits, misses):
        nonlocal added
        now = _utc_stamp()
        with engine.begin() as conn:
            if hits:
                conn.exec_driver_sql(
                    "INSERT OR REPLACE INTO geocodes"
                    " (address_key, lat, lon, quality, matched_address, source)"
                    " VALUES (?, ?, ?, ?, ?, 'census')",
                    [(k, h.lat, h.lon, h.quality, h.matched_address) for k, h in hits.items()])
                conn.exec_driver_sql("DELETE FROM geocode_misses WHERE address_key = ?",
                                     [(k,) for k in hits])
            if misses:
                conn.exec_driver_sql(
                    "INSERT INTO geocode_misses (address_key, status, tries, last_try)"
                    " VALUES (?, ?, 1, ?) ON CONFLICT(address_key) DO UPDATE SET"
                    " status = excluded.status, tries = geocode_misses.tries + 1,"
                    " last_try = excluded.last_try",
                    [(k, status, now) for k, status in misses.items()])
        added += len(hits)

    census_geocoder.geocode_batch(rows, on_chunk=persist, controls=_control_addresses())
    log.info("[uls] geocode backfill added %d locations", added)
    return added


def _merge_in_chunks(conn: sqlite3.Connection, source: str, sql: str) -> int:
    """Run an INSERT … SELECT from an attached release table over address_key
    ranges of _CHUNK rows, each its own short transaction with a gap for
    waiting writers: merged at once, the first release with matched
    addresses held the write lock ~12 s and signups failed. Inserted rows
    land at the end of the table, so key ranges keep each chunk's writes
    together. `sql` filters on `{keys}`. Returns the rows changed."""
    changed, lo = 0, None
    while True:
        after = " WHERE address_key > ?" if lo is not None else ""
        row = conn.execute(f"SELECT address_key FROM {source}{after} ORDER BY address_key"
                           " LIMIT 1 OFFSET ?", ((lo,) if lo is not None else ()) + (_CHUNK - 1,)
                           ).fetchone()
        hi = row[0] if row else None
        keys = [c for c, v in (("address_key > ?", lo), ("address_key <= ?", hi)) if v is not None]
        args = [v for v in (lo, hi) if v is not None]
        _let_writers_in()
        with _txn(conn):
            changed += conn.execute(sql.format(keys=" AND ".join(keys) or "true"), args).rowcount
        if hi is None:
            return changed
        lo = hi


def _upgrade_from_release(conn: sqlite3.Connection) -> int:
    """Give local geocodes that lack Census's matched address the release's
    row (lat/lon/quality too), in short transactions over the local table's
    rowid ranges: chunks in address_key order would each touch every page of
    the table (7x slower than one transaction, 5-12 s of lock per chunk)."""
    changed = 0
    lo, top = conn.execute("SELECT min(rowid) - 1, max(rowid) FROM geocodes").fetchone()
    while lo is not None and lo < top:
        _let_writers_in()
        with _txn(conn):
            changed += conn.execute(
                "UPDATE geocodes SET (lat, lon, quality, matched_address, source) ="
                " (SELECT r.lat, r.lon, r.quality, r.matched_address, r.source"
                "  FROM rel.geocodes AS r WHERE r.address_key = geocodes.address_key)"
                " WHERE rowid > ? AND rowid <= ? AND matched_address IS NULL"
                " AND EXISTS (SELECT 1 FROM rel.geocodes AS r"
                "  WHERE r.address_key = geocodes.address_key AND r.matched_address IS NOT NULL)",
                (lo, lo + _CHUNK)).rowcount
        lo += _CHUNK
    return changed


def fetch_data_release() -> int:
    """Merge the prebuilt weekly geocode cache (built by CI) into the local
    geocodes table. Returns rows merged, 0 when skipped or on failure — the
    Census backfill then covers the remainder.

    Downloads at most once per ULS_REFRESH_DAYS (marker file), so a
    container with unmatchable addresses never re-fetches 45 MB every
    30 minutes. Old releases (no matched_address/source, no misses table)
    and new ones merge alike; a new release upgrades rows that lack a
    matched address.
    """
    s = get_settings()
    if not s.data_release_enabled or not s.data_release_url:
        return 0
    last = _read_marker(_release_marker_path())
    if last is not None:
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        if age_days < s.uls_refresh_days:
            log.info("[uls] data release fetched %.1f days ago — not downloading again yet", age_days)
            return 0
    import gzip
    import shutil

    dest = os.path.join(_data_dir(), "geocodes_release.sqlite.gz")
    raw = dest[:-3]
    try:
        _download(s.data_release_url, dest)
        _write_marker(_release_marker_path())
        with gzip.open(dest, "rb") as f_in, open(raw + ".partial", "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.replace(raw + ".partial", raw)
        _ensure_geocode_table()
        conn = _connect()
        try:
            conn.execute("ATTACH DATABASE ? AS rel", (raw,))
            rcols = {r[1] for r in conn.execute("PRAGMA rel.table_info(geocodes)")}
            n = 0
            if "matched_address" in rcols:
                n += _upgrade_from_release(conn)
            extra = ", ".join(c if c in rcols else "NULL" for c in ("matched_address", "source"))
            n += _merge_in_chunks(conn, "rel.geocodes", (
                "INSERT INTO geocodes (address_key, lat, lon, quality, matched_address, source)"
                f" SELECT address_key, lat, lon, quality, {extra} FROM rel.geocodes WHERE {{keys}}"
                " ON CONFLICT(address_key) DO NOTHING"))
            n_miss = 0
            has_misses = conn.execute("SELECT 1 FROM rel.sqlite_master WHERE type = 'table'"
                                      " AND name = 'geocode_misses'").fetchone()
            if has_misses:
                n_miss = _merge_in_chunks(conn, "rel.geocode_misses", (
                    "INSERT INTO geocode_misses (address_key, status, tries, last_try)"
                    " SELECT address_key, status, tries, last_try FROM rel.geocode_misses"
                    " WHERE {keys} AND address_key NOT IN (SELECT address_key FROM geocodes)"
                    " ON CONFLICT(address_key) DO UPDATE SET status = excluded.status,"
                    " tries = max(geocode_misses.tries, excluded.tries),"
                    " last_try = max(geocode_misses.last_try, excluded.last_try)"))
            _let_writers_in()
            with _txn(conn):
                conn.execute("DELETE FROM geocode_misses WHERE address_key IN"
                             " (SELECT address_key FROM geocodes)")
            conn.execute("DETACH DATABASE rel")
        finally:
            conn.close()
        if "matched_address" in rcols:
            _write_marker(_release_matched_marker_path())
        log.info("[uls] merged %d geocodes and %d known-unmatchable addresses from the weekly"
                 " data release", n, n_miss)
        return n
    except Exception:
        log.exception("[uls] data release fetch failed — falling back to Census geocoding")
        return 0
    finally:
        for p in (dest, raw, raw + ".partial"):
            if os.path.exists(p):
                os.remove(p)


def new_extract_due(last: datetime) -> bool:
    """Should the next import start now? The FCC publishes a new extract every
    Sunday, so it is imported soon after (the scheduler asks daily) rather than
    ULS_REFRESH_DAYS after the previous import, which left the directory up to
    a week behind: when the FCC's Last-Modified date (published_extract) is
    later than that of the extract the last import read — or, before that was
    recorded, than the import itself. When the FCC gives no date,
    ULS_REFRESH_DAYS after the last import, as before."""
    published = published_extract()
    if published is not None:
        have = _read_marker(_extract_marker_path()) or last
        if published > have:
            log.info("[uls] the FCC published a new extract (%s; the last import read one from %s) — importing",
                     _utc_stamp(published), _utc_stamp(have))
            return True
        return False
    s = get_settings()
    age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
    if age_days >= s.uls_refresh_days:
        log.info("[uls] data is %.1f days old — refreshing", age_days)
        return True
    return False


def ensure_fresh() -> bool:
    """Keep the hams table current and street-level:

    1. import if the table is empty or the FCC published a newer extract
       (new_extract_due; uses cached geocodes);
    2. if many due addresses lack a geocode (or no release with Census
       matched addresses has been merged yet), merge the weekly data release
       (at most once per refresh period), then backfill the rest via the
       Census batch API and re-import so pins move to street level.

    Unmatchable addresses are parked in geocode_misses and retried monthly,
    so they never keep this loop busy. Returns True when everything is done;
    False when work remains (the scheduler then re-checks in 30 minutes).
    """
    s = get_settings()
    if not s.uls_import_enabled:
        return True
    ok = True
    last = last_import()
    if last is None:
        log.info("[uls] no previous import — starting initial import")
        ok = _run_import()
    elif new_extract_due(last):
        ok = _run_import()

    def due() -> int:
        try:
            return len(missing_geocode_addresses(limit=s.geocode_backfill_threshold + 1))
        except Exception:
            log.warning("[uls] could not count addresses due for geocoding", exc_info=True)
            return 0

    missing = due()
    # fast path first: the weekly CI-built geocode cache — also until one with
    # matched addresses has arrived (caches from older releases can't vet
    # their Non_Exact matches without it); fetch_data_release itself
    # downloads at most once per refresh period
    if missing > s.geocode_backfill_threshold or \
            not os.path.exists(_release_matched_marker_path()):
        if fetch_data_release():
            ok = _run_import() and ok  # re-import with the release cache
            missing = due()
    # slow path for whatever the release didn't cover (new/changed addresses)
    if missing > s.geocode_backfill_threshold:
        try:
            if geocode_backfill():
                ok = _run_import() and ok  # re-import with the warm geocode cache
        except Exception:
            log.exception("[uls] geocode backfill failed; will retry at next check")
            ok = False
        remaining = due()
        if remaining > s.geocode_backfill_threshold:
            log.info("[uls] %d+ addresses still due — will resume at next check", remaining)
            ok = False
    return ok


def _run_import() -> bool:
    try:
        import_hams()
        return True
    except Exception:
        log.exception("[uls] import failed; will retry at next check")
        return False
