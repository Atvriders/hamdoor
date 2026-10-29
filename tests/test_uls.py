"""ULS importer, geocode cache, data release and backfill. Uses tiny
fabricated l_amat-style zips and GeoNames files; no network access."""

import csv
import gzip
import io
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.integrations import census_geocoder, uls
from app.integrations.census_geocoder import GeocodeHit, address_key
from app.models import Geocode, Ham, User
from tests.conftest import signup

_PUBLISHED_EXTRACT = uls.published_extract  # the real one (the autouse fixture stubs it)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Markers, GeoNames cache and downloads live in tmp_path; any real
    HTTP request fails the test."""
    monkeypatch.setattr(uls, "_data_dir", lambda: str(tmp_path))

    def no_network(self, request):
        raise AssertionError(f"network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(census_geocoder.time, "sleep", lambda s: None)
    # the FCC's publication date is unknown unless a test serves one (BE-4 tests below)
    monkeypatch.setattr(uls, "published_extract", lambda: None)


def _line(*fields_by_index, total):
    row = [""] * total
    for idx, val in fields_by_index:
        row[idx] = val
    return "|".join(row) + "\n"


def hd(usi, call, status="A", service="HA", granted="06/15/2020", expires="01/01/2035"):
    return _line((0, "HD"), (1, usi), (4, call), (5, status), (6, service), (7, granted),
                 (8, expires), total=59)


def en(usi, call, street, city, state, zip_, first="", mi="", last="", entity="", email=""):
    return _line((0, "EN"), (1, usi), (4, call), (7, entity), (8, first), (9, mi), (10, last),
                 (14, email), (15, street), (16, city), (17, state), (18, zip_), total=30)


def am(usi, call, cls):
    return _line((0, "AM"), (1, usi), (4, call), (5, cls), total=18)


def write_uls_zip(path, hd_rows, en_rows, am_rows):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("HD.dat", "".join(hd_rows))
        zf.writestr("EN.dat", "".join(en_rows))
        zf.writestr("AM.dat", "".join(am_rows))
    return str(path)


def write_gazetteer(data_dir, us_rows, pr_rows=()):
    """GeoNames-format files where load_gazetteer caches them. Rows are
    (zip, place, state, lat, lon, accuracy)."""
    def line(cc, z, place, st, lat, lon, acc, admin1):
        return "\t".join([cc, z, place, admin1, st if cc == "US" else "001", "", "", "", "",
                          str(lat), str(lon), acc]) + "\n"
    with open(data_dir / "uszips.txt", "w") as fh:
        for z, place, st, lat, lon, acc in us_rows:
            fh.write(line("US", z, place, st, lat, lon, acc, "State"))
    with open(data_dir / "zips_PR.txt", "w") as fh:
        for z, place, _, lat, lon, acc in pr_rows:
            fh.write(line("PR", z, place, "", lat, lon, acc, place))
    for cc in ("GU", "VI", "MP", "AS"):
        (data_dir / f"zips_{cc}.txt").write_text("")


US_PLACES = [("06111", "Newington", "CT", 41.69, -72.73, "4"),
             ("06103", "Hartford", "CT", 41.77, -72.68, "4"),
             ("66210", "Overland Park", "KS", 38.93, -94.69, "4")]


@pytest.fixture()
def gazetteer(tmp_path):
    write_gazetteer(tmp_path, US_PLACES, [("00725", "Caguas", "PR", 18.23, -66.04, "4")])
    return tmp_path


@pytest.fixture()
def mini_uls_zip(tmp_path, gazetteer):
    """One active individual, one active vanity club, one terminated license."""
    return write_uls_zip(
        tmp_path / "mini_uls.zip",
        [hd("1001", "W1QQQ"), hd("1002", "W1NVV", service="HV", granted="01/15/2024",
                                  expires="02/02/2034"),
         hd("1003", "K0OLD", status="T", granted="05/05/2000", expires="03/03/2010")],
        [en("1001", "W1QQQ", "10 OAK ST", "NEWINGTON", "CT", "06111", first="QUINN", mi="Q",
            last="QUICK", email="W1QQQ@EXAMPLE.COM"),
         en("1002", "W1NVV", "20 ELM ST", "HARTFORD", "CT", "06103", entity="VANITY CLUB"),
         en("1003", "K0OLD", "1 MAIN ST", "NOWHERE", "KS", "67801", entity="OLD TIMER")],
        [am("1001", "W1QQQ", "E"), am("1002", "W1NVV", ""), am("1003", "K0OLD", "T")])


def _clear_geocodes(db):
    uls.migrate_hams_table()
    db.query(Geocode).delete()
    db.execute(text("DELETE FROM geocode_misses"))
    db.commit()


def _add_geocode(street, city, state, zip_, lat, lon, quality="Exact", matched=None):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT OR REPLACE INTO geocodes (address_key, lat, lon, quality, matched_address, source)"
            " VALUES (:k, :lat, :lon, :q, :m, 'census')"),
            {"k": address_key(street, city, state, zip_), "lat": lat, "lon": lon, "q": quality,
             "m": matched})


def _ham(callsign):
    with engine.begin() as conn:
        r = conn.execute(text("SELECT * FROM hams WHERE callsign = :c"), {"c": callsign}).first()
    return r


# ---------------------------------------------------------------- import


def test_import_hams(mini_uls_zip, db):
    _clear_geocodes(db)
    count = uls.import_hams(zip_path=mini_uls_zip)
    assert count == 2  # K0OLD is terminated and must not appear

    q = db.get(Ham, "W1QQQ")
    assert q is not None
    assert q.name == "Quinn Q Quick"          # title-cased from first/mi/last
    assert q.email == "w1qqq@example.com"     # lowercased
    assert q.street == "10 OAK ST"
    assert q.city == "Newington"
    assert q.license_class == "E"
    assert q.expires == "01/01/2035"
    assert q.granted == "06/15/2020"
    # no geocode cached -> exactly on the ZIP anchor (GeoNames centroid)
    assert (q.lat, q.lon) == (41.69, -72.73)
    assert q.loc_source == "zip"

    v = db.get(Ham, "W1NVV")
    assert v is not None                       # vanity service HV included
    assert v.name == "Vanity Club"             # fell back to entity name
    assert db.get(Ham, "K0OLD") is None

    assert uls.last_import() is not None       # marker written


def test_import_stores_iso_dates(tmp_path, gazetteer, db):
    path = write_uls_zip(tmp_path / "iso.zip",
                         [hd("1", "W1ISO", granted="12/08/2020", expires="02/26/2031"),
                          hd("2", "W1BAD", granted="13/45/2020", expires="")],
                         [en("1", "W1ISO", "1 A ST", "X", "CT", "06111"),
                          en("2", "W1BAD", "1 B ST", "X", "CT", "06111")],
                         [am("1", "W1ISO", "G"), am("2", "W1BAD", "G")])
    uls.import_hams(zip_path=path)
    good, bad = _ham("W1ISO"), _ham("W1BAD")
    assert (good.expires_iso, good.granted_iso) == ("2031-02-26", "2020-12-08")
    assert (good.expires, good.granted) == ("02/26/2031", "12/08/2020")  # kept as-is
    assert (bad.expires_iso, bad.granted_iso) == ("", "")


def test_usi_join_ignores_previous_holders_records(tmp_path, gazetteer, db):
    """Regression: EN/AM rows were joined by callsign, last row wins, so a
    recycled call showed the previous holder's name, address and class."""
    _clear_geocodes(db)
    _add_geocode("5 OLD RD", "LE SUEUR", "MN", "56058", 44.46, -93.91)
    path = write_uls_zip(
        tmp_path / "recycled.zip",
        [hd("2002", "AA0CE", granted="12/10/2022", expires="12/10/2032"),
         hd("1001", "AA0CE", status="C", granted="01/01/2011", expires="01/01/2021")],
        # the current license's rows come first; the old holder's last
        [en("2002", "AA0CE", "7 NEW ST", "OVERLAND PARK", "KS", "66210", first="NEW",
            last="HOLDER"),
         en("1001", "AA0CE", "5 OLD RD", "LE SUEUR", "MN", "56058", first="OLD", last="HOLDER")],
        [am("2002", "AA0CE", "E"), am("1001", "AA0CE", "T")])
    uls.import_hams(zip_path=path)
    h = _ham("AA0CE")
    assert h.name == "New Holder"       # old code: "Old Holder"
    assert h.city == "Overland Park" and h.state == "KS"
    assert h.license_class == "E"       # old code: "T"
    assert h.loc_source == "zip"        # not the previous holder's street pin
    assert (h.lat, h.lon) == (38.93, -94.69)


