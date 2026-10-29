"""GET /api/places/search over the GeoNames places table. No network."""

import httpx
import pytest
from sqlalchemy import text

from app.db import engine
from app.integrations import uls
from app.models import User
from app.security import create_token, hash_password
from tests.conftest import auth

PLACES = [
    ("66210", "Overland Park", "KS", 38.93, -94.69, 4),
    ("66212", "Overland Park", "KS", 38.96, -94.68, 4),
    ("66213", "Overland Park", "KS", 38.90, -94.70, 4),
    ("62701", "Springfield", "IL", 39.80, -89.64, 4),
    ("62702", "Springfield", "IL", 39.82, -89.65, 4),
    ("65801", "Springfield", "MO", 37.22, -93.29, 4),
    ("01101", "Springfield", "MA", 42.10, -72.59, 4),
    ("01102", "Springfield", "MA", 42.11, -72.58, 4),
    ("01103", "Springfield", "MA", 42.12, -72.57, 4),
    ("37172", "Springfield", "TN", 36.50, -86.88, 4),
    ("77373", "Spring", "TX", 30.06, -95.38, 4),
    ("00725", "Caguas", "PR", 18.23, -66.04, 4),
    ("10001", "100% Town", "NY", 40.75, -73.99, None),
]


@pytest.fixture(autouse=True)
def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(uls, "_data_dir", lambda: str(tmp_path))

    def no_network(self, request):
        raise AssertionError(f"network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    uls.migrate_hams_table()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM places"))
        conn.exec_driver_sql(uls.PLACES_INSERT, uls.places_rows(PLACES))


@pytest.fixture()
def tok(db):
    u = db.query(User).filter_by(callsign="W1PLC").one_or_none()
    if u is None:
        u = User(callsign="W1PLC", password_hash=hash_password("password123"),
                 email="w1plc@example.com")
        db.add(u)
        db.commit()
    return create_token(u)


