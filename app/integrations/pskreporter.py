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

log = logging.getLogger(__name__)


def parse_pskreporter_xml(text: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    spots = []
    for rep in root.iter("receptionReport"):
        get = rep.attrib.get
        ts = get("flowStartSeconds")
        try:
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else ""
        except (TypeError, ValueError, OSError):
            when = ""
        spots.append({
            "sender_callsign": (get("senderCallsign") or "").upper(),
            "sender_grid": get("senderLocator") or "",
            "receiver_callsign": (get("receiverCallsign") or "").upper(),
            "receiver_grid": get("receiverLocator") or "",
            "frequency_hz": int(get("frequency") or 0),
            "mode": get("mode") or "",
            "snr": int(get("sNR") or 0),
            "time": when,
        })
    return spots


def query_pskreporter(*, sender: str | None = None, receiver: str | None = None,
                      mode: str | None = None, max_wait: float = 8.0) -> list[dict]:
    """Query PSK Reporter. At least one of sender/receiver is required."""
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
        if resp.status_code != 200:
            log.warning("pskreporter returned %s", resp.status_code)
            return []
        spots = parse_pskreporter_xml(resp.text)
    except httpx.HTTPError as exc:
        log.warning("pskreporter query failed: %s", exc)
        return []
    spots.sort(key=lambda s_: s_["time"], reverse=True)
    return spots[: get_settings().activity_max_spots]
