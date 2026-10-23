"""US Census Bureau batch geocoder (free, no key, US addresses).

POSTs CSV batches of up to ~10k addresses to
https://geocoding.geo.census.gov/geocoder/locations/addressbatch and parses
the matched lon,lat back out. Used to upgrade hams from ZIP-level locations
to street-level locations from their FCC address.

Two Census behaviours shape this module:

* The service echoes the request's ID column back, but strips backslashes
  from it — so IDs are small per-batch integers mapped back to address keys
  locally, never the keys themselves.
* `Non_Exact` matches are fuzzy: ~3% land on a different street of the same
  town (1–16 km off), a few in another state. `matched_address_agrees`
  compares Census's matched address with the input so the importer can
  demote those to ZIP level.
"""

import csv
import io
import logging
import re
import time
from typing import NamedTuple

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# known-good addresses riding along in every batch as a throttling probe
_N_CONTROLS = 20


class GeocodeHit(NamedTuple):
    lat: float
    lon: float
    quality: str  # "Exact" | "Non_Exact"
    matched_address: str  # Census's normalized "STREET, CITY, ST, ZIP"


def address_key(street: str, city: str, state: str, zip_: str) -> str:
    """Normalized cache key for one address."""
    norm = lambda s: " ".join((s or "").upper().split())
    return f"{norm(street)}|{norm(city)}|{norm(state)}|{norm(zip_)[:5]}"


def split_address_key(key: str) -> tuple[str, str, str, str]:
    """Inverse of address_key: (street, city, state, zip). FCC fields are
    pipe-delimited, so a key always has exactly three '|'."""
    parts = key.split("|")
    if len(parts) != 4:
        return (key, "", "", "")
    return (parts[0], parts[1], parts[2], parts[3])


def parse_batch_response(text: str) -> tuple[dict[str, GeocodeHit], dict[str, str]]:
    """Parse the batch CSV response into (hits, misses).

    Match rows have 8 columns: id, input address, "Match", match type,
    matched address, "lon,lat", tigerLineId, side. `Tie` and `No_Match` rows
    have 3 columns and are misses ({id: status}). Rows missing from the
    reply are in neither dict, so they stay eligible for a retry.
    """
    hits: dict[str, GeocodeHit] = {}
    misses: dict[str, str] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            continue
        rid, status = row[0].strip(), row[2].strip()
        if status == "Match" and len(row) >= 6:
            try:
                lon_s, lat_s = row[5].split(",")
                hits[rid] = GeocodeHit(float(lat_s), float(lon_s), row[3].strip() or "Match",
                                       row[4].strip())
            except (ValueError, IndexError):
                continue
        elif status in ("No_Match", "Tie"):
            misses[rid] = status
    return hits, misses


def _post_chunk(client: httpx.Client, url: str, csv_text: str, label: str,
                expected: int = 1, control_ids: frozenset[str] = frozenset()
                ) -> tuple[dict[str, GeocodeHit], dict[str, str]]:
    """POST one chunk with backoff.

    The Census service throttles under load: read timeouts, 502s, or a 200
    reply where nearly every row is No_Match. A low match rate alone can't
    tell throttling apart from a batch of genuinely unmatchable addresses
    (the 30-day retries match ~0.5%), so each chunk carries a few known-good
    control addresses: if those fail too, the reply is junk. Without
    controls (empty cache) fall back to "<20% matched = throttled".

    A reply still judged throttled after the last attempt keeps its hits
    (real matches) but drops its misses, so those rows are retried later
    instead of being parked for a month.
    """
    delay = 30.0
    for attempt in range(1, 5):
        try:
            resp = client.post(
                url,
                data={"benchmark": "Public_AR_Current"},
                files={"addressFile": ("addresses.csv", csv_text, "text/csv")},
            )
            if resp.status_code == 200:
                hits, misses = parse_batch_response(resp.text)
                if control_ids:
                    ok = sum(1 for c in control_ids if c in hits)
                    throttled = ok < 0.5 * len(control_ids)
                    detail = f"{ok}/{len(control_ids)} controls matched"
                else:
                    throttled = bool(expected) and len(hits) < 0.2 * expected
                    detail = f"only {len(hits)}/{expected} matched"
                if not throttled:
                    return hits, misses
                if attempt < 4:
                    log.warning("[geocode] %s: %s — throttled response, backing off", label, detail)
                    time.sleep(delay)
                    delay *= 2
                    continue
                log.warning("[geocode] %s: still throttled (%s); keeping matches only", label, detail)
                return hits, {}
            log.warning("[geocode] %s HTTP %s (attempt %d/4)", label, resp.status_code, attempt)
        except httpx.HTTPError as exc:
            log.warning("[geocode] %s failed (attempt %d/4): %s", label, attempt, exc)
        if attempt < 4:
            time.sleep(delay)
            delay *= 2  # 30s, 60s, 120s between retries
    log.warning("[geocode] %s skipped after retries (will be retried at next scheduled run)", label)
    return {}, {}


