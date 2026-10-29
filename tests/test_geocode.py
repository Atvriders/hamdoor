"""Geocode quality and registered-user locations. Addresses here are made
up (patterns copied from real Census Non_Exact replies). No network."""

import itertools
import os
import re
import sqlite3

import httpx
import pytest
from sqlalchemy import text

from app.db import engine
from app.geo import grid_to_latlon, haversine_miles
from app.integrations import geocode, uls
from app.integrations.callook import CallookProvider
from app.integrations.census_geocoder import matched_address_agrees, split_address_key
from app.integrations.provider import CallsignProvider, CallsignRecord, get_provider
from app.main import app
from app.models import Ham, User
from app.security import create_token, hash_password
from tests.conftest import auth


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(uls, "_data_dir", lambda: str(tmp_path))

    def no_network(self, request):
        raise AssertionError(f"network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)


# ---------------------------------------------------------------- Non_Exact check

AGREE = [
    # (street, city, state, zip, Census matched address)
    ("12 Elmwood Ave 218", "Springdale", "AR", "72762", "12 ELMWOOD AVE, SPRINGDALE, AR, 72762"),  # bare unit
    ("40 VERNON", "Lakeside", "CA", "92040", "40 VERNON ST, LAKESIDE, CA, 92040"),  # no suffix
    ("9 VANGAURD AVE", "Lakeside", "CA", "92040", "9 VANGUARD AVE, LAKESIDE, CA, 92040"),  # typo
    ("31 Tervesta St #2", "Pinecrest", "FL", "33156", "31 TEQUESTA ST, PINECREST, FL, 33156"),
    ("77 TALLPINE", "Arcadia", "CA", "91006", "77 TALL PINE DR, ARCADIA, CA, 91006"),  # spacing
    ("5 Riverbottom Rd - LOT 2", "Nephi", "UT", "84648", "5 N RIVER BOTTOM RD, NEPHI, UT, 84648"),
    ("8 Ninth Ave Rear House", "Elkins", "WV", "26241", "8 9TH AVE, ELKINS, WV, 26241"),  # ordinal word
    ("6 Ne 6st Apt 2", "Dania", "FL", "33004", "6 NE 6TH ST, DANIA, FL, 33004"),
    ("3 W 28 Ave 203", "Hialeah", "FL", "33012", "3 W 28TH AVE, HIALEAH, FL, 33012"),
    ("41 SOUTH 4190 WEST", "Riverton", "UT", "84065", "41 S 4190 W, RIVERTON, UT, 84009"),  # grid, ZIP fixed
    ("90 N 3870 Rd.", "Weleetka", "OK", "74880", "90 N3870 RD, WELEETKA, OK, 74880"),
    ("3 Rt 221 W", "Marathon", "NY", "13803", "3 STATE RTE 221, MARATHON, NY, 13803"),  # highways
    ("44 w highway 33", "Sapulpa", "OK", "74066", "44 STATE HWY 33, SAPULPA, OK, 74066"),
    ("2 S. Camino Real", "Ivins", "UT", "84738", "2 CAM REAL, IVINS, UT, 84738"),
    ("8 CALLE DORADO", "San Clemente", "CA", "96272", "8 CLL DORADO, SAN CLEMENTE, CA, 92672"),  # bad ZIP
    ("7 MC CLELLAND", "Ashley", "MI", "48806", "7 S MCCLELLAND RD, ASHLEY, MI, 48806"),
    ("6 A Mechanic Street", "Harrington", "DE", "19952", "6 MECHANIC ST, HARRINGTON, DE, 19952"),
    ("4 O BRIEN AVE", "Brooksville", "FL", "34613", "4 OBRIEN AVE, BROOKSVILLE, FL, 34613"),
    ("63-12 84th Pl", "Middle Village", "NY", "11379", "6312 84TH PL, MIDDLE VILLAGE, NY, 11379"),
    ("G 405 W. Court St.", "Flint", "MI", "48532", "405 W COURT ST, FLINT, MI, 48532"),
    ("1 1/2 W MAIN ST", "Clinton", "NC", "28328", "1 1/2 W MAIN ST, CLINTON, NC, 28328"),
    ("5 KAUTZ DR.", "Dayton", "OH", "45424", "5 KAUTZ ST, DAYTON, OH, 45424"),  # type differs, same town
    ("9 Ralston ROAD", "Sherwood", "WA", "99362", "9 RALSTON RD, WALLA WALLA, WA, 99362"),
    ("3 Benchmark Loop", "Tucson", "AZ", "85747", "3 E BENCH MARK LOOP, TUCSON, AZ, 85747"),
    ("11 Harbur view BLVD Suite 4", "Suffolk", "VA", "23435", "11 HARBOUR VIEW BLVD, SUFFOLK, VA, 23435"),
    # review B1-4: the same street, rejected before (~2.4k cached addresses)
    ("123 E NORTH ST", "Ames", "IA", "50010", "123 E NORTH ST, AMES, IA, 50010"),  # compass-word name
    ("55 W SOUTH COVE RD", "Orem", "UT", "84057", "55 W SOUTH COVE RD, OREM, UT, 84057"),
    ("901 N WEST END BLVD", "Quakertown", "PA", "18951", "901 N WEST END BLVD, QUAKERTOWN, PA, 18951"),
    ("4410 BOX CANYON RD", "Sedona", "AZ", "86336", "4410 BOX CANYON RD, SEDONA, AZ, 86336"),
    ("1234 NO ATLANTIC AVE", "Daytona Beach", "FL", "32118", "1234 N ATLANTIC AVE, DAYTONA BEACH, FL, 32118"),
    ("C/O JANE DOE 12 MAIN ST", "Ames", "IA", "50010", "12 MAIN ST, AMES, IA, 50010"),  # prefixes
    ("URB LAS LOMAS 99 CEREIPO ST", "San Juan", "PR", "00921", "99 CEREIPO ST, SAN JUAN, PR, 00921"),
    ("PO BOX 123, 28 ALDER ST", "Ames", "IA", "50010", "28 ALDER ST, AMES, IA, 50010"),
    ("RR 4 21 OAK DR", "Ames", "IA", "50010", "21 OAK DR, AMES, IA, 50010"),
    ("5-1/2 KENWOOD AVE", "Catonsville", "MD", "21228", "5 1/2 KENWOOD AVE, CATONSVILLE, MD, 21228"),
    ("33020-10TH AVE SW", "Federal Way", "WA", "98023", "33020 10TH AVE SW, FEDERAL WAY, WA, 98023"),
    ("N6742 LAUREL RD", "Shawano", "WI", "54166", "N6742 LAUREL RD, SHAWANO, WI, 54166"),  # rural grid
    ("511SHERWOOD DR", "Ames", "IA", "50010", "511 SHERWOOD DR, AMES, IA, 50010"),
    # what the stricter name rule (review B1-3) must keep accepting
    ("100 N ST NW", "Washington", "DC", "20001", "100 N ST NW, WASHINGTON, DC, 20001"),  # street "N"
    ("420 N UNION", "Stafford", "KS", "67578", "420 N UNION, STAFFORD, KS, 67578"),
    ("17 175th Stw", "Taylor Ridge", "IL", "61284", "17 175TH ST W, TAYLOR RIDGE, IL, 61284"),
    ("50 LAKE CIR DR", "Portsmouth", "VA", "23703", "50 LAKE CIR, PORTSMOUTH, VA, 23703"),
    ("20 Ninth Ave Hope Garden", "Morgantown", "WV", "26508", "20 9TH AVE, MORGANTOWN, WV, 26508"),
    ("10 DOCKSIDE LA", "Ames", "IA", "50010", "10 DOCKSIDE LN, AMES, IA, 50010"),  # FCC abbreviations
    ("1 ALILAH CRT", "Ames", "IA", "50010", "1 ALILAH CT, AMES, IA, 50010"),
    ("48 EASTON TPK", "Lake Ariel", "PA", "18436", "48 EASTON TPKE, LAKE ARIEL, PA, 18436"),
    ("99 RIVERDALE SP 9", "Ames", "IA", "50010", "99 RIVERDALE RD, AMES, IA, 50010"),
    ("5 OAK HILL RD", "Ames", "IA", "50010", "5 OAK HL, AMES, IA, 50010"),
    # review B1-R2-4: the same street, rejected before
    ("45 W SOUTH ST", "Ames", "IA", "50010", "45 W SOUTH ST, AMES, IA, 50010"),
    ("88 BOX ELDER DR", "Ames", "IA", "50010", "88 BOX ELDER DR, AMES, IA, 50010"),
    ("212 NW NORTH SHORE DRIVE", "Ames", "IA", "50010", "212 NW NORTH SHORE DR, AMES, IA, 50010"),
    ("1200 TWENTY-FIRST AVE", "Ames", "IA", "50010", "1200 21ST AVE, AMES, IA, 50010"),
    ("1200 Twenty First Ave", "Ames", "IA", "50010", "1200 21ST AVE, AMES, IA, 50010"),
    ("118 UNIT G UNCLE LEO DR", "Ames", "IA", "50010", "118 UNCLE LEO DR, AMES, IA, 50010"),
    ("15 #2 SLOAN LANE", "Ames", "IA", "50010", "15 SLOAN LN, AMES, IA, 50010"),
    ("1115 NO 11TH", "Ames", "IA", "50010", "1115 N 11TH ST, AMES, IA, 50010"),
    ("131 NO 4090 EAST", "Orem", "UT", "84057", "131 N 4090 E, OREM, UT, 84057"),
    ("5 NO NAME RD", "Ames", "IA", "50010", "5 NO NAME RD, AMES, IA, 50010"),
    ("800 SLIP DOWN ROAD", "Ames", "IA", "50010", "800 SLIP DOWN RD, AMES, IA, 50010"),
    ("202 N MERIDIAN LOT #1", "Ames", "IA", "50010", "202 N MERIDIAN ST, AMES, IA, 50010"),
    ("2333 CHISHOLM PO BX2382", "Ames", "IA", "50010", "2333 CHISHOLM TRL, AMES, IA, 50010"),
    ("5745NW 50TH STREET", "Ames", "IA", "50010", "5745 NW 50TH ST, AMES, IA, 50010"),
    # directionals one side lacks, or moved to the other end, are tolerated
    ("20 W 60TH TER", "Ames", "IA", "50010", "20 60TH TER, AMES, IA, 50010"),
    ("301 WARD ST", "Ames", "IA", "50010", "301 S WARD ST, AMES, IA, 50010"),
    ("56 N WINSTON PARK BLVD unit 2", "Ames", "IA", "50010", "56 WINSTON PARK BLVD N, AMES, IA, 50010"),
    ("17 CHRISTIE DR NE", "Ames", "IA", "50010", "17 CHRISTIE DR, AMES, IA, 50010"),
]
DISAGREE = [
    ("13 Parkview Dr", "New Richmond", "WI", "52761", "13 PARKVIEW CT, RICHMOND, KY, 40475"),  # state
    ("31 W Lake Shore Dr", "Springfield", "IL", "62708", "31 LAKESHORE DR, SPRINGFIELD, MI, 49015"),
    ("3 Wilbar Court", "New Milford", "CT", "06776", "3 WILBAR AVE, MILFORD, CT, 06460"),  # other town
    ("18 happy Ridge dr", "West Columbia", "SC", "29170", "18 S RIDGE DR, WEST COLUMBIA, SC, 29170"),
    ("16 Ridge Pl", "Lahoma", "OK", "73754", "16 EASTRIDGE RD, LAHOMA, OK, 73754"),
    ("70 Northfork Drive", "Petal", "MS", "39465", "70 SOUTHFORK DR, PETAL, MS, 39465"),
    ("21 Parkview Ln", "Ames", "IA", "50010", "21 PARKSIDE LN, AMES, IA, 50010"),
    ("22 Main St", "Ames", "IA", "50010", "220 MAIN ST, AMES, IA, 50010"),  # house number
    ("PO BOX 12", "Ames", "IA", "50010", "12 BOX ST, AMES, IA, 50010"),  # no house number
    ("5 9TH ST", "Ames", "IA", "50010", "5 19TH ST, AMES, IA, 50010"),
    ("8 RT 9", "Ames", "IA", "50010", "8 STATE RTE 90, AMES, IA, 50010"),
    # review B1-3: another street of the same town whose name is a word-prefix
    ("12 OAK HILL RD", "Ames", "IA", "50010", "12 OAK ST, AMES, IA, 50010"),
    ("44 RIVER BEND RD", "Ames", "IA", "50010", "44 RIVER RD, AMES, IA, 50010"),
    ("100 MAPLE LEAF RD", "Ames", "IA", "50010", "100 MAPLE AVE, AMES, IA, 50010"),
    ("5 LAKE SHORE DR", "Ames", "IA", "50010", "5 LAKE ST, AMES, IA, 50010"),
    ("7 PINE TREE LN", "Ames", "IA", "50010", "7 PINE ST, AMES, IA, 50010"),
    ("9 CHURCH HILL RD", "Ames", "IA", "50010", "9 CHURCH ST, AMES, IA, 50010"),
    ("300 MILL POND LN", "Ames", "IA", "50010", "300 MILL ST, AMES, IA, 50010"),
    ("15 SUNSET VIEW DR", "Ames", "IA", "50010", "15 SUNSET DR, AMES, IA, 50010"),
    ("21 COUNTRY CLUB RD", "Ames", "IA", "50010", "21 COUNTRY RD, AMES, IA, 50010"),
    ("8 MOUNTAIN VIEW DR", "Ames", "IA", "50010", "8 MOUNTAIN DR, AMES, IA, 50010"),  # fuzzy path
    ("16 HOPEWELL CHURCH RD", "Ames", "IA", "50010", "16 HOPEWELL RD, AMES, IA, 50010"),
    ("17 COUNTY RD 5", "Ames", "IA", "50010", "17 COUNTY RD 50, AMES, IA, 50010"),  # numbers in names
    ("614 C AVE", "Ames", "IA", "50010", "614 E AVE, AMES, IA, 50010"),
    ("3 EASTLAWN AVE", "Ames", "IA", "50010", "3 EASTLAND AVE, AMES, IA, 50010"),
    # what the B1-4 relaxations must keep rejecting
    ("6 E 603RD LN", "Ames", "IA", "50010", "6 EAST ST, AMES, IA, 50010"),
    ("5 EASTER LN", "Ames", "IA", "50010", "5 EAST LN, AMES, IA, 50010"),
    ("BOX 161 ST CLAIR RD", "Ames", "IA", "50010", "161 SAINT CLAIR RD, AMES, IA, 50010"),
    # review B1-R2-2: Census's name is a word-prefix of the input's
    ("12 MAPLE LEAF DR", "Ames", "IA", "50010", "12 MAPLE DR, AMES, IA, 50010"),
    ("100 CEDAR CREEK RD", "Ames", "IA", "50010", "100 CEDAR RD, AMES, IA, 50010"),
    ("500 LAKE SHORE DR", "Ames", "IA", "50010", "500 LAKE DR, AMES, IA, 50010"),
    ("77 PARK VIEW DR", "Ames", "IA", "50010", "77 PARK AVE, AMES, IA, 50010"),
    ("100 COUNTY ROAD 5", "Ames", "IA", "50010", "100 COUNTY RD 50, AMES, IA, 50010"),
    # review B1-R2-9: different directionals name different streets
    ("1500 N MAIN ST", "Ames", "IA", "50010", "1500 S MAIN ST, AMES, IA, 50010"),
    ("1500 NORTH MAIN ST", "Ames", "IA", "50010", "1500 S MAIN ST, AMES, IA, 50010"),
    ("200 NE 5TH AVE", "Ames", "IA", "50010", "200 NW 5TH AVE, AMES, IA, 50010"),
    ("100 MAIN ST NW", "Ames", "IA", "50010", "100 MAIN ST NE, AMES, IA, 50010"),
    ("100 N MAIN ST", "Ames", "IA", "50010", "100 MAIN ST S, AMES, IA, 50010"),
    ("13 175th Stw", "Ames", "IA", "50010", "13 175TH ST E, AMES, IA, 50010"),
    ("44 W HWY 33", "Ames", "IA", "50010", "44 E STATE HWY 33, AMES, IA, 50010"),
    ("1429 NO ATLANTIC AVE", "Ames", "IA", "50010", "1429 S ATLANTIC AVE, AMES, IA, 50010"),
]


@pytest.mark.parametrize("street,city,state,zip_,matched", AGREE)
def test_matched_address_agrees(street, city, state, zip_, matched):
    assert matched_address_agrees(street, city, state, zip_, matched)


@pytest.mark.parametrize("street,city,state,zip_,matched", DISAGREE)
def test_matched_address_disagrees(street, city, state, zip_, matched):
    assert not matched_address_agrees(street, city, state, zip_, matched)


def test_unparsable_matched_address_is_not_evidence_against():
    assert matched_address_agrees("1 MAIN ST", "X", "CT", "06111", "garbage")


# every way an FCC record spells one street (prefix, directional, type, unit)
_PREFIXES = ["", "C/O J DOE ", "PO BOX 12, ", "ATTN RECEIVING "]
_PRE = [(None, [""]), ("N", ["N ", "N. ", "NORTH ", "NO "]), ("SW", ["SW ", "S.W. ", "SOUTHWEST "])]
_NAMES = ["MAPLE", "NORTH", "BOX ELDER", "OAK HILL", "WEST END", "21ST", "MARTIN LUTHER KING JR"]
_TYPES = [("ST", ["ST", "STREET", "ST."]), ("AVE", ["AVE", "AVENUE", "AV"]), ("RD", ["RD", "ROAD"]),
          ("DR", ["DR", "DRIVE"])]
_POST = [(None, [""]), ("NW", [" NW", " N.W.", " NORTHWEST"])]
_UNITS = ["", " APT 4", " #12", " UNIT B", " LOT 7", " SP 9", " SUITE 200"]
_OPPOSITE = {"N": "S", "SW": "NE", "NW": "SE"}


def _census(number, pre, name, typ, post):
    """Census's matched address: house number, USPS-abbreviated
    directionals and street type, no unit."""
    return " ".join(x for x in (number, pre, name, typ, post) if x) + ", AMES, IA, 50010"


def test_any_fcc_spelling_of_the_same_street_agrees_and_another_street_does_not():
    """Regression (review B1-R2-2/-4/-9), checked over every combination
    instead of hand-picked cases: Census's normalized answer for the same
    street is accepted whatever the FCC spelling; another house number, a
    flipped pre- or post-directional, an added or dropped name word or a
    changed street number is not. The old rule accepted 'MAPLE LEAF' for
    'MAPLE' and 'S MAIN' for 'N MAIN', and rejected 'N.W.' post-directionals,
    'BOX ELDER', 'NORTH' (the name) and care-of/PO-box prefixes."""
    checked = 0
    for i, (prefix, (pre, pres), name, (typ, typs), (post, posts), unit) in enumerate(
            itertools.product(_PREFIXES, _PRE, _NAMES, _TYPES, _POST, _UNITS)):
        # one spelling of each part per combination, rotating through them all
        inp = f"{prefix}1234 {pres[i % len(pres)]}{name} {typs[i % len(typs)]}" \
              f"{posts[i % len(posts)]}{unit}"
        assert matched_address_agrees(inp, "Ames", "IA", "50010",
                                      _census("1234", pre, name, typ, post)), inp
        others = [_census("1236", pre, name, typ, post), _census("1234", pre, name + " LEAF", typ, post)]
        if pre:
            others.append(_census("1234", _OPPOSITE[pre], name, typ, post))
        if post:
            others.append(_census("1234", pre, name, typ, _OPPOSITE[post]))
        if " " in name and not name.endswith(" JR"):  # "MLK" for "MLK JR" is the same street
            others.append(_census("1234", pre, name.rsplit(" ", 1)[0], typ, post))
        if name[0].isdigit():
            others.append(_census("1234", pre, "1" + name, typ, post))
        for other in others:
            assert not matched_address_agrees(inp, "Ames", "IA", "50010", other), (inp, other)
        checked += 1
    assert checked == 4 * 3 * 7 * 4 * 2 * 7


@pytest.mark.skipif(not os.environ.get("HAMDOOR_REPLICA_DB"),
                    reason="set HAMDOOR_REPLICA_DB to a copy of a real hamdoor.db")
def test_replica_non_exact_inputs_agree_with_their_own_street():
    """Identity check over a real database's Non_Exact inputs (review
    B1-R2-4): with Census's answer = the input's own street, only lines
    without a house number (PO/rural boxes, Puerto Rico urbanizations) and
    odd forms may be rejected. Measured on the replica: 489 of 103,185
    (0.47%; the old rule 2.8%)."""
    conn = sqlite3.connect(f"file:{os.environ['HAMDOOR_REPLICA_DB']}?mode=ro", uri=True)
    keys = [r[0] for r in conn.execute("SELECT address_key FROM geocodes WHERE quality = 'Non_Exact'")]
    rejected = 0
    for key in keys:
        street, city, state, zip_ = split_address_key(key)
        clean_city = re.sub(r"[^A-Z ]", "", city.split(",")[0]).strip() or "X"  # Census's own city
        rejected += not matched_address_agrees(street, city, state, zip_,
                                               f"{street}, {clean_city}, {state}, {zip_}")
    assert keys and rejected / len(keys) < 0.01


# ---------------------------------------------------------------- callook


W1AW_JSON = {
    "status": "VALID", "type": "CLUB", "current": {"callsign": "W1AW", "operClass": ""},
    "name": "ARRL HQ OPERATORS CLUB",
    "address": {"line1": "225 MAIN ST", "line2": "NEWINGTON, CT 06111"},
    "location": {"latitude": "41.714707", "longitude": "-72.728411", "gridsquare": "FN31pr"},
    "otherInfo": {"expiryDate": "02/26/2031"},
}


def test_callook_parses_coordinates():
    rec = CallookProvider._parse("w1aw", W1AW_JSON)
    assert (rec.lat, rec.lon) == (41.714707, -72.728411)
    assert rec.grid == "FN31pr" and rec.zip == "06111"
    blank = dict(W1AW_JSON, location={"latitude": "", "longitude": "", "gridsquare": ""})
    rec = CallookProvider._parse("w1aw", blank)
    assert rec.lat is None and rec.lon is None
    half = dict(W1AW_JSON, location={"latitude": "41.7", "longitude": "nan", "gridsquare": ""})
    assert CallookProvider._parse("w1aw", half).lat is None


def test_callook_capitals_are_stored_in_normal_case():
    """QA F6: callook writes names and addresses in capitals; sign-up and
    Update from FCC stored them verbatim ("NEWINGTON, Connecticut" on the QSL
    card, "ARRL HQ OPERATORS CLUB" in Nearby)."""
    rec = CallookProvider._parse("w1aw", W1AW_JSON)
    assert (rec.name, rec.address_line, rec.city) == ("ARRL HQ Operators Club", "225 Main St", "Newington")
    assert (rec.state, rec.zip, rec.grid) == ("CT", "06111", "FN31pr")
    mixed = dict(W1AW_JSON, name="Hiram Percy Maxim Memorial Station",
                 address={"line1": "225 Main St", "line2": "Newington, CT 06111"})
    rec = CallookProvider._parse("w1aw", mixed)
    assert (rec.name, rec.city) == ("Hiram Percy Maxim Memorial Station", "Newington")


def test_callook_lookup_reads_location_over_http():
    def handler(request):
        assert request.url.path == "/W1AW/json"
        return httpx.Response(200, json=W1AW_JSON)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://callook.info")
    rec = CallookProvider(client=client).lookup("W1AW")
    assert (rec.lat, rec.lon) == (41.714707, -72.728411)


# ---------------------------------------------------------------- resolve_user_location


def _put_ham(db, callsign, street, city, state, zip_, lat, lon, loc):
    db.query(Ham).filter_by(callsign=callsign).delete()
    db.add(Ham(callsign=callsign, name="X", street=street, city=city, state=state, zip=zip_,
               lat=lat, lon=lon, loc_source=loc))
    db.commit()


def _put_anchor(zip_, lat, lon):
    uls.migrate_hams_table()
    with engine.begin() as conn:
        conn.execute(text("INSERT OR REPLACE INTO zip_anchors VALUES (:z, :lat, :lon, 'median', 5)"),
                     {"z": zip_, "lat": lat, "lon": lon})


def test_resolve_street_pin_only_for_the_same_address(db, monkeypatch):
    """Regression: the directory's street pin was used whatever the address
    — a recycled callsign put the new holder at the old holder's house."""
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    _put_ham(db, "K9RES", "5 OLD RD", "Le Sueur", "MN", "56058", 44.46, -93.91, "address")
    _put_anchor("81501", 39.07, -108.55)
    same = geocode.resolve_user_location(db, "K9RES", "", "5 old rd", "LE SUEUR", "MN", "56058-1234")
    assert same == (44.46, -93.91)
    other = geocode.resolve_user_location(db, "K9RES", "", "7 New St", "Grand Junction", "CO", "81501")
    assert other != (44.46, -93.91)
    assert other == (39.07, -108.55)  # no provider coords, grid or Nominatim: the user's ZIP anchor


def test_resolve_order_provider_grid_nominatim_anchor(db, monkeypatch):
    _put_ham(db, "K9ORD", "1 ELM ST", "Ames", "IA", "50010", 42.02, -93.62, "zip")
    _put_anchor("50010", 42.02, -93.62)
    rec = CallsignRecord(callsign="K9ORD", address_line="1 ELM ST", city="AMES", state="IA",
                         zip="50010", grid="EN32ba", lat=42.0311, lon=-93.6319)
    nominatim = []
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: nominatim.append(a) or (42.1, -93.5))
    args = ("K9ORD", "EN32ba", "1 ELM ST", "Ames", "IA", "50010")
    assert geocode.resolve_user_location(db, *args, record=rec) == (42.0311, -93.6319)  # (2)
    grid = geocode.resolve_user_location(db, *args)                                    # (3)
    assert grid == pytest.approx((42.0208, -93.875), abs=1e-3)
    assert geocode.resolve_user_location(db, "K9ORD", "", "1 ELM ST", "Ames", "IA", "50010") == (42.1, -93.5)
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    assert geocode.resolve_user_location(db, "K9ORD", "", "1 ELM ST", "Ames", "IA", "50010") == (42.02, -93.62)
    assert len(nominatim) == 1  # Nominatim only after provider and grid had nothing


