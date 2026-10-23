"""APRS packet parser: one APRS-IS (TNC2 text) line in, one plain dict out.

Pure functions, stdlib only. Written from APRS101 (2000) and the APRS 1.1/1.2
addenda (DAO, base91 comment telemetry, Mic-E device types), and checked
against ~18k live APRS-IS packets. We do not use aprslib: it is GPL, has been
unmaintained since 2022, skips items and NMEA, reads weather wind as
course/speed and leaves Mic-E device bytes in comments.

Rules that matter for map accuracy (each one fixed a wrongly placed pin in
the old parser):
- Lines are bytes. Only the CR/LF terminator is removed: str.strip() would
  eat Mic-E bytes 0x1c-0x1f and the space in some device suffixes.
- An uncompressed position is never retried as base91 "compressed" unless
  the strict compressed pattern matches. The old fallback turned any 13 bytes
  into a plausible coordinate up to 3,900 km away.
- Position ambiguity (spaces in the minutes) and lowercase hemisphere
  letters are legal and decoded; ambiguous positions sit at the box centre.
- Weather stations (symbol code ``_``) put wind direction/speed where other
  stations put course/speed; those go into ``wx``, never into course/speed.
- No number in the output is NaN or infinite, and every position kind has a
  lat/lon inside -90..90 / -180..180 (checked once, in parse_line): one NaN
  in a JSON response is an HTTP 500 for everyone looking at that area.

Device names come from the aprs-deviceid database by Hessu OH7LZB
(https://github.com/aprsorg/aprs-deviceid, CC BY-SA 2.0).
"""

import math
import re

KNOT_KMH = 1.852
MPH_KMH = 1.609344
FT_M = 0.3048
INCH_MM = 25.4

_MAX_THIRD_PARTY_DEPTH = 3

# ---------------------------------------------------------------- patterns

_SRC_RE = re.compile(r"^[\x21-\x7e]{1,15}$")
_UNC_RE = re.compile(
    r"^(\d{2})([0-9 ]{2}\.[0-9 ]{2})([NnSs])([/\\0-9A-Z])"
    r"(\d{3})([0-9 ]{2}\.[0-9 ]{2})([EeWw])([\x21-\x7e])")
_COMP_RE = re.compile(
    r"^([/\\A-Za-j])([\x21-\x7b]{4})([\x21-\x7b]{4})([\x21-\x7e])([\x20-\x7b])([\x20-\x7b])([\x20-\x7b])")
_CSE_SPD_RE = re.compile(r"^(\d{3})/(\d{3})")
# PHGphgd, or APRS 1.1 PHGphgdR/ (R = beacons/hour; "PHG7600146.760MHz" shows
# why R needs the slash), RNGrrrr, DFSshgd. Height may exceed 9 (FAP: 0x30-0x7e).
# The "/" after R is left in place when it starts "/A=": AllStarLink sends
# "PHG74530/A=000492", and eating it loses the altitude (aprslib's bug; FAP
# never consumes the slash). Otherwise it is only R's terminator and goes.
_PHG_RE = re.compile(r"^PHG(\d[\x30-\x7e]\d\d)(?:[0-9A-Z]/(?!A=)|[0-9A-Z](?=/A=))?")
_DATA_EXT_RE = re.compile(r"^(?:RNG\d{4}|DFS\d{4})")
_ALT_RE = re.compile(r"/A=(-\d{5}|\d{6})")
_DAO_RE = re.compile(r"!([Ww])([\x21-\x7b])([\x21-\x7b])!")
_B91_TELEMETRY_RE = re.compile(r"\|((?:[\x21-\x7b]{2}){2,7})\|")
_TS_RE = re.compile(r"^\d{6}[\x21-\x7e]")  # lenient: real objects send 111111z
_OBJECT_RE = re.compile(r"^;([\x20-\x7e]{9})([*_])(\d{6}[\x21-\x7e])")
_ITEM_RE = re.compile(r"^\)((?:(?![!_])[\x20-\x7e]){3,9})([!_])")
_STATUS_TS_RE = re.compile(r"^\d{6}z")
_MICE_DST_RE = re.compile(r"^[0-9A-LP-Z]{3}[0-9LP-Z]{3}$")
# byte ranges of Mic-E info bytes 1-8 (APRS101 ch.10); anything else is garbage.
# No "^": matched with .match(info, 1), i.e. right after the DTI byte.
_MICE_INFO_RE = re.compile(
    r"[\x26-\x7f][\x26-\x61][\x1c-\x7f]{2}[\x1c-\x7d][\x1c-\x7f][\x21-\x7e][/\\0-9A-Z]")
_MICE_TELEMETRY_RE = re.compile(r"^(`[0-9a-f]{4}|'[0-9a-f]{10})")
_MICE_ALT_RE = re.compile(r"([\x21-\x7b]{3})\}")
_NMEA_RE = re.compile(r"^\$G[PNLA](RMC|GGA|GLL),")
# NMEA numbers as FAP reads them; float() alone would also take "nan", "inf",
# "1e308", "1_000", "+5" and " 5" (a valid checksum is trivial to compute)
_NMEA_LAT_RE = re.compile(r"^(\d{2})(\d{2}(?:\.\d+)?)$")
_NMEA_LON_RE = re.compile(r"^(\d{3})(\d{2}(?:\.\d+)?)$")
_NMEA_UNSIGNED_RE = re.compile(r"^\d+(?:\.\d+)?$")
_NMEA_SIGNED_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
# plausibility: civilian GPS stops reporting at 515 m/s (~1000 kn, export
# rules) and balloon-mode receivers near 50 km; the Dead Sea shore is -430 m
_NMEA_MAX_KNOTS = 1080.0  # ~2000 km/h
_NMEA_ALT_M = (-1000.0, 100000.0)
_Q_RE = re.compile(r"^q[A-Z][A-Za-z]$")
_PATH_ALIAS_RE = re.compile(
    r"^(WIDE|TRACE|RELAY|ECHO|GATE|TEMP|RFONLY|NOGATE|TCPIP|TCPXX|IGATE|SGATE|BEACON|"
    r"CQ|QST|BLN|HOP|NCA|SS)\d*(-\d{1,2})?$")
