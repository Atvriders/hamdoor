"""ULS importer + hams map endpoints. Uses a tiny fabricated l_amat-style zip;
no network access."""

import zipfile
from datetime import date

import pytest

from app.integrations import uls
from app.models import Geocode, Ham
from tests.conftest import auth, signup


def _line(*fields_by_index, total):
    row = [""] * total
    for idx, val in fields_by_index:
        row[idx] = val
    return "|".join(row) + "\n"


@pytest.fixture()
def mini_uls_zip(tmp_path, monkeypatch):
    """A minimal ULS extract: one active individual, one active vanity,
    one terminated license that must be excluded."""
    hd = (
        _line((0, "HD"), (1, "1001"), (4, "W1QQQ"), (5, "A"), (6, "HA"), (7, "06/15/2020"), (8, "01/01/2035"), total=59) +
        _line((0, "HD"), (1, "1002"), (4, "W1NVV"), (5, "A"), (6, "HV"), (7, "01/15/2024"), (8, "02/02/2034"), total=59) +
        _line((0, "HD"), (1, "1003"), (4, "K0OLD"), (5, "T"), (6, "HA"), (7, "05/05/2000"), (8, "03/03/2010"), total=59)
    )
    en = (
        _line((0, "EN"), (1, "1001"), (4, "W1QQQ"), (8, "QUINN"), (9, "Q"), (10, "QUICK"),
              (14, "W1QQQ@EXAMPLE.COM"), (15, "10 OAK ST"), (16, "NEWINGTON"),
              (17, "CT"), (18, "06111"), total=30) +
        _line((0, "EN"), (1, "1002"), (4, "W1NVV"), (7, "VANITY CLUB"),
              (15, "20 ELM ST"), (16, "HARTFORD"), (17, "CT"), (18, "06103"), total=30) +
        _line((0, "EN"), (1, "1003"), (4, "K0OLD"), (7, "OLD TIMER"),
              (15, "1 MAIN ST"), (16, "NOWHERE"), (17, "KS"), (18, "67801"), total=30)
    )
    am = (
        _line((0, "AM"), (1, "1001"), (4, "W1QQQ"), (5, "E"), total=18) +
        _line((0, "AM"), (1, "1002"), (4, "W1NVV"), (5, ""), total=18) +
        _line((0, "AM"), (1, "1003"), (4, "K0OLD"), (5, "T"), total=18)
    )
    path = tmp_path / "mini_uls.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("HD.dat", hd)
        zf.writestr("EN.dat", en)
        zf.writestr("AM.dat", am)

    monkeypatch.setattr(uls, "load_zip_centroids",
                        lambda: {"06111": (41.69, -72.73), "06103": (41.77, -72.68)})
    return str(path)


def test_import_hams(mini_uls_zip, db):
    db.query(Geocode).delete()
    db.commit()
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
    # no geocode cached -> jittered ZIP centroid, stays near 06111
    assert q.lat == pytest.approx(41.69, abs=0.05)
    assert q.lon == pytest.approx(-72.73, abs=0.05)
    assert q.loc_source == "zip"

    v = db.get(Ham, "W1NVV")
    assert v is not None                       # vanity service HV included
    assert v.name == "Vanity Club"             # fell back to entity name
    assert db.get(Ham, "K0OLD") is None

    assert uls.last_import() is not None       # marker written


def test_import_uses_cached_street_geocode(mini_uls_zip, db):
    from app.integrations.census_geocoder import address_key
    db.query(Geocode).delete()
    db.commit()
    db.add(Geocode(address_key=address_key("10 OAK ST", "Newington", "CT", "06111"),
                   lat=41.71489, lon=-72.72687, quality="Exact"))
    db.commit()
    uls.import_hams(zip_path=mini_uls_zip)
    q = db.get(Ham, "W1QQQ")
    assert q.lat == pytest.approx(41.71489, abs=1e-5)   # exact street location
    assert q.lon == pytest.approx(-72.72687, abs=1e-5)
    assert q.loc_source == "address"
    # the un-geocoded ham still falls back to its ZIP centroid
    assert db.get(Ham, "W1NVV").loc_source == "zip"


def test_geocode_backfill(mini_uls_zip, db, monkeypatch):
    db.query(Geocode).delete()
    db.commit()
    uls.import_hams(zip_path=mini_uls_zip)
    missing = uls.missing_geocode_addresses()
    assert len(missing) == 2                   # both mini-zip hams have streets

    fake = {key: (41.7, -72.7, "Exact") for key, *_ in missing}

    def fake_batch(rows, on_chunk=None):
        if on_chunk:
            on_chunk(fake)
        return fake

    monkeypatch.setattr("app.integrations.census_geocoder.geocode_batch", fake_batch)
    added = uls.geocode_backfill()
    assert added == 2
    assert uls.missing_geocode_addresses() == []


