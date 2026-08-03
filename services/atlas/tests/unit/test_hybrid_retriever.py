"""Tests for hybrid retrieval.

Driven against the in-memory store, so the whole stack — LlamaIndex's fusion,
our bridge, our SQL-shaped search modes — runs with no database and no network.
That is ADR-0003's exit test as a property of the suite rather than a claim: if
LlamaIndex vanished, these would fail and nothing above them would.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from atlas.domain.corpus import CandidateChunk, ChunkInput, Document, Visibility
from atlas.domain.retrieval import RetrievalRequest
from atlas.infrastructure.llamaindex.retriever import LlamaIndexHybridRetriever
from atlas.infrastructure.persistence.fakes import InMemoryVectorStore
from atlas.infrastructure.providers.fakes import HashEmbeddingProvider

pytestmark = pytest.mark.unit

DIMENSIONS = 64


class Corpus:
    """A small indexed corpus, embedded the way ingestion would embed it."""

    def __init__(self) -> None:
        self.store = InMemoryVectorStore()
        self.embedder = HashEmbeddingProvider(dimensions=DIMENSIONS)
        self._next = 1

    async def add(
        self,
        text: str,
        *,
        res_model: str = "sale.order",
        company_id: int = 1,
        visibility: Visibility = Visibility.INTERNAL,
        title: str | None = None,
    ) -> int:
        res_id = self._next
        self._next += 1
        embedded = await self.embedder.embed([text])
        document = Document(
            source_key=f"odoo.{res_model}",
            source_hash=f"hash-{res_id}",
            title=title or text[:20],
            embedding_model=self.embedder.model_id,
            embedding_dimensions=DIMENSIONS,
            res_model=res_model,
            res_id=res_id,
            company_id=company_id,
            visibility=visibility,
        )
        await self.store.upsert_document(
            document,
            [
                ChunkInput(
                    ordinal=0,
                    content=text,
                    embedding=embedded.vectors[0],
                    metadata={"title": document.title},
                )
            ],
        )
        return res_id

    def retriever(self, **kwargs: object) -> LlamaIndexHybridRetriever:
        return LlamaIndexHybridRetriever(
            store=self.store,
            embedder=self.embedder,
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.fixture
async def corpus() -> Corpus:
    return Corpus()


async def test_retrieval_returns_candidates_not_authorized_chunks(corpus: Corpus) -> None:
    """The port's return type is the reason authorization cannot be forgotten."""
    await corpus.add("Sales Order: S00035 Customer: Deco Addict")

    results = await corpus.retriever().retrieve(RetrievalRequest(query="Deco Addict"))

    assert results
    assert all(type(result).__name__ == "CandidateChunk" for result in results)


async def test_a_lexical_only_hit_is_ranked_first_by_fusion(corpus: Corpus) -> None:
    """The reason the lexical half exists.

    An order number is a token no semantic model has a useful opinion about, so
    dense search ranks it no better than chance. Full-text search finds it
    exactly. What this asserts is the mechanism that makes that matter: a record
    only the lexical side found comes out on top, because a reciprocal-rank hit
    in *both* lists beats a hit in one.

    Note what is deliberately *not* asserted. "Dense search misses it" is not a
    fact about this test — the offline embedder hashes text, so its ordering is
    arbitrary and asserting anything about it would be asserting a coin flip.
    Whether a real embedding model is weak on identifiers, and by how much, is
    M12's golden set to measure.
    """
    wanted = await corpus.add("Sales Order: S00035 Customer: Deco Addict Total: 4500")
    for index in range(6):
        await corpus.add(f"Sales Order: S0004{index} Customer: Other Buyer Total: {index}00")

    lexical_only = await corpus.store.search_lexical("S00035", limit=5)
    hybrid = await corpus.retriever().retrieve(RetrievalRequest(query="S00035", limit=3))

    assert [candidate.res_id for candidate in lexical_only] == [wanted]
    assert hybrid[0].res_id == wanted


async def test_a_semantic_question_still_works_without_matching_words(
    corpus: Corpus,
) -> None:
    """The dense half is not decoration: lexical search alone finds nothing here."""
    await corpus.add("Contact: Deco Addict Email: deco@example.com City: Brussels")

    lexical_only = await corpus.store.search_lexical("who is our customer", limit=5)
    hybrid = await corpus.retriever().retrieve(RetrievalRequest(query="who is our customer"))

    assert lexical_only == []
    assert hybrid