def test_zip_level_hams_share_one_anchor_without_jitter(tmp_path, gazetteer, db):
    """Regression: ZIP-level pins were jittered up to 0.035° (~4 km)."""
    _clear_geocodes(db)
    path = write_uls_zip(
        tmp_path / "zip.zip",
        [hd("1", "N0AAA"), hd("2", "N0BBB"), hd("3", "N0CCC")],
        [en("1", "N0AAA", "", "OVERLAND PARK", "KS", "66210"),
         en("2", "N0BBB", "PO BOX 5", "OVERLAND PARK", "KS", "66210"),
         en("3", "N0CCC", "", "NOWHERE", "KS", "99999")],
        [am("1", "N0AAA", "T"), am("2", "N0BBB", "G"), am("3", "N0CCC", "G")])
    uls.import_hams(zip_path=path)
    a, b, c = _ham("N0AAA"), _ham("N0BBB"), _ham("N0CCC")
    assert (a.lat, a.lon) == (b.lat, b.lon) == (38.93, -94.69)
    assert a.loc_source == b.loc_source == "zip"
    assert c.lat is None and c.loc_source == ""   # unknown ZIP: not on the map


def _metres(lat1, lon1, lat2, lon2):
    import math
    return math.hypot((lat1 - lat2) * 111_320, (lon1 - lon2) * 111_320 * math.cos(math.radians(lat1)))


def test_zip_anchor_is_median_of_street_hams(tmp_path, gazetteer, db):
    """With ≥ 5 street-geocoded hams in a ZIP, its ZIP-level hams sit at
    their coordinate-wise median (GeoNames centroids can be km off)."""
    _clear_geocodes(db)
    lats = [38.90, 38.91, 38.95, 38.96, 39.30]  # one far outlier
    lons = [-94.70, -94.66, -94.65, -94.60, -94.10]
    hds, ens, ams = [], [], []
    for i, (la, lo) in enumerate(zip(lats, lons)):
        call = f"K0S{i}"
        _add_geocode(f"{i} MAIN ST", "OVERLAND PARK", "KS", "66210", la, lo)
        hds.append(hd(str(i + 10), call))
        ens.append(en(str(i + 10), call, f"{i} MAIN ST", "OVERLAND PARK", "KS", "66210"))
        ams.append(am(str(i + 10), call, "T"))
    hds.append(hd("99", "K0ZIP"))
    ens.append(en("99", "K0ZIP", "PO BOX 1", "OVERLAND PARK", "KS", "66210"))
    ams.append(am("99", "K0ZIP", "T"))
    uls.import_hams(zip_path=write_uls_zip(tmp_path / "median.zip", hds, ens, ams))
    z = _ham("K0ZIP")
    assert z.loc_source == "zip"
    # the median (38.95, -94.65), not GeoNames 38.93,-94.69 — nudged off K0S2's
    # home, which the median lands on exactly (V4-F2)
    assert _metres(z.lat, z.lon, 38.95, -94.65) <= 250
    assert min(_metres(z.lat, z.lon, la, lo) for la, lo in zip(lats, lons)) >= uls.ANCHOR_CLEARANCE_M
    with engine.begin() as conn:
        anchor = conn.execute(text("SELECT * FROM zip_anchors WHERE zip = '66210'")).first()
    assert (anchor.source, anchor.n_street) == ("median", 5)

    # one street ham fewer -> back to the GeoNames centroid
    uls.import_hams(zip_path=write_uls_zip(tmp_path / "median4.zip", hds[1:], ens[1:], ams[1:]))
    assert (_ham("K0ZIP").lat, _ham("K0ZIP").lon) == (38.93, -94.69)


def test_territory_zips_are_placed(tmp_path, gazetteer, db):
    """Regression: GeoNames ships PR/GU/VI/MP/AS outside US.zip, so 78% of
    Puerto Rico's hams had no location at all."""
    _clear_geocodes(db)
    path = write_uls_zip(tmp_path / "pr.zip", [hd("1", "KP4AA")],
                         [en("1", "KP4AA", "", "CAGUAS", "PR", "00725")], [am("1", "KP4AA", "G")])
    uls.import_hams(zip_path=path)
    h = _ham("KP4AA")
    assert (h.lat, h.lon, h.loc_source) == (18.23, -66.04, "zip")


def test_load_gazetteer_territory_state_is_country_code(gazetteer):
    rows = {r[0]: r for r in uls.load_gazetteer()}
    assert rows["00725"][1:3] == ("Caguas", "PR")
    assert rows["66210"] == ("66210", "Overland Park", "KS", 38.93, -94.69, 4)


def test_load_gazetteer_downloads_missing_territories(tmp_path, monkeypatch):
    """Existing data dirs only have uszips.txt: the territory files are
    fetched (once) from the GeoNames directory of GEONAMES_URL."""
    write_gazetteer(tmp_path, US_PLACES)
    for cc in ("PR", "GU", "VI", "MP", "AS"):
        (tmp_path / f"zips_{cc}.txt").unlink()
    fetched = []

    def fake_download(url, dest, max_age_hours=None):
        cc = url.rsplit("/", 1)[1][:2]
        fetched.append(url)
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr(f"{cc}.txt", f"{cc}\t96910\tHagatna\tGu\t66\t\t\t\t\t13.45\t144.75\t6\n"
                        if cc == "GU" else "")
            zf.writestr("readme.txt", "x")

    monkeypatch.setattr(uls, "_download", fake_download)
    rows = uls.load_gazetteer()
    assert sorted(u.rsplit("/", 1)[1] for u in fetched) == ["AS.zip", "GU.zip", "MP.zip", "PR.zip", "VI.zip"]
    assert all(u.startswith("https://download.geonames.org/export/zip/") for u in fetched)
    assert ("96910", "Hagatna", "GU", 13.45, 144.75, 6) in rows
    uls.load_gazetteer()
    assert len(fetched) == 5  # cached after the first fetch


def test_places_table_built_at_import(mini_uls_zip, db):
    uls.import_hams(zip_path=mini_uls_zip)
    with engine.begin() as conn:
        rows = {r.zip: tuple(r) for r in conn.execute(text("SELECT * FROM places"))}
    assert rows["66210"] == ("66210", "Overland Park", "KS", 38.93, -94.69, 4, "overlandpark")  # + search key
    assert rows["00725"][2] == "PR"


def test_import_uses_cached_street_geocode(mini_uls_zip, db):
    _clear_geocodes(db)
    _add_geocode("10 OAK ST", "Newington", "CT", "06111", 41.71489, -72.72687)
    uls.import_hams(zip_path=mini_uls_zip)
    q = db.get(Ham, "W1QQQ")
    assert q.lat == pytest.approx(41.71489, abs=1e-5)   # exact street location
    assert q.lon == pytest.approx(-72.72687, abs=1e-5)
    assert q.loc_source == "address"
    # the un-geocoded ham still falls back to its ZIP anchor
    assert db.get(Ham, "W1NVV").loc_source == "zip"