def test_signup_uses_provider_coordinates(client, db):
    """callook's Bing coordinates (~10 m) beat the grid-square centre (~2 km)."""

    class CoordProvider(CallsignProvider):
        def lookup(self, callsign):
            return CallsignRecord(callsign="K9BNG", name="Bing Test", address_line="3 Oak St",
                                  city="Ames", state="IA", zip="50010", grid="EN32ba",
                                  lat=42.0311, lon=-93.6319, source="fake")

    db.query(User).filter_by(callsign="K9BNG").delete()
    db.commit()
    previous = app.dependency_overrides[get_provider]
    app.dependency_overrides[get_provider] = lambda: CoordProvider()
    try:
        r = client.post("/api/auth/signup", json={"callsign": "K9BNG", "password": "password123",
                                                  "email": "k9bng@example.com"})
    finally:
        app.dependency_overrides[get_provider] = previous
    assert r.status_code == 201, r.text
    assert (r.json()["user"]["lat"], r.json()["user"]["lon"]) == (42.0311, -93.6319)


# ---------------------------------------------------------------- sync_user_locations


def _put_user(db, callsign, street, city, state, zip_, lat, lon, grid=""):
    u = db.query(User).filter_by(callsign=callsign).one_or_none()
    if u is None:
        u = User(callsign=callsign, password_hash=hash_password("password123"),
                 email=f"{callsign.lower()}@example.com")
        db.add(u)
    u.address_line, u.city, u.state, u.zip, u.grid = street, city, state, zip_, grid
    u.lat, u.lon = lat, lon
    db.commit()
    return u


