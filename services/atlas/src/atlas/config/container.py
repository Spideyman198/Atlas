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

from atlas.application.authorization import AuthorizationFilter
from atlas.application.ingestion import SyncSource
from atlas.application.memory import ConversationMemory
from atlas.application.retrieval import ContextAssembler, RetrievalPipeline
from atlas.application.synthesis import AnswerBudget, AnswerService
from atlas.application.tools import ToolBox
from atlas.config.providers import build_providers
from atlas.config.settings import Settings, get_settings
from atlas.domain.errors import ConfigurationError
from atlas.domain.observability import Recorder
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.domain.ports.prompts import PromptLibrary
from atlas.domain.ports.retriever import Retriever
from atlas.domain.ports.vector_store import VectorStore
from atlas.infrastructure.llamaindex import LlamaIndexDocumentLoader, LlamaIndexHybridRetriever
from atlas.infrastructure.observability.recorder import PrometheusRecorder
from atlas.infrastructure.odoo import OdooHttpGateway, OdooHttpSourceReader
from atlas.infrastructure.persistence import (
    PgEmbeddingCache,
    PgJobQueue,
    PgSourceState,
    PgVectorStore,
    register_vector,
)
from atlas.infrastructure.prompts import JinjaPromptLibrary

logger = logging.getLogger(__name__)


class Container:
    """Holds configuration and the resources built from it.

    Construct with :meth:`create` and release with :meth:`aclose`, or use it as an
    async context manager.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - the composition root holds what it builds
        self,
        settings: Settings,
        pool: AsyncConnectionPool,
        chat: ChatProvider,
        embedding: EmbeddingProvider,
        odoo: OdooHttpGateway,
        source_reader: OdooHttpSourceReader,
        loader: LlamaIndexDocumentLoader,
        prompts: PromptLibrary,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._chat = chat
        self._embedding = embedding
        self._odoo = odoo
        self._source_reader = source_reader
        self._loader = loader
        self._prompts = prompts
        self._recorder = PrometheusRecorder()

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

    @property
    def job_queue(self) -> PgJobQueue:
        """The ingestion queue, over the same pool."""
        ingestion = self._settings.ingestion
        return PgJobQueue(
            self._pool,
            max_attempts=ingestion.max_attempts,
            backoff_seconds=ingestion.retry_backoff_seconds,
        )

    @property
    def source_state(self) -> PgSourceState:
        """Per-source registration and watermarks."""
        return PgSourceState(self._pool)

    @property
    def embedding_cache(self) -> PgEmbeddingCache:
        """Vectors already paid for."""
        return PgEmbeddingCache(self._pool)

    @property
    def source_reader(self) -> OdooHttpSourceReader:
        """Reads Odoo records for indexing, as the integration user."""
        return self._source_reader

    @property
    def document_loader(self) -> LlamaIndexDocumentLoader:
        """Chunking and file extraction.

        Built once and reused: the splitter loads a tokeniser, and doing that
        per document would dominate the sync.
        """
        return self._loader

    def sync_source(self) -> SyncSource:
        """The ingestion use case, wired to this container's collaborators."""
        return SyncSource(
            reader=self._source_reader,
            loader=self._loader,
            embedder=self._embedding,
            store=self.vector_store,
            cache=self.embedding_cache,
            state=self.source_state,
            page_size=self._settings.ingestion.page_size,
        )

    @property
    def retriever(self) -> Retriever:
        """Hybrid retrieval over the corpus.

        The chat provider is passed only so LlamaIndex cannot reach for a vendor
        of its own; retrieval asks no language model anything
        (``atlas.infrastructure.llamaindex.bridges``).
        """
        return LlamaIndexHybridRetriever(
            store=self.vector_store,
            embedder=self._embedding,
            chat=self._chat,
            mmr_lambda=self._settings.retrieval.mmr_lambda,
        )

    def retrieval_pipeline(self) -> RetrievalPipeline:
        """Retrieve, authorize, assemble — wired to this container.

        The authorization stage is not optional and takes no configuration that
        could switch it off (ADR-0006).
        """
        return RetrievalPipeline(
            retriever=self.retriever,
            authorization=AuthorizationFilter(self._odoo),
            assembler=ContextAssembler(),
            recorder=self._recorder,
        )

    @property
    def recorder(self) -> Recorder:
        """Where the application layer reports what it did."""
        return self._recorder

    @property
    def prompts(self) -> PromptLibrary:
        """The prompt templates, verified once per process.

        Built eagerly in :meth:`create` rather than here: a missing template is
        a deployment error, and finding it at startup beats finding it on the
        first question somebody asks.
        """
        return self._prompts

    @property
    def tools(self) -> ToolBox:
        """The typed tools, executed by Odoo as the acting user."""
        return ToolBox(self._odoo)

    @property
    def answers(self) -> AnswerService:
        """The orchestrator: route, gather, generate, resolve citations."""
        return AnswerService(
            chat=self._chat,
            prompts=self._prompts,
            retrieval=self.retrieval_pipeline(),
            tools=self.tools,
            memory=ConversationMemory(
                chat=self._chat,
                prompts=self._prompts,
                budget=self._settings.chat.history_budget,
            ),
            recorder=self._recorder,
            budget=AnswerBudget(
                retrieval_limit=self._settings.retrieval.limit,
                token_budget=self._settings.retrieval.token_budget,
                max_output_tokens=self._settings.chat.max_output_tokens,
            ),
        )

    @property
    def odoo(self) -> OdooHttpGateway:
        """The authorization authority (ADR-0006).

        Typed as the concrete adapter rather than the port because readiness
        calls :meth:`~OdooHttpGateway.status`, which acts for no user and is
        therefore not part of what the application layer is allowed to do.
        """
        return self._odoo

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
        odoo = _build_odoo_gateway(resolved)
        source_reader = _build_source_reader(resolved)
        loader = LlamaIndexDocumentLoader(
            chunk_size=resolved.ingestion.chunk_size,
            chunk_overlap=resolved.ingestion.chunk_overlap,
        )
        # Verifies every template it declares. A prompt file that did not make
        # it into the image fails here, not on the first question asked.
        prompts = JinjaPromptLibrary()

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
                "odoo_base_url": resolved.odoo.base_url,
                "odoo_database": resolved.odoo.database,
            },
        )
        return cls(resolved, pool, chat, embedding, odoo, source_reader, loader, prompts)

    async def aclose(self) -> None:
        """Release every resource the container owns."""
        await self._odoo.aclose()
        await self._source_reader.aclose()
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


