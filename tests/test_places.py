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
        conn.exec_driver_sql("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?)", PLACES)


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