def test_sync_moves_user_off_previous_holders_pin(db):
    """Users who signed up while their callsign carried the previous
    holder's address sit hundreds of km away; once the directory row is
    right (and still only ZIP-level) they move back to their own area."""
    _put_ham(db, "K9SYA", "7 NEW ST", "Grand Junction", "CO", "81501", 39.07, -108.55, "zip")
    _put_anchor("81501", 39.07, -108.55)
    _put_user(db, "K9SYA", "7 New St", "Grand Junction", "CO", "81501", 44.46, -93.91, grid="DM59ab")
    assert geocode.sync_user_locations(db) >= 1
    u = db.query(User).filter_by(callsign="K9SYA").one()
    # the profile grid DM59ab is ~75 mi from the ZIP anchor: the grid centre
    # (3) still beats the anchor (5), as in resolve_user_location (review B1-2)
    assert (u.lat, u.lon) == pytest.approx((39.0625, -109.9583), abs=1e-3)
    assert geocode.sync_user_locations(db) == 0  # and it stays there
    # with a grid square inside the ZIP area, the grid centre is used
    _put_user(db, "K9SYA", "7 New St", "Grand Junction", "CO", "81501", 44.46, -93.91, grid="DM59rb")
    geocode.sync_user_locations(db)
    u = db.query(User).filter_by(callsign="K9SYA").one()
    assert (u.lat, u.lon) == pytest.approx((39.0625, -108.5417), abs=1e-3)
    # no grid at all: the ZIP anchor
    _put_user(db, "K9SYA", "7 New St", "Grand Junction", "CO", "81501", 44.46, -93.91, grid="")
    geocode.sync_user_locations(db)
    u = db.query(User).filter_by(callsign="K9SYA").one()
    assert (u.lat, u.lon) == (39.07, -108.55)


