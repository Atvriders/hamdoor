"""Repeater directory: parsers, normalizers, location quality, merge/dedupe,
the refresh state machine (HTTP served by httpx.MockTransport, clock frozen)
and the /api/repeaters routes. No network access."""

import bz2
import copy
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.geo import cell_degrees
from app.integrations import repeaters as rp
from app.models import User
from tests.conftest import auth, signup

FIX = Path(__file__).parent / "fixtures" / "repeaters"
NOW = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)
_REAL_JITTER = rp._jitter_s  # the env fixture stubs it; the jitter test puts it back


def _json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _places() -> rp.PlaceIndex:
    return rp.PlaceIndex.from_tsv((FIX / "places.tsv").read_bytes())


def _places_zip() -> bytes:
    """The fixture place list as a GeoNames cities500-style archive."""
    rows = []
    for i, line in enumerate((FIX / "places.tsv").read_text(encoding="utf-8").splitlines()):
        if line.startswith("#"):
            continue
        la, lo, cc, a1, name = line.split("\t")
        rows.append("\t".join([str(i), name, name, "", la, lo, "P", "PPL", cc, "", a1, "", "", "",
                               "1000", "", "0", "UTC", "2026-01-01"]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cities500.txt", "\n".join(rows) + "\n")
    return buf.getvalue()


def _irlp_bz2(textual: str | None = None) -> bytes:
    raw = textual if textual is not None else (FIX / "irlp.txt").read_text(encoding="utf-8")
    return bz2.compress(raw.encode("latin-1"))


def _irlp_row(node, city="Testville", status="IDLE", lastupdate="1792181866", freq="146.9400",
              offset="-600.0000", lat="39.11111", lon="-94.61111") -> str:
    """One line of IRLP's nohtmlstatus file (the fixture's column layout)."""
    return "\t".join([str(node), "K0TST", city, "MO", "USA", status, str(node), "2010-01-01", lat, lon,
                      lastupdate, freq, offset, "100.0", "K0TST", "", "1792016113", "U"])


def _by_id(records):
    return {r["id"]: r for r in records}


def _rows() -> dict[str, dict]:
    with engine.begin() as conn:
        res = conn.execute(text("SELECT * FROM repeaters"))
        return {r["id"]: dict(r) for r in res.mappings()}


def _state(source):
    return rp._load_states().get(source)


def _reset_tables():
    rp.ensure_tables()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM repeater_sources"))
        conn.execute(text("DELETE FROM repeaters"))


# ============================================================== pure parsers

def test_band_edges_us_and_elsewhere():
    assert rp.band_of(146.94) == "2m"
    assert rp.band_of(29.62) == "10m" and rp.band_of(53.33) == "6m"
    assert rp.band_of(443.25) == "70cm" and rp.band_of(927.0) == "33cm" and rp.band_of(1282.0) == "23cm"
    # 1.25 m repeaters only from 222.15 MHz (97.205(b)); 219-222 is not a US repeater band
    assert rp.band_of(223.5) == "1.25m"
    assert rp.band_of(222.10) == "other" and rp.band_of(220.5) == "other"
    assert rp.band_of(220.5, us=False) == "1.25m"
    # 9 cm is gone from US amateur use; still a band elsewhere
    assert rp.band_of(3400.0) == "other"
    assert rp.band_of(3400.0, us=False) == "9cm"
    # US 13 cm is two pieces
    assert rp.band_of(2304.0) == "13cm" and rp.band_of(2350.0) == "other"
    assert rp.band_of(2350.0, us=False) == "13cm"
    assert rp.band_of(70.2, us=False) == "4m" and rp.band_of(70.2) == "other"
    assert rp.band_of(5760.0) == "6cm" and rp.band_of(10368.0) == "3cm"
    assert rp.band_of(None) == "other" and rp.band_of(462.65) == "other"


@pytest.mark.parametrize("raw, modes", [
    ("FM", ["FM"]), ("", ["FM"]), ("NFM", ["FM"]), ("D-star", ["DSTAR"]), ("DSTAR", ["DSTAR"]),
    ("YSF/FM ", ["FM", "YSF"]), ("DMR    ", ["DMR"]), ("P25/NFM", ["FM", "P25"]),
    ("P25YSFD-STARNXDNDMR/FM ", ["FM", "DMR", "DSTAR", "YSF", "P25", "NXDN"]),
    ("M17/FM ", ["FM", "M17"]), ("TV", ["ATV"]), ("ATV", ["ATV"]), ("AX25", ["OTHER"]),
    ("FM+YSF", ["FM", "YSF"]), ("TETRA", ["TETRA"]), ("weird", ["OTHER"]),
])
def test_parse_modes(raw, modes):
    assert rp.parse_modes(raw) == modes


@pytest.mark.parametrize("raw, expected", [
    ("151.4*", {"ctcss": 151.4}), ("0", {}), ("0.00", {}), ("", {}), ("B", {}), ("COS", {}),
    ("1\xa0750.0", {"burst": 1750.0}), ("CC7/97.4", {"cc": 7, "ctcss": 97.4}),
    ("DCS 043", {"dcs": "043N"}), ("D031*", {"dcs": "031N"}), ("dpl 411", {"dcs": "411N"}),
    ("DCS51", {"dcs": "051N"}), ("DCS411I", {"dcs": "411I"}), ("RAN1", {"ran": 1}),
    ("NAC293/100.0", {"nac": "293", "ctcss": 100.0}),
    ("123.0/RAN1/NAC293/C/CAN0", {"ctcss": 123.0, "ran": 1, "nac": "293", "can": 0}),
    ("CC293", {}), ("88.5/71.9", {"ctcss": 88.5}), ("107.2 1B", {"ctcss": 107.2}),
])
def test_parse_tone_field(raw, expected):
    assert rp.parse_tone_field(raw) == expected


def test_clean_output_and_offset_repairs():
    assert rp.clean_output(446525.0) == 446.525  # typed in kHz
    assert rp.clean_output(147500.0) == 147.5
    assert rp.clean_output(0.5) is None
    assert rp.clean_offset(146.94, -0.6) == -0.6
    assert rp.clean_offset(146.94, 0) == 0.0
    assert rp.clean_offset(438.5, -293.0) == -293.0  # cross-band input 145.5 is fine
    assert rp.clean_offset(147.33, 600.0) == 0.6  # Hz/kHz slip
    assert rp.clean_offset(147.24, 60.0) == 0.6
    assert rp.clean_offset(146.66, -600.0) == -0.6
    assert rp.clean_offset(442.7, 500.0) == 5.0
    assert rp.clean_offset(446.86, 5.0) == -5.0  # wrong sign: +5 leaves the band
    assert rp.clean_offset(224.48, 1.6) == -1.6
    assert rp.clean_offset(927.025, 25.0) == -25.0
    assert rp.clean_offset(444.0, 449.0) == 5.0  # the input typed into the offset field
    assert rp.clean_offset(145.27, -6.0) == -0.6
    assert rp.clean_offset(145.625, 7.6) is None  # nothing sensible
    assert rp.clean_offset(None, 0.6) is None and rp.clean_offset(146.94, None) is None


def test_tiny_splits_are_unit_slips_even_inside_the_band():
    """B2-6: no band uses a split under 100 kHz. HearHam '-5000' (Hz) meant
    -5 MHz and IRLP's '5.0000' (kHz) meant 5 MHz, although 447.745 is still in
    the band. Re-scaled onto the band's usual split, or dropped (kind unknown)."""
    assert rp.clean_offset(447.75, -0.005) == -5.0  # live hearham:204
    assert rp.clean_offset(444.3, 0.005) == 5.0
    assert rp.clean_offset(447.72, 0.005) == -5.0  # +5 leaves the band: the sign slipped too
    assert rp.clean_offset(147.885, -0.06) == -0.6
    assert rp.clean_offset(29.62, -0.01) == -0.1 and rp.clean_offset(29.62, 0.01) == -0.1  # 10 m: 100 kHz down
    assert rp.clean_offset(145.11, 0.0027) is None  # 2.7 MHz is no 2 m split
    assert rp.clean_offset(433.5, -0.01) is None  # -10 MHz would land on 423.5, not R1 70 cm
    assert rp.clean_offset(433.075, 0.001) is None
    assert rp.clean_offset(462.65, 0.005) is None  # GMRS: no amateur band to repair onto
    hh = _by_id(rp.normalize_hearham([_hh(1, 38.45505, -105.20972, frequency=447750000, offset=-5000),
                                      _hh(2, 61.11111, -149.11111, frequency=145110000, offset=2700)], "t"))
    one, two = hh["hearham:1"], hh["hearham:2"]
    assert (one["offset_mhz"], one["input_mhz"], one["kind"]) == (-5.0, 442.75, "repeater")
    assert (two["offset_mhz"], two["input_mhz"], two["kind"]) == (None, None, "unknown")
    header = (FIX / "irlp.txt").read_text(encoding="utf-8").splitlines()[0]
    (r,) = rp.normalize_irlp(f"{header}\n{_irlp_row(2860, freq='444.3000', offset='5.0000')}\n")
    assert (r["offset_mhz"], r["input_mhz"]) == (5.0, 449.3)  # the same slip in IRLP's kHz column


@pytest.mark.parametrize("raw, city, state, country", [
    ("Central Point, OR", "Central Point", "OR", "US"),
    ("LAKE CITY, Florida", "Lake City", "FL", "US"),
    ("ARRL HEADQUARTERS, CT", "ARRL Headquarters", "CT", "US"),  # QA F7: was "Arrl Headquarters"
    ("Arrl Headquarters", "ARRL Headquarters", None, None),        # ... and HearHam sends it like this
    ("Ares, Spain", "Ares", None, "ES"),                            # a town, not the ARES acronym
    ("Chula Vista, CA 91915", "Chula Vista", "CA", "US"),
    ("Moscow, ID USA", "Moscow", "ID", "US"),
    ("Vancouver, BC Canada", "Vancouver", "BC", "CA"),
    ("Courtland, Canada, Ontario", "Courtland", "ON", "CA"),
    ("Wincanton (UK)", "Wincanton", None, "GB"),
    ("Washington (UK)", "Washington", None, "GB"),
    ("Penteli, Greece, Pennsylvania", "Penteli", None, "GR"),
    ("Washington, United Kingdom, England", "Washington", None, "GB"),
    ("Darwin, NT Australia", "Darwin", "NT", "AU"),
    ("Capilla Peak (Mountainair, NM)", "Mountainair", "NM", "US"),
    ("Sullivan MO, EM48k", "Sullivan", "MO", "US"),
    ("Madison WI EN53", "Madison", "WI", "US"),
    ("Denver CO USA", "Denver", "CO", "US"),
    ("Lubbock Texas", "Lubbock", "TX", "US"),
    ("West Virginia", "West Virginia", None, None),
    ("Edenton/NC", "Edenton", "NC", "US"),
    ("Oesterreich/Austria", "Oesterreich", None, "AT"),
    ("Emeril Mountain, Labrador, CA", "Emeril Mountain", "NL", "CA"),
    ("Place Holder, California", "", "CA", "US"),
    ("Volos, KM19MJ", "Volos", None, None),
    ("St. Kitts", "", None, "KN"),
    ("Sant Jeroni, Montserrat", "Sant Jeroni", None, None),
    ("Statewide WICEN", "Statewide WICEN", None, None),
    ("", "", None, None),
])
def test_parse_place(raw, city, state, country):
    p = rp.parse_place(raw)
    assert (p["city"], p["state"], p["country"]) == (city, state, country)


def test_parse_place_context_keeps_foreign_codes_foreign():
    # BrandMeister licence country known: Italian 'CA' (Cagliari) is not California
    assert rp.parse_place("VILLAPUTZU  CA, JM49TM", "IT")["state"] is None
    assert rp.parse_place("IPSC Gallarate VA", "IT")["country"] is None
    assert rp.parse_place("Perosa Argentina ,JN", "IT")["country"] is None
    assert rp.parse_place("San Diego, CA", "US")["state"] == "CA"
    # an abbreviation alone is remembered so the coordinates can overrule it
    assert rp.parse_place("Obdam, NL")["code"] == "NL"
    assert rp.parse_place("Obdam, NL", "NL")["code"] is None


def test_scrub_private_and_notes():
    raw = ("Contact rptr.owner@example.com<br>Site: 4000 W Testwood Rd repeater &amp; tower."
           "<br/>Mail: P.O. Box 123<BR>Altitude 4985 ft. 84 in CT. <b>Bold</b> &#22269;")
    notes = rp.clean_notes(raw)
    assert "@" not in notes and "Testwood" not in notes and "Box 123" not in notes
    assert "[email removed]" in notes and notes.count("[address removed]") == 2
    assert "\n" in notes and "<br" not in notes.lower() and "<b>" not in notes
    assert "& tower" in notes and "国" in notes  # entities decoded
    assert "Altitude 4985 ft. 84 in CT." in notes  # numbers that aren't addresses survive
    long = rp.clean_notes("x" * 900)
    assert len(long) == 500 and long.endswith("…")
    # location text: the address goes, the place stays
    assert rp.parse_place("177 Testwood Road Helensville, New Zealand")["city"] == "Helensville"
    assert rp.parse_place("12 Testwood Lane, New Zealand") == {
        "city": "", "state": None, "country": "NZ", "state_name": False, "code": None}


@pytest.mark.parametrize("raw, city, country", [
    # B2-1: the shapes of the live leaks (lower case, ALL CAPS, 'Av', 'State
    # Highway', street type first, number after the street)
    ("125 n testwood st allentown ", "Allentown", None),  # hearham:13436 was "125 N Jefferson St Allentown"
    ("239 TESTWOOD AV", "", None),  # hearham:25099, a simplex node
    ("1133 State Highway 1 Testwood, New Zealand", "Testwood", "NZ"),
    ("178 rue du TESTWOOD RA, France, Pays de la Loire", "", "FR"),  # never "France" as the city
    ("2205 route de TESTWOOD, France", "", "FR"),
    ("48 Place des Testwood, France", "", "FR"),
    ("Via G.Testwood 103, Italy, Calabria", "", "IT"),
    ("Testwoodstraat 39, Belgium", "", "BE"),
    ("Teststr. 3A, Germany", "", "DE"),
    ("Testwoodtie 6 Jaervenpae, Finland", "Jaervenpae", "FI"),
    ("Testby Landevej 83, Denmark", "Testby", "DK"),
    ("Testvillage 328 Sela, Croatia", "Testvillage Sela", "HR"),  # no street word: the number goes
    ("40 J Testwood Road, Leigh., New Zealand", "Leigh", "NZ"),  # the next part is the city
    ("Testwood Hill Rd Rapid 101, New Zealand", "Testwood Hill Rd", "NZ"),  # NZ rural number
    ("12 avenue testwood", "", None),  # only matchable once title-cased: scrubbed after that too
])
def test_addresses_never_survive_in_place_text(raw, city, country):
    p = rp.parse_place(raw)
    assert (p["city"], p["country"]) == (city, country)


@pytest.mark.parametrize("raw, city", [
    ("100 Mile House, BC", "100 Mile House"), ("29 Palms, CA", "29 Palms"),
    ("Willow MP 70 Parks Hwy., AK USA", "Willow MP 70 Parks Hwy"),  # a milepost, not a house
    ("Testville - EchoIRLP #6895, KS USA", "Testville - EchoIRLP #6895"),
    ("Monte Testa 2183 m, Italy", "Monte Testa 2183 m"), ("Sydney 6/15", "Sydney 6/15"),
    ("St. Louis, MO", "St. Louis"), ('37°12\'33"N 38°48\'28"E', '37°12\'33"N 38°48\'28"E'),
    ("Testtown (2), AK", "Testtown"), ("Wien 22, Austria", "Wien"),
    ("4130 Testheim, Norway", "Testheim"),  # a district number or postcode is no part of a city
])
def test_place_text_keeps_what_is_not_an_address(raw, city):
    assert rp.parse_place(raw)["city"] == city


def test_notes_lose_addresses_in_any_case_but_keep_radio_text():
    for raw in ("Site: 125 n testwood st, allentown", "Club at 239 TESTWOOD AV.",
                "QTH 2205 route de Testwood", "Located at Testwoodstraat 39 near the church",
                "Shack: Via G.Testwood 103"):
        notes = rp.clean_notes(raw)
        assert "[address removed]" in notes and not re.search(r"[0-9]", notes), notes
    for raw in ("40 watt repeater at St. Mary's hospital", "Use 100.0 Hz PL encode and decode",
                "IRLP Node 7067 via LAX Reflector 9358", "Echolink 323942 via DL3GD-R",
                "a 2 lane highway", "Yaesu DR-2X, 2 Yaesu DR-1X units", "Tone 100 PL",
                "Linked via Brandmeister 3100", "Route 66 and I-55", "Willow MP 70 Parks Hwy"):
        assert rp.clean_notes(raw) == raw  # 'Use 100.[address removed] encode' before


@pytest.mark.parametrize("raw, city, state, country", [
    # B2R-1: the reviewer's forms as HearHam sends them (no country part), and
    # the five shapes found by hand (synthetic names, same shape)
    ("239 TESTWOOD AV, NJ", "", "NJ", "US"),
    ("Testwoodstraat 39", "", None, None), ("Via G.Testwood 103", "", None, None),
    ("Teststr. 3A", "", None, None), ("178 rue du TESTWOOD RA", "", None, None),
    ("Str. Testwood nr. 38, Romania", "", None, "RO"),  # was 'Str. Testwood nr'
    ("str. testwood nr.38", "", None, None),
    ("Testville Testowcow 26, Poland, Malopolskie", "Testville Testowcow", None, "PL"),
    ("Testcsaba Testkin 1., Hungary", "Testcsaba Testkin", None, "HU"),
    ("Testseljanska 9 Bj, Croatia", "Testseljanska Bj", None, "HR"),
    ("Testgreda 328 Sela, Croatia", "Testgreda Sela", None, "HR"),
    # a full postcode places a home as well as a house number does
    ("Testineau (Québec) J8Y 1W7", "Testineau (Québec)", None, None),
    ("Testineau, QC J8Y1W7", "Testineau", "QC", "CA"),
    ("Brendon Test Gates - Ta24 7lb (UK)", "Brendon Test Gates", None, "GB"),
])
def test_more_address_shapes_never_survive(raw, city, state, country):
    p = rp.parse_place(raw)
    assert (p["city"], p["state"], p["country"]) == (city, state, country)


def test_street_type_first_with_a_number_marker_is_scrubbed_from_notes():
    """B2R-1: 'Str. X nr. 38' (Romania, Moldova) passed through notes whole."""
    assert rp.clean_notes("QTH: Str. Testwood nr. 38, Iasi") == "QTH: [address removed], Iasi"
    assert rp.clean_notes("Shack at Testkin utca 12 in Testcsaba") == (
        "Shack at Testkin [address removed] in Testcsaba")
    for raw in ("Site near Testtown, nr. the water tower", "Located at Str. Testwood (no number)"):
        assert rp.clean_notes(raw) == raw


def test_dmr_network_text_never_carries_an_email():
    """B2R-1: the raw 'Network:' text is returned as dmr_network; it went out
    unscrubbed (an email there reached the API)."""
    assert rp._dmr_network("Network: ask k0tst@example.com") == "ask"
    assert rp._dmr_network("Network: k0tst@example.com") is None
    assert rp._dmr_network("Network: CT-ARES") == "CT-ARES"


def test_clean_callsign():
    assert rp._clean_callsign("W9KLS/R Testing") == "W9KLS"
    assert rp._clean_callsign(" wr7hln mprg cp ") == "WR7HLN"
    assert rp._clean_callsign("K9IIK-R") == "K9IIK"
    assert rp._clean_callsign("KB1YAC/ AC1M") == "KB1YAC"
    assert rp._clean_callsign("ZL2WA-3") == "ZL2WA-3"
    assert rp._clean_callsign(None) == ""


# ============================================================== normalizers

def test_normalize_hearham_matches_research_mapping():
    """Research §7.1 example (HearHam 1259), field for field."""
    raw = {"id": 1259, "callsign": "AK7SB", "latitude": 42.35694, "longitude": -122.97871,
           "city": "Central Point, OR", "group": "", "internet_node": None, "mode": "FM",
           "encode": "136.5", "decode": "", "frequency": 147100000, "offset": 600000,
           "description": "John's peak repeater.", "power": "grid", "operational": 0, "restriction": ""}
    (r,) = rp.normalize_hearham([raw], "2026-10-16T21:00:00Z")
    assert r["id"] == "hearham:1259" and r["source_url"] == "https://hearham.com/repeaters/1259"
    assert (r["callsign"], r["name"]) == ("AK7SB", "AK7SB 147.1000")
    assert (r["output_mhz"], r["input_mhz"], r["offset_mhz"]) == (147.1, 147.7, 0.6)
    assert r["modes"] == ["FM"] and r["tone_up"] == 136.5 and r["tone_down"] is None
    assert (r["city"], r["state"], r["country"]) == ("Central Point", "OR", "US")
    assert (r["status"], r["use"], r["kind"]) == ("off-air", "open", "repeater")
    assert r["fetched_at"] == "2026-10-16T21:00:00Z"


def test_normalize_hearham_fixture_fields():
    recs = _by_id(rp.normalize_hearham(_json("hearham.json"), "t"))
    assert len(recs) == 33
    dmr = recs["hearham:1005"]
    assert dmr["dmr_id"] == 310012 and dmr["dmr_color_code"] == 1 and dmr["modes"] == ["DMR"]
    assert dmr["dmr_network"] == "BrandMeister"  # "Network: Brandmiester"
    assert recs["hearham:1004"]["dmr_id"] == 235123 and recs["hearham:1004"]["_hint_approx"]
    assert recs["hearham:1007"]["irlp"] == 1528 and recs["hearham:1008"]["allstar"] == 2024
    yfm = recs["hearham:1016"]
    assert yfm["modes"] == ["FM", "YSF"] and (yfm["offset_mhz"], yfm["input_mhz"]) == (0.6, 147.93)
    multi = recs["hearham:1017"]
    assert multi["offset_mhz"] == -5.0 and multi["input_mhz"] == 441.86
    assert (multi["tone_up"], multi["nxdn_ran"], multi["p25_nac"], multi["m17_can"], multi["dcs"]) == \
        (123.0, 1, "293", 0, "043N")
    assert multi["use"] == "closed"
    priv = recs["hearham:1019"]
    assert priv["use"] == "private" and (priv["echolink"], priv["wiresx"]) == (123456, 62148)
    assert "@" not in priv["notes"] and "Testwood" not in priv["notes"]
    assert recs["hearham:1020"]["city"] == "" and recs["hearham:1020"]["country"] == "NZ"
    assert recs["hearham:1015"]["kind"] == "simplex_link"
    assert recs["hearham:1033"]["modes"] == ["FM"]  # empty mode means plain FM
    assert recs["hearham:1032"]["input_mhz"] == 144.99  # ISS cross-band input kept


def test_normalize_brandmeister():
    recs = _by_id(rp.normalize_brandmeister(_json("brandmeister.json"), "t"))
    assert "brandmeister:3101234" not in recs  # 7-digit IDs are hotspots: people's homes
    assert len(recs) == 8
    r = recs["brandmeister:310012"]
    assert (r["callsign"], r["name"]) == ("W5DMR", "W5DMR MPRG CP")
    assert (r["output_mhz"], r["input_mhz"], r["offset_mhz"]) == (443.25, 448.25, 5.0)
    assert (r["dmr_id"], r["dmr_color_code"], r["dmr_network"], r["modes"]) == (
        310012, 1, "BrandMeister", ["DMR"])
    assert (r["city"], r["state"], r["country"]) == ("Mountainair", "NM", "US")
    assert r["source_url"] == "https://brandmeister.network/#/device/310012"
    assert r["last_update"] == r["_bm_seen"] == "2026-10-15T21:00:00Z"
    assert "Capilla Peak" in r["notes"]
    assert recs["brandmeister:310013"]["name"] == "K6RDL 445.3000"
    assert recs["brandmeister:310500"]["state"] == "MO"
    tw = recs["brandmeister:313365"]
    assert tw["_claim_cc"] == "US" and tw["_soft_claim"]  # licence country, not location


def test_normalize_irlp():
    recs = _by_id(rp.normalize_irlp(rp._irlp_text(_irlp_bz2()), "t"))
    assert sorted(recs) == ["irlp:1528", "irlp:1811", "irlp:3004", "irlp:3020", "irlp:3079",
                            "irlp:4444", "irlp:9999"]  # duplicate 3020 row collapsed
    a = recs["irlp:1528"]
    assert (a["output_mhz"], a["offset_mhz"], a["input_mhz"], a["tone_up"]) == (448.7, -5.0, 443.7, 123.0)
    assert (a["status"], a["irlp"], a["state"], a["country"]) == ("on-air", 1528, "WA", "US")
    assert a["last_update"] == "2026-10-16T20:17:46Z"
    assert a["source_url"] == "https://www.irlp.net/status/index.php?nodeid=1528"
    assert recs["irlp:1811"]["tone_up"] is None  # PL 0.00
    linked = recs["irlp:3004"]
    assert linked["status"] == "on-air" and linked["kind"] == "simplex_link"
    assert "connected to 9200" in linked["notes"]
    assert recs["irlp:3079"]["status"] == "off-air" and "@" not in recs["irlp:3079"]["notes"]
    assert recs["irlp:4444"]["output_mhz"] is None and recs["irlp:4444"]["kind"] == "unknown"
    mtl = recs["irlp:9999"]
    assert (mtl["city"], mtl["state"], mtl["country"]) == ("Montréal", "QC", "CA")  # latin-1
    with pytest.raises(rp.FetchError):
        rp.normalize_irlp("<html>not the status file</html>")


def test_malformed_values_are_dropped_not_fatal():
    """B2-2: '31001²' passes str.isdigit() and then int() raised for the whole
    source; an unbounded ID was stored and then failed every swap."""
    raw = [_hh(1, 39.1, -94.6, group="DMR", mode="DMR", internet_node="31012345678901234567890"),
           _hh(2, 39.2, -94.6, group="Allstar", internet_node="31001²"),
           _hh(3, 39.3, -94.6, group="IRLP", internet_node="1528"),
           _hh(4, 39.4, -94.6, description="Club rptr \ud83d", city="Test\ud83d City, MO"),
           {"id": "x5", "callsign": "K0BAD"}, {"id": True}, "not a record"]
    recs = _by_id(rp.normalize_hearham(raw, "t"))
    assert sorted(recs) == ["hearham:1", "hearham:2", "hearham:3", "hearham:4"]
    assert recs["hearham:1"]["dmr_id"] is None and recs["hearham:2"]["allstar"] is None
    assert recs["hearham:3"]["irlp"] == 1528
    assert recs["hearham:4"]["notes"] == "Club rptr \ufffd" and recs["hearham:4"]["city"] == "Test\ufffd City"
    header = (FIX / "irlp.txt").read_text(encoding="utf-8").splitlines()[0]
    lines = [header, _irlp_row("1²"), _irlp_row("31012345678901234567890"),
             _irlp_row(7001, lastupdate="99999999999999"),  # datetime: year out of range
             _irlp_row(7002, city='"Testville'),  # csv would run this field on into the next rows
             _irlp_row(7003, city="Test\x00ville"), _irlp_row(7004, status="9²")]
    irlp = _by_id(rp.normalize_irlp("\n".join(lines) + "\n"))
    assert sorted(irlp) == ["irlp:7001", "irlp:7002", "irlp:7003", "irlp:7004"]
    assert irlp["irlp:7001"]["last_update"] is None and irlp["irlp:7002"]["city"] == "Testville"
    assert irlp["irlp:7004"]["status"] == "unknown"


def test_duplicate_upstream_ids_keep_one_copy():
    """B2R-3: a source listing one ID twice gave two rows with one primary key,
    and every table swap after that failed. One copy is kept: the one with
    coordinates, then the fuller one, then the later one."""
    hh = rp.normalize_hearham([
        _hh(7, None, None, description="no coordinates"), _hh(8, 39.2, -94.6, description="older"),
        _hh(7, 39.31234, -94.71234, description="with coordinates"),
        _hh(8, 39.2, -94.6, description="newer"), _hh(9, 39.4, -94.6)], "t")
    assert [r["id"] for r in hh] == ["hearham:7", "hearham:8", "hearham:9"]
    by = _by_id(hh)
    assert (by["hearham:7"]["lat"], by["hearham:7"]["notes"]) == (39.31234, "with coordinates")
    assert by["hearham:8"]["notes"] == "newer"
    bm = _json("brandmeister.json")
    recs = rp.normalize_brandmeister([dict(bm[0], lat=None, lng=None)] + bm, "t")
    assert len(recs) == len({r["id"] for r in recs}) == 8
    assert _by_id(recs)["brandmeister:310012"]["lat"] == 34.69712
    header = (FIX / "irlp.txt").read_text(encoding="utf-8").splitlines()[0]
    irlp = rp.normalize_irlp("\n".join([header, _irlp_row(7001), _irlp_row(7001, lat="", lon="")]) + "\n")
    assert [(r["id"], r["lat"]) for r in irlp] == [("irlp:7001", 39.11111)]
    # a set stored by older code may still hold both copies: the build keeps one
    (rec,) = rp.normalize_hearham([_hh(7, 39.31234, -94.71234)], "t")
    out, _ = rp.build({"hearham": [copy.deepcopy(rec), dict(copy.deepcopy(rec), lat=None, lon=None)]},
                      None, NOW)
    assert [(r["id"], r["lat"]) for r in out] == [("hearham:7", 39.31234)]


def test_irlp_link_keeps_the_node_number_as_listed():
    header = (FIX / "irlp.txt").read_text(encoding="utf-8").splitlines()[0]
    (r,) = rp.normalize_irlp(f"{header}\n{_irlp_row(7001, status='0097')}\n")
    assert r["status"] == "on-air" and "connected to 0097" in r["notes"]  # not 'connected to 97'


def test_one_record_that_breaks_the_normalizer_is_skipped(monkeypatch, caplog):
    real = rp.parse_modes
    monkeypatch.setattr(rp, "parse_modes", lambda raw: 1 / 0 if raw == "BOOM" else real(raw))
    recs = rp.normalize_hearham([_hh(1, 39.1, -94.6), _hh(2, 39.2, -94.6, mode="BOOM"),
                                 _hh(3, 39.3, -94.6)], "t")
    assert [r["id"] for r in recs] == ["hearham:1", "hearham:3"]  # not a failed HearHam refresh
    assert "HearHam: skipped 1 malformed records (first: ZeroDivisionError" in caplog.text


# ============================================================== location quality

def test_place_index_matches_brute_force():
    """The ring search (with its early exit) must find exactly what a full scan
    finds: mid-latitudes, near the poles and across the antimeridian."""
    import random

    rnd = random.Random(7)
    rows = []
    for _ in range(3000):
        lat = rnd.choice([rnd.uniform(-60, 75), rnd.uniform(60, 85), rnd.uniform(-5, 5)])
        lon = rnd.choice([rnd.uniform(-180, 180), rnd.uniform(175, 180), rnd.uniform(-180, -175)])
        rows.append((round(lat, 5), round(lon, 5), rnd.choice(["US", "CA", "MX"]), rnd.choice(["A", "B"])))
    idx = rp.PlaceIndex(rows)
    assert idx.size == 3000
    for _ in range(400):
        la = rnd.choice([rnd.uniform(-60, 75), rnd.uniform(60, 85), rnd.uniform(-5, 5)])
        lo = rnd.choice([rnd.uniform(-180, 180), rnd.uniform(179, 180), rnd.uniform(-180, -179)])
        km = rnd.choice([1, 30, 120, 250])
        pred = rnd.choice([None, lambda p: p[2] == "US", lambda p: p[3] == "B"])
        cands = [(rp._dist_km(la, lo, p[0], p[1]), p) for p in rows if pred is None or pred(p)]
        cands = [c for c in cands if c[0] <= km]
        want = min(cands, key=lambda c: c[0]) if cands else None
        got = idx.nearest(la, lo, km, pred)
        if want is None:
            assert got is None
        else:
            assert got is not None and got[0] == pytest.approx(want[0], abs=1e-6)
        assert idx.near(la, lo, 3000) == any(rp._dist_km(la, lo, p[0], p[1]) <= 3 for p in rows)


def _hh(rid, lat, lon, city="Kansas City, MO", call=None, **kw):
    x = {"id": rid, "callsign": call or f"K0{rid % 1000:03d}", "latitude": lat, "longitude": lon,
         "city": city, "group": "", "internet_node": None, "mode": "FM", "encode": "", "decode": "",
         "frequency": 146940000 + rid, "offset": -600000, "description": "", "operational": 1,
         "restriction": ""}
    x.update(kw)
    return x


def test_placeholders_and_bogus_defaults_are_never_plotted():
    recs = rp.normalize_hearham([
        _hh(1, 0, 0), _hh(2, 50, -3), _hh(3, None, None),
        _hh(4, 33.73544, -118.1091, "Rochester, NY"),  # HearHam's AllStar default
        _hh(5, 41.73329, -50.39989, "Manila, Philippines"),  # mid-Atlantic DMR default
        _hh(6, 36.77826, -119.41793, "Buzzard Peak, California"),  # centre of California
        _hh(7, 39.8355, -99.09083, "Corona, California"),  # centre of the US
    ], "t")
    stats = rp.classify_locations(recs, None)
    for r in recs:
        assert (r["lat"], r["lon"], r["location_quality"], r["precise"]) == (None, None, "unknown", False)
    assert stats["no_location_missing"] == 3 and stats["no_location_bogus_default"] == 4


def test_shared_point_with_many_place_names_is_bogus_but_a_busy_site_is_not():
    bogus = [_hh(100 + i, 40.12345, -100.54321, f"Town{chr(65 + i)}son, KS") for i in range(10)]
    # one mountain-top with ten repeaters whose listings spell the town ten ways
    site = [_hh(200 + i, -33.36008, 151.29121, city, call="VK2RAG")
            for i, city in enumerate(["Gosford", "Gosford (G)", "Gosford - Wyong", "Gosford-Wyong",
                                      "Gosford 6/15", "Gosford WICEN", "GOSFORD", "gosford",
                                      "Gosford-Wyong IRLP", "Gosford - Wyong 7/18"])]
    recs = rp.normalize_hearham(bogus + site, "t")
    rp.classify_locations(recs, None)
    assert all(r["lat"] is None for r in recs[:10])
    assert all(r["lat"] is not None for r in recs[10:])


def test_quality_classes():
    places = _places()
    recs = _by_id(rp.normalize_hearham([
        _hh(1, 39.15123, -94.50101),  # nowhere special -> site
        _hh(2, 39.0998, -94.5785),  # 10 m from the Kansas City place point -> city
        _hh(3, 39.1, -94.6),  # 1-2 decimals -> approximate
        _hh(4, 39.15555, -94.40001, description="Location is approximate."),
        _hh(5, 39.34567, -94.23456, call="W0AAA"), _hh(6, 39.34567, -94.23456, call="W0BBB"),
        _hh(7, 39.34567, -94.23456, call="W0CCC"),  # one point, three callsigns -> city
        _hh(8, -49.2713, 13.9524, "Space"),  # > 200 km from any place -> unknown
        _hh(9, -64.775, -64.051111, "Palmer Station"),  # Antarctica has no "places"
    ], "t"))
    stats = rp.classify_locations(list(recs.values()), places)
    q = {k: (r["location_quality"], r["precise"]) for k, r in recs.items()}
    assert q["hearham:1"] == ("site", True)
    assert q["hearham:2"] == ("city", False)
    assert q["hearham:3"] == ("approximate", False) and q["hearham:4"] == ("approximate", False)
    assert q["hearham:5"] == q["hearham:6"] == q["hearham:7"] == ("city", False)
    assert q["hearham:8"] == ("unknown", False) and recs["hearham:8"]["lat"] is None
    assert q["hearham:9"] == ("site", True)
    assert stats["no_location_far_from_places"] == 1


def test_without_places_only_text_rules_apply():
    recs = _by_id(rp.normalize_hearham([_hh(2, 39.0998, -94.5785), _hh(8, -49.2713, 13.9524),
                                        _hh(11, 42.57123, 87.82456, "Kenosha, Wisconsin")], "t"))
    rp.classify_locations(list(recs.values()), None)
    assert recs["hearham:2"]["location_quality"] == "site"  # no centroid list to compare with
    assert recs["hearham:8"]["lat"] == -49.2713  # no mid-ocean test
    assert recs["hearham:11"]["lon"] == 87.82456  # no repair either


def test_sign_and_swap_slips_are_repaired_when_the_listing_agrees():
    places = _places()
    recs = _by_id(rp.normalize_hearham([
        _hh(11, 42.57123, 87.82456, "Kenosha, Wisconsin"),  # longitude sign dropped
        _hh(28, -79.65432, 44.53217, "Orillia, ON"),  # lat/lon swapped
        _hh(29, 44.71234, 93.28765, "Mankato, MN"),  # 'MN' is Mongolia's code, but repairable
    ], "t"))
    stats = rp.classify_locations(list(recs.values()), places)
    assert (recs["hearham:11"]["lat"], recs["hearham:11"]["lon"]) == (42.57123, -87.82456)
    assert recs["hearham:11"]["location_quality"] == "site"
    assert (recs["hearham:28"]["lat"], recs["hearham:28"]["lon"]) == (44.53217, -79.65432)
    assert recs["hearham:29"]["lon"] == -93.28765 and recs["hearham:29"]["_claim_state"] == "MN"
    assert stats["coords_repaired"] == 3


def test_listing_contradicting_its_coordinates_is_dropped():
    """'Hiram, ME' geocoded into Mexico (the geocoder read ME as Mexico)."""
    (r,) = rp.normalize_hearham([_hh(12, 16.84123, -99.84567, "Hiram, ME")], "t")
    stats = rp.classify_locations([r], _places())
    assert r["lat"] is None and r["location_quality"] == "unknown"
    assert stats["no_location_contradicts_listing"] == 1


def test_ambiguous_abbreviations_follow_the_coordinates():
    recs = _by_id(rp.normalize_hearham([
        _hh(21, -31.90123, 115.90234, "Perth, WA"),  # Western Australia, not Washington
        _hh(22, 52.67123, 4.90456, "Obdam, NL"),  # Netherlands, not Newfoundland
        _hh(23, 41.92123, 45.49012, "Kurdghelauri St., Georgia"),  # the country
        _hh(24, 32.59321, -116.84567, "San Diego, CA"),  # border site: nearest town is Tijuana
    ], "t"))
    rp.classify_locations(list(recs.values()), _places())
    assert recs["hearham:21"]["lat"] is not None
    assert (recs["hearham:21"]["country"], recs["hearham:21"]["state"]) == ("AU", "WA")
    assert recs["hearham:22"]["country"] == "NL" and recs["hearham:22"]["lat"] is not None
    assert recs["hearham:23"]["country"] == "GE" and recs["hearham:23"]["lat"] is not None
    assert recs["hearham:24"]["lat"] is not None and recs["hearham:24"]["_claim_cc"] == "US"


def test_licence_country_never_drops_a_precise_fix_abroad():
    recs = _by_id(rp.normalize_brandmeister(_json("brandmeister.json"), "t"))
    tw, cn = recs["brandmeister:313365"], recs["brandmeister:460039"]
    rp.classify_locations([tw, cn], _places())
    assert (tw["lat"], tw["lon"]) == (25.02567, 121.54321)  # a US ham's repeater in Taipei
    assert cn["lat"] is None  # '38, -95' (Kansas) for a Chinese listing is just a default


# ============================================================== merge / dedupe / finalize

def _built(now=NOW):
    by_source = {
        "hearham": rp.normalize_hearham(_json("hearham.json"), "t"),
        "brandmeister": rp.normalize_brandmeister(_json("brandmeister.json"), "t"),
        "irlp": rp.normalize_irlp(rp._irlp_text(_irlp_bz2()), "t"),
    }
    merged, stats = rp.build(by_source, _places(), now)
    return _by_id(merged), stats


def test_build_merges_enriches_and_dedupes():
    out, stats = _built()
    assert len(out) == 44  # 33 HearHam - 1 duplicate + 6 BM-only + 6 IRLP-only
    assert stats["matched_brandmeister"] == 2 and stats["added_brandmeister"] == 6
    assert stats["matched_irlp"] == 1 and stats["added_irlp"] == 6 and stats["deduped"] == 1

    dmr = out["hearham:1005"]  # coarse HearHam fix + precise BrandMeister fix
    assert (dmr["lat"], dmr["lon"], dmr["precise"], dmr["location_quality"]) == (
        34.69712, -106.40456, True, "site")
    assert dmr["also_in"] == [{"source": "brandmeister", "id": "brandmeister:310012",
                               "url": "https://brandmeister.network/#/device/310012"}]
    assert dmr["status"] == "on-air"  # HearHam says off; BrandMeister saw it yesterday
    assert dmr["last_update"] == "2026-10-15T21:00:00Z" and dmr["dmr_network"] == "BrandMeister"
    assert "brandmeister:310012" not in out

    kept = out["hearham:1006"]  # HearHam's own site fix is not replaced
    assert (kept["lat"], kept["lon"]) == (34.01523, -117.13789)

    irlp = out["hearham:1007"]
    assert irlp["status"] == "on-air" and irlp["also_in"][0]["id"] == "irlp:1528"
    assert "irlp:1528" not in out and "irlp:1811" in out

    dup = out["hearham:1001"]  # precise original kept, coarse duplicate absorbed
    assert "hearham:1013" not in out
    assert {"source": "hearham", "id": "hearham:1013", "url": "https://hearham.com/repeaters/1013"} \
        in dup["also_in"]
    assert dup["echolink"] == 654321  # gap filled from the duplicate
    assert "hearham:1014" in out  # same call + frequency 50 km away: a different machine


def test_build_finalizes_country_state_band_and_kind():
    out, _ = _built()
    assert out["hearham:1011"]["lon"] == -87.82456 and out["hearham:1011"]["state"] == "WI"
    assert out["hearham:1012"]["lat"] is None  # Maine listing in Mexico
    assert out["hearham:1021"]["country"] == "AU"
    assert out["hearham:1029"]["country"] == "NL"
    assert out["hearham:1030"]["country"] == "PR" and out["hearham:1030"]["state"] == "PR"
    assert out["hearham:1031"]["country"] == "US"  # nearest town is in Mexico, the site isn't
    assert out["brandmeister:313365"]["country"] == "TW"
    assert out["hearham:1004"]["country"] == "GB" and out["hearham:1004"]["band"] == "70cm"
    assert out["hearham:1022"]["band"] == "other"  # 3.4 GHz in the US
    assert out["hearham:1023"]["band"] == "1.25m" and out["hearham:1024"]["band"] == "other"
    assert out["hearham:1015"]["kind"] == "simplex_link" and out["irlp:4444"]["kind"] == "unknown"
    assert out["irlp:9999"]["country"] == "CA" and out["irlp:9999"]["state"] == "QC"
    assert out["hearham:1033"]["country"] == ""  # Antarctica


def test_brandmeister_last_seen_decides_status():
    out, _ = _built()
    assert out["brandmeister:310014"]["status"] == "on-air"  # seen 2.5 days ago
    assert out["brandmeister:310500"]["status"] == "unknown"  # seen 20 days ago
    later, _ = _built(NOW + timedelta(days=8))
    assert later["hearham:1005"]["status"] == "off-air"  # stale BM never upgrades HearHam's "off"
    assert later["brandmeister:310014"]["status"] == "unknown"


def test_dedupe_tolerances():
    base = dict(callsign="K0DUP", output_mhz=146.94, offset_mhz=-0.6, input_mhz=146.34,
                modes=["FM"], status="on-air", location_quality="site", precise=True)

    def mk(i, **kw):
        r = rp._blank("hearham", i, f"https://hearham.com/repeaters/{i}", "t")
        r.update(base, lat=39.0, lon=-94.0)
        r.update(kw)
        return r

    recs = [mk(1), mk(2, output_mhz=146.9424, lat=39.1, precise=False, location_quality="city"),
            mk(3, output_mhz=146.9430), mk(4, lat=39.3), mk(5, callsign="K0OTH"),
            mk(6, status="off-air", lat=39.01), mk(7, lat=None, lon=None)]
    out = _by_id(rp.dedupe(recs))
    assert sorted(out) == ["hearham:1", "hearham:3", "hearham:4", "hearham:5", "hearham:7"]
    assert sorted(a["id"] for a in out["hearham:1"]["also_in"]) == ["hearham:2", "hearham:6"]


def _dedupe_pairs() -> tuple[list[dict], str]:
    """(raw HearHam listings, IRLP status text). Each pair is one machine
    listed twice, near a fixture place; the precise copy is the one kept."""
    header = (FIX / "irlp.txt").read_text(encoding="utf-8").splitlines()[0]
    raw = [
        # the precise copy is a stale listing; the coordinator's copy says on the air
        _hh(1, 39.21111, -94.41111, "Testville, MO", call="K0AAA", operational=0),
        _hh(2, 39.21, -94.41, "Testville, MO", call="K0AAA", operational=1),
        # an AllStar node listing with offset 0, and the repeater's own listing
        _hh(3, 38.87111, -94.80222, "Testtown, KS", call="K0BBB", offset=0, group="Allstar",
            internet_node="2560"),
        _hh(4, 38.87, -94.80, "Testtown, KS", call="K0BBB"),
        # IRLP says the node on the precise copy is down: a live verdict beats a listing
        _hh(5, 47.75111, -117.30222, "Testburg, WA", call="K7CCC", group="IRLP", internet_node="7101"),
        _hh(6, 47.75, -117.30, "Testburg, WA", call="K7CCC"),
        # ...and IRLP saying the other copy's node is up beats a stale listing
        _hh(7, 45.07111, -93.15222, "Testfield, MN", call="K0DDD", operational=0),
        _hh(8, 45.07, -93.15, "Testfield, MN", call="K0DDD", group="IRLP", internet_node="7102"),
        # a duplicate never lowers the kept copy's status (here: its IRLP node is down)
        _hh(9, 34.94111, -82.30222, "Testcity, SC", call="K4EEE"),
        _hh(10, 34.94, -82.30, "Testcity, SC", call="K4EEE", group="IRLP", internet_node="7103"),
    ]
    irlp = "\n".join([header, _irlp_row(7101, status="DOWN", lat="", lon=""),
                      _irlp_row(7102, status="IDLE", lat="", lon=""),
                      _irlp_row(7103, status="DOWN", lat="", lon="")]) + "\n"
    return raw, irlp


def test_dedupe_takes_the_best_status_and_the_real_split():
    """B2R-7: the kept copy (chosen for its precise location) kept its own
    status and split, so a stale listing hid a live repeater (off-air) and an
    AllStar node listing (offset 0) turned a repeater into a hidden simplex
    link. Now a duplicate can raise the kept copy's status (but not overturn
    its own live IRLP verdict with a mere listing) and never lowers it; a 0
    offset takes the duplicate's real split."""
    raw, irlp_text = _dedupe_pairs()
    for places in (None, _places()):
        out, stats = rp.build({"hearham": rp.normalize_hearham(copy.deepcopy(raw), "t"),
                               "irlp": rp.normalize_irlp(irlp_text, "t")}, places, NOW)
        _check_dedupe_pairs(_by_id(out), stats)


def _check_dedupe_pairs(out, stats):
    assert sorted(out) == ["hearham:1", "hearham:3", "hearham:5", "hearham:7", "hearham:9"]
    assert stats["deduped"] == 5
    assert out["hearham:1"]["status"] == "on-air"  # was off-air
    assert out["hearham:1"]["also_in"][0]["id"] == "hearham:2" and out["hearham:1"]["precise"] is True
    split = out["hearham:3"]
    assert (split["offset_mhz"], split["input_mhz"], split["kind"]) == (-0.6, 146.34, "repeater")  # was 0 / simplex_link
    assert split["allstar"] == 2560
    assert out["hearham:5"]["status"] == "off-air"  # IRLP's DOWN still wins over a listing
    assert out["hearham:7"]["status"] == "on-air"  # was off-air: IRLP sees the other copy's node
    assert out["hearham:9"]["status"] == "on-air"  # the duplicate's dead IRLP node changes nothing


# ============================================================== refresh state machine

class Upstream:
    """Stands in for HearHam, BrandMeister, IRLP and GeoNames."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.override: dict[str, object] = {}
        self.hearham = _json("hearham.json")
        self.bm = _json("brandmeister.json")
        self.irlp = _irlp_bz2()
        self.places = _places_zip()
        self.talkgroups = [{"talkgroup": "3135", "slot": "1", "repeaterid": "310012"},
                           {"talkgroup": "31301", "slot": "2", "repeaterid": "310012"},
                           {"talkgroup": "91", "slot": "1", "repeaterid": "310012"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        ov = self.override.get(host)
        if ov is not None:
            return ov(request) if callable(ov) else ov
        if host == "hearham.com":
            return httpx.Response(200, json=self.hearham)
        if host == "api.brandmeister.network":
            if request.url.path.endswith("/talkgroup"):
                return httpx.Response(200, json=self.talkgroups)
            return httpx.Response(200, json=self.bm)
        if host == "status.irlp.net":
            return httpx.Response(200, content=self.irlp)
        if host == "download.geonames.org":
            return httpx.Response(200, content=self.places)
        return httpx.Response(404)

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


@pytest.fixture()
def env(monkeypatch, tmp_path, db):
    monkeypatch.setattr(get_settings(), "repeaters_enabled", True)
    monkeypatch.setattr(rp, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(rp, "_MIN_PLACES", 1)
    monkeypatch.setattr(rp, "_jitter_s", lambda source: 0.0)  # jitter has its own test
    clock = {"now": NOW}
    monkeypatch.setattr(rp, "_now", lambda: clock["now"])
    up = Upstream()
    real_client = httpx.Client

    class MockClient(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(up.handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(rp.httpx, "Client", MockClient)
    _reset_tables()
    rp.reset_talkgroup_limits()
    yield SimpleNamespace(up=up, clock=clock, tmp=tmp_path)
    _reset_tables()
    rp.reset_talkgroup_limits()


def _advance(env, **delta):
    env.clock["now"] = env.clock["now"] + timedelta(**delta)
    env.up.requests.clear()


def test_first_refresh_fetches_every_source_once_and_builds(env):
    assert rp.ensure_fresh() is True
    assert sorted(env.up.hosts()) == ["api.brandmeister.network", "download.geonames.org",
                                      "hearham.com", "status.irlp.net"]
    ua = get_settings().http_user_agent
    for req in env.up.requests:
        assert req.headers["user-agent"] == ua
        timeout = req.extensions["timeout"]
        assert timeout["connect"] and timeout["read"]  # never an unbounded request
    bm_req = next(r for r in env.up.requests if r.url.host == "api.brandmeister.network")
    assert bm_req.url.path == "/v2/device" and bm_req.url.params["repeater"] == "true"
    rows = _rows()
    assert len(rows) == 44
    assert rows["hearham:1005"]["lat"] == 34.69712 and rows["hearham:1012"]["lat"] is None
    assert json.loads(rows["hearham:1005"]["also_in"])[0]["id"] == "brandmeister:310012"
    for name in rp.SOURCES:
        st = _state(name)
        assert (st["ok"], st["error"], st["fetched_at"]) == (1, None, "2026-10-16T21:00:00Z")
    assert _state("hearham")["count"] == 33 and _state("brandmeister")["count"] == 8
    assert list(env.tmp.iterdir()) == []  # temp downloads cleaned up


def test_refresh_respects_each_sources_interval(env):
    assert rp.ensure_fresh() is True
    _advance(env, minutes=30)
    assert rp.ensure_fresh() is True and env.up.requests == []  # nothing due, nothing rebuilt

    _advance(env, hours=12)  # IRLP (12 h) is due; BM (24 h) and HearHam (7 d) are not
    env.up.irlp = _irlp_bz2((FIX / "irlp.txt").read_text(encoding="utf-8").replace("\tIDLE\t", "\tDOWN\t", 1))
    assert rp.ensure_fresh() is True
    assert env.up.hosts() == ["status.irlp.net"]
    rows = _rows()
    assert rows["hearham:1007"]["status"] == "off-air"  # IRLP says node 1528 is down now
    assert rows["hearham:1001"]["lat"] is not None and "brandmeister:310014" in rows  # others kept

    _advance(env, hours=12)  # BM 24 h after its last fetch; IRLP 12 h after its last
    assert rp.ensure_fresh() is True
    assert sorted(env.up.hosts()) == ["api.brandmeister.network", "status.irlp.net"]

    _advance(env, days=6)  # HearHam's 7 days are up
    assert rp.ensure_fresh() is True
    assert "hearham.com" in env.up.hosts() and "download.geonames.org" not in env.up.hosts()


def test_failing_source_keeps_its_last_good_set_and_backs_off(env):
    assert rp.ensure_fresh() is True
    _advance(env, days=8)
    env.up.override["hearham.com"] = httpx.Response(503)
    assert rp.ensure_fresh() is False  # scheduler retries in 15 minutes
    st = _state("hearham")
    assert (st["ok"], st["count"], st["failures"]) == (0, 33, 1)
    assert st["error"] == "HearHam: HTTP 503"
    assert st["retry_at"] == rp._iso(env.clock["now"] + timedelta(minutes=15))
    assert st["last_ok_at"] == "2026-10-16T21:00:00Z"
    rows = _rows()
    assert "hearham:1001" in rows and len(rows) == 44  # BM/IRLP refreshed, HearHam rows kept
    assert _state("brandmeister")["fetched_at"] == rp._iso(env.clock["now"])

    _advance(env, minutes=5)
    assert rp.ensure_fresh() is True and env.up.requests == []  # backing off, nothing due

    _advance(env, minutes=11)
    assert rp.ensure_fresh() is False and env.up.hosts() == ["hearham.com"]
    st = _state("hearham")
    assert st["failures"] == 2
    assert st["retry_at"] == rp._iso(env.clock["now"] + timedelta(minutes=30))  # doubled

    _advance(env, minutes=31)
    del env.up.override["hearham.com"]
    assert rp.ensure_fresh() is True and env.up.hosts() == ["hearham.com"]
    st = _state("hearham")
    assert (st["ok"], st["failures"], st["error"], st["retry_at"]) == (1, 0, None, None)


def test_429_retry_after_is_honoured(env):
    env.up.override["api.brandmeister.network"] = httpx.Response(429, headers={"Retry-After": "7200"})
    assert rp.ensure_fresh() is False
    st = _state("brandmeister")
    assert st["retry_at"] == rp._iso(NOW + timedelta(hours=2))
    assert st["error"] == "BrandMeister: HTTP 429"
    _advance(env, hours=1)
    assert rp.ensure_fresh() is True and env.up.requests == []
    assert rp._backoff_s(10, None) == rp._MAX_BACKOFF_S  # capped at 6 h


def test_503_retry_after_is_honoured_too(env):
    """B2R-9: a 503's Retry-After (not only a 429's) sets the next try."""
    env.up.override["hearham.com"] = httpx.Response(503, headers={"Retry-After": "7200"})
    assert rp.ensure_fresh() is False
    assert _state("hearham")["retry_at"] == rp._iso(NOW + timedelta(hours=2))  # not the 15-min backoff
    _advance(env, minutes=119)
    assert rp.ensure_fresh() is True and env.up.requests == []
    del env.up.override["hearham.com"]
    _advance(env, minutes=1)
    assert rp.ensure_fresh() is True and env.up.hosts() == ["hearham.com"]


def test_retry_after_values():
    day = rp._MAX_RETRY_AFTER_S
    assert day == 24 * 3600
    assert rp._retry_after("120") == 120.0 and rp._retry_after("-5") == 0.0
    assert rp._retry_after("99999999") == day and rp._retry_after("1e12") == day
    assert rp._retry_after("Fri, 31 Dec 9999 23:59:59 GMT") == day
    for junk in (None, "", "inf", "-inf", "nan", "1e400", "soon", "Mon, 99 Foo 2026"):
        assert rp._retry_after(junk) is None, junk
    assert rp._backoff_s(1, 10.0 ** 12) == day  # whatever reaches it


def test_retry_after_is_capped_at_a_day_and_nonsense_is_ignored(env):
    """B2R-6: 'Retry-After: 99999999' parked BrandMeister until 2029 (and the
    stored retry_at survived restarts); '1e12' or 'inf' overflowed inside the
    failure handler, so nothing was recorded and the source was fetched again
    on every 15-minute cycle."""
    bm = "api.brandmeister.network"
    env.up.override[bm] = httpx.Response(503, headers={"Retry-After": "99999999"})
    assert rp.ensure_fresh() is False
    assert _state("brandmeister")["retry_at"] == rp._iso(NOW + timedelta(days=1))  # was 2029-12-17
    _advance(env, hours=23, minutes=59)
    assert rp.ensure_fresh() is True and bm not in env.up.hosts()  # (IRLP's 12 h were up)
    _advance(env, minutes=1)
    env.up.override[bm] = httpx.Response(429, headers={"Retry-After": "1e12"})
    assert rp.ensure_fresh() is False  # was: OverflowError out of ensure_fresh
    assert env.up.hosts() == [bm]
    st = _state("brandmeister")
    assert (st["failures"], st["retry_at"]) == (2, rp._iso(env.clock["now"] + timedelta(days=1)))
    _advance(env, days=1)
    env.up.override[bm] = httpx.Response(429, headers={"Retry-After": "inf"})
    assert rp.ensure_fresh() is False  # nonsense: the usual backoff (3rd failure: 60 min)
    assert _state("brandmeister")["retry_at"] == rp._iso(env.clock["now"] + timedelta(hours=1))
    _advance(env, minutes=15)
    assert rp.ensure_fresh() is True and env.up.requests == []  # backing off, not every cycle
    del env.up.override[bm]
    _advance(env, minutes=45)
    assert rp.ensure_fresh() is True and env.up.hosts() == [bm]
    assert _state("brandmeister")["ok"] == 1


def test_a_retry_stored_years_ahead_is_capped(env):
    """A retry_at stored before the cap existed no longer parks a source."""
    rp._upsert_state("brandmeister", {"fetched_at": rp._iso(NOW), "ok": 0, "failures": 1,
                                      "error": "BrandMeister: HTTP 503", "retry_at": "2029-12-17T07:46:39Z"})
    assert rp.next_due("brandmeister", _state("brandmeister")) == NOW + timedelta(days=1)
    assert rp.ensure_fresh() is True and "api.brandmeister.network" not in env.up.hosts()
    _advance(env, days=1)
    assert rp.ensure_fresh() is True and "api.brandmeister.network" in env.up.hosts()


def test_duplicate_upstream_id_never_blocks_a_rebuild(env):
    """B2R-3, the reviewer's scenario: HearHam lists one ID twice (one copy
    without coordinates, so dedupe never pairs them). The swap then failed
    with 'UNIQUE constraint failed' on every retry, and IRLP's news (node 1528
    went down) never reached the map."""
    assert rp.ensure_fresh() is True
    _advance(env, days=8)  # every source due
    twin = _hh(9010, 39.31234, -94.71234)
    env.up.hearham = env.up.hearham + [dict(twin, latitude=None, longitude=None), twin]
    env.up.irlp = _irlp_bz2((FIX / "irlp.txt").read_text(encoding="utf-8").replace("\tIDLE\t", "\tDOWN\t", 1))
    assert rp.ensure_fresh() is True  # was False, and False on every retry
    rows = _rows()
    assert rows["hearham:9010"]["lat"] == 39.31234
    assert rows["hearham:1007"]["status"] == "off-air"
    assert _state("hearham")["count"] == 34 and rp.source_status()["_build"]["error"] is None


def test_stored_set_with_duplicate_ids_still_builds(env):
    """A last good set stored by older code may hold both copies."""
    (rec,) = rp.normalize_hearham([_hh(9011, 39.31234, -94.71234)], "t")
    rp._save_success("hearham", rp._pack_records([rec, dict(rec, lat=None, lon=None)]), 2,
                     NOW - timedelta(days=1))
    assert rp.ensure_fresh() is True  # HearHam isn't due; the others are, and the build works
    assert "hearham.com" not in env.up.hosts()
    assert _rows()["hearham:9011"]["lat"] == 39.31234


def test_a_download_that_takes_too_long_is_cut_off(env, monkeypatch):
    """B2R-9: the wall-clock cap on streamed downloads (HearHam, GeoNames)."""
    clock = {"t": 0.0}
    monkeypatch.setattr(rp, "time", SimpleNamespace(monotonic=lambda: clock["t"]))

    def trickle():  # a valid JSON list, a megabyte every ~7 minutes
        for _ in range(3):
            clock["t"] += 400
            yield b" " * (1 << 20)
        yield json.dumps(env.up.hearham).encode()

    env.up.override["hearham.com"] = lambda req: httpx.Response(200, content=trickle())
    assert rp.ensure_fresh() is False
    assert _state("hearham")["error"] == "HearHam: download took longer than 600s"
    assert list(env.tmp.iterdir()) == []  # the partial file is gone


def test_irlp_status_file_over_the_size_cap_is_rejected(env, monkeypatch):
    """B2R-9: the bz2 is unpacked only up to a cap (a bomb can't eat the RAM)."""
    monkeypatch.setattr(rp, "_IRLP_MAX_TEXT", 500)  # the fixture unpacks to ~1.2 kB
    assert rp.ensure_fresh() is False
    assert _state("irlp")["error"] == "IRLP: status file truncated or too large"
    assert not any(k.startswith("irlp:") for k in _rows())


def test_table_with_an_old_schema_is_rebuilt(env):
    """B2R-9: a repeaters table with other columns (older code) is rebuilt
    from the stored sets, with no network, even though it still has rows."""
    assert rp.ensure_fresh() is True
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE repeaters"))
        conn.execute(text("CREATE TABLE repeaters (id TEXT PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO repeaters VALUES ('hearham:1', 'old')"))
    _advance(env, minutes=1)
    assert rp.ensure_fresh() is True and env.up.requests == []
    assert rp._schema_ok() and len(_rows()) == 44


def test_first_run_with_one_source_down_still_builds_the_rest(env):
    env.up.override["hearham.com"] = httpx.Response(500)
    assert rp.ensure_fresh() is False
    rows = _rows()
    assert rows and all(not k.startswith("hearham:") for k in rows)
    assert "brandmeister:310012" in rows  # unmatched without HearHam: its own row
    _advance(env, minutes=16)
    del env.up.override["hearham.com"]
    assert rp.ensure_fresh() is True
    rows = _rows()
    assert len(rows) == 44 and "brandmeister:310012" not in rows


def test_suspiciously_small_refresh_is_rejected(env):
    big = [_hh(5000 + i, 39.0 + i / 1000, -94.0 - i / 1000) for i in range(150)]
    env.up.hearham = big
    assert rp.ensure_fresh() is True and _state("hearham")["count"] == 150
    _advance(env, days=8)
    env.up.hearham = big[:10]
    assert rp.ensure_fresh() is False
    st = _state("hearham")
    assert st["ok"] == 0 and st["count"] == 150 and "only 10 records" in st["error"]
    assert sum(1 for k in _rows() if k.startswith("hearham:")) == 150


def test_invalid_payloads_fail_cleanly(env):
    env.up.override["hearham.com"] = httpx.Response(200, json={"error": "rate limited"})
    env.up.override["status.irlp.net"] = httpx.Response(200, content=b"BZh91AY&SY garbage")
    env.up.override["api.brandmeister.network"] = httpx.Response(200, content=b"<html>")
    assert rp.ensure_fresh() is False
    assert "expected a JSON list" in _state("hearham")["error"]
    assert _state("brandmeister")["error"].startswith("BrandMeister: invalid JSON")
    assert _state("irlp")["ok"] == 0 and _state("irlp")["error"].startswith("IRLP: unreadable")
    assert _rows() == {}


def test_failed_rebuild_is_retried_even_when_no_fetch_is_due(env, monkeypatch):
    assert rp.ensure_fresh() is True
    _advance(env, hours=13)
    env.up.irlp = _irlp_bz2((FIX / "irlp.txt").read_text(encoding="utf-8").replace("\tIDLE\t", "\tDOWN\t", 1))
    real_write = rp.write_table

    def locked(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rp, "write_table", locked)
    assert rp.ensure_fresh() is False  # the IRLP set was stored, the swap failed
    assert _rows()["hearham:1007"]["status"] == "on-air"  # still the old table
    monkeypatch.setattr(rp, "write_table", real_write)
    _advance(env, minutes=15)
    assert rp.ensure_fresh() is True and env.up.requests == []  # nothing due, but stale table
    assert _rows()["hearham:1007"]["status"] == "off-air"


def test_malformed_upstream_records_never_block_a_rebuild(env):
    """B2-2, the reviewer's scenario: an unbounded internet_node or a lone
    surrogate (a truncated emoji) was stored as part of the last good set, and
    then every swap failed (OverflowError / UnicodeEncodeError) on every
    15-minute retry, so IRLP's and BrandMeister's news never reached the map."""
    assert rp.ensure_fresh() is True
    _advance(env, days=8)  # every source due
    poison = [_hh(9001, 39.15123, -94.40101, group="DMR", mode="DMR",
                  internet_node="31012345678901234567890"),
              _hh(9002, 39.25123, -94.30101, group="Allstar", internet_node="31001²"),
              _hh(9003, 39.35123, -94.20101, description="Club rptr \ud83d")]
    env.up.override["hearham.com"] = httpx.Response(  # json.dumps sends the surrogate as '\\ud83d'
        200, content=json.dumps(env.up.hearham + poison).encode(),
        headers={"content-type": "application/json"})
    irlp = (FIX / "irlp.txt").read_text(encoding="utf-8").replace("\tIDLE\t", "\tDOWN\t", 1)
    env.up.irlp = _irlp_bz2(irlp + _irlp_row(7001, lastupdate="99999999999999") + "\n")
    assert rp.ensure_fresh() is True  # before: False, then False on every retry
    rows = _rows()
    assert rows["hearham:1007"]["status"] == "off-air"  # IRLP says node 1528 went down: the map knows
    assert rows["hearham:9001"]["dmr_id"] is None and rows["hearham:9002"]["allstar"] is None
    assert rows["hearham:9003"]["notes"] == "Club rptr \ufffd" and "irlp:7001" in rows
    assert all(rp.source_status()[n]["ok"] for n in rp.SOURCES)
    assert rp.source_status()["_build"]["error"] is None
    _advance(env, minutes=15)
    assert rp.ensure_fresh() is True and env.up.requests == []  # nothing stuck, nothing to retry


def test_unstorable_values_cost_one_value_not_the_swap(env):
    """A set stored by older code may still hold such values: the swap stores
    the rest of the record and every other record."""
    bad = _rec("hearham:9301", float("inf"), -94.6, dmr_id=31012345678901234567890, notes="Club \ud83d",
               city="Test\udc00ville")
    assert rp.write_table([bad, _rec("hearham:9302", 39.2, -94.6)]) == 2
    rows = _rows()
    assert (rows["hearham:9301"]["dmr_id"], rows["hearham:9301"]["lat"]) == (None, None)
    assert (rows["hearham:9301"]["notes"], rows["hearham:9301"]["city"]) == ("Club \ufffd", "Test\ufffdville")
    assert rows["hearham:9302"]["lat"] == 39.2


def test_stored_set_from_older_code_gets_todays_privacy_rules(env):
    """A source's last good set is used until its re-fetch succeeds. One
    normalized by code from before this scrub must not leak while HearHam
    is down."""
    (rec,) = rp.normalize_hearham([_hh(9201, 39.15123, -94.50101)], "t")
    rec.update(city="125 N Testwood St Allentown", notes="QTH: 239 TESTWOOD AV",
               name="125 n testwood st 146.9400")  # what the old normalizer stored
    rp._save_success("hearham", rp._pack_records([rec]), 1, NOW - timedelta(days=1),
                     version=rp.NORMALIZER_VERSION - 1)
    env.up.override["hearham.com"] = httpx.Response(503)  # its re-fetch fails: the old set stays
    assert rp.ensure_fresh() is False
    row = _rows()["hearham:9201"]
    assert (row["city"], row["notes"]) == ("Allentown", "QTH: [address removed]")
    assert "testwood" not in row["name"].lower()


def test_a_table_from_the_first_release_is_rescrubbed_with_every_source_down(env):
    """B2R-1 on upgrade: commit 6e93885 stored its sets as normalizer version 1
    and built its table as build version 1, street addresses included. With
    every re-fetch failing, nothing rebuilt that table (no fetch succeeded, the
    build version matched), so it kept serving them. The build version bump
    rebuilds it from the stored sets with today's privacy rules."""
    (rec,) = rp.normalize_hearham([_hh(9201, 39.15123, -94.50101)], "t")
    rec.update(city="125 N Testwood St Allentown", notes="QTH: 239 TESTWOOD AV")
    earlier = NOW - timedelta(hours=1)
    rp._save_success("hearham", rp._pack_records([rec]), 1, earlier, version=1)
    rp.write_table([copy.deepcopy(rec)])
    rp._upsert_state(rp._BUILD, {"fetched_at": rp._iso(earlier), "ok": 1, "count": 1, "version": 1})
    for host in ("hearham.com", "api.brandmeister.network", "status.irlp.net", "download.geonames.org"):
        env.up.override[host] = httpx.Response(503)
    assert rp.ensure_fresh() is False
    row = _rows()["hearham:9201"]
    assert (row["city"], row["notes"]) == ("Allentown", "QTH: [address removed]")  # was the old row


def test_empty_build_never_replaces_the_map(env):
    rp.write_table([_rec("hearham:1", 39.1, -94.6)])
    assert rp.write_table([]) == 0
    assert list(_rows()) == ["hearham:1"]


def test_status_on_a_fresh_database_creates_nothing(env):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE repeater_sources"))
    st = rp.source_status()
    assert st["hearham"]["ok"] is None and st["_build"]["built_at"] is None
    with engine.begin() as conn:
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name = 'repeater_sources'")
                            ).first() is None


