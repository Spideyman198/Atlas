"""The only package permitted to import ``llama_index``.

ADR-0003 in one sentence: LlamaIndex is an implementation of ports the domain
owns, not a foundation the system is built on. Everything above this package
speaks in Atlas types and would keep working if the dependency were deleted —
only the tests in here would fail.

An ``import-linter`` contract enforces that, so it is a property of the build
rather than a request in a document.

What LlamaIndex is used for here is **algorithms**: node parsing and file
readers. Never transport, never storage. The vendor path and the schema stay
ours, which is the whole point of the inversion described in ADR-0003 §3.
"""

from atlas.infrastructure.llamaindex.bridges import (
    AtlasLlamaEmbedding,
    AtlasLlamaLLM,
    AtlasLlamaVectorStore,
)
from atlas.infrastructure.llamaindex.loaders import LlamaIndexDocumentLoader
from atlas.infrastructure.llamaindex.retriever import LlamaIndexHybridRetriever

__all__ = [
    "AtlasLlamaEmbedding",
    "AtlasLlamaLLM",
    "AtlasLlamaVectorStore",
    "LlamaIndexDocumentLoader",
    "LlamaIndexHybridRetriever",
]