def test_non_exact_geocode_on_another_street_is_demoted(tmp_path, gazetteer, db):
    """Regression: a Census Non_Exact match on a different street / town /
    state was drawn as the ham's street pin (50–700 km off)."""
    _clear_geocodes(db)
    cases = {
        # call: (street, matched address, quality, expected loc)
        "K0WS": ("1383 PARKVIEW DR", "1383 PARKVIEW CT, RICHMOND, KY, 40475", "Non_Exact", "zip"),
        "K0WT": ("70 NORTHFORK DRIVE", "70 SOUTHFORK DR, OVERLAND PARK, KS, 66210", "Non_Exact", "zip"),
        "K0OK": ("138 VANGAURD AVE APT 2", "138 VANGUARD AVE, OVERLAND PARK, KS, 66212", "Non_Exact", "address"),
        "K0OL": ("15 ELM", "15 ELM ST, OVERLAND PARK, KS, 66210", "Non_Exact", "address"),
        "K0EX": ("9 MAPLE ST", "9 MAPLE ST, OVERLAND PARK, KS, 66210", "Exact", "address"),
        "K0NM": ("4 PINE RD", None, "Non_Exact", "address"),  # old release: no matched address
        # review B1-3: a prefix-named street of the same town is another street
        "K0PX": ("12 OAK HILL RD", "12 OAK ST, OVERLAND PARK, KS, 66210", "Non_Exact", "zip"),
        # review B1-4: compass-word and care-of addresses on the very same street
        "K0CW": ("123 E NORTH ST", "123 E NORTH ST, OVERLAND PARK, KS, 66210", "Non_Exact", "address"),
        "K0CO": ("C/O J DOE 7 ELM AVE", "7 ELM AVE, OVERLAND PARK, KS, 66210", "Non_Exact", "address"),
    }
    hds, ens, ams = [], [], []
    for i, (call, (street, matched, quality, _)) in enumerate(cases.items()):
        _add_geocode(street, "OVERLAND PARK", "KS", "66210", 38.9 + i / 100, -94.7, quality, matched)
        hds.append(hd(str(i), call))
        ens.append(en(str(i), call, street, "OVERLAND PARK", "KS", "66210"))
        ams.append(am(str(i), call, "G"))
    uls.import_hams(zip_path=write_uls_zip(tmp_path / "nonexact.zip", hds, ens, ams))
    for call, (*_, expected) in cases.items():
        assert _ham(call).loc_source == expected, call


def test_import_stages_in_short_transactions(tmp_path, gazetteer, db, monkeypatch):
    """Regression: the import held one write transaction for ~30–45 s, so
    signups and posts failed with "database is locked" (5 s busy timeout).
    Rows are now staged in chunks, indexes and the aggregate on the staging
    table, and only the rename swap runs at the end."""
    from contextlib import contextmanager

    monkeypatch.setattr(uls, "_CHUNK", 3)
    monkeypatch.setattr(uls, "_LOCK_GAP_SECONDS", 0)
    txns = []
    real_txn = uls._txn

    @contextmanager
    def spy(conn, fast_free=False):
        stmts = []
        conn.set_trace_callback(stmts.append)
        try:
            with real_txn(conn, fast_free) as c:
                yield c
        finally:
            conn.set_trace_callback(None)
            txns.append(stmts)

    monkeypatch.setattr(uls, "_txn", spy)
    calls = [f"K1T{i}" for i in range(7)]
    path = write_uls_zip(tmp_path / "chunks.zip", [hd(str(i), c) for i, c in enumerate(calls)],
                         [en(str(i), c, "", "NEWINGTON", "CT", "06111") for i, c in enumerate(calls)],
                         [am(str(i), c, "T") for i, c in enumerate(calls)])
    names = []
    for _ in range(3):
        txns.clear()
        assert uls.import_hams(zip_path=path) == 7
        inserts = [t for t in txns if any(s.startswith("INSERT INTO hams_import") for s in t)]
        assert len(inserts) == 3                       # 3 + 3 + 1 rows
        (swap,) = [t for t in txns if any("RENAME TO hams" in s for s in t)]
        assert not any("INSERT INTO hams_import" in s or "CREATE INDEX ix_hams" in s for s in swap)
        with engine.begin() as conn:
            names.append(sorted(r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'hams'"
                " AND name LIKE 'ix_hams_map%'"))))
            assert conn.execute(text("SELECT COUNT(*) FROM sqlite_master"
                                     " WHERE name LIKE '%_import'")).scalar() == 0
            assert conn.execute(text("SELECT valid FROM hams_cluster_meta")).scalar() == 1
    assert len({n[0] for n in names}) == 2 and all(len(n) == 1 for n in names)  # names alternate


def test_import_swap_replaces_data(mini_uls_zip, db):
    uls.import_hams(zip_path=mini_uls_zip)
    db.add(Ham(callsign="EXTRA1", name="should vanish"))
    db.commit()
    uls.import_hams(zip_path=mini_uls_zip)     # second run swaps cleanly
    assert db.get(Ham, "EXTRA1") is None
    assert db.get(Ham, "W1QQQ") is not None


def test_migrate_old_table_adds_iso_columns_and_forces_reimport(tmp_path, db):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS hams"))
        conn.execute(text(
            "CREATE TABLE hams (callsign TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
            " street TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',"
            " state TEXT NOT NULL DEFAULT '', zip TEXT NOT NULL DEFAULT '',"
            " email TEXT NOT NULL DEFAULT '', license_class TEXT NOT NULL DEFAULT '',"
            " expires TEXT NOT NULL DEFAULT '', granted TEXT NOT NULL DEFAULT '',"
            " lat REAL, lon REAL, loc_source TEXT NOT NULL DEFAULT '')"))
        conn.execute(text("INSERT INTO hams (callsign, expires, granted)"
                          " VALUES ('W1MIG', '02/26/2031', '12/08/2020')"))
    uls._touch_marker()
    uls.migrate_hams_table()
    h = _ham("W1MIG")
    assert (h.expires_iso, h.granted_iso) == ("2031-02-26", "2020-12-08")
    assert uls.last_import() is None  # the scheduler re-imports right away
    uls._touch_marker()
    uls.migrate_hams_table()          # idempotent: no second forced re-import
    assert uls.last_import() is not None


# ---------------------------------------------------------------- users follow imports


def test_users_follow_every_import(client, mini_uls_zip, db, monkeypatch):
    """Regression: users were only re-synced at app startup, so a user who
    signed up before the geocode existed stayed at the ZIP/grid centre."""
    monkeypatch.setattr("app.integrations.geocode.geocode_address", lambda *a: None)
    _clear_geocodes(db)
    uls.import_hams(zip_path=mini_uls_zip)
    db.query(User).filter_by(callsign="W1QQQ").delete()
    db.commit()
    signup(client, "W1QQQ")  # FCC profile from the local ULS row; grid unknown
    user = db.query(User).filter_by(callsign="W1QQQ").one()
    assert (user.lat, user.lon) == (41.69, -72.73)   # ZIP anchor

    _add_geocode("10 OAK ST", "Newington", "CT", "06111", 41.71489, -72.72687)
    uls.import_hams(zip_path=mini_uls_zip)           # no manual sync call
    db.expire_all()
    user = db.query(User).filter_by(callsign="W1QQQ").one()
    assert (user.lat, user.lon) == (41.71489, -72.72687)


def test_lookup_falls_back_to_local_uls(client, mini_uls_zip, db):
    uls.import_hams(zip_path=mini_uls_zip)
    # W1QQQ is unknown to the (fake) live provider but present locally
    r = client.get("/api/lookup/W1QQQ")
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is True
    assert data["email"] == "w1qqq@example.com"
    assert "ULS" in data["source"]
    # QA F6: the importer keeps the FCC's street as written ("10 OAK ST"); sign-up gets normal case
    assert data["address_line"] == "10 Oak St" and data["city"] == "Newington"


# ---------------------------------------------------------------- Census batch