def test_geonames_outage_degrades_then_rebuilds_without_refetching(env):
    env.up.override["download.geonames.org"] = httpx.Response(500)
    assert rp.ensure_fresh() is True  # repeaters still build; places retry on their own
    meta = rp.source_status()["_build"]
    assert meta["built_at"] and meta["with_places"] is False
    rows = _rows()
    assert rows["hearham:1011"]["lon"] == 87.82456  # unrepaired without places
    assert rows["hearham:1002"]["location_quality"] == "site"

    _advance(env, minutes=16)
    del env.up.override["download.geonames.org"]
    assert rp.ensure_fresh() is True
    assert env.up.hosts() == ["download.geonames.org"]  # sources were not fetched again
    rows = _rows()
    assert rows["hearham:1011"]["lon"] == -87.82456
    assert rows["hearham:1002"]["location_quality"] == "city"
    assert rp.source_status()["_build"]["with_places"] is True


def test_brandmeister_list_is_always_asked_for_repeaters_only(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "brandmeister_url",
                        "https://api.brandmeister.network/v2/device?repeater=false&x=1")
    rp.ensure_fresh()
    (req,) = [r for r in env.up.requests if r.url.host == "api.brandmeister.network"]
    assert req.url.params.get_list("repeater") == ["true"] and req.url.params["x"] == "1"
    assert rp.brandmeister_list_url("https://h/v2/device") == "https://h/v2/device?repeater=true"


