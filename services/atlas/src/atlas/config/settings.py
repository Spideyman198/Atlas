"""Runtime configuration, read from the environment and validated once.

Configuration is fail-fast: a misconfigured deployment refuses to start rather
than surfacing the mistake hours later as a confusing runtime error.

Settings are grouped by concern rather than flattened, so ``ATLAS_DATABASE__URL``
and ``ATLAS_PROVIDER__CHAT_MODEL`` stay legible as the number of options grows.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["development", "staging", "production"]


class DatabaseSettings(BaseModel):
    """Connection settings for the Atlas database.

    This is the dedicated pgvector database described in ADR-0004, not Odoo's.
    """

    model_config = {"frozen": True}

    url: str = Field(description="libpq connection URL for the Atlas database.")
    pool_min_size: int = Field(
        default=0,
        ge=0,
        description=(
            "Connections held open when idle. Zero lets the process start before "
            "PostgreSQL is reachable, which readiness — not liveness — then reports."
        ),
    )
    pool_max_size: int = Field(default=8, ge=1)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)


class Settings(BaseSettings):
    """Engine configuration, sourced from ``ATLAS_*`` environment variables.

    Frozen because configuration that changes under a running process is a class of
    bug nobody enjoys diagnosing.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Environment = "development"
    service_name: str = "atlas-api"
    log_level: LogLevel = "INFO"
    log_json: bool = Field(
        default=True,
        description="JSON lines when true; a human-readable format when false.",
    )

    database: DatabaseSettings

    @property
    def is_production(self) -> bool:
        """True when running with production semantics."""
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so validation happens exactly once per process. Tests that need a
    different configuration must call ``get_settings.cache_clear()``.
    """
    # No arguments: pydantic-settings populates every field from the environment,
    # raising ValidationError if a required one is absent.
    return Settings()
