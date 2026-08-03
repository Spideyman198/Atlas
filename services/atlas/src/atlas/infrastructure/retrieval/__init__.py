"""Retrieval strategy that needs no framework.

Diversity and reranking are plain functions over domain types. They live outside
``infrastructure.llamaindex`` because nothing in them needs it, and an algorithm
that could be read without knowing LlamaIndex should not require knowing it.
"""

from atlas.infrastructure.retrieval.diversity import maximal_marginal_relevance
from atlas.infrastructure.retrieval.rerank import NoOpReranker

__all__ = ["NoOpReranker", "maximal_marginal_relevance"]