def test_version_bumps_refetch_or_rebuild(env, monkeypatch):
    assert rp.ensure_fresh() is True
    _advance(env, minutes=1)
    monkeypatch.setattr(rp, "BUILD_VERSION", rp.BUILD_VERSION + 1)
    assert rp.ensure_fresh() is True
    assert env.up.requests == []  # rebuilt from stored records
    assert rp._load_states()[rp._BUILD]["version"] == rp.BUILD_VERSION

    _advance(env, minutes=1)
    monkeypatch.setattr(rp, "NORMALIZER_VERSION", rp.NORMALIZER_VERSION + 1)
    assert rp.ensure_fresh() is True
    assert sorted(env.up.hosts()) == ["api.brandmeister.network", "hearham.com", "status.irlp.net"]


def test_lost_table_is_rebuilt_from_stored_records_without_network(env):
    assert rp.ensure_fresh() is True
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE repeaters"))
    _advance(env, minutes=1)
    assert rp.ensure_fresh() is True
    assert env.up.requests == [] and len(_rows()) == 44


def test_talkgroup_cache_survives_rebuilds_for_24_hours(env, monkeypatch):
    assert rp.ensure_fresh() is True
    fresh = json.dumps({"at": rp._iso(NOW - timedelta(hours=1)), "talkgroups": [{"slot": 1, "tg": 91, "name": None}]})
    stale = json.dumps({"at": rp._iso(NOW - timedelta(hours=25)), "talkgroups": []})
    with engine.begin() as conn:
        conn.execute(text("UPDATE repeaters SET talkgroups = :t WHERE id = 'hearham:1005'"), {"t": fresh})
        conn.execute(text("UPDATE repeaters SET talkgroups = :t WHERE id = 'hearham:1006'"), {"t": stale})
    monkeypatch.setattr(rp, "BUILD_VERSION", rp.BUILD_VERSION + 1)
    assert rp.ensure_fresh() is True
    rows = _rows()
    assert rows["hearham:1005"]["talkgroups"] == fresh
    assert rows["hearham:1006"]["talkgroups"] is None