def test_parse_census_response():
    from app.integrations.census_geocoder import parse_batch_response
    sample = (
        '"1","225 Main St, Newington, CT, 06111","Match","Exact",'
        '"225 MAIN ST, NEWINGTON, CT, 06111","-72.72687,41.71489","3514734","R"\n'
        '"2","1 Infinite Loop, Cupertino, CA, 95014","No_Match"\n'
        '"3","1 Main St, Nowhere, CT, 06001","Tie","Exact",'
        '"1 MAIN ST, NOWHERE, CT, 06001","-72.70,41.70","123","L"\n'
    )
    out = parse_batch_response(sample)
    assert out["1"] == (41.71489, -72.72687, "Exact")
    assert "2" not in out                       # No_Match dropped
    assert out["3"][0] == pytest.approx(41.70)  # Tie accepted


def test_import_swap_replaces_data(mini_uls_zip, db):
    uls.import_hams(zip_path=mini_uls_zip)
    db.add(Ham(callsign="EXTRA1", name="should vanish"))
    db.commit()
    uls.import_hams(zip_path=mini_uls_zip)     # second run swaps cleanly
    assert db.get(Ham, "EXTRA1") is None
    assert db.get(Ham, "W1QQQ") is not None


def test_lookup_falls_back_to_local_uls(client, mini_uls_zip, db):
    uls.import_hams(zip_path=mini_uls_zip)
    # W1QQQ is unknown to the (fake) live provider but present locally
    r = client.get("/api/lookup/W1QQQ")
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is True
    assert data["email"] == "w1qqq@example.com"
    assert "ULS" in data["source"]


def test_signup_via_local_uls_only(client, mini_uls_zip, db, monkeypatch):
    uls.import_hams(zip_path=mini_uls_zip)
    # no network geocoding in tests; the ham's ZIP centroid should be used
    monkeypatch.setattr("app.routes.auth.resolve_location", lambda *a: None)
    data = signup(client, "W1QQQ")
    assert data["user"]["name"] == "Quinn Q Quick"
    assert data["user"]["lat"] == pytest.approx(41.69, abs=0.05)


def _token(client, db, callsign):
    r = client.post("/api/auth/login", json={"callsign": callsign, "password": "password123"})
    if r.status_code == 200:
        return r.json()["token"]
    return signup(client, callsign)["token"]


def _seed_hams(db):
    db.query(Ham).delete(synchronize_session=False)
    recent = date.today().strftime("%m/%d/%Y")
    db.add_all([
        Ham(callsign="AA1A", lat=41.0, lon=-72.0, name="A", city="X", state="CT",
            license_class="E", expires="01/01/2099", granted=recent),  # brand new
        Ham(callsign="AA1B", lat=41.2, lon=-72.2, name="B", city="Y", state="CT",
            license_class="T", expires="01/01/2099", granted="06/15/2010"),
        Ham(callsign="AA1C", lat=35.0, lon=-100.0, name="C", city="Z", state="TX",
            license_class="G", expires="01/01/2000", granted="06/15/1990"),  # grace period
        Ham(callsign="NOLOC", lat=None, lon=None, name="N", city="?", state="??"),
    ])
    db.commit()


def test_hams_count_and_map(client, db):
    _seed_hams(db)
    tok = _token(client, db, "W1AAA")

    r = client.get("/api/hams/count", headers=auth(tok))
    assert r.status_code == 200
    assert r.json()["count"] == 4

    # close zoom -> individual hams, NOLOC excluded (no coordinates)
    r = client.get("/api/hams/map?min_lat=40&max_lat=42&min_lon=-73&max_lon=-71&zoom=12",
                   headers=auth(tok))
    body = r.json()
    assert body["type"] == "hams"
    assert {h["callsign"] for h in body["hams"]} == {"AA1A", "AA1B"}
    by_call = {h["callsign"]: h for h in body["hams"]}
    assert by_call["AA1A"]["license_class"] == "E"
    assert by_call["AA1A"]["expired"] is False
    assert by_call["AA1A"]["new"] is True        # granted today
    assert by_call["AA1B"]["new"] is False       # granted 2010

    # the Texas ham is past its expiration date -> flagged expired
    r = client.get("/api/hams/map?min_lat=34&max_lat=36&min_lon=-101&max_lon=-99&zoom=12",
                   headers=auth(tok))
    tx = r.json()["hams"][0]
    assert tx["callsign"] == "AA1C"
    assert tx["expired"] is True
    assert tx["license_class"] == "G"

    # wide zoom -> clusters with counts
    r = client.get("/api/hams/map?min_lat=30&max_lat=45&min_lon=-105&max_lon=-70&zoom=4",
                   headers=auth(tok))
    body = r.json()
    assert body["type"] == "clusters"
    assert sum(c["count"] for c in body["cells"]) == 3

    # bbox excludes the Texas ham
    r = client.get("/api/hams/map?min_lat=40&max_lat=42&min_lon=-73&max_lon=-71&zoom=4",
                   headers=auth(tok))
    assert sum(c["count"] for c in r.json()["cells"]) == 2


def test_ham_detail_privacy(client, db):
    _seed_hams(db)
    tok = _token(client, db, "W1BBB")
    r = client.get("/api/hams/AA1A", headers=auth(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["callsign"] == "AA1A"
    assert "email" not in body and "street" not in body
    assert r.status_code == 200
    assert client.get("/api/hams/ZZ9ZZ", headers=auth(tok)).status_code == 404
    assert client.get("/api/hams/AA1A").status_code == 401  # auth required
