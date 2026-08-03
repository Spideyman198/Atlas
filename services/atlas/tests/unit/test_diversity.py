"""Tests for the diversity pass.

The failure MMR exists to fix is a top-of-list that says one thing eight times.
An ERP corpus produces that reliably: shared order headers, product
descriptions copied across variants, the same delivery paragraph on fifty
quotations.
"""

from __future__ import annotations

import pytest

from atlas.domain.corpus import CandidateChunk
from atlas.infrastructure.retrieval.diversity import maximal_marginal_relevance

pytestmark = pytest.mark.unit


def chunk(chunk_id: int, content: str, score: float) -> CandidateChunk:
    return CandidateChunk(chunk_id=chunk_id, document_id=chunk_id, content=content, score=score)


def test_nothing_in_means_nothing_out() -> None:
    assert maximal_marginal_relevance([], limit=5) == []


def test_a_zero_limit_returns_nothing() -> None:
    assert maximal_marginal_relevance([chunk(1, "a", 1.0)], limit=0) == []


def test_the_most_relevant_candidate_is_always_first() -> None:
    """The first pick has nothing to be redundant with, so relevance decides."""
    candidates = [
        chunk(1, "Sales order for Deco Addict", 0.4),
        chunk(2, "Contact record for Gemini", 0.9),
    ]

    result = maximal_marginal_relevance(candidates, limit=2, lambda_=0.5)

    assert result[0].chunk_id == 2


def test_a_near_duplicate_is_pushed_below_something_different() -> None:
    """The behaviour the whole function exists for."""
    candidates = [
        chunk(1, "Sales Order S00001 Customer Deco Addict Total 4500 EUR", 1.0),
        chunk(2, "Sales Order S00002 Customer Deco Addict Total 4500 EUR", 0.9),
        chunk(3, "Warehouse stock levels for oak desks in Brussels", 0.8),
    ]

    result = maximal_marginal_relevance(candidates, limit=3, lambda_=0.5)

    assert [candidate.chunk_id for candidate in result] == [1, 3, 2]


def test_lambda_one_is_pure_relevance_order() -> None:
    candidates = [
        chunk(1, "Sales Order S00001 Customer Deco Addict", 1.0),
        chunk(2, "Sales Order S00002 Customer Deco Addict", 0.9),
        chunk(3, "Something entirely different", 0.8),
    ]

    result = maximal_marginal_relevance(candidates, limit=3, lambda_=1.0)

    assert [candidate.chunk_id for candidate in result] == [1, 2, 3]


def test_nothing_is_invented_or_duplicated() -> None:
    candidates = [chunk(index, f"content {index}", 1.0 / index) for index in range(1, 6)]

    result = maximal_marginal_relevance(candidates, limit=5, lambda_=0.5)

    assert len(result) == 5
    assert len({candidate.chunk_id for candidate in result}) == 5


def test_the_limit_is_respected() -> None:
    candidates = [chunk(index, f"content {index}", 1.0 / index) for index in range(1, 10)]

    assert len(maximal_marginal_relevance(candidates, limit=3, lambda_=0.5)) == 3


def test_scores_are_left_alone() -> None:
    """Reordering is not rescoring: a caller can still see the original ranking."""
    candidates = [chunk(1, "alpha beta", 0.75), chunk(2, "gamma delta", 0.25)]

    result = maximal_marginal_relevance(candidates, limit=2, lambda_=0.5)

    assert {candidate.chunk_id: candidate.score for candidate in result} == {1: 0.75, 2: 0.25}


def test_identical_scores_do_not_break_the_normalisation() -> None:
    """A flat score list has no range to scale into, and must not divide by zero."""
    candidates = [chunk(index, f"content {index}", 0.5) for index in range(1, 4)]

    result = maximal_marginal_relevance(candidates, limit=3, lambda_=0.5)

    assert len(result) == 3


def test_stop_words_do_not_make_two_chunks_look_alike() -> None:
    """Otherwise every pair of English sentences is 'similar'."""
    candidates = [
        chunk(1, "the invoice is in the post and it is on the way", 1.0),
        chunk(2, "the delivery is at the depot and it is on the truck", 0.9),
        chunk(3, "invoice INV0001 for Deco Addict is overdue", 0.8),
    ]

    result = maximal_marginal_relevance(candidates, limit=2, lambda_=0.5)

    # 2 shares only stop words with 1, so it is not treated as a duplicate.
    assert [candidate.chunk_id for candidate in result] == [1, 2]


def test_an_empty_chunk_is_not_similar_to_everything() -> None:
    candidates = [chunk(1, "real content here", 1.0), chunk(2, "", 0.9)]

    result = maximal_marginal_relevance(candidates, limit=2, lambda_=0.5)

    assert len(result) == 2
