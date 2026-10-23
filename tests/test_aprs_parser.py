"""APRS parser: the 50 real-packet vectors from the research phase, plus
regression tests for every way the old parser (app.integrations.aprsis
.parse_aprs_line, removed) put pins in the wrong place or missed them.

No network: all packets are captured APRS-IS lines (public radio traffic).
"""

import base64
import json
import math
from pathlib import Path

import pytest

from app.integrations import aprs_parser
from app.integrations.aprs_parser import device_for_tocall, frequency_mhz, parse_line

FIXTURES = Path(__file__).parent / "fixtures"
VECTORS = json.loads((FIXTURES / "aprs_vectors.json").read_text())

# Vectors whose packets carry no map position.
NO_POSITION = {"v40", "v42", "v43", "v48", "v49", "v50"}

# Where hamdoor goes beyond both aprslib ("expected") and the research's
# expected_spec. Both read weather fields the way Ham::APRS::FAP (aprs.fi) does:
OVERRIDES = {
    # "rpPh99b10187": empty r/p/P fields are skipped, so humidity 99 and
    # pressure 1018.7 are read instead of all of it landing in the comment
    "v38_weather_position": {"comment": "NodeRED"},
    # "h066b10102": 3-digit humidity (66 %) then pressure, instead of h06 + "6b10102"
    "v41_third_party": {"comment": ""},
}

MPH_KMH = 1.609344


def _vid(v):
    return v["id"].split("_", 1)[0]


def _parsed(v):
    return parse_line(base64.b64decode(v["raw_b64"]))


@pytest.mark.parametrize("v", VECTORS, ids=[v["id"] for v in VECTORS])
def test_vector(v):
    p = _parsed(v)
    exp, spec = v["expected"], v.get("expected_spec") or {}
    want = {**exp, **spec, **OVERRIDES.get(v["id"], {})}
    vid = _vid(v)

    if vid in ("v43", "v48", "v49", "v50"):  # message / malformed: nothing for the map
        assert p is None
        return
    assert p is not None, v["raw"]
    if vid in NO_POSITION:
        assert p["lat"] is None and p["lon"] is None
        if vid == "v42":
            assert p["kind"] == "status" and p["status"] == exp["comment"]
        if vid == "v40":
            assert p["kind"] == "weather" and p["comment"] == exp["comment"]
            wx = v["aprslib_extra"]["weather"]
            assert p["wx"]["wind_dir"] == wx["wind_direction"]
            assert p["wx"]["humidity"] == wx["humidity"]
            assert p["wx"]["temp_c"] == pytest.approx(wx["temperature"], abs=0.06)
        return

    # position: the reason this parser exists
    assert p["kind"] in ("position", "object", "item")
    assert p["lat"] == pytest.approx(want["lat"], abs=1.5e-6)
    assert p["lon"] == pytest.approx(want["lon"], abs=1.5e-6)

    if vid == "v46":  # raw NMEA: aprslib has nothing, expected_spec is the RMC decode
        assert p["format"] == "nmea" and p["speed_kmh"] == 0.0 and p["course"] == 168
        return

    # symbol: overlay characters render on the alternate table
    if "symbol_overlay" in spec:
        assert p["overlay"] == spec["symbol_overlay"] and p["sym_table"] == spec["render_table"]
    else:
        assert p["overlay"] is None and p["sym_table"] == want["symbol_table"]
    assert p["sym_code"] == want["symbol_code"]

    # course/speed (weather stations: wind, never course/speed)
    course = want["course"]
    if p["format"] == "mic-e" and course == 0:
        course = None  # Mic-E course 0 = unknown (APRS101 p.52)
    assert p["course"] == course
    if want["speed_kmh"] is None:
        assert p["speed_kmh"] is None
    else:
        assert p["speed_kmh"] == pytest.approx(want["speed_kmh"], abs=1e-3)
    if "wind_direction_deg" in spec:
        assert p["wx"]["wind_dir"] == spec["wind_direction_deg"]
        assert p["wx"]["wind_kmh"] == pytest.approx(spec["wind_speed_mph"] * MPH_KMH, abs=0.06)
    if want["altitude_m"] is None:
        assert p["alt_m"] is None
    else:
        assert p["alt_m"] == pytest.approx(want["altitude_m"], abs=0.01)

    # objects / items
    if p["kind"] in ("object", "item"):
        assert p["name"] == spec.get("object_name_display", want["object_name"]).rstrip()
        assert p["alive"] is want["alive"]
    else:
        assert p["name"] is None and p["alive"] is True

    # comment, with Mic-E device bytes removed
    assert p["comment"] == want["comment"]
    if "mice_device" in spec:
        assert p["device"] == spec["mice_device"]
        assert p["messaging"] is bool(spec["mice_messaging_capable"])