def test_sync_keeps_a_zip_level_users_own_location_far_from_the_anchor(db, monkeypatch):
    """Regression (review B1-2): a ZIP-level user (PO box, or a typo'd FCC
    ZIP) whose grid / provider location is more than 30 mi from the FCC ZIP
    anchor was moved to the anchor by every post-import sync, overriding
    resolve_user_location's order (grid before ZIP anchor)."""
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    _put_ham(db, "K7SYF", "PO BOX 12", "Sheridan", "WY", "82801", 44.8, -106.95, "zip")
    _put_anchor("82801", 44.8, -106.95)
    # PATCH /api/me {"grid": "DN64bt"} stores the grid centre, ~45 mi from the anchor
    u = _put_user(db, "K7SYF", "PO BOX 12", "Sheridan", "WY", "82801", None, None, grid="DN64bt")
    u.lat, u.lon = geocode.resolve_user_location(db, u.callsign, u.grid, u.address_line,
                                                 u.city, u.state, u.zip)
    db.commit()
    grid_centre = (u.lat, u.lon)
    assert grid_centre == pytest.approx((44.8125, -107.875))
    # callook coordinates inside a 4-character grid square, >30 mi from both
    # the square's centre and the anchor
    _put_ham(db, "K7SYG", "PO BOX 13", "Sheridan", "WY", "82801", 44.8, -106.95, "zip")
    _put_user(db, "K7SYG", "PO BOX 13", "Sheridan", "WY", "82801", 44.35, -107.85, grid="DN64")
    # no location yet, grid far from the anchor: the grid centre, not the anchor
    _put_ham(db, "K7SYH", "PO BOX 14", "Sheridan", "WY", "82801", 44.8, -106.95, "zip")
    _put_user(db, "K7SYH", "PO BOX 14", "Sheridan", "WY", "82801", None, None, grid="DN64bt")
    for _ in range(2):  # every import runs the sync
        geocode.sync_user_locations(db)
        db.expire_all()
        assert (db.query(User).filter_by(callsign="K7SYF").one().lat,
                db.query(User).filter_by(callsign="K7SYF").one().lon) == grid_centre  # old: anchor
        g = db.query(User).filter_by(callsign="K7SYG").one()
        assert (g.lat, g.lon) == (44.35, -107.85)
        h = db.query(User).filter_by(callsign="K7SYH").one()
        assert (h.lat, h.lon) == pytest.approx((44.8125, -107.875))  # old: the anchor


