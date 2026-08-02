"""Runtime configuration, read from the environment and validated once.

Configuration is fail-fast: a misconfigured deployment refuses to start rather
than surfacing the mistake hours later as a confusing runtime error.

Settings are grouped by concern rather than flattened, so ``ATLAS_DATABASE__URL``
and ``ATLAS_PROVIDER__CHAT_MODEL`` stay legible as the number of options grows.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["development", "staging", "production"]

#: `fake` and `hash` select the offline providers. They are configuration values
#: rather than a test-only import so an air-gapped demo or a smoke environment can
#: run the whole stack without a vendor account.
ChatVendor = Literal["anthropic", "openai", "fake"]
EmbeddingVendor = Literal["openai", "voyage", "hash"]


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


class ChatSettings(BaseModel):
    """Which model answers questions, and how it is reached."""

    model_config = {"frozen": True}

    vendor: ChatVendor = "anthropic"
    model: str = "claude-opus-5"
    api_key: SecretStr | None = None
    base_url: str | None = Field(
        default=None,
        description="Override the API host. This is how Azure OpenAI is reached.",
    )
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(
        default=3,
        ge=1,
        description=(
            "Total attempts including the first. Applied by the Atlas retry "
            "decorator; the vendor SDK's own retrying is disabled so there is one "
            "backoff policy rather than two multiplying together."
        ),
    )
    max_output_tokens: int = Field(
        default=8192,
        gt=0,
        description=(
            "Caps reasoning and answer together on current models, so a tight "
            "budget truncates mid-response rather than shortening the answer."
        ),
    )


class EmbeddingSettings(BaseModel):
    """Which model produces vectors, and the shape they must have."""

    model_config = {"frozen": True}

    vendor: EmbeddingVendor = "openai"
    model: str = "text-embedding-3-small"
    api_key: SecretStr | None = None
    dimensions: int = Field(
        default=1536,
        gt=0,
        description=(
            "Baked into the pgvector column type. Changing it is a re-index, not "
            "a configuration change (ADR-0005)."
        ),
    )
    max_batch_size: int = Field(default=96, gt=0)
    timeout_seconds: float = Field(default=30.0, gt=0)


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
    chat: ChatSettings = ChatSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()

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