async def test_the_pre_filter_narrows_by_company(corpus: Corpus) -> None:
    ours = await corpus.add("Sales Order: S00001 Customer: Ours", company_id=1)
    await corpus.add("Sales Order: S00002 Customer: Theirs", company_id=2)

    results = await corpus.retriever().retrieve(
        RetrievalRequest(query="Sales Order", company_ids=(1,))
    )

    assert [candidate.res_id for candidate in results] == [ours]


async def test_the_pre_filter_respects_the_visibility_ceiling(corpus: Corpus) -> None:
    await corpus.add("Invoice: INV/001 Total: 9000", visibility=Visibility.RESTRICTED)

    results = await corpus.retriever().retrieve(
        RetrievalRequest(query="Invoice", max_visibility=Visibility.INTERNAL)
    )

    assert results == []


async def test_the_pre_filter_narrows_by_model(corpus: Corpus) -> None:
    order = await corpus.add("Deco Addict order", res_model="sale.order")
    await corpus.add("Deco Addict contact", res_model="res.partner")

    results = await corpus.retriever().retrieve(
        RetrievalRequest(query="Deco Addict", res_models=("sale.order",))
    )

    assert [candidate.res_id for candidate in results] == [order]


async def test_retrieval_over_fetches_for_the_authorization_step(corpus: Corpus) -> None:
    """Authorization discards an unknown fraction, so retrieval brings extras."""
    for index in range(20):
        await corpus.add(f"Sales Order: S000{index:02d} Customer: Buyer {index}")

    results = await corpus.retriever().retrieve(
        RetrievalRequest(query="Sales Order", limit=3, over_fetch=4)
    )

    assert len(results) > 3
    assert len(results) <= 12


async def test_every_candidate_carries_what_authorization_needs(corpus: Corpus) -> None:
    """A candidate with no record behind it cannot be authorized or cited."""
    await corpus.add("Sales Order: S00035 Customer: Deco Addict", title="S00035")

    results = await corpus.retriever().retrieve(RetrievalRequest(query="Deco Addict"))

    candidate = results[0]
    assert candidate.res_model == "sale.order"
    assert candidate.res_id
    assert candidate.chunk_id
    assert candidate.company_id == 1
    assert candidate.content


async def test_chunks_keep_distinct_identities_through_fusion(corpus: Corpus) -> None:
    """Fusion identifies nodes by id, so colliding ids silently lose results.

    Caught by running it: the in-memory store handed every chunk the id 0, and a
    result set of three collapsed into one on the way through LlamaIndex.
    """
    for index in range(5):
        await corpus.add(f"Sales Order: S000{index} Customer: Buyer {index}")

    results = await corpus.retriever().retrieve(RetrievalRequest(query="Sales Order"))

    ids = [candidate.chunk_id for candidate in results]
    assert len(ids) == 5
    assert len(set(ids)) == 5


async def test_an_empty_corpus_returns_nothing_rather_than_failing(corpus: Corpus) -> None:
    assert await corpus.retriever().retrieve(RetrievalRequest(query="anything")) == []


async def test_diversity_can_be_switched_off(corpus: Corpus) -> None:
    """`mmr_lambda=1.0` is pure relevance order, near-duplicates included."""
    for index in range(4):
        await corpus.add(f"Sales Order: S000{index} Customer: Deco Addict Total: 100")

    diverse = await corpus.retriever(mmr_lambda=0.5).retrieve(RetrievalRequest(query="Deco Addict"))
    plain = await corpus.retriever(mmr_lambda=1.0).retrieve(RetrievalRequest(query="Deco Addict"))

    assert {candidate.res_id for candidate in diverse} == {c.res_id for c in plain}
    assert [candidate.score for candidate in plain] == sorted(
        (candidate.score for candidate in plain), reverse=True
    )


async def test_a_reranker_gets_the_last_word(corpus: Corpus) -> None:
    first = await corpus.add("Sales Order: S00001 Customer: Deco Addict")
    await corpus.add("Sales Order: S00002 Customer: Deco Addict")

    class Reversing:
        name = "reversing"

        async def rerank(
            self,
            query: str,
            candidates: Sequence[CandidateChunk],
            *,
            limit: int,
        ) -> list[CandidateChunk]:
            return list(reversed(candidates))[:limit]

    results = await corpus.retriever(reranker=Reversing()).retrieve(
        RetrievalRequest(query="Deco Addict")
    )

    assert results[-1].res_id == first