def test_sync_snaps_to_street_pin_and_respects_edited_addresses(db):
    _put_ham(db, "K9SYB", "4 PINE RD", "Ames", "IA", "50010", 42.0301, -93.6101, "address")
    _put_ham(db, "K9SYC", "5 PINE RD", "Ames", "IA", "50010", 42.0302, -93.6102, "address")
    _put_user(db, "K9SYB", "4 Pine Rd", "AMES", "IA", "50010", 42.0, -93.6)
    _put_user(db, "K9SYC", "99 Elsewhere Ave", "Ames", "IA", "50010", 42.5, -93.5)  # moved
    geocode.sync_user_locations(db)
    assert (db.query(User).filter_by(callsign="K9SYB").one().lat) == 42.0301
    assert (db.query(User).filter_by(callsign="K9SYC").one().lat) == 42.5


def test_sync_keeps_precise_provider_location_and_places_unplaced_users(db):
    _put_ham(db, "K9SYD", "6 PINE RD", "Ames", "IA", "50010", 42.02, -93.62, "zip")
    _put_ham(db, "K9SYE", "7 PINE RD", "Ames", "IA", "50010", 42.02, -93.62, "zip")
    _put_anchor("50010", 42.02, -93.62)
    _put_user(db, "K9SYD", "6 Pine Rd", "Ames", "IA", "50010", 42.0311, -93.6319)  # callook, 1.5 km
    _put_user(db, "K9SYE", "7 Pine Rd", "Ames", "IA", "50010", None, None)
    geocode.sync_user_locations(db)
    assert db.query(User).filter_by(callsign="K9SYD").one().lat == 42.0311
    e = db.query(User).filter_by(callsign="K9SYE").one()
    assert (e.lat, e.lon) == (42.02, -93.62)
    assert geocode.sync_user_locations(db) == 0  # idempotent