def geocode_batch(rows: list[tuple[str, str, str, str, str]],
                  pause_seconds: float = 1.0,
                  on_chunk=None,
                  controls: list[tuple[str, str, str, str]] | None = None,
                  ) -> tuple[dict[str, GeocodeHit], dict[str, str]]:
    """Geocode (key, street, city, state, zip) rows in Census-sized chunks.

    Returns (hits, misses) keyed by the caller's key; misses map to the
    Census status ("No_Match" / "Tie"). `on_chunk(hits, misses)` fires after
    every chunk so callers can persist incrementally. `controls` are
    (street, city, state, zip) addresses known to match; they ride along in
    every chunk as a throttling probe and never appear in the results.
    """
    s = get_settings()
    controls = list(controls or [])[:_N_CONTROLS]
    all_hits: dict[str, GeocodeHit] = {}
    all_misses: dict[str, str] = {}
    approx_chunks = (len(rows) + s.census_batch_size - 1) // max(1, s.census_batch_size)

    buf = io.StringIO()
    i = n = 0
    with httpx.Client(timeout=s.census_timeout_seconds,
                      headers={"User-Agent": s.http_user_agent}) as client:
        while i < len(rows):
            # controls count against CENSUS_BATCH_SIZE (Census caps a batch at 10k)
            part = rows[i: i + max(1, s.census_batch_size - len(controls))]
            i += len(part)
            n += 1
            buf.seek(0)
            buf.truncate(0)
            w = csv.writer(buf)
            for n_id, (_, street, city, state, zip_) in enumerate(part):
                w.writerow([n_id, street, city, state, zip_])
            control_ids = frozenset(str(len(part) + j) for j in range(len(controls)))
            for j, (street, city, state, zip_) in enumerate(controls):
                w.writerow([len(part) + j, street, city, state, zip_])
            hits, misses = _post_chunk(client, s.census_url, buf.getvalue(),
                                       f"batch {n}/~{approx_chunks}", expected=len(part),
                                       control_ids=control_ids)
            # numeric per-batch IDs -> the caller's keys
            chunk_hits = {part[int(k)][0]: v for k, v in hits.items()
                          if k.isdigit() and int(k) < len(part)}
            chunk_misses = {part[int(k)][0]: v for k, v in misses.items()
                            if k.isdigit() and int(k) < len(part)}
            all_hits.update(chunk_hits)
            all_misses.update(chunk_misses)
            if chunk_hits or chunk_misses:
                log.info("[geocode] batch %d/~%d: %d/%d matched, %d unmatchable",
                         n, approx_chunks, len(chunk_hits), len(part), len(chunk_misses))
                if on_chunk:
                    on_chunk(chunk_hits, chunk_misses)
            if not controls and chunk_hits:
                # no probe yet (empty cache, e.g. the CI build): seed it from
                # this batch's exact matches
                exact = {k for k, h in chunk_hits.items() if h.quality == "Exact"}
                controls = [r[1:] for r in part if r[0] in exact][:_N_CONTROLS]
                if len(controls) < 5:
                    controls = []
            # back off politely after a failed batch; 1s after successes
            time.sleep(pause_seconds if (chunk_hits or chunk_misses) else 30.0)
    return all_hits, all_misses


# ---------------------------------------------------------------------------
# Non_Exact sanity check: does Census's matched address name the same
# street (number + name, same state) as the FCC input?
# ---------------------------------------------------------------------------

# directionals as FCC records spell them (input side; "NO ATLANTIC AVE")
_DIRECTIONALS = {
    "N": "N", "NORTH": "N", "NO": "N", "S": "S", "SOUTH": "S", "SO": "S", "E": "E", "EAST": "E",
    "W": "W", "WEST": "W", "NE": "NE", "NORTHEAST": "NE", "NW": "NW",
    "NORTHWEST": "NW", "SE": "SE", "SOUTHEAST": "SE", "SW": "SW", "SOUTHWEST": "SW",
}
_DIRECTIONAL_WORD = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
                     "NE": "NORTHEAST", "NW": "NORTHWEST", "SE": "SOUTHEAST", "SW": "SOUTHWEST"}