def search(client, tok, q, **kw):
    r = client.get("/api/places/search", params={"q": q, **kw}, headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_zip_lookup(client, tok):
    (r,) = search(client, tok, "66210")
    assert r == {"label": "66210 · Overland Park, KS", "place": "Overland Park", "state": "KS",
                 "zip": "66210", "lat": 38.93, "lon": -94.69, "kind": "zip"}
    assert search(client, tok, "66210-1234")[0]["zip"] == "66210"
    assert search(client, tok, "00725")[0]["label"] == "00725 · Caguas, PR"  # territories
    assert search(client, tok, "99999") == []


def test_city_prefix_one_row_per_place_at_mean_of_zips(client, tok):
    res = search(client, tok, "overland")
    assert res == [{"label": "Overland Park, KS", "place": "Overland Park", "state": "KS",
                    "zip": None, "lat": 38.93, "lon": -94.69, "kind": "city"}]


def test_more_zips_first_and_limit(client, tok):
    """A member with no location: the exact name first, then more ZIPs first."""
    res = search(client, tok, "spring")
    assert [r["label"] for r in res] == ["Spring, TX", "Springfield, MA", "Springfield, IL",
                                         "Springfield, MO", "Springfield, TN"]
    assert len(search(client, tok, "spring", limit=2)) == 2


def test_exact_name_then_nearest_to_the_member(client, db):
    """V1-06 regression: places were ranked by ZIP count alone, so a Kansas
    City member searching "Springfield" got Massachusetts and Illinois first
    (and "Lawrence" put Lawrenceville, Georgia ahead of Lawrence, Kansas)."""
    u = db.query(User).filter_by(callsign="W0PLK").one_or_none()
    if u is None:
        u = User(callsign="W0PLK", password_hash=hash_password("password123"),
                 email="w0plk@example.com", lat=39.03, lon=-94.64)
        db.add(u)
        db.commit()
    tok = create_token(u)
    assert [r["label"] for r in search(client, tok, "springfield")] == [
        "Springfield, MO", "Springfield, IL", "Springfield, TN", "Springfield, MA"]
    # the exact name beats a nearer or bigger prefix match
    assert [r["label"] for r in search(client, tok, "spring")][:2] == ["Spring, TX", "Springfield, MO"]
    assert search(client, tok, "springfield", limit=1)[0]["label"] == "Springfield, MO"


@pytest.mark.parametrize("q,labels", [
    ("Springfield, IL", ["Springfield, IL"]),
    ("springfield, m", ["Springfield, MA", "Springfield, MO"]),
    ("Springfield, Tennessee", ["Springfield, TN"]),
    ("Springfield, New", []),
    ("Springfield, Missouri", ["Springfield, MO"]),
])
def test_state_suffix_narrows(client, tok, q, labels):
    assert [r["label"] for r in search(client, tok, q)] == labels


def test_short_or_wildcard_queries(client, tok):
    assert search(client, tok, "s") == []
    assert search(client, tok, "") == []
    assert search(client, tok, "%%") == []                 # LIKE wildcards are literal
    assert [r["place"] for r in search(client, tok, "100%")] == ["100% Town"]
    assert search(client, tok, "__") == []


def test_auth_required(client):
    assert client.get("/api/places/search", params={"q": "spring"}).status_code == 401


# ---------------------------------------------------------------- QA BE-2: St./Ft./Mt. both ways

def _add_places(rows):
    with engine.begin() as conn:
        conn.exec_driver_sql(uls.PLACES_INSERT, uls.places_rows(rows))


def _zips(first: int, n: int, place: str, state: str, lat: float, lon: float):
    """n ZIP rows of one place, all at its centre (the ZIP count is its size)."""
    return [(f"{first + i:05d}", place, state, lat, lon, 4) for i in range(n)]


def _member(db, callsign, lat, lon):
    u = db.query(User).filter_by(callsign=callsign).one_or_none()
    if u is None:
        u = User(callsign=callsign, password_hash=hash_password("password123"),
                 email=f"{callsign.lower()}@example.com", lat=lat, lon=lon)
        db.add(u)
        db.commit()
    return create_token(u)


ABBREVIATED = [
    *_zips(63101, 3, "Saint Louis", "MO", 38.63, -90.19),
    ("48880", "Saint Louis", "MI", 43.41, -84.61, 4),
    ("43071", "Saint Louisville", "OH", 40.17, -82.42, 4),
    ("55101", "Saint Paul", "MN", 44.95, -93.09, 4),
    ("76101", "Fort Worth", "TX", 32.75, -97.33, 4),
    ("10550", "Mount Vernon", "NY", 40.91, -73.84, 4),
    ("00802", "St Thomas", "VI", 18.34, -64.93, 4),
    ("63670", "Sainte Genevieve", "MO", 37.98, -90.04, 4),
    ("06901", "Stamford", "CT", 41.05, -73.54, 4),
]


@pytest.mark.parametrize("q,first", [
    ("St. Louis", "Saint Louis, MO"), ("St Louis", "Saint Louis, MO"), ("st louis", "Saint Louis, MO"),
    ("Saint Louis", "Saint Louis, MO"), ("St.Louis", "Saint Louis, MO"), ("St. Louis, MI", "Saint Louis, MI"),
    ("St. Paul", "Saint Paul, MN"), ("St Paul", "Saint Paul, MN"),
    ("Ft Worth", "Fort Worth, TX"), ("Ft. Worth", "Fort Worth, TX"), ("Fort Worth", "Fort Worth, TX"),
    ("Mt Vernon", "Mount Vernon, NY"), ("Mt. Vernon", "Mount Vernon, NY"),
    ("Saint Thomas", "St Thomas, VI"), ("St. Thomas", "St Thomas, VI"),  # stored the short way
    ("Ste Genevieve", "Sainte Genevieve, MO"), ("St L", "Saint Louis, MO"),
])
def test_abbreviated_and_spelled_out_names_find_each_other(client, tok, q, first):
    """QA BE-2: GeoNames spells "Saint Louis", "Fort Worth", "Mount Vernon"
    out, so "St. Louis", "Ft Worth" and "Mt Vernon" found nothing — and the
    Virgin Islands' "St Thomas" was missed by "Saint Thomas"."""
    _add_places(ABBREVIATED)
    res = search(client, tok, q)
    assert res and res[0]["label"] == first, [r["label"] for r in res]


def test_an_abbreviation_changes_only_once_the_word_is_complete(client, tok):
    _add_places(ABBREVIATED)
    labels = [r["label"] for r in search(client, tok, "St")]  # still typing: Stamford, or "St ..."
    assert "Stamford, CT" in labels and "St Thomas, VI" in labels
    assert not any(label.startswith("Saint") for label in labels)
    # the exact name first, then the longer names it begins
    assert [r["label"] for r in search(client, tok, "St Louis")] == [
        "Saint Louis, MO", "Saint Louis, MI", "Saint Louisville, OH"]
    assert search(client, tok, "..") == [] and search(client, tok, "s.") == []


# ---------------------------------------------------------------- QA BE-3 / MAP-6: size against distance

SAME_NAMES = [
    *_zips(66601, 31, "Topeka", "KS", 39.05, -95.68), ("46571", "Topeka", "IN", 41.54, -85.54, 4),
    ("61567", "Topeka", "IL", 40.33, -89.93, 4),
    *_zips(43201, 45, "Columbus", "OH", 39.96, -83.00), ("08022", "Columbus", "NJ", 40.07, -74.72, 4),
    *_zips(31901, 14, "Columbus", "GA", 32.46, -84.99), ("16405", "Columbus", "PA", 41.94, -79.58, 4),
    *_zips(97201, 60, "Portland", "OR", 45.52, -122.68), *_zips(4101, 9, "Portland", "ME", 43.66, -70.26),
    ("06480", "Portland", "CT", 41.57, -72.62, 4), ("18351", "Portland", "PA", 40.92, -75.10, 4),
    *_zips(66044, 5, "Lawrence", "KS", 38.97, -95.24), *_zips(1840, 4, "Lawrence", "MA", 42.71, -71.16),
]
NEWINGTON = (41.71, -72.73)
KANSAS_CITY = (39.03, -94.64)


@pytest.mark.parametrize("where,q,top", [
    (NEWINGTON, "Topeka", ["Topeka, KS", "Topeka, IN", "Topeka, IL"]),  # was IN, IL, KS: Enter flew to Indiana
    (NEWINGTON, "Columbus", ["Columbus, OH", "Columbus, GA", "Columbus, NJ"]),  # was NJ, PA, ...: no Ohio
    (NEWINGTON, "Portland", ["Portland, CT", "Portland, ME", "Portland, OR"]),  # the town next door, then the cities
    (NEWINGTON, "Lawrence", ["Lawrence, MA", "Lawrence, KS"]),
    (KANSAS_CITY, "Lawrence", ["Lawrence, KS", "Lawrence, MA"]),  # V1-06 stays fixed
    (KANSAS_CITY, "Topeka", ["Topeka, KS", "Topeka, IL", "Topeka, IN"]),
])
def test_a_much_bigger_place_beats_a_far_smaller_one(client, db, where, q, top):
    """QA BE-3 / MAP-6: ranking same-name places by distance alone hid
    Topeka KS, Columbus OH and Portland OR from a Connecticut member behind
    villages that happened to be nearer (the map shows 3 rows and Enter takes
    the first). A much bigger place now wins unless a smaller one is close."""
    _add_places(SAME_NAMES)
    tok = _member(db, "W1PLD" if where == NEWINGTON else "W0PLD", *where)
    assert [r["label"] for r in search(client, tok, q, limit=len(top))] == top


# ---------------------------------------------------------------- QA R6: apostrophes, hyphens, spaces, accents

SPELLINGS = [
    *_zips(64063, 7, "Lees Summit", "MO", 38.91, -94.38),
    *_zips(63366, 2, "O Fallon", "MO", 38.81, -90.70), ("62269", "O Fallon", "IL", 38.59, -89.91, 4),
    ("66536", "Saint Marys", "KS", 39.19, -96.07, 4), ("45885", "Saint Marys", "OH", 40.54, -84.39, 4),
    *_zips(18701, 14, "Wilkes Barre", "PA", 41.25, -75.88),
    *_zips(27101, 25, "Winston-Salem", "NC", 36.10, -80.24),
    *_zips(22101, 6, "Mc Lean", "VA", 38.93, -77.18), ("68763", "Mclean", "NE", 42.39, -97.47, 4),
    ("83814", "Coeur d'Alene", "ID", 47.68, -116.78, 4),
    ("00680", "Mayaguez", "PR", 18.20, -67.14, 4),
]


@pytest.mark.parametrize("q,first", [
    ("Lee's Summit", "Lees Summit, MO"), ("Lee’s Summit", "Lees Summit, MO"), ("Lee's", "Lees Summit, MO"),
    ("Lees Summit", "Lees Summit, MO"),
    ("O'Fallon", "O Fallon, MO"), ("O’Fallon, IL", "O Fallon, IL"), ("OFallon", "O Fallon, MO"),
    ("St. Mary's", "Saint Marys, KS"), ("Saint Mary's", "Saint Marys, KS"),
    ("Wilkes-Barre", "Wilkes Barre, PA"), ("Wilkes Barre", "Wilkes Barre, PA"),
    ("Winston Salem", "Winston-Salem, NC"), ("Winston-Salem", "Winston-Salem, NC"), ("Winston-", "Winston-Salem, NC"),
    ("McLean", "Mc Lean, VA"), ("Mc Lean", "Mc Lean, VA"),
    ("Coeur D Alene", "Coeur d'Alene, ID"), ("Coeur d’Alene", "Coeur d'Alene, ID"),
    ("Mayagüez", "Mayaguez, PR"),
])
def test_apostrophes_hyphens_spaces_and_accents_never_matter(client, tok, q, first):
    """QA R6: GeoNames writes "Lees Summit" (a Kansas City suburb), "O Fallon",
    "Saint Marys", "Wilkes Barre", "Mc Lean" and "Winston-Salem", so "Lee's
    Summit", "O'Fallon", "St. Mary's", "Wilkes-Barre", "McLean" and "Winston
    Salem" found nothing. Both sides are compared as a key without them."""
    _add_places(SPELLINGS)
    res = search(client, tok, q)
    assert res and res[0]["label"] == first, [r["label"] for r in res]


def test_both_mclean_spellings_are_exact_matches(client, tok):
    _add_places(SPELLINGS)
    assert [r["label"] for r in search(client, tok, "McLean")] == ["Mc Lean, VA", "Mclean, NE"]
    assert [r["label"] for r in search(client, tok, "Mc Lean")] == ["Mc Lean, VA", "Mclean, NE"]
    assert search(client, tok, "O'") == []  # one letter of a name is not a search yet


def test_a_places_table_from_before_the_key_gets_one_at_startup(client, tok):
    """Databases built before the key: the startup migration adds the column,
    fills it from the names already there and indexes it, so search works
    before the next import rebuilds the table."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE places"))
        conn.execute(text("CREATE TABLE places (zip TEXT PRIMARY KEY, place TEXT NOT NULL, state TEXT NOT NULL,"
                          " lat REAL NOT NULL, lon REAL NOT NULL, accuracy INTEGER)"))
        conn.exec_driver_sql("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?)", SPELLINGS)
    uls.migrate_hams_table()
    with engine.begin() as conn:
        assert conn.execute(text("SELECT key FROM places WHERE zip = '64063'")).scalar() == "leessummit"
        assert conn.execute(text("SELECT COUNT(*) FROM places WHERE key = ''")).scalar() == 0
        indexes = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))}
        assert "ix_places_key" in indexes
        plan = " ".join(str(r[-1]) for r in conn.execute(text(
            "EXPLAIN QUERY PLAN SELECT place FROM places WHERE key GLOB 'leessum*'")))
        assert "ix_places_key" in plan  # a prefix search, not a scan of every ZIP
    assert search(client, tok, "Lee's Summit")[0]["label"] == "Lees Summit, MO"
