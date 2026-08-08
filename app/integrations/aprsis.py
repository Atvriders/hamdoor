"""APRS-IS live position sampling around a location.

Opens a short read-only connection to an APRS-IS server with a radius filter
(`r/lat/lon/km`), collects position packets for a few seconds, and parses
them (uncompressed, timestamped, and base91-compressed positions).

The APRS-IS passcode algorithm is the well-known public XOR hash; we log in
with the requesting user's own licensed callsign.
"""

import logging
import re
import socket
import time

from app.config import get_settings

log = logging.getLogger(__name__)


def aprs_passcode(callsign: str) -> int:
    """Standard public APRS-IS passcode hash (XOR with 0x73E2)."""
    call = callsign.split("-")[0].upper()
    h = 0x73E2
    for i in range(0, len(call) - 1, 2):
        h ^= ord(call[i]) << 8
        h ^= ord(call[i + 1])
    if len(call) % 2 == 1:
        h ^= ord(call[-1]) << 8
    return h & 0x7FFF


_UNCOMPRESSED_RE = re.compile(
    r"^(?P<lat>\d{4}\.\d{2}[NS])(?P<sym>.)(?P<lon>\d{5}\.\d{2}[EW])(?P<code>.)(?P<rest>.*)$"
)


def _dm_to_deg(dm: str, is_lat: bool) -> float | None:
    try:
        if is_lat:
            deg, minutes = int(dm[:2]), float(dm[2:-1])
        else:
            deg, minutes = int(dm[:3]), float(dm[3:-1])
        hemi = dm[-1]
        val = deg + minutes / 60.0
        return val if hemi in "NE" else -val
    except (ValueError, IndexError):
        return None


def _decode_compressed(block: str) -> tuple[float, float] | None:
    """Decode a 13-char base91-compressed position block -> (lat, lon)."""
    if len(block) < 9:
        return None
    try:
        lat_v = 0
        for ch in block[1:5]:
            lat_v = lat_v * 91 + (ord(ch) - 33)
        lon_v = 0
        for ch in block[5:9]:
            lon_v = lon_v * 91 + (ord(ch) - 33)
        lat = 90.0 - lat_v / 380926.0
        lon = -180.0 + lon_v / 190463.0
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (round(lat, 5), round(lon, 5))
    except (ValueError, ZeroDivisionError):
        pass
    return None


def _parse_position_body(body: str) -> tuple[float | None, float | None, str]:
    """Parse an APRS position body (uncompressed or base91-compressed)."""
    m = _UNCOMPRESSED_RE.match(body)
    if m:
        return (_dm_to_deg(m.group("lat"), True), _dm_to_deg(m.group("lon"), False), m.group("rest"))
    ll = _decode_compressed(body[:13])
    if ll:
        return (ll[0], ll[1], body[13:])
    return (None, None, "")


def parse_aprs_line(line: str) -> dict | None:
    """Parse one APRS-IS line into a position dict, or None if unsupported."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if ">" not in line or ":" not in line:
        return None
    sender, rest = line.split(">", 1)
    if ":" not in rest:
        return None
    _, rest = rest.split(":", 1)
    if not rest:
        return None

    dtype = rest[0]
    body = rest[1:]
    obj_name = ""

    if dtype in ("!", "="):
        lat, lon, comment = _parse_position_body(body)
    elif dtype in ("/", "@"):
        lat, lon, comment = _parse_position_body(body[7:] if len(body) > 7 else "")
    elif dtype == ";":  # object: 9-char name + live/killed + 7-char ts + position
        if len(body) < 17:
            return None
        obj_name = body[:9].strip()
        lat, lon, comment = _parse_position_body(body[17:])
    else:
        return None

    if lat is None or lon is None:
        return None
    return {
        "callsign": sender.strip().upper(),
        "object": obj_name,
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "comment": comment.strip(),
    }


def sample_aprs(callsign: str, lat: float, lon: float, radius_km: int) -> list[dict]:
    """Collect APRS positions within `radius_km` of (lat, lon)."""
    s = get_settings()
    seconds = s.activity_sample_seconds
    deadline = time.monotonic() + seconds
    stations: dict[str, dict] = {}
    try:
        passcode = aprs_passcode(callsign)
        login = (
            f"user {callsign} pass {passcode} vers hamdoor 1.0 "
            f"filter r/{lat:.3f}/{lon:.3f}/{int(radius_km)}\r\n"
        ).encode("ascii", errors="replace")
        with socket.create_connection((s.aprs_host, s.aprs_port), timeout=8) as sock:
            sock.settimeout(2.0)
            sock.sendall(login)
            buf = b""
            while time.monotonic() < deadline and len(stations) < s.activity_max_spots:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    pos = parse_aprs_line(raw.decode("utf-8", errors="replace"))
                    if pos:
                        key = pos["object"] or pos["callsign"]
                        stations[key] = pos
    except OSError as exc:
        log.warning("aprs-is sample failed: %s", exc)
    return list(stations.values())