# Census writes real directionals abbreviated (its preDirection/suffixDirection
# fields), so a spelled-out compass word in a matched address is part of the
# street name: "E NORTH ST", "W SOUTH COVE RD", "N WEST END BLVD"
_CENSUS_DIRECTIONALS = frozenset(_DIRECTIONAL_WORD)
_COMPASS = ("NORTH", "SOUTH", "EAST", "WEST")

# USPS Publication 28, Appendix C1 (common street suffixes and their variants)
_SUFFIX_GROUPS = {
    "ALY": "ALLEY ALLEE ALLY ALY", "ANX": "ANNEX ANEX ANNX ANX", "ARC": "ARCADE ARC",
    "AVE": "AVENUE AV AVE AVEN AVENU AVN AVNUE", "BYU": "BAYOU BAYOO BYU",
    "BCH": "BEACH BCH", "BND": "BEND BND", "BLF": "BLUFF BLUF BLF", "BLFS": "BLUFFS BLFS",
    "BTM": "BOTTOM BOT BOTTM BTM", "BLVD": "BOULEVARD BOUL BOULV BLVD",
    "BR": "BRANCH BRNCH BR", "BRG": "BRIDGE BRDGE BRG", "BRK": "BROOK BRK",
    "BRKS": "BROOKS BRKS", "BG": "BURG BG", "BYP": "BYPASS BYPA BYPAS BYPS BYP",
    "CP": "CAMP CMP CP", "CYN": "CANYON CANYN CNYN CYN", "CPE": "CAPE CPE",
    "CSWY": "CAUSEWAY CAUSWA CSWY", "CTR": "CENTER CEN CENT CENTR CENTRE CNTER CNTR CTR",
    "CIR": "CIRCLE CIRC CIRCL CRCL CRCLE CIR", "CIRS": "CIRCLES CIRS", "CLF": "CLIFF CLF",
    "CLFS": "CLIFFS CLFS", "CLB": "CLUB CLB", "CMN": "COMMON CMN", "CMNS": "COMMONS CMNS",
    "COR": "CORNER COR", "CORS": "CORNERS CORS", "CRSE": "COURSE CRSE", "CT": "COURT CT",
    "CTS": "COURTS CTS", "CV": "COVE CV", "CVS": "COVES CVS", "CRK": "CREEK CRK",
    "CRES": "CRESCENT CRSENT CRSNT CRES", "CRST": "CREST CRST", "XING": "CROSSING CRSSNG XING",
    "XRD": "CROSSROAD XRD", "CURV": "CURVE CURV", "DL": "DALE DL", "DM": "DAM DM",
    "DV": "DIVIDE DIV DVD DV", "DR": "DRIVE DRIV DRV DR", "DRS": "DRIVES DRS",
    "EST": "ESTATE EST", "ESTS": "ESTATES ESTS", "EXPY": "EXPRESSWAY EXP EXPR EXPRESS EXPW EXPY",
    "EXT": "EXTENSION EXTN EXTNSN EXT", "FLS": "FALLS FLS", "FRY": "FERRY FRRY FRY",
    "FLD": "FIELD FLD", "FLDS": "FIELDS FLDS", "FLT": "FLAT FLT", "FLTS": "FLATS FLTS",
    "FRD": "FORD FRD", "FRST": "FOREST FORESTS FRST", "FRG": "FORGE FORG FRG",
    "FRK": "FORK FRK", "FRKS": "FORKS FRKS", "FT": "FORT FRT FT",
    "FWY": "FREEWAY FREEWY FRWAY FRWY FWY", "GDN": "GARDEN GARDN GRDEN GRDN GDN",
    "GDNS": "GARDENS GRDNS GDNS", "GTWY": "GATEWAY GATEWY GATWAY GTWAY GTWY",
    "GLN": "GLEN GLN", "GRN": "GREEN GRN", "GRV": "GROVE GROV GRV", "HBR": "HARBOR HARB HARBR HRBOR HBR",
    "HVN": "HAVEN HVN", "HTS": "HEIGHTS HT HTS", "HWY": "HIGHWAY HIGHWY HIWAY HIWY HWAY HWY",
    "HL": "HILL HL", "HLS": "HILLS HLS", "HOLW": "HOLLOW HLLW HOLLOWS HOLWS HOLW",
    "INLT": "INLET INLT", "IS": "ISLAND ISLND IS", "ISS": "ISLANDS ISLNDS ISS",
    "JCT": "JUNCTION JCTION JCTN JUNCTN JUNCTON JCT", "KY": "KEY KY", "KNL": "KNOLL KNOL KNL",
    "KNLS": "KNOLLS KNLS", "LK": "LAKE LK", "LKS": "LAKES LKS", "LNDG": "LANDING LNDNG LNDG",
    "LN": "LANE LN", "LGT": "LIGHT LGT", "LF": "LOAF LF", "LCK": "LOCK LCK", "LCKS": "LOCKS LCKS",
    "LDG": "LODGE LDGE LODG LDG", "LOOP": "LOOP LOOPS", "MNR": "MANOR MNR",
    "MNRS": "MANORS MNRS", "MDW": "MEADOW MDW", "MDWS": "MEADOWS MEDOWS MDWS",
    "ML": "MILL ML", "MLS": "MILLS MLS", "MSN": "MISSION MISSN MSSN MSN",
    "MTWY": "MOTORWAY MTWY", "MT": "MOUNT MNT MT", "MTN": "MOUNTAIN MNTAIN MNTN MOUNTIN MTIN MTN",
    "NCK": "NECK NCK", "ORCH": "ORCHARD ORCHRD ORCH", "OPAS": "OVERPASS OPAS",
    "PARK": "PARK PRK PARKS", "PKWY": "PARKWAY PARKWY PKWAY PKY PARKWAYS PKWYS PKWY",
    "PSGE": "PASSAGE PSGE", "PNE": "PINE PNE", "PNES": "PINES PNES", "PL": "PLACE PL",
    "PLN": "PLAIN PLN", "PLNS": "PLAINS PLNS", "PLZ": "PLAZA PLZA PLZ", "PT": "POINT PT",
    "PTS": "POINTS PTS", "PRT": "PORT PRT", "PRTS": "PORTS PRTS", "PR": "PRAIRIE PRR PR",
    "RADL": "RADIAL RAD RADIEL RADL", "RNCH": "RANCH RANCHES RNCHS RNCH",
    "RPD": "RAPID RPD", "RPDS": "RAPIDS RPDS", "RST": "REST RST", "RDG": "RIDGE RDGE RDG",
    "RDGS": "RIDGES RDGS", "RIV": "RIVER RVR RIVR RIV", "RD": "ROAD RD", "RDS": "ROADS RDS",
    "RTE": "ROUTE RTE RT", "SHL": "SHOAL SHL", "SHLS": "SHOALS SHLS", "SHR": "SHORE SHOAR SHR",
    "SHRS": "SHORES SHOARS SHRS", "SKWY": "SKYWAY SKWY", "SPG": "SPRING SPNG SPRNG SPG",
    "SPGS": "SPRINGS SPNGS SPRNGS SPGS", "SPUR": "SPUR SPURS", "SQ": "SQUARE SQR SQRE SQU SQ",
    "SQS": "SQUARES SQRS SQS", "STA": "STATION STATN STN STA",
    "STRA": "STRAVENUE STRAV STRAVEN STRAVN STRVN STRVNUE STRA", "STRM": "STREAM STREME STRM",
    "ST": "STREET STRT STR ST", "STS": "STREETS STS", "SMT": "SUMMIT SUMIT SUMITT SMT",
    "TER": "TERRACE TERR TER", "TRWY": "THROUGHWAY TRWY", "TRCE": "TRACE TRACES TRCE",
    "TRAK": "TRACK TRACKS TRK TRKS TRAK", "TRFY": "TRAFFICWAY TRFY",
    "TRL": "TRAIL TRAILS TRLS TRL", "TUNL": "TUNNEL TUNEL TUNLS TUNNELS TUNNL TUNL",
    "TPKE": "TURNPIKE TRNPK TURNPK TPKE", "UPAS": "UNDERPASS UPAS", "UN": "UNION UN",
    "VLY": "VALLEY VALLY VLLY VLY", "VLYS": "VALLEYS VLYS", "VIA": "VIADUCT VDCT VIADCT VIA",
    "VW": "VIEW VW", "VWS": "VIEWS VWS", "VLG": "VILLAGE VILL VILLAG VILLG VLG",
    "VLGS": "VILLAGES VLGS", "VL": "VILLE VL", "VIS": "VISTA VIST VST VSTA VIS",
    "WALK": "WALK WALKS", "WAY": "WAY WY", "WAYS": "WAYS", "WL": "WELL WL", "WLS": "WELLS WLS",
    "PIKE": "PIKE PIKES", "PATH": "PATH PATHS", "PASS": "PASS", "ROW": "ROW", "RUN": "RUN",
    "MALL": "MALL", "OVAL": "OVAL OVL", "MEWS": "MEWS",
}
# abbreviations FCC records use that C1 doesn't list (counted on the replica:
# CR 429, TR 348, CRT 259, LP 202, LA 126, PK 73, BL 55 as the street type)
_FCC_SUFFIX_VARIANTS = {"CR": "CIR", "TR": "TRL", "CRT": "CT", "LP": "LOOP", "LA": "LN",
                        "PK": "PARK", "BL": "BLVD", "TPK": "TPKE"}
