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


def clamp_bbox(min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> tuple[float, float, float, float]:
    """Normalize a map viewport to valid coordinates.

    Leaflet reports west/east beyond ±180 when the view is wider than the
    world or panned across the antimeridian; clamp instead of rejecting.
    Swapped edges are put back in order. Returns (min_lat, max_lat, min_lon, max_lon).
    """
    min_lat, max_lat = sorted((min_lat, max_lat))
    min_lon, max_lon = sorted((min_lon, max_lon))
    if max_lon - min_lon >= 360:
        min_lon, max_lon = -180.0, 180.0
    return (max(-90.0, min_lat), min(90.0, max_lat), max(-180.0, min_lon), min(180.0, max_lon))


# map zoom -> cluster cell size in degrees (shared by every clustered map layer)
_CELL_BY_ZOOM = {4: 5.0, 5: 3.0, 6: 2.0, 7: 1.0, 8: 0.5, 9: 0.25,
                 10: 0.05, 11: 0.025, 12: 0.0125, 13: 0.00625}


def cell_degrees(zoom: int) -> float:
    """Cluster cell size for a zoom level. Below 4 → 6°, beyond 13 → the z13 size.
    180 is a multiple of every size, so cells never straddle the antimeridian."""
    if zoom < 4:
        return 6.0
    return _CELL_BY_ZOOM.get(zoom, _CELL_BY_ZOOM[13])


def snap_bbox(min_lat: float, max_lat: float, min_lon: float, max_lon: float,
              cell: float) -> tuple[float, float, float, float]:
    """Expand a bbox outward to whole cells so edge clusters count their whole
    cell (stable while panning) instead of only the visible sliver."""
    return (math.floor(min_lat / cell) * cell, (math.floor(max_lat / cell) + 1) * cell,
            math.floor(min_lon / cell) * cell, (math.floor(max_lon / cell) + 1) * cell)


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