def test_disabled_never_touches_the_network(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "repeaters_enabled", False)
    assert rp.ensure_fresh() is True
    assert env.up.requests == [] and rp._load_states() == {}


def test_hearham_jitter(db, monkeypatch):
    _reset_tables()
    try:
        monkeypatch.setattr(rp.random, "uniform", lambda a, b: (a, b))
        assert rp._jitter_s("hearham") == (0, 6 * 3600)
        assert rp._jitter_s("irlp") == 0.0 and rp._jitter_s("brandmeister") == 0.0
        rp._save_success("hearham", rp._pack_records([]), 0, NOW, jitter_s=4321.0)
        due = rp.next_due("hearham", rp._load_states()["hearham"])
        assert due == NOW + timedelta(days=get_settings().repeaters_refresh_days, seconds=4321)
    finally:
        _reset_tables()


def test_hearham_jitter_applies_on_the_fetch_path(env, monkeypatch):
    """B2-5: the contract's 0-6 h HearHam jitter, end to end through
    ensure_fresh (the env fixture stubs _jitter_s, so nothing else covers it):
    drawn at every successful fetch, stored, and honoured by the schedule."""
    monkeypatch.setattr(rp, "_jitter_s", _REAL_JITTER)
    draws, jitter = [], {"s": 3 * 3600.0}

    def uniform(a, b):
        draws.append((a, b))
        return jitter["s"]

    monkeypatch.setattr(rp.random, "uniform", uniform)
    assert rp.ensure_fresh() is True
    assert draws == [(0, 6 * 3600)]  # HearHam only
    assert [_state(n)["jitter_s"] for n in rp.SOURCES] == [3 * 3600, 0, 0]
    days = get_settings().repeaters_refresh_days
    _advance(env, days=days, minutes=1)  # the interval is up, the jitter isn't
    assert rp.ensure_fresh() is True
    assert "hearham.com" not in env.up.hosts() and "status.irlp.net" in env.up.hosts()
    _advance(env, hours=3)  # 7 d + 3 h + 1 min after the first fetch
    jitter["s"] = 1800.0
    assert rp.ensure_fresh() is True and "hearham.com" in env.up.hosts()
    assert _state("hearham")["jitter_s"] == 1800  # a fresh draw at every success
    assert rp.next_due("hearham", _state("hearham")) == env.clock["now"] + timedelta(days=days, seconds=1800)


