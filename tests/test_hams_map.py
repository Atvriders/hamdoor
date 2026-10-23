"""GET /api/hams/map, /search, /{callsign}, /count. No network: every
fixture seeds the hams table directly."""

import math
import random
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine
from app.integrations import uls
from app.models import Ham, User
from app.routes import hams as hams_route
from app.security import create_token, hash_password
from tests.conftest import auth

TODAY = date(2026, 10, 20)
_COLS = ("callsign", "name", "street", "city", "state", "zip", "email", "license_class",
         "expires", "granted", "lat", "lon", "loc_source")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """No network, no markers outside tmp_path, a fixed 'today'."""
    monkeypatch.setattr(uls, "_data_dir", lambda: str(tmp_path))

    def no_network(self, request):
        raise AssertionError(f"network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(hams_route, "_today", lambda: TODAY)


def mdy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def ham(callsign, lat, lon, loc="address", cls="E", zip_="06111", expires=None, granted=None,
        **kw):
    row = {"callsign": callsign, "name": kw.get("name", f"Name {callsign}"),
           "street": kw.get("street", f"1 {callsign} ST"), "city": kw.get("city", "Newington"),
           "state": kw.get("state", "CT"), "zip": zip_, "email": f"{callsign.lower()}@example.com",
           "license_class": cls,
           "expires": expires if expires is not None else mdy(TODAY + timedelta(days=3000)),
           "granted": granted if granted is not None else mdy(TODAY - timedelta(days=3000)),
           "lat": lat, "lon": lon, "loc_source": loc if lat is not None else ""}
    return row


def seed(rows, aggregate_on: date | None = None):
    """Replace the hams table content; optionally build the cluster aggregate."""
    uls.migrate_hams_table()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM hams"))
        conn.exec_driver_sql(
            f"INSERT INTO hams ({', '.join(_COLS)}, expires_iso, granted_iso)"
            f" VALUES ({', '.join('?' for _ in _COLS)}, ?, ?)",
            [tuple(r[c] for c in _COLS) + (uls.iso_date(r["expires"]), uls.iso_date(r["granted"]))
             for r in rows])
    if aggregate_on is not None:
        uls.rebuild_cluster_aggregate(aggregate_on)


def user_token(db, callsign):
    u = db.query(User).filter_by(callsign=callsign).one_or_none()
    if u is None:
        u = User(callsign=callsign, password_hash=hash_password("password123"),
                 email=f"{callsign.lower()}@example.com", name=callsign)
        db.add(u)
        db.commit()
    return create_token(u)


def get_map(client, tok, **params):
    r = client.get("/api/hams/map", params=params, headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- clusters


def test_cluster_bubble_sits_at_member_mean_not_cell_centre(client, db):
    """Regression: bubbles were drawn at the fixed cell centre (149 km off
    at z4); they must sit at AVG(lat), AVG(lon) of their members."""
    seed([ham("AA1AA", 41.70, -72.70), ham("AA1AB", 41.80, -72.80), ham("AA1AC", 41.90, -72.60)])
    tok = user_token(db, "W1MAPA")
    body = get_map(client, tok, min_lat=40, max_lat=43, min_lon=-74, max_lon=-71, zoom=7)
    assert body["type"] == "clusters" and body["reason"] == "zoom"
    assert body["zoom"] == 7 and body["cell_degrees"] == 1.0
    (cell,) = body["cells"]
    assert cell["count"] == 3 and body["total"] == 3
    assert cell["lat"] == pytest.approx(41.8, abs=1e-5)   # old code: 41.5 (cell centre)
    assert cell["lon"] == pytest.approx(-72.7, abs=1e-5)  # old code: -72.5


def test_cluster_bbox_snapped_to_whole_cells(client, db):
    """An edge bubble counts its whole cell, not just the visible sliver,
    so it doesn't change while panning — while `total` stays the number of
    hams in the view itself (§1)."""
    seed([ham("AA1BA", 41.95, -72.95), ham("AA1BB", 41.05, -72.05), ham("AA1BC", 41.2, -72.8)])
    tok = user_token(db, "W1MAPA")
    # the view ends at 41.5 / -72.5, mid-cell: all three share cell (41, -73),
    # only AA1BC is inside the view
    body = get_map(client, tok, min_lat=40.2, max_lat=41.5, min_lon=-73.9, max_lon=-72.5, zoom=7)
    assert [c["count"] for c in body["cells"]] == [3]
    assert body["total"] == 1  # review B1-R2-10, old code: 3 (the snapped bbox)


@pytest.mark.parametrize("zoom", [4, 6, 9])
@pytest.mark.parametrize("aggregate", [True, False])
def test_cluster_total_counts_the_requested_bbox(client, db, zoom, aggregate):
    """Regression (review B1-R2-10): `total` summed the cells of the
    outward-snapped bbox — 7,760 for a Manhattan z9 view holding 1,107
    hams. §1: total = filtered hams inside the bbox; the cells still cover
    whole cells. Same on the aggregate and the live path, with filters."""
    rows = [ham(f"AT{i:03d}", round(40.71 + (i % 10) * 0.01, 2), round(-74.0 + (i // 10) * 0.01, 2))
            for i in range(50)]                                             # in the view
    rows += [ham(f"AU{i:03d}", 40.9 + (i % 5) * 0.01, -73.8, cls="T") for i in range(30)]  # same cells
    seed(rows, aggregate_on=TODAY if aggregate else None)
    if not aggregate:
        with engine.begin() as conn:
            conn.execute(text("UPDATE hams_cluster_meta SET valid = 0"))
    tok = user_token(db, "W1MAPT")
    view = {"min_lat": 40.70, "max_lat": 40.80, "min_lon": -74.02, "max_lon": -73.93, "zoom": zoom}
    body = get_map(client, tok, **view)
    assert body["reason"] == "zoom"
    assert sum(c["count"] for c in body["cells"]) == 80
    assert body["total"] == 50                    # old code: 80
    body = get_map(client, tok, classes="T", **view)
    assert (body["total"], sum(c["count"] for c in body["cells"])) == (0, 30)
    body = get_map(client, tok, classes="E", **view)
    assert (body["total"], sum(c["count"] for c in body["cells"])) == (50, 50)


def test_zoomed_out_view_of_a_huge_bbox_is_bounded(client, db):
    """Below the detail zoom too, a bbox far larger than any screen gets
    cells coarse enough to stay within MAX_CELLS (a world bbox at z9 was
    9,734 cells / 426 KB on the replica); real screens are unaffected."""
    seed([ham("AW1AA", 41.7, -72.7), ham("KL7AW", 61.2, -149.9, zip_="99501", state="AK")],
         aggregate_on=TODAY)
    tok = user_token(db, "W1MAPF")
    body = get_map(client, tok, min_lat=-90, max_lat=90, min_lon=-180, max_lon=180, zoom=9)
    assert body["reason"] == "zoom" and body["total"] == 2
    cell = body["cell_degrees"]
    assert cell > 0.25                            # old code: 0.25 over 721 x 1441 cells
    assert (math.floor(90 / cell) - math.floor(-90 / cell) + 1) * \
        (math.floor(180 / cell) - math.floor(-180 / cell) + 1) <= hams_route.MAX_CELLS
    assert sum(c["count"] for c in body["cells"]) == 2
    screen = get_map(client, tok, min_lat=41.0, max_lat=42.6, min_lon=-74.5, max_lon=-70.5, zoom=9)
    assert screen["cell_degrees"] == 0.25 and screen["total"] == 1


@pytest.mark.parametrize("params", [
    {"min_lat": 10, "max_lat": 70, "min_lon": -250, "max_lon": -40, "zoom": 4},   # wide screen
    {"min_lat": 50, "max_lat": 72, "min_lon": -190.8, "max_lon": -120, "zoom": 5},  # Alaska
    {"min_lat": -100, "max_lat": 100, "min_lon": -400, "max_lon": 400, "zoom": 2},
    {"min_lat": 43, "max_lat": 40, "min_lon": -71, "max_lon": -74, "zoom": 7},   # swapped
    {"min_lat": "nan", "max_lat": 43, "min_lon": -74, "max_lon": "inf", "zoom": 7},
    {"min_lat": "-inf", "max_lat": "inf", "min_lon": "-inf", "max_lon": "inf", "zoom": 12},
    {"min_lat": "inf", "max_lat": "inf", "min_lon": -74, "max_lon": -71, "zoom": 7},
])
def test_out_of_range_bbox_is_clamped_not_rejected(client, db, params):
    """Regression: min_lon < -180 (any wide screen at the default zoom-4
    view) returned 422 and an empty map."""
    seed([ham("AA1CA", 41.7, -72.7), ham("KL7AA", 61.2, -149.9, zip_="99501", state="AK")])
    tok = user_token(db, "W1MAPA")
    body = get_map(client, tok, **params)
    if params["min_lat"] == "inf":
        assert body["total"] == 0  # a box above the pole is empty, not an error
        return
    assert body["type"] == ("clusters" if params["zoom"] < 10 else "points")
    assert body["total"] >= 1


@pytest.mark.parametrize("params,expected_total", [
    ({"min_lat": 1e300, "max_lat": 1e300, "min_lon": -74, "max_lon": -71, "zoom": 7}, 0),
    ({"min_lat": -1e300, "max_lat": -1e300, "min_lon": -74, "max_lon": -71, "zoom": 4}, 0),
    ({"min_lat": 40, "max_lat": 42, "min_lon": 1e300, "max_lon": 1e300, "zoom": 9}, 0),
    ({"min_lat": 40, "max_lat": 42, "min_lon": -1e300, "max_lon": -1e300, "zoom": 3}, 0),
    ({"min_lat": 1e300, "max_lat": 1e300, "min_lon": -74, "max_lon": -71, "zoom": 12}, 0),
    ({"min_lat": -1e300, "max_lat": 1e300, "min_lon": -1e300, "max_lon": 1e300, "zoom": 5}, 2),
    ({"min_lat": -1.7e308, "max_lat": 1.7e308, "min_lon": -1.7e308, "max_lon": 1.7e308, "zoom": 13}, 2),
])
def test_huge_finite_bbox_is_clamped_not_500(db, params, expected_total):
    """Regression (review B1-5): finite edges far out of range (1e300) got
    past clamp_bbox and floor(1e300 / cell) overflowed SQLite's integer: 500."""
    from fastapi.testclient import TestClient

    from app.main import app

    seed([ham("AA1CA", 41.7, -72.7), ham("KL7AA", 61.2, -149.9, zip_="99501", state="AK")])
    tok = user_token(db, "W1MAPA")
    r = TestClient(app, raise_server_exceptions=False).get("/api/hams/map", params=params,
                                                          headers=auth(tok))
    assert r.status_code == 200, r.text  # old code: 500 (OverflowError)
    assert r.json()["total"] == expected_total


def test_zoom_is_clamped(client, db):
    seed([ham("AA1DA", 41.7, -72.7)])
    tok = user_token(db, "W1MAPA")
    assert get_map(client, tok, min_lat=41, max_lat=42, min_lon=-73, max_lon=-72, zoom=40)["zoom"] == 22
    low = get_map(client, tok, min_lat=0, max_lat=60, min_lon=-130, max_lon=-60, zoom=-5)
    assert low["zoom"] == 0 and low["cell_degrees"] == 6.0


def test_dense_view_returns_fine_clusters_not_an_alphabetical_prefix(client, db, monkeypatch):
    """Regression: above the cap the old endpoint returned the first N
    callsigns alphabetically (every N/W call missing in big metros)."""
    rows = [ham(f"K{i:03d}A", 34.0 + (i % 10) * 0.01, -118.3 + (i // 10) * 0.01) for i in range(60)]
    rows += [ham(f"W{i:03d}B", 34.2, -118.1) for i in range(5)]
    seed(rows)
    monkeypatch.setattr(get_settings(), "hams_map_max_results", 50)
    tok = user_token(db, "W1MAPA")
    body = get_map(client, tok, min_lat=33.9, max_lat=34.3, min_lon=-118.4, max_lon=-118.0, zoom=10)
    assert body["type"] == "clusters" and body["reason"] == "dense"
    assert body["cell_degrees"] == 0.05
    assert body["total"] == 65
    assert sum(c["count"] for c in body["cells"]) == 65  # W-calls included
    # under the cap: points
    body = get_map(client, tok, min_lat=34.15, max_lat=34.3, min_lon=-118.2, max_lon=-118.0, zoom=12)
    assert body["type"] == "points" and body["total"] == 5


@pytest.mark.parametrize("zoom", [10, 11, 12, 13, 22])
def test_dense_view_of_a_huge_bbox_is_bounded(client, db, monkeypatch, zoom):
    """Regression (review B1-1): a world-sized bbox at a detail zoom returned
    one cell per occupied cell_degrees(zoom) square — 431k cells, 18.5 MB,
    5 s and +300 MB RSS on the replica for one request. A view far larger
    than any screen at that zoom gets the finest coarser cell that keeps the
    snapped bbox within 10,000 cells; every ham is still counted."""
    rng = random.Random(zoom)
    seed([ham(f"R{i:05d}", round(rng.uniform(25, 49), 6), round(rng.uniform(-124, -67), 6))
          for i in range(3000)])
    monkeypatch.setattr(get_settings(), "hams_map_max_results", 100)
    tok = user_token(db, "W1MAPF")
    body = get_map(client, tok, min_lat=-90, max_lat=90, min_lon=-180, max_lon=180, zoom=zoom)
    assert body["type"] == "clusters" and body["reason"] == "dense" and body["zoom"] == zoom
    cell = body["cell_degrees"]
    snapped = ((math.floor(90 / cell) - math.floor(-90 / cell) + 1)
               * (math.floor(180 / cell) - math.floor(-180 / cell) + 1))
    assert snapped <= 10_000          # old code: cell 0.05–0.00625, 25.9M–1.66G cells
    assert cell > hams_route.cell_degrees(zoom)
    assert len(body["cells"]) <= 10_000
    assert body["total"] == sum(c["count"] for c in body["cells"]) == 3000


def test_cluster_cell_keeps_the_zoom_size_for_real_screens():
    """Only oversized bboxes are coarsened: real screens keep
    cell_degrees(zoom); CONUS at z12 and the world get the finest ladder
    size within MAX_CELLS."""
    cluster_cell = hams_route._cluster_cell
    assert cluster_cell((32.9, 35.2, -120.9, -115.6), 10) == 0.05      # 3840x2160 at z10
    assert cluster_cell((40.0, 44.9, -75.0, -70.1), 10) == 0.05        # 98 x 99 cells
    assert cluster_cell((33.9, 34.2, -118.5, -118.0), 13) == 0.00625
    assert cluster_cell((24, 50, -125, -66), 12) == 0.5                # 0.25 would be 24.5k
    assert cluster_cell((-90, 90, -180, 180), 13) == 3.0
    assert cluster_cell((36.0, 42.1, -101.0, -87.0), 9) == 0.25        # 5120x2880 at z9
    assert cluster_cell((24, 50, -125, -66), 4) == 5.0
    assert cluster_cell((-90, 90, -180, 180), 9) == 3.0                # world: not 1.04M cells
    assert cluster_cell((-90, 90, -180, 180), 0) == 6.0
    assert hams_route.MAX_CELLS == 10_000


# ---------------------------------------------------------------- points


def test_points_group_every_ham_at_a_location(client, db):
    seed([
        ham("W1ZZZ", 41.714893, -72.726873),  # household of three at one street point
        ham("W1REG", 41.714893, -72.726873),
        ham("K1MMM", 41.714893, -72.726873),
        ham("N1SOLO", 41.70001, -72.71001),
        ham("KC1ZA", 41.69, -72.73, loc="zip", zip_="06111"),  # ZIP-level: one anchor
        ham("KC1ZB", 41.69, -72.73, loc="zip", zip_="06111"),
        ham("AA1NOL", None, None),
    ])
    tok = user_token(db, "W1MAPB")
    user_token(db, "W1REG")  # one of them has a hamdoor account
    body = get_map(client, tok, min_lat=41.6, max_lat=41.8, min_lon=-72.8, max_lon=-72.6, zoom=14)
    assert body["type"] == "points" and body["zoom"] == 14
    assert body["total"] == 6  # unplaced ham excluded
    pts = body["points"]
    assert [(p["lat"], p["lon"]) for p in pts] == sorted((p["lat"], p["lon"]) for p in pts)
    by_loc = {(p["lat"], p["lon"]): p for p in pts}
    home = by_loc[(41.714893, -72.726873)]
    assert home["loc"] == "address" and home["zip"] is None
    assert home["count"] == 3 and home["shared"] is False
    assert [h["callsign"] for h in home["hams"]] == ["K1MMM", "W1REG", "W1ZZZ"]
    assert {h["callsign"]: h["registered"] for h in home["hams"]} == \
        {"K1MMM": False, "W1REG": True, "W1ZZZ": False}
    zp = by_loc[(41.69, -72.73)]
    assert zp["loc"] == "zip" and zp["zip"] == "06111" and zp["count"] == 2
    assert set(home["hams"][0]) == {"callsign", "name", "city", "state", "license_class",
                                    "expired", "new", "granted", "registered"}


def test_shared_address_flag_only_for_street_points(client, db):
    rows = [ham(f"KD{i:02d}MD", 30.0, -81.7) for i in range(10)]
    rows += [ham(f"KE{i:02d}ZP", 30.1, -81.6, loc="zip", zip_="32043") for i in range(12)]
    rows += [ham(f"KF{i:02d}HH", 30.05, -81.65) for i in range(9)]
    seed(rows)
    tok = user_token(db, "W1MAPB")
    body = get_map(client, tok, min_lat=29.9, max_lat=30.2, min_lon=-81.8, max_lon=-81.5, zoom=13)
    shared = {(p["loc"], p["count"]): p["shared"] for p in body["points"]}
    assert shared == {("address", 10): True, ("zip", 12): False, ("address", 9): False}


def test_points_never_expose_street_or_email(client, db):
    seed([ham("W1PRV", 41.7, -72.7, street="99 SECRET LANE"),
          ham("W1PRZ", 41.71, -72.71, loc="zip")])
    tok = user_token(db, "W1MAPB")
    for params in ({"zoom": 14}, {"zoom": 6}):
        r = client.get("/api/hams/map", params={"min_lat": 41, "max_lat": 42, "min_lon": -73,
                                                "max_lon": -72, **params}, headers=auth(tok))
        assert "SECRET" not in r.text and "@example.com" not in r.text
    for path in ("/api/hams/W1PRV", "/api/hams/search?q=W1PR"):
        r = client.get(path, headers=auth(tok))
        assert r.status_code == 200
        assert "SECRET" not in r.text and "@example.com" not in r.text
        assert "street" not in r.text and "email" not in r.text


# ---------------------------------------------------------------- filters


def _filter_rows():
    t = TODAY
    return [
        ham("AE1E", 41.70, -72.70, cls="E"), ham("AA1A", 41.70, -72.71, cls="A"),
        ham("AG1G", 41.70, -72.72, cls="G"), ham("AT1T", 41.70, -72.73, cls="T"),
        ham("AN1N", 41.70, -72.74, cls="N"), ham("AP1P", 41.70, -72.75, cls="P"),
        ham("AC1CLB", 41.70, -72.76, cls=""),  # club / unknown
        ham("AX1EXP", 41.71, -72.70, cls="G", expires=mdy(t - timedelta(days=5))),
        ham("AW1NEW", 41.71, -72.71, cls="T", granted=mdy(t - timedelta(days=10))),
        ham("AZ1ZIP", 41.72, -72.72, cls="E", loc="zip"),
    ]


@pytest.mark.parametrize("params,expected", [
    ({}, {"AE1E", "AA1A", "AG1G", "AT1T", "AN1N", "AP1P", "AC1CLB", "AX1EXP", "AW1NEW", "AZ1ZIP"}),
    ({"classes": "E"}, {"AE1E", "AZ1ZIP"}),
    ({"classes": "N"}, {"AN1N", "AP1P"}),
    ({"classes": "U"}, {"AC1CLB"}),
    ({"classes": "g,t"}, {"AG1G", "AT1T", "AX1EXP", "AW1NEW"}),
    ({"classes": ""}, set()),
    ({"expired": "0"}, {"AE1E", "AA1A", "AG1G", "AT1T", "AN1N", "AP1P", "AC1CLB", "AW1NEW", "AZ1ZIP"}),
    ({"new": "false"}, {"AE1E", "AA1A", "AG1G", "AT1T", "AN1N", "AP1P", "AC1CLB", "AX1EXP", "AZ1ZIP"}),
    ({"approx": "0"}, {"AE1E", "AA1A", "AG1G", "AT1T", "AN1N", "AP1P", "AC1CLB", "AX1EXP", "AW1NEW"}),
    ({"classes": "G,T", "expired": "0", "new": "0"}, {"AG1G", "AT1T"}),
])
def test_filters_points_and_clusters_agree(client, db, params, expected):
    seed(_filter_rows(), aggregate_on=TODAY)
    tok = user_token(db, "W1MAPC")
    box = {"min_lat": 41.6, "max_lat": 41.8, "min_lon": -72.8, "max_lon": -72.6}
    pts = get_map(client, tok, zoom=14, **box, **params)
    got = {h["callsign"] for p in pts["points"] for h in p["hams"]}
    assert got == expected
    assert pts["total"] == len(expected)
    flags = {h["callsign"]: (h["expired"], h["new"]) for p in pts["points"] for h in p["hams"]}
    if "AX1EXP" in flags:
        assert flags["AX1EXP"] == (True, False)
    if "AW1NEW" in flags:
        assert flags["AW1NEW"] == (False, True)
    # the aggregate path and the live path give the same totals
    agg = get_map(client, tok, zoom=6, **box, **params)
    assert agg["total"] == len(expected)
    with engine.begin() as conn:
        conn.execute(text("UPDATE hams_cluster_meta SET valid = 0"))
    live = get_map(client, tok, zoom=6, **box, **params)
    assert_same_cells(live, agg)


def assert_same_cells(a, b):
    assert a["total"] == b["total"]
    assert [c["count"] for c in a["cells"]] == [c["count"] for c in b["cells"]]
    for ca, cb in zip(a["cells"], b["cells"]):
        assert ca["lat"] == pytest.approx(cb["lat"], abs=2e-5)
        assert ca["lon"] == pytest.approx(cb["lon"], abs=2e-5)


def test_bad_boolean_is_422(client, db):
    tok = user_token(db, "W1MAPC")
    r = client.get("/api/hams/map", params={"min_lat": 41, "max_lat": 42, "min_lon": -73,
                                            "max_lon": -72, "zoom": 5, "expired": "maybe"},
                   headers=auth(tok))
    assert r.status_code == 422


# ---------------------------------------------------------------- aggregate


def _random_hams(rng: random.Random, n: int, built_on: date):
    regions = [(41.7, -72.7), (34.05, -118.25), (61.2, -149.9), (18.4, -66.1), (39.1, -94.6)]
    rows = []
    for i in range(n):
        lat0, lon0 = rng.choice(regions)
        exp = built_on + timedelta(days=rng.choice([-400, -30, -1, 0, 1, 3, 10, 29, 44, 70, 900]))
        gr = built_on + timedelta(days=rng.choice([-5000, -120, -91, -90, -89, -60, -5, 0, 2, 20]))
        rows.append(ham(f"R{i:05d}", round(lat0 + rng.uniform(-6, 6), 6),
                        round(lon0 + rng.uniform(-9, 9), 6),
                        loc=rng.choice(["address", "address", "zip"]),
                        cls=rng.choice(["E", "A", "G", "T", "N", "P", "", "X"]),
                        expires=mdy(exp), granted=mdy(gr)))
    return rows


def test_aggregate_matches_live_query_across_days(db):
    """The import-time aggregate (+ correction for licenses whose expired /
    new status changed since it was built) must equal a live GROUP BY for
    every filter combination, view and day — checked over many calls."""
    rng = random.Random(7)
    built_on = date(2026, 9, 1)
    seed(_random_hams(rng, 1500, built_on), aggregate_on=built_on)
    new_days = get_settings().new_ham_days
    checked = 0
    for days_later in (0, 1, 13, 29, 45, 60):
        today = built_on + timedelta(days=days_later)
        for _ in range(25):
            zoom = rng.randint(0, 9)
            lat0, lon0 = rng.uniform(5, 60), rng.uniform(-170, -50)
            bbox = (lat0, lat0 + rng.uniform(1, 40), lon0, lon0 + rng.uniform(1, 80))
            f = hams_route.MapFilters(
                classes=rng.choice([None, frozenset("E"), frozenset("NU"), frozenset("AGT")]),
                expired=rng.random() < 0.5, new=rng.random() < 0.5, approx=rng.random() < 0.5,
                today=today, new_days=new_days)
            cell = uls.cell_degrees(zoom)
            rngc = (math.floor(bbox[0] / cell), math.floor(bbox[1] / cell),
                    math.floor(bbox[2] / cell), math.floor(bbox[3] / cell))
            with engine.connect() as conn:
                s = Session(bind=conn)
                agg = hams_route._cells_from_aggregate(s, cell, rngc, f)
                live = hams_route._cells_live(s, cell, rngc, f)
                s.close()
            assert agg is not None
            assert {k: v[0] for k, v in agg.items() if v[0]} == {k: v[0] for k, v in live.items()}
            for k, (n, slat, slon) in live.items():
                assert agg[k][1] / n == pytest.approx(slat / n, abs=1e-7)
                assert agg[k][2] / n == pytest.approx(slon / n, abs=1e-7)
            checked += 1
    assert checked == 150


def test_aggregate_ignored_after_direct_writes(client, db):
    """Rows written outside an import invalidate the aggregate (trigger), so
    the map never shows stale counts."""
    seed([ham("AA1TR", 41.7, -72.7)], aggregate_on=TODAY)
    tok = user_token(db, "W1MAPD")
    box = {"min_lat": 41, "max_lat": 42, "min_lon": -73, "max_lon": -72, "zoom": 6}
    assert get_map(client, tok, **box)["total"] == 1
    db.add(Ham(callsign="AA1TS", name="Late Addition", lat=41.75, lon=-72.75,
               loc_source="address"))
    db.commit()
    assert get_map(client, tok, **box)["total"] == 2
    uls.rebuild_cluster_aggregate(TODAY)
    assert get_map(client, tok, **box)["total"] == 2


def test_aggregate_too_old_for_filters_falls_back_to_live(client, db, monkeypatch):
    built = TODAY - timedelta(days=200)
    seed([ham("AA1OL", 41.7, -72.7, expires=mdy(TODAY - timedelta(days=50)))], aggregate_on=built)
    tok = user_token(db, "W1MAPD")
    box = {"min_lat": 41, "max_lat": 42, "min_lon": -73, "max_lon": -72, "zoom": 6}
    assert get_map(client, tok, **box)["total"] == 1
    assert get_map(client, tok, expired=0, **box)["total"] == 0


# ---------------------------------------------------------------- search / detail / count


def test_search_prefix_exact_first_then_shortest(client, db):
    seed([ham("W1AW", 41.7149, -72.7269), ham("W1AWX", 41.0, -72.0), ham("W1AWA", 41.0, -72.1),
          ham("W1AWAB", 41.0, -72.2), ham("KW1AW", 41.0, -72.3), ham("W1AWZ", None, None)])
    tok = user_token(db, "W1MAPE")
    r = client.get("/api/hams/search", params={"q": "w1-aw"}, headers=auth(tok))
    res = r.json()["results"]
    assert [x["callsign"] for x in res] == ["W1AW", "W1AWA", "W1AWX", "W1AWZ", "W1AWAB"]
    first = res[0]
    assert set(first) == {"callsign", "name", "city", "state", "lat", "lon", "loc",
                          "license_class", "expired", "new", "registered"}
    assert first["loc"] == "address"
    assert next(x for x in res if x["callsign"] == "W1AWZ")["loc"] is None
    assert client.get("/api/hams/search", params={"q": "W"}, headers=auth(tok)).json() == {"results": []}
    assert client.get("/api/hams/search", params={"q": "W1AW", "limit": 2},
                      headers=auth(tok)).json()["results"][1]["callsign"] == "W1AWA"
    assert client.get("/api/hams/search", params={"q": "W1"}).status_code == 401


def test_search_limit_capped(client, db):
    seed([ham(f"K9A{i:02d}", 40.0, -90.0) for i in range(40)])
    tok = user_token(db, "W1MAPE")
    res = client.get("/api/hams/search", params={"q": "K9A", "limit": 500}, headers=auth(tok))
    assert len(res.json()["results"]) == 25


def test_detail_reports_loc_and_zip(client, db):
    seed([ham("W1DTA", 41.7, -72.7), ham("W1DTZ", 41.69, -72.73, loc="zip", zip_="06111")])
    tok = user_token(db, "W1MAPE")
    a = client.get("/api/hams/W1DTA", headers=auth(tok)).json()
    z = client.get("/api/hams/w1dtz", headers=auth(tok)).json()
    assert a["loc"] == "address" and z["loc"] == "zip" and z["zip"] == "06111"
    assert client.get("/api/hams/not-a-call!", headers=auth(tok)).status_code == 404


def _member(db, callsign, precision):
    u = db.query(User).filter_by(callsign=callsign).one_or_none()
    if u is None:
        u = User(callsign=callsign, password_hash=hash_password("password123"),
                 email=f"{callsign.lower()}@example.com", name=callsign)
        db.add(u)
    u.map_precision = precision
    db.commit()


def test_grid_members_fcc_entry_does_not_reveal_membership(client, db):
    """A member who chose map precision 'grid' must not be marked
    `registered` on their public FCC dot, in search or in the detail view —
    that would pin their account to their licence address."""
    seed([ham("W1PRA", 41.70, -72.70), ham("W1PRG", 41.71, -72.71), ham("W1PRN", 41.72, -72.72)])
    _member(db, "W1PRA", "address")
    _member(db, "W1PRG", "grid")
    tok = user_token(db, "W1MAPG")
    expected = {"W1PRA": True, "W1PRG": False, "W1PRN": False}
    pts = get_map(client, tok, min_lat=41.6, max_lat=41.8, min_lon=-72.8, max_lon=-72.6, zoom=14)
    assert {h["callsign"]: h["registered"] for p in pts["points"] for h in p["hams"]} == expected
    res = client.get("/api/hams/search", params={"q": "W1PR"}, headers=auth(tok)).json()["results"]
    assert {r["callsign"]: r["registered"] for r in res} == expected
    for call, flag in expected.items():
        assert client.get(f"/api/hams/{call}", headers=auth(tok)).json()["registered"] is flag
    _member(db, "W1PRG", "address")  # switching back shows the membership again
    res = client.get("/api/hams/search", params={"q": "W1PRG"}, headers=auth(tok)).json()["results"]
    assert res[0]["registered"] is True


def test_count_reports_unplaced_and_misses(client, db):
    seed([ham("W1CNA", 41.7, -72.7), ham("W1CNZ", 41.6, -72.6, loc="zip"), ham("W1CNN", None, None)])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM geocode_misses"))
        conn.execute(text("INSERT INTO geocode_misses VALUES ('X|Y|CT|06111', 'No_Match', 1, '2026-10-01T00:00:00Z')"))
    tok = user_token(db, "W1MAPE")
    body = client.get("/api/hams/count", headers=auth(tok)).json()
    assert body["count"] == 3 and body["street_level"] == 1 and body["zip_level"] == 1
    assert body["unplaced"] == 1 and body["geocode_misses"] == 1


@pytest.mark.parametrize("aggregate", [False, True])
def test_class_breakdown_for_the_legend_chips(client, db, aggregate):
    """Every map response says how many hams of each class bucket it covers,
    so the layer chips can show counts at any zoom (N folds in the old Tech
    Plus 'P'; U is club / unknown)."""
    seed([ham("AA1AA", 41.70, -72.70, cls="E"), ham("AA1AB", 41.71, -72.71, cls="E"),
          ham("AA1AC", 41.72, -72.72, cls="G"), ham("AA1AD", 41.73, -72.73, cls="T"),
          ham("AA1AE", 41.74, -72.74, cls="P"), ham("AA1AF", 41.75, -72.75, cls=""),
          ham("AA1AG", 41.76, -72.76, cls="A")],
         aggregate_on=TODAY if aggregate else None)
    tok = user_token(db, "W1MAPB")
    want = {"E": 2, "A": 1, "G": 1, "T": 1, "N": 1, "U": 1}
    zoomed_out = get_map(client, tok, min_lat=40, max_lat=43, min_lon=-74, max_lon=-71, zoom=6)
    assert zoomed_out["type"] == "clusters" and zoomed_out["by_class"] == want
    zoomed_in = get_map(client, tok, min_lat=41.6, max_lat=41.8, min_lon=-72.8, max_lon=-72.6, zoom=12)
    assert zoomed_in["type"] == "points" and zoomed_in["by_class"] == want
    only_e = get_map(client, tok, min_lat=40, max_lat=43, min_lon=-74, max_lon=-71, zoom=6, classes="E")
    assert only_e["by_class"] == {**{k: 0 for k in want}, "E": 2}
