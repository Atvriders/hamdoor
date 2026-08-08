"""callook.info callsign lookup (free, no API key, US callsigns).

Response shape (https://callook.info/{call}/json):
{
  "status": "VALID",
  "type": "PERSON",
  "current": {"callsign": "W1AW", "operClass": "E"},
  "name": "Hiram Percy Maxim Memorial Station",
  "address": {"line1": "225 Main St", "line2": "Newington, CT 06111", ...},
  "location": {"latitude": "...", "longitude": "...", "gridsquare": "FN31pr"},
  "otherInfo": {"expiryDate": "...", ...}
}
"""

import logging

import httpx

from app.config import get_settings
from app.integrations.provider import CallsignProvider, CallsignRecord

log = logging.getLogger(__name__)


class CallookProvider(CallsignProvider):
    name = "callook.info"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def lookup(self, callsign: str) -> CallsignRecord | None:
        s = get_settings()
        url = f"{s.callook_base_url}/{callsign}/json"
        try:
            if self._client is not None:
                resp = self._client.get(url)
            else:
                with httpx.Client(
                    timeout=s.http_timeout_seconds,
                    headers={"User-Agent": s.http_user_agent},
                ) as client:
                    resp = client.get(url)
        except httpx.HTTPError as exc:
            log.warning("callook lookup failed for %s: %s", callsign, exc)
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if data.get("status") != "VALID":
            return None
        return self._parse(callsign, data)

    @staticmethod
    def _parse(callsign: str, data: dict) -> CallsignRecord:
        addr = data.get("address") or {}
        loc = data.get("location") or {}
        current = data.get("current") or {}
        other = data.get("otherInfo") or {}

        # line2 is typically "City, ST ZIP"
        city, state, zip_ = "", "", ""
        line2 = addr.get("line2", "")
        if "," in line2:
            city, _, rest = line2.partition(",")
            parts = rest.strip().split()
            if parts:
                state = parts[0]
                zip_ = parts[1] if len(parts) > 1 else ""

        return CallsignRecord(
            callsign=callsign.upper(),
            name=(data.get("name") or "").strip(),
            address_line=(addr.get("line1") or "").strip(),
            city=city.strip(),
            state=state.strip(),
            zip=zip_.strip(),
            grid=(loc.get("gridsquare") or "").strip(),
            license_class=(current.get("operClass") or "").strip(),
            expires=(other.get("expiryDate") or "").strip(),
            source=CallookProvider.name,
        )