_SUFFIXES = {v: std for std, variants in _SUFFIX_GROUPS.items() for v in variants.split()}
_SUFFIXES.update(_FCC_SUFFIX_VARIANTS)
# Spanish / route words Census abbreviates inside the name
_NAME_WORDS = {"CAMINO": "CAM", "CALLE": "CLL", "HIGHWAY": "HWY", "ROUTE": "RTE", "RT": "RTE"}
_HIGHWAY_WORDS = {"HWY", "RTE", "FM", "SR", "CR", "RR", "US", "I", "IH"}
_ORDINAL_WORDS = {
    "FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5", "SIXTH": "6",
    "SEVENTH": "7", "EIGHTH": "8", "NINTH": "9", "TENTH": "10", "ELEVENTH": "11",
    "TWELFTH": "12", "THIRTEENTH": "13", "FOURTEENTH": "14", "FIFTEENTH": "15",
    "SIXTEENTH": "16", "SEVENTEENTH": "17", "EIGHTEENTH": "18", "NINETEENTH": "19",
}
# compound ordinals Census writes as numbers: "TWENTY-FIRST" / "TWENTY FIRST" = 21ST
_TENS = {"TWENTY": 20, "THIRTY": 30, "FORTY": 40, "FIFTY": 50, "SIXTY": 60, "SEVENTY": 70,
         "EIGHTY": 80, "NINETY": 90}