# ============================================================== routes

def _rec(rid, lat, lon, **kw):
    source, sid = rid.split(":")
    r = rp._blank(source, sid, f"https://example.org/{source}/{sid}", "2026-10-16T21:00:00Z")
    r.update(callsign="K0TST", output_mhz=146.94, offset_mhz=-0.6, input_mhz=146.34, band="2m",
             modes=["FM"], lat=lat, lon=lon, precise=True, location_quality="site",
             status="on-air", use="open", kind="repeater", country="US", state="MO",
             city="Kansas City")
    r.update(kw)
    r["name"] = r["name"] or f"{r['callsign']} {r['output_mhz']:.4f}"
    return r


ROUTE_ROWS = [
    _rec("hearham:1", 39.10, -94.60),
    _rec("hearham:2", 39.20, -94.40, band="70cm", output_mhz=444.1, modes=["DMR"], dmr_id=310777,
         dmr_network="BrandMeister", dmr_color_code=1),
    _rec("hearham:3", 39.15, -94.50, precise=False, location_quality="city"),
    _rec("hearham:4", 39.05, -94.55, status="off-air"),
    _rec("hearham:5", 39.12, -94.45, kind="simplex_link", offset_mhz=0.0, input_mhz=146.94),
    _rec("hearham:6", 39.08, -94.52, band="1.25m", output_mhz=224.5, modes=["FM", "YSF"],
         also_in=[{"source": "irlp", "id": "irlp:9", "url": "https://www.irlp.net/status/index.php?nodeid=9"}],
         allstar=2560, notes="Club machine"),
    _rec("hearham:7", 39.11, -94.58, band="other", output_mhz=3400.0, modes=["TETRA"]),
    _rec("hearham:8", None, None),  # no coordinates: never on the map
    _rec("hearham:9", 39.13, -94.57, status="unknown", band="6m", output_mhz=53.33),
    _rec("irlp:10", 41.70, -72.70, country="US", state="CT"),  # far away (Connecticut)
    _rec("hearham:11", 51.20, 179.95, country="US", state="AK"),  # west of the antimeridian
    _rec("hearham:12", 51.20, -179.95, country="US", state="AK"),  # east of it
]