def _build_odoo_gateway(settings: Settings) -> OdooHttpGateway:
    """Build the gateway, refusing to start without the shared secret.

    A missing token is not a degraded mode. Odoo would refuse every call, so the
    engine could retrieve candidates and never clear one — an outage that looks
    like "the assistant knows nothing" rather than like a misconfiguration.
    Failing here says which environment variable is missing instead.
    """
    odoo = settings.odoo
    if odoo.service_token is None or not odoo.service_token.get_secret_value():
        message = "ATLAS_ODOO__SERVICE_TOKEN is required: Odoo authorises every retrieval"
        raise ConfigurationError(message)

    return OdooHttpGateway(
        base_url=odoo.base_url,
        database=odoo.database,
        service_token=odoo.service_token.get_secret_value(),
        timeout_seconds=odoo.timeout_seconds,
        max_ids_per_call=odoo.max_ids_per_call,
    )


def _build_source_reader(settings: Settings) -> OdooHttpSourceReader:
    """Build the ingestion reader.

    Shares the service token with the gateway but not the timeout: ingestion
    reads pages of a hundred orders with their lines, and nobody is waiting on
    it, so it gets a far longer budget than a query-time authorization call.
    """
    odoo = settings.odoo
    token = odoo.service_token.get_secret_value() if odoo.service_token else ""
    return OdooHttpSourceReader(
        base_url=odoo.base_url,
        database=odoo.database,
        service_token=token,
        timeout_seconds=odoo.ingest_timeout_seconds,
    )