_UNIT_ORDINALS = {w: int(n) for w, n in _ORDINAL_WORDS.items() if int(n) < 10}
for _w, _n in _TENS.items():
    _ORDINAL_WORDS[_w[:-1] + "IETH"] = str(_n)  # TWENTIETH … NINETIETH
    for _u, _m in _UNIT_ORDINALS.items():
        _ORDINAL_WORDS[_w + _u] = str(_n + _m)  # TWENTYFIRST (hyphen already removed)
del _w, _n, _u, _m
# secondary-unit designators (USPS C2) plus PMB/BOX: everything after is not
# part of the street name
_UNIT_WORDS = {"APT", "APARTMENT", "AP", "UNIT", "UNT", "STE", "SUITE", "#", "BLDG",
               "BUILDING", "LOT", "SPC", "SPACE", "SP", "RM", "ROOM", "TRLR", "TRAILER", "PMB",
               "DEPT", "FL", "FLOOR", "OFC", "OFFICE", "BOX", "SLIP", "NO", "NUMBER", "BSMT",
               "PO", "POB"}
# ...that are also street-name words ("BOX CANYON RD", "NO ATLANTIC AVE",
# "SLIP DOWN RD", "OFFICE PARK DR"): a unit only when a unit number or
# letter follows ("BOX 12", "NO 5", "SP 99", "LOT B"; not the ordinal of
# "1115 NO 11TH" = North 11th)
_UNIT_WORDS_BEFORE_NUMBER = {"BOX", "NO", "NUMBER", "SP", "SLIP", "LOT", "SPACE", "OFFICE",
                             "BUILDING", "ROOM", "FLOOR", "TRAILER", "DEPT", "APARTMENT"}
_UNIT_NUMBER_RE = re.compile(r"^(?:\d+(?:-?[A-Z])?|[A-Z]|[A-Z]-?\d+|#)$")  # "#": "LOT # 21"
# a number right after one of these is a box / unit / route number, never the
# house number ("PO BOX 123, 28 MAIN ST", "RR 4 21 OAK DR", "RM 677 IMU 900 E 7TH ST")
_NOT_HOUSE_AFTER = _UNIT_WORDS | {"PO", "POB", "DRAWER", "RR", "RFD", "HC", "HCR", "RT", "RTE",
                                  "ROUTE", "PSC", "CMR"}