def test_parse_census_response():
    sample = (
        '"1","225 Main St, Newington, CT, 06111","Match","Exact",'
        '"225 MAIN ST, NEWINGTON, CT, 06111","-72.72687,41.71489","3514734","R"\n'
        '"2","1 Infinite Loop, Cupertino, CA, 95014","No_Match"\n'
        '"3","8800 Pine Mountain Rd, Springville, AL, 35146","Tie"\n'
    )
    hits, misses = census_geocoder.parse_batch_response(sample)
    assert hits["1"] == GeocodeHit(41.71489, -72.72687, "Exact", "225 MAIN ST, NEWINGTON, CT, 06111")
    assert misses == {"2": "No_Match", "3": "Tie"}   # old code accepted Tie as a match


class FakeCensus:
    """Stands in for httpx.Client in census_geocoder: records each posted CSV
    and answers like the batch API (echoed IDs lose their backslashes)."""

    def __init__(self, answer):
        self.answer = answer  # (street, attempt) -> "match" | "nomatch" | None (row dropped)
        self.posts = []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None, files=None):
        body = files["addressFile"][1]
        rows = list(csv.reader(io.StringIO(body)))
        self.posts.append(rows)
        out = io.StringIO()
        w = csv.writer(out, quoting=csv.QUOTE_ALL)
        for rid, street, city, state, zip_ in rows:
            verdict = self.answer(street, len(self.posts))
            rid = rid.replace("\\", "")
            if verdict == "match":
                w.writerow([rid, f"{street}, {city}, {state}, {zip_}", "Match", "Exact",
                            f"{street.upper()}, {city.upper()}, {state}, {zip_}",
                            "-72.7,41.7", "1", "L"])
            elif verdict == "nomatch":
                w.writerow([rid, f"{street}, {city}, {state}, {zip_}", "No_Match"])
        return httpx.Response(200, text=out.getvalue())


def test_geocode_batch_sends_numeric_ids_and_maps_back(monkeypatch):
    """Regression: address keys were sent as batch IDs; Census strips
    backslashes from echoed IDs, filing results under the wrong key."""
    fake = FakeCensus(lambda street, attempt: "match" if "OAK" in street else "nomatch")
    monkeypatch.setattr(census_geocoder.httpx, "Client", fake)
    rows = [("1 OAK ST\\B|X|CT|06111", "1 OAK ST\\B", "X", "CT", "06111"),
            ("2 ELM ST|X|CT|06111", "2 ELM ST", "X", "CT", "06111")]
    hits, misses = census_geocoder.geocode_batch(rows)
    assert [r[0] for r in fake.posts[0]] == ["0", "1"]
    assert set(hits) == {"1 OAK ST\\B|X|CT|06111"}
    assert misses == {"2 ELM ST|X|CT|06111": "No_Match"}


def test_unmatchable_batch_with_healthy_controls_is_accepted_first_time(monkeypatch):
    """Regression (verifier note 1): a batch of hard addresses matches ~0.5%,
    which the old <20% rule took for throttling: 4 POSTs per chunk, forever."""
    controls = [(f"{i} GOOD ST", "X", "CT", "06111") for i in range(10)]
    fake = FakeCensus(lambda street, attempt: "match" if "GOOD" in street else "nomatch")
    monkeypatch.setattr(census_geocoder.httpx, "Client", fake)
    rows = [(f"K{i}", f"{i} HARD RD", "X", "CT", "06111") for i in range(50)]
    hits, misses = census_geocoder.geocode_batch(rows, controls=controls)
    assert len(fake.posts) == 1
    assert hits == {} and len(misses) == 50
    assert len(fake.posts[0]) == 60   # controls ride along, never reported


def test_probe_seeded_from_first_batch_when_cache_is_empty(monkeypatch):
    """The CI build starts with an empty cache (no controls): the first
    healthy batch's exact matches become the probe for the rest, so hard
    batches go through in one POST and throttled replies are still caught
    (and never parked as unmatchable)."""
    monkeypatch.setattr(get_settings(), "census_batch_size", 30)
    # every address of POST no. 3 comes back No_Match: a throttled reply
    fake = FakeCensus(lambda street, post: "nomatch" if post == 3 or "HARD" in street else "match")
    monkeypatch.setattr(census_geocoder.httpx, "Client", fake)
    rows = [(f"G{i}", f"{i} GOOD ST", "X", "CT", "06111") for i in range(30)]
    rows += [(f"H{i}", f"{i} HARD RD", "X", "CT", "06111") for i in range(40)]
    hits, misses = census_geocoder.geocode_batch(rows)
    assert len(hits) == 30 and len(misses) == 40
    # 1 healthy batch, then 4 batches of 10 hard rows + 20 controls; the
    # throttled 3rd POST is retried once. The old <20% rule: 1 + 4 x 4 POSTs
    assert len(fake.posts) == 6
    assert [len(p) for p in fake.posts] == [30, 30, 30, 30, 30, 30]


def test_throttled_reply_detected_by_controls_and_retried(monkeypatch):
    controls = [(f"{i} GOOD ST", "X", "CT", "06111") for i in range(10)]
    # attempt 1: the service is throttling (everything No_Match); attempt 2 healthy
    fake = FakeCensus(lambda street, attempt: "nomatch" if attempt == 1 or "HARD" in street else "match")
    monkeypatch.setattr(census_geocoder.httpx, "Client", fake)
    rows = [("A", "1 OAK ST", "X", "CT", "06111"), ("B", "2 HARD RD", "X", "CT", "06111")]
    hits, misses = census_geocoder.geocode_batch(rows, controls=controls)
    assert len(fake.posts) == 2
    assert set(hits) == {"A"} and misses == {"B": "No_Match"}

    # still throttled after every retry: keep nothing as "unmatchable"
    fake = FakeCensus(lambda street, attempt: "nomatch")
    monkeypatch.setattr(census_geocoder.httpx, "Client", fake)
    hits, misses = census_geocoder.geocode_batch(rows, controls=controls)
    assert len(fake.posts) == 4 and hits == {} and misses == {}


# ---------------------------------------------------------------- backfill / misses


def test_geocode_backfill_records_hits_and_misses(mini_uls_zip, db, monkeypatch):
    _clear_geocodes(db)
    uls.import_hams(zip_path=mini_uls_zip)
    missing = uls.missing_geocode_addresses()
    assert len(missing) == 2                   # both mini-zip hams have streets

    calls = []

    def fake_batch(rows, on_chunk=None, controls=None):
        calls.append([r[0] for r in rows])
        hits = {k: GeocodeHit(41.7, -72.7, "Exact", "10 OAK ST, NEWINGTON, CT, 06111")
                for k, s, *_ in rows if "OAK" in s}
        misses = {k: "No_Match" for k, s, *_ in rows if "OAK" not in s}
        on_chunk(hits, misses)
        return hits, misses

    monkeypatch.setattr(census_geocoder, "geocode_batch", fake_batch)
    assert uls.geocode_backfill() == 1
    assert uls.missing_geocode_addresses() == []    # the miss is parked, not re-sent
    with engine.begin() as conn:
        row = conn.execute(text("SELECT * FROM geocodes")).first()
        miss = conn.execute(text("SELECT * FROM geocode_misses")).first()
    assert (row.matched_address, row.source) == ("10 OAK ST, NEWINGTON, CT, 06111", "census")
    assert (miss.status, miss.tries) == ("No_Match", 1)

    assert uls.geocode_backfill() == 0
    assert len(calls) == 1                         # nothing due -> no Census call

    # a month later the miss is due again, and a second failure counts a try
    with engine.begin() as conn:
        conn.execute(text("UPDATE geocode_misses SET last_try = '2020-01-01T00:00:00Z'"))
    assert [k for k, *_ in uls.missing_geocode_addresses()] == [miss.address_key]
    uls.geocode_backfill()
    with engine.begin() as conn:
        assert conn.execute(text("SELECT tries FROM geocode_misses")).scalar() == 2