def test_every_vector_is_covered():
    assert len(VECTORS) == 50
    ids = {_vid(v) for v in VECTORS}
    assert ids == {f"v{i:02d}" for i in range(1, 51)}


# ---------------------------------------------------------------- old-parser regressions
# Each test names what the removed parser returned for the same packet.

def test_ambiguous_position_is_not_decoded_as_base91():
    # old parser: (55.995, -108.804), 3,720 km off (unvalidated base91 fallback)
    p = parse_line("KE4KMD-9>APDR16,TCPIP*,qAC,Q2AN:=3225.3 N\\08043.9 W>448.850MHz T100 +500")
    assert p["amb"] == 1
    assert p["lat"] == pytest.approx(32 + 25.35 / 60, abs=1e-6)
    assert p["lon"] == pytest.approx(-(80 + 43.95 / 60), abs=1e-6)


def test_eight_char_object_name_has_no_position():
    # old parser: (51.91, -114.73) in Alberta for a Pennsylvania repeater object
    assert parse_line("NJ3T-4>APN382,WIDE1-1,qAO,KC4RCR-10:;146.76PA*111111z3948.41N/07910.31Wr146.76MHz") is None


def test_ultimeter_data_has_no_position():
    # old parser: invented (59.996, -119.983)
    assert parse_line("QA4ADJ-3>APN383,qAO,Q4AAM-10:!!000000DD018B0A2C----------------002B026000000000") is None


def test_lowercase_hemisphere_letters():
    # old parser: fell through to base91 -> a point thousands of km away
    p = parse_line("QA7ACZ-10>APRS,WIDE1-1,WIDE2-1,qAR,QA7ACL:!4909.30n/12201.05w#ve7rad-10 fill in digi#")
    assert (p["lat"], p["lon"]) == (pytest.approx(49.155), pytest.approx(-122.0175))


def test_mic_e_is_decoded():
    # old parser: None for every Mic-E packet (11.4 % of all positions)
    p = parse_line(b'Q1ABF-9>TP3Y1R,qAR,QA1I:`fJiprgv/`"4^}_4\r\n')
    assert p["format"] == "mic-e" and p["kind"] == "position"
    assert (p["lat"], p["lon"]) == (pytest.approx(40.652), pytest.approx(-74.7795))
    assert p["course"] == 275 and p["speed_kmh"] == pytest.approx(88.896)
    assert p["alt_m"] == 71.0 and p["mice_msg"] == "En Route" and p["emergency"] is False
    assert p["device"] == "Yaesu FTM-500D" and p["messaging"] is True and p["comment"] == ""


def test_mic_e_emergency_bits():
    # same packet with message bits 000 (digits in dest bytes 1-3): Emergency
    p = parse_line(b'Q1ABF-9>403Y1R,qAR,QA1I:`fJiprgv/`"4^}_4')
    assert p["lat"] == pytest.approx(40.652)  # latitude digits unchanged
    assert p["mice_msg"] == "Emergency" and p["emergency"] is True


def test_mic_e_trailing_space_device_suffix_survives():
    # "_ " (Yaesu VX-8) ends in a space: the old parser's line.strip() ate it
    p = parse_line(b'Q1ABF-9>TP3Y1R,qAR,QA1I:`fJiprgv/`"4^}_ \r\n')
    assert p["device"] == "Yaesu VX-8" and p["comment"] == ""


def test_mic_e_rejects_garbage_bytes():
    # symbol table byte (info[8]) must be / \ 0-9 A-Z
    assert parse_line(b'Q1ABF-9>TP3Y1R,qAR,QA1I:`fJiprgva`"4^}_4') is None
    assert parse_line(b"Q1ABF-9>TP3Y1R,qAR,QA1I:`fJi") is None  # shorter than 9 bytes