_HOUSE_RE = re.compile(r"^(\d+)(?:-?(\d+))?[A-Z]?$|^(\d+)-[A-Z]$")
_GRID_HOUSE_RE = re.compile(r"^(?:[NSEW]\d+[NSEW]\d+|\d+[NSEW]\d+)$")  # WI "N123W456", IL "4N727"
_RURAL_GRID_HOUSE_RE = re.compile(r"^[NSEW]\d+$")  # WI/MN "N6742 LAUREL RD" (first token only)
_HYPHEN_FRACTION_RE = re.compile(r"^(\d+)-\d+/\d+$")  # "5-1/2"
_GLUED_HOUSE_RE = re.compile(r"^(\d+)([A-Z]{3,}|NE|NW|SE|SW)$")  # "511SHERWOOD", "5745NW" (not "21ST")
_HYPHEN_ORDINAL_RE = re.compile(r"^(\d+)-(\d+(?:ST|ND|RD|TH))$")  # "33020-10TH AVE SW"
_GLUED_DIR_RE = re.compile(r"^([NSEW])(\d+)$")
_GLUED_TYPE_RE = re.compile(r"^([A-Z]{2,4})[NSEW]$")  # "STW", "AVEN": type + directional
_ORDINAL_NUM_RE = re.compile(r"^(\d+)[A-Z]{1,3}$")


def _tokens(street: str) -> list[str]:
    s = (street or "").upper().replace("#", " # ")
    s = re.sub(r"\b([NS])\.\s?([EW])\b\.?", r"\1\2", s)  # "N.W." -> "NW"
    s = re.sub(r"[.,;:'\"()]", " ", s)
    out: list[str] = []
    for t in s.split():
        g = _GLUED_HOUSE_RE.match(t) or _HYPHEN_ORDINAL_RE.match(t)
        out += [g.group(1), g.group(2)] if g else [t]
    return out


def _house_token(token: str) -> str | None:
    """The house number a token spells, or None."""
    m = _HOUSE_RE.match(token)
    if m:
        return (m.group(1) + (m.group(2) or "")) if m.group(1) else m.group(3)
    if _GRID_HOUSE_RE.match(token):
        return token
    m = _HYPHEN_FRACTION_RE.match(token)
    return m.group(1) if m else None


def _house_number(tokens: list[str]) -> tuple[str, list[str]] | None:
    """Split off the house number. Returns (digits, remaining tokens) or None
    when the address has no house number (PO boxes, rural routes).

    The number is normally the first token. FCC street lines also carry a
    prefix before it — "C/O JANE DOE 12 MAIN ST", "URB LAS LOMAS 99 CEREIPO
    ST", "PO BOX 123, 28 MAIN ST", Michigan's "G 4058 W COURT ST" — so the
    first house-number token not belonging to a box/unit/route designator
    starts the street. (Census's matched address always starts with it.)
    """
    for i, t in enumerate(tokens):
        if i and tokens[i - 1] in _NOT_HOUSE_AFTER:
            continue
        number = _house_token(t)
        if number is None and i == 0 and _RURAL_GRID_HOUSE_RE.match(t):
            number = t  # later in the line "N3870" is a street name ("90 N3870 RD")
        if number is None:
            continue
        rest = tokens[i + 1:]
        if rest and re.match(r"^\d+/\d+$", rest[0]):
            rest = rest[1:]  # "317 1/2 W MAIN ST"
        return number, rest
    return None


def _normalize_name_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for i, t in enumerate(tokens):
        if skip:
            skip = False
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if t in _UNIT_WORDS and (t not in _UNIT_WORDS_BEFORE_NUMBER or (
                _UNIT_NUMBER_RE.match(nxt) and not (  # "131 NO 4090 EAST" = North 4090 East
                    t == "NO" and i + 2 < len(tokens) and tokens[i + 2] in _DIRECTIONALS))):
            if out:
                break  # a unit after the street name ends it
            skip = bool(_UNIT_NUMBER_RE.match(nxt))  # "118 UNIT G UNCLE LEO DR": skip the unit
            continue
        if t in _TENS and nxt in _UNIT_ORDINALS:  # "TWENTY FIRST" -> "21"
            out.append(str(_TENS[t] + _UNIT_ORDINALS[nxt]))
            skip = True
            continue
        t = t.replace("-", "")
        if not t:
            continue
        g = _GLUED_DIR_RE.match(t)
        if g:  # "N3870" -> "N", "3870"
            out += [g.group(1), g.group(2)]
            continue
        t = _NAME_WORDS.get(t, t)
        t = _ORDINAL_WORDS.get(t, t)
        o = _ORDINAL_NUM_RE.match(t)
        if o:  # "28TH" / "6ST" (typo) -> "28" / "6"
            t = o.group(1)
        out.append(t)
    return out


def _highway_number(tokens: list[str]) -> str | None:
    for i, t in enumerate(tokens[:-1]):
        if t in _HIGHWAY_WORDS and tokens[i + 1].isdigit():
            return tokens[i + 1]
    return None


