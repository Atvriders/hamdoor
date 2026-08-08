"""Activity toolbox: parser unit tests + route tests with mocked collectors.

No network access happens in these tests.
"""

import pytest

from app.integrations import aprsis, bandcond, pskreporter, telnetfeed
from app.routes import activity as activity_routes
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


def test_pskreporter_parse_garbage():
    assert pskreporter.parse_pskreporter_xml("not xml") == []


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


def test_aprs_passcode():
    assert aprsis.aprs_passcode("W1AW") == 25988
    assert aprsis.aprs_passcode("w1aw-9") == 25988  # case-insensitive, SSID stripped


def test_aprs_parse_uncompressed():
    pos = aprsis.parse_aprs_line("W1AW-9>APRS,TCPIP*:!4143.75N/07242.50W-home QTH")
    assert pos["callsign"] == "W1AW-9"
    assert pos["lat"] == pytest.approx(41.7292, abs=0.001)
    assert pos["lon"] == pytest.approx(-72.7083, abs=0.001)
    assert pos["comment"] == "home QTH"


def test_aprs_parse_timestamped_and_object():
    pos = aprsis.parse_aprs_line("K1ABC>APRS,TCPIP*:@121200z4143.75N/07242.50W>mobile")
    assert pos["lat"] == pytest.approx(41.7292, abs=0.001)
    obj = aprsis.parse_aprs_line("W1ABC>APRS,TCPIP*:;REPEATER *121200z4143.75N/07242.50Wr146.94MHz T123")
    assert obj["object"] == "REPEATER"
    assert "146.94" in obj["comment"]


def test_aprs_parse_compressed_and_rejects():
    pos = aprsis.parse_aprs_line("W1XYZ>APRS,TCPIP*:!/9EU(<+f&- compressed")
    assert pos is not None and -91 < pos["lat"] < 91
    assert aprsis.parse_aprs_line("# server comment") is None
    assert aprsis.parse_aprs_line("W1AW>APRS,TCPIP*:>status text only") is None
    assert aprsis.parse_aprs_line("") is None


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


def test_aprs_route(client, token, monkeypatch):
    sample = [{"callsign": "W1BBB-9", "object": "", "lat": 40.1, "lon": -75.1, "comment": "mobile"}]
    monkeypatch.setattr(activity_routes.aprsis, "sample_aprs", lambda cs, lat, lon, km: sample)
    r = client.get("/api/activity/aprs?radius_km=50", headers=auth(token))
    assert r.status_code == 200
    st = r.json()["stations"][0]
    assert st["callsign"] == "W1BBB-9"
    assert 5 < st["distance_miles"] < 10


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