def test_ensure_fresh_completes_when_only_unmatchable_addresses_remain(
        tmp_path, gazetteer, db, monkeypatch):
    """Regression (verifier note 1): with ~42k unmatchable addresses the old
    ensure_fresh returned False forever — every 30 min it re-downloaded the
    release, re-posted the addresses and re-imported 822k rows."""
    _clear_geocodes(db)
    hds, ens, ams = [], [], []
    for i in range(8):
        call = f"K0U{i}"
        hds.append(hd(str(i), call))
        ens.append(en(str(i), call, f"{i} NOWHERE RD", "OVERLAND PARK", "KS", "66210"))
        ams.append(am(str(i), call, "T"))
    path = write_uls_zip(tmp_path / "l_amat.zip", hds, ens, ams)
    s = get_settings()
    monkeypatch.setattr(s, "uls_import_enabled", True)
    monkeypatch.setattr(s, "geocode_backfill_threshold", 3)
    monkeypatch.setattr(s, "data_release_enabled", False)
    downloads, batches, imports = [], [], []
    monkeypatch.setattr(uls, "_download", lambda url, dest, max_age_hours=None: downloads.append(url))
    real_import = uls.import_hams
    monkeypatch.setattr(uls, "import_hams", lambda zip_path=None: imports.append(1) or real_import(path))

    def fake_batch(rows, on_chunk=None, controls=None):
        batches.append(len(rows))
        misses = {k: "No_Match" for k, *_ in rows}
        on_chunk({}, misses)
        return {}, misses

    monkeypatch.setattr(census_geocoder, "geocode_batch", fake_batch)
    assert uls.ensure_fresh() is True          # imported, tried all 8, parked them
    assert batches == [8] and len(imports) == 1
    assert uls.ensure_fresh() is True          # next check: nothing to do
    assert batches == [8] and len(imports) == 1