def _damerau(a: str, b: str) -> int:
    """Optimal-string-alignment edit distance (transpositions count as 1)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _names_agree(a: str, b: str) -> bool:
    """Same street name, allowing the typos/spacing FCC records carry
    ("VANGAURD"/"VANGUARD", "TALLPINE"/"TALL PINE") but not a different
    street that merely shares a word ("HAPPY RIDGE"/"RIDGE",
    "NORTHFORK"/"SOUTHFORK", "PARKVIEW"/"PARKSIDE", "MOUNTAIN VIEW"/"MOUNTAIN")."""
    if a == b:
        return True
    if not a or not b:
        return False
    ca = next((c for c in _COMPASS if a.startswith(c)), "")
    cb = next((c for c in _COMPASS if b.startswith(c)), "")
    if ca != cb:
        return False
    if ca:  # judge what follows the shared compass word: "EASTER"/"EAST", "WESTB"/"WESTA"
        return _names_agree(a[len(ca):], b[len(cb):])
    if a[0].isdigit() or b[0].isdigit():
        return False  # numbered streets must match exactly ("9TH" vs "19TH")
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False  # "COUNTY RD 5" vs "COUNTY RD 50"
    if (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) >= 3:
        return False  # a whole extra word, not a typo: "MOUNTAINVIEW" vs "MOUNTAIN"
    if a[0] != b[0]:
        return False
    # a typo is an edit or two, not a different ending: "BOXELDERAV" (the
    # street type read as part of the name) is not "BOXELDERLEAF"
    shorter = min(len(a), len(b))
    return _damerau(a, b) <= (2 if shorter >= 7 else 1 if shorter >= 4 else 0)


def _census_name(tokens: list[str]) -> tuple[str, str | None, str | None, str | None]:
    """(name, USPS street type, pre-directional, post-directional) of
    Census's normalized street: directionals and the final street type
    removed, never the last name word ("E NORTH ST" -> "NORTH" with pre E,
    "N ST NW" -> "N" with post NW, "N UNION" -> "UNION" with pre N).
    Census writes directionals and street types in their standard USPS
    abbreviations, so a spelled-out word is part of the name."""
    core = list(tokens)
    post = None
    while len(core) > 1 and core[-1] in _CENSUS_DIRECTIONALS:
        post = core.pop()
    suffix = core[-1] if len(core) > 1 and core[-1] in _SUFFIX_GROUPS else None
    if suffix:
        core.pop()
    pre = None
    while len(core) > 1 and core[0] in _CENSUS_DIRECTIONALS:
        pre = pre or core[0]
        core.pop(0)
    return "".join(core), suffix, pre, post


def _input_starts(tokens: list[str]) -> list[tuple[list[str], str | None]]:
    """Where the input's street name may begin, each with the
    pre-directional that start implies: as written; after a leading
    directional (or with it spelled out, "N FORK" = "NORTH FORK") — kept as
    the name itself only before a street type ("100 N ST NW") or when the
    word is also an ordinary one ("NO NAME RD"); after a unit letter Census
    drops ("110 A MECHANIC ST")."""
    starts: list[tuple[list[str], str | None]] = [(tokens, None)]
    if tokens and tokens[0] in _DIRECTIONALS:
        d = _DIRECTIONALS[tokens[0]]
        keep = (len(tokens) > 1 and _type_word(tokens[1])) or tokens[0] in ("NO", "SO")
        starts = [(tokens, None)] if keep else []
        starts += [(tokens[1:], d), ([_DIRECTIONAL_WORD[d]] + tokens[1:], None)]
    for toks, pre in list(starts):
        if len(toks) >= 2 and len(toks[0]) == 1 and toks[0].isalpha():
            starts.append((toks[1:], pre))
    return starts


def _post_directional(rest: list[str]) -> str | None:
    """The input's post-directional: right after the name or after its
    street type ("MAIN ST NW", "175TH STW", "S 4190 WEST")."""
    for t in rest[:2]:
        if t in _DIRECTIONALS:
            return _DIRECTIONALS[t]
        if t in _SUFFIXES:
            continue  # the street type; a directional may follow it
        g = _GLUED_TYPE_RE.match(t)
        return t[-1] if g and g.group(1) in _SUFFIX_GROUPS else None
    return None


def _directionals_conflict(in_pre: str | None, in_post: str | None,
                           m_pre: str | None, m_post: str | None) -> bool:
    """Do the two addresses name different directionals? One side lacking
    one is tolerated (Census adds and drops them), but "1500 N MAIN ST" and
    "1500 S MAIN ST" are blocks to miles apart in a grid city — also when
    the directional moved to the other end ("N MAIN ST" / "MAIN ST S")."""
    if in_pre and m_pre and in_pre != m_pre:
        return True
    if in_post and m_post and in_post != m_post:
        return True
    if in_pre and not in_post and m_post and not m_pre:
        return in_pre != m_post
    if in_post and not in_pre and m_pre and not m_post:
        return in_post != m_pre
    return False


def _type_word(t: str) -> str | None:
    """The USPS street type a word spells — also glued to a one-letter
    directional ("175TH STW" = "175TH ST W") — or None."""
    std = _SUFFIXES.get(t)
    if std:
        return std
    g = _GLUED_TYPE_RE.match(t)
    return g.group(1) if g and g.group(1) in _SUFFIX_GROUPS else None


def _continues_name(rest: list[str], census_type: str | None) -> bool:
    """Do the input words after an agreeing name prefix carry more of the
    street name? After the name may come a unit number or letter, a
    directional, or the street type — then anything ("9TH AVE REAR HOUSE").
    Any other word continues the name ("MAPLE LEAF RD" is not "MAPLE AVE"),
    and so does a number, letter, directional or type word followed by a
    street type ("E 603RD LN" is not "EAST ST", "OAK HILL RD" is not "OAK
    ST") — unless it is the type Census names ("LAKE CIR DR" is "LAKE CIR")."""
    if not rest:
        return False
    t = rest[0]
    typ = _type_word(t)
    if typ is not None and typ == census_type:
        return False
    if typ is None and not (t[:1].isdigit() or len(t) == 1 or t in _DIRECTIONALS):
        return True
    return any(_type_word(x) not in (None, "EXT") for x in rest[1:])


def parse_matched_address(matched: str) -> tuple[str, str, str, str] | None:
    """Census "STREET, CITY, ST, ZIP" -> (street, city, state, zip)."""
    parts = [p.strip() for p in (matched or "").rsplit(",", 3)]
    if len(parts) != 4 or not parts[0]:
        return None
    return parts[0], parts[1], parts[2].upper(), parts[3][:5]


def matched_address_agrees(street: str, city: str, state: str, zip_: str, matched: str) -> bool:
    """Does Census's matched address name the input's street?

    Required: same state, same house number, same street name (case,
    punctuation, USPS suffix/directional abbreviations, ordinals, spacing and
    small typos normalized; no extra name words), no conflicting
    directionals. When both name a street type and the types differ
    ("WILBAR CT" vs "WILBAR AVE"), the city or ZIP must also agree. An
    unparsable matched address is accepted (no evidence against it).
    """
    m = parse_matched_address(matched)
    if m is None:
        return True
    m_street, m_city, m_state, m_zip = m
    if (state or "").strip().upper() != m_state:
        return False
    inp = _house_number(_tokens(street))
    mat = _house_number(_tokens(m_street))
    if inp is None or mat is None or inp[0] != mat[0]:
        return False
    in_tokens = _normalize_name_tokens(inp[1])
    m_tokens = _normalize_name_tokens(mat[1])
    hwy_in, hwy_m = _highway_number(in_tokens), _highway_number(m_tokens)
    if hwy_in and hwy_m:  # "RT 221 W" == "STATE RTE 221", "W HWY 33" != "E HWY 33"
        return hwy_in == hwy_m and not _directionals_conflict(
            _DIRECTIONALS.get(in_tokens[0]), _DIRECTIONALS.get(in_tokens[-1]),
            m_tokens[0] if m_tokens[0] in _CENSUS_DIRECTIONALS else None,
            m_tokens[-1] if m_tokens[-1] in _CENSUS_DIRECTIONALS else None)

    m_name, m_suffix, m_pre, m_post = _census_name(m_tokens)
    if not m_name:
        return False

    # input side is messy (units without a designator, extra words, a
    # directional Census dropped): accept when some token prefix names the
    # same street, the words after it don't continue the name and no
    # directional contradicts Census's
    for toks, in_pre in _input_starts(in_tokens):
        for k in range(1, len(toks) + 1):
            rest = toks[k:]
            if not _names_agree("".join(toks[:k]), m_name) or _continues_name(rest, m_suffix):
                continue
            if _directionals_conflict(in_pre, _post_directional(rest), m_pre, m_post):
                continue
            in_suffix = _type_word(toks[k]) if k < len(toks) else None
            if in_suffix and m_suffix and in_suffix != m_suffix:
                same_city = re.sub(r"[^A-Z0-9]", "", (city or "").upper()) == \
                    re.sub(r"[^A-Z0-9]", "", m_city.upper())
                same_zip = bool(zip_) and (zip_ or "")[:5] == m_zip
                if not (same_city or same_zip):
                    continue  # "3 WILBAR CT, NEW MILFORD" vs "3 WILBAR AVE, MILFORD"
            return True
    return False
