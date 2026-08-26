"""
All configuration is sourced from environment variables (optionally via a
.env file for local dev) so the app is safe to run in any container
orchestrator without touching code. See .env.example for the full list.
"""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="URLSHORT_", extra="ignore")

    # Required-in-practice: point this at your Postgres instance.
    database_url: str = "postgresql://postgres:postgres@localhost:5432/urlshortener"

    # Used to build the short_url returned to clients. Set this to your
    # public-facing domain/scheme in production (e.g. https://short.example.com).
    base_url: str = "http://localhost:8000"

    short_code_length: int = 7
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20

    # Use an UNLOGGED table for faster writes (skips WAL). Set to "false" if
    # you need the mapping table to survive a Postgres crash.
    unlogged_table: bool = True

    # Max entries held in the in-process LRU cache per app instance.
    cache_max_size: int = 10000

    # How often buffered hit-counters are flushed to Postgres.
    hit_flush_interval_seconds: float = 2.0

    # Optional global default expiry (days) applied when a request doesn't
    # specify one. Leave unset for links that never expire.
    default_expiry_days: Optional[int] = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
