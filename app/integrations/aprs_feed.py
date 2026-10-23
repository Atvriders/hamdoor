"""Live APRS traffic: one persistent, receive-only APRS-IS connection
feeding an in-memory store of recent stations and their tracks.

Why one long-lived connection: aprs.fi's API has no area query and its terms
forbid scraping, so "what is on the air in this map view" has to come from
APRS-IS itself, the feed aprs.fi ingests. A North America box is ~30 packets/s
(~3 KB/s); a 15-second sample per request saw ~5% of what an 8-minute window
holds. The store keeps the last position of every station/object for
`aprs_retention_hours`, plus short track tails, indexed by 1-degree cells.

Etiquette (aprs-is.net, aprsc): `pass -1` (receive-only, never a user's
passcode), a unique non-callsign login, fresh DNS on every connect, drain the
socket continuously (aprsc drops slow readers), a `#` keepalive every 15 min
(aprsc drops silent clients after 48 h), exponential backoff 5 s -> 5 min, and
a refused login is never retried in a tight loop.
"""

import bisect
import gzip
import heapq
import json
import logging
import math
import os
import random
import re
import socket
import string
import threading
import time
from collections import deque
from operator import itemgetter
from urllib.parse import quote

from app.config import get_settings
from app.integrations import aprs_parser

log = logging.getLogger(__name__)

SOFTWARE = "hamdoor"
SOFTWARE_VERSION = "1.0.0"  # keep in step with FastAPI(version=...) in app/main.py

CATEGORIES = ("mobile", "fixed", "wx", "digi", "repeater", "object")

TRACK_MIN_MOVE_M = 30.0  # a new track point needs this much displacement
# APRS-IS drops repeated packets only within ~30 s: a digipeated copy of an
# earlier beacon can arrive later, and taking it as news jumps the marker back
# and draws a star of back-and-forth lines. A fix equal to one of the last
# few track points (but not the latest) is such a copy.
TRACK_DUP_LOOKBACK = 6
TRACK_DUP_EPS_DEG = 1e-6
# faster than this from the last track point is a glitch (or a second
# tracker on the same call), unless the station flies: the fix is held back
# until a second fix agrees with it
TRACK_MAX_GROUND_KMH = 400.0
_AIR_SYMBOLS = {("/", "'"), ("/", "^"), ("/", "X"), ("/", "O"), ("/", "g"), ("\\", "^"), ("\\", "O"),
                ("\\", "S")}
_AIRBORNE_ALT_M = 1500.0
# Track points in one /api/aprs/map response, most recent stations first:
# 166 full 120-point tails. The densest real z10 viewport in the 8-min NA
# capture held 16 movers (~190 points); a CONUS bbox sent with zoom=10 held
# 84k points (5.9 MB), 350k (19 MB) in a saturated 6 h store.
TRACK_POINTS_MAX = 20_000
PACKETS_KEPT = 20  # raw lines kept per station for the detail view
SNAPSHOT_PACKETS = 5  # raw lines per station worth persisting across a restart
SNAPSHOT_CHUNK = 1000  # stations copied per store-lock hold while snapshotting
MAX_STATIONS = 100_000  # hard cap against a misconfigured (near-full-feed) filter
KEEPALIVE_S = 15 * 60
SILENCE_S = 60.0  # servers send a "#" line every ~20 s; 60 s of nothing = dead
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
_BACKOFF_EXP_MAX = 16  # 5 s * 2**16 is far past the cap; 5.0 * 2**1024 overflows a float
HEALTHY_SESSION_S = 120.0  # a session this long resets the backoff
PRUNE_EVERY_S = 60.0
SNAPSHOT_EVERY_S = 300.0
SNAPSHOT_FILE = "aprs_snapshot.json.gz"
LOGIN_FILE = "aprs_login.txt"
GENERATED_LOGIN_PREFIX = "HAMDOOR-"

# "JUST-MOBILE-SYMBOLS" (aprs.org symbolsX.txt, Nov 2008 revision)
_MOBILE_PRIMARY = frozenset("!'<=>()*0123456789CFOPRSUXY[\\^abefgjkpsuv")
_MOBILE_ALTERNATE = frozenset(">KOSY[^ksuv\\")

_LOGIN_RE = re.compile(r"^[A-Z0-9]{1,9}(-[A-Z0-9]{1,2})?$")
_SERVER_RE = re.compile(r"\bserver (\S+)")

EARTH_RADIUS_KM = 6371.0088
_POINT_T = itemgetter(2)  # (lat, lon, t) track point -> t

# test seams
_clock = time.time


def _data_dir() -> str:
    url = get_settings().database_url
    path = url.split("///", 1)[-1] if "///" in url else "/data/hamdoor.db"
    return os.path.dirname(path) or "."


def _open_connection(host: str, port: int, timeout: float) -> socket.socket:
    # create_connection resolves the name on every call: aprs2.net pools are
    # DNS load-balanced and aprsc asks clients not to cache the answer
    return socket.create_connection((host, port), timeout=timeout)


# ---------------------------------------------------------------- login

_login_lock = threading.Lock()
_login_cache: str | None = None


