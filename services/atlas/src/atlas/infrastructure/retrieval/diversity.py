"""Maximal marginal relevance: stop the top of the list saying one thing.

Hybrid search over an ERP corpus reliably returns near-duplicates. One order
produces several chunks that share a header; a product description is copied
across every variant; the same delivery paragraph appears on fifty quotations.
Rank by relevance alone and the top eight results are frequently the same fact
eight times, which wastes a context window that could have held eight different
facts.

MMR picks greedily, trading relevance against how much a candidate repeats what
has already been picked:

    score = lambda * relevance - (1 - lambda) * max similarity to anything chosen

**Similarity here is over words, not vectors.** The textbook formulation uses
the embedding space, which would mean carrying a 1536-float vector back from the
database for every candidate — real bandwidth on the hot path — and would still
say nothing about the lexical half of a hybrid result set, which has no vector
at all. Token overlap costs nothing, needs no extra round-trip, and is a good
detector of exactly the failure being fixed: two chunks that are largely the
same words. Whether embedding-space MMR earns its bandwidth is an M13 question,
and this function's signature does not change if the answer is yes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from atlas.domain.corpus import CandidateChunk

#: Weight on relevance. At 1.0 this is a no-op ranking; at 0.0 it picks the most
#: unlike-everything-else chunk regardless of the query. 0.7 keeps relevance
#: firmly in charge while breaking up runs of near-identical results.
DEFAULT_LAMBDA = 0.7

#: Words that appear in nearly every ERP chunk and so carry no signal about
#: whether two chunks are saying the same thing.
_NOISE = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)

_WORD = re.compile(r"[\w'-]+")


def maximal_marginal_relevance(
    candidates: Sequence[CandidateChunk],
    *,
    limit: int,
    lambda_: float = DEFAULT_LAMBDA,
) -> list[CandidateChunk]:
    """Re-order candidates to trade relevance against redundancy.

    Args:
        candidates: Ranked best-first. Their ``score`` is the relevance term.
        limit: How many to return.
        lambda_: Weight on relevance, in ``[0, 1]``.

    Returns:
        At most ``limit`` candidates. Scores are left untouched — this reorders
        and trims, and a caller that wants to know the original ranking still
        can.
    """
    if not candidates or limit < 1:
        return []
    if lambda_ >= 1.0:
        return list(candidates[:limit])

    relevance = _normalised_scores(candidates)
    words = [_words(candidate.content) for candidate in candidates]

    remaining = set(range(len(candidates)))
    chosen: list[int] = []

    while remaining and len(chosen) < limit:
        best_index, best_score = None, float("-inf")
        for index in remaining:
            redundancy = max(
                (_similarity(words[index], words[picked]) for picked in chosen),
                default=0.0,
            )
            score = lambda_ * relevance[index] - (1.0 - lambda_) * redundancy
            if score > best_score:
                best_index, best_score = index, score
        # `best_index` is set on the first iteration because `remaining` is
        # non-empty and any real score beats negative infinity.
        assert best_index is not None  # noqa: S101
        chosen.append(best_index)
        remaining.discard(best_index)

    return [candidates[index] for index in chosen]


def _normalised_scores(candidates: Sequence[CandidateChunk]) -> list[float]:
    """Scale relevance into ``[0, 1]`` so it is comparable with an overlap ratio.

    Fused scores are reciprocal-rank sums with no natural range, and subtracting
    a ratio from an unbounded number would make ``lambda_`` meaningless.
    """
    scores = [candidate.score for candidate in candidates]
    lowest, highest = min(scores), max(scores)
    if highest <= lowest:
        return [1.0] * len(scores)
    span = highest - lowest
    return [(score - lowest) / span for score in scores]


def _words(text: str) -> frozenset[str]:
    return frozenset(word for token in _WORD.findall(text.lower()) if (word := token) not in _NOISE)


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard overlap: how much of the two chunks' vocabulary is shared."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