def test_killed_object_and_items():
    # old parser: ignored the killed flag (dead objects stayed) and skipped items
    killed = parse_line("OUNFLS>APRS,qAO,AE5PL-WX:;OUNFLS90A_162100z3534.50N\\09816.80Ww FLOOD")
    assert killed["kind"] == "object" and killed["alive"] is False and killed["name"] == "OUNFLS90A"
    item = parse_line("NEWPRT>APRS,qAR,INDIAN:)PCH!4437.74N/12403.57Wh")
    assert item["kind"] == "item" and item["name"] == "PCH" and item["alive"] is True
    dead_item = parse_line("Q4AU-13>APMI06,TCPIP*,qAS,QA4G:)W21_3844.04N\\07750.01W_")
    assert dead_item["kind"] == "item" and dead_item["alive"] is False


def test_ambiguous_object_position():
    # old parser: regex demanded digits -> every WINLINK gateway object missed
    p = parse_line("QA4AS-11>APMI06,qAR,QA2E-10:;442.825_P*111111z2752.  N/08148.  Wr442.825MHz C100 + R40m")
    assert p["amb"] == 2 and p["lat"] == pytest.approx(27.875) and p["lon"] == pytest.approx(-81.808333, abs=1e-6)


def test_weather_wind_is_not_course_and_speed():
    p = parse_line("QA3AF-13>APRS,TCPIP*,qAS,QA3AF:@162105z4922.50N/01105.00E_355/006t052rpPh99b10187NodeRED")
    assert p["course"] is None and p["speed_kmh"] is None
    assert p["wx"] == {"temp_c": 11.1, "wind_dir": 355, "wind_kmh": 9.7, "gust_kmh": None,
                       "rain_1h_mm": None, "rain_24h_mm": None, "rain_midnight_mm": None,
                       "humidity": 99, "pressure_hpa": 1018.7}


def test_weather_comment_that_looks_like_fields_is_kept():
    # "rpi ..." starts with r and p but has no reading: roll back, keep the text
    p = parse_line("N0WX>APRS,TCPIP*:!3900.00N/09400.00W_090/005t070rpi weather")
    assert p["wx"]["temp_c"] == pytest.approx(21.1) and p["comment"] == "rpi weather"


def test_positionless_weather_report():
    p = parse_line("BENRDG>APRS,WIDE2-2,qAR,QA7ABI:_10161529c102s000g000t040h43e1w")
    assert p["kind"] == "weather" and p["lat"] is None
    assert p["wx"]["wind_dir"] == 102 and p["wx"]["humidity"] == 43 and p["comment"] == "e1w"


def test_third_party_uses_inner_station_and_gateway():
    raw = ("Q0AF>APX220,TCPIP*,qAC,Q2ACA:}Q0AF-13>APX220,WIDE1-1,WIDE2-1:"
           "!3309.71N/08643.18W_225/005g009t080r000p000P000h066b10102")
    p = parse_line(raw)
    assert p["src"] == "Q0AF-13" and p["igate"] == "Q0AF"
    assert p["wx"]["humidity"] == 66 and p["wx"]["pressure_hpa"] == 1010.2
    assert p["raw"] == raw


def test_header_fields():
    p = parse_line("Q0ABE-9>APAT51,Q0AAD-3*,WIDE2-1,qAR,QA0AAI-3:!3910.85N/09154.61W-")
    assert p["src"] == "Q0ABE-9" and p["dst"] == "APAT51" and p["dti"] == "!"
    assert p["path"] == ["Q0AAD-3*", "WIDE2-1", "qAR", "QA0AAI-3"]
    assert p["igate"] == "QA0AAI-3" and p["via"] == ["Q0AAD-3"]
    assert p["device"] == "AnyTone AT-D578" and p["messaging"] is False
    assert parse_line("W1AW>APRS,TCPIP*,qAC,T2X:=4143.75N/07242.50W-")["messaging"] is True


def test_status_packet():
    p = parse_line("Q8ACT-15>APRS,qAR,K8XX:>092345zNet control tonight")
    assert p["kind"] == "status" and p["status"] == "Net control tonight" and p["lat"] is None


def test_things_without_a_map_meaning_are_none():
    assert parse_line("# aprsc 2.1.23-g4ece07a 16 Oct 2026 20:50:47 GMT Q2ACD") is None
    assert parse_line("WXBOT>APRS,qAS,QA6ABT::QA1AAS-3 :ack125") is None  # message
    assert parse_line("N0ABC>APRS:T#005,199,000,255,073,123,01101001") is None  # telemetry
    assert parse_line("") is None and parse_line(b"\r\n") is None
    assert parse_line("no header here") is None