def _release(tmp_path, name, rows, new_format):
    raw = tmp_path / f"{name}.sqlite"
    conn = sqlite3.connect(raw)
    extra = ", matched_address TEXT, source TEXT" if new_format else ""
    conn.execute(f"CREATE TABLE geocodes (address_key TEXT PRIMARY KEY, lat REAL, lon REAL, quality TEXT{extra})")
    for key, lat, lon, q, matched in rows:
        vals = (key, lat, lon, q) + ((matched, "census") if new_format else ())
        conn.execute(f"INSERT INTO geocodes VALUES ({', '.join('?' for _ in vals)})", vals)
    conn.commit()
    conn.close()
    gz = tmp_path / f"{name}.gz"
    with open(raw, "rb") as fi, gzip.open(gz, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    return gz


def test_release_fetched_weekly_until_matched_addresses_arrive(mini_uls_zip, tmp_path, db, monkeypatch):
    """Caches built from old releases have no Census matched address, so
    their Non_Exact matches can't be vetted: keep fetching the release
    (weekly at most) until one with matched addresses has been merged, then
    only when many addresses are due."""
    _clear_geocodes(db)
    k1 = address_key("10 OAK ST", "Newington", "CT", "06111")
    _add_geocode("10 OAK ST", "Newington", "CT", "06111", 41.71, -72.72, "Non_Exact")
    _add_geocode("20 ELM ST", "Hartford", "CT", "06103", 41.77, -72.68, "Exact")
    uls.import_hams(zip_path=mini_uls_zip)        # nothing due: both addresses cached
    s = get_settings()
    monkeypatch.setattr(s, "uls_import_enabled", True)
    monkeypatch.setattr(uls, "import_hams", lambda zip_path=None: 2)
    old = _release(tmp_path, "old", [(k1, 41.71, -72.72, "Non_Exact", None)], new_format=False)
    new = _release(tmp_path, "new", [(k1, 41.71, -72.72, "Non_Exact", "10 OAK RD, NEWINGTON, CT, 06111")],
                   new_format=True)
    served = [old]
    downloads = []

    def fake_download(url, dest, max_age_hours=None):
        downloads.append(url)
        shutil.copy(served[0], dest)

    monkeypatch.setattr(uls, "_download", fake_download)

    def age_release_marker():
        aged = datetime.now(timezone.utc) - timedelta(days=s.uls_refresh_days + 1)
        (tmp_path / ".data_release_fetched").write_text(aged.isoformat())

    assert uls.ensure_fresh() is True and len(downloads) == 1   # old-format release
    assert uls.ensure_fresh() is True and len(downloads) == 1   # not again the same week
    age_release_marker()
    served[0] = new
    assert uls.ensure_fresh() is True and len(downloads) == 2   # a week later: new format
    with engine.begin() as conn:
        assert conn.execute(text("SELECT matched_address FROM geocodes WHERE address_key = :k"),
                            {"k": k1}).scalar() == "10 OAK RD, NEWINGTON, CT, 06111"
    age_release_marker()
    assert uls.ensure_fresh() is True and len(downloads) == 2   # nothing due: no more downloads


def test_fetch_data_release_merges_old_format_once_per_refresh(db, tmp_path, monkeypatch):
    raw = tmp_path / "geocodes.sqlite"
    conn = sqlite3.connect(raw)
    conn.execute("CREATE TABLE geocodes (address_key TEXT PRIMARY KEY, lat REAL, lon REAL, quality TEXT)")
    conn.executemany("INSERT INTO geocodes VALUES (?,?,?,?)",
                     [("ADDR|ONE|CT|06111", 41.0, -72.0, "Exact"),
                      ("ADDR|TWO|CT|06103", 42.0, -73.0, "Non_Exact")])
    conn.commit()
    conn.close()
    gz_path = tmp_path / "artifact.gz"
    with open(raw, "rb") as fi, gzip.open(gz_path, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    downloads = []

    def fake_download(url, dest, max_age_hours=None):
        downloads.append(url)
        shutil.copy(gz_path, dest)

    monkeypatch.setattr(uls, "_download", fake_download)
    _clear_geocodes(db)

    assert uls.fetch_data_release() == 2
    assert db.query(Geocode).count() == 2
    # regression: the next scheduler pass (30 min later) must not download 45 MB again
    assert uls.fetch_data_release() == 0
    assert downloads == [get_settings().data_release_url]
    # after a refresh period it does
    old = datetime.now(timezone.utc) - timedelta(days=get_settings().uls_refresh_days + 1)
    (tmp_path / ".data_release_fetched").write_text(old.isoformat())
    assert uls.fetch_data_release() == 0       # same content: nothing new to merge
    assert len(downloads) == 2


def test_fetch_data_release_new_format_upgrades_rows_and_merges_misses(db, tmp_path, monkeypatch):
    _clear_geocodes(db)
    with engine.begin() as conn:  # an old-format local row and a stale local miss
        conn.execute(text("INSERT INTO geocodes (address_key, lat, lon, quality)"
                          " VALUES ('ADDR|ONE|CT|06111', 41.0, -72.0, 'Non_Exact')"))
        conn.execute(text("INSERT INTO geocode_misses VALUES"
                          " ('ADDR|THREE|CT|06111', 'No_Match', 3, '2026-01-01T00:00:00Z')"))
    raw = tmp_path / "geocodes.sqlite"
    conn = sqlite3.connect(raw)
    conn.execute("CREATE TABLE geocodes (address_key TEXT PRIMARY KEY, lat REAL, lon REAL,"
                 " quality TEXT, matched_address TEXT, source TEXT)")
    conn.executemany("INSERT INTO geocodes VALUES (?,?,?,?,?,?)", [
        ("ADDR|ONE|CT|06111", 41.5, -72.5, "Non_Exact", "1 ADDR, ONE, CT, 06111", "census"),
        ("ADDR|TWO|CT|06103", 42.0, -73.0, "Exact", "2 ADDR, TWO, CT, 06103", "census")])
    conn.execute("CREATE TABLE geocode_misses (address_key TEXT PRIMARY KEY, status TEXT,"
                 " tries INTEGER, last_try TEXT)")
    conn.executemany("INSERT INTO geocode_misses VALUES (?,?,?,?)", [
        ("ADDR|THREE|CT|06111", "Tie", 1, "2026-10-12T00:00:00Z"),
        ("ADDR|FOUR|CT|06111", "No_Match", 2, "2026-10-12T00:00:00Z")])
    conn.commit()
    conn.close()
    gz_path = tmp_path / "artifact.gz"
    with open(raw, "rb") as fi, gzip.open(gz_path, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    monkeypatch.setattr(uls, "_download", lambda url, dest, max_age_hours=None: shutil.copy(gz_path, dest))

    assert uls.fetch_data_release() == 2     # one upgraded, one inserted
    with engine.begin() as conn:
        one = conn.execute(text("SELECT * FROM geocodes WHERE address_key = 'ADDR|ONE|CT|06111'")).first()
        misses = {r.address_key: tuple(r)[1:] for r in conn.execute(text("SELECT * FROM geocode_misses"))}
    assert (one.lat, one.matched_address, one.source) == (41.5, "1 ADDR, ONE, CT, 06111", "census")
    assert misses == {"ADDR|THREE|CT|06111": ("Tie", 3, "2026-10-12T00:00:00Z"),
                      "ADDR|FOUR|CT|06111": ("No_Match", 2, "2026-10-12T00:00:00Z")}


def test_release_builder_exports_new_columns_and_misses(db, tmp_path, monkeypatch):
    import tools.build_data_release as tool

    _clear_geocodes(db)
    _add_geocode("1 A ST", "X", "CT", "06111", 41.0, -72.0, "Non_Exact", "1 A ST, X, CT, 06111")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO geocode_misses VALUES ('2 B ST|X|CT|06111', 'Tie', 1, '2026-10-12T00:00:00Z')"))
    monkeypatch.setattr(tool, "OUT_DIR", str(tmp_path / "build"))
    monkeypatch.setattr(tool.uls, "import_hams", lambda: 0)
    monkeypatch.setattr(tool.uls, "geocode_backfill", lambda: 0)
    with pytest.raises(SystemExit):     # the "suspiciously few" guard still fires
        tool.main()
    with gzip.open(tmp_path / "build" / "geocodes.sqlite.gz", "rb") as fi, \
            open(tmp_path / "out.sqlite", "wb") as fo:
        shutil.copyfileobj(fi, fo)
    conn = sqlite3.connect(tmp_path / "out.sqlite")
    assert conn.execute("SELECT matched_address, source FROM geocodes").fetchall() == \
        [("1 A ST, X, CT, 06111", "census")]
    assert conn.execute("SELECT * FROM geocode_misses").fetchall() == \
        [("2 B ST|X|CT|06111", "Tie", 1, "2026-10-12T00:00:00Z")]
    conn.close()


def test_signup_via_local_uls_only(client, mini_uls_zip, db, monkeypatch):
    _clear_geocodes(db)
    uls.import_hams(zip_path=mini_uls_zip)
    db.query(User).filter_by(callsign="W1QQQ").delete()
    db.commit()
    # no network geocoding in tests; the ham's ZIP anchor should be used
    monkeypatch.setattr("app.integrations.geocode.resolve_location", lambda *a: None)
    data = signup(client, "W1QQQ")
    assert data["user"]["name"] == "Quinn Q Quick"
    assert (data["user"]["lat"], data["user"]["lon"]) == (41.69, -72.73)


def test_migrate_users_table_adds_map_precision(tmp_path):
    """A users table from before map_precision existed gains the column, and
    every existing member keeps being shown where they are shown today."""
    import sqlite3

    from sqlalchemy import create_engine

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, callsign TEXT)")
    con.execute("INSERT INTO users (callsign) VALUES ('W1OLD')")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    uls.migrate_users_table(eng)
    uls.migrate_users_table(eng)  # idempotent: a second start changes nothing
    con = sqlite3.connect(path)
    assert con.execute("SELECT map_precision FROM users WHERE callsign = 'W1OLD'").fetchone() == ("address",)
    con.close()


# ---------------------------------------------------------------- final pass: V4-F5, V4-F2, V3-F3, V3-F2


def test_a_zip_from_another_state_leaves_the_licence_unplaced(tmp_path, gazetteer, db):
    """V4-F5 regression: FCC ZIP typos put licences in another state or
    territory (Honolulu with a Washington ZIP, 4,219 km off)."""
    _clear_geocodes(db)
    path = write_uls_zip(
        tmp_path / "typo.zip", [hd("1", "KH6ZZT"), hd("2", "K0ZZK")],
        [en("1", "KH6ZZT", "", "HONOLULU", "HI", "66210"),      # 66210 is Overland Park, KS
         en("2", "K0ZZK", "", "OVERLAND PARK", "KS", "66210")],
        [am("1", "KH6ZZT", "G"), am("2", "K0ZZK", "G")])
    uls.import_hams(zip_path=path)
    typo, ok = _ham("KH6ZZT"), _ham("K0ZZK")
    assert typo.lat is None and typo.loc_source == ""          # old code: placed in Kansas
    assert (ok.lat, ok.lon, ok.loc_source) == (38.93, -94.69, "zip")


def test_zip_anchor_ignores_mail_drops_and_keeps_off_homes(tmp_path, gazetteer, db):
    """V4-F2 regression: the anchor (a coordinate-wise median) landed exactly
    on a mail drop with 377 licensees, or on one licensee's home, hiding
    that pin under the dashed ZIP group."""
    _clear_geocodes(db)
    homes = [(38.90, -94.70), (38.91, -94.66), (38.95, -94.65), (38.96, -94.60), (39.30, -94.10)]
    hds, ens, ams = [], [], []
    for i, (la, lo) in enumerate(homes):
        _add_geocode(f"{i} MAIN ST", "OVERLAND PARK", "KS", "66210", la, lo)
        hds.append(hd(str(10 + i), f"K0H{i}"))
        ens.append(en(str(10 + i), f"K0H{i}", f"{i} MAIN ST", "OVERLAND PARK", "KS", "66210"))
        ams.append(am(str(10 + i), f"K0H{i}", "T"))
    _add_geocode("9 MAILBOX WAY", "OVERLAND PARK", "KS", "66210", 39.20, -94.20)
    for i in range(12):  # a mail drop: 12 licensees at one point
        hds.append(hd(str(100 + i), f"K0M{i:02d}"))
        ens.append(en(str(100 + i), f"K0M{i:02d}", "9 MAILBOX WAY", "OVERLAND PARK", "KS", "66210"))
        ams.append(am(str(100 + i), f"K0M{i:02d}", "G"))
    hds.append(hd("99", "K0ZIP"))
    ens.append(en("99", "K0ZIP", "PO BOX 1", "OVERLAND PARK", "KS", "66210"))
    ams.append(am("99", "K0ZIP", "T"))
    uls.import_hams(zip_path=write_uls_zip(tmp_path / "drop.zip", hds, ens, ams))
    z = _ham("K0ZIP")
    # the median of the five homes (38.95, -94.65) — old code: the mail drop
    # pulled it to (39.2, -94.2) — nudged off the home it lands on
    assert _metres(z.lat, z.lon, 38.95, -94.65) <= 250
    assert min(_metres(z.lat, z.lon, la, lo) for la, lo in homes + [(39.20, -94.20)]) >= 25


def test_derived_tables_are_built_for_an_upgraded_database(mini_uls_zip, db):
    """V3-F3 regression: a database upgraded from a4ac1e7 had no cluster
    aggregate, places or ZIP anchors until its first import (forever with
    imports off): zoomed-out maps took ~5.5 s and place search was empty."""
    _clear_geocodes(db)
    uls.import_hams(zip_path=mini_uls_zip)
    with engine.begin() as conn:  # what an a4ac1e7 database looks like
        conn.execute(text("DELETE FROM hams_cluster_meta"))
        conn.execute(text("DELETE FROM hams_clusters"))
        conn.execute(text("DELETE FROM places"))
        conn.execute(text("DELETE FROM zip_anchors"))
    assert uls.derived_tables_due() == (True, True, True)
    from app.scheduler import start_uls_scheduler

    assert not get_settings().uls_import_enabled
    assert start_uls_scheduler() is None  # imports off: a one-off thread builds them
    import threading
    for t in threading.enumerate():
        if t.name == "uls-derived-tables":
            t.join(60)
    assert uls.derived_tables_due() == (False, False, False)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT valid FROM hams_cluster_meta WHERE id = 1")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM hams_clusters")).scalar() > 0
        assert conn.execute(text("SELECT COUNT(*) FROM places")).scalar() == 4
        assert conn.execute(text("SELECT COUNT(*) FROM zip_anchors")).scalar() == 4


