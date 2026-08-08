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


@lru_cache
def get_settings() -> Settings:
    return Settings()
