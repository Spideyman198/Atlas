"""Running the golden set, and auditing what an answer did with its context.

Two evaluators, deliberately separate.

:class:`RetrievalEvaluator` asks the retriever for each golden question and
scores the ranking. It needs no model and no key, which is what makes it a gate:
a number that costs money to produce is a number CI will eventually stop
producing.

:class:`AnswerAuditor` looks at a finished answer next to the context it was
built from. It checks the things that can be checked mechanically — every marker
resolves, a grounded answer carries citations, figures in the text appear in the
context — and is honest about the fact that this is a proxy for faithfulness
rather than a measurement of it. A model can restate a number correctly and
still draw a wrong conclusion from it, and nothing here will notice.

Both take ports. Neither knows what a pgvector index is.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from atlas.domain.evaluation import (
    AnswerCheck,
    GoldenQuestion,
    QuestionResult,
    RetrievalReport,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)
from atlas.domain.orchestration import Answer
from atlas.domain.ports.retriever import Retriever
from atlas.domain.retrieval import PromptContext, RetrievalRequest

logger = logging.getLogger(__name__)

#: A citation marker in an answer.
_MARKER = re.compile(r"\[(\d{1,3})\]")

#: Figures worth checking: money, quantities, dates, references. Deliberately
#: not every digit — a model writing "the first three" should not be accused of
#: inventing a 3.
_FIGURE = re.compile(r"\b\d[\d,.]{2,}\b")


class RetrievalEvaluator:
    """Scores a retriever against a labelled question set."""

    def __init__(self, retriever: Retriever, *, k: int = 8, over_fetch: int = 4) -> None:
        self._retriever = retriever
        self._k = k
        self._over_fetch = over_fetch

    async def run(self, questions: Sequence[GoldenQuestion]) -> RetrievalReport:
        """Ask every question and score the ranking that came back.

        Retrieval runs unauthorized on purpose. Authorization is a per-user
        filter that removes results, and folding it in here would measure the
        fixture's access rules rather than the ranking — the thing the gate is
        for. What a given user may see is tested in ``test_authorization_filter``
        and enforced structurally in the pipeline.
        """
        results = []
        for question in questions:
            candidates = await self._retriever.retrieve(
                RetrievalRequest(
                    query=question.question,
                    limit=self._k,
                    over_fetch=self._over_fetch,
                )
            )
            # Document keys, deduplicated, in rank order: several chunks of one
            # document are one hit, not several. Counting them separately would
            # let a long document score as though it were the whole corpus.
            retrieved = _document_keys(candidates)
            results.append(
                QuestionResult(
                    question=question,
                    retrieved=retrieved,
                    recall=recall_at_k(retrieved, question.relevant, self._k),
                    reciprocal_rank=reciprocal_rank(retrieved, question.relevant),
                    ndcg=ndcg_at_k(retrieved, question.relevant, self._k),
                    k=self._k,
                )
            )

        report = RetrievalReport(k=self._k, results=tuple(results))
        logger.info(
            "retrieval evaluated",
            extra={
                "questions": report.questions,
                "recall_at_k": round(report.recall, 4),
                "mrr": round(report.mrr, 4),
                "ndcg_at_k": round(report.ndcg, 4),
            },
        )
        return report


class AnswerAuditor:
    """Checks an answer against the context it was given.

    What this catches: a marker pointing at a block that was never there, a
    grounded answer that cites nothing, a figure the context does not contain.

    What it does not catch: a correct number in a wrong sentence. Judging that
    needs either a human or a second model, and a gate whose verdict is itself a
    generation is a gate that fails for reasons unrelated to the change under
    test. M12 measures what can be measured without one.
    """

    def audit(self, question: str, answer: Answer, context: PromptContext) -> AnswerCheck:
        """Check one answer.

        Args:
            question: What was asked.
            answer: What came back, with citations already resolved.
            context: What the answer was built from.
        """
        markers = {int(match) for match in _MARKER.findall(answer.text)}
        available = {citation.sequence for citation in context.citations}

        return AnswerCheck(
            question=question,
            grounded=not context.is_empty,
            cited=bool(answer.citations),
            markers_resolved=markers.issubset(available),
            unsupported_figures=_unsupported_figures(answer.text, context.text),
        )


def _document_keys(candidates: Sequence[object]) -> tuple[str, ...]:
    """Rank-ordered document keys, one per document.

    The key is carried in chunk metadata by the evaluation loader. Falling back
    to ``res_model:res_id`` keeps this usable against a corpus that was ingested
    normally rather than built by the harness.
    """
    seen: dict[str, None] = {}
    for candidate in candidates:
        metadata = getattr(candidate, "metadata", {}) or {}
        key = metadata.get("golden_key")
        if not key:
            model = getattr(candidate, "res_model", None)
            record = getattr(candidate, "res_id", None)
            key = f"{model}:{record}" if model and record else None
        if key and key not in seen:
            seen[key] = None
    return tuple(seen)


def _unsupported_figures(text: str, context: str) -> tuple[str, ...]:
    """Figures in the answer that appear nowhere in the context.

    Normalised before comparing, because a model writes ``12,480.00`` where the
    record says ``12480.0`` and both mean the same thing. Being strict about
    formatting would produce a check that fails constantly and gets switched off.
    """
    if not context.strip():
        return ()

    haystack = {_normalise(figure) for figure in _FIGURE.findall(context)}
    return tuple(
        figure
        for figure in dict.fromkeys(_FIGURE.findall(text))
        if _normalise(figure) not in haystack
    )


def _normalise(figure: str) -> str:
    """Strip thousands separators and trailing zeros so 12,480.00 == 12480."""
    cleaned = figure.replace(",", "")
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned
