"""Runtime configuration, read from the environment and validated once.

Configuration is **fail-fast**: a misconfigured deployment refuses to start
rather than surfacing the mistake hours later as a confusing runtime error. That
trade — a loud failure at boot over a quiet one under load — is deliberate.

M1 defines only what the operational probes need. M2 grows this into the full
settings model alongside the composition root.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    """Engine configuration, sourced from ``ATLAS_*`` environment variables.

    Frozen because configuration that changes underneath a running process is a
    class of bug nobody enjoys diagnosing.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Environment = "development"
    log_level: LogLevel = "INFO"
    service_name: str = "atlas-api"

    database_url: str = Field(
        description=(
            "libpq connection URL for the Atlas database — the dedicated pgvector "
            "database described in docs/adr/0004-vector-store-and-index-strategy.md. "
            "This is NOT Odoo's database."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that validation happens exactly once per process. Tests that need a
    different configuration must call ``get_settings.cache_clear()``.
    """
    # No arguments: pydantic-settings populates every field from the environment,
    # raising ValidationError if a required one is absent.
    return Settings()
