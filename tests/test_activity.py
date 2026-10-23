"""Activity toolbox: parser unit tests + route tests with mocked collectors.

No network access happens in these tests.
"""

import socket
from types import SimpleNamespace

import httpx
import pytest

from app.config import get_settings
from app.geo import grid_to_latlon, haversine_miles
from app.integrations import (aprs_feed, aprs_parser, aprsis, bandcond, common, pota_sota, pskreporter, telnetfeed,
                              wsprlive)
from app.integrations.common import UpstreamError
from app.models import User
from app.routes import activity as activity_routes
from app.security import create_token
from tests.conftest import auth, set_location, signup

# ---------------------------------------------------------------- parsers

PSK_XML = """<?xml version="1.0"?>
<receptionReports>
  <receptionReport receiverCallsign="k1abc" receiverLocator="FN42aa"
    senderCallsign="w1aaa" senderLocator="FN31pr" frequency="14074000"
    mode="FT8" sNR="-12" flowStartSeconds="1700000000" />
  <receptionReport receiverCallsign="W9XYZ" receiverLocator="EM10"
    senderCallsign="W1DDD" senderLocator="FN31pr" frequency="7074000"
    mode="FT4" sNR="3" flowStartSeconds="1700000100" />
</receptionReports>
"""


def test_pskreporter_parse():
    spots = pskreporter.parse_pskreporter_xml(PSK_XML)
    assert len(spots) == 2
    s0 = spots[0]
    assert s0["receiver_callsign"] == "K1ABC"  # uppercased
    assert s0["sender_callsign"] == "W1AAA"
    assert s0["frequency_hz"] == 14074000
    assert s0["mode"] == "FT8"
    assert s0["snr"] == -12
    assert s0["time"].startswith("2023-11-14T22:13:20")


def test_pskreporter_spots_carry_grid_coordinates():
    # map layers need both ends as lat/lon (grid-square centres)
    s0, s1 = pskreporter.parse_pskreporter_xml(PSK_XML)
    assert (s0["receiver_lat"], s0["receiver_lon"]) == (pytest.approx(42.02083), pytest.approx(-71.95833))
    assert (s0["sender_lat"], s0["sender_lon"]) == (pytest.approx(41.72917), pytest.approx(-72.70833))
    assert (s1["receiver_lat"], s1["receiver_lon"]) == (30.5, -97.0)  # 4-character "EM10"
    assert pskreporter.grid_center("FN31pr41ab") == pskreporter.grid_center("FN31pr41")  # 10 chars
    assert pskreporter.grid_center("") is None and pskreporter.grid_center("ZZ99") is None


def test_pskreporter_parse_garbage():
    assert pskreporter.parse_pskreporter_xml("not xml") == []
    bad = '<r><receptionReport senderCallsign="X" frequency="n/a" sNR="?" /></r>'
    assert pskreporter.parse_pskreporter_xml(bad)[0]["frequency_hz"] == 0


def test_dxcluster_line_parse():
    spot = telnetfeed.parse_spot_line("DX de W1DDD:     14025.0  K1XYZ        CW op great fist  0123Z")
    assert spot["spotter"] == "W1DDD"
    assert spot["dx_callsign"] == "K1XYZ"
    assert spot["frequency_khz"] == 14025.0
    assert spot["frequency_mhz"] == 14.025
    assert spot["time"] == "0123Z"
    assert "great fist" in spot["comment"]


def test_rbn_line_parse():
    spot = telnetfeed.parse_spot_line("DX de SKIMMER:    7035.5  W1DDD   CW   12 dB  24 WPM  CQ      2345Z")
    assert spot["dx_callsign"] == "W1DDD"
    assert spot["frequency_khz"] == 7035.5
    assert "12 dB" in spot["comment"]


def test_spot_line_rejects_non_spots():
    assert telnetfeed.parse_spot_line("login: ") is None
    assert telnetfeed.parse_spot_line("WWV de K1ABC <01Z> : geomagnetic storm") is None
    assert telnetfeed.parse_spot_line("") is None


# Field layouts as served by the live APIs on 2026-10-20 (one request each).
POTA_LIVE = [{"spotId": 59174218, "activator": "KI7LPT", "frequency": "21345", "mode": "SSB",
              "reference": "US-12903", "parkName": None, "spotTime": "2026-10-20T18:11:21",
              "spotter": "WB9NDG", "comments": "", "source": "Web", "invalid": None,
              "name": "Ninemile Prairie State Fish and Wildlife Area", "locationDesc": "US-MT",
              "grid4": "DN36", "grid6": "DN36fw", "latitude": 46.9444, "longitude": -113.503,
              "count": 12, "expire": 954}]