def test_dao_precision():
    p = parse_line("QA0P-1>APURSX,TCPIP*,qAC,T2PERTH:!3837.78N\\09220.65W\\ !w`(!")
    assert p["lat"] == pytest.approx(38.629782, abs=1e-6) and p["lon"] == pytest.approx(-92.344179, abs=1e-6)
    assert p["comment"] == ""


def test_phg_and_altitude_together():
    p = parse_line("Q1AR-2>APSTAR,TCPIP*,qAC,T2CHILE:!4342.83N/07209.38WrPHG9760/A=000098"
                   "Twin State Radio Club 145.330MHz T100 -060")
    assert p["phg"] == "9760" and p["alt_m"] == pytest.approx(29.87, abs=0.01)
    assert p["comment"] == "Twin State Radio Club 145.330MHz T100 -060"
    rate = parse_line("N0ABC>APN383:!4256.73NS07601.65W#PHG50304/Probe")
    assert rate["phg"] == "5030" and rate["comment"] == "Probe"
    freq = parse_line("N0ABC>APN383:!4256.73NS07601.65W#PHG7600146.760MHz")
    assert freq["phg"] == "7600" and freq["comment"] == "146.760MHz"


def test_raw_nmea():
    p = parse_line("KA8SPW-15>GPSLV,qAR,PETSKY:$GPGGA,205759.000,4529.0722,N,08435.7149,W,1,08,1.1,204.1,M,-36.0,M,,0000*69")
    assert p["format"] == "nmea" and p["alt_m"] == pytest.approx(204.1)
    assert p["lat"] == pytest.approx(45.484537, abs=1e-6) and p["lon"] == pytest.approx(-84.595248, abs=1e-6)
    # a corrupted checksum is not a position
    assert parse_line("KA8SPW-15>GPSLV,qAR,PETSKY:$GPGGA,205759.000,4529.0722,N,08435.7149,W,1,08,1.1,204.1,M,-36.0,M,,0000*00") is None


def test_minutes_over_59_rejected():
    # "2862.11N" is decimal degrees written as minutes: no pin beats a pin 50 km off
    assert parse_line("KD4WLE-7>APBPQ1,TCPIP*,qAC,Q2ACB:!2862.11NI08082.35W#PHG7260 TITUS1") is None


def test_text_decoding_and_control_bytes():
    utf8 = parse_line("N0ABC>APRS:!3900.00N/09400.00W-Café ☕".encode("utf-8"))
    assert utf8["comment"] == "Café ☕"
    latin1 = parse_line(b"N0ABC>APRS:!3900.00N/09400.00W-caf\xe9")
    assert latin1["comment"] == "café"
    ctrl = parse_line(b"N0ABC>APRS:!3900.00N/09400.00W-hi\x07there\x00")
    assert ctrl["comment"] == "hithere"
    # str and bytes inputs agree
    assert parse_line("N0ABC>APRS:!3900.00N/09400.00W-Café")["comment"] == "Café"


def test_compressed_overlay_digit_and_range():
    p = parse_line("QA6AP-6>APDW19,TCPIP*,qAC,Q2ACB:!g;/+Y/ls]#  !|#-#7%7)N*p!M|")
    assert p["sym_table"] == "\\" and p["overlay"] == "6" and p["sym_code"] == "#"
    assert p["comment"] == ""  # base91 comment telemetry removed


def test_frequency_mhz():
    assert frequency_mhz("146.94-KC", "") == 146.94
    assert frequency_mhz("443.775NE", "W0EQU") == 443.775
    assert frequency_mhz("Q9ABY-10", "145.610MHz Winlink Packet Gateway") == 145.61
    assert frequency_mhz(None, "Twin State 145.330MHz T100") == 145.33
    assert frequency_mhz("WX3IN1", "Batt 13.8V") is None
    assert frequency_mhz("1234.5", "") == 1234.5
    assert frequency_mhz("9999.9", "") is None  # outside 28-1300 MHz


def test_tocall_devices():
    assert device_for_tocall("APDW19") == "WB2OSZ DireWolf"
    assert device_for_tocall("APK003") == "Kenwood TH-D72"  # exact beats APK0??
    assert device_for_tocall("APK0ZZ") == "Kenwood TH-D7"
    assert device_for_tocall("APTT4-1") == "Byonics TinyTrak"  # "*" wildcard, SSID ignored
    assert device_for_tocall("APRS") is None and device_for_tocall("BEACON") is None


