"""The retrieval ports.

``Retriever`` returns :class:`~atlas.domain.corpus.CandidateChunk` — named for
what it is not. Nothing here decides what anybody may see; that is settled
afterwards, by asking Odoo (ADR-0006). Keeping the port's return type
unauthorized is what makes the authorization stage impossible to forget rather
than merely easy to remember.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from atlas.domain.corpus import CandidateChunk
from atlas.domain.retrieval import RetrievalRequest


@runtime_checkable
class Retriever(Protocol):
    """Query text in, ranked candidates out.

    How the ranking happens — dense search, lexical search, fusion, diversity,
    reranking — is entirely the adapter's business. That is the point of the
    port: retrieval strategy is where most of the quality work will happen
    (M12, M13), and it should be replaceable without anything above it noticing.
    """

    async def retrieve(self, request: RetrievalRequest) -> list[CandidateChunk]:
        """Return candidates for a query, best first.

        Returns at most ``request.candidate_limit`` items: retrieval
        deliberately over-fetches, because authorization will discard an unknown
        fraction of them and the denial rate is not knowable in advance.

        Raises:
            StorageError: The index could not be searched.
            ProviderError: The query could not be embedded.
        """
        ...


@runtime_checkable
class Reranker(Protocol):
    """Re-scores candidates against the query with a stronger model.

    Bi-encoder retrieval scores a query and a chunk independently, which is what
    makes it fast enough to run over a whole corpus and also what limits it. A
    cross-encoder reads both together and is markedly better at ordering the
    top few — at a latency and dependency cost that has to be earned.

    The port exists now so the pipeline has somewhere to put one. The default
    implementation does nothing; see ``atlas.infrastructure.retrieval.rerank``.
    """

    async def rerank(
        self,
        query: str,
        candidates: Sequence[CandidateChunk],
        *,
        limit: int,
    ) -> list[CandidateChunk]:
        """Return the best ``limit`` candidates, best first."""
        ...
