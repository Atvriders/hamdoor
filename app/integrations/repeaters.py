"""Amateur repeater directory: HearHam bulk JSON (primary), enriched with
BrandMeister (DMR, live last-seen) and IRLP (live node status).

Every installation fetches straight from the publishers. HearHam relays
third-party listings it holds "sourced with permission", and BrandMeister /
IRLP publish no data licence, so nothing here is ever mirrored or bundled.
GeoNames cities500 (CC-BY 4.0) is fetched too, only to judge coordinates
(city-centroid / mid-ocean detection, sign-flip repair) and to tell which
country a record is in.

Pipeline
  fetch    each source on its own schedule -> normalize -> keep that source's
           last good normalized set (table repeater_sources), so one failing
           source never wipes the others;
  rebuild  whenever anything changed: classify locations per source -> merge
           (BrandMeister/IRLP enrich HearHam) -> dedupe -> finalize (country,
           band, kind) -> repeaters_import -> atomic swap into repeaters.
"""

import bz2
import html
import io
import json
import logging
import math
import os
import random
import re
import threading
import time
import unicodedata
import zipfile
import zlib
from array import array
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import text
from sqlalchemy.schema import CreateTable

from app.config import get_settings
from app.db import engine

log = logging.getLogger(__name__)

SOURCES = ("hearham", "brandmeister", "irlp")
PLACES = "geonames"  # pseudo-source: GeoNames cities500, used to judge coordinates
_BUILD = "_build"  # bookkeeping row: when/how the merged table was last built

# bump when the stored per-source records change shape or content (forces a
# re-fetch; until it succeeds, rebuilds re-apply the privacy scrub to the old set)
NORMALIZER_VERSION = 2
# bump when classification / merge rules change (rebuilds from stored records,
# no network needed; also re-applies the privacy scrub to sets older code
# stored, even while every source's re-fetch is failing)
BUILD_VERSION = 2

_HEARHAM_JITTER_S = 6 * 3600  # so installations don't all hit HearHam at once
_PLACES_REFRESH_DAYS = 90  # populated-place centroids barely move
_MIN_BACKOFF_S = 900  # matches the scheduler's 15-minute failure retry
_MAX_BACKOFF_S = 6 * 3600
_MAX_RETRY_AFTER_S = 24 * 3600  # a server's Retry-After is honoured up to a day, never longer
_SHRINK_GUARD = 0.5  # a refresh with < half the last good count is treated as a failure
_TALKGROUP_TTL_S = 24 * 3600
_TALKGROUP_FAIL_TTL_S = 600
_MIN_PLACES = 1000  # a real cities500 has ~236k; fewer means a broken download
_BM_ONAIR_DAYS = 7  # BM keeps listing repeaters ~30 days after they were last seen

US_JURISDICTION = frozenset({"US", "PR", "VI", "GU", "AS", "MP", "UM"})  # FCC rules apply

# ============================================================== bands

# Output-frequency edges (MHz). US: 47 CFR 97.301 (eCFR 2026-10-14); 1.25 m only
# where 97.205(b) allows repeaters (222.15-225); 9 cm was withdrawn from US
# amateur use in 2020 (WT 19-348), so a US "3.4 GHz" listing is "other".
_US_BANDS = (
    ("10m", 28.0, 29.7), ("6m", 50.0, 54.0), ("2m", 144.0, 148.0), ("1.25m", 222.15, 225.0),
    ("70cm", 420.0, 450.0), ("33cm", 902.0, 928.0), ("23cm", 1240.0, 1300.0),
    ("13cm", 2300.0, 2310.0), ("13cm", 2390.0, 2450.0), ("6cm", 5650.0, 5925.0),
    ("3cm", 10000.0, 10500.0),
)
# everywhere else (Regions 1/3, Canada, ...): wider or extra allocations
_WORLD_BANDS = (
    ("10m", 28.0, 29.7), ("6m", 50.0, 54.0), ("4m", 69.9, 70.5), ("2m", 144.0, 148.0),
    ("1.25m", 219.0, 225.0), ("70cm", 420.0, 450.0), ("33cm", 902.0, 928.0),
    ("23cm", 1240.0, 1300.0), ("13cm", 2300.0, 2450.0), ("9cm", 3300.0, 3500.0),
    ("6cm", 5650.0, 5925.0), ("3cm", 10000.0, 10500.0),
)
# the map filter's named bands; every other label filters as "other"
FILTER_BANDS = ("10m", "6m", "2m", "1.25m", "70cm", "33cm", "23cm")
MODE_ORDER = ("FM", "DMR", "DSTAR", "YSF", "P25", "NXDN", "M17", "ATV", "TETRA", "OTHER")


def band_of(mhz: float | None, us: bool = True) -> str:
    """Band label for an output frequency, using US edges for US-jurisdiction records."""
    if mhz is None:
        return "other"
    for name, lo, hi in (_US_BANDS if us else _WORLD_BANDS):
        if lo <= mhz <= hi:
            return name
    return "other"


def _world_band(mhz: float | None) -> str | None:
    if mhz is None:
        return None
    for name, lo, hi in _WORLD_BANDS:
        if lo <= mhz <= hi:
            return name
    return None


# ============================================================== small helpers

class FetchError(Exception):
    """A source could not be fetched/parsed. retry_after = server's Retry-After (s)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(x) -> float | None:
    """Float from upstream numbers/strings ('151.4*', '1\xa0750.0'); None if not finite."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(str(x).replace("*", "").replace("\xa0", "").replace("\u202f", "").strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _digits(raw, lo: int, hi: int) -> int | None:
    """An ID of lo..hi ASCII digits, else None. str.isdigit() also passes
    '31001²' (int() then raises) and unbounded lengths (SQLite can't store them)."""
    s = "" if raw is None or isinstance(raw, bool) else str(raw).strip()
    return int(s) if re.fullmatch(f"[0-9]{{{lo},{hi}}}", s) else None


_EPOCH_RANGE = (946684800, 4102444800)  # 2000-01-01 .. 2100-01-01


def _epoch_iso(raw) -> str | None:
    """Unix seconds -> ISO time; None when absent or absurd (99999999999999)."""
    ts = _digits(raw, 9, 10)
    if ts is None or not _EPOCH_RANGE[0] <= ts < _EPOCH_RANGE[1]:
        return None
    return _iso(datetime.fromtimestamp(ts, timezone.utc))


_SURROGATE = re.compile("[\ud800-\udfff]")


def _safe_text(s: str) -> str:
    """A lone surrogate (half a truncated emoji, from a JSON '\\ud83d' escape)
    can be neither stored in SQLite nor served as UTF-8."""
    return s if s.isascii() else _SURROGATE.sub("\ufffd", s)


def _each(items, label: str, convert) -> list[dict]:
    """Normalize upstream records one at a time: a malformed one is skipped
    and counted, never allowed to fail its whole source or, once stored, every
    rebuild after it. Text comes out storable."""
    out, skipped, first = [], 0, ""
    for x in items or ():
        try:
            r = convert(x)
        except Exception as exc:  # upstream data can be anything
            skipped += 1
            first = first or f"{exc.__class__.__name__}: {exc}"[:200]
            continue
        if r is not None:
            for k, v in r.items():
                if isinstance(v, str):
                    r[k] = _safe_text(v)
            out.append(r)
    if skipped:
        log.warning("[repeaters] %s: skipped %d malformed records (first: %s)", label, skipped, first)
    return out


def _one_per_id(recs: list[dict], label: str) -> list[dict]:
    """One record per ID. Upstream lists an ID twice now and then, and two rows
    with one primary key fail every table swap (dedupe can't pair the copies
    when one has no coordinates). Keep the copy with coordinates, then the
    fuller one, then the later one."""
    best: dict[str, tuple[tuple, dict]] = {}
    for i, r in enumerate(recs):
        rank = (_usable(r.get("lat"), r.get("lon")),
                sum(1 for f in _INFO_FIELDS if r.get(f) not in (None, "")), i)
        if r["id"] not in best or rank > best[r["id"]][0]:
            best[r["id"]] = (rank, r)
    if len(best) < len(recs):
        log.info("[repeaters] %s: %d duplicate IDs; kept one copy of each", label, len(recs) - len(best))
    return [r for _, r in best.values()]


def _ascii_key(s: str) -> str:
    """Lower-case ASCII key for name lookups ('México' -> 'mexico')."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9& ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _clean_callsign(raw) -> str:
    """'W9KLS/R Testing' -> 'W9KLS'; 'WR7HLN MPRG CP' -> 'WR7HLN'. Upper case,
    first token, repeater suffixes stripped so duplicates compare equal."""
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    tok = re.split(r"[\s,]+", s)[0]
    tok = re.sub(r"[/-](?:R|RPT|RPTR)$", "", tok)
    return tok.strip("/-.,")[:32]


# ============================================================== place text

_US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "VI": "Virgin Islands", "GU": "Guam", "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}
_US_TERRITORIES = frozenset({"PR", "VI", "GU", "AS", "MP"})
_US_STATE_BY_NAME = {_ascii_key(v): k for k, v in _US_STATE_NAMES.items()}
_CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}
_CA_PROVINCE_BY_NAME = {_ascii_key(v): k for k, v in _CA_PROVINCES.items()}
_CA_PROVINCE_BY_NAME.update({"newfoundland": "NL", "labrador": "NL", "pei": "PE",
                             "yukon territory": "YT"})
_AU_STATES = {
    "ACT": "Australian Capital Territory", "NSW": "New South Wales", "NT": "Northern Territory",
    "QLD": "Queensland", "SA": "South Australia", "TAS": "Tasmania", "VIC": "Victoria",
    "WA": "Western Australia",
}
_AU_STATE_BY_NAME = {_ascii_key(v): k for k, v in _AU_STATES.items()}
_AU_STATE_BY_NAME.update({"tasmania": "TAS", "queensland": "QLD", "victoria": "VIC"})
# GeoNames admin1 codes are numeric for Canada and Australia
_GEONAMES_ADMIN1 = {
    "CA": {"01": "AB", "02": "BC", "03": "MB", "04": "NB", "05": "NL", "07": "NS", "08": "ON",
           "09": "PE", "10": "QC", "11": "SK", "12": "YT", "13": "NT", "14": "NU"},
    "AU": {"01": "ACT", "02": "NSW", "03": "NT", "04": "QLD", "05": "SA", "06": "TAS",
           "07": "VIC", "08": "WA"},
}

# Country names as they appear in HearHam (RadioID-style "City, Country, Region"),
# IRLP's Country column and BM city text. "Georgia" is deliberately absent: it
# is far more often the US state (the location check sorts out the rest), and
# "Montserrat" is also a Catalan mountain (only trusted in IRLP's Country column).
_COUNTRY_NAMES = {
    "usa": "US", "us": "US", "u s a": "US", "u s": "US", "united states": "US",
    "united states of america": "US", "canada": "CA", "mexico": "MX", "puerto rico": "PR",
    "us virgin islands": "VI", "u s virgin islands": "VI", "virgin islands": "VI",
    "guam": "GU", "american samoa": "AS", "northern mariana islands": "MP",
    "uk": "GB", "united kingdom": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB", "great britain": "GB", "ireland": "IE", "eire": "IE",
    "germany": "DE", "deutschland": "DE", "austria": "AT", "oesterreich": "AT",
    "osterreich": "AT", "switzerland": "CH", "schweiz": "CH", "suisse": "CH", "france": "FR",
    "italy": "IT", "italia": "IT", "spain": "ES", "espana": "ES", "canary islands": "ES",
    "portugal": "PT", "netherlands": "NL", "the netherlands": "NL", "nederland": "NL",
    "holland": "NL", "belgium": "BE", "belgique": "BE", "belgie": "BE", "luxembourg": "LU",
    "luxemburg": "LU", "denmark": "DK", "danmark": "DK", "sweden": "SE", "sverige": "SE",
    "norway": "NO", "norge": "NO", "finland": "FI", "suomi": "FI", "iceland": "IS",
    "poland": "PL", "polska": "PL", "czech republic": "CZ", "czechia": "CZ", "slovakia": "SK",
    "hungary": "HU", "slovenia": "SI", "croatia": "HR", "hrvatska": "HR",
    "bosnia and hercegovina": "BA", "bosnia and herzegovina": "BA", "bosnia": "BA",
    "serbia": "RS", "montenegro": "ME", "macedonia": "MK", "north macedonia": "MK",
    "albania": "AL", "greece": "GR", "bulgaria": "BG", "romania": "RO", "moldova": "MD",
    "ukraine": "UA", "belarus": "BY", "russia": "RU", "russian federation": "RU",
    "lithuania": "LT", "latvia": "LV", "estonia": "EE", "turkey": "TR", "turkiye": "TR",
    "cyprus": "CY", "malta": "MT", "san marino": "SM", "monaco": "MC", "andorra": "AD",
    "liechtenstein": "LI", "israel": "IL", "kazakhstan": "KZ", "uzbekistan": "UZ",
    "china": "CN", "hong kong": "HK", "taiwan": "TW", "japan": "JP",
    "korea republic of": "KR", "korea s": "KR", "south korea": "KR", "korea": "KR",
    "philippines": "PH", "malaysia": "MY", "indonesia": "ID", "thailand": "TH", "india": "IN",
    "pakistan": "PK", "nepal": "NP", "oman": "OM", "qatar": "QA", "united arab emirates": "AE",
    "uae": "AE", "saudi arabia": "SA", "kuwait": "KW", "australia": "AU", "new zealand": "NZ",
    "south africa": "ZA", "morocco": "MA", "marocco": "MA", "reunion": "RE", "brazil": "BR",
    "brasil": "BR", "argentina": "AR", "argentina republic": "AR", "chile": "CL",
    "colombia": "CO", "ecuador": "EC", "peru": "PE", "venezuela": "VE", "uruguay": "UY",
    "paraguay": "PY", "bolivia": "BO", "costa rica": "CR", "el salvador": "SV",
    "guatemala": "GT", "honduras": "HN", "nicaragua": "NI", "panama": "PA", "belize": "BZ",
    "cuba": "CU", "dominican republic": "DO", "haiti": "HT", "jamaica": "JM", "bahamas": "BS",
    "barbados": "BB", "trinidad and tobago": "TT", "saint lucia": "LC", "st lucia": "LC",
    "saint kitts & nevis": "KN", "saint kitts and nevis": "KN", "st kitts": "KN",
    "st kitts and nevis": "KN", "antigua & barbuda": "AG", "antigua and barbuda": "AG",
    "dominica": "DM", "anguilla": "AI", "bermuda": "BM", "bonaire": "BQ",
    "curacao": "CW", "aruba": "AW", "grenada": "GD", "cayman islands": "KY",
    "british virgin islands": "VG", "st maarten": "SX", "sint maarten": "SX",
}
_STRUCTURED_COUNTRY_NAMES = {"montserrat": "MS"}

# A listing that says "UK" may sit on Jersey; "France" may be French St. Martin.
_COUNTRY_GROUPS = {
    "US": US_JURISDICTION,
    "GB": frozenset({"GB", "JE", "GG", "IM", "GI"}),
    "FR": frozenset({"FR", "GP", "MQ", "GF", "RE", "YT", "PM", "BL", "MF", "PF", "NC", "WF"}),
    "NL": frozenset({"NL", "AW", "CW", "SX", "BQ"}),
    "DK": frozenset({"DK", "GL", "FO"}),
    "NO": frozenset({"NO", "SJ"}),
    "AU": frozenset({"AU", "CX", "CC", "NF"}),
    "NZ": frozenset({"NZ", "CK", "NU", "TK"}),
}


def _group(cc: str | None) -> frozenset:
    if not cc:
        return frozenset()
    return _COUNTRY_GROUPS.get(cc) or (US_JURISDICTION if cc in US_JURISDICTION
                                       else frozenset({cc}))


# Mobile country code (first 3 digits of a 6-digit BrandMeister repeater ID) ->
# country. That is the owner's licence country, not necessarily the site's
# (US hams run BM repeaters in Taiwan), so it is only ever a soft hint.
# Codes shared by several territories (340, 362, 647, ...) are left out.
_MCC = {
    202: "GR", 204: "NL", 206: "BE", 208: "FR", 212: "MC", 213: "AD", 214: "ES", 216: "HU",
    218: "BA", 219: "HR", 220: "RS", 222: "IT", 225: "VA", 226: "RO", 228: "CH", 230: "CZ",
    231: "SK", 232: "AT", 234: "GB", 235: "GB", 238: "DK", 240: "SE", 242: "NO", 244: "FI",
    246: "LT", 247: "LV", 248: "EE", 250: "RU", 255: "UA", 257: "BY", 259: "MD", 260: "PL",
    262: "DE", 266: "GI", 268: "PT", 270: "LU", 272: "IE", 274: "IS", 276: "AL", 278: "MT",
    280: "CY", 282: "GE", 283: "AM", 284: "BG", 286: "TR", 288: "FO", 290: "GL", 292: "SM",
    293: "SI", 294: "MK", 295: "LI", 297: "ME", 302: "CA", 330: "PR", 334: "MX", 338: "JM",
    342: "BB", 344: "AG", 346: "KY", 348: "VG", 350: "BM", 352: "GD", 356: "KN", 358: "LC",
    360: "VC", 363: "AW", 364: "BS", 366: "DM", 368: "CU", 370: "DO", 372: "HT", 374: "TT",
    376: "TC", 400: "AZ", 401: "KZ", 404: "IN", 405: "IN", 410: "PK", 413: "LK", 415: "LB",
    416: "JO", 418: "IQ", 419: "KW", 420: "SA", 422: "OM", 424: "AE", 425: "IL", 426: "BH",
    427: "QA", 428: "MN", 429: "NP", 432: "IR", 434: "UZ", 436: "TJ", 437: "KG", 438: "TM",
    440: "JP", 441: "JP", 450: "KR", 452: "VN", 454: "HK", 455: "MO", 456: "KH", 457: "LA",
    460: "CN", 466: "TW", 470: "BD", 502: "MY", 505: "AU", 510: "ID", 515: "PH", 520: "TH",
    525: "SG", 530: "NZ", 602: "EG", 603: "DZ", 604: "MA", 605: "TN", 621: "NG", 639: "KE",
    655: "ZA", 702: "BZ", 704: "GT", 706: "SV", 708: "HN", 710: "NI", 712: "CR", 714: "PA",
    716: "PE", 722: "AR", 724: "BR", 730: "CL", 732: "CO", 734: "VE", 736: "BO", 740: "EC",
    744: "PY", 748: "UY",
}
_MCC.update({mcc: "US" for mcc in range(310, 317)})

_LOCATOR_TOKEN = re.compile(r"^[A-Ra-r]{2}\d{2}(?:[A-Xa-x]{1,2}(?:\d{2})?)?$")
# Postcodes are no part of a place name, and a full Canadian or UK postcode
# narrows a listing down to a few houses ('Gatineau (Québec) J8Y 1W7').
_POSTCODE = re.compile(
    r"\b\d{5}(?:-\d{4})?\b"  # US ZIP (+4)
    r"|(?i:\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d\b)"  # Canada
    r"|(?i:\b[A-Z]{1,2}\d[A-Z\d]?[ \t]+\d[ABD-HJLNP-UW-Z]{2}\b)")  # UK, outward + inward
_PLACEHOLDER_CITY = re.compile(r"^place\s*holder$", re.I)

# Privacy: no endpoint may return a street address or an email. Upstream free
# text has both: sysop contacts, and listings that use a home or club address
# as the "city" (New Zealand, much of Europe, some US AllStar nodes). Addresses
# are matched whatever their case ('125 n jefferson st', '239 MONTCLAIN AV');
# words that never belong to a street name (units, radio terms, function
# words) stop a match, so '40 watt repeater at St. Mary's', '123 Hz PL' and
# 'IRLP node 7067 via LAX Reflector 9358' stay as they are.
_EMAIL = re.compile(r"(?:mailto:)?[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_NOT_STREET = (
    "a|an|the|at|on|in|of|to|and|or|from|by|for|near|with|into|onto|off|over|under|via|per|is|"
    "are|was|be|as|plus|but|if|it|its|this|that|we|our|you|your|my|his|her|their|all|any|each|"
    "every|no|not|only|also|up|down|out|about|than|then|there|here|has|have|had|will|can|may|"
    "see|use|used|call|contact|email|phone|tel|fax|mail|box|lane|lanes|"
    "w|watt|watts|kw|ft|feet|foot|m|meter|meters|metre|metres|mi|mile|miles|km|kms|yd|yds|yards|"
    "mhz|khz|hz|ghz|db|dbd|dbi|dbm|el|elev|elevation|asl|amsl|agl|haat|hrs|hr|hour|hours|min|"
    "mins|minute|minutes|sec|am|pm|utc|gmt|edt|est|cdt|cst|mdt|mst|pdt|pst|"
    "pl|dpl|dcs|ctcss|tone|tones|cc|ts|slot|tg|talkgroup|talkgroups|node|nodes|repeater|"
    "repeaters|rptr|rpt|link|linked|links|echolink|irlp|allstar|dmr|ysf|fusion|dstar|p25|nxdn|"
    "m17|wires|wiresx|net|nets|band|bands|channel|ch|freq|frequency|offset|input|output|split|"
    "simplex|duplex|mode|analog|digital|beacon|antenna|antennas|element|elements|yagi|dipole|"
    "vertical|brandmeister|bm|tgif|reflector|ref|xlx|hotspot|internet|network|server"
)
_NAME_WORD = rf"(?!(?:{_NOT_STREET})\b)[^\W\d_][\w'’.-]*"
_HOUSE = r"(?<![\w.,/#-])\d{1,6}[A-Za-z]?(?:[ \t]*[-/][ \t]*\d{1,6}[A-Za-z]?)?"
_ARTICLE = r"(?:d[e'’]|du|des|la|le|les|l['’]|del|della|dei|di|da|do|dos|das|el|los|las)"
# number first, street type last: '125 N Jefferson St', '1133 State Highway 1'.
# Not after 'MP' / 'Mile' / 'Exit' ('Willow MP 70 Parks Hwy' is a milepost).
_STREET = re.compile(
    rf"(?i)(?=[0-9])(?<!\bmp )(?<!\bmile )(?<!\bexit ){_HOUSE}[ \t]+"
    r"(?:(?:north|south|east|west|[nsew]|ne|nw|se|sw)\.?[ \t]+)?"
    rf"(?:{_NAME_WORD}[ \t]+){{1,3}}"
    r"(?:st|street|ave|av|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|ct|court|way|pl|place|"
    r"pkwy|parkway|cir|circle|ter|terrace|trl|trail|cres|crescent|hwy|highway|route|rte|pike|"
    r"tpke|turnpike|loop|sq|square|pde|parade|esplanade|aly|alley|plz|plaza|expy|expressway|"
    r"fwy|freeway|(?-i:Line|Row|Walk|Rise|Track|Close|Grove|Quay|Mews))\b(?!-)\.?"
    r"(?:[ \t]+\d{1,4}[A-Za-z]?\b(?![.,]\d))?")
# number first, street type first: '178 rue du Chesne', '2205 route de Bras'.
# Types that are also English words need an article or a capital after them.
_STREET_TYPE_FIRST = re.compile(
    rf"(?i){_HOUSE}(?:[ \t]*(?:bis|ter))?,?[ \t]+"
    r"(?:(?:rue|chemin|all[ée]e|impasse|quai|voie|sentier|lieu[- ]dit|chauss[ée]e|calle|avenida|"
    r"avda|carrera|camino|paseo|carretera|ctra|ronda|traves[íi]a|rua|travessa|estrada|pra[çc]a|"
    r"alameda|rodovia|viale|piazza|piazzale|corso|vicolo|strada)\b\.?"
    rf"|(?:place|route|avenue|av|boulevard|bd|square|passage|plaza)\b\.?"
    rf"(?=[ \t]+(?:{_ARTICLE}\b|(?-i:[A-Z]))))"
    rf"(?:[ \t]+(?:{_ARTICLE}\b|{_NAME_WORD})){{1,4}}")
# street type first, number after: 'Via G.Marconi 103', 'Calle Mayor 5', 'ul. Polna 3',
# 'Str. Voinesti nr. 38' (Romania / Moldova write 'nr.' before the number)
_STREET_NUMBER_AFTER = re.compile(
    r"(?i)\b(?:(?-i:Via|Viale|Piazza|Corso|Calle|Avenida|Rua|Travessa|Estrada|Carrera)"
    r"|viale|piazza|calle|avenida|rua|travessa|ulica|ul\.|utca|str\.|strada)"
    rf"\.?(?:[ \t]+(?:{_ARTICLE}\b|{_NAME_WORD})){{1,4}}[ \t]+(?:(?:nr|no|n[°º])\.?[ \t]*)?"
    r"\d{1,5}[A-Za-z]?\b(?![.,]\d)")
# street type glued to its name, number after: 'Veldestraat 39', 'Hauptstr. 3A',
# 'Ouluntie 31'. A word followed by a number is only a candidate; its ending
# decides (one regex with every ending costs ~10x more on HearHam's notes).
_WORD_NUMBER = re.compile(r"(?<!\w)([^\W\d_]{4,}\.?)[ \t]*[0-9]{1,4}[A-Za-z]?\b(?![.,][0-9])")
_GLUED_ENDINGS = (
    "straße", "strasse", "str.", "straat", "steenweg", "laan", "gasse", "platz", "allee", "weg",
    "damm", "ufer", "schanze", "gracht", "kade", "plein", "singel", "vej", "vejen", "gaden",
    "gatan", "gata", "gaten", "vägen", "vagen", "vegen", "veien", "tie", "katu", "kuja", "polku",
    "väylä", "vayla", "utca", "ulica")  # Hungarian / Croatian: 'Puskin utca 1', 'Novoseljanska ulica 9'


def _glued_street(word: str) -> bool:
    w = word.lower()
    return w.endswith(_GLUED_ENDINGS) or (w.endswith("gade") and not w.endswith("brigade"))


_RAPID = re.compile(r"(?i)\brapid[ \t]*(?:no\.?|#)?[ \t]*\d{1,5}\b")  # NZ rural address number
_POBOX = re.compile(r"\bP\.?[ \t]*O\.?[ \t]*Box[ \t]*\d+\b", re.I)
_ADDR = "\x00"  # where parse_place found an address (so it knows a part was one)


def _scrub(s: str, email: str, address: str) -> str:
    s = _EMAIL.sub(email, s)
    for rx in (_STREET_TYPE_FIRST, _STREET_NUMBER_AFTER):
        s = rx.sub(address, s)
    s = _WORD_NUMBER.sub(lambda m: address if _glued_street(m.group(1)) else m.group(0), s)
    for rx in (_STREET, _RAPID, _POBOX):
        s = rx.sub(address, s)
    return s


def scrub_private(s: str, marker: bool = True) -> str:
    """Remove emails, street addresses and PO boxes from upstream text."""
    return _scrub(s, "[email removed]" if marker else "", "[address removed]" if marker else "")


# a number is not a house number after a road / radio word ('Hwy 50', 'MP 70',
# 'IRLP 7966') or before a unit ('100 Mile House', '2183 m')
_NUMBER_AFTER = frozenset({
    "hwy", "highway", "route", "rte", "rt", "sr", "us", "i", "interstate", "fm", "rm", "cr", "exit",
    "mile", "mp", "milepost", "km", "jct", "junction", "echolink", "echo", "echoirlp", "irlp",
    "allstar", "asl", "node", "nodo", "tg", "talkgroup", "dmr", "link", "site", "rptr", "rpt",
    "repeater", "ch", "channel", "pl", "tone", "ctcss", "dcs"})
_NUMBER_BEFORE = frozenset({
    "mile", "miles", "mi", "km", "m", "ft", "feet", "meter", "meters", "metre", "metres", "cm", "mm",
    "mhz", "khz", "hz", "ghz", "w", "watt", "watts", "palms"})
_NUMBER_TOKEN = re.compile(r"(?<![^\s(])[0-9]{1,5}[A-Ha-h]?(?=$|[\s,;)]|\.(?![0-9]))")


def _drop_house_numbers(s: str) -> str:
    """City text: a bare number next to a name is a house number ('Greda 328',
    'Novoseljanska 9') or a postcode ('4130 Hjelmeland'); a city needs neither."""
    def repl(m: re.Match) -> str:
        before, after = s[:m.start()].split(), s[m.end():].split()
        prev = before[-1].strip(".,;:()#").lower() if before else ""
        nxt = after[0].strip(".,;:()").lower() if after else ""
        return m.group(0) if prev in _NUMBER_AFTER or nxt in _NUMBER_BEFORE else ""

    s = _NUMBER_TOKEN.sub(repl, s)
    s = re.sub(r"\(\s*\)", "", re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", s)))
    return re.sub(r"\s+", " ", s).strip(" .,-;:")


def _country_of(name: str) -> str | None:
    return _COUNTRY_NAMES.get(_ascii_key(name)) if name else None


def _country_field(name: str) -> str | None:
    """A structured country column (IRLP): free-text names plus the ambiguous ones."""
    k = _ascii_key(name or "")
    return _COUNTRY_NAMES.get(k) or _STRUCTURED_COUNTRY_NAMES.get(k)


def _tidy_city(s: str) -> str:
    """Display form of a city: no locators, ZIPs, street addresses or house
    numbers; ALL-CAPS / all-lower text title-cased."""
    words = [w for w in s.replace(_ADDR, " ").split() if not _LOCATOR_TOKEN.match(w)]
    s = re.sub(r"\s+", " ", _POSTCODE.sub("", " ".join(words))).strip(" .,-;:")
    if _PLACEHOLDER_CITY.match(s):
        return ""
    letters = [c for c in s if c.isalpha()]
    if len(letters) > 3 and (s.upper() == s or s.lower() == s):
        s = s.title()
    # last, whatever the case looks like now: title-casing must never turn a
    # missed address into a presentable one ('12 avenue foch' -> '12 Avenue Foch')
    return _drop_house_numbers(_scrub(s, "", ""))[:128]


def _clean_part(p: str) -> str:
    words = [w for w in p.split() if not _LOCATOR_TOKEN.match(w)]
    return re.sub(r"\s+", " ", _POSTCODE.sub("", " ".join(words))).strip(" .,-;:")


def _region_name(p: str) -> tuple[str, str] | None:
    """A full region name -> (country, code): 'Ontario', 'Florida', 'Queensland'."""
    k = _ascii_key(p)
    if k in _CA_PROVINCE_BY_NAME:
        return ("CA", _CA_PROVINCE_BY_NAME[k])
    if k in _AU_STATE_BY_NAME:
        return ("AU", _AU_STATE_BY_NAME[k])
    if k in _US_STATE_BY_NAME:
        return ("US", _US_STATE_BY_NAME[k])
    return None


def _region_code(p: str, country: str | None) -> tuple[str, str] | None:
    """A region abbreviation -> (country, code). Australian codes only when the
    country is already known ('WA' is far more often Washington)."""
    c = p.strip().upper()
    if country in (None, "US") and c in _US_STATE_NAMES:
        return ("US", c)
    if country in (None, "CA") and c in _CA_PROVINCES:
        return ("CA", c)
    if country == "AU" and c in _AU_STATES:
        return ("AU", c)
    return None


def _country_suffix(p: str) -> tuple[str | None, str]:
    """'BC Canada' -> ('CA', 'BC'); 'Germany' -> ('DE', ''); 'New Mexico' -> (None, ...)."""
    if _region_name(p):
        return None, p  # a state/province name, even if it ends like a country
    words = p.split()
    for n in range(min(4, len(words)), 0, -1):
        cc = _country_of(" ".join(words[-n:]))
        if cc:
            return cc, " ".join(words[:-n])
    return None, p


def parse_place(raw, context: str | None = None) -> dict:
    """Free-text location -> {"city", "state", "country", "state_name", "code"}.

    Handles 'City, ST', 'City, State USA', 'City, Country, Region' (RadioID
    style), 'City (UK)', 'Site (City, ST)', ZIP codes and Maidenhead locators.
    `state` is a USPS code for the US, a province/state code for Canada and
    Australia. `state_name` says it was spelled out; `code` is the bare
    abbreviation the country was inferred from ('NL' may be Newfoundland or
    the Netherlands), so the coordinates can overrule it later.

    `context` is a country already known from elsewhere (BrandMeister's
    licence country): abbreviations are then read only as that country's
    regions (Italian 'CA' = Cagliari, not California)."""
    out = {"city": "", "state": None, "country": None, "state_name": False, "code": None}
    s = html.unescape(str(raw or "")).replace(_ADDR, "")
    s = _scrub(s, "", _ADDR).replace("\t", " ").strip()
    if not s.replace(_ADDR, "").strip(" .,-;:"):
        return out
    paren_cc = None
    m = re.search(r"\(([^()]*)\)\s*$", s)
    if m:
        inner, outer = m.group(1).strip(), s[:m.start()].strip(" ,-")
        if _country_of(inner):
            paren_cc, s = _country_of(inner), outer
        elif "," in inner:
            sub = parse_place(inner, context)
            if sub["state"] or sub["country"]:
                return sub if sub["city"] else {**sub, "city": _tidy_city(outer)}
            s = outer
        else:
            s = outer or inner
    parts = [_clean_part(p) for p in re.split(r",|(?<=[A-Za-z])/(?=[A-Za-z])", s)]
    # a first part that was nothing but a street address: the next part is the
    # city ('40 J Testwood Road, Leigh') unless it is already a country or
    # region ('12 Testwood Rd, France, Pays de la Loire' has no city)
    lead_address = bool(parts) and _ADDR in parts[0] and not _clean_part(parts[0].replace(_ADDR, " "))
    parts = [_clean_part(p.replace(_ADDR, " ")) for p in parts]
    if lead_address:
        rest = [p for p in parts[1:] if p]
        if rest and sum(c.isalpha() for c in rest[0]) >= 2 and not (
                _country_of(rest[0]) or _region_name(rest[0]) or _region_code(rest[0], None)):
            city, regions = rest[0], rest[1:]
        else:
            city, regions = "", rest
    else:
        parts = [p for p in parts if p]
        if not parts:
            return out
        city, regions = parts[0], parts[1:]
    country, state, state_name, code = paren_cc, None, False, None

    for p in reversed(regions):  # explicit countries, rightmost first
        cc, rest = _country_suffix(p)
        if cc:
            country = country or cc
            if rest:
                hit = _region_code(rest, country) or _region_name(rest)
                if hit and hit[0] == country:
                    state = hit[1]
            break
    scope = country or context
    if state is None:
        for p in reversed(regions):  # spelled-out regions beat abbreviations
            hit = _region_name(p)
            if hit and scope in (None, hit[0]):
                country, state, state_name = hit[0], hit[1], True
                break
    if state is None:
        for p in reversed(regions):
            hit = _region_code(p, scope)
            if hit and country in (None, hit[0]):
                if scope is None:
                    code = p.strip().upper()  # the abbreviation alone decided the country
                country, state = hit[0], hit[1]
                break
    if state is None and country is None:
        # 'St. Kitts', 'Denver CO USA', 'Lubbock Texas', 'Sullivan MO', 'Madison WI EN53'
        words = city.split()
        if context is None:
            if not regions and _country_of(city):
                country, words = _country_of(city), []
            else:
                for n in range(min(3, len(words) - 1), 0, -1):
                    cc = _country_of(" ".join(words[-n:]))
                    if cc:
                        country, words = cc, words[:-n]
                        break
        scope = country or context
        if len(words) >= 2 and not _region_name(" ".join(words)):  # never split 'West Virginia'
            for n in range(min(3, len(words) - 1), 0, -1):
                hit = _region_name(" ".join(words[-n:]))
                if hit and hit[0] == "US" and scope in (None, "US"):
                    country, state, state_name, words = hit[0], hit[1], True, words[:-n]
                    break
        if state is None and len(words) >= 2 and re.fullmatch(r"[A-Z]{2}", words[-1]):
            hit = _region_code(words[-1], scope)
            if hit:
                country, state, words = hit[0], hit[1], words[:-1]
                code = None if scope else hit[1]
        city = " ".join(words)
    out.update(city=_tidy_city(city), state=state, country=country, state_name=state_name,
               code=code)
    return out


def _city_key(raw) -> str:
    """Letters-only first part of the location text: 'Gosford - Wyong' and
    'Gosford-Wyong' compare equal when counting names at a shared coordinate."""
    s = html.unescape(str(raw or "")).split(",")[0]
    s = re.sub(r"\(.*?\)", " ", s)
    return "".join(re.findall(r"[a-z]+", _ascii_key(s)))


# ============================================================== modes / tones / text

_MODE_PATTERNS = (
    (re.compile(r"D-?STAR", re.I), "DSTAR"),
    (re.compile(r"DMR", re.I), "DMR"),
    (re.compile(r"YSF|FUSION|C4FM", re.I), "YSF"),
    (re.compile(r"P-?25", re.I), "P25"),
    (re.compile(r"NXDN", re.I), "NXDN"),
    (re.compile(r"M17", re.I), "M17"),
    (re.compile(r"\bA?TV\b|ATV", re.I), "ATV"),
    (re.compile(r"TETRA", re.I), "TETRA"),
    (re.compile(r"(^|[^A-Z])N?FM($|[^A-Z])", re.I), "FM"),
    (re.compile(r"AX\.?25|PACKET", re.I), "OTHER"),
)


def _order_modes(modes) -> list[str]:
    return [m for m in MODE_ORDER if m in set(modes)]


def parse_modes(raw) -> list[str]:
    """HearHam free-text mode ('YSF/FM ', 'P25YSFD-STARNXDNDMR/FM') -> canonical list."""
    s = str(raw or "").strip()
    if not s:
        return ["FM"]
    found = {m for rx, m in _MODE_PATTERNS if rx.search(s)}
    return _order_modes(found) or ["OTHER"]


_EMPTY_TONES = {"0", "0.0", "0.00", "NONE", "N/A", "NA", "COS", "CSQ", "CC", "-"}


def parse_tone_field(raw) -> dict:
    """One HearHam encode/decode field -> any of ctcss, dcs, cc, ran, nac, can, burst.
    Values can be combined: '123.0/RAN1/NAC293/C/CAN0', 'CC7/97.4', 'DCS 043', '151.4*'."""
    out: dict = {}
    s = str(raw or "").replace("\xa0", "").replace("\u202f", "").strip()
    for part in re.split(r"[/,;]", s):
        p = part.strip().upper()
        if not p or p in _EMPTY_TONES:
            continue
        m = re.fullmatch(r"CC\s*(\d{1,2})", p)
        if m:
            if int(m.group(1)) <= 15:
                out.setdefault("cc", int(m.group(1)))
            continue
        m = re.fullmatch(r"(?:DCS|DPL|D)\s*([0-7]{2,3})\s*([NI])?\s*\*?", p)
        if m:
            out.setdefault("dcs", m.group(1).zfill(3) + (m.group(2) or "N"))
            continue
        m = re.fullmatch(r"RAN\s*(\d{1,2})", p)
        if m:
            if int(m.group(1)) <= 63:
                out.setdefault("ran", int(m.group(1)))
            continue
        m = re.fullmatch(r"NAC\s*([0-9A-F]{3})", p)
        if m:
            out.setdefault("nac", m.group(1))
            continue
        m = re.fullmatch(r"CAN\s*(\d{1,2})", p)
        if m:
            if int(m.group(1)) <= 15:
                out.setdefault("can", int(m.group(1)))
            continue
        m = re.match(r"(\d{1,4}(?:\.\d{1,2})?)\*?(?:\s|$)", p)
        if m:
            v = float(m.group(1))
            if 60 <= v <= 260:
                out.setdefault("ctcss", round(v, 1))
            elif 1000 <= v <= 3000:
                out.setdefault("burst", v)
    return out


_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
_SIMPLE_TAGS = re.compile(r"</?\s*(?:b|i|u|p|div|span|strong|em|font|a|img)\b[^>]*>", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_notes(raw, limit: int = 500) -> str:
    """Upstream description -> plain text (<br> to newlines, entities decoded,
    trivial tags dropped), at most `limit` chars. Still untrusted: the browser
    escapes it."""
    s = html.unescape(str(raw or ""))
    s = _SIMPLE_TAGS.sub("", _BR.sub("\n", s))
    s = _CONTROL.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))
    s = scrub_private(s)
    s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


_APPROX_HINT = re.compile(r"location\s+(?:is\s+)?approximate", re.I)
_NETWORK_RX = re.compile(r"\bNetwork:\s*([^\n<]{1,60})", re.I)
_ECHOLINK_RX = re.compile(
    r"\becho\s*-?\s*link\b(?:\s*(?:node|no\.?|number|id|#|:|=|-))*\s*#?\s*([0-9]{3,7})\b", re.I)
_WIRESX_RX = re.compile(
    r"\bwires\s*-?\s*x\b(?:\s*(?:node|room|link|to|id|no\.?|number|#|:|=|-))*\s*#?\s*([0-9]{5})\b",
    re.I)


def _dmr_network(desc: str) -> str | None:
    m = _NETWORK_RX.search(desc or "")
    if not m:
        return None
    # returned as-is for networks we don't know, so it gets the notes' privacy scrub
    v = re.sub(r"\s+", " ", scrub_private(m.group(1), marker=False)).strip().strip(".,;")
    k = v.lower().replace(" ", "")
    if not v or k in ("none", "n/a", "na", "-", "unknown"):
        return None
    if k.startswith(("brandm", "bm")):
        return "BrandMeister"
    if k in ("dmr-plus", "dmr+", "dmrplus"):
        return "DMR+"
    if k.startswith("tgif"):
        return "TGIF"
    if k.startswith(("dmr-marc", "dmrmarc")):
        return "DMR-MARC"
    return v[:32]


def _use(raw) -> str:
    t = str(raw or "").strip().lower()
    if not t or t in ("open", "none"):
        return "open"
    return "private" if "private" in t else "closed"


# ============================================================== frequencies

# common repeater splits (MHz); used only to accept a re-scaled offset
_STD_OFFSETS = frozenset({0.1, 0.5, 0.6, 1.0, 1.6, 1.7, 2.0, 3.0, 5.0, 7.6, 9.0, 9.4, 10.0,
                          12.0, 20.0, 25.0, 28.0})
# No amateur band uses a split under 100 kHz, so a smaller non-zero one is a
# unit slip even though it stays in the band: HearHam '-5000' (Hz) meant -5 MHz,
# IRLP's '5.0000' (kHz) meant 5 MHz. It is only re-scaled onto its band's usual
# splits (2026-10 HearHam dump), never onto a rare one: LZ0SAN's 433.5 '-0.01'
# would become -10 MHz, outside Region 1's 70 cm band.
_TINY_SPLIT = 0.1
_BAND_SPLITS = {
    "10m": frozenset({0.1}), "6m": frozenset({0.5, 0.6, 1.0, 1.7}), "2m": frozenset({0.6}),
    "1.25m": frozenset({1.6}), "70cm": frozenset({1.6, 2.0, 5.0, 7.0, 7.6, 9.0, 9.4}),
    "33cm": frozenset({12.0, 25.0}), "23cm": frozenset({6.0, 12.0, 20.0, 28.0}),
}


def clean_output(mhz: float | None) -> float | None:
    """Output frequency in MHz. Values typed in kHz/Hz where MHz was meant
    (446525.0, 147500.0) are scaled back when that lands in a band."""
    if mhz is None or mhz < 1:
        return None
    if _world_band(mhz):
        return round(mhz, 5)
    for k in (1e3, 1e6):
        if _world_band(mhz / k):
            return round(mhz / k, 5)
    return round(mhz, 5) if mhz < 300000 else None


def clean_offset(output: float | None, offset: float | None) -> float | None:
    """Signed input-output split in MHz, repairing the usual upstream slips:
    wrong sign (224.48 +1.6), Hz/kHz scale (147.33 +600, and 447.75 -0.005
    although that one stays in the band), the input typed into the offset
    field (444.0 +449.0). Cross-band inputs that land in any amateur band are
    kept. Returns None when the split can't be made sense of."""
    if output is None or offset is None:
        return None
    if offset == 0:
        return 0.0
    band = _world_band(output)
    if abs(offset) < _TINY_SPLIT:
        if band is None:
            return None
        splits = _BAND_SPLITS.get(band, _STD_OFFSETS)
        for k in (10, 100, 1000):
            for sign in (1, -1):
                o = round(sign * offset * k, 4)
                if abs(o) in splits and _world_band(output + o) == band:
                    return o
        return None
    if _world_band(output + offset):
        return round(offset, 4)
    if band is None:
        return None

    def same(x: float) -> bool:
        return _world_band(x) == band

    if same(output - offset):
        return round(-offset, 4)
    if abs(offset) > 25 and same(abs(offset)):
        return round(abs(offset) - output, 4)
    for k in (10, 100, 1000):
        for sign in (1, -1):
            o = sign * offset / k
            if round(abs(o), 3) in _STD_OFFSETS and same(output + o):
                return round(o, 4)
    return None


def _set_split(r: dict, output: float | None, offset: float | None) -> None:
    r["output_mhz"] = output
    r["offset_mhz"] = clean_offset(output, offset)
    r["input_mhz"] = (round(output + r["offset_mhz"], 5)
                      if output is not None and r["offset_mhz"] is not None else None)


def _kind(r: dict) -> str:
    if r["output_mhz"] is None or r["offset_mhz"] is None:
        return "unknown"
    return "simplex_link" if r["offset_mhz"] == 0 else "repeater"


# ============================================================== normalizers

def _blank(source: str, source_id, url: str, fetched_at: str | None) -> dict:
    return {
        "id": f"{source}:{source_id}", "source": source, "source_id": str(source_id),
        "source_url": url, "name": "", "callsign": "", "output_mhz": None, "input_mhz": None,
        "offset_mhz": None, "band": "other", "modes": [], "tone_up": None, "tone_down": None,
        "dcs": None, "tone_burst": None, "dmr_color_code": None, "dmr_id": None,
        "dmr_network": None, "nxdn_ran": None, "p25_nac": None, "m17_can": None, "city": "",
        "county": "", "state": "", "country": "", "lat": None, "lon": None, "precise": False,
        "location_quality": "unknown", "status": "unknown", "use": "unknown", "kind": "unknown",
        "echolink": None, "allstar": None, "irlp": None, "wiresx": None, "notes": "",
        "last_update": None, "fetched_at": fetched_at or "", "also_in": [],
        # internal, never written to the table
        "_claim_cc": None, "_claim_state": None, "_claim_code": None, "_soft_claim": False,
        "_state_name": False, "_hint_approx": False, "_city_key": "", "_bm_seen": None,
        "_irlp_status": None, "_group": "", "_live_status": False,
    }


def _name(r: dict, label: str) -> str:
    label = scrub_private(label or "", marker=False).strip() or r["callsign"] or "Repeater"
    return (f"{label} {r['output_mhz']:.4f}" if r["output_mhz"] else label)[:255]


def _apply_place(r: dict, place: dict, context: str | None = None, soft: bool = False) -> None:
    """Copy parsed location text onto a record and remember what it claims, so
    the coordinates can be checked against it. A soft claim (BrandMeister's
    licence country) may repair coordinates but never discards precise ones."""
    cc = place["country"] or context
    r["city"] = place["city"]
    r["country"] = cc or ""
    same_country = place["country"] in (None, cc)
    r["state"] = (place["state"] or "") if same_country else ""
    r["_claim_cc"] = cc
    r["_claim_state"] = place["state"] if cc == "US" and same_country and place["state"] else None
    r["_claim_code"] = place.get("code")
    r["_state_name"] = place["state_name"]
    r["_soft_claim"] = soft and r["_claim_state"] is None


def _hearham_record(x, fetched_at: str | None) -> dict | None:
    if not isinstance(x, dict):
        return None
    hid = _digits(x.get("id"), 1, 12)
    if hid is None:
        return None
    r = _blank("hearham", hid, f"https://hearham.com/repeaters/{hid}", fetched_at)
    raw_call = str(x.get("callsign") or "").strip()
    r["callsign"] = _clean_callsign(raw_call)
    hz = _num(x.get("frequency"))
    out_mhz = clean_output(hz / 1e6) if hz and hz >= 1e6 else None
    off_hz = _num(x.get("offset"))
    _set_split(r, out_mhz, off_hz / 1e6 if off_hz is not None else None)
    r["modes"] = parse_modes(x.get("mode"))
    enc, dec = parse_tone_field(x.get("encode")), parse_tone_field(x.get("decode"))
    r["tone_up"], r["tone_down"] = enc.get("ctcss"), dec.get("ctcss")
    for key, col in (("dcs", "dcs"), ("cc", "dmr_color_code"), ("ran", "nxdn_ran"),
                     ("nac", "p25_nac"), ("can", "m17_can"), ("burst", "tone_burst")):
        r[col] = enc[key] if key in enc else dec.get(key)
    group = str(x.get("group") or "").strip()
    node = x.get("internet_node")  # IRLP 4 digits, AllStar 4-6, DMR 6 (2026-10 dump)
    if group == "IRLP":
        r["irlp"] = _digits(node, 1, 5)
    elif group == "Allstar":
        r["allstar"] = _digits(node, 1, 7)
    elif group == "DMR" or "DMR" in r["modes"]:
        r["dmr_id"] = _digits(node, 6, 7)
    _apply_place(r, parse_place(x.get("city")))
    r["_city_key"] = _city_key(x.get("city"))
    r["lat"], r["lon"] = _num(x.get("latitude")), _num(x.get("longitude"))
    op = x.get("operational")
    r["status"] = "on-air" if op in (1, "1", True) else ("off-air" if op in (0, "0", False)
                                                         else "unknown")
    r["use"] = _use(x.get("restriction"))
    desc = str(x.get("description") or "")
    r["notes"] = clean_notes(desc)
    r["_hint_approx"] = bool(_APPROX_HINT.search(desc))
    if "DMR" in r["modes"]:
        r["dmr_network"] = _dmr_network(desc)
    for col, rx in (("echolink", _ECHOLINK_RX), ("wiresx", _WIRESX_RX)):
        m = rx.search(desc)
        if m:
            r[col] = int(m.group(1))
    r["_group"] = group
    r["name"] = _name(r, raw_call)
    r["kind"] = _kind(r)
    return r


def normalize_hearham(items, fetched_at: str | None = None) -> list[dict]:
    """HearHam /api/repeaters/v1 records -> normalized records (research §7.1)."""
    return _one_per_id(_each(items, "HearHam", lambda x: _hearham_record(x, fetched_at)), "HearHam")


def _bm_country(device_id: int) -> str | None:
    return _MCC.get(int(str(device_id)[:3]))  # 6-digit repeater IDs start with the MCC


def _brandmeister_record(x, fetched_at: str | None) -> dict | None:
    if not isinstance(x, dict):
        return None
    did = _digits(x.get("id"), 6, 6)
    if did is None or did < 100000:
        return None
    r = _blank("brandmeister", did, f"https://brandmeister.network/#/device/{did}", fetched_at)
    raw_call = re.sub(r"\s+", " ", str(x.get("callsign") or "")).strip()
    r["callsign"] = _clean_callsign(raw_call)
    tx, rx = _num(x.get("tx")), _num(x.get("rx"))
    out_mhz = clean_output(tx) if tx else None
    in_mhz = clean_output(rx) if rx else None
    _set_split(r, out_mhz, (in_mhz - out_mhz) if out_mhz is not None and in_mhz is not None
               else None)
    r["modes"], r["dmr_id"], r["dmr_network"], r["use"] = ["DMR"], did, "BrandMeister", "open"
    cc = x.get("colorcode")
    r["dmr_color_code"] = cc if isinstance(cc, int) and 0 <= cc <= 15 else None
    city_raw = re.sub(r"\s+", " ", str(x.get("city") or "")).strip()
    mcc_cc = _bm_country(did)
    _apply_place(r, parse_place(city_raw, mcc_cc), mcc_cc, soft=True)
    r["_city_key"] = _city_key(city_raw)
    r["lat"], r["lon"] = _num(x.get("lat")), _num(x.get("lng"))
    try:
        seen = datetime.strptime(str(x.get("last_seen") or ""), "%Y-%m-%d %H:%M:%S")
        r["last_update"] = r["_bm_seen"] = _iso(seen.replace(tzinfo=timezone.utc))
    except ValueError:
        pass
    shown = ", ".join(p for p in (r["city"], r["state"]) if p)
    if city_raw and city_raw != shown:
        r["notes"] = clean_notes(f"Location (BrandMeister): {city_raw}")
    full = scrub_private(raw_call, marker=False).strip()
    r["name"] = full[:255] if full and full.upper() != r["callsign"] else _name(r, r["callsign"])
    r["kind"] = _kind(r)
    return r


def normalize_brandmeister(items, fetched_at: str | None = None) -> list[dict]:
    """BrandMeister /v2/device?repeater=true -> normalized records (research §7.2).
    Only 6-digit repeater IDs are accepted: 7/9-digit IDs are hotspots, i.e.
    somebody's home."""
    return _one_per_id(_each(items, "BrandMeister", lambda x: _brandmeister_record(x, fetched_at)),
                       "BrandMeister")


_IRLP_MAX_TEXT = 64 << 20  # the real file unpacks to ~116 kB; a bz2 bomb stops here


def _irlp_text(payload: bytes) -> str:
    """IRLP status file: bz2 (normally) or plain, latin-1."""
    if payload[:3] == b"BZh":
        d = bz2.BZ2Decompressor()
        payload = d.decompress(payload, max_length=_IRLP_MAX_TEXT)
        if not d.eof:
            raise FetchError("IRLP: status file truncated or too large")
    return payload.decode("latin-1")


def _irlp_record(line: str, header: list[str], fetched_at: str | None) -> dict | None:
    # plain TSV, no quoting: a stray '"' is text, never a field that runs on
    # into the next lines (csv would merge rows, or raise on a NUL / huge field)
    cells = [c.replace('"', "").strip() for c in line.split("\t")]
    d = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(header) if k}
    node = _digits(d.get("Node"), 1, 6)
    if node is None:
        return None  # blank line, footer
    r = _blank("irlp", node, f"https://www.irlp.net/status/index.php?nodeid={node}", fetched_at)
    r["callsign"] = _clean_callsign(d.get("CallSign"))
    freq = _num(d.get("Freq"))
    out_mhz = clean_output(freq) if freq else None
    off_khz = _num(d.get("Offset"))
    _set_split(r, out_mhz, off_khz / 1000.0 if off_khz is not None else None)
    pl = _num(d.get("PL"))
    r["tone_up"] = round(pl, 1) if pl and 60 <= pl <= 260 else None
    r["modes"], r["irlp"], r["use"] = ["FM"], node, "open"
    cc = _country_field(d.get("Country", ""))
    st_raw = d.get("Prov./St", "")
    hit = _region_code(st_raw, cc) or _region_name(st_raw)
    st = hit[1] if hit and hit[0] == (cc or hit[0]) else None
    city = _tidy_city(scrub_private(d.get("City", ""), marker=False))
    _apply_place(r, {"city": city, "state": st,
                     "country": cc or (hit[0] if hit else None),
                     "state_name": bool(hit and len(st_raw) > 3), "code": None})
    r["_city_key"] = _city_key(d.get("City"))
    r["lat"], r["lon"] = _num(d.get("lat")), _num(d.get("long"))
    status = d.get("Status", "").upper()
    linked = _digits(status, 1, 6)  # the node it is connected to
    r["status"] = ("on-air" if status == "IDLE" or linked is not None
                   else "off-air" if status in ("DOWN", "OFFLINE") else "unknown")
    r["_irlp_status"] = r["status"]
    r["last_update"] = _epoch_iso(d.get("Lastupdate"))
    notes = [f"IRLP node {node}"]
    if linked is not None:
        notes.append(f"connected to {status}")  # as IRLP writes it: reflector '0097', not 97
    url = d.get("URL", "")
    if url.lower().startswith(("http://", "https://")) and "@" not in url:
        notes.append(url)
    r["notes"] = clean_notes(" · ".join(notes))
    r["name"] = _name(r, r["callsign"]) if out_mhz else f"{r['callsign'] or 'IRLP'} (IRLP {node})"
    r["kind"] = _kind(r)
    return r


def normalize_irlp(payload: str, fetched_at: str | None = None) -> list[dict]:
    """IRLP nohtmlstatus (tab-separated, Offset in kHz) -> normalized records (§7.3)."""
    lines = payload.replace("\r", "").split("\n")
    header = [h.replace('"', "").strip() for h in lines[0].split("\t")]
    if "Node" not in header:
        raise FetchError("IRLP: unexpected status file header")
    return _one_per_id(_each(lines[1:], "IRLP", lambda line: _irlp_record(line, header, fetched_at)),
                       "IRLP")


# ============================================================== GeoNames places

def _dist_km(la1: float, lo1: float, la2: float, lo2: float) -> float:
    """Equirectangular distance; plenty for the <= 250 km checks here."""
    dlon = (lo2 - lo1 + 180.0) % 360.0 - 180.0
    x = math.radians(dlon) * math.cos(math.radians((la1 + la2) / 2))
    return 6371.0 * math.hypot(x, math.radians(la2 - la1))


def haversine_km(la1: float, lo1: float, la2: float, lo2: float) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


class PlaceIndex:
    """GeoNames populated places (lat, lon, country, admin1) on a 0.1-degree
    grid, packed into flat arrays sorted by cell: ~10 MB for cities500's ~236k
    places instead of ~57 MB as tuples (it is rebuilt a few times a day)."""

    def __init__(self, rows):
        lat, lon, cc, a1, keys = array("i"), array("i"), array("I"), array("I"), array("q")
        names: dict[str, int] = {}
        for la, lo, c, a in rows:
            lat.append(round(la * 1e5))
            lon.append(round(lo * 1e5))
            cc.append(names.setdefault(c, len(names)))
            a1.append(names.setdefault(a, len(names)))
            keys.append(self._key(math.floor(la * 10), math.floor(lo * 10)))
        order = sorted(range(len(keys)), key=keys.__getitem__)
        self._lat = array("i", (lat[k] for k in order))
        self._lon = array("i", (lon[k] for k in order))
        self._cc = array("I", (cc[k] for k in order))
        self._a1 = array("I", (a1[k] for k in order))
        self._names = list(names)  # index -> string, in insertion order
        self._spans: dict[int, int] = {}  # cell key -> start << 20 | count
        first = 0
        for i in range(1, len(order) + 1):
            if i == len(order) or keys[order[i]] != keys[order[first]]:
                self._spans[keys[order[first]]] = (first << 20) | (i - first)
                first = i
        self.size = len(order)

    @staticmethod
    def _key(i: int, j: int) -> int:
        return (i + 1000) * 4000 + (j + 1800) % 3600  # longitude wraps at the antimeridian

    @classmethod
    def from_tsv(cls, data: bytes) -> "PlaceIndex":
        def rows():
            for line in data.decode("utf-8").splitlines():
                p = line.split("\t")
                if len(p) >= 3:
                    try:
                        yield float(p[0]), float(p[1]), p[2], p[3] if len(p) > 3 else ""
                    except ValueError:
                        continue
        return cls(rows())

    def _span(self, i: int, j: int) -> range:
        v = self._spans.get(self._key(i, j))
        if v is None:
            return range(0)
        return range(v >> 20, (v >> 20) + (v & 0xFFFFF))

    def _place(self, k: int) -> tuple[float, float, str, str]:
        return (self._lat[k] / 1e5, self._lon[k] / 1e5, self._names[self._cc[k]],
                self._names[self._a1[k]])

    def near(self, lat: float, lon: float, meters: float) -> bool:
        """Any place within `meters` (a few km at most)."""
        ci, cj = math.floor(lat * 10), math.floor(lon * 10)
        km = meters / 1000.0
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for k in self._span(ci + di, cj + dj):
                    if _dist_km(lat, lon, self._lat[k] / 1e5, self._lon[k] / 1e5) <= km:
                        return True
        return False

    def nearest(self, lat: float, lon: float, max_km: float, pred=None):
        """(distance_km, (lat, lon, country, admin1)) of the nearest place within
        max_km matching `pred`, or None. Searches outward ring by ring and stops
        as soon as no further ring can hold anything closer."""
        ci, cj = math.floor(lat * 10), math.floor(lon * 10)
        edge = min(89.9, abs(lat) + max_km / 111.0)
        cell_km = 11.1 * max(0.02, math.cos(math.radians(edge)))  # narrowest cell side in range
        best = None
        for ring in range(int(max_km / cell_km) + 2):
            if best is not None and best[0] <= (ring - 1) * cell_km:
                break
            for di in range(-ring, ring + 1):
                step = 1 if abs(di) == ring else 2 * ring
                for dj in range(-ring, ring + 1, max(1, step)):
                    for k in self._span(ci + di, cj + dj):
                        d = _dist_km(lat, lon, self._lat[k] / 1e5, self._lon[k] / 1e5)
                        if d > max_km or (best is not None and d >= best[0]):
                            continue
                        p = self._place(k)
                        if pred is None or pred(p):
                            best = (d, p)
        return best


def _places_tsv_from_zip(path: str) -> bytes:
    """GeoNames cities500.zip -> compact 'lat\\tlon\\tcc\\tadmin1' lines."""
    out = io.StringIO()
    n = 0
    with zipfile.ZipFile(path) as zf:
        name = next((m for m in zf.namelist() if m.endswith(".txt")), None)
        if name is None:
            raise FetchError("GeoNames: no .txt member in the archive")
        with zf.open(name) as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                p = line.rstrip("\n").split("\t")
                if len(p) < 11:
                    continue
                try:
                    la, lo = float(p[4]), float(p[5])
                except ValueError:
                    continue
                out.write(f"{la:.5f}\t{lo:.5f}\t{p[8]}\t{p[10]}\n")
                n += 1
    if n < _MIN_PLACES:
        raise FetchError(f"GeoNames: only {n} places in the archive")
    return out.getvalue().encode("utf-8")


# ============================================================== location quality

# Coordinates that stand for "nowhere in particular" (5 dp). Research §9.1:
# HearHam's AllStar default, a mid-Atlantic DMR default, WIA's "Statewide"
# Sydney point, the US centre, and the geocoder's whole-country / whole-state
# centroids (CA, NV, FL and CO appear in the 2026-10 HearHam dump; the rest of
# the set is the same geocoder's state table — an entry that never matches is
# harmless).
_BOGUS_POINTS = frozenset({
    (33.73544, -118.1091), (41.73329, -50.39989), (-33.80234, 150.9247), (39.8355, -99.09083),
    (37.09024, -95.71289),
    (32.31823, -86.9023), (64.20084, -149.49367), (34.04893, -111.09373), (35.20105, -91.83183),
    (36.77826, -119.41793), (39.55005, -105.78207), (41.60322, -73.08775), (38.91083, -75.52767),
    (27.66483, -81.51575), (32.16562, -82.90008), (19.89677, -155.58278), (44.0682, -114.74204),
    (40.63312, -89.39853), (40.26719, -86.1349), (41.878, -93.0977), (39.0119, -98.48425),
    (37.83933, -84.27002), (30.9843, -91.96233), (45.25378, -69.44547), (39.04575, -76.64127),
    (42.40721, -71.38244), (44.31484, -85.60236), (46.72955, -94.6859), (32.35467, -89.39853),
    (37.96425, -91.83183), (46.87968, -110.36257), (41.49254, -99.90181), (38.80261, -116.41939),
    (43.19385, -71.5724), (40.05832, -74.40566), (34.51994, -105.87009), (43.29943, -74.21793),
    (35.75957, -79.0193), (47.55149, -101.002), (40.41729, -82.90712), (35.00775, -97.09288),
    (43.80413, -120.5542), (41.20332, -77.19452), (41.58009, -71.47743), (33.83608, -81.16372),
    (43.96951, -99.90181), (35.51749, -86.58045), (31.9686, -99.90181), (39.32098, -111.09373),
    (44.5588, -72.57784), (37.43157, -78.6569), (47.75107, -120.74014), (38.59763, -80.4549),
    (43.78444, -88.78787), (43.07597, -107.29028), (18.22083, -66.59015),
})
_NOWHERE_KM = 200  # farther than this from any populated place = mid-ocean / garbage
_CLAIM_KM = 150  # a listing's own state/country must have a place this close
_REPAIR_KM = 50  # a repaired coordinate must land this close to the listed state


def _usable(lat, lon) -> bool:
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or lat == 0 or lon == 0:
        return False
    return (round(lat, 4), round(lon, 4)) != (50.0, -3.0)  # Pi-Star default


def _bogus(lat: float, lon: float) -> bool:
    return (round(lat, 5), round(lon, 5)) in _BOGUS_POINTS


def _decimals(x: float) -> int:
    s = repr(float(x))
    if "e" in s or "E" in s:
        return 10
    return len(s.split(".")[1].rstrip("0")) if "." in s else 0


def _no_location(r: dict, why: str, stats: Counter) -> None:
    r["lat"] = r["lon"] = None
    r["precise"] = False
    r["location_quality"] = "unknown"
    stats[f"no_location_{why}"] += 1


def _claim_pred(state: str | None, cc: str | None):
    if state in _US_TERRITORIES:
        return lambda p: p[2] == state
    if state:
        return lambda p: p[2] == "US" and p[3] == state
    if cc:
        group = _group(cc)
        return lambda p: p[2] in group
    return None


def _repair(r: dict, places: PlaceIndex, pred, stats: Counter) -> bool:
    """Try the usual slips: longitude sign dropped, lat/lon swapped (and both).
    A fix must land within 50 km of the listed state; with only a country to
    go on, the nearest place must be in that country and the coordinate must
    carry real digits ('38.0, -95.0' says nothing)."""
    la, lo = r["lat"], r["lon"]
    by_state = r["_claim_state"] is not None
    if not by_state and min(_decimals(la), _decimals(lo)) <= 1:
        return False
    group = _group(r["_claim_cc"])
    for cand in ((la, -lo), (lo, la), (-lo, -la), (-la, lo), (-la, -lo)):
        if not _usable(*cand):
            continue
        if by_state:
            ok = places.nearest(cand[0], cand[1], _REPAIR_KM, pred) is not None
        else:
            near = places.nearest(cand[0], cand[1], _REPAIR_KM)
            ok = near is not None and near[1][2] in group
        if ok:
            r["lat"], r["lon"] = cand
            stats["coords_repaired"] += 1
            return True
    return False


def _reinterpret(r: dict, ncc: str, stats: Counter) -> bool:
    """The listing's 'state' was really something else: an ISO country code
    ('NL' = Netherlands, 'IN' = India), Western Australia, or the country
    Georgia. Only when the coordinates sit in exactly that place."""
    code, st = r["_claim_code"], r["_claim_state"]
    if not ((code and ncc == code) or (code == "WA" and ncc == "AU")
            or (st == "GA" and r["_state_name"] and ncc == "GE")):
        return False
    r["_claim_cc"], r["_claim_state"], r["_claim_code"] = ncc, None, None
    r["country"], r["state"] = ncc, ("WA" if code == "WA" and ncc == "AU" else "")
    stats["claim_reinterpreted"] += 1
    return True


def _check_claim(r: dict, places: PlaceIndex, stats: Counter) -> bool:
    """Does the coordinate agree with the listing's own state/country? Repairs
    sign/swap slips ('Kenosha, Wisconsin' at 42.58, +87.82). Returns False for
    a contradiction nobody can fix ('Hiram, ME' geocoded into Mexico)."""
    pred = _claim_pred(r["_claim_state"], r["_claim_cc"])
    if pred is None:
        return True
    la, lo = r["lat"], r["lon"]
    if places.nearest(la, lo, _CLAIM_KM, pred):
        return True
    closest = places.nearest(la, lo, 250)
    if closest is not None and closest[1][2] in _group(r["_claim_cc"]):
        return True  # sparse corner of the right country (remote Alaska, the outback)
    if _repair(r, places, pred, stats):
        return True
    if closest is None:
        return True  # nowhere near anything: the mid-ocean rule decides
    if _reinterpret(r, closest[1][2], stats):
        return True
    if r["_soft_claim"] and min(_decimals(la), _decimals(lo)) > 1:
        return True  # licence country only; a precise fix abroad is believable
    return False


def classify_locations(recs: list[dict], places: PlaceIndex | None) -> Counter:
    """Research §9.1, applied to one source's records, first match wins:
    no location / placeholder -> unknown; listing contradicts its coordinates
    -> unknown; bogus default (known point, or one point shared by >= 10
    different place names) -> unknown; farther than 200 km from any populated
    place -> unknown; source says approximate -> approximate; <= 2 decimals ->
    approximate; shared by >= 3 callsigns or within 300 m of a place centroid
    -> city; else site. Unknown records lose their coordinates (never plotted)."""
    stats: Counter = Counter()
    live = []
    for r in recs:
        if not _usable(r["lat"], r["lon"]):
            _no_location(r, "missing", stats)
        elif places is not None and not _check_claim(r, places, stats):
            _no_location(r, "contradicts_listing", stats)
        elif _bogus(r["lat"], r["lon"]):
            _no_location(r, "bogus_default", stats)
        else:
            live.append(r)
    names, calls = defaultdict(set), defaultdict(set)
    for r in live:
        k = (round(r["lat"], 5), round(r["lon"], 5))
        if r["_city_key"]:
            names[k].add(r["_city_key"])
        if r["callsign"]:
            calls[k].add(r["callsign"])
    for r in live:
        k = (round(r["lat"], 5), round(r["lon"], 5))
        if len(names[k]) >= 10:
            _no_location(r, "bogus_shared", stats)
            continue
        if (places is not None and r["lat"] > -60  # Antarctic stations have no "places"
                and places.nearest(r["lat"], r["lon"], _NOWHERE_KM) is None):
            _no_location(r, "far_from_places", stats)
            continue
        if r["_hint_approx"] or min(_decimals(r["lat"]), _decimals(r["lon"])) <= 2:
            quality = "approximate"
        elif len(calls[k]) >= 3 or (places is not None and places.near(r["lat"], r["lon"], 300)):
            quality = "city"
        else:
            quality = "site"
        r["location_quality"], r["precise"] = quality, quality == "site"
        stats[quality] += 1
    return stats


# ============================================================== merge / dedupe / finalize

def _ref(r: dict) -> dict:
    return {"source": r["source"], "id": r["id"], "url": r["source_url"]}


def _add_also(target: dict, other: dict) -> None:
    have = {a["id"] for a in target["also_in"]} | {target["id"]}
    for a in [_ref(other)] + list(other.get("also_in") or []):
        if a["id"] not in have:
            target["also_in"].append(a)
            have.add(a["id"])


def _later(a: str | None, b: str | None) -> str | None:
    return max((x for x in (a, b) if x), default=None)


def _take_location(target: dict, other: dict) -> bool:
    """Better coordinates from a matched source: fill missing ones, or replace
    approximate ones with a site fix."""
    if other["lat"] is None:
        return False
    if target["lat"] is None or (not target["precise"] and other["precise"]):
        for k in ("lat", "lon", "precise", "location_quality"):
            target[k] = other[k]
        return True
    return False


def _enrich(t: dict, o: dict, stats: Counter) -> None:
    _add_also(t, o)
    t["last_update"] = _later(t["last_update"], o["last_update"])
    if o["source"] == "brandmeister":
        t["_bm_seen"] = _later(t["_bm_seen"], o["_bm_seen"])
        t["dmr_network"] = "BrandMeister"
        if t["dmr_color_code"] is None:
            t["dmr_color_code"] = o["dmr_color_code"]
    elif o["_irlp_status"]:
        t["_irlp_status"] = o["_irlp_status"]
    if t["output_mhz"] is None and o["output_mhz"] is not None:
        for k in ("output_mhz", "input_mhz", "offset_mhz"):
            t[k] = o[k]
    if _take_location(t, o):
        stats[f"coords_from_{o['source']}"] += 1


def merge(by_source: dict[str, list[dict]], stats: Counter | None = None) -> list[dict]:
    """HearHam is the base; BrandMeister matches on the DMR ID, IRLP on the node
    number (research §3.4). Matches enrich every HearHam row carrying that
    ID; unmatched records are added as their own rows."""
    stats = stats if stats is not None else Counter()
    hh = list(by_source.get("hearham") or [])
    by_dmr, by_node = defaultdict(list), defaultdict(list)
    for r in hh:
        if r["dmr_id"]:
            by_dmr[r["dmr_id"]].append(r)
        if r["irlp"]:
            by_node[r["irlp"]].append(r)
    out = list(hh)
    for name, index, key in (("brandmeister", by_dmr, "dmr_id"), ("irlp", by_node, "irlp")):
        for o in by_source.get(name) or []:
            targets = index.get(o[key])
            if targets:
                stats[f"matched_{name}"] += 1
                for t in targets:
                    _enrich(t, o, stats)
            else:
                stats[f"added_{name}"] += 1
                out.append(o)
    return out


def _resolve_status(r: dict, now: datetime) -> None:
    """Live sources win: IRLP node state, then BrandMeister last-seen (on-air
    if seen within 7 days; a stale BM record never upgrades 'off-air').
    `_live_status` says a live source decided, which dedupe respects."""
    status, live = r["status"], False
    if r["_irlp_status"] in ("on-air", "off-air"):
        status, live = r["_irlp_status"], True
    seen = _parse_iso(r["_bm_seen"])
    if seen is not None:
        if now - seen < timedelta(days=_BM_ONAIR_DAYS):
            status, live = "on-air", True
        elif status != "off-air":
            status, live = "unknown", False
    r["status"], r["_live_status"] = status, live


_STATUS_RANK = {"on-air": 2, "testing": 1, "unknown": 1, "off-air": 0}
_SOURCE_RANK = {"hearham": 3, "brandmeister": 2, "irlp": 1}
_FILL_FIELDS = ("tone_up", "tone_down", "dcs", "tone_burst", "dmr_color_code", "dmr_id",
                "dmr_network", "nxdn_ran", "p25_nac", "m17_can", "echolink", "allstar", "irlp",
                "wiresx", "last_update")
_INFO_FIELDS = _FILL_FIELDS + ("input_mhz", "notes", "city", "state")


def _preference(r: dict) -> tuple:
    filled = sum(1 for f in _INFO_FIELDS if r.get(f) not in (None, ""))
    return (bool(r["precise"]), _STATUS_RANK.get(r["status"], 1), filled,
            _SOURCE_RANK.get(r["source"], 0))


def _merge_status(keep: dict, drop: dict) -> None:
    """Two listings of one machine. The kept copy is chosen for its location,
    so a duplicate may only raise its status (on-air > unknown > off-air): one
    stale listing must not hide a repeater that another listing, or a live
    source, puts on the air. The exception is the kept copy's own live verdict
    (IRLP says its node is down): a mere listing doesn't overturn that, just
    as it wouldn't on a single record."""
    if _STATUS_RANK.get(drop["status"], 1) <= _STATUS_RANK.get(keep["status"], 1):
        return
    if keep.get("_live_status") and not drop.get("_live_status"):
        return
    keep["status"] = drop["status"]


def _absorb(keep: dict, drop: dict) -> None:
    _add_also(keep, drop)
    _merge_status(keep, drop)
    for f in _FILL_FIELDS:
        if keep.get(f) in (None, "") and drop.get(f) not in (None, ""):
            keep[f] = drop[f]
    # a missing split, or an AllStar / simplex-node listing's 0 beside the
    # repeater's own listing with its real split (else: a hidden simplex link)
    if drop["offset_mhz"] is not None and (
            keep["offset_mhz"] is None or (keep["offset_mhz"] == 0 and drop["offset_mhz"] != 0)):
        keep["offset_mhz"] = drop["offset_mhz"]
        keep["input_mhz"] = round(keep["output_mhz"] + drop["offset_mhz"], 5)
    keep["modes"] = _order_modes(set(keep["modes"]) | set(drop["modes"]))
    for f in ("city", "state", "notes"):
        if not keep[f] and drop[f]:
            keep[f] = drop[f]


def dedupe(recs: list[dict], stats: Counter | None = None) -> list[dict]:
    """Same callsign, output within ±2.5 kHz, < 30 km apart = one repeater
    (research §3.4). Keep precise over approximate, on-air over off-air, then
    the fuller record; the others go into also_in, fill its gaps, and lend it
    a better status or the real split (_merge_status, _absorb)."""
    stats = stats if stats is not None else Counter()
    groups, out = defaultdict(list), []
    for r in recs:
        if r["callsign"] and r["output_mhz"] is not None and r["lat"] is not None:
            groups[r["callsign"]].append(r)
        else:
            out.append(r)
    for rs in groups.values():
        rs.sort(key=lambda r: r["id"])
        rs.sort(key=_preference, reverse=True)
        kept: list[dict] = []
        for r in rs:
            for k in kept:
                if (abs(k["output_mhz"] - r["output_mhz"]) <= 0.0025 + 1e-9
                        and haversine_km(k["lat"], k["lon"], r["lat"], r["lon"]) < 30):
                    _absorb(k, r)
                    stats["deduped"] += 1
                    break
            else:
                kept.append(r)
        out.extend(kept)
    return out


def _in_us_box(lat: float, lon: float) -> bool:
    return ((24.4 <= lat <= 49.4 and -124.9 <= lon <= -66.8) or (51 <= lat <= 72 and -170 <= lon <= -129)
            or (18.5 <= lat <= 22.5 and -161 <= lon <= -154)
            or (17.8 <= lat <= 18.6 and -67.3 <= lon <= -65.2))


def _final_country(r: dict, places: PlaceIndex | None) -> str:
    claim = r["_claim_cc"]
    if r["lat"] is None:
        return claim or ""
    if places is None:
        return claim or ("US" if _in_us_box(r["lat"], r["lon"]) else "")
    near = places.nearest(r["lat"], r["lon"], 250)
    ncc = near[1][2] if near else None
    if claim:
        if ncc in _group(claim):
            return ncc  # PR / VI / Jersey are more specific than "US" / "UK"
        if places.nearest(r["lat"], r["lon"], _CLAIM_KM, _claim_pred(None, claim)):
            return claim  # border sites: Otay Mountain is "San Diego, CA", not Mexico
    return ncc or claim or ""


def finalize(recs: list[dict], places: PlaceIndex | None) -> None:
    """Country, state fill-in, band (US edges for US records), kind, modes order."""
    for r in recs:
        r["country"] = _final_country(r, places)
        us = r["country"] in US_JURISDICTION
        if r["country"] != "US" and r["state"] and r["_claim_cc"] == "US" and not us:
            r["state"] = ""
        if not r["state"] and r["lat"] is not None and places is not None and r["country"] in (
                "US", "CA", "AU"):
            cc = r["country"]
            near = places.nearest(r["lat"], r["lon"], 60, lambda p, cc=cc: p[2] == cc)
            if near:
                a1 = near[1][3]
                r["state"] = _GEONAMES_ADMIN1.get(cc, {}).get(a1, a1 if cc == "US" else "")
        r["band"] = band_of(r["output_mhz"], us)
        r["kind"] = _kind(r)
        r["modes"] = _order_modes(r["modes"]) or ["OTHER"]


def build(by_source: dict[str, list[dict]], places: PlaceIndex | None,
          now: datetime | None = None) -> tuple[list[dict], Counter]:
    """Pure pipeline: per-source location classes -> merge -> status -> dedupe -> finalize."""
    now = now or _now()
    stats: Counter = Counter()
    # sets stored by older code may still list an ID twice
    by_source = {name: _one_per_id(by_source.get(name) or [], name) for name in SOURCES}
    for name in SOURCES:
        for k, v in classify_locations(by_source.get(name) or [], places).items():
            stats[f"{name}.{k}"] += v
    merged = merge(by_source, stats)
    for r in merged:
        _resolve_status(r, now)
    merged = dedupe(merged, stats)
    finalize(merged, places)
    merged.sort(key=lambda r: r["id"])
    return merged, stats


# ============================================================== storage

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS repeater_sources (
    source TEXT PRIMARY KEY,
    fetched_at TEXT,
    ok INTEGER,
    count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    last_ok_at TEXT,
    retry_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    jitter_s REAL NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    records BLOB
)"""
_STATE_COLS = ("source", "fetched_at", "ok", "count", "error", "last_ok_at", "retry_at",
               "failures", "jitter_s", "version")
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_repeaters_lat_lon ON repeaters (lat, lon)",
    "CREATE INDEX IF NOT EXISTS ix_repeaters_callsign ON repeaters (callsign)",
    "CREATE INDEX IF NOT EXISTS ix_repeaters_band ON repeaters (band)",
    "CREATE INDEX IF NOT EXISTS ix_repeaters_dmr_id ON repeaters (dmr_id)",
)


def _model():
    from app.models import Repeater
    return Repeater


def _columns() -> list[str]:
    return [c.name for c in _model().__table__.columns]


def ensure_tables() -> None:
    with engine.begin() as conn:
        conn.execute(text(_STATE_DDL))
    _model().__table__.create(engine, checkfirst=True)


def _load_states() -> dict[str, dict]:
    with engine.begin() as conn:
        rows = conn.execute(text(f"SELECT {', '.join(_STATE_COLS)} FROM repeater_sources")).all()
    return {r[0]: dict(zip(_STATE_COLS, r)) for r in rows}


def _load_blob(source: str) -> bytes | None:
    with engine.begin() as conn:
        blob = conn.execute(text("SELECT records FROM repeater_sources WHERE source = :s"),
                            {"s": source}).scalar()
    return zlib.decompress(blob) if blob else None


def load_records(source: str) -> list[dict]:
    """The last good normalized set for one source ([] if never fetched)."""
    raw = _load_blob(source)
    return json.loads(raw) if raw else []


def _upsert_state(source: str, values: dict) -> None:
    cols = ["source"] + list(values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    sql = (f"INSERT INTO repeater_sources ({', '.join(cols)}) VALUES "
           f"({', '.join(':' + c for c in cols)}) ON CONFLICT(source) DO UPDATE SET {sets}")
    with engine.begin() as conn:
        conn.execute(text(sql), {"source": source, **values})


def _pack_records(records: list[dict]) -> bytes:
    """A source's normalized set as zlib-compressed JSON (HearHam: ~23 MB -> ~1.8 MB).
    One json.dumps beats a streamed encoder here: the stdlib's iterencode falls
    back to pure Python and its chunks cost ~8x more memory."""
    return zlib.compress(json.dumps(records, separators=(",", ":")).encode(), 6)


def _save_success(source: str, blob: bytes, count: int, now: datetime,
                  jitter_s: float = 0.0, version: int | None = None) -> None:
    """Store a source's new good set (`blob` is zlib-compressed)."""
    _upsert_state(source, {
        "fetched_at": _iso(now), "ok": 1, "count": count, "error": None,
        "last_ok_at": _iso(now), "retry_at": None, "failures": 0, "jitter_s": jitter_s,
        "version": NORMALIZER_VERSION if version is None else version,
        "records": blob,
    })


def _backoff_s(failures: int, retry_after: float | None) -> float:
    """15 min doubling to 6 h, or the server's Retry-After when that is longer
    (at most a day: one bad header must not park a source for years)."""
    ra = min(retry_after, _MAX_RETRY_AFTER_S) if retry_after and math.isfinite(retry_after) else 0.0
    return max(ra, min(_MIN_BACKOFF_S * 2 ** max(0, failures - 1), _MAX_BACKOFF_S))


def _save_failure(source: str, prev: dict | None, exc: Exception, now: datetime) -> None:
    failures = (prev or {}).get("failures", 0) + 1
    retry_after = getattr(exc, "retry_after", None)
    _upsert_state(source, {
        "fetched_at": _iso(now), "ok": 0, "error": str(exc)[:500] or exc.__class__.__name__,
        "failures": failures,
        "retry_at": _iso(now + timedelta(seconds=_backoff_s(failures, retry_after))),
    })


def _repeater_count() -> int | None:
    try:
        with engine.begin() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM repeaters")).scalar()
    except Exception:
        return None


def _schema_ok() -> bool:
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(repeaters)"))]
    return cols == _columns()


def _create_import_sql() -> str:
    ddl = str(CreateTable(_model().__table__).compile(dialect=engine.dialect)).strip()
    return ddl.replace("CREATE TABLE repeaters ", "CREATE TABLE repeaters_import ", 1)


_INT64 = 1 << 63


def _db_value(v):
    """A value SQLite can store. An over-long integer or an unencodable string
    from upstream costs that one value, never the whole swap (which would
    otherwise fail on every retry, freezing the map)."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        return _safe_text(v)
    if isinstance(v, int):
        return v if -_INT64 <= v < _INT64 else None
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return None  # nothing else belongs in a column


def _row(r: dict, cols: list[str], talkgroups: dict[str, str]) -> tuple:
    vals = dict(r)
    vals["modes"] = ",".join(r["modes"])
    vals["also_in"] = json.dumps(r["also_in"], separators=(",", ":")) if r["also_in"] else None
    vals["precise"] = 1 if r["precise"] else 0
    vals["talkgroups"] = talkgroups.get(r["id"])
    for k in ("name", "callsign", "city", "county", "state", "country", "notes", "fetched_at"):
        vals[k] = vals.get(k) or ""
    vals["name"] = vals["name"][:255]
    return tuple(_db_value(vals.get(c)) for c in cols)


def _fresh_talkgroups(conn, now: datetime) -> dict[str, str]:
    """Talkgroup caches younger than 24 h survive the table swap."""
    keep = {}
    try:
        rows = conn.execute(text("SELECT id, talkgroups FROM repeaters WHERE talkgroups IS NOT NULL"))
        for rid, raw in rows:
            try:
                at = _parse_iso(json.loads(raw).get("at"))
            except (ValueError, AttributeError, TypeError):
                continue
            if at and (now - at).total_seconds() < _TALKGROUP_TTL_S:
                keep[rid] = raw
    except Exception:
        pass  # no table yet
    return keep


def write_table(recs: list[dict], now: datetime | None = None) -> int:
    """Build repeaters_import and swap it in, like the ULS import, so the map
    keeps serving the old rows until the new ones are complete."""
    if not recs:
        return 0  # never swap the map for nothing (and keep the swap transactional)
    now = now or _now()
    cols = _columns()
    insert = (f"INSERT INTO repeaters_import ({', '.join(cols)}) "
              f"VALUES ({', '.join('?' for _ in cols)})")
    with engine.begin() as conn:
        talkgroups = _fresh_talkgroups(conn, now)
        conn.execute(text("DROP TABLE IF EXISTS repeaters_import"))
        conn.execute(text(_create_import_sql()))
        for i in range(0, len(recs), 5000):
            conn.exec_driver_sql(insert, [_row(r, cols, talkgroups) for r in recs[i:i + 5000]])
        conn.execute(text("DROP TABLE IF EXISTS repeaters"))
        conn.execute(text("ALTER TABLE repeaters_import RENAME TO repeaters"))
        for ddl in _INDEXES:
            conn.execute(text(ddl))
    return len(recs)


def load_places() -> PlaceIndex | None:
    raw = _load_blob(PLACES)
    if not raw:
        return None
    try:
        return PlaceIndex.from_tsv(raw)
    except Exception:
        log.exception("[repeaters] stored GeoNames places unreadable")
        return None


last_build_stats: dict = {}  # counters from the most recent rebuild (logs / diagnostics)


def _rescrub(recs: list[dict]) -> None:
    """Today's privacy rules on a stored set that older code normalized: it is
    still used until that source's re-fetch succeeds."""
    for r in recs:
        r["city"] = _tidy_city(r.get("city") or "")
        r["name"] = (scrub_private(r.get("name") or "", marker=False).strip()
                     or r.get("callsign") or "Repeater")
        r["notes"] = scrub_private(r.get("notes") or "")


def rebuild(now: datetime | None = None) -> int:
    """Rebuild the merged table from every source's last good set (no network)."""
    now = now or _now()
    t0 = time.monotonic()
    states = _load_states()
    by_source = {name: load_records(name) for name in SOURCES}
    for name, recs in by_source.items():
        if recs and (states.get(name) or {}).get("version") != NORMALIZER_VERSION:
            _rescrub(recs)
    if not any(by_source.values()):
        return 0
    places = load_places()
    merged, stats = build(by_source, places, now)
    n = write_table(merged, now)
    _upsert_state(_BUILD, {"fetched_at": _iso(now), "ok": 1 if places is not None else 0,
                           "count": n, "version": BUILD_VERSION, "error": None, "failures": 0,
                           "retry_at": None})
    on_map = sum(1 for r in merged if r["lat"] is not None)
    log.info("[repeaters] rebuilt %d rows (%d mappable; %s) in %.1fs%s", n, on_map,
             ", ".join(f"{k}={v}" for k, v in sorted(stats.items())), time.monotonic() - t0,
             "" if places is not None else " — without GeoNames places (degraded)")
    global last_build_stats
    last_build_stats = dict(stats)
    return n


# ============================================================== fetching

def _data_dir() -> str:
    url = get_settings().database_url
    path = url.split("///", 1)[-1] if "///" in url else "/data/hamdoor.db"
    return os.path.dirname(path) or "."


def _client(timeout: float | None = None) -> httpx.Client:
    s = get_settings()
    t = httpx.Timeout(timeout or max(30.0, s.http_timeout_seconds), connect=s.http_timeout_seconds)
    return httpx.Client(timeout=t, follow_redirects=True,
                        headers={"User-Agent": s.http_user_agent})


def _retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (a delay or an HTTP date), at
    most a day; None when absent or nonsense ('inf', 'nan', a bad date). An
    unbounded value parked a source for years, or overflowed the failure
    bookkeeping so that nothing backed off at all."""
    if not value:
        return None
    try:
        secs = float(value)
    except ValueError:
        try:
            secs = (parsedate_to_datetime(value) - _now()).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(secs):
        return None
    return min(max(0.0, secs), _MAX_RETRY_AFTER_S)


def _check(resp: httpx.Response, label: str) -> None:
    if resp.status_code == 200:
        return
    ra = _retry_after(resp.headers.get("Retry-After")) if resp.status_code in (429, 503) else None
    raise FetchError(f"{label}: HTTP {resp.status_code}", retry_after=ra)


def _get_bytes(url: str, label: str, max_bytes: int) -> bytes:
    try:
        with _client() as client, client.stream("GET", url) as resp:
            _check(resp, label)
            buf = bytearray()
            for chunk in resp.iter_bytes(1 << 16):
                buf += chunk
                if len(buf) > max_bytes:
                    raise FetchError(f"{label}: response larger than {max_bytes >> 20} MB")
            return bytes(buf)
    except httpx.HTTPError as exc:
        raise FetchError(f"{label}: {exc.__class__.__name__}: {exc}") from exc


def _download(url: str, dest: str, label: str, max_bytes: int, max_seconds: float = 600) -> None:
    """Stream to a temp file in the data dir (the HearHam dump is ~10 MB, GeoNames ~14 MB)."""
    tmp = dest + ".partial"
    deadline = time.monotonic() + max_seconds
    try:
        with _client() as client, client.stream("GET", url) as resp:
            _check(resp, label)
            n = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    n += len(chunk)
                    if n > max_bytes:
                        raise FetchError(f"{label}: download larger than {max_bytes >> 20} MB")
                    if time.monotonic() > deadline:
                        raise FetchError(f"{label}: download took longer than {max_seconds:.0f}s")
                    fh.write(chunk)
        os.replace(tmp, dest)
    except httpx.HTTPError as exc:
        raise FetchError(f"{label}: {exc.__class__.__name__}: {exc}") from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def brandmeister_list_url(url: str) -> str:
    """Always ask for repeater=true: without it /v2/device lists hotspots,
    which sit in people's homes."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() != "repeater"]
    query.append(("repeater", "true"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _bm_device_base() -> str:
    p = urlsplit(get_settings().brandmeister_url)
    i = p.path.find("/device")
    path = p.path[: i + len("/device")] if i >= 0 else "/v2/device"
    return urlunsplit((p.scheme or "https", p.netloc or "api.brandmeister.network", path, "", ""))


def fetch_hearham(now: datetime) -> list[dict]:
    dest = os.path.join(_data_dir(), ".repeaters-hearham.json")
    try:
        _download(get_settings().hearham_url, dest, "HearHam", 200 << 20)
        with open(dest, "rb") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise FetchError(f"HearHam: invalid JSON ({exc})") from exc
    finally:
        if os.path.exists(dest):
            os.remove(dest)
    if not isinstance(data, list):
        raise FetchError("HearHam: expected a JSON list of repeaters")
    return normalize_hearham(_drain(data), _iso(now))


def _drain(items: list):
    """Yield list items in order, dropping each from the list as it goes, so the
    raw upstream objects are freed while their normalized copies are built."""
    items.reverse()
    while items:
        yield items.pop()


def fetch_brandmeister(now: datetime) -> list[dict]:
    raw = _get_bytes(brandmeister_list_url(get_settings().brandmeister_url), "BrandMeister",
                     64 << 20)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise FetchError(f"BrandMeister: invalid JSON ({exc})") from exc
    if not isinstance(data, list):
        raise FetchError("BrandMeister: expected a JSON list of devices")
    return normalize_brandmeister(data, _iso(now))


def fetch_irlp(now: datetime) -> list[dict]:
    raw = _get_bytes(get_settings().irlp_url, "IRLP", 16 << 20)
    try:
        text_ = _irlp_text(raw)
    except (OSError, EOFError, ValueError) as exc:
        raise FetchError(f"IRLP: unreadable status file ({exc})") from exc
    return normalize_irlp(text_, _iso(now))


def fetch_places() -> bytes:
    s = get_settings()
    dest = os.path.join(_data_dir(), ".repeaters-cities500.zip")
    try:
        _download(s.geonames_cities_url, dest, "GeoNames", 128 << 20)
        return _places_tsv_from_zip(dest)
    except zipfile.BadZipFile as exc:
        raise FetchError(f"GeoNames: not a zip archive ({exc})") from exc
    finally:
        if os.path.exists(dest):
            os.remove(dest)


_FETCHERS = {"hearham": fetch_hearham, "brandmeister": fetch_brandmeister, "irlp": fetch_irlp}


def _interval_s(source: str) -> float:
    s = get_settings()
    return {"hearham": s.repeaters_refresh_days * 86400.0,
            "brandmeister": s.brandmeister_refresh_hours * 3600.0,
            "irlp": s.irlp_refresh_hours * 3600.0,
            PLACES: _PLACES_REFRESH_DAYS * 86400.0}[source]


def next_due(source: str, st: dict | None) -> datetime | None:
    """When a source should next be fetched (None = now)."""
    if st is None:
        return None
    if st["ok"] == 0 and st["retry_at"]:
        retry, failed = _parse_iso(st["retry_at"]), _parse_iso(st["fetched_at"])
        if retry is not None and failed is not None:  # a retry_at stored before the cap
            retry = min(retry, failed + timedelta(seconds=_MAX_RETRY_AFTER_S))
        return retry
    last_ok = _parse_iso(st["last_ok_at"])
    if last_ok is None:
        return None
    if source in SOURCES and st["version"] != NORMALIZER_VERSION:
        return None
    return last_ok + timedelta(seconds=_interval_s(source) + (st["jitter_s"] or 0.0))


def _is_due(source: str, st: dict | None, now: datetime) -> bool:
    due = next_due(source, st)
    return due is None or now >= due


def _jitter_s(source: str) -> float:
    return random.uniform(0, _HEARHAM_JITTER_S) if source == "hearham" else 0.0


def _refresh_source(name: str, st: dict | None, now: datetime) -> bool:
    t0 = time.monotonic()
    try:
        recs = _FETCHERS[name](now)
        prev = (st or {}).get("count") or 0
        if not recs:
            raise FetchError(f"{name}: no usable records")
        if prev >= 100 and len(recs) < prev * _SHRINK_GUARD:
            raise FetchError(f"{name}: only {len(recs)} records (last good set had {prev}); "
                             "keeping the old set")
        _save_success(name, _pack_records(recs), len(recs), now, _jitter_s(name))
        log.info("[repeaters] %s: %d records (%.1fs)", name, len(recs), time.monotonic() - t0)
        return True
    except Exception as exc:
        _save_failure(name, st, exc, now)
        log.warning("[repeaters] %s fetch failed (%s); keeping its last good set", name, exc)
        return False


def _refresh_places(st: dict | None, now: datetime) -> bool:
    """GeoNames cities500 once (then every 90 days). Its failures back off on
    their own and never fail the refresh: repeaters still build without it."""
    if not _is_due(PLACES, st, now):
        return False
    t0 = time.monotonic()
    try:
        tsv = fetch_places()
        n = tsv.count(b"\n")
        _save_success(PLACES, zlib.compress(tsv, 6), n, now, version=1)
        log.info("[repeaters] GeoNames: %d populated places (%.1fs)", n, time.monotonic() - t0)
        return True
    except Exception as exc:
        _save_failure(PLACES, st, exc, now)
        log.warning("[repeaters] GeoNames places fetch failed (%s); will retry", exc)
        return False


def _needs_rebuild(states: dict[str, dict]) -> bool:
    """The table must be at least as new as the newest successful fetch (a
    rebuild that failed after a fetch is retried), built by this code version,
    with this schema, and not lost."""
    if not any((states.get(n) or {}).get("count") for n in SOURCES):
        return False
    meta = states.get(_BUILD)
    if meta is None or meta["version"] != BUILD_VERSION or not _schema_ok():
        return True
    newest = max((st["last_ok_at"] for n in SOURCES + (PLACES,)
                  if (st := states.get(n)) and st["last_ok_at"]), default="")
    if (meta["fetched_at"] or "") < newest:
        return True
    return bool(meta["count"]) and not _repeater_count()


def _save_build_failure(meta: dict | None, exc: Exception, now: datetime) -> None:
    """A failed rebuild backs off like a failed fetch (15 min doubling to 6 h)
    and /status says why; built_at and the served table stay as they were."""
    failures = ((meta or {}).get("failures") or 0) + 1
    _upsert_state(_BUILD, {
        "error": f"{exc.__class__.__name__}: {exc}"[:500], "failures": failures,
        "retry_at": _iso(now + timedelta(seconds=_backoff_s(failures, None))),
    })


_refresh_lock = threading.Lock()


def ensure_fresh() -> bool:
    """Refresh every source whose interval has elapsed, then rebuild the merged
    table if anything changed. Returns False when a due fetch or the rebuild
    failed (or a failed rebuild is still backing off), so the scheduler
    retries in 15 minutes."""
    if not get_settings().repeaters_enabled:
        return True
    with _refresh_lock:
        ensure_tables()
        now = _now()
        states = _load_states()
        changed = _refresh_places(states.get(PLACES), now)
        ok = True
        for name in SOURCES:
            if _is_due(name, states.get(name), now):
                if _refresh_source(name, states.get(name), now):
                    changed = True
                else:
                    ok = False
        states = _load_states()
        if changed or _needs_rebuild(states):
            meta = states.get(_BUILD)
            retry_at = _parse_iso((meta or {}).get("retry_at"))
            if not changed and retry_at is not None and now < retry_at:
                return False  # the last rebuild failed; not time to try again yet
            try:
                rebuild(now)
            except Exception as exc:
                log.exception("[repeaters] rebuild failed")
                _save_build_failure(meta, exc, now)
                ok = False
        return ok


def source_status() -> dict:
    """Per-source state for /api/repeaters/status."""
    try:
        states = _load_states()
    except Exception:
        states = {}  # nothing fetched yet on this database
    out = {}
    for name in SOURCES + (PLACES,):
        st = states.get(name)
        due = next_due(name, st)
        out[name] = {
            "fetched_at": st["fetched_at"] if st else None,
            "ok": None if not st or st["ok"] is None else bool(st["ok"]),
            "count": st["count"] if st else 0,
            "error": st["error"] if st else None,
            "last_ok_at": st["last_ok_at"] if st else None,
            "next_due": _iso(due) if due else None,
        }
    meta = states.get(_BUILD)
    out[_BUILD] = {"built_at": meta["fetched_at"] if meta else None,
                   "with_places": bool(meta and meta["ok"]),
                   "error": meta["error"] if meta else None,
                   "retry_at": meta["retry_at"] if meta else None}
    return out


# ============================================================== talkgroups (on demand)

# BrandMeister advertises x-ratelimit-limit: 60 (per minute). Popups from many
# users, or a script walking every repeater ID, must stay well inside that,
# and a 429/5xx pauses every lookup (not just that one repeater's).
_TG_PAUSE_MIN_S = 600
_TG_PAUSE_MAX_S = 24 * 3600  # a garbage Retry-After can't switch lookups off for good
_TG_PER_MINUTE = 30

_tg_failures: dict[int, float] = {}  # dmr_id -> monotonic time its negative cache ends
_tg_pause_until = 0.0  # monotonic; no lookups at all before this
_tg_recent: deque = deque()  # monotonic times of the upstream lookups in the last minute
_tg_lock = threading.Lock()


def _mono() -> float:
    return time.monotonic()  # its own name so tests can cross pause / rate windows


def reset_talkgroup_limits() -> None:
    """Forget negative caches, pauses and the rate window (tests)."""
    global _tg_pause_until
    with _tg_lock:
        _tg_failures.clear()
        _tg_recent.clear()
        _tg_pause_until = 0.0


def fetch_talkgroups(dmr_id: int) -> list[dict] | None:
    """Static talkgroups of one BrandMeister repeater, or None when BrandMeister
    has nothing usable for it. Raises FetchError (with Retry-After) when
    BrandMeister is rate-limiting, failing or unreachable: that pauses every
    lookup, not just this one."""
    url = f"{_bm_device_base()}/{int(dmr_id)}/talkgroup"
    try:
        with _client(timeout=get_settings().http_timeout_seconds) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise FetchError(f"BrandMeister talkgroups: {exc.__class__.__name__}") from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise FetchError(f"BrandMeister talkgroups: HTTP {resp.status_code}",
                         retry_after=_retry_after(resp.headers.get("Retry-After")))
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    seen, out = set(), []
    for t in data:
        if not isinstance(t, dict):
            continue
        try:
            tg, slot = int(str(t.get("talkgroup")).strip()), int(str(t.get("slot")).strip())
        except (TypeError, ValueError):
            continue
        if (slot, tg) not in seen:
            seen.add((slot, tg))
            out.append({"slot": slot, "tg": tg, "name": None})
    return sorted(out, key=lambda t: (t["slot"], t["tg"]))


def talkgroups_for(repeater_id: str, dmr_id: int | None, cached: str | None) -> list[dict]:
    """Talkgroups for a BrandMeister repeater: the row's cache if younger than
    24 h, else one live fetch (stored back in the row). Failures give []. A
    repeater BrandMeister has nothing for is not asked again for 10 minutes;
    a 429/5xx/network error pauses every lookup for max(Retry-After, 10 min);
    and at most 30 lookups a minute leave this server."""
    global _tg_pause_until
    if not dmr_id:
        return []
    now = _now()
    if cached:
        try:
            c = json.loads(cached)
            at = _parse_iso(c.get("at"))
            if at and (now - at).total_seconds() < _TALKGROUP_TTL_S:
                return list(c.get("talkgroups") or [])
        except (ValueError, AttributeError, TypeError):
            pass
    with _tg_lock:
        mono = _mono()
        if _tg_pause_until > mono or _tg_failures.get(dmr_id, 0) > mono:
            return []
        while _tg_recent and _tg_recent[0] <= mono - 60:
            _tg_recent.popleft()
        if len(_tg_recent) >= _TG_PER_MINUTE:
            return []  # over this server's own budget; the next popup can try again
        _tg_recent.append(mono)
    try:
        tgs = fetch_talkgroups(dmr_id)
    except FetchError as exc:
        pause = min(max(exc.retry_after or 0.0, _TG_PAUSE_MIN_S), _TG_PAUSE_MAX_S)
        with _tg_lock:
            _tg_pause_until = max(_tg_pause_until, _mono() + pause)
        log.warning("[repeaters] %s; pausing talkgroup lookups for %d min", exc, pause // 60)
        return []
    if tgs is None:
        with _tg_lock:
            _tg_failures[dmr_id] = _mono() + _TALKGROUP_FAIL_TTL_S
        return []
    payload = json.dumps({"at": _iso(now), "talkgroups": tgs}, separators=(",", ":"))
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE repeaters SET talkgroups = :t WHERE id = :id"),
                         {"t": payload, "id": repeater_id})
    except Exception:
        log.warning("[repeaters] could not cache talkgroups for %s", repeater_id, exc_info=True)
    return tgs