SOTA_LIVE = [{"id": 413975, "timeStamp": "2026-10-20T18:17:57.721466Z", "activatorCallsign": "W7ODO",
              "activatorName": "Ron", "callsign": "W7ODO", "comments": "Crazy bands", "frequency": 14.286,
              "mode": "SSB", "summitCode": "W7W/CW-085", "summitName": "4100 Snooze Peak", "AltM": 1250,
              "AltFt": 4100, "points": 4, "userID": 99674, "type": "NORMAL",
              "latitude": 47.37110137939453, "longitude": -121.27880096435547},
             {"id": 413976, "timeStamp": "2026-10-20T18:16:00Z", "activatorCallsign": "K7XYZ",
              "frequency": None, "mode": "cw", "summitCode": "W7A/MN-0XX", "summitName": "",
              "latitude": None, "longitude": None}]


def test_wspr_rows_keep_numeric_coordinates():
    # ClickHouse JSON quotes UInt64 ("frequency": "14097081"); coordinates stay floats
    row = {"time": "2026-10-20 18:00:00", "band": 14, "tx_sign": "W1DDD", "tx_lat": 40.0, "tx_lon": -75.0,
           "rx_sign": "G0XYZ", "rx_lat": "51.4792", "rx_lon": 0.0, "distance": 5500,
           "frequency": "14097081", "power": 37, "snr": -20}
    (spot,) = wsprlive.parse_rows([row, "junk"])
    assert spot["frequency_hz"] == 14097081 and spot["rx_lat"] == 51.4792 and spot["tx_lon"] == -75.0
    assert wsprlive.parse_rows([{"tx_lat": "n/a", "rx_lat": 999}])[0]["tx_lat"] is None


def test_pota_spots_carry_park_coordinates():
    (spot,) = pota_sota.parse_pota(POTA_LIVE)
    assert (spot["lat"], spot["lon"]) == (46.9444, -113.503)
    assert spot["park_ref"] == "US-12903" and spot["park_name"].startswith("Ninemile")
    assert spot["frequency_khz"] == "21345" and spot["grid"] == "DN36fw"


def test_sota_fields_match_the_live_api():
    # old code: summit_name "" (read "summitDetails") and frequency_khz "14.286"
    # (MHz labelled kHz) or "None"
    s0, s1 = pota_sota.parse_sota(SOTA_LIVE)
    assert s0["summit"] == "W7W/CW-085" and s0["summit_name"] == "4100 Snooze Peak"
    assert s0["frequency_khz"] == "14286" and s0["activator"] == "W7ODO"
    assert (s0["lat"], s0["lon"]) == (pytest.approx(47.371101), pytest.approx(-121.278801))
    assert s1["frequency_khz"] == "" and s1["lat"] is None and s1["lon"] is None and s1["mode"] == "CW"
    legacy = pota_sota.parse_sota([{"associationCode": "W1", "summitCode": "CT-001", "frequency": 7.032}])
    assert legacy[0]["summit"] == "W1/CT-001" and legacy[0]["frequency_khz"] == "7032"


class _FakeAprsSocket:
    """Plays APRS-IS lines once, then reports the connection closed."""

    def __init__(self, lines):
        self.chunks = [b"# aprsc test\r\n", b"".join(ln + b"\r\n" for ln in lines)]
        self.sent = []

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_aprs_sample_logs_in_receive_only(monkeypatch, tmp_path):
    """Regression: the old sampler logged in as the *user* with a computed
    passcode (a verified login that kicks the user's own igate/app off
    APRS-IS). Now: the server's login and pass -1."""
    monkeypatch.setattr(get_settings(), "aprs_login", "HAMDOOR-T")
    monkeypatch.setattr(aprs_feed, "_login_cache", None)
    monkeypatch.setattr(aprs_feed, "_data_dir", lambda: str(tmp_path))
    sock = _FakeAprsSocket([b"W1BBB-9>APRS,TCPIP*:!4006.00N/07506.00W>090/030mobile",
                            b"K0OBJ>APRS:;146.94-PA*111111z4000.60N/07500.00Wrrepeater",
                            b"W9FAR>APRS:!4500.00N/09000.00W-far away"])
    monkeypatch.setattr(aprs_feed, "_open_connection", lambda h, p, t: sock)
    stations = aprsis.sample_aprs(40.0, -75.0, 50)
    assert sock.sent == [b"user HAMDOOR-T pass -1 vers hamdoor 1.0.0 filter r/40.000/-75.000/50\r\n"]
    assert [s["key"] for s in stations] == ["obj:146.94-PA", "W1BBB-9"]  # nearest first, W9FAR outside
    assert stations[0]["distance_km"] == pytest.approx(1.1, abs=0.1)
    assert not hasattr(aprsis, "aprs_passcode")


SOLAR_XML = """<?xml version="1.0"?>
<solar><solardata>
<updated>14 Nov 2023 2355 GMT</updated>
<solarflux>141</solarflux><aindex>8</aindex><kindex>2</kindex>
<sunspots>110</sunspots><xray>B9.9</xray><solarwind>420</solarwind>
<magneticfield>QUIET</magneticfield>
<band name="80m-40m" time="day">Fair</band>
<band name="80m-40m" time="night">Good</band>
<band name="30m-20m" time="day">Good</band>
</solardata></solar>
"""


