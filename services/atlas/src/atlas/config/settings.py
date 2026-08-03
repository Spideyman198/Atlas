"""Runtime configuration, read from the environment and validated once.

Configuration is fail-fast: a misconfigured deployment refuses to start rather
than surfacing the mistake hours later as a confusing runtime error.

Settings are grouped by concern rather than flattened, so ``ATLAS_DATABASE__URL``
and ``ATLAS_PROVIDER__CHAT_MODEL`` stay legible as the number of options grows.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
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
    history_budget: int = Field(
        default=1500,
        gt=0,
        description=(
            "Tokens of conversation history a prompt may carry before older "
            "turns are summarised. Deliberately smaller than the retrieval "
            "budget: history competes with the context that grounds the answer."
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


class OdooSettings(BaseModel):
    """How the engine reaches Odoo to authorize what it retrieved.

    Odoo is the authorization authority (ADR-0006), so these are not optional
    conveniences: without them the engine can retrieve candidates and can never
    clear any of them for use.
    """

    model_config = {"frozen": True}

    base_url: str = Field(
        default="http://odoo:8069",
        description="Odoo's origin, reachable from this process. An internal address.",
    )
    database: str = Field(
        default="odoo",
        description=(
            "Which Odoo database to address. Sent as the X-Odoo-Database header, "
            "because these calls deliberately carry no session."
        ),
    )
    service_token: SecretStr | None = Field(
        default=None,
        description=(
            "Shared secret proving to Odoo that a call came from the engine. The "
            "engine is given only this one: the key that signs user context tokens "
            "stays on Odoo's side, so the engine cannot mint a context of its own."
        ),
    )
    timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Hard ceiling on one authorization call. Sits inside a multi-second "
            "answer, so a slow Odoo must fail rather than consume the whole budget."
        ),
    )
    max_ids_per_call: int = Field(
        default=500,
        gt=0,
        description="Matches the addon's own limit, so an over-large batch fails here first.",
    )
    ingest_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description=(
            "Budget for one page of ingestion reads. Far longer than the "
            "query-time timeout: a hundred orders with their lines is real work, "
            "and nobody is waiting on it."
        ),
    )


class IngestionSettings(BaseModel):
    """How the cold path behaves.

    Chunking parameters sit here rather than in the loader because they are a
    retrieval strategy, not an implementation detail — ADR-0003 puts chunking
    inside the adapter and exposes it through settings, which is this.
    """

    model_config = {"frozen": True}

    page_size: int = Field(
        default=100,
        gt=0,
        description="Records read from Odoo per round-trip.",
    )
    chunk_size: int = Field(
        default=512,
        gt=0,
        description="Tokens per segment. Retrieval quality is mostly decided here.",
    )
    chunk_overlap: int = Field(
        default=64,
        ge=0,
        description="Tokens neighbouring segments share, so a fact on a boundary survives.",
    )
    worker_poll_seconds: float = Field(
        default=5.0,
        gt=0,
        description="How long a worker waits before asking for work again when the queue is empty.",
    )
    max_attempts: int = Field(
        default=5,
        ge=1,
        description="Attempts before a job is declared dead and left for a person.",
    )
    retry_backoff_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Base of the exponential backoff between attempts.",
    )
    stale_job_seconds: float = Field(
        default=900.0,
        gt=0,
        description=(
            "How long a claimed job may sit untouched before it is assumed its "
            "worker died and returned to the queue."
        ),
    )

    @model_validator(mode="after")
    def _overlap_fits_inside_a_chunk(self) -> Self:
        """An overlap at least as large as the chunk never terminates.

        Every segment would contain the whole of the previous one, so the
        splitter makes no progress. Rejecting it at start-up is kinder than a
        sync that runs until the disk fills.
        """
        if self.chunk_overlap >= self.chunk_size:
            message = (
                f"chunk_overlap ({self.chunk_overlap}) must be smaller than "
                f"chunk_size ({self.chunk_size})"
            )
            raise ValueError(message)
        return self


class RetrievalSettings(BaseModel):
    """How retrieval behaves. Every value here is a quality/cost trade."""

    model_config = {"frozen": True}

    limit: int = Field(
        default=8,
        gt=0,
        description="Chunks offered to the prompt, after authorization.",
    )
    over_fetch: int = Field(
        default=4,
        gt=0,
        description=(
            "Candidates fetched per result wanted. Authorization discards an "
            "unknown fraction of them, and the denial rate is not knowable in "
            "advance (ADR-0006). M13 makes this adaptive."
        ),
    )
    mmr_lambda: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Relevance weight in the diversity pass. 1.0 disables it and returns "
            "pure relevance order, near-duplicates and all."
        ),
    )
    token_budget: int = Field(
        default=4000,
        gt=0,
        description="Tokens of context a single answer may be grounded on.",
    )


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
    odoo: OdooSettings = OdooSettings()
    ingestion: IngestionSettings = IngestionSettings()
    retrieval: RetrievalSettings = RetrievalSettings()

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
