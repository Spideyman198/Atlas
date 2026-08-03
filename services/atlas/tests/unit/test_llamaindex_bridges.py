"""Tests for the LlamaIndex bridges.

These defend the inversion in ADR-0003 §3: LlamaIndex uses our infrastructure,
never its own. Most of what is asserted is what the bridges *refuse* — writing,
and reaching for a vendor — because those are the ways the containment quietly
stops being true.
"""

from __future__ import annotations

import pytest
from llama_index.core.schema import MetadataMode, TextNode
from llama_index.core.vector_stores.types import VectorStoreQuery, VectorStoreQueryMode

from atlas.domain.corpus import CandidateChunk, ChunkInput, Document, SearchFilter, Visibility
from atlas.infrastructure.llamaindex.bridges import (
    AtlasLlamaEmbedding,
    AtlasLlamaLLM,
    AtlasLlamaVectorStore,
    to_candidate,
    to_node,
)
from atlas.infrastructure.persistence.fakes import InMemoryVectorStore
from atlas.infrastructure.providers.fakes import FakeChatProvider, HashEmbeddingProvider

pytestmark = pytest.mark.unit


@pytest.fixture
async def store() -> InMemoryVectorStore:
    memory = InMemoryVectorStore()
    embedder = HashEmbeddingProvider(dimensions=8)
    embedded = await embedder.embed(["Sales Order S00035 for Deco Addict"])
    await memory.upsert_document(
        Document(
            source_key="odoo.sale.order",
            source_hash="hash-1",
            title="S00035",
            embedding_model=embedder.model_id,
            embedding_dimensions=8,
            res_model="sale.order",
            res_id=35,
            company_id=1,
        ),
        [
            ChunkInput(
                ordinal=0,
                content="Sales Order S00035 for Deco Addict",
                embedding=embedded.vectors[0],
            )
        ],
    )
    return memory


# --- the vector store bridge -----------------------------------------------


async def test_a_dense_query_reaches_our_store(store: InMemoryVectorStore) -> None:
    bridge = AtlasLlamaVectorStore(store)

    result = await bridge.aquery(VectorStoreQuery(query_embedding=[1.0] * 8, similarity_top_k=5))

    assert len(result.nodes or []) == 1
    assert result.similarities


async def test_a_text_query_reaches_the_lexical_side(store: InMemoryVectorStore) -> None:
    bridge = AtlasLlamaVectorStore(store)

    result = await bridge.aquery(
        VectorStoreQuery(
            query_str="S00035", similarity_top_k=5, mode=VectorStoreQueryMode.TEXT_SEARCH
        )
    )

    assert len(result.nodes or []) == 1


async def test_sparse_is_treated_as_lexical(store: InMemoryVectorStore) -> None:
    """Our lexical index is PostgreSQL full-text search, not a sparse vector.

    Mapping both onto it is less surprising than refusing one of them.
    """
    bridge = AtlasLlamaVectorStore(store)

    result = await bridge.aquery(
        VectorStoreQuery(query_str="S00035", similarity_top_k=5, mode=VectorStoreQueryMode.SPARSE)
    )

    assert len(result.nodes or []) == 1


async def test_the_pre_filter_is_carried_by_the_bridge(store: InMemoryVectorStore) -> None:
    bridge = AtlasLlamaVectorStore(store, filters=SearchFilter(company_ids=(2,)))

    result = await bridge.aquery(VectorStoreQuery(query_embedding=[1.0] * 8, similarity_top_k=5))

    assert not result.nodes


async def test_a_dense_query_without_an_embedding_is_refused(
    store: InMemoryVectorStore,
) -> None:
    bridge = AtlasLlamaVectorStore(store)

    with pytest.raises(ValueError, match="embedding"):
        await bridge.aquery(VectorStoreQuery(similarity_top_k=5))


def test_writing_through_the_bridge_is_refused(store: InMemoryVectorStore) -> None:
    """Ingestion owns writes.

    A framework writing to our schema is what ADR-0003 rules out, and refusing
    loudly beats discovering it in a migration.
    """
    bridge = AtlasLlamaVectorStore(store)

    with pytest.raises(NotImplementedError, match="read-only"):
        bridge.add([TextNode(text="nope")])
    with pytest.raises(NotImplementedError, match="read-only"):
        bridge.delete("doc-1")


def test_synchronous_querying_is_refused(store: InMemoryVectorStore) -> None:
    bridge = AtlasLlamaVectorStore(store)

    with pytest.raises(NotImplementedError, match="async-only"):
        bridge.query(VectorStoreQuery(query_embedding=[1.0] * 8))


# --- node translation -------------------------------------------------------


def test_a_candidate_survives_the_round_trip() -> None:
    original = CandidateChunk(
        chunk_id=41,
        document_id=7,
        content="Sales Order S00035",
        score=0.9,
        res_model="sale.order",
        res_id=35,
        company_id=1,
        visibility=Visibility.RESTRICTED,
        external_ref="S00035",
    )

    restored = to_candidate(to_node(original), 0.9)

    assert restored == original


def test_metadata_is_kept_out_of_what_is_embedded_and_read() -> None:
    """`atlas_chunk_id: 41` is plumbing, not content.

    LlamaIndex will happily prepend metadata to the text a model sees and an
    embedding covers unless told not to. Left on, every chunk would carry a line
    of our bookkeeping into the prompt and into the vector.
    """
    node = to_node(CandidateChunk(chunk_id=41, document_id=7, content="the text", score=0.5))

    assert node.get_content() == "the text"
    assert node.get_content(metadata_mode=MetadataMode.EMBED) == "the text"
    assert node.get_content(metadata_mode=MetadataMode.LLM) == "the text"
    # Still available to us, just not to the model.
    assert node.metadata["atlas_chunk_id"] == 41


def test_identical_text_on_different_chunks_stays_distinct() -> None:
    """Fusion deduplicates by node identity, so a collision loses a citation."""
    first = to_node(CandidateChunk(chunk_id=1, document_id=1, content="same words", score=1.0))
    second = to_node(CandidateChunk(chunk_id=2, document_id=2, content="same words", score=1.0))

    assert first.node_id != second.node_id
    assert first.hash != second.hash


# --- the embedding and language-model bridges ------------------------------


async def test_embedding_goes_through_our_provider() -> None:
    bridge = AtlasLlamaEmbedding(HashEmbeddingProvider(dimensions=16))

    vector = await bridge._aget_query_embedding("what is overdue")

    assert len(vector) == 16
    assert bridge.model_name == "hash-embedding-v1"


async def test_synchronous_embedding_is_refused() -> None:
    bridge = AtlasLlamaEmbedding(HashEmbeddingProvider(dimensions=8))

    with pytest.raises(NotImplementedError, match="async-only"):
        bridge._get_query_embedding("anything")


async def test_a_language_model_bridge_without_a_provider_refuses() -> None:
    """Retrieval asks no model anything, and the bridge makes that structural."""
    bridge = AtlasLlamaLLM()

    with pytest.raises(NotImplementedError, match="nothing in"):
        await bridge.acomplete("summarise this")


async def test_a_language_model_bridge_delegates_when_it_has_a_provider() -> None:
    """So there is one vendor path, one retry policy and one cost meter."""
    bridge = AtlasLlamaLLM(FakeChatProvider())

    response = await bridge.acomplete("hello")

    assert response.text
    assert bridge.metadata.model_name == "fake-model"