# ---------------------------------------------------------------- provider coordinates (review B1-R2-3)


class _CallookLike(CallsignProvider):
    """callook's answer for a ZIP-level licensee: coordinates of the FCC
    licence address (~10 m) and the grid that contains them."""

    def __init__(self, callsign):
        self.callsign = callsign

    def lookup(self, callsign):
        return CallsignRecord(callsign=self.callsign, name="Ed", address_line="3 Oak St", city="Ames",
                              state="IA", zip="50010", grid="EN32ea", lat=42.0311, lon=-93.6319,
                              source="fake")


def _signup_with(client, db, callsign, **fields):
    db.query(User).filter_by(callsign=callsign).delete()
    db.commit()
    previous = app.dependency_overrides[get_provider]
    app.dependency_overrides[get_provider] = lambda: _CallookLike(callsign)
    try:
        r = client.post("/api/auth/signup", json={"callsign": callsign, "password": "password123",
                                                  "email": f"{callsign.lower()}@example.com", **fields})
    finally:
        app.dependency_overrides[get_provider] = previous
    assert r.status_code == 201, r.text
    return r.json()["user"]


def test_signup_with_another_address_is_not_pinned_at_the_licence_address(client, db, monkeypatch):
    """Regression (review B1-R2-3): resolve step 2 used the provider's exact
    coordinates of the FCC licence address whatever the user entered, so a
    member who signed up with another town (or grid) was shown to other
    members ~10 m from the address they chose not to use."""
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    # another town and an explicit 4-character grid: the grid wins
    u = _signup_with(client, db, "K9EDA", city="Des Moines", zip="50309", grid="EN31")
    assert (u["lat"], u["lon"]) == grid_to_latlon("EN31")          # old code: (42.0311, -93.6319)
    # another street, the provider's grid kept: that grid's centre, not the point
    u = _signup_with(client, db, "K9EDB", address_line="77 Elm St", grid="EN32ea")
    assert (u["lat"], u["lon"]) == grid_to_latlon("EN32ea")
    # the same address, an explicit grid of their own: the grid
    u = _signup_with(client, db, "K9EDC", grid="EN41")
    assert (u["lat"], u["lon"]) == grid_to_latlon("EN41")
    # the provider's own address and grid (autofilled, in any case/spacing): its coordinates
    u = _signup_with(client, db, "K9EDD", address_line="3 OAK ST.", city="AMES", grid="en32EA")
    assert (u["lat"], u["lon"]) == (42.0311, -93.6319)
    u = _signup_with(client, db, "K9EDE")
    assert (u["lat"], u["lon"]) == (42.0311, -93.6319)


