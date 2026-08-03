"""Measuring retrieval, and being honest about what each number means.

Three metrics, because each one hides something the others show.

**Recall@k** — of the documents that should have been found, how many were. It
says nothing about order: a relevant document ranked eighth counts the same as
one ranked first. It is the metric that matters most here, because a chunk that
never enters the candidate set cannot be authorized, cannot be assembled, and
cannot be cited. Everything downstream is bounded by it.

**MRR** — how far down the first relevant result was. Sensitive to rank and
blind to everything after the first hit, which makes it the right metric for
"where is order S00042" and the wrong one for "which customers haven't ordered".

**nDCG** — graded, position-weighted, and normalised so a question with two
relevant documents is comparable to one with nine. The number nobody can read
off intuitively, and the only one that changes when a good result moves from
third to second.

All three are pure functions over ranked identifiers. Nothing here knows what a
chunk is, and nothing here does IO — the harness that builds a corpus and calls
a retriever lives in the application layer, so these can be tested against
hand-written rankings where the right answer is arithmetic rather than opinion.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Fraction of the relevant documents that appear in the top ``k``.

    Returns 0.0 when nothing is relevant. A question with no right answer cannot
    be scored, and returning 1.0 for it — "we found all zero of them" — would
    quietly inflate every average it appears in.
    """
    wanted = set(relevant)
    if not wanted:
        return 0.0
    found = wanted.intersection(retrieved[:k])
    return len(found) / len(wanted)


def reciprocal_rank(retrieved: Sequence[str], relevant: Sequence[str]) -> float:
    """One over the position of the first relevant result, or 0.0 if absent.

    Positions are one-based: a hit at the top scores 1.0, second 0.5, tenth 0.1.
    """
    wanted = set(relevant)
    for position, identifier in enumerate(retrieved, start=1):
        if identifier in wanted:
            return 1.0 / position
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Normalised discounted cumulative gain over the top ``k``.

    Binary relevance: a document is either labelled or it is not. Graded labels
    would be more informative and would need somebody to agree on what a 2 means
    versus a 3, which is a judgement the golden set does not currently carry.
    """
    wanted = set(relevant)
    if not wanted:
        return 0.0

    gain = sum(
        1.0 / math.log2(position + 1)
        for position, identifier in enumerate(retrieved[:k], start=1)
        if identifier in wanted
    )
    # The best achievable ranking: every relevant document, in a row, at the top.
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(wanted), k) + 1))
    return gain / ideal if ideal else 0.0


@dataclass(frozen=True, slots=True)
class GoldenQuestion:
    """One question, and the documents an answer to it should rest on.

    Attributes:
        question: Asked verbatim, as a user would type it.
        relevant: Document keys that ought to be retrieved. Deliberately keys
            rather than chunk ids — chunking is an implementation detail that
            changes, and a golden set that has to be relabelled every time the
            chunk size moves is one nobody maintains.
        note: Why these documents and not others. Read by whoever has to decide
            whether a regression is a bug or a better answer.
    """

    id: str
    question: str
    relevant: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True, slots=True)
class QuestionResult:
    """What retrieval did with one golden question."""

    question: GoldenQuestion
    retrieved: tuple[str, ...]
    recall: float
    reciprocal_rank: float
    ndcg: float
    k: int = 0

    @property
    def missed(self) -> tuple[str, ...]:
        """Relevant documents that did not make the top ``k``.

        Cut off at ``k``, the same place recall is. Reporting against the whole
        ranked list instead showed "nothing missed" next to a recall of 0.00 —
        true, and useless, because the document was at position six and the
        prompt only ever sees four.

        The most useful line in a failing run: an average says something got
        worse, this says which question to go and look at.
        """
        top = self.retrieved[: self.k] if self.k else self.retrieved
        return tuple(key for key in self.question.relevant if key not in top)

    @property
    def rank_of_first_hit(self) -> int | None:
        """Where the first relevant document landed, or ``None`` if nowhere.

        Distinguishes "ranked just outside the cut-off" from "not found at all",
        which are the same number in every metric here and very different
        problems.
        """
        wanted = set(self.question.relevant)
        for position, key in enumerate(self.retrieved, start=1):
            if key in wanted:
                return position
        return None


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    """Aggregate scores across a golden set."""

    k: int
    results: tuple[QuestionResult, ...] = ()

    @property
    def questions(self) -> int:
        """How many golden questions this run covered."""
        return len(self.results)

    @property
    def recall(self) -> float:
        """Mean recall@k across the set."""
        return _mean(result.recall for result in self.results)

    @property
    def mrr(self) -> float:
        """Mean reciprocal rank across the set."""
        return _mean(result.reciprocal_rank for result in self.results)

    @property
    def ndcg(self) -> float:
        """Mean nDCG@k across the set."""
        return _mean(result.ndcg for result in self.results)

    @property
    def perfect(self) -> int:
        """Questions where every relevant document was retrieved."""
        return sum(1 for result in self.results if result.recall == 1.0)

    @property
    def worst(self) -> tuple[QuestionResult, ...]:
        """Results ordered worst first, for the part of a report anyone reads."""
        return tuple(sorted(self.results, key=lambda result: (result.recall, result.ndcg)))

    def as_dict(self) -> dict[str, float | int]:
        """The numbers a threshold file is compared against."""
        return {
            "questions": self.questions,
            "recall_at_k": round(self.recall, 4),
            "mrr": round(self.mrr, 4),
            "ndcg_at_k": round(self.ndcg, 4),
            "perfect": self.perfect,
        }


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The floor a run has to clear.

    Absolute rather than "no worse than last time". A relative gate ratchets
    downward one acceptable-looking commit at a time, and nobody notices until
    the number is half what it was.
    """

    recall_at_k: float = 0.0
    mrr: float = 0.0
    ndcg_at_k: float = 0.0

    def failures(self, report: RetrievalReport) -> tuple[str, ...]:
        """Which floors this report failed to clear, as sentences.

        Returns an empty tuple when everything passed, so the caller's check is
        ``if failures:`` rather than a comparison it might get backwards.
        """
        checks = (
            ("recall@k", report.recall, self.recall_at_k),
            ("MRR", report.mrr, self.mrr),
            ("nDCG@k", report.ndcg, self.ndcg_at_k),
        )
        return tuple(
            f"{name} is {actual:.3f}, below the floor of {floor:.3f}"
            for name, actual, floor in checks
            if actual < floor
        )


@dataclass(frozen=True, slots=True)
class AnswerCheck:
    """What an answer did with the context it was given.

    Attributes:
        cited: Whether the answer carries at least one citation.
        markers_resolved: Whether every marker in the text names a real block.
        unsupported_figures: Numbers in the answer that appear nowhere in the
            context. A proxy for faithfulness, not a measure of it — see
            ``application/evaluation.py``.
    """

    question: str
    grounded: bool
    cited: bool
    markers_resolved: bool
    unsupported_figures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """Whether this answer is defensible on the evidence it shipped with.

        An ungrounded answer passes trivially: there was no context, so there is
        nothing to be unfaithful to. Whether it should have refused is the
        orchestrator's business and is tested there.
        """
        if not self.grounded:
            return True
        return self.cited and self.markers_resolved and not self.unsupported_figures


def _mean(values: Iterable[float]) -> float:
    collected = list(values)
    return sum(collected) / len(collected) if collected else 0.0