def test_bandcond_parse():
    data = bandcond.parse_solarxml(SOLAR_XML)
    assert data["solar_flux"] == "141"
    assert data["k_index"] == "2"
    assert data["geomag"] == "QUIET"
    assert {b["band"] for b in data["bands"]} == {"80m-40m", "30m-20m"}
    day = next(b for b in data["bands"] if b["time"] == "day" and b["band"] == "80m-40m")
    assert day["condition"] == "Fair"


# ---------------------------------------------------------------- routes


@pytest.fixture()
def token(client, db):
    r = client.post("/api/auth/login", json={"callsign": "W1DDD", "password": "password123"})
    tok = r.json()["token"] if r.status_code == 200 else signup(client, "W1DDD")["token"]
    set_location(db, "W1DDD", 40.0, -75.0)
    return tok


def test_pskreporter_route(client, token, monkeypatch):
    monkeypatch.setattr(activity_routes.pskreporter, "query_pskreporter",
                        lambda **kw: pskreporter.parse_pskreporter_xml(PSK_XML))
    r = client.get("/api/activity/pskreporter?direction=sent", headers=auth(token))
    assert r.status_code == 200
    spots = r.json()["spots"]
    assert len(spots) == 2
    # FN42aa is ~60 mi from the test location; distance filled in
    assert spots[0]["distance_miles"] is not None