def valid_login(login: str) -> bool:
    """APRS-IS login rules: letters/digits with an optional -SSID of 1-2
    characters, 9 characters at most (aprsc refuses longer: CALLSIGNLEN_MAX),
    base call >= 3 characters, not 8 hex digits (q.aspx), never N0CALL."""
    if not login or len(login) > 9 or not _LOGIN_RE.match(login):
        return False
    base = login.split("-", 1)[0]
    if len(base) < 3 or base in ("N0CALL", "NOCALL", "SERVER"):
        return False
    if len(login) == 8 and all(c in "0123456789ABCDEF" for c in login):
        return False
    return True


def _generated_login() -> str:
    """Persistent auto login "HAMDOOR-X" (X random), saved in the data dir so
    the server sees the same client across restarts. One character: aprsc
    refuses logins longer than 9 characters."""
    path = os.path.join(_data_dir(), LOGIN_FILE)
    try:
        with open(path) as fh:
            existing = fh.read().strip().upper()
        if existing.startswith(GENERATED_LOGIN_PREFIX) and valid_login(existing):
            return existing
    except OSError:
        pass
    login = GENERATED_LOGIN_PREFIX + random.SystemRandom().choice(string.ascii_uppercase + string.digits)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(login + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("[aprs] could not save the generated login to %s: %s", path, exc)
    return login


def get_login() -> str:
    """The receive-only APRS-IS login this server uses (feed and one-off
    samples alike): APRS_LOGIN when valid, else the generated one."""
    global _login_cache
    with _login_lock:
        if _login_cache is None:
            configured = (get_settings().aprs_login or "").strip().upper()
            if configured and valid_login(configured):
                _login_cache = configured
            else:
                if configured:
                    log.error("[aprs] APRS_LOGIN %r is not a valid APRS-IS login (letters/digits, "
                              "optional -SSID, at most 9 characters); using a generated one", configured)
                _login_cache = _generated_login()
        return _login_cache


def login_line(login: str, filter_spec: str) -> bytes:
    """`user LOGIN pass -1 vers hamdoor VERSION filter FILTER`: pass -1 is
    receive-only; we never compute or send anyone's passcode."""
    line = f"user {login} pass -1 vers {SOFTWARE} {SOFTWARE_VERSION}"
    filter_spec = " ".join((filter_spec or "").split())
    if filter_spec:
        line += f" filter {filter_spec}"
    return (line + "\r\n").encode("ascii", errors="ignore")


# ---------------------------------------------------------------- store

def _move_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance in metres: exact enough for a 30 m threshold."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000.0 * math.hypot(x, y)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _near_boxes(lat: float, lon: float, radius_km: float) -> list[tuple[float, float, float, float]]:
    """Lat/lon boxes (min_lat, max_lat, min_lon, max_lon) that together hold
    every point within radius_km of (lat, lon): near()'s grid prefilter.

    A circle on the sphere is widest in longitude poleward of its centre,
    asin(sin d / cos lat) rather than d / cos lat (2000 km at Anchorage:
    39.9 degrees, not 37.4); it spans every longitude once it reaches a
    pole; and near the antimeridian it wraps (two boxes, not a clamp)."""
    d = radius_km / EARTH_RADIUS_KM * 1.001  # angular radius, a hair generous
    dlat = math.degrees(d)
    lat0, lat1 = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    if abs(lat) + dlat >= 90.0:  # a pole is inside the circle
        return [(lat0, lat1, -180.0, 180.0)]
    dlon = math.degrees(math.asin(min(1.0, math.sin(d) / math.cos(math.radians(lat)))))  # <= 90
    lo, hi = lon - dlon, lon + dlon
    if lo < -180.0:
        return [(lat0, lat1, -180.0, hi), (lat0, lat1, lo + 360.0, 180.0)]
    if hi > 180.0:
        return [(lat0, lat1, lo, 180.0), (lat0, lat1, -180.0, hi - 360.0)]
    return [(lat0, lat1, lo, hi)]


def _clamp(v: float, lo: float, hi: float) -> float:
    """v inside [lo, hi]; +-inf go to the nearer bound, NaN to lo."""
    if not v >= lo:  # below, or NaN
        return lo
    return hi if v > hi else v


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return int(round(math.degrees(math.atan2(y, x)))) % 360


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return (math.floor(lat), math.floor(lon))


def _finite(v):
    """v when it is a finite number, else None. The store must never hold
    NaN/inf: Starlette's JSONResponse refuses them (HTTP 500 for every view
    of that area) and math.floor(nan) in _cell raises."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return v if math.isfinite(v) else None
    except TypeError:
        return None


def _valid_coords(lat, lon) -> bool:
    return (_finite(lat) is not None and _finite(lon) is not None
            and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0)


_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def aprsfi_url(call: str) -> str:
    """aprs.fi deep link, same form aprs.fi uses itself (objects included)."""
    return "https://aprs.fi/#!call=a%2F" + (call if _URL_SAFE_RE.match(call) else quote(call, safe=""))


class Station:
    """Last known state of one station, object or item.

    `last` is when anything was last heard (it keeps the record within the
    retention); `pos_t` is when the position was last reported, and that is
    what the map window and the API's "last" use: a status or positionless
    weather packet must not make a 5-hour-old pin look fresh."""

    __slots__ = ("key", "call", "src", "kind", "sym_table", "sym_code", "overlay", "lat", "lon",
                 "amb", "first", "last", "pos_t", "speed_kmh", "course", "alt_m", "comment",
                 "status", "path", "igate", "messaging", "mice_msg", "emergency", "device", "wx",
                 "phg", "freq", "track", "moved_at", "packets", "pending")

    def __init__(self, key: str, kind: str, now: float):
        self.key, self.kind = key, kind
        self.call = self.src = ""
        self.sym_table = self.sym_code = self.overlay = None
        self.lat = self.lon = None
        self.amb = 0
        self.first = self.last = now
        self.pos_t = None
        self.speed_kmh = self.course = self.alt_m = None
        self.comment = ""
        self.status = None
        self.path, self.igate = "", None
        self.messaging, self.mice_msg, self.emergency = False, None, False
        self.device = self.wx = self.phg = self.freq = None
        # plain lists: a deque costs ~600 bytes even when nearly empty
        self.track: list = []  # [(lat, lon, t)], oldest first
        self.moved_at = None
        self.packets: list = []  # [(t, raw)], oldest first, <= PACKETS_KEPT
        # a fix too far from the track to be believed yet: (lat, lon, t)
        # (transient — not in snapshots)
        self.pending = None

    # snapshot form: plain JSON types only
    def to_dict(self, packets_kept: int = PACKETS_KEPT) -> dict:
        d = {k: getattr(self, k) for k in self.__slots__ if k not in ("track", "packets", "pending")}
        d["track"] = [list(p) for p in self.track]
        d["packets"] = [list(p) for p in self.packets[-packets_kept:]] if packets_kept else []
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Station":
        """Rebuild from a snapshot. json.load turns NaN/Infinity tokens back
        into floats (a snapshot saved before the parser refused them carried
        them across restarts), so every number is checked again."""
        kind = d.get("kind", "station")
        if kind not in ("station", "object", "item"):
            raise ValueError(f"bad kind {kind!r}")
        first, last = _finite(float(d["first"])), _finite(float(d["last"]))
        if first is None or last is None:
            raise ValueError("bad first/last time")
        st = cls(str(d["key"]), kind, first)
        for k in cls.__slots__:
            if k in ("track", "packets", "pending", "key", "kind") or k not in d:
                continue
            setattr(st, k, d[k])
        st.last = last
        st.speed_kmh, st.course, st.alt_m = _finite(st.speed_kmh), _finite(st.course), _finite(st.alt_m)
        st.moved_at = _finite(st.moved_at)
        st.track = []
        if st.lat is None and st.lon is None:
            st.pos_t = None
        else:
            st.lat, st.lon = float(st.lat), float(st.lon)
            if not _valid_coords(st.lat, st.lon):
                raise ValueError("bad position")
            st.pos_t = _finite(st.pos_t)
            if st.pos_t is None:
                st.pos_t = last
            for p in d.get("track") or []:
                plat, plon, pt = float(p[0]), float(p[1]), _finite(float(p[2]))
                if _valid_coords(plat, plon) and pt is not None:
                    st.track.append((plat, plon, int(pt)))
        st.packets = [(int(p[0]), str(p[1])) for p in d.get("packets") or []][-PACKETS_KEPT:]
        return st


def _log_packet(st: Station, now: float, pkt: dict) -> None:
    st.packets.append((int(now), pkt.get("raw") or ""))
    if len(st.packets) > PACKETS_KEPT:
        del st.packets[0]


def _vehicle_symbol(st: Station) -> bool:
    if st.sym_table == "/":
        return st.sym_code in _MOBILE_PRIMARY
    return st.sym_table == "\\" and st.sym_code in _MOBILE_ALTERNATE


def category(st: Station, now: float, window_s: float, retention_s: float) -> str:
    """One category per station, first rule that applies:
    objects/items -> "repeater" (symbol /r or /m, or a voice frequency in the
    name/comment) else "object"; stations -> "wx" (symbol _ or weather data),
    "digi" (# digipeaters, & iGates/gateways, any overlay), "repeater" (/r or
    /m symbol), "mobile" (last speed > 0, or displaced >= 30 m within the
    window, or a vehicle symbol displaced within the retention period), else
    "fixed"."""
    if st.kind != "station":
        if (st.sym_table == "/" and st.sym_code in ("r", "m")) or st.freq is not None:
            return "repeater"
        return "object"
    if st.sym_code == "_" or st.wx is not None:
        return "wx"
    if st.sym_code in ("#", "&"):
        return "digi"
    if st.sym_table == "/" and st.sym_code in ("r", "m"):
        return "repeater"
    if (st.speed_kmh or 0) > 0:
        return "mobile"
    if st.moved_at is not None:
        age = now - st.moved_at
        if age <= window_s or (_vehicle_symbol(st) and age <= retention_s):
            return "mobile"
    return "fixed"


class StationStore:
    """Thread-safe store of recent stations, keyed "CALL-SSID" for stations
    and "obj:NAME" / "item:NAME" for objects/items, with a 1-degree grid."""

    def __init__(self, retention_s: float | None = None, track_max: int | None = None):
        s = get_settings()
        self.retention_s = float(retention_s if retention_s is not None else s.aprs_retention_hours * 3600)
        self.track_max = int(track_max if track_max is not None else s.aprs_track_max_points)
        self._lock = threading.RLock()
        self._st: dict[str, Station] = {}
        self._grid: dict[tuple[int, int], set[str]] = {}

    # ------------------------------------------------------------ writes

    def ingest(self, pkt: dict, now: float) -> str | None:
        """Apply one parsed packet. Returns the key it updated (None if the
        packet carried nothing for the map)."""
        kind = pkt.get("kind")
        if kind in ("object", "item"):
            key = ("obj:" if kind == "object" else "item:") + pkt["name"]
            if not pkt.get("alive", True):
                self.remove(key)  # killed: off the map immediately
                return None
        elif kind in ("position", "status", "weather"):
            key = pkt["src"]
        else:
            return None
        with self._lock:
            st = self._st.get(key)
            if kind in ("status", "weather"):
                if st is None:
                    st = self._st[key] = Station(key, "station", now)
                    st.call = st.src = pkt["src"]
                if kind == "status":
                    st.status = pkt.get("status") or None
                elif pkt.get("wx"):
                    st.wx = pkt["wx"]
                st.last = now
                st.path, st.igate = ",".join(pkt.get("path") or []), pkt.get("igate")
                _log_packet(st, now, pkt)
                return key
            lat, lon = pkt.get("lat"), pkt.get("lon")
            if not _valid_coords(lat, lon) or (lat == 0 and lon == 0):  # no (usable) fix
                return None
            if st is None:
                st = self._st[key] = Station(key, "station" if kind == "position" else kind, now)
            self._update_position(st, pkt, now, lat, lon)
            return key

    def _stale_or_glitch(self, st: Station, pkt: dict, now: float, lat: float, lon: float) -> bool:
        """True when this fix must not move the station: a late copy of an
        earlier fix, or a jump faster than anything on the ground (held in
        st.pending until a second fix agrees with it; then the track starts
        again from there)."""
        if not st.track:
            return False
        plat, plon, pt = st.track[-1]
        moved = _move_m(plat, plon, lat, lon)
        if moved < TRACK_MIN_MOVE_M:
            return False
        for qlat, qlon, _ in st.track[-TRACK_DUP_LOOKBACK - 1:-1]:
            if abs(qlat - lat) <= TRACK_DUP_EPS_DEG and abs(qlon - lon) <= TRACK_DUP_EPS_DEG:
                return True
        sym = (pkt.get("sym_table"), pkt.get("sym_code"))
        alt, speed = _finite(pkt.get("alt_m")), _finite(pkt.get("speed_kmh"))
        if sym in _AIR_SYMBOLS or (alt or 0) >= _AIRBORNE_ALT_M or (speed or 0) > TRACK_MAX_GROUND_KMH:
            return False
        if moved / max(now - pt, 1.0) * 3.6 <= TRACK_MAX_GROUND_KMH:
            st.pending = None
            return False
        held = st.pending
        if held is not None and _move_m(held[0], held[1], lat, lon) / max(now - held[2], 1.0) * 3.6 \
                <= TRACK_MAX_GROUND_KMH:
            # two fixes agree: the station really is over here now
            st.track[:] = [(held[0], held[1], int(held[2]))]
            st.pending = None
            return False
        st.pending = (lat, lon, now)
        return True

    def _update_position(self, st: Station, pkt: dict, now: float, lat: float, lon: float) -> None:
        if self._stale_or_glitch(st, pkt, now, lat, lon):
            st.last = now  # heard, but nothing new about where it is
            _log_packet(st, now, pkt)
            return
        st.call = pkt["name"] if st.kind != "station" else pkt["src"]
        st.src = pkt["src"]
        st.sym_table, st.sym_code, st.overlay = pkt.get("sym_table"), pkt.get("sym_code"), pkt.get("overlay")
        old_cell = _cell(st.lat, st.lon) if st.lat is not None else None
        if not st.track:
            st.track.append((lat, lon, int(now)))
        else:
            plat, plon, _ = st.track[-1]
            if _move_m(plat, plon, lat, lon) >= TRACK_MIN_MOVE_M:
                st.track.append((lat, lon, int(now)))
                st.moved_at = now
                if len(st.track) > self.track_max:
                    del st.track[: len(st.track) - self.track_max]
        new_cell = _cell(lat, lon)
        if new_cell != old_cell:
            if old_cell is not None:
                self._grid_discard(st.key, st.lat, st.lon)
            self._grid.setdefault(new_cell, set()).add(st.key)
        st.lat, st.lon, st.amb = lat, lon, pkt.get("amb") or 0
        st.speed_kmh, st.course = _finite(pkt.get("speed_kmh")), _finite(pkt.get("course"))
        st.alt_m = _finite(pkt.get("alt_m"))
        # some trackers send their comment only every Nth beacon: keep the last one
        if pkt.get("comment"):
            st.comment = pkt["comment"]
        st.messaging = bool(pkt.get("messaging"))
        st.mice_msg, st.emergency = pkt.get("mice_msg"), bool(pkt.get("emergency"))
        st.device = pkt.get("device") or st.device
        st.phg = pkt.get("phg") or st.phg
        if pkt.get("wx"):
            st.wx = pkt["wx"]
        st.freq = aprs_parser.frequency_mhz(pkt.get("name"), st.comment)
        st.path, st.igate = ",".join(pkt.get("path") or []), pkt.get("igate")
        st.last = st.pos_t = now
        _log_packet(st, now, pkt)

    def _grid_discard(self, key: str, lat: float, lon: float) -> None:
        c = _cell(lat, lon)
        cell = self._grid.get(c)
        if cell is not None:
            cell.discard(key)
            if not cell:
                del self._grid[c]

    def remove(self, key: str) -> bool:
        with self._lock:
            st = self._st.pop(key, None)
            if st is None:
                return False
            if st.lat is not None:
                self._grid_discard(key, st.lat, st.lon)
            return True

    def prune(self, now: float) -> int:
        """Drop stations not heard within the retention period, positions
        not reported within it (the record stays while status/weather keep
        coming, off the map until the next position), and track points /
        raw lines older than it. Returns stations removed."""
        cutoff = now - self.retention_s
        removed = 0
        with self._lock:
            for key in [k for k, st in self._st.items() if st.last < cutoff]:
                self.remove(key)
                removed += 1
            for st in self._st.values():
                if st.lat is not None and st.pos_t < cutoff:
                    self._grid_discard(st.key, st.lat, st.lon)
                    st.lat = st.lon = st.pos_t = st.moved_at = None
                    st.track = []
                old = 0
                while old < len(st.track) - 1 and st.track[old][2] < cutoff:
                    old += 1
                if old:
                    del st.track[:old]
                old = 0
                while old < len(st.packets) and st.packets[old][0] < cutoff:
                    old += 1
                if old:
                    del st.packets[:old]
                if st.moved_at is not None and st.moved_at < cutoff:
                    st.moved_at = None
            if len(self._st) > MAX_STATIONS:
                excess = sorted(self._st.values(), key=lambda s: s.last)[: len(self._st) - MAX_STATIONS]
                for st in excess:
                    self.remove(st.key)
                    removed += 1
        return removed

    def clear(self) -> None:
        with self._lock:
            self._st.clear()
            self._grid.clear()

    # ------------------------------------------------------------ reads

    def __len__(self) -> int:
        return len(self._st)

    def positioned_count(self) -> int:
        with self._lock:
            return sum(len(keys) for keys in self._grid.values())

    def _keys_in_bbox(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float):
        """Keys in every 1-degree cell the bbox touches. Edges are clamped
        into range first: FastAPI accepts "inf" for a float query parameter,
        app.geo.clamp_bbox lets min_lat=max_lat=-inf through as -inf, and
        math.floor(inf) raises (HTTP 500 instead of a clamped bbox)."""
        lat0, lat1 = math.floor(_clamp(min_lat, -90.0, 90.0)), math.floor(_clamp(max_lat, -90.0, 90.0))
        lon0, lon1 = math.floor(_clamp(min_lon, -180.0, 180.0)), math.floor(_clamp(max_lon, -180.0, 180.0))
        if (lat1 - lat0 + 1) * (lon1 - lon0 + 1) > len(self._grid):
            for (clat, clon), keys in self._grid.items():
                if lat0 <= clat <= lat1 and lon0 <= clon <= lon1:
                    yield from keys
        else:
            for clat in range(lat0, lat1 + 1):
                for clon in range(lon0, lon1 + 1):
                    keys = self._grid.get((clat, clon))
                    if keys:
                        yield from keys

    def query(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float, *,
              now: float, window_s: float, cats=CATEGORIES, limit: int | None = None,
              tracks: bool = False) -> tuple[int, list[dict]]:
        """(total matching, station JSON most recent position first, capped at limit).

        With tracks, a station gets its tail in the window (>= 2 points)
        while the response still has room under TRACK_POINTS_MAX; a tail
        that does not fit is None. Zoom and bbox are both client-chosen, so
        a CONUS bbox sent with zoom=10 must not cost MBs and the lock."""
        since = now - window_s
        room = TRACK_POINTS_MAX
        with self._lock:
            hits = self._matching(min_lat, max_lat, min_lon, max_lon, now, window_s, cats)
            total = len(hits)
            order = lambda st: (-st.pos_t, st.key)  # noqa: E731  most recent first
            if limit is not None and limit < total:
                page = heapq.nsmallest(limit, hits, key=order)
            else:
                page = sorted(hits, key=order)
            out = []
            for st in page:
                track = self._track(st, since) if tracks else None
                if track is not None:
                    if len(track) > room:
                        track = None
                    else:
                        room -= len(track)
                out.append(self._json(st, category(st, now, window_s, self.retention_s), track))
            return total, out

    def _matching(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float,
                  now: float, window_s: float, cats) -> list[Station]:
        """Stations whose position was reported within the window inside the
        bbox and whose category is wanted (categories are only computed when
        some are filtered out)."""
        since = now - window_s
        cats = set(cats)
        filtered = not cats.issuperset(CATEGORIES)
        hits = []
        for key in self._keys_in_bbox(min_lat, max_lat, min_lon, max_lon):
            st = self._st[key]
            if st.pos_t < since or not (min_lat <= st.lat <= max_lat and min_lon <= st.lon <= max_lon):
                continue
            if filtered and category(st, now, window_s, self.retention_s) not in cats:
                continue
            hits.append(st)
        return hits

    def count(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float, *,
              now: float, window_s: float, cats=CATEGORIES) -> int:
        with self._lock:
            return len(self._matching(min_lat, max_lat, min_lon, max_lon, now, window_s, cats))

    def near(self, lat: float, lon: float, radius_km: float, *, now: float, window_s: float,
             cats=CATEGORIES, limit: int | None = None) -> tuple[int, list[tuple[float, dict]]]:
        """(stations within radius_km, [(distance_km, station JSON)] nearest
        first, capped at limit). JSON is built only for the rows returned:
        2000 km over 6 h matches ~18k stations, all under the lock the feed
        thread needs."""
        since = now - window_s
        cats = set(cats)
        filtered = not cats.issuperset(CATEGORIES)
        with self._lock:
            hits = []
            for box in _near_boxes(lat, lon, radius_km):  # disjoint cells: no key twice
                for key in self._keys_in_bbox(*box):
                    st = self._st[key]
                    if st.pos_t < since:
                        continue
                    d = _haversine_km(lat, lon, st.lat, st.lon)
                    if d > radius_km:
                        continue
                    if filtered and category(st, now, window_s, self.retention_s) not in cats:
                        continue
                    hits.append((d, st.key, st))
            order = lambda h: (h[0], h[1])  # noqa: E731  nearest first, then key
            if limit is not None and limit < len(hits):
                page = heapq.nsmallest(limit, hits, key=order)
            else:
                page = sorted(hits, key=order)
            return len(hits), [(d, self._json(st, category(st, now, window_s, self.retention_s), None))
                               for d, _, st in page]

    def station(self, key: str, *, now: float, window_s: float) -> dict | None:
        """Station JSON with its whole track in the window and raw packets."""
        with self._lock:
            st = self._st.get(key)
            if st is None or st.lat is None:
                return None
            cat = category(st, now, window_s, self.retention_s)
            out = self._json(st, cat, self._track(st, now - window_s))
            out["packets"] = [{"t": t, "raw": raw} for t, raw in reversed(st.packets)]
            return out

    def search(self, q: str, *, now: float, limit: int = 10) -> list[dict]:
        """Prefix match on callsign / object name, case-insensitive; exact
        match first, then the most recent position."""
        q = q.strip().upper()
        if not q:
            return []
        with self._lock:
            hits = [st for st in self._st.values()
                    if st.lat is not None and st.call.upper().startswith(q)]
            hits.sort(key=lambda st: (st.call.upper() != q, -st.pos_t, st.call))
            return [{"key": st.key, "call": st.call,
                     "cat": category(st, now, 3600.0, self.retention_s),
                     "lat": st.lat, "lon": st.lon, "last": int(st.pos_t)} for st in hits[:limit]]

    @staticmethod
    def _track(st: Station, since: float) -> list | None:
        """The track points reported since `since` (oldest first), or None
        below 2 points. Points are appended in time order, so this is a
        bisect and a slice of immutable (lat, lon, t) tuples: cheap under the
        lock, and serialized as [lat, lon, t] arrays after it is released."""
        i = bisect.bisect_left(st.track, since, key=_POINT_T)
        return st.track[i:] if len(st.track) - i >= 2 else None

    def _json(self, st: Station, cat: str, track: list | None) -> dict:
        return {
            "key": st.key, "call": st.call, "from": st.src, "kind": st.kind, "cat": cat,
            "sym": {"table": st.sym_table, "code": st.sym_code, "overlay": st.overlay},
            "lat": st.lat, "lon": st.lon, "amb": st.amb,
            "last": int(st.pos_t), "first": int(st.first),  # last = the position's time
            "speed_kmh": round(st.speed_kmh, 1) if st.speed_kmh is not None else None,
            "course": st.course,
            "alt_m": round(st.alt_m, 1) if st.alt_m is not None else None,
            "comment": st.comment, "status": st.status, "path": st.path, "igate": st.igate,
            "messaging": st.messaging, "mice_msg": st.mice_msg, "emergency": st.emergency,
            "device": st.device, "wx": st.wx, "phg": st.phg, "freq": st.freq,
            "track": track, "aprsfi_url": aprsfi_url(st.call),
        }

    # ------------------------------------------------------------ snapshot

    def snapshot(self, now: float) -> dict:
        """Copy in chunks, releasing the lock in between: a full 6-hour store
        (~24k stations) would otherwise stall the feed and the API for
        seconds. Only the last few raw lines per station are persisted."""
        with self._lock:
            keys = list(self._st)
        stations = []
        for i in range(0, len(keys), SNAPSHOT_CHUNK):
            with self._lock:
                for key in keys[i:i + SNAPSHOT_CHUNK]:
                    st = self._st.get(key)
                    if st is not None:
                        stations.append(st.to_dict(SNAPSHOT_PACKETS))
        return {"version": 1, "saved_at": int(now), "stations": stations}

    def load(self, data: dict, now: float) -> int:
        """Load a snapshot, dropping anything older than the retention."""
        cutoff = now - self.retention_s
        n = 0
        with self._lock:
            for d in data.get("stations") or []:
                try:
                    st = Station.from_dict(d)
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                if st.last < cutoff:
                    continue
                self._st[st.key] = st
                if st.lat is not None:
                    self._grid.setdefault(_cell(st.lat, st.lon), set()).add(st.key)
                n += 1
        self.prune(now)
        return n


# ---------------------------------------------------------------- feed

class LoginRefused(Exception):
    """The server rejected our login: back off to the cap, never hammer."""


def backoff_delay(attempt: int, refused: bool = False) -> float:
    """Seconds before reconnect number `attempt` (0-based): 5, 10, 20 ...
    capped at 300, +-20 % jitter (never above the cap); a refused login
    waits the cap. The exponent is bounded: 5.0 * 2**1024 does not fit a
    float, and that OverflowError ended the feed thread for good after
    1,025 failed reconnects (~3.4 days of outage)."""
    if refused:
        base = BACKOFF_MAX_S
    else:
        base = min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2 ** min(max(attempt, 0), _BACKOFF_EXP_MAX))
    return min(BACKOFF_MAX_S, base * random.uniform(0.8, 1.2))


class _Feed:
    def __init__(self, store: StationStore):
        s = get_settings()
        self.store = store
        self.host, self.port = s.aprs_host, s.aprs_port
        self.filter = " ".join((s.aprs_feed_filter or "").split())
        self.login = get_login()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.connected = False
        self.server: str | None = None
        self.since: float | None = None
        self.packets = 0  # this connection
        self.packets_total = 0
        self.parsed_total = 0
        self.last_error: str | None = None
        self._logged_in = False
        self._rate: deque = deque()  # (second, count) for the last minute
        self.threads: list[threading.Thread] = []

    # -------------------------------------------------------- stats

    def _count_packet(self, now: float) -> None:
        sec = int(now)
        with self.lock:
            if self._rate and self._rate[-1][0] == sec:
                self._rate[-1][1] += 1
            else:
                self._rate.append([sec, 1])
            while self._rate and self._rate[0][0] <= sec - 60:
                self._rate.popleft()

    def packets_per_min(self, now: float) -> int:
        cutoff = int(now) - 60
        with self.lock:
            return sum(c for s, c in self._rate if s > cutoff)

    # -------------------------------------------------------- connection

    def run(self) -> None:
        attempt = 0
        while not self.stop_event.is_set():
            started = _clock()
            refused = False
            try:
                self._session()
            except LoginRefused as exc:
                refused = True
                self.last_error = str(exc)
                log.warning("[aprs] login refused by %s:%s: %s (retrying in %d s)",
                            self.host, self.port, exc, BACKOFF_MAX_S)
            except (OSError, ValueError, ConnectionError) as exc:
                if not self.stop_event.is_set():
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("[aprs] connection to %s:%s lost: %s", self.host, self.port, self.last_error)
            except Exception as exc:  # never let the feed thread die
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("[aprs] feed error")
            finally:
                self._close()
            if self.stop_event.is_set():
                break
            if _clock() - started >= HEALTHY_SESSION_S:
                attempt = 0
            # pure arithmetic that cannot raise: anything thrown out here
            # (outside the try) would end the thread for good
            delay = backoff_delay(attempt, refused)
            attempt += 1
            self.stop_event.wait(delay)

    def _session(self) -> None:
        sock = _open_connection(self.host, self.port, 15.0)
        with self.lock:
            self.sock = sock
        if self.stop_event.is_set():
            return
        sock.settimeout(5.0)
        buf = b""
        # the server speaks first ("# aprsc 2.1.x ..."); wait for it briefly
        deadline = _clock() + 10.0
        while b"\n" not in buf and _clock() < deadline and not self.stop_event.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("closed before the banner")
            buf += chunk
        if not buf.startswith(b"#"):
            raise ConnectionError("no APRS-IS banner")
        sock.sendall(login_line(self.login, self.filter))
        now = _clock()
        self.connected, self.since, self.packets, self.server = True, now, 0, None
        self._logged_in = False
        last_rx = last_keepalive = now
        log.info("[aprs] connected to %s:%s as %s (filter %s)", self.host, self.port, self.login,
                 self.filter or "none")
        while not self.stop_event.is_set():
            lines, buf = buf.split(b"\n"), b""
            buf = lines.pop()
            if len(buf) > 4096:  # APRS-IS lines are <= 512 bytes: garbage
                buf = b""
            now = _clock()
            for line in lines:
                self._handle_line(line, now)
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                chunk = None
            now = _clock()
            if chunk == b"":
                raise ConnectionError("closed by server")
            if chunk:
                last_rx = now
                buf += chunk
            elif now - last_rx > SILENCE_S:
                raise TimeoutError(f"no data for {int(now - last_rx)} s")
            if now - last_keepalive >= KEEPALIVE_S:
                sock.sendall(b"#keepalive\r\n")
                last_keepalive = now

    def _handle_line(self, line: bytes, now: float) -> None:
        line = line.rstrip(b"\r")
        if not line:
            return
        if line[:1] == b"#":
            text = line.decode("latin-1", errors="replace")
            low = text.lower()
            if low.startswith("# logresp"):
                m = _SERVER_RE.search(text)
                self.server = m.group(1) if m else self.server
                self._logged_in = True
                log.info("[aprs] %s", text[2:])
            elif not self._logged_in and ("not allowed" in low or "invalid" in low or "full" in low):
                # "# Login by user not allowed", "# Invalid username format", "# Server full"
                raise LoginRefused(text.lstrip("# "))
            return
        self.packets += 1
        self.packets_total += 1
        self._count_packet(now)
        try:
            pkt = aprs_parser.parse_line(line)
        except Exception:  # the parser is pure; this is belt and braces
            log.debug("[aprs] parser error on %r", line[:200])
            return
        if pkt is None:
            return
        self.parsed_total += 1
        self.store.ingest(pkt, now)

    def _close(self) -> None:
        with self.lock:
            sock, self.sock = self.sock, None
        if self.connected:
            log.info("[aprs] disconnected from %s", self.server or self.host)
        self.connected, self.since = False, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def interrupt(self) -> None:
        self.stop_event.set()
        with self.lock:
            sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -------------------------------------------------------- housekeeping

    def housekeeping(self) -> None:
        last_snapshot = _clock()
        while not self.stop_event.wait(PRUNE_EVERY_S):
            now = _clock()
            try:
                removed = self.store.prune(now)
                if removed:
                    log.debug("[aprs] pruned %d stale stations", removed)
                if get_settings().aprs_snapshot_enabled and now - last_snapshot >= SNAPSHOT_EVERY_S:
                    save_snapshot(self.store)
                    last_snapshot = now
            except Exception:
                log.exception("[aprs] housekeeping failed")


# ---------------------------------------------------------------- snapshot I/O

def _snapshot_path() -> str:
    return os.path.join(_data_dir(), SNAPSHOT_FILE)


def save_snapshot(store: StationStore) -> bool:
    path = _snapshot_path()
    data = store.snapshot(_clock())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("[aprs] could not write snapshot %s: %s", path, exc)
        return False


def load_snapshot(store: StationStore) -> int:
    path = _snapshot_path()
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, EOFError) as exc:
        log.warning("[aprs] ignoring unreadable snapshot %s: %s", path, exc)
        return 0
    n = store.load(data, _clock())
    log.info("[aprs] restored %d stations from the snapshot", n)
    return n


# ---------------------------------------------------------------- module API

_store: StationStore | None = None
_feed: _Feed | None = None
_state_lock = threading.Lock()


def get_store() -> StationStore:
    global _store
    with _state_lock:
        if _store is None:
            _store = StationStore()
        return _store


def is_running() -> bool:
    return _feed is not None


def is_connected() -> bool:
    """Is the live feed logged in to an APRS-IS server right now?"""
    feed = _feed
    return bool(feed and feed.connected)


def now() -> float:
    """Wall-clock seconds used for every store read/write (one seam to freeze in tests)."""
    return _clock()


def start() -> None:
    """Start the background consumer. No-op when APRS_FEED_ENABLED is false."""
    global _feed
    s = get_settings()
    if not s.aprs_feed_enabled:
        log.info("[aprs] live feed disabled via APRS_FEED_ENABLED")
        return
    store = get_store()
    with _state_lock:
        if _feed is not None:
            return
        if s.aprs_snapshot_enabled:
            try:
                load_snapshot(store)
            except Exception:
                log.exception("[aprs] snapshot load failed")
        feed = _Feed(store)
        feed.threads = [threading.Thread(target=feed.run, daemon=True, name="aprs-feed"),
                        threading.Thread(target=feed.housekeeping, daemon=True, name="aprs-housekeeping")]
        _feed = feed
    for t in feed.threads:
        t.start()
    log.info("[aprs] feed started: %s:%s login %s", feed.host, feed.port, feed.login)


def stop() -> None:
    """Stop the consumer (and persist a snapshot when enabled)."""
    global _feed
    with _state_lock:
        feed, _feed = _feed, None
    if feed is None:
        return
    feed.interrupt()
    for t in feed.threads:
        t.join(timeout=10)
    if get_settings().aprs_snapshot_enabled:
        save_snapshot(feed.store)
    log.info("[aprs] feed stopped")


def feed_info() -> dict:
    """Small status block embedded in every /api/aprs/map response."""
    feed = _feed
    return {
        "connected": bool(feed and feed.connected),
        "server": feed.server if feed else None,
        "since": int(feed.since) if feed and feed.since else None,
        "packets": feed.packets if feed else 0,
        "stations": get_store().positioned_count(),
    }


def status() -> dict:
    s = get_settings()
    feed = _feed
    now = _clock()
    return {
        "enabled": bool(s.aprs_feed_enabled),
        "connected": bool(feed and feed.connected),
        "server": feed.server if feed else None,
        "login": feed.login if feed else None,
        "filter": feed.filter if feed else (" ".join((s.aprs_feed_filter or "").split()) or None),
        "since": int(feed.since) if feed and feed.since else None,
        "packets": feed.packets if feed else 0,
        "packets_per_min": feed.packets_per_min(now) if feed else 0,
        "stations": get_store().positioned_count(),
        "window_max_s": int(s.aprs_retention_hours * 3600),
        "last_error": feed.last_error if feed else None,
    }
