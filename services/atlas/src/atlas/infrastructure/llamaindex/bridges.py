"""Bridges that let LlamaIndex use our infrastructure rather than its own.

This is the inversion described in :doc:`ADR-0003 </adr/0003-rag-framework-selection>`
§3, and it is the single largest cost of adopting the framework. LlamaIndex
components want a vector store and an embedding model; we hand them adapters
that delegate straight back to our own ports.

Two consequences, and they are the reason the tax is worth paying:

**One path to every vendor.** Retry, backoff, token accounting and cost
telemetry live in one decorator stack. A second path through LlamaIndex's own
embedding integrations would mean two retry policies and two cost meters that
disagree.

**One schema.** The HNSW index, the pre-filter columns and the generated
``content_tsv`` from :doc:`ADR-0004 </adr/0004-vector-store-and-index-strategy>`
survive intact, because LlamaIndex never sees the database.

Both bridges are **read-only**. LlamaIndex is used for algorithms — fusion,
ranking, parsing — and never for transport or storage. Writing is refused
loudly rather than quietly doing the wrong thing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.base.llms.types import (
    CompletionResponse,
    CompletionResponseGen,
    LLMMetadata,
)
from llama_index.core.llms import CustomLLM
from llama_index.core.schema import BaseNode, TextNode
from llama_index.core.vector_stores.types import (
    BasePydanticVectorStore,
    VectorStoreQuery,
    VectorStoreQueryMode,
    VectorStoreQueryResult,
)
from pydantic import PrivateAttr

from atlas.domain.chat import ChatRequest, Message, Role
from atlas.domain.corpus import CandidateChunk, SearchFilter, Visibility
from atlas.domain.embedding import EmbeddingPurpose, Vector
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.domain.ports.vector_store import VectorStore

#: Metadata keys carried on every node, so a candidate survives the round-trip
#: through LlamaIndex's types and comes back with everything the authorization
#: filter and the citation builder need.
CHUNK_ID = "atlas_chunk_id"
DOCUMENT_ID = "atlas_document_id"
RES_MODEL = "atlas_res_model"
RES_ID = "atlas_res_id"
COMPANY_ID = "atlas_company_id"
VISIBILITY = "atlas_visibility"
EXTERNAL_REF = "atlas_external_ref"

#: The chunk's own metadata, carried whole under one key so it cannot collide
#: with the plumbing keys above. Without this it is dropped on the way through
#: LlamaIndex, and `record_name` — what a citation is labelled with — is lost.
CHUNK_METADATA = "atlas_metadata"

_WRITES_REFUSED = (
    "the Atlas vector store is read-only from LlamaIndex: ingestion writes "
    "through PgVectorStore so that one schema and one migration history exist "
    "(ADR-0003, ADR-0004)"
)


class AtlasLlamaVectorStore(BasePydanticVectorStore):
    """A LlamaIndex vector store backed by our own.

    Constructed per request, because it carries the query's pre-filter. That is
    cheaper than it sounds — it holds two references — and it keeps the filter
    out of the ``kwargs`` smuggling that the alternative would need.

    Only the query side is implemented. ``add`` and ``delete`` raise: ingestion
    owns writes, and a framework quietly writing to our schema is precisely what
    ADR-0003 rules out.
    """

    stores_text: bool = True
    is_embedding_query: bool = True

    _store: VectorStore = PrivateAttr()
    _filters: SearchFilter | None = PrivateAttr(default=None)

    def __init__(self, store: VectorStore, *, filters: SearchFilter | None = None) -> None:
        # Passed explicitly rather than left to the class default: LlamaIndex
        # declares it a required field on the base model, so a bare super()
        # call does not type-check.
        super().__init__(stores_text=True, is_embedding_query=True)
        self._store = store
        self._filters = filters

    @property
    def client(self) -> Any:
        """The underlying store, as LlamaIndex names this accessor."""
        return self._store

    def add(self, nodes: Sequence[BaseNode], **kwargs: Any) -> list[str]:
        raise NotImplementedError(_WRITES_REFUSED)

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        raise NotImplementedError(_WRITES_REFUSED)

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        """Refused: the store beneath is async.

        Running an event loop from a synchronous call to satisfy an interface
        would be a deadlock waiting for the first caller who already has one.
        Every Atlas retrieval path is async, so this is unreachable rather than
        merely discouraged.
        """
        message = "AtlasLlamaVectorStore is async-only; use aquery"
        raise NotImplementedError(message)

    async def aquery(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        """Search, choosing the mode LlamaIndex asked for.

        ``TEXT_SEARCH`` and ``SPARSE`` both mean "the lexical side" here: our
        lexical index is PostgreSQL full-text search, which is neither a sparse
        vector nor a separate service, and mapping both onto it is less
        surprising than refusing one of them.
        """
        limit = query.similarity_top_k or 10
        if query.mode in (VectorStoreQueryMode.TEXT_SEARCH, VectorStoreQueryMode.SPARSE):
            candidates = await self._store.search_lexical(
                query.query_str or "", limit=limit, filters=self._filters
            )
        else:
            if query.query_embedding is None:
                message = "a dense query needs an embedding"
                raise ValueError(message)
            candidates = await self._store.search_dense(
                tuple(query.query_embedding), limit=limit, filters=self._filters
            )

        nodes = [to_node(candidate) for candidate in candidates]
        return VectorStoreQueryResult(
            nodes=nodes,
            similarities=[candidate.score for candidate in candidates],
            ids=[node.node_id for node in nodes],
        )


class AtlasLlamaEmbedding(BaseEmbedding):
    """A LlamaIndex embedding model that delegates to our provider.

    So that a retriever asking LlamaIndex to embed a query gets the same model,
    the same retry policy and the same cost accounting as everything else
    (ADR-0005). Without this there would be two ways to spend money on
    embeddings and only one of them would appear in the telemetry.
    """

    _provider: EmbeddingProvider = PrivateAttr()

    def __init__(self, provider: EmbeddingProvider) -> None:
        super().__init__(model_name=provider.model_id, embed_batch_size=provider.max_batch_size)
        self._provider = provider

    @classmethod
    def class_name(cls) -> str:
        return "AtlasLlamaEmbedding"

    async def _aget_query_embedding(self, query: str) -> list[float]:
        result = await self._provider.embed([query], EmbeddingPurpose.QUERY)
        return list(result.vectors[0])

    async def _aget_text_embedding(self, text: str) -> list[float]:
        result = await self._provider.embed([text], EmbeddingPurpose.DOCUMENT)
        return list(result.vectors[0])

    def _get_query_embedding(self, query: str) -> list[float]:
        raise NotImplementedError(_SYNC_REFUSED)

    def _get_text_embedding(self, text: str) -> list[float]:
        raise NotImplementedError(_SYNC_REFUSED)


_SYNC_REFUSED = (
    "AtlasLlamaEmbedding is async-only: the provider stack underneath it is "
    "async, and spinning an event loop here would deadlock any caller that "
    "already has one"
)


class AtlasLlamaLLM(CustomLLM):
    """A LlamaIndex language model that delegates to our chat provider.

    This bridge is not optional decoration. Constructing a
    ``QueryFusionRetriever`` without one makes LlamaIndex resolve
    ``Settings.llm``, which tries to import ``llama-index-llms-openai`` and
    fails — and would, if that package were ever installed, quietly open a
    *second* path to a vendor with its own retry policy and its own cost meter.
    That is the exact failure ADR-0003 §3 inverts the dependency to prevent, and
    it turns out to bite on the first component that touches it.

    ``provider`` may be ``None``. Retrieval genuinely needs no language model —
    fusion with a single query never generates anything — and a bridge that
    refuses is a better guarantee of that than a comment saying so.
    """

    _provider: ChatProvider | None = PrivateAttr(default=None)

    def __init__(self, provider: ChatProvider | None = None) -> None:
        super().__init__()
        self._provider = provider

    @classmethod
    def class_name(cls) -> str:
        return "AtlasLlamaLLM"

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            model_name=self._provider.model if self._provider else "atlas-no-llm",
            is_chat_model=True,
        )

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        raise NotImplementedError(_SYNC_REFUSED)

    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseGen:
        raise NotImplementedError(_SYNC_REFUSED)

    async def acomplete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        """Complete through our provider, so one decorator stack sees the call."""
        if self._provider is None:
            message = (
                "no chat provider is wired into this LlamaIndex bridge; nothing in "
                "retrieval should be asking a language model for anything"
            )
            raise NotImplementedError(message)
        response = await self._provider.complete(
            ChatRequest(messages=(Message(role=Role.USER, content=prompt),))
        )
        return CompletionResponse(text=response.content)


def to_node(candidate: CandidateChunk) -> TextNode:
    """Translate a candidate into LlamaIndex's type.

    The chunk id is both the node id and a metadata key. The id alone is not
    enough: fusion deduplicates on the node's *content hash*, and two chunks
    with identical text — the same boilerplate paragraph on two orders — would
    otherwise collapse into one and lose a citation.
    """
    return TextNode(
        id_=str(candidate.chunk_id),
        text=candidate.content,
        metadata={
            CHUNK_ID: candidate.chunk_id,
            DOCUMENT_ID: candidate.document_id,
            RES_MODEL: candidate.res_model,
            RES_ID: candidate.res_id,
            COMPANY_ID: candidate.company_id,
            VISIBILITY: int(candidate.visibility),
            EXTERNAL_REF: candidate.external_ref,
            CHUNK_METADATA: dict(candidate.metadata or {}),
        },
        # Metadata is plumbing, not content. Leaving it in would put
        # `atlas_chunk_id: 41` into the text a model reads and an embedding
        # covers.
        excluded_embed_metadata_keys=_METADATA_KEYS,
        excluded_llm_metadata_keys=_METADATA_KEYS,
    )


def to_candidate(node: BaseNode, score: float) -> CandidateChunk:
    """Translate back, restoring everything authorization and citations need."""
    metadata = dict(node.metadata or {})
    return CandidateChunk(
        chunk_id=int(metadata.get(CHUNK_ID) or 0),
        document_id=int(metadata.get(DOCUMENT_ID) or 0),
        content=node.get_content(),
        score=score,
        res_model=metadata.get(RES_MODEL),
        res_id=metadata.get(RES_ID),
        company_id=metadata.get(COMPANY_ID),
        visibility=Visibility(int(metadata.get(VISIBILITY, Visibility.INTERNAL))),
        external_ref=metadata.get(EXTERNAL_REF),
        metadata=metadata.get(CHUNK_METADATA) or {},
    )


def as_vector(values: Sequence[float]) -> Vector:
    """Normalise a LlamaIndex embedding into the domain's tuple form."""
    return tuple(float(value) for value in values)


_METADATA_KEYS = [
    CHUNK_METADATA,
    CHUNK_ID,
    DOCUMENT_ID,
    RES_MODEL,
    RES_ID,
    COMPANY_ID,
    VISIBILITY,
    EXTERNAL_REF,
]