def test_refresh_callsign_still_uses_the_provider_coordinates(client, db, monkeypatch):
    """refresh-callsign copies the provider's address and grid into the
    profile, so its coordinates still apply (multi-call: signup elsewhere,
    then refresh)."""
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    _signup_with(client, db, "K9EDF", city="Des Moines", zip="50309", grid="")
    u = db.query(User).filter_by(callsign="K9EDF").one()
    assert (u.lat, u.lon) != (42.0311, -93.6319)
    previous = app.dependency_overrides[get_provider]
    app.dependency_overrides[get_provider] = lambda: _CallookLike("K9EDF")
    try:
        r = client.post("/api/me/refresh-callsign", headers=auth(create_token(u)))
    finally:
        app.dependency_overrides[get_provider] = previous
    assert r.status_code == 200, r.text
    assert (r.json()["lat"], r.json()["lon"]) == (42.0311, -93.6319)


# ---------------------------------------------------------------- PO-box licensees (review B1-R2-5)


def test_sync_repairs_po_box_licensees(db):
    """Regression (review B1-R2-5): the FCC row of a PO-box licensee has no
    street line (the importer keeps EN street_address; the box is in
    another field) while callook's profile says "P.O. BOX 132", so every
    such member (48% of ZIP-level hams) counted as having edited their
    address: a previous holder's pin was never repaired, and members with
    no location were never placed."""
    _put_ham(db, "KI5PBA", "", "Decatur", "MS", "39327", 32.44, -89.11, "zip")
    _put_ham(db, "KI5PBB", "", "Decatur", "MS", "39327", 32.44, -89.11, "zip")
    _put_ham(db, "KI5PBC", "", "Decatur", "MS", "39327", 32.44, -89.11, "zip")
    _put_anchor("39327", 32.44, -89.11)
    _put_user(db, "KI5PBA", "P.O. BOX 132", "DECATUR", "MS", "39327", 44.46, -93.91)  # stale pin
    _put_user(db, "KI5PBB", "PO Box 7", "Decatur", "MS", "39327-0007", None, None)    # never placed
    _put_user(db, "KI5PBC", "P.O. BOX 9", "Jackson", "MS", "39201", 44.46, -93.91)    # moved away
    for _ in range(2):  # every import runs the sync; the second moves nobody
        moved = geocode.sync_user_locations(db)
        db.expire_all()
        assert (db.query(User).filter_by(callsign="KI5PBA").one().lat,
                db.query(User).filter_by(callsign="KI5PBA").one().lon) == (32.44, -89.11)  # old: stale
        assert db.query(User).filter_by(callsign="KI5PBB").one().lat == 32.44                 # old: None
        assert db.query(User).filter_by(callsign="KI5PBC").one().lat == 44.46  # an edited town: kept
    assert moved == 0


def test_profile_is_fcc_address_ignores_case_spacing_and_punctuation(db):
    ham = Ham(callsign="K9PUN", street="5 OLD RD", city="Le Sueur", state="MN", zip="56058")
    same = User(address_line="5 Old Rd.", city="LE  SUEUR", state="mn", zip="56058-1234")
    edited = User(address_line="7 Old Rd", city="Le Sueur", state="MN", zip="56058")
    assert geocode.profile_is_fcc_address(same, ham)
    assert not geocode.profile_is_fcc_address(edited, ham)
    box = Ham(callsign="K9PUN", street="", city="Decatur", state="MS", zip="39327")
    assert geocode.profile_is_fcc_address(User(address_line="P.O. Box 1", city="Decatur", state="MS",
                                               zip="39327"), box)
    assert not geocode.profile_is_fcc_address(User(address_line="", city="Newton", state="MS",
                                                   zip="39345"), box)


# ---------------------------------------------------------------- sync keeps a member's own location (review B1-R2-7)


def test_sync_keeps_provider_location_far_from_a_misplaced_zip_anchor(db):
    """Regression (review B1-R2-7): a member at callook's coordinates of
    their home, ~48 mi from their (large, rural) ZIP's anchor, was moved
    onto the anchor by every import — against the resolve order (provider
    > grid > anchor), and back again by every refresh-callsign."""
    _put_ham(db, "K7FAR", "4410 RANCH RD 12", "Decatur", "MS", "59011", 46.1, -109.0, "zip")
    _put_anchor("59011", 46.1, -109.0)
    _put_user(db, "K7FAR", "4410 Ranch Rd 12", "Decatur", "MS", "59011", 45.5, -109.5, grid="DN55gm")
    for _ in range(2):
        geocode.sync_user_locations(db)
        db.expire_all()
        u = db.query(User).filter_by(callsign="K7FAR").one()
        assert (u.lat, u.lon) == (45.5, -109.5)  # old code: (46.1, -109.0)


# ---------------------------------------------------------------- maidenhead helpers


@pytest.mark.parametrize("lat,lon,length,expected", [
    (41.714707, -72.728411, 6, "FN31pr"),      # W1AW
    (41.714707, -72.728411, 4, "FN31"),
    (41.714707, -72.728411, 8, "FN31pr21"),
    (-33.8688, 151.2093, 6, "QF56od"),         # Sydney
    (90.0, 180.0, 6, "RR99xx"),                # the pole and the antimeridian: the last square
    (-90.0, -180.0, 6, "AA00aa"),
    (95.0, -190.0, 4, "AR09"),                 # out of range: clamped
])
def test_latlon_to_grid(lat, lon, length, expected):
    assert geocode.latlon_to_grid(lat, lon, length) == expected


