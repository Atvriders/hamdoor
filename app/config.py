from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, overridable via environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite:////data/hamdoor.db"
    # SQLite write-ahead log (readers never block writers); false keeps the
    # rollback journal, for a data dir on a network or FUSE filesystem
    sqlite_wal: bool = True
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 14  # two weeks
    # throttle log in, sign up and callsign lookup (429 + Retry-After)
    rate_limit_enabled: bool = True
    # header that carries the visitor's real address when every request comes
    # through a proxy that sets it (e.g. "CF-Connecting-IP" behind Cloudflare,
    # "X-Real-IP" behind nginx). Blank = the connecting address. Never set it
    # when the server is also reachable directly: the header could be forged.
    trusted_ip_header: str = ""
    # largest request body accepted (bytes); a post is at most 10,000 characters
    max_request_bytes: int = 128 * 1024
    # HTTP calls to callsign provider / geocoder
    http_timeout_seconds: float = 8.0
    http_user_agent: str = "hamdoor/1.0 (https://github.com/Atvriders/hamdoor)"
    callook_base_url: str = "https://callook.info"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    # range limits (miles)
    min_range_miles: int = 5
    max_range_miles: int = 500
    default_range_miles: int = 25
    # ---- activity toolbox sources ----
    pskreporter_url: str = "https://pskreporter.info/query"
    wspr_live_url: str = "https://db1.wspr.live/"
    pota_url: str = "https://api.pota.app/spot/activator"
    sota_url: str = "https://api2.sota.org.uk/api/spots/-2/h/all"
    hamqsl_url: str = "https://www.hamqsl.com/solarxml.php"
    # regional APRS-IS pool (rotate.aprs2.net can land on servers that refuse
    # some logins)
    aprs_host: str = "noam.aprs2.net"
    aprs_port: int = 14580
    dxc_host: str = "ve7cc.net"
    dxc_port: int = 23
    rbn_host: str = "telnet.reversebeacon.net"
    rbn_port: int = 7000
    # seconds to sample live feeds (APRS-IS / cluster / RBN) per request
    activity_sample_seconds: float = 15.0
    # cache TTL for activity responses (shared across users)
    activity_cache_seconds: float = 60.0
    activity_max_spots: int = 200
    # ---- FCC ULS whole-database import ----
    uls_import_enabled: bool = True
    uls_url: str = "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"
    geonames_url: str = "https://download.geonames.org/export/zip/US.zip"
    uls_refresh_days: int = 7
    # scheduler wakeup interval (hours) for checking refresh staleness
    uls_check_interval_hours: int = 24
    # map endpoint caps
    hams_map_max_results: int = 10000
    # a ham is "newly licensed" for this many days after the FCC grant date
    new_ham_days: int = 90
    # ---- Census batch geocoding (street-level ham locations) ----
    census_url: str = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
    census_batch_size: int = 9000
    census_timeout_seconds: float = 300.0
    # backfill runs when more than this many addresses lack a geocode
    geocode_backfill_threshold: int = 500
    # prebuilt weekly geocode cache (built by CI); containers download this
    # instead of geocoding everything themselves
    data_release_url: str = (
        "https://github.com/Atvriders/hamdoor/releases/download/uls-data-latest/geocodes.sqlite.gz"
    )
    data_release_enabled: bool = True

    # ---- map basemaps (sent to the browser by GET /api/config) ----
    map_streets_style_url: str = "https://tiles.openfreemap.org/styles/liberty"
    map_dark_style_url: str = "https://tiles.openfreemap.org/styles/dark"
    map_osm_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_satellite_url: str = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    )
    map_topo_url: str = (
        "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
    )

    # ---- amateur repeaters (HearHam + BrandMeister + IRLP, fetched live) ----
    repeaters_enabled: bool = True
    hearham_url: str = "https://hearham.com/api/repeaters/v1"
    brandmeister_url: str = "https://api.brandmeister.network/v2/device?repeater=true"
    irlp_url: str = "https://status.irlp.net/nohtmlstatus.txt.bz2"
    repeaters_refresh_days: int = 7
    brandmeister_refresh_hours: int = 24
    irlp_refresh_hours: int = 12
    repeaters_map_max_results: int = 2000

    # ---- live APRS traffic (one receive-only APRS-IS connection) ----
    aprs_feed_enabled: bool = True
    # your callsign plus an app SSID, e.g. N0CALL-HD; blank = auto-generated
    aprs_login: str = ""
    aprs_feed_filter: str = "a/72/-180/17/-64"
    aprs_retention_hours: float = 6.0
    aprs_track_max_points: int = 120
    aprs_map_max_results: int = 3000
    aprs_snapshot_enabled: bool = True
    # optional aprs.fi API key: enables on-demand callsign lookups
    aprsfi_api_key: str = ""

    # ---- FCC directory and pin accuracy additions ----
    # addresses the Census geocoder could not match are retried after this
    # many days (instead of on every scheduler pass)
    geocode_miss_retry_days: int = 30

    # ---- repeaters additions ----
    # GeoNames populated places (CC-BY 4.0), fetched once per 90 days: judges
    # repeater coordinates (city centroid, mid-ocean, sign-flip repair) and
    # tells which country a listing is in. Never served to browsers.
    geonames_cities_url: str = "https://download.geonames.org/export/dump/cities500.zip"


@lru_cache
def get_settings() -> Settings:
    return Settings()
