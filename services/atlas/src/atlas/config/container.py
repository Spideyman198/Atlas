"""The composition root.

The only module permitted to know which concrete adapter satisfies which domain
port. Everything else receives its collaborators by injection, which is what keeps
the application layer testable against fakes.

It also owns the lifetime of process-wide resources — connection pools now, model
provider clients from M3 — so nothing constructs a client at import time.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Self

from psycopg_pool import AsyncConnectionPool

from atlas.config.providers import build_providers
from atlas.config.settings import Settings, get_settings
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.domain.ports.vector_store import VectorStore
from atlas.infrastructure.persistence import PgVectorStore, register_vector

logger = logging.getLogger(__name__)


class Container:
    """Holds configuration and the resources built from it.

    Construct with :meth:`create` and release with :meth:`aclose`, or use it as an
    async context manager.
    """

    def __init__(
        self,
        settings: Settings,
        pool: AsyncConnectionPool,
        chat: ChatProvider,
        embedding: EmbeddingProvider,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._chat = chat
        self._embedding = embedding

    @property
    def settings(self) -> Settings:
        """Validated configuration for this process."""
        return self._settings

    @property
    def pool(self) -> AsyncConnectionPool:
        """Connection pool for the Atlas database."""
        return self._pool

    @property
    def chat(self) -> ChatProvider:
        """The configured chat provider, wrapped in its decorator stack."""
        return self._chat

    @property
    def embedding(self) -> EmbeddingProvider:
        """The configured embedding provider."""
        return self._embedding

    @property
    def vector_store(self) -> VectorStore:
        """The corpus store, over the same pool."""
        return PgVectorStore(self._pool)

    @classmethod
    async def create(cls, settings: Settings | None = None) -> Self:
        """Build the container and open its resources.

        The pool is opened without waiting. The process must start even when
        PostgreSQL is unreachable so that liveness can answer and readiness can
        report which dependency is missing; blocking here would make a slow
        database indistinguishable from a crashed service.
        """
        resolved = settings if settings is not None else get_settings()
        database = resolved.database

        # Built first: a missing API key, an unpriced model or a dimension
        # mismatch should stop the process here rather than surface on the first
        # user request.
        chat, embedding = build_providers(resolved)

        pool = AsyncConnectionPool(
            conninfo=database.url,
            min_size=database.pool_min_size,
            max_size=database.pool_max_size,
            timeout=database.connect_timeout_seconds,
            open=False,
            # Every connection learns the `vector` type, so embeddings bind as
            # parameters instead of being formatted into query text.
            configure=register_vector,
        )
        await pool.open(wait=False)

        logger.info(
            "container built",
            extra={
                "env": resolved.env,
                "pool_max_size": database.pool_max_size,
                "chat_provider": chat.name,
                "chat_model": chat.model,
                "embedding_provider": embedding.name,
                "embedding_model": embedding.model_id,
                "embedding_dimensions": embedding.dimensions,
            },
        )
        return cls(resolved, pool, chat, embedding)

    async def aclose(self) -> None:
        """Release every resource the container owns."""
        await self._pool.close()
        logger.info("container closed")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