def test_an_old_aggregate_is_rebuilt(mini_uls_zip, db):
    _clear_geocodes(db)
    uls.import_hams(zip_path=mini_uls_zip)
    assert uls.derived_tables_due() == (False, False, False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE hams_cluster_meta SET built_on = '2000-01-01'"))
    assert uls.derived_tables_due()[0] is True
    assert uls.ensure_derived_tables() is True
    assert uls.derived_tables_due() == (False, False, False)


def test_members_who_edited_their_address_are_not_labelled_licence(db, monkeypatch):
    """V3-F2 regression: older versions pinned every street-geocoded member
    at their licence, whatever address they had entered; the migration then
    labelled them 'licence' and they stayed there. They are left
    unattributed and re-placed from their own profile."""
    from app.integrations import geocode

    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    uls.migrate_hams_table()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM hams WHERE callsign IN ('AC0MKA', 'AC0MKB')"))
        conn.execute(text("DELETE FROM users WHERE callsign IN ('AC0MKA', 'AC0MKB')"))
        for call in ("AC0MKA", "AC0MKB"):
            conn.execute(text(
                "INSERT INTO hams (callsign, name, street, city, state, zip, lat, lon, loc_source)"
                " VALUES (:c, 'X', '8100 WAYNE AVE', 'KANSAS CITY', 'MO', '64131', 38.981, -94.5694,"
                " 'address')"), {"c": call})
        conn.execute(text(
            "INSERT INTO users (callsign, password_hash, name, email, address_line, city, state, zip,"
            " grid, lat, lon, range_miles, created_at, map_precision)"
            " VALUES ('AC0MKA', 'x', 'A', 'a@example.com', '8100 Wayne Ave', 'Kansas City', 'MO',"
            " '64131', '', 38.981, -94.5694, 25, '2026-01-01', 'address'),"
            " ('AC0MKB', 'x', 'B', 'b@example.com', '4500 Main St', 'Kansas City', 'MO', '64111',"
            " 'EM29qa', 38.981, -94.5694, 25, '2026-01-01', 'address')"))
        uls.guess_location_sources(conn)
        got = dict(conn.execute(text("SELECT callsign, location_source FROM users"
                                     " WHERE callsign IN ('AC0MKA', 'AC0MKB')")).all())
    assert got == {"AC0MKA": "licence", "AC0MKB": None}  # old code: both 'licence'
    assert geocode.place_mispinned_members(db, pause_s=0) == 1
    b = db.query(User).filter_by(callsign="AC0MKB").one()
    db.refresh(b)
    assert b.location_source == "grid" and (b.lat, b.lon) != (38.981, -94.5694)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM hams WHERE callsign IN ('AC0MKA', 'AC0MKB')"))
        conn.execute(text("DELETE FROM users WHERE callsign IN ('AC0MKA', 'AC0MKB')"))


# ---------------------------------------------------------------- QA F7: acronyms survive the import


def test_imported_names_and_cities_keep_their_acronyms(tmp_path, gazetteer, db):
    """QA F7: str.title() made W1AW "Arrl Hq Operators Club" and W1HQ "Arrl
    Radio Laboratory"; what a licensee wrote in mixed case is kept as written."""
    path = write_uls_zip(
        tmp_path / "acr.zip",
        [hd("1", "W1AW"), hd("2", "W1HQ"), hd("3", "K1MIX"), hd("4", "K1JR")],
        [en("1", "W1AW", "225 MAIN ST", "NEWINGTON", "CT", "06111", entity="ARRL HQ OPERATORS CLUB"),
         en("2", "W1HQ", "225 Main Street", "NEWINGTON", "CT", "06111", entity="ARRL RADIO LABORATORY"),
         en("3", "K1MIX", "1 ELM ST", "Hartford", "CT", "06103", first="Mary", last="McDonald"),
         en("4", "K1JR", "2 ELM ST", "HARTFORD", "CT", "06103", first="JOHN", mi="Q", last="O'BRIEN III")],
        [am("1", "W1AW", ""), am("2", "W1HQ", ""), am("3", "K1MIX", "G"), am("4", "K1JR", "E")])
    uls.import_hams(zip_path=path)
    assert (db.get(Ham, "W1AW").name, db.get(Ham, "W1AW").city) == ("ARRL HQ Operators Club", "Newington")
    assert db.get(Ham, "W1HQ").name == "ARRL Radio Laboratory"
    assert (db.get(Ham, "K1MIX").name, db.get(Ham, "K1MIX").city) == ("Mary McDonald", "Hartford")
    assert db.get(Ham, "K1JR").name == "John Q O'Brien III"


def test_imported_names_and_cities_typed_in_lower_or_odd_case_or_postal_abbreviations(tmp_path, gazetteer, db):
    """QA R1: the acronym-safe title case changed only text in capitals, so
    what licensees typed in lower case stayed so ("william T douglass",
    "taylorville": 708 names and 3,799 cities that str.title() had fixed),
    odd case stayed ("ChARLES", "WRIGHt"), the FCC's postal abbreviations
    stayed in capitals ("Pine GRV MLS", "El Dorado HLS") and a club's acronym
    turned up in a town ("Des ARC")."""
    rows = [  # callsign, (first, mi, last) or entity, city, state
        ("N9TOP", ("Sue", "I", "Goodin"), "taylorville", "IL"),
        ("W0WTD", ("william", "T", "douglass"), "Saint Paul", "MN"),
        ("KO6LQR", ("tracy", "", "orvik"), "Morgan Hill", "CA"),
        ("K7CCW", ("ChARLES", "C", "Edgar"), "Tigard", "OR"),
        ("KN0WHF", ("REYNOLD", "D", "WRIGHt"), "BLOOMFIELD HiLLS", "MI"),
        ("KO6MLB", ("donald", "m", "mclean"), "Pleasant view", "UT"),
        ("WL7CPG", ("Richard", "M", "o Connor"), "Anchorage", "AK"),
        ("K7VMI", ("Allen", "L", "de Steiguer"), "Coeur d Alene", "ID"),
        ("KJ5BTH", ("Mildred", "M", "ODonnell"), "OFallon", "MO"),
        ("KC3UAS", ("ERIK", "", "LENZING"), "PINE GRV MLS", "PA"),
        ("WB6OGD", ("GARY", "A", "WINBLAD"), "EL DORADO HLS", "CA"),
        ("KF0IMK", ("CHRIS", "M", "CUNNINGHAM"), "HGHLNDS RANCH", "CO"),
        ("K5PQ", ("LEE", "", "PARKER"), "DES ARC", "AR"),
        ("KE2BFZ", ("Sony", "", "Sinha"), "JOINT BASE MDL", "NJ"),
        ("K0IG", ("Joe", "", "Smith"), "austin, tx", "TX"),
        ("W4FTK", "FleetTalk Amateur Radio Group", "Pismo BEACH", "CA"),
        ("W1AW", "ARRL HQ OPERATORS CLUB", "NEWINGTON", "CT"),
    ]
    ens = []
    for i, (call, name, city, st) in enumerate(rows, 1):
        parts = dict(entity=name) if isinstance(name, str) else dict(zip(("first", "mi", "last"), name))
        ens.append(en(str(i), call, f"{i} ELM ST", city, st, "06111", **parts))
    path = write_uls_zip(tmp_path / "case.zip", [hd(str(i), r[0]) for i, r in enumerate(rows, 1)], ens,
                         [am(str(i), r[0], "G") for i, r in enumerate(rows, 1)])
    uls.import_hams(zip_path=path)
    got = {r[0]: (db.get(Ham, r[0]).name, db.get(Ham, r[0]).city) for r in rows}
    assert got == {
        "N9TOP": ("Sue I Goodin", "Taylorville"),
        "W0WTD": ("William T Douglass", "Saint Paul"),
        "KO6LQR": ("Tracy Orvik", "Morgan Hill"),
        "K7CCW": ("Charles C Edgar", "Tigard"),
        "KN0WHF": ("Reynold D Wright", "Bloomfield Hills"),
        "KO6MLB": ("Donald M McLean", "Pleasant View"),
        "WL7CPG": ("Richard M O Connor", "Anchorage"),
        "K7VMI": ("Allen L de Steiguer", "Coeur d Alene"),       # particles as written
        "KJ5BTH": ("Mildred M ODonnell", "OFallon"),             # O' without the apostrophe
        "KC3UAS": ("Erik Lenzing", "Pine Grv Mls"),
        "WB6OGD": ("Gary A Winblad", "El Dorado Hls"),
        "KF0IMK": ("Chris M Cunningham", "Hghlnds Ranch"),
        "K5PQ": ("Lee Parker", "Des Arc"),
        "KE2BFZ": ("Sony Sinha", "Joint Base MDL"),
        "K0IG": ("Joe Smith", "Austin, TX"),                     # the licence's own state
        "W4FTK": ("FleetTalk Amateur Radio Group", "Pismo Beach"),
        "W1AW": ("ARRL HQ Operators Club", "Newington"),
    }


# ---------------------------------------------------------------- QA F6: members in the provider's capitals


def test_members_stored_in_the_providers_capitals_are_recased_once(tmp_path):
    """QA F6: sign-up and Update from FCC stored callook's capitals verbatim
    ("NEWINGTON, Connecticut" on the QSL card, "JOHN" in Nearby). Existing
    rows are put in normal case once; what a member typed in mixed case, and
    anything typed after the upgrade, is left alone."""
    from sqlalchemy import create_engine

    path = tmp_path / "members.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, callsign TEXT, name TEXT NOT NULL DEFAULT '',"
                " address_line TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '')")
    con.executemany("INSERT INTO users (callsign, name, address_line, city) VALUES (?, ?, ?, ?)", [
        ("W1HQ", "ARRL RADIO LABORATORY", "225 MAIN STREET", "NEWINGTON"),
        ("K1ABC", "John Smith", "12 Elm St", "West Hartford"),       # typed by the member
        ("K1DEF", "JOHN Q SMITH JR", "1ST AVE NE", "St. Louis"),     # mixed: only the capitals change
        ("K1GHI", "", "", ""),
        ("K3UAS", "ERIK LENZING", "4 ELM ST", "PINE GRV MLS"),         # a town as the FCC prints it (QA R1)
    ])
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    uls.migrate_users_table(eng)

    def rows():
        c = sqlite3.connect(path)
        try:
            return {r[0]: r[1:] for r in c.execute("SELECT callsign, name, address_line, city FROM users")}
        finally:
            c.close()

    assert rows() == {
        "W1HQ": ("ARRL Radio Laboratory", "225 Main Street", "Newington"),
        "K1ABC": ("John Smith", "12 Elm St", "West Hartford"),
        "K1DEF": ("John Q Smith Jr", "1st Ave NE", "St. Louis"),
        "K1GHI": ("", "", ""),
        "K3UAS": ("Erik Lenzing", "4 Elm St", "Pine Grv Mls"),
    }
    c = sqlite3.connect(path)
    c.execute("UPDATE users SET name = 'BOB' WHERE callsign = 'K1ABC'")  # typed later, on purpose
    c.commit()
    c.close()
    uls.migrate_users_table(eng)  # the next start: done already
    assert rows()["K1ABC"][0] == "BOB"