@pytest.fixture()
def api(client, db, monkeypatch, env):
    """Signed-in user near Kansas City + a known repeaters table."""
    monkeypatch.setattr(get_settings(), "repeaters_enabled", True)
    rp.write_table(copy.deepcopy(ROUTE_ROWS))
    r = client.post("/api/auth/login", json={"callsign": "W1DDD", "password": "password123"})
    token = r.json()["token"] if r.status_code == 200 else signup(client, "W1DDD")["token"]
    user = db.query(User).filter_by(callsign="W1DDD").one()
    saved = (user.lat, user.lon)
    user.lat, user.lon = 39.10, -94.58
    db.commit()
    yield SimpleNamespace(client=client, h=auth(token), env=env, db=db, user=user)
    user.lat, user.lon = saved
    db.commit()


def _map(api, **params):
    base = dict(min_lat=38.5, max_lat=39.6, min_lon=-95.0, max_lon=-94.0, zoom=12)
    base.update(params)
    resp = api.client.get("/api/repeaters/map", params=base, headers=api.h)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(payload):
    return [r["id"] for r in payload["repeaters"]]


def test_routes_require_auth(client):
    for path in ("/api/repeaters/map?min_lat=0&max_lat=1&min_lon=0&max_lon=1&zoom=4",
                 "/api/repeaters/near?lat=1&lon=1", "/api/repeaters/status", "/api/repeaters/hearham:1"):
        assert client.get(path).status_code == 401


