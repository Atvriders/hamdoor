from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, overridable via environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite:////data/hamdoor.db"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 14  # two weeks
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
    aprs_host: str = "rotate.aprs2.net"
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
    hams_map_max_results: int = 1500
    # a ham is "newly licensed" for this many days after the FCC grant date
    new_ham_days: int = 90


@lru_cache
def get_settings() -> Settings:
    return Settings()
