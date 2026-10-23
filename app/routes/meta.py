"""Browser configuration: basemaps and feature switches.

Public (no auth): it holds no secrets, and the sign-in page may use it.
"""

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(prefix="/api", tags=["meta"])

_OSM = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
_OFM = ('<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> '
        '&copy; <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> '
        'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>')


@router.get("/config")
def client_config():
    s = get_settings()
    basemaps = [
        # "gl" = vector style for MapLibre GL (via maplibre-gl-leaflet); the
        # browser falls back to "fallback" when WebGL or the library is missing
        {"id": "streets", "label": "Streets", "kind": "gl", "url": s.map_streets_style_url,
         "tone": "light", "attribution": _OFM, "fallback": "osm"},
        {"id": "satellite", "label": "Satellite", "kind": "raster", "url": s.map_satellite_url,
         "tone": "imagery", "max_native_zoom": 19,
         "attribution": "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"},
        {"id": "topo", "label": "Topo", "kind": "raster", "url": s.map_topo_url,
         "tone": "light", "max_native_zoom": 16,
         "attribution": 'Tiles courtesy of the <a href="https://www.usgs.gov/" target="_blank" rel="noopener">U.S. Geological Survey</a>'},
        {"id": "dark", "label": "Dark", "kind": "gl", "url": s.map_dark_style_url,
         "tone": "dark", "attribution": _OFM, "fallback": "osm"},
        {"id": "osm", "label": "OSM", "kind": "raster", "url": s.map_osm_url,
         "tone": "light", "max_native_zoom": 19, "attribution": _OSM},
    ]
    return {
        "basemaps": basemaps,
        "default_basemap": "streets",
        "features": {
            "repeaters": s.repeaters_enabled,
            "aprs": s.aprs_feed_enabled,
            "aprsfi_lookup": bool(s.aprsfi_api_key),
        },
        "new_ham_days": s.new_ham_days,
        "aprs_window_max_s": int(s.aprs_retention_hours * 3600),
    }