def test_map_detail_view_shape_order_and_default_filters(api):
    d = _map(api)
    assert d["type"] == "repeaters" and d["zoom"] == 12 and d["truncated"] is False
    # default: off-air (4) and simplex links (5) hidden, no-coords (8) never; precise first,
    # then on-air before unknown, then id
    assert _ids(d) == ["hearham:1", "hearham:2", "hearham:6", "hearham:7", "hearham:9", "hearham:3"]
    assert d["total"] == 6
    r = next(x for x in d["repeaters"] if x["id"] == "hearham:6")
    assert r == {
        "id": "hearham:6", "callsign": "K0TST", "name": "K0TST 224.5000", "output_mhz": 224.5,
        "input_mhz": 146.34, "offset_mhz": -0.6, "band": "1.25m", "modes": ["FM", "YSF"],
        "tone_up": None, "tone_down": None, "dcs": None, "tone_burst": None, "dmr_color_code": None,
        "dmr_id": None, "dmr_network": None, "nxdn_ran": None, "p25_nac": None, "m17_can": None,
        "city": "Kansas City", "state": "MO", "country": "US", "lat": 39.08, "lon": -94.52,
        "precise": True, "location_quality": "site", "status": "on-air", "use": "open",
        "kind": "repeater", "links": {"echolink": None, "allstar": 2560, "irlp": None, "wiresx": None},
        "notes": "Club machine", "source": "hearham", "source_url": "https://example.org/hearham/6",
        "also_in": [{"source": "irlp", "id": "irlp:9", "url": "https://www.irlp.net/status/index.php?nodeid=9"}],
        "last_update": None,
    }


def test_map_cap_reports_truncation(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "repeaters_map_max_results", 2)
    d = _map(api)
    assert _ids(d) == ["hearham:1", "hearham:2"] and d["total"] == 6 and d["truncated"] is True


@pytest.mark.parametrize("params, expected", [
    ({"bands": "2m"}, ["hearham:1", "hearham:3"]),
    ({"bands": "70cm,1.25m"}, ["hearham:2", "hearham:6"]),
    ({"bands": "other"}, ["hearham:7"]),
    ({"bands": "6m,other"}, ["hearham:7", "hearham:9"]),
    ({"bands": ""}, []),
    ({"bands": "nonsense"}, []),
    ({"modes": "DMR"}, ["hearham:2"]),
    ({"modes": "ysf,d-star"}, ["hearham:6"]),  # any listed mode matches
    ({"modes": "OTHER"}, ["hearham:7"]),  # TETRA files under OTHER
    ({"onair": "0"}, ["hearham:1", "hearham:2", "hearham:6", "hearham:7", "hearham:4",
                      "hearham:9", "hearham:3"]),  # precise first, then on-air, then id
    ({"onair": "false", "bands": "2m"}, ["hearham:1", "hearham:4", "hearham:3"]),
    ({"simplex": "true", "bands": "2m"}, ["hearham:1", "hearham:5", "hearham:3"]),
    ({"simplex": "1", "onair": "0", "bands": "2m"}, ["hearham:1", "hearham:5", "hearham:4", "hearham:3"]),
    ({"approx": "0"}, ["hearham:1", "hearham:2", "hearham:6", "hearham:7", "hearham:9"]),
])
def test_map_filters(api, params, expected):
    assert _ids(_map(api, **params)) == expected


def test_map_clusters_sit_on_the_members_mean(api):
    d = _map(api, zoom=7)
    cell = cell_degrees(7)
    assert (d["type"], d["zoom"], d["reason"], d["cell_degrees"]) == ("clusters", 7, "zoom", cell)
    assert d["total"] == 6
    (c,) = d["cells"]  # the six visible Kansas City repeaters share one 1-degree cell
    kc = [r for r in ROUTE_ROWS if r["id"] in ("hearham:1", "hearham:2", "hearham:3", "hearham:6",
                                               "hearham:7", "hearham:9")]
    assert c["count"] == 6
    assert c["lat"] == round(sum(r["lat"] for r in kc) / 6, 5)
    assert c["lon"] == round(sum(r["lon"] for r in kc) / 6, 5)
    assert (c["lat"], c["lon"]) != (39.5, -94.5)  # never the cell centre


def test_map_clusters_count_whole_edge_cells_but_total_counts_the_view(api):
    # the view stops at -94.56; the cells snap out to -94.50 and count whole cells
    d = _map(api, zoom=9, min_lat=39.0, max_lat=39.24, min_lon=-94.75, max_lon=-94.56)
    assert d["total"] == 3  # hearham:1, :7 and :9 are inside the view itself
    assert sum(c["count"] for c in d["cells"]) == 5  # + hearham:3 and :6 just east of it


def test_map_never_422s_on_wide_or_swapped_bboxes(api):
    d = _map(api, min_lat=95, max_lat=-95, min_lon=-400, max_lon=400, zoom=2)
    assert d["type"] == "clusters" and d["total"] == 9  # 6 KC + CT + both Aleutian rows
    d = _map(api, min_lon=-200, max_lon=-94.0, zoom=30)
    assert d["zoom"] == 22 and d["type"] == "repeaters"
    resp = api.client.get("/api/repeaters/map", params=dict(min_lat="nan", max_lat=1, min_lon=0,
                                                            max_lon=1, zoom=4), headers=api.h)
    assert resp.status_code == 422


def test_near_uses_the_users_location_and_sorts_by_distance(api):
    resp = api.client.get("/api/repeaters/near", params={"radius_km": 25}, headers=api.h)
    assert resp.status_code == 200
    reps = resp.json()["repeaters"]
    assert [r["id"] for r in reps] == ["hearham:7", "hearham:1", "hearham:9", "hearham:6",
                                       "hearham:3", "hearham:2"]
    first = reps[0]
    assert first["distance_km"] == round(rp.haversine_km(39.10, -94.58, 39.11, -94.58), 2)
    assert first["bearing_deg"] == 0.0  # due north
    one = next(r for r in reps if r["id"] == "hearham:1")
    assert 260 < one["bearing_deg"] < 280  # due west-ish
    assert all(a["distance_km"] <= b["distance_km"] for a, b in zip(reps, reps[1:]))
    assert {"distance_km", "bearing_deg", "links", "also_in"} <= set(first)


def test_near_explicit_point_radius_limit_and_validation(api):
    get = lambda **p: api.client.get("/api/repeaters/near", params=p, headers=api.h)  # noqa: E731
    reps = get(lat=41.7, lon=-72.7, radius_km=5).json()["repeaters"]
    assert [r["id"] for r in reps] == ["irlp:10"] and reps[0]["distance_km"] == 0.0
    assert len(get(radius_km=100, limit=2).json()["repeaters"]) == 2
    assert [r["id"] for r in get(radius_km=100, bands="70cm").json()["repeaters"]] == ["hearham:2"]
    assert get(lat=41.7).status_code == 400
    assert get(radius_km="nan").status_code == 422 and get(lat=95, lon=0).status_code == 422


def test_near_counts_every_repeater_within_the_radius(api):
    """QA F3: Activity asks for the nearest 50 and printed "50 repeaters within
    25 miles" where 150 are listed; `total` is the whole count, the list the
    nearest `limit`."""
    get = lambda **p: api.client.get("/api/repeaters/near", params=p, headers=api.h)  # noqa: E731
    everything = get(radius_km=25).json()
    assert everything["total"] == len(everything["repeaters"]) == 6
    capped = get(radius_km=25, limit=2).json()
    assert capped["total"] == 6 and [r["id"] for r in capped["repeaters"]] == ["hearham:7", "hearham:1"]
    assert get(radius_km=25, bands="70cm").json()["total"] == 1


def test_near_clamps_radius_and_limit_instead_of_422(api):
    """B2-4: like every other map/search limit (hams, places, APRS search),
    out-of-range values are clamped: the radius at 805 km, the app's own
    500-mile range."""
    get = lambda **p: api.client.get("/api/repeaters/near", params=p, headers=api.h)  # noqa: E731
    far = get(radius_km=5000)
    assert far.status_code == 200 and far.json() == get(radius_km=805).json()
    assert [r["id"] for r in get(lat=41.7, lon=-72.7, radius_km=0).json()["repeaters"]] == ["irlp:10"]
    assert get(lat=41.7, lon=-72.7, radius_km=-3).json()["repeaters"][0]["id"] == "irlp:10"
    assert len(get(limit=0).json()["repeaters"]) == 1 and len(get(limit=-5).json()["repeaters"]) == 1
    rp.write_table(copy.deepcopy(ROUTE_ROWS)
                   + [_rec(f"hearham:{5000 + i}", 39.1 + i / 10000, -94.58) for i in range(210)])
    assert len(get(radius_km=50, limit=1000).json()["repeaters"]) == 200


def test_near_counts_a_members_widest_range(api):
    """QA R2: the range goes up to 500 miles (805 km), but /near clamped the
    radius to 500 km (311 miles), so "The nearest 50 of 1,999 repeaters within
    500 miles" and the Feed's "Repeaters 1,999 listed" counted only 311 miles
    for every range over about 310 miles."""
    rp.write_table(copy.deepcopy(ROUTE_ROWS) + [
        _rec("hearham:60", 41.88, -87.63, state="IL", city="Chicago"),   # 664 km from the member
        _rec("hearham:61", 39.74, -104.99, state="CO", city="Denver"),   # 897 km: beyond 500 miles
    ])
    get = lambda **p: api.client.get("/api/repeaters/near", params=p, headers=api.h).json()  # noqa: E731
    assert get(radius_km=500)["total"] == 6
    wide = get(radius_km=805, limit=3)  # what Activity asks for a 500-mile range
    assert wide["total"] == 7 and len(wide["repeaters"]) == 3
    assert [r["id"] for r in wide["repeaters"]] == ["hearham:7", "hearham:1", "hearham:9"]  # still the nearest
    last = get(radius_km=805)["repeaters"][-1]
    assert last["id"] == "hearham:60" and 660 < last["distance_km"] < 668
    assert get(radius_km=2000)["total"] == 7  # clamped to 805 km: Denver stays out


