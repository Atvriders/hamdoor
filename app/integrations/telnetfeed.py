"""Live DX cluster / CW Reverse Beacon Network spots via telnet feed sampling.

Both DX cluster nodes and the RBN node speak the same AR-Cluster line format:

    DX de W1AAA:     14025.0  K1XYZ   CW op nr      0123Z
    DX de SKIMMER:   14032.5  K1XYZ   CW  12 dB  24 WPM  CQ  0124Z

We open a short-lived connection, log in with the requesting user's callsign,
and collect spot lines for a few seconds, then parse them.
"""

import logging
import re
import socket
import time

from app.config import get_settings
from app.integrations.common import UpstreamError

log = logging.getLogger(__name__)

SPOT_RE = re.compile(
    r"^DX de (?P<spotter>[A-Za-z0-9/#_-]+?):\s+"
    r"(?P<freq>\d+(?:\.\d+)?)\s+"
    r"(?P<dx>[A-Za-z0-9/#_-]+)\s+"
    r"(?P<comment>.*?)\s*"
    r"(?P<time>\d{4}Z)?\s*$"
)


def parse_spot_line(line: str) -> dict | None:
    """Parse one 'DX de ...' line. Returns None for non-spot lines."""
    m = SPOT_RE.match(line.strip())
    if not m:
        return None
    freq_khz = float(m.group("freq"))
    return {
        "spotter": m.group("spotter").upper(),
        "frequency_khz": freq_khz,
        "frequency_mhz": round(freq_khz / 1000.0, 4),
        "dx_callsign": m.group("dx").upper(),
        "comment": m.group("comment"),
        "time": m.group("time") or "",
    }


def sample_telnet_feed(host: str, port: int, callsign: str,
                       seconds: float, max_spots: int = 200,
                       post_login_cmds: tuple = ()) -> list[dict]:
    """Connect, log in, optionally send setup commands, and collect parsed
    spot lines for `seconds`.

    Raises UpstreamError when the node failed us without a single spot: no
    connection, a connection error, the node hanging up before the sample
    ended, or not one byte in the whole sample. Spots heard before a failure
    are returned (that is what the node sent); [] means the node stayed
    connected and spotted nothing."""
    deadline = time.monotonic() + seconds
    spots: list[dict] = []
    buf = b""
    logged_in = False
    received = hung_up = False
    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            sock.settimeout(2.0)
            while time.monotonic() < deadline and len(spots) < max_spots:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    hung_up = True
                    break
                received = True
                buf += chunk
                # the login prompt often arrives without a trailing newline —
                # watch the raw buffer, not just split lines
                if not logged_in:
                    low_buf = buf.decode("utf-8", errors="replace").lower()
                    if low_buf.rstrip().endswith(("login:", "call:")) or "enter your call" in low_buf:
                        sock.sendall(callsign.encode("ascii", errors="replace")[:16] + b"\r\n")
                        for cmd in post_login_cmds:
                            sock.sendall(cmd.encode("ascii", errors="replace") + b"\r\n")
                        logged_in = True
                        buf = b""
                        continue
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    spot = parse_spot_line(line)
                    if spot:
                        spots.append(spot)
    except OSError as exc:
        log.warning("[activity] telnet feed %s:%s failed: %s", host, port, exc)
        if not spots:
            raise UpstreamError(f"{host}:{port}: {type(exc).__name__}") from None
        return spots
    if not spots and (hung_up or not received):
        log.warning("[activity] telnet feed %s:%s %s", host, port,
                    "hung up before the sample ended" if hung_up else "sent nothing")
        raise UpstreamError(f"{host}:{port}: {'hung up' if hung_up else 'silent'}")
    return spots


def sample_dxcluster(callsign: str) -> list[dict]:
    s = get_settings()
    # set/ft8 turns on the FT8 spot stream on CC Cluster nodes (off by default)
    return sample_telnet_feed(s.dxc_host, s.dxc_port, callsign,
                              s.activity_sample_seconds, s.activity_max_spots,
                              post_login_cmds=("set/ft8",))


def sample_rbn(callsign: str) -> list[dict]:
    """Recent CW RBN spots. Everything the skimmer network reported while we
    sampled; rows where `dx_callsign` == the queried callsign are stations
    heard BY the network (i.e. your own signal reports)."""
    s = get_settings()
    spots = sample_telnet_feed(s.rbn_host, s.rbn_port, callsign,
                               s.activity_sample_seconds, s.activity_max_spots)
    for spot in spots:
        spot["comment"] = re.sub(r"\s+", " ", spot["comment"])
    return spots