def test_capture_sample_regression():
    """202 real lines (fixtures/aprs_sample.txt, received bytes incl. CR).
    The old parser returned 139 positions here (0 of the 28 Mic-E)."""
    lines = [ln for ln in (FIXTURES / "aprs_sample.txt").read_bytes().split(b"\n") if ln]
    assert len(lines) == 202
    kinds, mice = {}, 0
    for line in lines:
        p = parse_line(line)
        if p is None:
            continue
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        mice += p.get("format") == "mic-e"
        if p["kind"] in ("position", "object", "item"):
            assert -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180
            assert p["sym_table"] in ("/", "\\") and p["sym_code"]
    assert kinds == {"position": 144, "object": 26, "item": 2, "status": 3, "weather": 3}
    assert mice == 28


def test_parser_never_raises_on_garbage():
    junk = [b"\xff\xfe\xfd", b">:", b"A>B:", b"A>B:`", b"A>B:;", b"A>B:)", b"A>B:}", b"A>B:}}}}}}}",
            b"A>B:$GPRMC,,,,", b"A>B:_", b"A>B:@", b"A>B:/123456z", b"A>B:!" + b"9" * 40,
            b"A>B:=/" + bytes(range(0x21, 0x7f)), b"A>B:!0000.00N/00000.00W>" + b"\x00" * 10]
    for raw in junk:
        aprs_parser.parse_line(raw)  # must not raise


# ---------------------------------------------------------------- review fixes (B3-R*)

def _nmea(sentence: str) -> str:
    """APRS-IS line carrying a raw NMEA sentence with a valid checksum: the
    checksum is no protection, anyone can compute it."""
    cs = 0
    for ch in sentence[1:]:
        cs ^= ord(ch)
    return f"N0NMEA-15>GPSLV,qAR,K0IG:{sentence}*{cs:02X}"


def _all_finite(p):
    nums = [p["lat"], p["lon"], p["speed_kmh"], p["course"], p["alt_m"]]
    nums += list((p["wx"] or {}).values())
    return all(v is None or math.isfinite(v) for v in nums)


@pytest.mark.parametrize("alt", ["nan", "NaN", "inf", "-inf", "Infinity", "1e308", "1_000", "+5", "999999"])
def test_nmea_altitude_must_be_a_plain_plausible_number(alt):
    # B3-R1: float() took "nan"/"inf"/"1e308"; one such packet made every
    # response containing the station an HTTP 500 (JSON has no NaN). Like
    # FAP, an unreadable field is dropped and the (good) fix is kept.
    p = parse_line(_nmea(f"$GPGGA,205759.000,3906.0000,N,09436.0000,W,1,08,1.1,{alt},M,-36.0,M,,0000"))
    assert p is not None and p["format"] == "nmea"
    assert (p["lat"], p["lon"]) == (pytest.approx(39.1), pytest.approx(-94.6))
    assert p["alt_m"] is None and _all_finite(p)


@pytest.mark.parametrize("speed", ["nan", "inf", "1e308", "-5", "99999"])
def test_nmea_speed_must_be_a_plain_plausible_number(speed):
    # B3-R1: "1e308" knots * 1.852 overflowed to inf
    p = parse_line(_nmea(f"$GPRMC,205049,A,3907.0000,N,09437.0000,W,{speed},168,020307,,"))
    assert p is not None and p["speed_kmh"] is None and p["course"] == 168 and _all_finite(p)


def test_nmea_plain_numbers_still_decode():
    p = parse_line(_nmea("$GPGGA,205759.000,3906.0000,N,09436.0000,W,1,08,1.1,-12.5,M,-36.0,M,,0000"))
    assert p["alt_m"] == pytest.approx(-12.5)  # below the geoid is real (Dead Sea shore: -430 m)
    p = parse_line(_nmea("$GPRMC,205049,A,3907.0000,N,09437.0000,W,012.5,168.4,020307,,"))
    assert p["speed_kmh"] == pytest.approx(12.5 * 1.852) and p["course"] == 168
    # altitude in anything but metres is not read as metres
    assert parse_line(_nmea("$GPGGA,205759.000,3906.0000,N,09436.0000,W,1,08,1.1,670,F,-36.0,M,,0000"))["alt_m"] is None


