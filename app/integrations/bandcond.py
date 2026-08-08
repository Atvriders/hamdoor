"""Solar / band conditions from hamqsl.com's keyless solarxml.php feed."""

import logging
import xml.etree.ElementTree as ET

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

BAND_KEYS = ("80m-40m", "30m-20m", "17m-15m", "12m-10m")


def parse_solarxml(text: str) -> dict:
    root = ET.fromstring(text)
    data = root.find("solardata")
    if data is None:
        data = root

    def txt(tag: str) -> str:
        el = data.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    bands = []
    # <band> elements live inside <calculatedconditions> — search the whole tree
    for el in root.iter("band"):
        bands.append({
            "band": el.attrib.get("name", ""),
            "time": el.attrib.get("time", ""),
            "condition": (el.text or "").strip(),
        })
    return {
        "updated": txt("updated"),
        "solar_flux": txt("solarflux"),
        "a_index": txt("aindex"),
        "k_index": txt("kindex"),
        "sunspots": txt("sunspots"),
        "xray": txt("xray"),
        "solar_wind": txt("solarwind"),
        "geomag": txt("geomagfield") or txt("magneticfield"),
        "signal_noise": txt("signalnoise"),
        "bands": bands,
    }


def query_band_conditions() -> dict | None:
    s = get_settings()
    try:
        with httpx.Client(timeout=s.http_timeout_seconds,
                          headers={"User-Agent": s.http_user_agent}) as client:
            resp = client.get(s.hamqsl_url)
        if resp.status_code != 200:
            return None
        return parse_solarxml(resp.text)
    except (httpx.HTTPError, ET.ParseError) as exc:
        log.warning("band conditions query failed: %s", exc)
        return None