def test_bands_route(client, token, monkeypatch):
    monkeypatch.setattr(activity_routes.bandcond, "query_band_conditions",
                        lambda: bandcond.parse_solarxml(SOLAR_XML))
    r = client.get("/api/activity/bands", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["solar_flux"] == "141"


def test_bands_route_upstream_down(client, token, monkeypatch):
    activity_routes.activity_cache.set("bands", None, -1)  # force miss
    monkeypatch.setattr(activity_routes.bandcond, "query_band_conditions", lambda: None)
    r = client.get("/api/activity/bands", headers=auth(token))
    assert r.status_code == 502


def test_dxc_and_rbn_routes(client, token, monkeypatch):
    line = "DX de W1DDD:     14025.0  K1XYZ        CW op            0123Z"
    parsed = [telnetfeed.parse_spot_line(line)]
    monkeypatch.setattr(activity_routes.telnetfeed, "sample_dxcluster", lambda cs: parsed)
    monkeypatch.setattr(activity_routes.telnetfeed, "sample_rbn", lambda cs: parsed)

    r = client.get("/api/activity/dxcluster", headers=auth(token))
    assert r.status_code == 200 and r.json()["spots"][0]["dx_callsign"] == "K1XYZ"

    r = client.get("/api/activity/rbn?mine_only=true", headers=auth(token))
    assert r.status_code == 200
    # mine_only filters to spots OF my callsign; the mocked spot is K1XYZ
    assert r.json()["spots"] == []


def test_aprs_route_sample_mode_when_feed_disabled(client, token, monkeypatch):
    activity_routes.activity_cache.set("aprs:40.0:-75.0:50", None, -1)  # force a miss
    sample = [{"key": "W1BBB-9", "call": "W1BBB-9", "from": "W1BBB-9", "kind": "station", "cat": "mobile",
               "sym": {"table": "/", "code": ">", "overlay": None}, "lat": 40.1, "lon": -75.1,
               "comment": "mobile", "distance_km": 14.0}]
    seen = []
    monkeypatch.setattr(activity_routes.aprsis, "sample_aprs",
                        lambda lat, lon, km: seen.append((lat, lon, km)) or [dict(s) for s in sample])
    r = client.get("/api/activity/aprs?radius_km=50", headers=auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "sample" and body["sample_seconds"] == get_settings().activity_sample_seconds
    # no callsign, no passcode; the sample covers the whole 0.1-degree cell (B3-2R5)
    assert seen == [(40.0, -75.0, 50 + activity_routes._SAMPLE_CELL_MARGIN_KM)]
    st = body["stations"][0]
    assert st["callsign"] == "W1BBB-9" and st["object"] == "" and st["comment"] == "mobile"
    assert st["distance_miles"] == pytest.approx(8.7, abs=0.05) and st["cat"] == "mobile"
    assert "distance_km" not in st


def test_aprs_route_served_from_live_store(client, token, monkeypatch):
    """With the feed running, the Activity tab reads the rolling store (no
    upstream connection per request) - multi-call: newer packets show up."""
    t = {"now": 1_800_000_000.0}
    monkeypatch.setattr(aprs_feed, "_clock", lambda: t["now"])
    store = aprs_feed.StationStore(retention_s=6 * 3600)
    monkeypatch.setattr(aprs_feed, "_store", store)
    monkeypatch.setattr(aprs_feed, "is_running", lambda: True)
    monkeypatch.setattr(aprs_feed, "is_connected", lambda: True)
    monkeypatch.setattr(activity_routes.aprsis, "sample_aprs",
                        lambda *a: pytest.fail("must not sample when the feed runs"))
    for line in (b"W1BBB-9>APRS,TCPIP*:!4006.00N/07506.00W>090/030mobile",
                 b"K0OBJ>APRS:;146.94-PA*111111z4000.60N/07500.00Wrrepeater"):
        store.ingest(aprs_parser.parse_line(line), t["now"] - 60)
    body = client.get("/api/activity/aprs?radius_km=50", headers=auth(token)).json()
    assert body["mode"] == "feed" and body["window_s"] == 3600 and body["total"] == 2
    obj, car = body["stations"]
    assert obj["callsign"] == "K0OBJ" and obj["object"] == "146.94-PA" and obj["cat"] == "repeater"
    assert car["callsign"] == "W1BBB-9" and car["object"] == "" and car["speed_kmh"] == 55.6
    assert obj["distance_miles"] < car["distance_miles"]
    store.ingest(aprs_parser.parse_line(b"N3NEW>APRS:!4000.00N/07500.00W-just heard"), t["now"])
    body = client.get("/api/activity/aprs?radius_km=50", headers=auth(token)).json()
    assert body["stations"][0]["callsign"] == "N3NEW" and body["stations"][0]["distance_miles"] == 0.0
    t["now"] += 2 * 3600  # older than the default 1 h window
    assert client.get("/api/activity/aprs?radius_km=50", headers=auth(token)).json()["total"] == 0
    assert client.get("/api/activity/aprs?radius_km=50&window=10800",
                      headers=auth(token)).json()["total"] == 3


def test_pota_sota_routes(client, token, monkeypatch):
    monkeypatch.setattr(activity_routes.pota_sota, "query_pota", lambda limit=100: [
        {"activator": "W1BBB", "frequency_khz": "14250", "mode": "SSB",
         "park_ref": "US-0001", "park_name": "Test Park", "grid": "FN31",
         "spotter": "K1ZZZ", "comments": "", "time": "2023-11-14T22:00:00Z", "source": "POTA"}])
    monkeypatch.setattr(activity_routes.pota_sota, "query_sota", lambda limit=100: [
        {"activator": "W9CCC", "frequency_khz": "7032", "mode": "CW",
         "summit": "W1/CT-001", "summit_name": "Test Summit", "lat": 41.0, "lon": -73.0,
         "spotter": "K1ZZZ", "comments": "", "time": "2023-11-14T22:00:00Z", "source": "SOTA"}])

    r = client.get("/api/activity/pota", headers=auth(token))
    assert r.status_code == 200 and r.json()["spots"][0]["park_ref"] == "US-0001"
    r = client.get("/api/activity/sota", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["spots"][0]["distance_miles"] is not None


def test_wspr_route(client, token, monkeypatch):
    monkeypatch.setattr(activity_routes.wsprlive, "query_wspr", lambda cs: [
        {"time": "2023-11-14 22:00:00", "band_m": 20, "tx_callsign": "W1DDD",
         "tx_lat": 40.0, "tx_lon": -75.0, "rx_callsign": "G0XYZ",
         "rx_lat": 51.0, "rx_lon": 0.0, "distance_km": 5500,
         "frequency_hz": 14095600, "power_dbm": 37, "snr": -20}])
    r = client.get("/api/activity/wspr", headers=auth(token))
    assert r.status_code == 200
    spot = r.json()["spots"][0]
    # my station is the TX end, so distance is to the RX (G0XYZ ~3400 mi)
    assert spot["distance_miles"] > 3000


def test_activity_requires_auth(client):
    assert client.get("/api/activity/bands").status_code == 401
    assert client.get("/api/activity/dxcluster").status_code == 401


# ---------------------------------------------------------------- review fixes (B3-R*)

@pytest.mark.parametrize("mhz,khz", [
    (14.286, "14286"), (7.0631, "7063.1"), ("144.2", "144200"), (432.0655, "432065.5"),
    (1296.2, "1296200"), (10368.1, "10368100"), (24192.05, "24192050"),
    (0.0, ""), (-7.032, ""), (None, ""), ("nan", ""), ("inf", ""), ("junk", ""),
])
def test_sota_frequency_khz_is_fixed_point_text(mhz, khz):
    # B3-R8: "%g" printed 1296.2 MHz as "1.2962e+06" and rounded 432.0655 to
    # "432066"; the live feed also sends 0.0 for "no frequency"
    (spot,) = pota_sota.parse_sota([{"summitCode": "W6/CT-001", "frequency": mhz, "mode": "FM"}])
    assert spot["frequency_khz"] == khz


def test_sota_spotter_is_the_callsign_field():
    # B3-R9: the live API has no spotterCallsign; `callsign` is the spotter
    # (it differs from activatorCallsign in 25 of 49 live spots, e.g. RBNHOLE)
    live = {**SOTA_LIVE[0], "activatorCallsign": "WJ7WJ", "callsign": "K7ATN"}
    (spot,) = pota_sota.parse_sota([live])
    assert spot["activator"] == "WJ7WJ" and spot["spotter"] == "K7ATN"
    (rbn,) = pota_sota.parse_sota([{**live, "callsign": "RBNHOLE"}])
    assert rbn["spotter"] == "RBNHOLE"
    (no_activator,) = pota_sota.parse_sota([{**live, "activatorCallsign": None}])
    assert no_activator["activator"] == "" and no_activator["spotter"] == "K7ATN"
    (self_spot,) = pota_sota.parse_sota(SOTA_LIVE[:1])
    assert self_spot["activator"] == self_spot["spotter"] == "W7ODO"


@pytest.fixture()
def live_store(monkeypatch):
    t = {"now": 1_800_000_000.0}
    monkeypatch.setattr(aprs_feed, "_clock", lambda: t["now"])
    store = aprs_feed.StationStore(retention_s=6 * 3600)
    monkeypatch.setattr(aprs_feed, "_store", store)
    monkeypatch.setattr(aprs_feed, "is_running", lambda: True)
    monkeypatch.setattr(activity_routes.aprsis, "sample_aprs",
                        lambda *a: pytest.fail("must not sample when the feed runs"))
    for i in range(5):
        line = f"N{i}ABC>APRS,TCPIP*:!40{i:02d}.00N/07500.00W-station {i}".encode()
        store.ingest(aprs_parser.parse_line(line), t["now"] - 60)
    return store


def test_aprs_feed_mode_reports_its_window_as_the_sample(client, token, live_store):
    # B3-R4: feed mode sent sample_seconds null, and today's Activity tab
    # prints `${sample_seconds}s live sample` -> "(nulls live sample)"
    body = client.get("/api/activity/aprs?radius_km=50", headers=auth(token)).json()
    assert body["mode"] == "feed" and body["sample_seconds"] == body["window_s"] == 3600
    body = client.get("/api/activity/aprs?radius_km=50&window=10800", headers=auth(token)).json()
    assert body["sample_seconds"] == body["window_s"] == 10800


def test_aprs_feed_mode_builds_only_the_rows_it_returns(client, token, live_store, monkeypatch):
    # B3-R7: every station in the radius was turned into JSON under the store
    # lock, then all but activity_max_spots were thrown away
    monkeypatch.setattr(get_settings(), "activity_max_spots", 2)
    built = []
    real = aprs_feed.StationStore._json
    monkeypatch.setattr(aprs_feed.StationStore, "_json",
                        lambda self, st, *a: built.append(st.key) or real(self, st, *a))
    body = client.get("/api/activity/aprs?radius_km=50", headers=auth(token)).json()
    assert body["total"] == 5 and [s["callsign"] for s in body["stations"]] == ["N0ABC", "N1ABC"]
    assert built == ["N0ABC", "N1ABC"]


# ---------------------------------------------------------------- second review (B3-2R*)

def _user(db, call, lat, lon):
    """A token for an account of the test's own at (lat, lon). No signup:
    tests/test_auth.py signs up the fake provider's callsigns and expects
    them to be new in the shared test database."""
    user = db.query(User).filter_by(callsign=call).one_or_none()
    if user is None:
        user = User(callsign=call, password_hash="!")
        db.add(user)
    user.lat, user.lon = lat, lon
    db.commit()
    return create_token(user)


def _miles(lat1, lon1, lat2, lon2):
    return round(aprs_feed._haversine_km(lat1, lon1, lat2, lon2) / 1.609344, 1)


def test_aprs_sample_mode_ranks_the_shared_sample_per_user(client, db, monkeypatch):
    """B3-2R5, multi-call: with the feed off, users in one 0.1-degree cell
    share a sample, but it was taken around the first asker and stored with
    that user's radius cut, order and distances, served to everyone in the
    cell for 30 s (up to ~14 km apart). Now: one sample per cell, centred on
    the cell and radius + 8 km wide; every user gets their own rows."""
    monkeypatch.setattr(activity_routes, "activity_cache", common.TTLCache())
    tok_a = _user(db, "K0RA", 40.04, -75.04)
    tok_b = _user(db, "K0RB", 39.96, -74.96)  # same cell (40.0, -75.0), ~11 km away
    where = {"K0N": (40.45, -75.08),  # 45.7 km from A, 55.4 km from B
             "K0S": (39.55, -74.92),  # 55.4 km from A, 45.7 km from B
             "K0MID": (40.02, -75.0)}  # close to both
    calls = []

    def fake_sample(lat, lon, km):
        """Like sample_aprs: stations within km of (lat, lon), nearest first."""
        calls.append((lat, lon, km))
        rows = [{"key": k, "call": k, "from": k, "kind": "station", "cat": "fixed", "lat": la, "lon": lo,
                 "comment": "", "distance_km": round(aprs_feed._haversine_km(lat, lon, la, lo), 1)}
                for k, (la, lo) in where.items()]
        return sorted((r for r in rows if r["distance_km"] <= km), key=lambda r: r["distance_km"])
    monkeypatch.setattr(activity_routes.aprsis, "sample_aprs", fake_sample)
    a = client.get("/api/activity/aprs?radius_km=50", headers=auth(tok_a)).json()
    b = client.get("/api/activity/aprs?radius_km=50", headers=auth(tok_b)).json()
    a2 = client.get("/api/activity/aprs?radius_km=50", headers=auth(tok_a)).json()
    assert [(s["callsign"], s["distance_miles"]) for s in a["stations"]] == [
        (k, _miles(40.04, -75.04, *where[k])) for k in ("K0MID", "K0N")]
    assert [(s["callsign"], s["distance_miles"]) for s in b["stations"]] == [
        (k, _miles(39.96, -74.96, *where[k])) for k in ("K0MID", "K0S")]
    assert a["total"] == b["total"] == 2 and a2 == a  # B's request did not rewrite the shared sample
    assert calls == [(40.0, -75.0, 58)]  # one sample for the cell, wide enough for both users


def test_pota_sota_distances_are_each_users_own(client, db, monkeypatch):
    """Same flaw as B3-2R5, in POTA/SOTA (pre-B3): one cache entry for
    everyone, with distance_miles computed for whoever filled it, so a user
    in Kansas was shown a Connecticut user's distances for 60 s."""
    monkeypatch.setattr(activity_routes, "activity_cache", common.TTLCache())
    tok_a = _user(db, "K0RA", 41.7, -72.7)  # Hartford
    tok_c = _user(db, "K0RC", 37.75, -100.0)  # Dodge City
    fetched = []
    pota = [{"activator": "K0P", "frequency_khz": "14250", "mode": "SSB", "park_ref": "US-1", "park_name": "P",
             "grid": "EM29", "lat": 39.0, "lon": -94.6, "spotter": "", "comments": "", "time": "", "source": "POTA"},
            {"activator": "K0Q", "frequency_khz": "7200", "mode": "SSB", "park_ref": "US-2", "park_name": "Q",
             "grid": "EM29", "lat": None, "lon": None, "spotter": "", "comments": "", "time": "", "source": "POTA"}]
    sota = [{"activator": "K0S", "frequency_khz": "14062", "mode": "CW", "summit": "W0C/FR-001",
             "summit_name": "S", "lat": 39.0, "lon": -94.6, "spotter": "", "comments": "", "time": "",
             "source": "SOTA"}]
    monkeypatch.setattr(activity_routes.pota_sota, "query_pota",
                        lambda limit=100: fetched.append("pota") or [dict(s) for s in pota])
    monkeypatch.setattr(activity_routes.pota_sota, "query_sota",
                        lambda limit=100: fetched.append("sota") or [dict(s) for s in sota])
    em29 = grid_to_latlon("EM29")
    for path in ("/api/activity/pota", "/api/activity/sota"):
        a = [s["distance_miles"] for s in client.get(path, headers=auth(tok_a)).json()["spots"]]
        c = [s["distance_miles"] for s in client.get(path, headers=auth(tok_c)).json()["spots"]]
        again = [s["distance_miles"] for s in client.get(path, headers=auth(tok_a)).json()["spots"]]
        assert a[0] == round(haversine_miles(41.7, -72.7, 39.0, -94.6), 1)
        assert c[0] == round(haversine_miles(37.75, -100.0, 39.0, -94.6), 1)
        assert again == a  # C's request did not rewrite the cached spots
        if path.endswith("pota"):  # no coordinates: each user's distance to the grid centre
            assert (a[1], c[1]) == (round(haversine_miles(41.7, -72.7, *em29), 1),
                                    round(haversine_miles(37.75, -100.0, *em29), 1))
    assert fetched == ["pota", "sota"]  # upstream still fetched once per cache period


# ---------------------------------------------------------------- G11: upstream failures are 502s (B4)

def _http(monkeypatch, handler):
    """Answer every httpx request with handler(request) — no network."""
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: handler(request))


_HTTP_FETCHERS = {
    "pskreporter": lambda: pskreporter.query_pskreporter(sender="W1AW"),
    "wspr": lambda: wsprlive.query_wspr("W1AW"),
    "pota": lambda: pota_sota.query_pota(),
    "sota": lambda: pota_sota.query_sota(),
}
_EMPTY_REPLIES = {  # what each source sends when it has nothing to report
    "pskreporter": '<?xml version="1.0"?><receptionReports currentSeconds="1792200000"/>',
    "wspr": '{"meta": [], "data": [], "rows": 0}',
    "pota": "[]",
    "sota": "[]",
}


@pytest.mark.parametrize("name", list(_HTTP_FETCHERS))
@pytest.mark.parametrize("failure", ["503", "429", "404", "connect", "timeout", "html", "xhtml", "wrong-json"])
def test_http_sources_raise_when_the_upstream_fails(monkeypatch, name, failure):
    """G11 regression: every fetcher swallowed a failure and returned [], so
    "PSK Reporter is down" read as "nobody heard you" (and was cached)."""
    def handler(request):
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "html":
            return httpx.Response(200, text="<!DOCTYPE html><html><body>Busy<br></body></html>")
        if failure == "xhtml":
            return httpx.Response(200, text="<html><body>Busy</body></html>")
        if failure == "wrong-json":  # JSON, but not this source's shape
            return httpx.Response(200, json={"error": "busy"} if name != "wspr" else [{"error": "busy"}])
        return httpx.Response(int(failure), text="busy")

    _http(monkeypatch, handler)
    with pytest.raises(UpstreamError):
        _HTTP_FETCHERS[name]()


@pytest.mark.parametrize("name", list(_HTTP_FETCHERS))
def test_http_sources_return_empty_when_the_upstream_answered_nothing(monkeypatch, name):
    _http(monkeypatch, lambda request: httpx.Response(200, text=_EMPTY_REPLIES[name]))
    assert _HTTP_FETCHERS[name]() == []


def test_pskreporter_reply_with_a_namespace_still_parses(monkeypatch):
    xml = PSK_XML.replace("<receptionReports>", '<receptionReports xmlns="http://pskreporter.info/x">')
    _http(monkeypatch, lambda request: httpx.Response(200, text=xml))
    assert len(pskreporter.query_pskreporter(sender="W1AW")) == 2


class _ScriptedSocket:
    """A telnet / APRS-IS server playing a script: bytes are sent, None is
    2 s of silence, b"" hangs up, an exception instance is raised. Every
    recv moves the fake clock 1 s; after the script, silence."""

    def __init__(self, script, clock):
        self.script, self.clock, self.sent = list(script), clock, []

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        self.clock["t"] += 1.0
        item = self.script.pop(0) if self.script else None
        if item is None:
            raise socket.timeout()
        if isinstance(item, BaseException):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_clock(monkeypatch, module):
    """A clock only `module` sees (its own `time` name), moved by recv()."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    return clock


def _telnet(monkeypatch, script=None, error=None):
    clock = _fake_clock(monkeypatch, telnetfeed)

    def connect(address, timeout=None):
        if error is not None:
            raise error
        return _ScriptedSocket(script, clock)

    monkeypatch.setattr(telnetfeed, "socket", SimpleNamespace(create_connection=connect, timeout=socket.timeout))


SPOT = b"DX de W1DDD:     14025.0  K1XYZ        CW op            0123Z\r\n"


@pytest.mark.parametrize("script,error", [
    (None, ConnectionRefusedError("refused")),               # no connection
    (None, socket.timeout("timed out")),                     # connect timeout
    ([b"Please enter your call: ", b""], None),              # hung up after the prompt
    ([b"login: ", ConnectionResetError("reset")], None),     # connection reset, nothing spotted
    ([None] * 20, None),                                     # not one byte in the whole sample
])
def test_telnet_sample_raises_when_the_node_fails(monkeypatch, script, error):
    """G11 regression: a dead cluster or skimmer node gave [] ("no spots")."""
    _telnet(monkeypatch, script, error)
    with pytest.raises(UpstreamError):
        telnetfeed.sample_telnet_feed("node.example", 7300, "W1AW", seconds=10)


def test_telnet_sample_returns_what_the_node_sent(monkeypatch):
    # connected for the whole sample, nothing spotted: the node answered with nothing
    _telnet(monkeypatch, [b"login: ", b"Hello W1AW\r\n"])
    assert telnetfeed.sample_telnet_feed("node.example", 7300, "W1AW", seconds=10) == []
    # spots before a hang-up or a reset are still what the node sent
    _telnet(monkeypatch, [b"login: ", SPOT, b""])
    assert [s["dx_callsign"] for s in telnetfeed.sample_telnet_feed("n", 7300, "W1AW", seconds=10)] == ["K1XYZ"]
    _telnet(monkeypatch, [b"login: ", SPOT, ConnectionResetError("reset")])
    assert len(telnetfeed.sample_telnet_feed("n", 7300, "W1AW", seconds=10)) == 1


@pytest.fixture()
def aprs_sampler(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "aprs_login", "HAMDOOR-T")
    monkeypatch.setattr(aprs_feed, "_login_cache", None)
    monkeypatch.setattr(aprs_feed, "_data_dir", lambda: str(tmp_path))
    clock = _fake_clock(monkeypatch, aprsis)

    def serve(script=None, error=None):
        def connect(host, port, timeout):
            if error is not None:
                raise error
            return _ScriptedSocket(script, clock)
        monkeypatch.setattr(aprs_feed, "_open_connection", connect)

    return serve


@pytest.mark.parametrize("script,error", [
    (None, OSError("no route to host")),
    ([b"# aprsc 2.1.19\r\n# Login by user not allowed\r\n"], None),  # refused login
    ([b"# aprsc 2.1.19\r\n", b""], None),                             # hung up
    ([None] * 30, None),                                                # silent
])
def test_aprs_sample_raises_when_aprs_is_fails(aprs_sampler, script, error):
    """G11 regression: a refused login or a dead server gave [] ("nothing heard")."""
    aprs_sampler(script, error)
    with pytest.raises(UpstreamError):
        aprsis.sample_aprs(40.0, -75.0, 50)


def test_aprs_sample_heard_nothing_is_an_empty_list(aprs_sampler):
    aprs_sampler([b"# aprsc 2.1.19\r\n", b"# logresp HAMDOOR-T unverified, server T2TEST\r\n"])
    assert aprsis.sample_aprs(40.0, -75.0, 50) == []


_ROUTES = [  # path, module the route calls, function, source named in the 502
    ("/api/activity/pskreporter", "pskreporter", "query_pskreporter", "PSK Reporter"),
    ("/api/activity/pskreporter?direction=received", "pskreporter", "query_pskreporter", "PSK Reporter"),
    ("/api/activity/wspr", "wsprlive", "query_wspr", "wspr.live"),
    ("/api/activity/dxcluster", "telnetfeed", "sample_dxcluster", "The DX cluster"),
    ("/api/activity/rbn", "telnetfeed", "sample_rbn", "The Reverse Beacon Network"),
    ("/api/activity/pota", "pota_sota", "query_pota", "pota.app"),
    ("/api/activity/sota", "pota_sota", "query_sota", "SOTAwatch"),
    ("/api/activity/aprs?radius_km=50", "aprsis", "sample_aprs", "APRS-IS"),
]


@pytest.mark.parametrize("path,module,fn,source", _ROUTES)
def test_activity_route_502_is_never_cached(client, token, monkeypatch, path, module, fn, source):
    """G11, multi-call: a failed upstream is a 502 with a readable detail and
    is not cached — the next request asks again; an empty answer is a 200
    with [] and is cached like any answer."""
    monkeypatch.setattr(activity_routes, "activity_cache", common.TTLCache())
    answers = [UpstreamError("down"), UpstreamError("still down"), []]

    def fake(*args, **kwargs):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(getattr(activity_routes, module), fn, fake)
    key = "stations" if "aprs" in path else "spots"
    for _ in range(2):
        r = client.get(path, headers=auth(token))
        assert r.status_code == 502, r.text
        assert r.json() == {"detail": f"{source} did not answer. Try again in a minute."}
    r = client.get(path, headers=auth(token))
    assert r.status_code == 200 and r.json()[key] == []
    assert answers == []
    assert client.get(path, headers=auth(token)).status_code == 200  # cached: not asked again


def test_activity_routes_502_end_to_end(client, token, monkeypatch):
    """G11 through the real fetchers: PSK Reporter answering 503, a DX
    cluster refusing the connection and APRS-IS unreachable were each a 200
    with an empty list before (old code); now 502 — and the next request
    retries and gets PSK Reporter's real (empty) answer."""
    monkeypatch.setattr(activity_routes, "activity_cache", common.TTLCache())
    up = {"ok": False, "calls": 0}

    def handler(request):
        up["calls"] += 1
        if not up["ok"]:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, text=_EMPTY_REPLIES["pskreporter"])

    _http(monkeypatch, handler)
    _telnet(monkeypatch, error=ConnectionRefusedError("refused"))
    monkeypatch.setattr(aprs_feed, "_open_connection",
                        lambda h, p, t: (_ for _ in ()).throw(OSError("unreachable")))
    assert client.get("/api/activity/pskreporter", headers=auth(token)).status_code == 502
    assert client.get("/api/activity/dxcluster", headers=auth(token)).status_code == 502
    assert client.get("/api/activity/aprs?radius_km=50", headers=auth(token)).status_code == 502
    up["ok"] = True
    r = client.get("/api/activity/pskreporter", headers=auth(token))
    assert r.status_code == 200 and r.json()["spots"] == [] and up["calls"] == 2


def test_aprs_live_feed_disconnected_with_nothing_stored_is_a_502(client, token, monkeypatch):
    """G11 in feed mode: no upstream call per request, but a disconnected
    feed with nothing in the window must not read as "nothing heard"; what
    the store holds is still served while the feed reconnects."""
    t = {"now": 1_800_000_000.0}
    monkeypatch.setattr(aprs_feed, "_clock", lambda: t["now"])
    store = aprs_feed.StationStore(retention_s=6 * 3600)
    monkeypatch.setattr(aprs_feed, "_store", store)
    monkeypatch.setattr(aprs_feed, "is_running", lambda: True)
    feed = {"connected": False}
    monkeypatch.setattr(aprs_feed, "is_connected", lambda: feed["connected"])
    r = client.get("/api/activity/aprs?radius_km=50", headers=auth(token))
    assert r.status_code == 502 and r.json()["detail"] == "APRS-IS did not answer. Try again in a minute."
    feed["connected"] = True
    r = client.get("/api/activity/aprs?radius_km=50", headers=auth(token))
    assert r.status_code == 200 and r.json()["stations"] == []           # connected: really nothing heard
    store.ingest(aprs_parser.parse_line(b"N3NEW>APRS:!4000.00N/07500.00W-just heard"), t["now"])
    feed["connected"] = False
    r = client.get("/api/activity/aprs?radius_km=50", headers=auth(token))
    assert r.status_code == 200 and [s["callsign"] for s in r.json()["stations"]] == ["N3NEW"]


def test_is_connected_reads_the_running_feed(monkeypatch):
    assert aprs_feed.is_connected() is False  # the test settings never start the feed

    class _F:
        connected = True

    monkeypatch.setattr(aprs_feed, "_feed", _F())
    assert aprs_feed.is_connected() is True