_FREQ_NAME_RE = re.compile(r"^(\d{2,4}\.\d{1,4})")
_FREQ_COMMENT_RE = re.compile(r"(?<![\d.])(\d{2,4}\.\d{1,4}) ?MHz", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Mic-E standard message codes (APRS101 p.45) by the 3 message bits A/B/C
_MICE_MESSAGES = {7: "Off Duty", 6: "En Route", 5: "In Service", 4: "Returning",
                  3: "Committed", 2: "Special", 1: "Priority", 0: "Emergency"}

# aprs-deviceid "mice" (new-style 2-char suffix) and "micelegacy" tables
_MICE_SUFFIX = {
    "_ ": "Yaesu VX-8", '_"': "Yaesu FTM-350", "_#": "Yaesu VX-8G", "_$": "Yaesu FT1D",
    "_(": "Yaesu FT2D", "_0": "Yaesu FT3D", "_3": "Yaesu FT5D", "_1": "Yaesu FTM-300D",
    "_2": "Yaesu FTM-200D", "_4": "Yaesu FTM-500D", "_5": "Yaesu FTM-510D", "_6": "Yaesu FTX-1",
    "_7": "Yaesu FTM-310D", "_)": "Yaesu FTM-100D", "_%": "Yaesu FTM-400DR",
    "(5": "Anytone D578UV", "(8": "Anytone D878UV", "|3": "Byonics TinyTrak3",
    "|4": "Byonics TinyTrak4", "^A": "AxelStack APRS Connect", "^v": "HinzTec anyfrog",
    "*v": "KissOZ Tracker", "*9": "NOR AVRT9", ":2": "SQ8L VP-Tracker", ":4": "SQ8L pocketTracker",
    "~v": "Other", " X": "SainSonic AP510", "[1": "APRSdroid",
}
_MICE_LEGACY = {  # (prefix, suffix) -> device; suffix "" = prefix only
    (">", ""): "Kenwood TH-D7A", (">", "="): "Kenwood TH-D72", (">", "^"): "Kenwood TH-D74",
    (">", "&"): "Kenwood TH-D75", ("]", ""): "Kenwood TM-D700", ("]", "="): "Kenwood TM-D710",
}

# default symbol from the source SSID (APRS101 ch.20), used for raw NMEA only
_SSID_SYMBOL = {"1": "a", "2": "U", "3": "f", "4": "b", "5": "Y", "6": "X", "7": "[", "8": "s",
                "9": ">", "10": "<", "11": "O", "12": "j", "13": "R", "14": "k", "15": "v"}

_WX_KEYS = ("temp_c", "wind_dir", "wind_kmh", "gust_kmh", "rain_1h_mm", "rain_24h_mm",
            "rain_midnight_mm", "humidity", "pressure_hpa")
# field letter -> value pattern (APRS101 ch.12); "..." / spaces = no reading
_WX_FIELD_RE = {
    "c": re.compile(r"(\d{3}|\.{3}| {3})"),
    "s": re.compile(r"(\d{3}|\.{3}| {3})"),
    "g": re.compile(r"(\d{3}|\.{3}| {3})"),
    "t": re.compile(r"(-\d{1,2}|\d{1,3}|\.{3}| {3})"),
    "r": re.compile(r"(\d{3}|\.{3}| {3})"),
    "p": re.compile(r"(\d{3}|\.{3}| {3})"),
    "P": re.compile(r"(\d{3}|\.{3}| {3})"),
    # FAP (aprs.fi) accepts 1-3 humidity digits; live stations send "h066".
    # Ambient CWOP bridges write "no reading" as three dots in EVERY field,
    # whatever its width ("h...", "b..."): the third dot of "h..." used to end
    # the run, losing pressure and putting ".b10042L007..." in the comment.
    "h": re.compile(r"(\d{1,3}|\.{2,3}| {2})"),
    "b": re.compile(r"(\d{5}|\.{3,5}| {5})"),
    "L": re.compile(r"(\d{3}|\.{3}| {3})"),
    "l": re.compile(r"(\d{3}|\.{3}| {3})"),
    "#": re.compile(r"(\d{3}|\.{3}| {3})"),
}
_WX_WIND_RE = re.compile(r"^(\d{3}|\.{3}| {3})/(\d{3}|\.{3}| {3})")


# ---------------------------------------------------------------- helpers

def _b91(s: str) -> int:
    v = 0
    for ch in s:
        v = v * 91 + (ord(ch) - 33)
    return v


def _text(s: str) -> str:
    """Display text from a latin-1 byte string: UTF-8 when it decodes,
    else latin-1; control bytes dropped (frontend escapes the rest)."""
    try:
        s = s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return _CONTROL_RE.sub("", s)


def _raw_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _symbol(table: str, code: str, compressed: bool = False) -> tuple[str, str, str | None]:
    """(table, code, overlay): overlay characters live on the alternate table."""
    if table in "/\\":
        return table, code, None
    if compressed and "a" <= table <= "j":  # compressed overlay a-j = digits 0-9
        table = chr(ord(table) - ord("a") + ord("0"))
    return "\\", code, table


def _center(minutes: str) -> tuple[str, int] | None:
    """Ambiguity: trailing spaces in the minutes. First space -> 5 (centre of
    the box), the rest -> 0. Spaces must be trailing, else the field is bad."""
    digits = minutes.replace(".", "")
    amb = digits.count(" ")
    if amb and digits.rstrip(" ") != digits[: len(digits) - amb]:
        return None
    if not amb:
        return minutes, 0
    return minutes.replace(" ", "5", 1).replace(" ", "0"), amb


def _igate(path: list[str]) -> str | None:
    """The call after qAR/qAr/qAO/qAo is the IGate that heard it on RF."""
    for i, p in enumerate(path[:-1]):
        if p in ("qAR", "qAr", "qAO", "qAo"):
            return path[i + 1]
    return None


def _via(path: list[str]) -> list[str]:
    """Digipeaters that actually repeated the packet: path entries up to the
    last '*' before the q construct, minus generic aliases (WIDEn-N ...)."""
    pre = []
    for p in path:
        if _Q_RE.match(p):
            break
        pre.append(p)
    last = max((i for i, p in enumerate(pre) if p.endswith("*")), default=-1)
    out = []
    for p in pre[: last + 1]:
        call = p.rstrip("*")
        if call and not _PATH_ALIAS_RE.match(call.upper()):
            out.append(call)
    return out


def frequency_mhz(name: str | None, comment: str | None) -> float | None:
    """Voice frequency announced by an object name ("146.94-KC") or an APRS
    1.2 frequency comment ("443.775MHz T100"), limited to 28-1300 MHz."""
    for text, rx in ((name or "", _FREQ_NAME_RE), (comment or "", _FREQ_COMMENT_RE)):
        m = rx.search(text) if rx is _FREQ_COMMENT_RE else rx.match(text)
        if m:
            try:
                f = float(m.group(1))
            except ValueError:
                continue
            if 28.0 <= f <= 1300.0:
                return f
    return None


# ---------------------------------------------------------------- positions

def _parse_uncompressed(body: str) -> dict | None:
    m = _UNC_RE.match(body)
    if not m:
        return None
    latd, latm, ns, table, lond, lonm, ew, code = m.groups()
    lat_c, lon_c = _center(latm), _center(lonm)
    if lat_c is None or lon_c is None:
        return None
    (latm_s, amb_lat), (lonm_s, amb_lon) = lat_c, lon_c
    if amb_lat != amb_lon:  # aprslib/FAP reject mismatched ambiguity too
        return None
    if amb_lat >= 4:  # whole degrees ambiguous -> centre of the 1x1 degree box
        latm_s = lonm_s = "30.00"
    try:
        lat_min, lon_min = float(latm_s), float(lonm_s)
    except ValueError:
        return None
    if int(latd) > 89 or int(lond) > 179 or lat_min >= 60 or lon_min >= 60:
        return None
    lat = int(latd) + lat_min / 60.0
    lon = int(lond) + lon_min / 60.0
    if ns in "Ss":
        lat = -lat
    if ew in "Ww":
        lon = -lon
    tab, code, overlay = _symbol(table, code)
    out = {"format": "uncompressed", "lat": lat, "lon": lon, "sym_table": tab, "sym_code": code,
           "overlay": overlay, "amb": amb_lat, "course": None, "speed_kmh": None, "alt_m": None}
    rest = body[m.end():]
    if code != "_":  # weather stations put wind direction/speed here instead
        cs = _CSE_SPD_RE.match(rest)
        if cs:
            cse, spd = int(cs.group(1)), int(cs.group(2))
            if 1 <= cse <= 360:
                out["course"] = cse
            if spd > 0:
                out["speed_kmh"] = spd * KNOT_KMH
            rest = rest[7:]
    out["_rest"] = rest
    return out


def _parse_compressed(body: str) -> dict | None:
    m = _COMP_RE.match(body)
    if not m:
        return None
    table, y, x, code, c, s, t = m.groups()
    lat = 90.0 - _b91(y) / 380926.0
    lon = -180.0 + _b91(x) / 190463.0
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    tab, code, overlay = _symbol(table, code, compressed=True)
    out = {"format": "compressed", "lat": lat, "lon": lon, "sym_table": tab, "sym_code": code,
           "overlay": overlay, "amb": 0, "course": None, "speed_kmh": None, "alt_m": None}
    if c != " ":
        cv, sv, tv = ord(c) - 33, ord(s) - 33, ord(t) - 33
        if (tv & 0x18) == 0x10:  # GGA fix: cs is altitude, 1.002^cs feet
            out["alt_m"] = (1.002 ** (cv * 91 + sv)) * FT_M
        elif 0 <= cv <= 89:
            out["course"] = 360 if cv == 0 else cv * 4  # FAP/aprs.fi convention
            out["speed_kmh"] = (1.08 ** sv - 1) * KNOT_KMH
        elif cv == 90:  # '{' = pre-calculated radio range (miles)
            out["range_km"] = 2 * 1.08 ** sv * 1.609344
    out["_rest"] = body[13:]
    return out


def _parse_pos_body(body: str) -> dict | None:
    # disjoint first bytes (digit vs table char), and base91 only when the
    # strict compressed pattern matches
    return _parse_compressed(body) or _parse_uncompressed(body)


def _wx_value(field: str, raw: str):
    if not raw or set(raw) <= {".", " "}:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    if field in ("c", "wind_dir"):
        return v % 360 if 0 <= v <= 360 else None
    if field in ("s", "g"):
        return round(v * MPH_KMH, 1)
    if field == "t":
        return round((v - 32) * 5.0 / 9.0, 1) if -100 <= v <= 200 else None
    if field in ("r", "p", "P"):
        return round(v / 100.0 * INCH_MM, 1)
    if field == "h":
        if v == 0:
            return 100
        return v if v <= 100 else None
    if field == "b":
        return round(v / 10.0, 1)
    return None


_WX_TARGET = {"c": "wind_dir", "s": "wind_kmh", "g": "gust_kmh", "t": "temp_c", "r": "rain_1h_mm",
              "p": "rain_24h_mm", "P": "rain_midnight_mm", "h": "humidity", "b": "pressure_hpa"}


def _parse_wx_fields(s: str, positionless: bool) -> tuple[dict, str]:
    """Weather fields from the start of `s` -> (values, rest). Tolerates empty
    fields ("rpPh99" = no rain readings, humidity 99) but rolls back if the
    run of empty letters is not followed by a real reading, so a comment like
    "rpi weather" stays intact."""
    vals: dict = {}
    committed = (0, {})
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        rx = _WX_FIELD_RE.get(ch)
        if rx is None or (ch == "c" and not positionless):
            break
        m = rx.match(s, i + 1)
        if m:
            target = _WX_TARGET.get(ch)
            if ch == "s" and not positionless:
                target = None  # snowfall: not in our wx dict
            if target:
                vals[target] = _wx_value(ch, m.group(1))
            i = m.end()
            committed = (i, dict(vals))
        elif i + 1 < n and s[i + 1] in _WX_FIELD_RE:
            i += 1  # empty field, tentatively skip it
        else:
            break
    end, vals = committed
    return vals, s[end:]


def _wx_dict(vals: dict) -> dict | None:
    if not any(vals.get(k) is not None for k in _WX_KEYS):
        return None
    return {k: vals.get(k) for k in _WX_KEYS}


# ---------------------------------------------------------------- comments

def _strip_b91_telemetry(rest: str) -> str:
    m = _B91_TELEMETRY_RE.search(rest)
    return rest[: m.start()] + rest[m.end():] if m else rest


def _apply_dao(out: dict, rest: str) -> str:
    """DAO !Wab! (digits, +0.001') or !wab! (base91, +0.01'/91): extra
    precision, applied away from zero; the last occurrence wins (FAP)."""
    d = None
    for d in _DAO_RE.finditer(rest):
        pass
    if not d:
        return rest
    kind, a1, o1 = d.groups()
    if out["format"] != "compressed":
        if kind == "W" and a1.isdigit() and o1.isdigit():
            dl, do = int(a1) * 0.001 / 60, int(o1) * 0.001 / 60
        elif kind == "w":
            dl, do = (ord(a1) - 33) / 91.0 * 0.01 / 60, (ord(o1) - 33) / 91.0 * 0.01 / 60
        else:
            dl = do = 0.0
        out["lat"] += dl if out["lat"] >= 0 else -dl
        out["lon"] += do if out["lon"] >= 0 else -do
    return rest[: d.start()] + rest[d.end():]


def _finish(out: dict, rest: str) -> None:
    """Weather fields, data extensions, /A=, DAO and telemetry out of the
    comment of an uncompressed/compressed position, object or item."""
    if out["sym_code"] == "_":
        vals: dict = {}
        if out["format"] == "compressed":
            # cs bytes of a compressed weather position are wind dir/speed
            if out["course"] is not None:
                vals["wind_dir"] = out["course"] % 360
            if out["speed_kmh"] is not None:
                vals["wind_kmh"] = round(out["speed_kmh"], 1)
        else:
            w = _WX_WIND_RE.match(rest)
            if w:
                vals["wind_dir"] = _wx_value("c", w.group(1))
                vals["wind_kmh"] = _wx_value("s", w.group(2))
                rest = rest[7:]
        positionless_style = rest[:1] == "c" and not vals
        fields, rest = _parse_wx_fields(rest, positionless=positionless_style)
        vals.update(fields)
        out["course"] = out["speed_kmh"] = None
        out["wx"] = _wx_dict(vals)
    else:
        phg = _PHG_RE.match(rest)
        if phg:
            out["phg"] = phg.group(1)
            rest = rest[phg.end():]
        elif _DATA_EXT_RE.match(rest):
            rest = rest[7:]
    a = _ALT_RE.search(rest)
    if a:
        out["alt_m"] = int(a.group(1)) * FT_M
        rest = rest[: a.start()] + rest[a.end():]
    rest = _apply_dao(out, rest)
    rest = _strip_b91_telemetry(rest)
    out["comment"] = _text(rest.strip(" "))


# ---------------------------------------------------------------- Mic-E

def _mice_message(dst: str) -> tuple[str, bool]:
    bits, std, custom = 0, False, False
    for ch in dst[:3]:
        bits <<= 1
        if "A" <= ch <= "K":
            bits |= 1
            custom = True
        elif "P" <= ch <= "Z":
            bits |= 1
            std = True
    if bits == 0:
        return "Emergency", True
    if std and custom:
        return "Unknown", False
    if custom:
        return f"Custom-{7 - bits}", False
    return _MICE_MESSAGES[bits], False


def _mice_device(rest: str) -> tuple[str | None, bool, str]:
    """(device, messaging, comment) from the Mic-E status text: new-style
    `/' prefix + 2-char suffix, or Kenwood legacy >/] prefix (+ suffix).
    Runs before any whitespace stripping: "_ " (VX-8) ends in a space."""
    if len(rest) >= 3 and rest[0] in "`'" and rest[-2:] in _MICE_SUFFIX:
        return _MICE_SUFFIX[rest[-2:]], rest[0] == "`", rest[1:-2]
    if rest[:1] in (">", "]"):
        if len(rest) >= 2 and (rest[0], rest[-1]) in _MICE_LEGACY:
            return _MICE_LEGACY[(rest[0], rest[-1])], True, rest[1:-1]
        return _MICE_LEGACY[(rest[0], "")], True, rest[1:]
    return None, False, rest


def _parse_mice(dst: str, info: str) -> dict | None:
    dst = dst.split("-")[0]
    if len(dst) != 6 or not _MICE_DST_RE.match(dst) or len(info) < 9 or not _MICE_INFO_RE.match(info, 1):
        return None
    digits = []
    for ch in dst:
        if "0" <= ch <= "9":
            digits.append(ch)
        elif "A" <= ch <= "J":
            digits.append(chr(ord(ch) - 17))  # A-J -> 0-9 (custom message bit)
        elif "P" <= ch <= "Y":
            digits.append(chr(ord(ch) - 32))  # P-Y -> 0-9 (standard message bit)
        else:  # K, L, Z -> ambiguity space
            digits.append(" ")
    s = "".join(digits)
    m = re.match(r"^\d+( *)$", s)
    if not m:
        return None
    amb = len(m.group(1))
    if amb:
        chars = list(s)
        if amb >= 4:
            chars[2:6] = list("3000")
        else:
            chars[6 - amb] = "5"
            for i in range(7 - amb, 6):
                chars[i] = "0"
        s = "".join(chars)
    lat_min = float(s[2:4] + "." + s[4:6])
    if lat_min >= 60:
        return None
    lat = int(s[0:2]) + lat_min / 60.0
    if not "P" <= dst[3] <= "Z":
        lat = -lat
    d = ord(info[1]) - 28 + (100 if "P" <= dst[4] <= "Z" else 0)
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190
    mm = ord(info[2]) - 28
    if mm >= 60:
        mm -= 60
    hh = ord(info[3]) - 28
    if not (0 <= d <= 179 and 0 <= mm < 60 and 0 <= hh <= 99):
        return None
    if amb == 4:
        lon_min = 30.0
    elif amb == 3:
        lon_min = (mm // 10 + 0.5) * 10
    elif amb == 2:
        lon_min = mm + 0.5
    elif amb == 1:
        lon_min = mm + (hh // 10 + 0.5) / 10
    else:
        lon_min = mm + hh / 100.0
    lon = d + lon_min / 60.0
    if "P" <= dst[5] <= "Z":
        lon = -lon
    sp, dc, se = ord(info[4]) - 28, ord(info[5]) - 28, ord(info[6]) - 28
    speed = sp * 10 + dc // 10
    course = (dc % 10) * 100 + se
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400
    tab, code, overlay = _symbol(info[8], info[7])
    msg, emergency = _mice_message(dst)
    out = {"format": "mic-e", "lat": lat, "lon": lon, "sym_table": tab, "sym_code": code,
           "overlay": overlay, "amb": amb, "speed_kmh": speed * KNOT_KMH,
           "course": course if 1 <= course <= 360 else None,  # 0 = unknown
           "alt_m": None, "mice_msg": msg, "emergency": emergency}
    rest = info[9:]
    # altitude "xxx}" (base91 metres + 10 km): the spec puts it first, real
    # devices (Winlink APMAIL) append it - take the first occurrence (FAP)
    a = _MICE_ALT_RE.search(rest)
    if a:
        out["alt_m"] = float(_b91(a.group(1)) - 10000)
        rest = rest[: a.start()] + rest[a.end():]
    device, messaging, rest = _mice_device(rest)
    if device is None:
        t = _MICE_TELEMETRY_RE.match(rest)  # old 2/5-channel hex telemetry
        if t:
            rest = rest[t.end():]
        elif rest[:1] in ("`", "'"):  # type byte of a device not in the table
            messaging, rest = rest[0] == "`", rest[1:]
    out["device"] = device
    out["messaging"] = messaging
    rest = _apply_dao(out, rest)
    rest = _strip_b91_telemetry(rest)
    out["comment"] = _text(rest.strip(" "))
    return out


# ---------------------------------------------------------------- NMEA

def _nmea_coord(value: str, hemi: str, rx: re.Pattern, hemis: str) -> float | None:
    """DDMM.mmmm / DDDMM.mmmm plus its hemisphere letter. An empty or unknown
    letter is no position (it used to mean north/east)."""
    m = rx.match(value)
    if not m or not hemi or hemi not in hemis:
        return None
    minutes = float(m.group(2))
    if minutes >= 60:
        return None
    v = int(m.group(1)) + minutes / 60.0
    return -v if hemi == hemis[1] else v


def _nmea_number(text: str, rx: re.Pattern, low: float, high: float) -> float | None:
    """A plain decimal inside [low, high], else None: like FAP, an unreadable
    or absurd field is dropped and the fix itself is kept."""
    if not rx.match(text):
        return None
    v = float(text)
    return v if low <= v <= high else None


def _parse_nmea(info: str) -> dict | None:
    """Raw $GPRMC / $GPGGA / $GPGLL from old trackers (0.07% of traffic)."""
    m = _NMEA_RE.match(info)
    if not m:
        return None
    sentence = info.split("*", 1)
    if len(sentence) == 2:
        want = sentence[1][:2]
        cs = 0
        for ch in sentence[0][1:]:
            cs ^= ord(ch)
        try:
            if int(want, 16) != cs:
                return None
        except ValueError:
            return None
    f = sentence[0].split(",")
    kind = m.group(1)
    speed = course = alt = None
    if kind == "RMC":  # time,status,lat,N,lon,W,speed(kn),course,date
        if len(f) < 9 or f[2] != "A":
            return None
        lat, lon = _nmea_coord(f[3], f[4], _NMEA_LAT_RE, "NS"), _nmea_coord(f[5], f[6], _NMEA_LON_RE, "EW")
        knots = _nmea_number(f[7], _NMEA_UNSIGNED_RE, 0.0, _NMEA_MAX_KNOTS)
        speed = knots * KNOT_KMH if knots is not None else None
        course = _nmea_number(f[8], _NMEA_UNSIGNED_RE, 0.0, 360.0)
    elif kind == "GGA":  # time,lat,N,lon,W,fix,sats,hdop,alt,M
        if len(f) < 10 or f[6] in ("", "0"):
            return None
        lat, lon = _nmea_coord(f[2], f[3], _NMEA_LAT_RE, "NS"), _nmea_coord(f[4], f[5], _NMEA_LON_RE, "EW")
        if len(f) > 10 and f[10] == "M":  # metres only (FAP)
            alt = _nmea_number(f[9], _NMEA_SIGNED_RE, *_NMEA_ALT_M)
    else:  # GLL: lat,N,lon,W,time,status
        if len(f) < 5 or (len(f) > 6 and f[6][:1] == "V"):
            return None
        lat, lon = _nmea_coord(f[1], f[2], _NMEA_LAT_RE, "NS"), _nmea_coord(f[3], f[4], _NMEA_LON_RE, "EW")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"format": "nmea", "lat": lat, "lon": lon, "amb": 0, "speed_kmh": speed,
            "course": int(course) if course is not None and 0 < course <= 360 else None,
            "alt_m": alt, "comment": ""}


# ---------------------------------------------------------------- dispatch

def _blank(src: str, dst: str, path: list[str], dti: str, raw: str) -> dict:
    return {"src": src, "dst": dst, "path": path, "igate": _igate(path), "via": _via(path),
            "dti": dti, "kind": None, "name": None, "alive": True, "lat": None, "lon": None,
            "amb": 0, "sym_table": None, "sym_code": None, "overlay": None, "course": None,
            "speed_kmh": None, "alt_m": None, "comment": "", "status": None, "messaging": False,
            "mice_msg": None, "emergency": False, "device": None, "wx": None, "phg": None,
            "raw": raw, "format": None}


def _position(out: dict, pos: dict | None) -> dict | None:
    if pos is None:
        return None
    rest = pos.pop("_rest", "")
    pos.pop("range_km", None)  # pre-calculated radio range: not shown anywhere
    out.update(pos)
    _finish(out, rest)
    if not (-90.0 <= out["lat"] <= 90.0 and -180.0 <= out["lon"] <= 180.0):
        return None
    out["lat"] = round(out["lat"], 6)
    out["lon"] = round(out["lon"], 6)
    return out


def _parse(text: str, raw: str, depth: int) -> dict | None:
    head, sep, info = text.partition(":")
    if not sep or not info:
        return None
    src, sep, rest = head.partition(">")
    if not sep or not rest or not _SRC_RE.match(src):
        return None
    parts = rest.split(",")
    dst, path = parts[0], parts[1:]
    if not dst:
        return None
    dti = info[0]

    if dti == "}":  # third party: the inner packet is the real one
        if depth >= _MAX_THIRD_PARTY_DEPTH:
            return None
        inner = _parse(info[1:], raw, depth + 1)
        if inner is None:
            return None
        inner["path"] = inner["path"] + path
        inner["igate"] = inner["igate"] or _igate(path) or src
        return inner

    out = _blank(src, dst, path, dti, raw)

    if dti in "'`\x1c\x1d":
        pos = _parse_mice(dst, info)
        if pos is None:
            return None
        out.update(pos)
        out["kind"] = "position"
        out["lat"], out["lon"] = round(out["lat"], 6), round(out["lon"], 6)
        return out

    out["device"] = device_for_tocall(dst)  # Mic-E returned above: its dst is latitude

    if dti in "!=":
        if info[1:2] == "!":  # Ultimeter "!!" logging data: no position
            return None
        out["kind"] = "position"
        out["messaging"] = dti == "="
        return _position(out, _parse_pos_body(info[1:]))
    if dti in "/@":
        if not _TS_RE.match(info[1:8]):
            return None
        out["kind"] = "position"
        out["messaging"] = dti == "@"
        return _position(out, _parse_pos_body(info[8:]))
    if dti == ";":
        m = _OBJECT_RE.match(info)
        if not m:  # e.g. an 8-char name: info[10] is not the alive/killed flag
            return None
        out.update(kind="object", name=_text(m.group(1).rstrip(" ")), alive=m.group(2) == "*")
        if not out["name"]:
            return None
        return _position(out, _parse_pos_body(info[m.end():]))
    if dti == ")":
        m = _ITEM_RE.match(info)
        if not m:
            return None
        out.update(kind="item", name=_text(m.group(1).rstrip(" ")), alive=m.group(2) == "!")
        if not out["name"]:
            return None
        return _position(out, _parse_pos_body(info[m.end():]))
    if dti == ">":
        status = info[1:]
        if _STATUS_TS_RE.match(status):
            status = status[7:]
        out.update(kind="status", status=_text(status.strip(" ")))
        return out
    if dti == "_":
        if not re.match(r"^_\d{8}", info):
            return None
        fields, rest = _parse_wx_fields(info[9:], positionless=True)
        wx = _wx_dict(fields)
        if wx is None:
            return None
        out.update(kind="weather", wx=wx, comment=_text(rest.strip(" ")))
        return out
    if dti == "$":
        pos = _parse_nmea(info)
        if pos is None:
            return None
        ssid = src.split("-", 1)[1] if "-" in src else ""
        out.update(pos)
        out.update(kind="position", sym_table="/", sym_code=_SSID_SYMBOL.get(ssid, "/"))
        out["lat"], out["lon"] = round(out["lat"], 6), round(out["lon"], 6)
        return out
    # X1J4 rule: a '!' position within the first 40 characters
    i = info.find("!")
    if 0 <= i < 40 and dti not in ":>T_{<":
        out["kind"] = "position"
        return _position(out, _parse_pos_body(info[i + 1:]))
    return None


def parse_line(line: bytes | str) -> dict | None:
    """Parse one APRS-IS line. Returns None for comments, messages, telemetry
    and anything without a decodable meaning for the map. Position kinds
    ("position", "object", "item") always carry a valid lat/lon; "status"
    and positionless "weather" never do."""
    if isinstance(line, str):
        try:
            data = line.encode("latin-1")  # byte-faithful text (e.g. latin-1 decoded)
        except UnicodeEncodeError:
            data = line.encode("utf-8")
    else:
        data = bytes(line)
    data = data.rstrip(b"\r\n")  # only the terminator: never strip the info field
    if not data or data[:1] == b"#":
        return None
    try:
        return _checked(_parse(data.decode("latin-1"), _raw_text(data), 0))
    except (ValueError, IndexError, OverflowError):  # malformed beyond the patterns
        return None


def _checked(p: dict | None) -> dict | None:
    """The one gate every result passes. Position kinds need a lat/lon in
    range: Mic-E and NMEA build theirs outside _position, and one corrupted
    Mic-E destination byte reads as up to 99 N. No number may be NaN/inf:
    JSON has no NaN, so one would make every response holding the station
    an HTTP 500."""
    if p is None:
        return None
    if p["kind"] in ("position", "object", "item"):
        lat, lon = p["lat"], p["lon"]
        if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None  # NaN fails the comparison too
    for key in ("speed_kmh", "course", "alt_m"):
        if p[key] is not None and not math.isfinite(p[key]):
            p[key] = None
    if p["wx"]:
        p["wx"] = {k: v if v is None or math.isfinite(v) else None for k, v in p["wx"].items()}
    return p


# ---------------------------------------------------------------- devices

_TOCALL_EXACT: dict[str, str] = {}
_TOCALL_WILD: list[tuple[re.Pattern, str]] = []


def device_for_tocall(dst: str) -> str | None:
    """Device/software from the destination ("tocall"), exact match first,
    then the most specific wildcard (? = any char, n = digit, * = rest)."""
    if not _TOCALL_EXACT:
        _load_tocalls()
    t = dst.split("-", 1)[0].upper()
    hit = _TOCALL_EXACT.get(t)
    if hit:
        return hit
    for rx, label in _TOCALL_WILD:
        if rx.match(t):
            return label
    return None


def _load_tocalls() -> None:
    wild = []
    for line in _TOCALLS.strip().splitlines():
        tocall, _, label = line.partition("\t")
        if not label:
            continue
        if any(ch in tocall for ch in "?n*"):
            pattern = "".join("." if ch == "?" else "[0-9]" if ch == "n" else ".*" if ch == "*"
                              else re.escape(ch) for ch in tocall)
            literal = sum(1 for ch in tocall if ch not in "?n*")
            wild.append((literal, len(tocall), re.compile("^" + pattern + "$"), label))
        else:
            _TOCALL_EXACT[tocall] = label
    wild.sort(key=lambda w: (-w[0], -w[1]))
    _TOCALL_WILD[:] = [(rx, label) for _, _, rx, label in wild]


# aprs-deviceid tocalls index (CC BY-SA 2.0, https://github.com/aprsorg/aprs-deviceid),
# reduced to "TOCALL<tab>shown name"
_TOCALLS = r"""
AP1WWX	TAPR T-238+
AP3VSS	Active APRS tracker
AP4R??	APRS4R
AP9BBL	BI9BBL APRS Beacon Bot
AP9UAG	NusaAPRS
APAEP1	EIRUAPRSDIGIS&FV1
APAF??	AFilter
APAG??	AGate
APAGW	SV2AGW AGWtracker
APAGW?	SV2AGW AGWtracker
APAH??	AHub
APAIOR	APRSPH net bot based on Ioreth
APALH*	Retevis
APALH1	Retevis Ailunce H1
APALHA	Retevis Ailunce HA2
APAM??	Altus Metrum AltOS
APANC?	APRS Net Central
APAND?	APRSdroid
APAR??	Arctic Tracker
APAT16	AnyTone AT-D168UV
APAT51	AnyTone AT-D578
APAT81	AnyTone AT-D878
APAT89	AnyTone AT-D890UV
APAT??	AnyTone
APATAR	ATA-R APRS Digipeater
APAVR9	NOR AVRT9
APAVT5	SainSonic AP510
APAW??	SV2AGW AGWPE
APAX??	AFilterX
APB2MF	MF2APRS Radiosonde tracking tool
APBCNF	BEACONFIRE
APBK??	PY5BK Bravo Tracker
APBL??	BigRedBee BeeLine GPS
APBM??	R3ABM BrandMeister DMR
APBOX0	SQ9MDD Ryszard Labus APRSBox
APBPQ?	BPQ32
APBSD?	hambsd.org HamBSD
APBT*	BTECH
APBT62	BTECH DMR 6x2
APBT72	BTECH DA 7X2
APBTUV	BTECH UV-PRO
APC???	APRS/CE
APCDS0	ZS6LMG cell tracker
APCHP?	Digital Voice & APRS Software
APCLEY	ZS6EY EYTraker
APCLEZ	ZS6EY Telit EZ10 GSM application
APCLUB	Brazil APRS network
APCLWX	ZS6EY EYWeather
APCMD?	APRS Command
APCN??	DG5OAW carNET
APCSMS	USNA Cosmos
APCSS	AMSAT CubeSatSim CubeSat Simulator
APCTLK	Codec2Talkie
APCWP8	GM7HHB WinphoneAPRS
APD5T?	Open Source DStarGateway
APDAGW	NodeJS APRS WX
APDDRW	Dynamic Mobile Multi-Band Tracker & Bidirectional iGate
APDF??	Automatic DF units
APDG??	ircDDB Gateway
APDI??	DIXPRS
APDMPS	Douglas Pervine DMP APRS Simulator
APDNO?	DO3SWW APRSduino
APDP25	vk44.net Project 25 (P25)
APDPRS	D-Star APDPRS
APDR??	APRSdroid
APDS??	SP9UOB dsDIGI
APDST?	SP9UOB dsTracker
APDT??	APRStouch Tone (DTMF)
APDU??	JA7UDE U2APRS
APDV??	OE6PLD SSTV with APRS
APDW??	WB2OSZ DireWolf
APDnnn	aprsd
APE2A?	Email-2-APRS gateway
APE32I	Hiperion esp32idf_APRS
APE???	Telemetry devices
APECAN	KT5TK/DL7AD Pecan Pico APRS Balloon Tracker
APECWB	ecowitt-to-aprs-bridge
APELK?	WB8ELK Balloon tracker
APEML?	SP9MLI for WX, Telemetry
APEP??	LoRa WX station
APERHX	WX, Telemetry Scripts
APERRB	KG5JNC APRS Backend for Errbot
APERS?	Runner tracking
APERXQ	PE1RXQ APRS Tracker
APESP1	LY3PH APRS-ESP
APESPG	OH2TH ESP SmartBeacon APRS-IS Client
APESPW	OH2TH ESP Weather Station APRS-IS Client
APETBT	PD7R TBTracker Balloon Telemetry Tracker
APFG??	KP4DJT Flood Gage
APFI??	aprs.fi
APFIAN	aprs.fi Android app
APFII?	aprs.fi iPhone/iPad app
APFLIP	LU3ARN 818 APRS Transceiver
APFLPZ	YO3GND Flipper Zero standalone beacon
APFMN?	FM-Funknetz HS Dashboard
APFMO?	BG5ESN FMO (NFM Over Internet)
APFSYC	FSY Packet Console
APGBLN	NW5W GoBalloon
APGDT?	VK4FAST Graphic Data Terminal
APGKEY	9W2KEY iGate
APGLDG	AI7BQ GoldenGate AREDN MeshChat gateway
APGO??	AA3NJ APRS-Go
APGRW?	NW5W Graywolf
APHAX?	PY2UEP SM2APRS SondeMonitor
APHBL?	KF7EEL HBLink D-APRS Gateway
APHH?	HamHud
APHK??	LA1BR Digipeater/tracker
APHMEY	APRS-IS Client for Athom Homey
APHPIA	HP3ICC Arduino APRS
APHPIB	HP3ICC Python APRS Beacon
APHPIW	HP3ICC Python APRS WX
APHRM?	Meteo
APHRT?	Telemetry
APHT??	IU0AAC HMTracker
APHW??	HamWAN
API282	Icom IC-2820
API31	Icom IC-31
API410	Icom IC-4100
API51	Icom IC-51
API510	Icom IC-5100
API710	Icom IC-7100
API80	Icom IC-80
API880	Icom IC-880
API910	Icom IC-9100
API92	Icom IC-92
API970	Icom IC-9700
API???	Icom unknown
APIC??	HA9MCQ PICiGATE
APIE??	W7KMV PiAPRS
APIN??	AB0WV PinPoint
APIZCI	hymTR IZCI Tracker
APJ8??	KN4CRD JS8Call
APJA??	K4HG & AE5PL JavAPRS
APJE??	JeAPRS
APJI??	jAPRSIgate
APJID2	D-Star APJID2
APJS??	javAPRSSrvr
APJY??	KA2DDO YAAC
APK003	Kenwood TH-D72
APK004	Kenwood TH-D74
APK005	Kenwood TH-D75
APK0??	Kenwood TH-D7
APK1??	Kenwood TM-D700
APKDXn	LAHKHUANO APRS
APKEYn	9W2KEY ESP8266 APRS
APKHTW	Tempest Weather Bridge
APKJH?	VA3KJH TD-H9 BLE KISS
APKRAM	kramstuff.com Ham Tracker
APKTY1	KISSTTY
APKVPA	kv4p HT (Android)
APKVPI	kv4p HT (iOS)
APLC??	DL3DCW APRScube
APLDAG	DAGA LoRa/APRS SOTA spotting
APLDG?	LoRAIGate
APLDH?	LoraTracker
APLDI?	LoRa IGate/Digipeater
APLDL1	LoRa Digipeater
APLDM?	LoRa Meteostation
APLER?	TROY LoRa Tracker/iGate
APLETK	DL5TKL T-Echo
APLFG?	LoRa WX station
APLFL?	LoRa/APRS Balloon
APLFM?	DO1MA FemtoAPRS
APLG??	OE5BPA LoRa Gateway/Digipeater
APLHB9	SWISS-ARTG LoRa iGate RPI
APLHI?	LoRa IGate/Digipeater/Telemetry
APLHM?	LoRa Meteostation
APLHT1	HA8LHT LoRa Gateway
APLHT2	HA8LHT LoRa Tracker
APLIF?	TA5Y TIF LORA APRS I-GATE
APLIG?	TA2MUN/TA9OHC LightAPRS Tracker
APLLO?	HB4LO HAB BOT
APLLT?	LoRa Tracker
APLM??	WA0TQG
APLO??	SQ9MDD LoRa KISS TNC/Tracker
APLP0?	SQ9P fajne digi
APLP1?	SQ9P LORA/FSK/AFSK fajny tracker
APLPS?	ESP-32 LoRa
APLRF?	LoRa APRS
APLRG?	ESP32 LoRa iGate
APLRM?	Railab LoRa board
APLRS?	Railab LoRa board
APLRT?	ESP32 LoRa Tracker
APLS??	SARIMESH
APLT??	OE5BPA LoRa Tracker
APLU0?	SP9UP ESP32/SX12xx LoRa iGate / Digi
APLU1?	SP9UP ESP32/SX12xx LoRa Tracker
APLUL?	Alex YO2LDK
APLULD	Alex YO2LDK ESP32S3 UltimateLoRa iGate+Digi
APLULT	Alex YO2LDK ESP32S3 UltimateLoRa Tracker
APLXY1	2E0LXY LoRa APRS iGate
APLZA?	LoRa
APLZX?	N1AF LoRa-APRS
APM2A?	ZR1RF Meshtastic to APRS (mesh2aprs)
APMAA?	LA3MAA messaging projects
APMAIL	APRS Mailbox
APMBL3	Mobilinkd LLC TNC3
APMBL4	Mobilinkd LLC TNC4
APMBL?	Mobilinkd LLC
APMBLN	Mobilinkd LLC NucleoTNC
APMDN?	Meridian APRS
APMG??	PiCrumbs and MiniGate
APMI01	Microsat WX3in1
APMI02	Microsat WXEth
APMI03	Microsat PLXDigi
APMI04	Microsat WX3in1 Mini
APMI05	Microsat PLXTracker
APMI06	Microsat WX3in1 Plus 2.0
APMI??	Microsat
APMON?	APRS Balloon Tracker
APMPAD	DF1JSL Multi-Purpose APRS Daemon
APMQ??	WB2OSZ Ham Radio of Things
APMT??	LZ1PPL Micro APRS Tracker
APN0A?	N0ARY Full Service Packet BBS
APN102	APRSNow
APN2??	VE4KLM NOSaprs for JNOS 2.0
APN3??	Kantronics KPC-3
APN9??	Kantronics KPC-9612
APNCM	Net Control Manager
APND??	PE1MEW DIGI_NED
APNEO?	chevybowtie nesdr-aprs-igate
APNIC4	SQ5EKU BidaTrak
APNIFC	National Interagency Fire Center Alerts
APNJS?	Web messaging service
APNK01	Kenwood TM-D700
APNK80	Kantronics KAM
APNKMP	Kantronics KAM+
APNKMX	Kantronics KAM-XL
APNL??	dxlAPRS
APNM??	MFJ TNC
APNP??	PacComm TNC
APNT??	SV2AGW TNT TNC as a digipeater
APNU??	IW3FQG UIdigi
APNV0?	SQ8L VP-Digi
APNV1?	SQ8L VP-Node
APNV2?	SQ8L VP-Tracker
APNV??	SQ8L
APNW??	SQ3FYK WX3in1
APNX??	K6DBG TNC-X
APOA??	OpenAPRS app
APOCSG	N0AGI POCSAG
APODOT	Oregon Department of Transportion Traffic Alerts
APOFA?	OffARO — Offline APRS Radio Operation
APOG7?	OpenGD77
APOLU?	AMSAT-LU Oscar
APOMTR	OZ-TRAK APRS Mobile Tracker
APONE?	aprs.one
APONEA	aprs.one Mobile app tracker
APONET	aprs.one Hardware tracker
APOPEN	OpenTNC
APOPYT	NA7Q Messenger
APORT?	OpenRTX Contributors OpenRTX
APOSAT	Open Source Satellite Gateway
APOSB	SharkRF openSPOT3
APOSB4	SharkRF openSPOT4
APOSB?	SharkRF
APOSBM	SharkRF M1KE
APOSMS	Open Source SMS Gateway
APOSW?	SharkRF openSPOT2
APOT??	Argent Data Systems OpenTracker
APOVK5	UV-K5 TA1JS
APOVU?	K J Somaiya Institute BeliefSat
APOZ??	KissOZ
APP6??	APRSlib
APPCO?	PicoAPRSTracker
APPIC?	DB1NTO PicoAPRS
APPM??	DL1MX rtl-sdr Python iGate
APPRIS	DF1JSL Apprise APRS plugin
APPRJ?	Traveler's ptFlex and ptSolar trackers
APPRO?	KO6IKR PyTNC Pro
APPRPV	APRS PropView
APPS??	Polaric Server
APPT??	JF6LZE KetaiTracker
APPYTT	Lorenzo "vash" IU1BOT PyttiAPRS
APQCA?	ClassicAPRS
APQRX	QRX - APRS Missed Message Mailbox
APQTH?	QTH.app
APQUIZ	QUIZME APRS Quizmaster
APR2MF	MF2wxAPRS Tinkerforge gateway
APR8??	APRSdos
APRARX	radiosonde_auto_rx
APREAL	HA8LHT Real Packet
APREST	cwop.rest HTTP - TCP CWOP Packet Submission
APRFG?	RF.Guru
APRFGB	RF.Guru APRS LoRa Pager
APRFGD	RF.Guru APRS Digipeater
APRFGH	RF.Guru Hotspot
APRFGI	RF.Guru LoRa APRS iGate
APRFGL	RF.Guru Lora APRS Digipeater
APRFGM	RF.Guru Mobile Radio
APRFGP	RF.Guru Portable Radio
APRFGR	RF.Guru Repeater
APRFGT	RF.Guru LoRa APRS Tracker
APRFGW	RF.Guru LoRa APRS Weather Station
APRFTH	Treasure Hunt Service
APRG??	OH2GVE aprsg
APRHH?	HamHud
APRKEY	MYANET APRS Bot
APRM20	M20 radiosonde
APRMI?	Island Magic Co. iPhone/iPad app
APRNFW	Franek SP5FRA RS41-NFW
APRNOW	APRSNow
APRPI?	TA2KVC Raspberry Pi APRS / Pico W APRS
APRPJB	9M2PJU APRS BBS Bot
APRPJC	9M2PJU Malaysia Callsign Database Bot
APRPJE	9M2PJU APRS Echo and Signal Path Bot
APRPJG	9M2PJU APRS Riddle Game Bot
APRPJH	9M2PJU APRS Heat Bot
APRPJL	9M2PJU Bidirectional APRS To Email Bot
APRPJM	APRSMY Net Bot
APRPJS	9M2PJU APRS Web Messenger
APRPJT	9M2PJU APRS to Telegram Bot
APRPJU	9M2PJU APRS Bot
APRPJW	9M2PJU APRS WX Bot
APRPR?	Teensy RPR TNC
APRRDZ	DL9RDZ rdzTTGOsonde
APRRES	xssfox APRS-RepeaterRescue
APRRF?	Tracker for RRF
APRRT?	RPC Electronics RTrak
APRTAC	ModernHam APRStac
APRX??	Aprx
APSAR	ZL4FOX SARTrack
APSC20	aprsc
APSCOP	APRScope
APSDA?	APSD Developers APSD
APSDR?	sdr-control
APSF??	embedded APRS devices
APSFLG	LoRa/APRS Gateway
APSFRP	VHF/UHF Repeater
APSFTL	LoRa/APRS Telemetry Reporter
APSFWX	embedded Weather Station
APSIGK	Signal K APRS Plugin
APSIK?	SiKA APRS Suite
APSK63	APRS Messenger
APSMS?	Paul Dufresne SMS gateway
APSN01	CSN Technologies Inc. iGateMini
APSN??	CSN Technologies Inc.
APSPOT	APRSpot
APSRF?	SoftRF Ham Edition
APSTAR	AllStar Link LLC Asterisk/app_rpt
APSTKA	AxelStack LLC APRS Connect (Android)
APSTKB	AxelStack LLC APRS Connect (web)
APSTKI	AxelStack LLC APRS Connect (iOS)
APSTKL	AxelStack LLC APRS Connect (Linux)
APSTKM	AxelStack LLC APRS Connect (macOS)
APSTKW	AxelStack LLC APRS Connect (Windows)
APSTM?	W7QO Balloon tracker
APSTPO	N0AGI Satellite Tracking and Operations
APSVX?	SvxLink
APSWIF	APRSSwift
APSnnn	APRS+SA
APT2??	Byonics TinyTrak2
APT3??	Byonics TinyTrak3
APT4??	Byonics TinyTrak4
APTAAP	TA-APRS
APTB??	BG5HHP TinyAPRS
APTCHE	PU3IKE TcheTracker, Tcheduino
APTCMA	CAPI Tracker
APTEMP	KL7AF APRS-Tempest Weather Gateway
APTGIK	APRS Melaka
APTHUR	YD0BCX APRSThursday weekly event mapbot daemon
APTKJ?	W9JAJ ATTiny APRS Tracker
APTKVB	IT9KVB Python APRS QTH and Weather-Station
APTLVC	TA5LVC TR80 APRS Tracker
APTNG?	Filip YU1TTN Tango Tracker
APTPN?	KN4ORB TARPN Packet Node Tracker
APTR??	Motorola MotoTRBO
APTSLA	HA2NON tesla-aprs
APTT*	Byonics TinyTrak
APTUR?	Türkiye'nin APRS Uygulaması
APTW??	Byonics WXTrak
APU1??	UI-View16
APU2*	UI-View32
APUAGP	YF9UAG Python APRS UI
APUDR?	NW Digital Radio UDR
APURSX	UPRSx
APVE??	EchoLink
APVM??	Digital Radio China Club DRCC-DVM
APVOX?	VoxLink
APVR??	IRLP
APW2W?	WiresX2Web Software
APW9??	WX Katarina
APWA??	KJ4ERJ APRSISCE
APWEE?	WeeWX Weather Software
APWHE?	KF6UFO WX-Helios
APWM??	KJ4ERJ APRSISCE
APWOLF	APRSWolf
APWW??	KJ4ERJ APRSIS32
APWXS?	aprs-weather-submit
APWnnn	Sproul Brothers WinAPRS
APX???	Xastir
APXR??	G8PZT Xrouter
APY01D	Yaesu FT1D
APY02D	Yaesu FT2D
APY05D	Yaesu FT5D
APY200	Yaesu FTM-200D
APY300	Yaesu FTM-300D
APY310	Yaesu FTM-310D
APY400	Yaesu FTM-400
APY500	Yaesu FTM-500D
APY510	Yaesu FTM-510D
APYS??	W2GMD Python APRS
APYX01	Yaesu FTX-1
APZ*	Experimental
APZ18	IW3FQG UIdigi
APZ186	IW3FQG UIdigi
APZ19	IW3FQG UIdigi
APZ247	NR0Q UPRS
APZG??	OH2GVE aprsg
APZMAJ	M1MAJ DeLorme inReach Tracker
APZMDR	HaMDR
APZTKP	TrackPoint
APZWKR	GM1WKR NetSked
APnnnD	Painter Engineering uSmartDigi D-Gate
APnnnU	Painter Engineering uSmartDigi Digipeater
PSKAPR	PSKmail
"""