# ---------------------------------------------------------------- QA BE-4: import when the FCC publishes


def _serve_head(monkeypatch, answer):
    """HEAD requests answered by `answer(request)` (an httpx.Response or an exception)."""
    seen = []

    def handle(self, request):
        seen.append(request)
        out = answer(request)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return seen


def test_published_extract_is_the_last_modified_date_of_a_head_request(monkeypatch):
    seen = _serve_head(monkeypatch, lambda r: httpx.Response(
        200, headers={"Last-Modified": "Sun, 25 Oct 2026 13:10:40 GMT"}))
    assert _PUBLISHED_EXTRACT() == datetime(2026, 10, 25, 13, 10, 40, tzinfo=timezone.utc)
    assert [(r.method, str(r.url)) for r in seen] == [("HEAD", get_settings().uls_url)]
    for answer in (lambda r: httpx.Response(200),                                   # no header
                   lambda r: httpx.Response(200, headers={"Last-Modified": "soon"}),  # unreadable
                   lambda r: httpx.Response(503),
                   lambda r: httpx.ConnectError("no route", request=r)):
        _serve_head(monkeypatch, answer)
        assert _PUBLISHED_EXTRACT() is None


def test_a_new_extract_is_imported_soon_after_the_fcc_publishes_it(mini_uls_zip, tmp_path, db, monkeypatch):
    """QA BE-4: imports ran ULS_REFRESH_DAYS after the previous one, so the
    directory lagged the FCC's Sunday extract by 6-13 days. ensure_fresh now
    asks the FCC (a HEAD request, daily) and imports when its extract is newer
    than the one the last import read; without a date, the old timer."""
    _clear_geocodes(db)
    s = get_settings()
    monkeypatch.setattr(s, "uls_import_enabled", True)
    monkeypatch.setattr(s, "data_release_enabled", False)
    sunday1 = datetime(2026, 10, 18, 13, 5, tzinfo=timezone.utc)
    sunday2 = datetime(2026, 10, 25, 13, 10, 40, tzinfo=timezone.utc)
    fcc = {"date": sunday1}
    downloads = []

    def fake_download(url, dest, max_age_hours=None):
        downloads.append(url)
        shutil.copy(mini_uls_zip, dest)
        return fcc["date"]  # the GET's Last-Modified

    monkeypatch.setattr(uls, "_download", fake_download)
    monkeypatch.setattr(uls, "published_extract", lambda: fcc["date"])
    assert uls.ensure_fresh() is True and len(downloads) == 1          # the first import
    assert uls._read_marker(str(tmp_path / ".uls_extract")) == sunday1
    assert uls.ensure_fresh() is True and len(downloads) == 1          # same extract: nothing to do
    fcc["date"] = sunday2
    assert uls.ensure_fresh() is True and len(downloads) == 2          # the FCC published: import now
    assert uls._read_marker(str(tmp_path / ".uls_extract")) == sunday2
    assert uls.ensure_fresh() is True and len(downloads) == 2
    # the FCC gives no date: ULS_REFRESH_DAYS after the last import, as before
    fcc["date"] = None
    assert uls.ensure_fresh() is True and len(downloads) == 2
    aged = datetime.now(timezone.utc) - timedelta(days=s.uls_refresh_days, minutes=1)
    (tmp_path / ".uls_last_import").write_text(aged.isoformat())
    assert uls.ensure_fresh() is True and len(downloads) == 3


def test_an_import_from_before_extract_dates_were_kept_compares_its_own_date(tmp_path, monkeypatch):
    """The live case: the last import (Oct 23) read the Oct 18 extract and no
    extract date was recorded; the FCC's Oct 25 extract is due at once, one
    published before that import is not."""
    imported = datetime(2026, 10, 23, 18, 57, 39, tzinfo=timezone.utc)
    (tmp_path / ".uls_last_import").write_text(imported.isoformat())
    monkeypatch.setattr(uls, "published_extract", lambda: datetime(2026, 10, 25, 13, 10, 40, tzinfo=timezone.utc))
    assert uls.new_extract_due(uls.last_import()) is True
    monkeypatch.setattr(uls, "published_extract", lambda: datetime(2026, 10, 18, 13, 5, tzinfo=timezone.utc))
    assert uls.new_extract_due(uls.last_import()) is False