def test_near_reaches_over_the_pole(api):
    """B2R-9: a radius that covers a pole covers every longitude; without that
    guard in _lon_ranges, asin() got a value > 1 and /near raised (500)."""
    rp.write_table(copy.deepcopy(ROUTE_ROWS) + [_rec("hearham:90", 89.95, 120.0, country="", state="")])
    get = lambda **p: api.client.get("/api/repeaters/near", params=p, headers=api.h)  # noqa: E731
    over = get(lat=89.9, lon=-60.0, radius_km=50)  # 0.15 degrees away, across the pole
    assert over.status_code == 200 and [r["id"] for r in over.json()["repeaters"]] == ["hearham:90"]
    assert over.json()["repeaters"][0]["distance_km"] < 20
    at = get(lat=90, lon=0, radius_km=10)
    assert at.status_code == 200 and [r["id"] for r in at.json()["repeaters"]] == ["hearham:90"]


def test_one_machine_listed_twice_shows_on_the_default_map(api):
    """B2R-7 end to end (the K7MLA / KB1AEV cases): the stale or offset-0
    listing that wins dedupe no longer hides the repeater from the default map."""
    raw, irlp_text = _dedupe_pairs()
    api.env.up.hearham, api.env.up.irlp = raw, _irlp_bz2(irlp_text)
    assert rp.ensure_fresh() is True
    box = lambda la, lo: _ids(_map(api, min_lat=la - 0.05, max_lat=la + 0.05,  # noqa: E731
                                   min_lon=lo - 0.05, max_lon=lo + 0.05))
    assert box(39.21, -94.41) == ["hearham:1"]  # was []: off-air
    assert box(38.87, -94.80) == ["hearham:3"]  # was []: a simplex link
    assert box(47.75, -117.30) == []  # IRLP says it is down
    assert box(45.07, -93.15) == ["hearham:7"]  # was []: off-air
    assert box(34.94, -82.30) == ["hearham:9"]
    d = api.client.get("/api/repeaters/hearham:3", headers=api.h).json()
    assert (d["kind"], d["offset_mhz"], d["input_mhz"], d["status"]) == ("repeater", -0.6, 146.34, "on-air")
    assert [a["id"] for a in d["also_in"]] == ["hearham:4"]


def test_near_crosses_the_antimeridian(api):
    reps = api.client.get("/api/repeaters/near", params=dict(lat=51.2, lon=179.99, radius_km=50),
                          headers=api.h).json()["repeaters"]
    assert [r["id"] for r in reps] == ["hearham:11", "hearham:12"]
    assert reps[1]["distance_km"] < 10


def test_near_without_any_location_is_400(api):
    api.user.lat = api.user.lon = None
    api.db.commit()
    resp = api.client.get("/api/repeaters/near", headers=api.h)
    assert resp.status_code == 400 and "location" in resp.json()["detail"]


def test_detail_and_talkgroups_fetched_once_then_cached(api):
    resp = api.client.get("/api/repeaters/hearham:6", headers=api.h)
    assert resp.status_code == 200 and resp.json()["talkgroups"] == []
    assert api.env.up.requests == []  # not a BrandMeister repeater: no lookup

    d = api.client.get("/api/repeaters/hearham:2", headers=api.h).json()
    assert d["talkgroups"] == [{"slot": 1, "tg": 91, "name": None}, {"slot": 1, "tg": 3135, "name": None},
                               {"slot": 2, "tg": 31301, "name": None}]
    (req,) = api.env.up.requests
    assert str(req.url) == "https://api.brandmeister.network/v2/device/310777/talkgroup"
    assert req.headers["user-agent"] == get_settings().http_user_agent
    again = api.client.get("/api/repeaters/hearham:2", headers=api.h).json()
    assert again["talkgroups"] == d["talkgroups"] and len(api.env.up.requests) == 1  # row cache

    _advance(api.env, hours=25)  # cache expired: one more lookup
    api.client.get("/api/repeaters/hearham:2", headers=api.h)
    assert len(api.env.up.requests) == 1

    assert api.client.get("/api/repeaters/nope:1", headers=api.h).status_code == 404


def test_talkgroup_failures_give_empty_list_and_are_not_hammered(api):
    api.env.up.override["api.brandmeister.network"] = httpx.Response(502)
    assert api.client.get("/api/repeaters/hearham:2", headers=api.h).json()["talkgroups"] == []
    assert api.client.get("/api/repeaters/hearham:2", headers=api.h).json()["talkgroups"] == []
    assert len(api.env.up.requests) == 1  # second call inside the 10-minute failure window
    with engine.begin() as conn:
        assert conn.execute(text("SELECT talkgroups FROM repeaters WHERE id = 'hearham:2'")).scalar() is None


def test_talkgroup_lookups_pause_on_429_and_keep_to_a_budget(api, monkeypatch):
    """B2-3: a 429 (or 5xx, or no answer) from BrandMeister pauses every
    talkgroup lookup for max(Retry-After, 10 min), not just that repeater's;
    and no more than 30 lookups a minute ever leave the server."""
    rp.write_table([_rec(f"hearham:{900 + i}", 39.1 + i / 1000, -94.6, modes=["DMR"], dmr_id=310700 + i,
                         dmr_network="BrandMeister") for i in range(40)])
    clock = {"t": 1000.0}
    monkeypatch.setattr(rp, "_mono", lambda: clock["t"])
    up = api.env.up

    def tg(i):
        return api.client.get(f"/api/repeaters/hearham:{900 + i}", headers=api.h).json()["talkgroups"]

    up.override["api.brandmeister.network"] = httpx.Response(429, headers={"Retry-After": "3600"})
    assert [tg(i) for i in range(5)] == [[]] * 5
    assert len(up.requests) == 1  # one 429 paused every lookup (before: 5 requests)
    clock["t"] += 601
    assert tg(0) == [] and len(up.requests) == 1  # 10 min later is still inside Retry-After
    del up.override["api.brandmeister.network"]
    clock["t"] += 3000
    assert tg(0) != [] and len(up.requests) == 2  # the hour is up

    up.override["api.brandmeister.network"] = httpx.Response(502)  # no Retry-After: 10 minutes
    assert tg(1) == [] and tg(2) == [] and len(up.requests) == 3
    clock["t"] += 599
    assert tg(2) == [] and len(up.requests) == 3
    clock["t"] += 2
    up.override["api.brandmeister.network"] = (
        lambda req: httpx.Response(404) if "/310701/" in req.url.path else httpx.Response(200, json=up.talkgroups))
    assert tg(1) == [] and tg(2) != [] and len(up.requests) == 5  # a 404 is that repeater's alone
    assert tg(1) == [] and len(up.requests) == 5  # ...and is not asked again for 10 minutes

    clock["t"] += 3600
    up.requests.clear()
    assert sum(1 for i in range(3, 40) if tg(i)) == 30  # 37 popups in a minute: 30 lookups
    assert len(up.requests) == 30
    clock["t"] += 61
    assert tg(39) != [] and len(up.requests) == 31


def test_no_street_address_reaches_the_api(api):
    """B2-1 end to end: listings whose 'city' is a street address (the live
    dump's shapes: lower case, 'AV' on a simplex node, 'State Highway', street
    type first, number after) are fetched, stored, rebuilt, and then served
    by /{id}, /near and /map without it."""
    api.env.up.hearham = [
        _hh(9101, 39.15123, -94.50101, "125 n testwood st allentown "),
        _hh(9102, 39.25123, -94.40101, "239 TESTWOOD AV", offset=0),
        _hh(9103, -36.41234, 174.61234, "1133 State Highway 1 Testwood, New Zealand",
            description="QTH: 1133 state highway 1, testwood"),
        _hh(9104, 52.41234, 4.91234, "Testwoodstraat 39, Netherlands"),
        _hh(9105, 45.52123, -73.60123, "178 rue du Testwood, Montréal, QC"),
    ]
    assert rp.ensure_fresh() is True
    ids = [f"hearham:{i}" for i in range(9101, 9106)]
    detail = [api.client.get(f"/api/repeaters/{i}", headers=api.h).json() for i in ids]
    assert [(d["city"], d["country"]) for d in detail] == [
        ("Allentown", "US"), ("", "US"), ("Testwood", "NZ"), ("", "NL"), ("Montréal", "CA")]
    assert detail[2]["notes"] == "QTH: [address removed], testwood"
    served = list(detail)
    for d in detail:
        near = api.client.get("/api/repeaters/near", headers=api.h, params=dict(
            lat=d["lat"], lon=d["lon"], radius_km=1, simplex=1)).json()["repeaters"]
        box = _map(api, min_lat=d["lat"] - 0.01, max_lat=d["lat"] + 0.01, min_lon=d["lon"] - 0.01,
                   max_lon=d["lon"] + 0.01, simplex=1)["repeaters"]
        assert d["id"] in [r["id"] for r in near] and d["id"] in [r["id"] for r in box]
        served += near + box
    for r in served:
        assert not re.search(r"[0-9]", r["city"]), r["city"]
        assert not re.search(r"(?i)testwood (st|av)|state highway|straat|rue du", r["name"] + r["notes"])


def test_failed_rebuild_backs_off_and_status_says_why(api, monkeypatch):
    """B2-2: a rebuild that keeps failing (here: the disk) is retried at 15,
    then 30 min... instead of a 7 s / 240 MB rebuild every 15 minutes, and
    /status shows why built_at stopped moving."""
    assert rp.ensure_fresh() is True
    real, calls = rp.write_table, []

    def broken(*a, **kw):
        calls.append(rp._now())
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(rp, "write_table", broken)
    status = lambda: api.client.get("/api/repeaters/status", headers=api.h).json()  # noqa: E731
    _advance(api.env, hours=13)  # IRLP due: fetched fine, the swap fails
    assert rp.ensure_fresh() is False and len(calls) == 1
    assert status()["build_error"] == "RuntimeError: disk I/O error"
    assert status()["built_at"] == "2026-10-16T21:00:00Z"
    _advance(api.env, minutes=5)
    assert rp.ensure_fresh() is False and len(calls) == 1  # backing off: no rebuild attempt
    _advance(api.env, minutes=10)
    assert rp.ensure_fresh() is False and len(calls) == 2  # 15 min after the first failure
    _advance(api.env, minutes=29)
    assert rp.ensure_fresh() is False and len(calls) == 2  # now 30 min
    monkeypatch.setattr(rp, "write_table", real)
    _advance(api.env, minutes=1)
    assert rp.ensure_fresh() is True and api.env.up.requests == []
    assert status()["build_error"] is None and status()["built_at"] == rp._iso(api.env.clock["now"])


def test_status(api):
    d = api.client.get("/api/repeaters/status", headers=api.h).json()
    assert d["enabled"] is True
    assert (d["count"], d["with_coords"], d["us"]) == (12, 11, 11)
    assert set(d["sources"]) == {"hearham", "brandmeister", "irlp"}
    assert d["sources"]["hearham"] == {"fetched_at": None, "ok": None, "count": 0, "error": None,
                                       "last_ok_at": None, "next_due": None}
    assert d["built_at"] is None and d["build_error"] is None
    assert d["attribution"] == [
        {"name": "HearHam", "url": "https://hearham.com/repeaters"},
        {"name": "BrandMeister", "url": "https://brandmeister.network/"},
        {"name": "IRLP", "url": "https://www.irlp.net/"},
    ]
    assert d["repeaterbook_near_url"] == (
        "https://www.repeaterbook.com/repeaters/prox2_result.php?lat={lat}&long={lon}&distance=25&Dunit=m")


def test_status_reports_source_health_after_a_refresh(api):
    api.env.up.override["status.irlp.net"] = httpx.Response(503)
    rp.ensure_fresh()
    d = api.client.get("/api/repeaters/status", headers=api.h).json()
    assert d["sources"]["hearham"]["ok"] is True and d["sources"]["hearham"]["count"] == 33
    irlp = d["sources"]["irlp"]
    assert irlp["ok"] is False and irlp["error"] == "IRLP: HTTP 503"
    assert irlp["next_due"] == rp._iso(NOW + timedelta(minutes=15))
    assert d["built_at"] == "2026-10-16T21:00:00Z"
    assert d["count"] == 38  # 32 HearHam + 6 BrandMeister-only; no IRLP set yet


def test_disabled_server_serves_no_repeaters(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "repeaters_enabled", False)
    d = _map(api)
    assert d["repeaters"] == [] and d["total"] == 0
    assert _map(api, zoom=5)["cells"] == []
    assert api.client.get("/api/repeaters/near", headers=api.h).json() == {"repeaters": [], "total": 0}
    assert api.client.get("/api/repeaters/hearham:1", headers=api.h).status_code == 404
    assert api.client.get("/api/repeaters/status", headers=api.h).json()["enabled"] is False
