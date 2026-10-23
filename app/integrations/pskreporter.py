"""PSK Reporter spots (digital modes: FT8, FT4, PSK31, ...).

Uses the documented read-only query API:
  https://api.pskreporter.info/query?senderCallsign=W1AW
Returns XML <receptionReport> elements. Rate-limited — responses are cached
by the caller.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.geo import grid_to_latlon
from app.integrations.common import UpstreamError

log = logging.getLogger(__name__)


def grid_center(grid: str) -> tuple[float, float] | None:
    """Centre of a Maidenhead locator. PSK Reporter sends 4-10 characters;
    use the longest prefix geo.grid_to_latlon understands (8, 6 or 4)."""
    g = (grid or "").strip()
    for n in (8, 6, 4):
        if len(g) >= n:
            ll = grid_to_latlon(g[:n])
            if ll:
                return (round(ll[0], 5), round(ll[1], 5))
    return None


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_pskreporter_xml(text: str) -> list[dict]:
    """Reception reports in a PSK Reporter reply; [] for unreadable XML."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    return _reports(root)


def _local(tag: str) -> str:
    """An element's name without its namespace ("{ns}receptionReport")."""
    return tag.rsplit("}", 1)[-1]


def _reports(root: ET.Element) -> list[dict]:
    spots = []
    for rep in root.iter():
        if _local(rep.tag) != "receptionReport":
            continue
        get = rep.attrib.get
        ts = get("flowStartSeconds")
        try:
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else ""
        except (TypeError, ValueError, OSError):
            when = ""
        sender_grid, receiver_grid = get("senderLocator") or "", get("receiverLocator") or ""
        s_ll, r_ll = grid_center(sender_grid), grid_center(receiver_grid)
        spots.append({
            "sender_callsign": (get("senderCallsign") or "").upper(),
            "sender_grid": sender_grid,
            "sender_lat": s_ll[0] if s_ll else None,
            "sender_lon": s_ll[1] if s_ll else None,
            "receiver_callsign": (get("receiverCallsign") or "").upper(),
            "receiver_grid": receiver_grid,
            "receiver_lat": r_ll[0] if r_ll else None,
            "receiver_lon": r_ll[1] if r_ll else None,
            "frequency_hz": _int(get("frequency")),
            "mode": get("mode") or "",
            "snr": _int(get("sNR")),
            "time": when,
        })
    return spots


def query_pskreporter(*, sender: str | None = None, receiver: str | None = None,
                      mode: str | None = None, max_wait: float = 8.0) -> list[dict]:
    """Query PSK Reporter. At least one of sender/receiver is required.
    Raises UpstreamError when PSK Reporter fails (network, HTTP error status,
    a reply that isn't a <receptionReports> document); [] means it answered
    with no reports."""
    params = {}
    if sender:
        params["senderCallsign"] = sender.upper()
    if receiver:
        params["receiverCallsign"] = receiver.upper()
    if mode:
        params["mode"] = mode
    if not params:
        return []
    s = get_settings()
    try:
        with httpx.Client(timeout=max_wait, headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(s.pskreporter_url, params=params)
    except httpx.HTTPError as exc:
        log.warning("[activity] pskreporter query failed: %s", exc)
        raise UpstreamError(f"pskreporter: {type(exc).__name__}") from None
    if resp.status_code != 200:
        log.warning("[activity] pskreporter returned %s", resp.status_code)
        raise UpstreamError(f"pskreporter: HTTP {resp.status_code}")
    try:
        root = ET.fromstring(resp.text.lstrip("\ufeff \t\r\n"))
    except ET.ParseError:
        root = None
    # a real answer is <receptionReports> (empty when nobody reported); any
    # other document is an error page
    if root is None or _local(root.tag) != "receptionReports":
        log.warning("[activity] pskreporter reply is not a report list: %r", resp.text[:120])
        raise UpstreamError("pskreporter: unreadable reply")
    spots = _reports(root)
    spots.sort(key=lambda s_: s_["time"], reverse=True)
    return spots[: get_settings().activity_max_spots]