@pytest.mark.parametrize("lat,ns", [("39-5.000", "N"), ("39 5.000", "N"), ("39+5.000", "N"),
                                    ("3906.0000", ""), ("3906.0000", "X"), ("39nan", "N")])
def test_nmea_coordinates_are_strict(lat, ns):
    # B3-R1: int()/float() accepted signs, spaces and "nan"; an empty or
    # unknown hemisphere letter silently meant north/east
    assert parse_line(_nmea(f"$GPRMC,205049,A,{lat},{ns},09437.0000,W,0.0,168,020307,,")) is None


def test_mice_latitude_beyond_90_is_rejected():
    # B3-R3: _parse_mice never range-checked the latitude. Dest "9Y0P0P" is
    # 99 00.00 N; fuzzing real lines with one corrupted destination byte gave
    # 17 such pins (up to 99 N) that the store indexed and the API returned.
    assert parse_line(b'Q1ABF-9>9Y0P0P,qAR,QA1I:`fJiprgv/`"4^}_4') is None
    assert parse_line(b"KC5NMB-9>Y3SQ1R,KC5ULN-1*,W5NEM*,WIDE2*,qAR,W5NEM-1:`t4XoTo>/g4h}_4") is None  # 93.5 N
    assert parse_line(b'Q1ABF-9>TP3Y1R,qAR,QA1I:`fJiprgv/`"4^}_4')["lat"] == pytest.approx(40.652)


def test_phg_rate_digit_keeps_the_altitude_that_follows():
    # B3-R2: in "PHG74530/A=000492" the 5th digit is the beacon rate and the
    # "/" also starts "/A=". Consuming it lost the altitude and leaked
    # "A=000492..." into the comment: the aprslib defect research §4.1 lists
    # (FAP keeps the "/"; ref_decode reads 149.96 m).
    p = parse_line("K3MN>APSTAR,TCPIP*,qAC,T2ERFURT:!3959.21N/07524.04WrPHG74530/A=000492AllStarLink Node 62654")
    assert p["phg"] == "7453" and p["alt_m"] == pytest.approx(492 * 0.3048)
    assert p["comment"] == "AllStarLink Node 62654"
    p = parse_line("KC2NJV-13>APSTAR,TCPIP*,qAC,T2UKRAINE:!4039.38N/07331.33WrPHG04030/A=000033AllStar 62499")
    assert p["phg"] == "0403" and p["alt_m"] == pytest.approx(33 * 0.3048) and p["comment"] == "AllStar 62499"


def test_weather_humidity_written_as_three_dots():
    # B3-R10: Ambient CWOP bridges send "h..." for "no humidity"; the third
    # dot ended the field loop, so pressure was lost and ".b10042L007..."
    # became the comment (20 of 2,690 weather packets in the captures)
    p = parse_line("K1WJY>APRS,TCPIP*,qAC,AMBCWOP-2:@162050z4432.26N/06818.57W_324/000g000t..."
                   "r000p056P056h...b10042L007AmbientCWOP.com")
    assert p["wx"]["humidity"] is None and p["wx"]["pressure_hpa"] == pytest.approx(1004.2)
    assert p["wx"]["rain_24h_mm"] == pytest.approx(14.2) and p["wx"]["temp_c"] is None
    assert p["comment"] == "AmbientCWOP.com"
    two = parse_line("K1WJY>APRS,TCPIP*,qAC,AMBCWOP-2:@162050z4432.26N/06818.57W_324/000g000t...h..b10042")
    assert two["wx"]["pressure_hpa"] == pytest.approx(1004.2) and two["comment"] == ""
    # the same bridge writes "no pressure" as "b..." too (22 more captured packets)
    p = parse_line("K0AMB>APRS,TCPIP*,qAC,AMBCWOP-2:@162050z3853.66N/09451.78W_238/004g006t049r000p000P000"
                   "h67b...L024AmbientCWOP.com")
    assert p["wx"]["humidity"] == 67 and p["wx"]["pressure_hpa"] is None and p["comment"] == "AmbientCWOP.com"
    empty = parse_line("W8BUG>APRS,TCPIP*,qAC,AMBCWOP-2:@162051z3556.87N/08408.43W_.../...g...t...r...p...P..."
                       "h...b...AmbientCWOP.com")
    assert empty["wx"] is None and empty["comment"] == "AmbientCWOP.com"  # no readings at all
