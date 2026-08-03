"""Hybrid retrieval: dense and lexical, fused, then diversified.

Two searches run over the same chunks and are good at different things. Dense
search finds a paragraph about late payment when somebody asks about overdue
invoices. Lexical search finds ``SO00035`` — an identifier that embeds to
nothing in particular and that dense search is reliably poor at.

Their scores are not comparable: cosine similarity and ``ts_rank_cd`` do not
share a scale, and normalising them would be inventing one. Reciprocal rank
fusion sidesteps that entirely by using only the *positions*, which is why it is
the standard answer and why ADR-0004 named it.

The fusion itself is LlamaIndex's, per ADR-0003: algorithms come from the
library, storage and transport stay ours.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES
from llama_index.core.schema import NodeWithScore, QueryBundle
from llama_index.core.vector_stores.types import VectorStoreQuery, VectorStoreQueryMode

from atlas.domain.corpus import CandidateChunk
from atlas.domain.embedding import EmbeddingPurpose
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.domain.ports.retriever import Reranker
from atlas.domain.ports.vector_store import VectorStore
from atlas.domain.retrieval import RetrievalRequest
from atlas.infrastructure.llamaindex.bridges import (
    AtlasLlamaLLM,
    AtlasLlamaVectorStore,
    to_candidate,
)
from atlas.infrastructure.retrieval.diversity import DEFAULT_LAMBDA, maximal_marginal_relevance
from atlas.infrastructure.retrieval.rerank import NoOpReranker

logger = logging.getLogger(__name__)

#: One query, so fusion never asks a language model to invent variations of it.
#: Query expansion is a real technique and a real cost — a model call before the
#: model call — and it belongs behind a measurement (M12), not on by default.
_SINGLE_QUERY: Final = 1

_SYNC_REFUSED: Final = (
    "Atlas retrievers are async-only: the store and the embedding provider "
    "beneath them are async, and spinning an event loop here would deadlock any "
    "caller that already has one"
)


class _DenseRetriever(BaseRetriever):
    """The semantic half. Embeds the query, then asks for nearest neighbours."""

    def __init__(
        self,
        bridge: AtlasLlamaVectorStore,
        embedder: EmbeddingProvider,
        limit: int,
    ) -> None:
        super().__init__()
        self._bridge = bridge
        self._embedder = embedder
        self._limit = limit

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        raise NotImplementedError(_SYNC_REFUSED)

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        embedded = await self._embedder.embed([query_bundle.query_str], EmbeddingPurpose.QUERY)
        result = await self._bridge.aquery(
            VectorStoreQuery(
                query_embedding=list(embedded.vectors[0]),
                similarity_top_k=self._limit,
                mode=VectorStoreQueryMode.DEFAULT,
            )
        )
        return _to_scored(result)


class _LexicalRetriever(BaseRetriever):
    """The keyword half. Catches the exact strings dense search loses."""

    def __init__(self, bridge: AtlasLlamaVectorStore, limit: int) -> None:
        super().__init__()
        self._bridge = bridge
        self._limit = limit

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        raise NotImplementedError(_SYNC_REFUSED)

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        result = await self._bridge.aquery(
            VectorStoreQuery(
                query_str=query_bundle.query_str,
                similarity_top_k=self._limit,
                mode=VectorStoreQueryMode.TEXT_SEARCH,
            )
        )
        return _to_scored(result)


class LlamaIndexHybridRetriever:
    """A :class:`~atlas.domain.ports.retriever.Retriever` over both search modes.

    Args:
        store: The corpus. Reached through the LlamaIndex bridge, so the schema
            and the SQL stay ours.
        embedder: Embeds the query. The same provider, retry policy and cost
            meter as ingestion (ADR-0005).
        reranker: Optional. Nothing reranks by default; see
            ``atlas.infrastructure.retrieval.rerank`` for why.
        mmr_lambda: Relevance weight for the diversity pass. ``1.0`` disables it.
        chat: Only ever used to stop LlamaIndex reaching for a vendor of its
            own. Retrieval asks no language model anything; leaving this unset
            gives the fusion retriever a bridge that refuses, which makes that
            a guarantee rather than a comment.
    """

    def __init__(
        self,
        *,
        store: VectorStore,
        embedder: EmbeddingProvider,
        reranker: Reranker | None = None,
        mmr_lambda: float = DEFAULT_LAMBDA,
        chat: ChatProvider | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker or NoOpReranker()
        self._mmr_lambda = mmr_lambda
        self._llm = AtlasLlamaLLM(chat)

    async def retrieve(self, request: RetrievalRequest) -> list[CandidateChunk]:
        """Search both ways, fuse, diversify, and hand back candidates.

        The result is deliberately *unauthorized*. Nothing here knows who is
        asking; that is settled by the application layer, which is the only
        thing that can turn one of these into an
        :class:`~atlas.domain.corpus.AuthorizedChunk` (ADR-0006).
        """
        # Built per request because it carries the pre-filter. Two references
        # and no I/O, so this is cheaper than threading the filter through
        # LlamaIndex's query types would be.
        bridge = AtlasLlamaVectorStore(self._store, filters=request.search_filter())
        wanted = request.candidate_limit

        fusion = QueryFusionRetriever(
            retrievers=[
                _DenseRetriever(bridge, self._embedder, wanted),
                _LexicalRetriever(bridge, wanted),
            ],
            mode=FUSION_MODES.RECIPROCAL_RANK,
            num_queries=_SINGLE_QUERY,
            similarity_top_k=wanted,
            use_async=True,
            # Not optional. Left as None, LlamaIndex resolves `Settings.llm`,
            # tries to import its OpenAI integration, and either fails or —
            # worse, if that package is ever installed — opens a second path to
            # a vendor with its own retry policy and cost meter. The bridge
            # refuses every call, which is the correct behaviour: with a single
            # query, fusion never generates anything.
            llm=self._llm,
        )

        fused = await fusion.aretrieve(QueryBundle(request.query))
        candidates = [to_candidate(scored.node, scored.score or 0.0) for scored in fused]

        diversified = maximal_marginal_relevance(candidates, limit=wanted, lambda_=self._mmr_lambda)
        ranked = await self._reranker.rerank(request.query, diversified, limit=wanted)

        logger.info(
            "hybrid retrieval",
            extra={
                "fused": len(candidates),
                "returned": len(ranked),
                "limit": request.limit,
                "over_fetch": request.over_fetch,
                "reranker": getattr(self._reranker, "name", type(self._reranker).__name__),
            },
        )
        return ranked


def _to_scored(result: Any) -> list[NodeWithScore]:
    """Pair nodes with their scores, tolerating a store that returned neither."""
    nodes = list(result.nodes or [])
    similarities = list(result.similarities or [])
    return [
        NodeWithScore(node=node, score=similarities[index] if index < len(similarities) else 0.0)
        for index, node in enumerate(nodes)
    ]
