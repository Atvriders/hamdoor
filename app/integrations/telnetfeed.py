"""Live DX cluster / CW Reverse Beacon Network spots via telnet feed sampling.

Both DX cluster nodes and the RBN node speak the same AR-Cluster line format:

    DX de W1AAA:     14025.0  K1XYZ   CW op nr      0123Z
    DX de SKIMMER:   14032.5  K1XYZ   CW  12 dB  24 WPM  CQ  0124Z

We open a short-lived connection, log in with the requesting user's callsign,
and collect spot lines for a few seconds, then parse them. A DX cluster is
also asked for its latest spots right after the log in (SH/DX), which it
lists one per line, newest first:

    14025.0  K1XYZ       28-Oct-2026 0123Z  CW op nr               <W1AAA>

A DX cluster node that sends no log-in prompt within a few seconds is passed
over for the next one in DXC_FALLBACKS (ve7cc.net:23, the old default, now
accepts connections and never answers).
"""

import logging
import re
import socket
import time
from datetime import datetime, timezone

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


# one line of a node's answer to SH/DX: frequency, DX, date, time, comment, <spotter>
LISTED_RE = re.compile(
    r"^(?P<freq>\d+(?:\.\d+)?)\s+"
    r"(?P<dx>[A-Za-z0-9/#_-]+)\s+"
    r"(?:\d{1,2}-[A-Za-z]{3}-\d{2,4}\s+)?"
    r"(?P<time>\d{4}Z)\s*"
    r"(?P<comment>.*?)\s*"
    r"<(?P<spotter>[A-Za-z0-9/#_-]+)>\s*$"
)


def parse_spot_line(line: str) -> dict | None:
    """Parse one 'DX de ...' line, or one line of a SH/DX answer. Returns
    None for anything else."""
    line = line.strip()
    m = SPOT_RE.match(line) or LISTED_RE.match(line)
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
                       post_login_cmds: tuple = (), prompt_seconds: float | None = None) -> list[dict]:
    """Connect, log in, optionally send setup commands, and collect parsed
    spot lines for `seconds`.

    Raises UpstreamError when the node failed us without a single spot: no
    connection, a connection error, the node hanging up before the sample
    ended, not one byte in the whole sample, or (with prompt_seconds) no
    log-in prompt that soon. Spots heard before a failure are returned (that
    is what the node sent); [] means the node stayed connected and spotted
    nothing."""
    deadline = time.monotonic() + seconds
    spots: list[dict] = []
    buf = b""
    logged_in = False
    received = hung_up = no_prompt = False
    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            sock.settimeout(2.0)
            connected = time.monotonic()  # the prompt is timed from here: a slow connect is not a silent node
            while time.monotonic() < deadline and len(spots) < max_spots:
                if prompt_seconds is not None and not logged_in and time.monotonic() - connected >= prompt_seconds:
                    no_prompt = True
                    break
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
    if no_prompt:
        log.warning("[activity] telnet feed %s:%s sent no log-in prompt in %.0f s", host, port, prompt_seconds)
        raise UpstreamError(f"{host}:{port}: no log-in prompt")
    if not spots and (hung_up or not received):
        log.warning("[activity] telnet feed %s:%s %s", host, port,
                    "hung up before the sample ended" if hung_up else "sent nothing")
        raise UpstreamError(f"{host}:{port}: {'hung up' if hung_up else 'silent'}")
    return spots


# a DX cluster node that sends no log-in prompt within this many seconds is
# passed over for the next one (a live node prompts at once)
PROMPT_SECONDS = 6.0
# after the log in: FT8 spots on (CC Cluster nodes have them off; others shrug),
# then the node's latest spots, so the list never waits on the sample alone
DXC_COMMANDS = ("set/ft8", "sh/dx 30")
# the node that answered last is asked first for this long: a dead DXC_HOST
# then costs its PROMPT_SECONDS once an hour, not on every refresh
_PREFER_SECONDS = 3600.0
_dxc_last: dict = {"node": None, "until": 0.0}


def dxc_nodes() -> list[tuple[str, int]]:
    """DXC_HOST:DXC_PORT, then each DXC_FALLBACKS node (host:port, separated
    by commas or spaces) not listed already."""
    s = get_settings()
    nodes = [(s.dxc_host.strip(), int(s.dxc_port))]
    for item in re.split(r"[\s,]+", s.dxc_fallbacks or ""):
        if not item:
            continue
        host, _, port = item.rpartition(":")
        if not host or not port.isdigit():
            log.warning("[activity] DXC_FALLBACKS: %r is not host:port", item)
            continue
        node = (host.strip("[]"), int(port))
        if node not in nodes:
            nodes.append(node)
    return nodes


def dxc_source() -> str:
    """host:port of the DX cluster node that answered last (else DXC_HOST's)."""
    host, port = _dxc_last["node"] or dxc_nodes()[0]
    return f"{host}:{port}"


def _newest_first(spots: list[dict], now: datetime | None = None) -> list[dict]:
    """The node's SH/DX list (newest first) and the spots that came in live
    after it, as one list newest first, each spot once. Spots carry only a
    UTC time of day: a time up to 10 minutes ahead of ours is a node clock
    running fast, not yesterday."""
    now = now or datetime.now(timezone.utc)
    now_min = now.hour * 60 + now.minute

    def age(spot: dict) -> int:
        t = spot.get("time") or ""
        if not (len(t) >= 4 and t[:4].isdigit()):
            return 0
        ago = (now_min - (int(t[:2]) * 60 + int(t[2:4]))) % 1440
        return 0 if ago > 1440 - 10 else ago

    out, seen = [], set()
    for spot in sorted(spots, key=age):  # stable: spots of one minute keep the node's order
        key = (spot["dx_callsign"], round(spot["frequency_khz"]), spot["spotter"], spot["time"])
        if key not in seen:
            seen.add(key)
            out.append(spot)
    return out


def sample_dxcluster(callsign: str) -> list[dict]:
    """The latest DX cluster spots, newest first: the node's own list (SH/DX)
    and what it sends during the sample. Nodes are tried in turn (dxc_nodes,
    the one that answered last first); UpstreamError when none answered."""
    s = get_settings()
    nodes = dxc_nodes()
    if _dxc_last["node"] in nodes and time.monotonic() < _dxc_last["until"]:
        nodes.remove(_dxc_last["node"])
        nodes.insert(0, _dxc_last["node"])
    failed = []
    for host, port in nodes:
        if failed:
            log.info("[activity] DX cluster: trying %s:%s (%s)", host, port, "; ".join(failed))
        try:
            spots = sample_telnet_feed(host, port, callsign, s.activity_sample_seconds, s.activity_max_spots,
                                       post_login_cmds=DXC_COMMANDS, prompt_seconds=PROMPT_SECONDS)
        except UpstreamError as exc:
            failed.append(str(exc))
            continue
        _dxc_last.update(node=(host, port), until=time.monotonic() + _PREFER_SECONDS)
        return _newest_first(spots)
    raise UpstreamError("; ".join(failed))


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
