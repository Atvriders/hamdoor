"""Geographic helpers: haversine distance, bounding boxes, maidenhead locators."""

import math

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def bounding_box(lat: float, lon: float, radius_miles: float) -> tuple[float, float, float, float]:
    """(min_lat, max_lat, min_lon, max_lon) box containing the radius circle.

    Used as a cheap SQL prefilter before the exact haversine check.
    """
    dlat = radius_miles / 69.0
    cos_lat = max(0.01, abs(math.cos(math.radians(lat))))
    dlon = radius_miles / (69.0 * cos_lat)
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def grid_to_latlon(grid: str) -> tuple[float, float] | None:
    """Convert a maidenhead locator (4, 6 or 8 chars) to its center lat/lon.

    Returns None for invalid locators.
    """
    g = (grid or "").strip().upper()
    if len(g) not in (4, 6, 8):
        return None
    try:
        if not (g[0] in "ABCDEFGHIJKLMNOPQR" and g[1] in "ABCDEFGHIJKLMNOPQR"):
            return None
        lon = (ord(g[0]) - ord("A")) * 20 - 180
        lat = (ord(g[1]) - ord("A")) * 10 - 90
        if not (g[2].isdigit() and g[3].isdigit()):
            return None
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        if len(g) >= 6:
            if not ("A" <= g[4] <= "X" and "A" <= g[5] <= "X"):
                return None
            lon += (ord(g[4]) - ord("A")) * (5 / 60)
            lat += (ord(g[5]) - ord("A")) * (2.5 / 60)
            if len(g) == 8:
                if not (g[6].isdigit() and g[7].isdigit()):
                    return None
                lon += int(g[6]) * (5 / 600)
                lat += int(g[7]) * (2.5 / 600)
                return (lat + 2.5 / 1200, lon + 5 / 1200)
            return (lat + 2.5 / 120, lon + 5 / 120)
        return (lat + 0.5, lon + 1.0)
    except (IndexError, ValueError):
        return None