def test_latlon_to_grid_round_trips_with_grid_to_latlon():
    """grid_to_latlon(latlon_to_grid(p)) is the centre of the square that
    holds p, and that centre maps back to the same locator."""
    import random

    rng = random.Random(42)
    half = {4: (0.5, 1.0), 6: (2.5 / 120, 5 / 120), 8: (2.5 / 1200, 5 / 1200)}
    for _ in range(3000):
        lat, lon = rng.uniform(-90, 90), rng.uniform(-180, 180)
        for length, (hlat, hlon) in half.items():
            g = geocode.latlon_to_grid(lat, lon, length)
            centre = grid_to_latlon(g)
            assert centre is not None and geocode.latlon_to_grid(*centre, length) == g
            assert abs(centre[0] - lat) <= hlat + 1e-9 and abs(centre[1] - lon) <= hlon + 1e-9
    with pytest.raises(ValueError):
        geocode.latlon_to_grid(1, 1, 5)


@pytest.mark.parametrize("grid,lat,lon,expected", [
    ("FN31pr", None, None, "FN31pr"),
    ("fn31PR12", 1.0, 1.0, "FN31pr"),          # 8 characters: never more precise than 6
    ("", 41.714707, -72.728411, "FN31pr"),     # blank: from the stored location
    ("FN31", 41.714707, -72.728411, "FN31pr"),  # 4 characters: refined only inside the square
    ("FN32", 41.714707, -72.728411, "FN32"),   # location elsewhere: the square, coarser still
    ("FN31zz", 41.714707, -72.728411, "FN31pr"),  # invalid subsquare: the valid square
    ("nonsense", 41.714707, -72.728411, "FN31pr"),
    ("", None, None, None),
])
def test_member_subsquare(grid, lat, lon, expected):
    assert geocode.member_subsquare(grid, lat, lon) == expected


# ---------------------------------------------------------------- /api/operators/nearby and map precision


def _member_at(db, callsign, lat, lon, grid="", precision="address"):
    u = _put_user(db, callsign, "1 X ST", "Ames", "IA", "50010", lat, lon, grid=grid)
    u.map_precision = precision
    db.commit()
    return u


def _nearby(client, viewer, **params):
    r = client.get("/api/operators/nearby", params=params, headers=auth(create_token(viewer)))
    assert r.status_code == 200, r.text
    return {o["callsign"]: o for o in r.json()}, r.text


def test_nearby_shows_grid_members_at_their_subsquare_centre(client, db):
    viewer = _member_at(db, "K0VWR", 42.0, -93.6)
    _member_at(db, "K0EXA", 42.0311, -93.6319, grid="EN32ea")                      # default
    _member_at(db, "K0GR8", 42.0312, -93.6318, grid="EN32ea12", precision="grid")
    _member_at(db, "K0GRB", 42.0313, -93.6317, grid="", precision="grid")
    _member_at(db, "K0GR4", 42.0314, -93.6316, grid="EN32", precision="grid")
    got, body = _nearby(client, viewer, range=50)
    exact = got["K0EXA"]
    assert (exact["lat"], exact["lon"], exact["grid"]) == (42.0311, -93.6319, "EN32ea")  # as today
    assert exact["distance_miles"] == round(haversine_miles(42.0, -93.6, 42.0311, -93.6319), 1)
    centre = grid_to_latlon("EN32ea")
    for call in ("K0GR8", "K0GRB", "K0GR4"):
        o = got[call]
        assert (o["lat"], o["lon"]) == centre and o["grid"] == "EN32ea"
        assert o["distance_miles"] == round(haversine_miles(42.0, -93.6, *centre))  # whole miles
    for exact_point in ("42.0312", "42.0313", "42.0314", "-93.6318", "-93.6317", "-93.6316"):
        assert exact_point not in body
    assert list(got) == sorted(got, key=lambda c: got[c]["distance_miles"])


def test_nearby_judges_grid_members_by_the_point_it_shows(client, db):
    """A 'grid' member is in range — and at a distance — by their subsquare
    centre only: with the exact point behind either, `range` (a float) could
    be bisected from three spots to recover the point."""
    viewer = _member_at(db, "K0VWS", 42.05, -93.575)
    _member_at(db, "K0GRC", 42.0400, -93.5840, grid="", precision="grid")  # 0.8 mi away, in EN32ea
    _member_at(db, "K0EXC", 42.0400, -93.5840, grid="")                     # same point, 'address'
    got, _ = _nearby(client, viewer, range=1)
    assert "K0EXC" in got and "K0GRC" not in got   # the shown centre is ~3.3 mi away
    got, _ = _nearby(client, viewer, range=4)
    assert got["K0GRC"]["distance_miles"] == 3.0
    # a 'grid' member whose stored grid is far from their stored point is
    # found (and shown) at the grid, not missed by a prefilter on the point
    _member_at(db, "K0GRF", 42.0400, -93.5840, grid="FN31pr", precision="grid")
    far = _member_at(db, "K0VWF", 41.70, -72.70)
    got, _ = _nearby(client, far, range=5)
    assert (got["K0GRF"]["lat"], got["K0GRF"]["lon"]) == grid_to_latlon("FN31pr")
    assert "K0GRF" not in _nearby(client, viewer, range=4)[0]


def test_switching_back_to_address_restores_the_exact_point(client, db):
    viewer = _member_at(db, "K0VWT", 42.0, -93.6)
    member = _member_at(db, "K0SWT", 42.0311, -93.6319, grid="EN32ea", precision="grid")
    tok = create_token(member)
    assert client.patch("/api/me", headers=auth(tok), json={"map_precision": "address"}).status_code == 200
    got, _ = _nearby(client, viewer, range=50)
    assert (got["K0SWT"]["lat"], got["K0SWT"]["lon"]) == (42.0311, -93.6319)
    assert client.patch("/api/me", headers=auth(tok), json={"map_precision": "grid"}).status_code == 200
    got, _ = _nearby(client, viewer, range=50)
    assert (got["K0SWT"]["lat"], got["K0SWT"]["lon"]) == grid_to_latlon("EN32ea")
    db.expire_all()
    assert (db.query(User).filter_by(callsign="K0SWT").one().lat) == 42.0311  # stored point kept
