"""The metrics, and the harness that produces them.

The metric tests use hand-written rankings where the right answer is arithmetic
rather than opinion. That is the point of keeping them pure: a test that asserts
"recall went up" against a real retriever tells you nothing about whether recall
is computed correctly.
"""

from __future__ import annotations

import math

import pytest

from atlas.application.evaluation import AnswerAuditor, RetrievalEvaluator
from atlas.domain.corpus import CandidateChunk
from atlas.domain.evaluation import (
    AnswerCheck,
    GoldenQuestion,
    QuestionResult,
    RetrievalReport,
    Thresholds,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)
from atlas.domain.orchestration import Answer
from atlas.domain.retrieval import Citation, PromptContext, RetrievalRequest

pytestmark = pytest.mark.unit


class TestRecall:
    def test_everything_found(self) -> None:
        assert recall_at_k(["a", "b", "c"], ["a", "b"], 3) == 1.0

    def test_half_found(self) -> None:
        assert recall_at_k(["a", "x"], ["a", "b"], 2) == 0.5

    def test_the_cut_off_is_respected(self) -> None:
        """A document at position 5 does not count towards recall@3."""
        assert recall_at_k(["x", "y", "z", "w", "a"], ["a"], 3) == 0.0

    def test_a_question_with_no_right_answer_scores_zero(self) -> None:
        """Not 1.0.

        "We found all zero of them" would inflate every average it appears in.
        """
        assert recall_at_k(["a"], [], 3) == 0.0


class TestReciprocalRank:
    @pytest.mark.parametrize(
        ("retrieved", "expected"),
        [
            (["a", "x", "y"], 1.0),
            (["x", "a", "y"], 0.5),
            (["x", "y", "a"], 1 / 3),
            (["x", "y", "z"], 0.0),
        ],
    )
    def test_it_is_one_over_the_first_hit(self, retrieved: list[str], expected: float) -> None:
        assert reciprocal_rank(retrieved, ["a"]) == pytest.approx(expected)

    def test_only_the_first_hit_counts(self) -> None:
        assert reciprocal_rank(["a", "b"], ["a", "b"]) == 1.0


class TestNdcg:
    def test_a_perfect_ranking_scores_one(self) -> None:
        assert ndcg_at_k(["a", "b", "c"], ["a", "b"], 3) == pytest.approx(1.0)

    def test_order_matters(self) -> None:
        """Order has to matter.

        This is the metric that changes when a good result moves from third to
        second, which is the reason it is here at all.
        """
        better = ndcg_at_k(["x", "a", "y"], ["a"], 3)
        worse = ndcg_at_k(["x", "y", "a"], ["a"], 3)

        assert better > worse

    def test_it_matches_the_arithmetic(self) -> None:
        # One relevant document at position 2: gain 1/log2(3), ideal 1/log2(2).
        expected = (1 / math.log2(3)) / (1 / math.log2(2))

        assert ndcg_at_k(["x", "a"], ["a"], 2) == pytest.approx(expected)

    def test_nothing_relevant_scores_zero(self) -> None:
        assert ndcg_at_k(["a", "b"], [], 2) == 0.0


class TestQuestionResult:
    def result(self, retrieved: tuple[str, ...], k: int = 3) -> QuestionResult:
        question = GoldenQuestion(id="q", question="?", relevant=("a", "b"))
        return QuestionResult(
            question=question,
            retrieved=retrieved,
            recall=recall_at_k(retrieved, question.relevant, k),
            reciprocal_rank=reciprocal_rank(retrieved, question.relevant),
            ndcg=ndcg_at_k(retrieved, question.relevant, k),
            k=k,
        )

    def test_missed_is_cut_off_where_recall_is(self) -> None:
        """Cut off where recall is.

        Reporting against the whole ranked list showed "nothing missed" next to
        a recall of 0.00 — true, and useless.
        """
        result = self.result(("x", "y", "z", "a", "b"))

        assert result.recall == 0.0
        assert result.missed == ("a", "b")

    def test_a_near_miss_is_distinguishable_from_a_total_one(self) -> None:
        just_outside = self.result(("x", "y", "z", "a"))
        nowhere = self.result(("x", "y", "z"))

        assert just_outside.rank_of_first_hit == 4
        assert nowhere.rank_of_first_hit is None


class TestThresholds:
    def report(self, recall: float, mrr: float, ndcg: float) -> RetrievalReport:
        question = GoldenQuestion(id="q", question="?", relevant=("a",))
        return RetrievalReport(
            k=3,
            results=(
                QuestionResult(
                    question=question,
                    retrieved=("a",),
                    recall=recall,
                    reciprocal_rank=mrr,
                    ndcg=ndcg,
                    k=3,
                ),
            ),
        )

    def test_a_report_that_clears_every_floor_passes(self) -> None:
        thresholds = Thresholds(recall_at_k=0.5, mrr=0.5, ndcg_at_k=0.5)

        assert thresholds.failures(self.report(0.6, 0.6, 0.6)) == ()

    def test_each_failure_names_the_metric_and_the_floor(self) -> None:
        thresholds = Thresholds(recall_at_k=0.9, mrr=0.5, ndcg_at_k=0.5)

        failures = thresholds.failures(self.report(0.6, 0.6, 0.6))

        assert len(failures) == 1
        assert "recall@k" in failures[0]
        assert "0.900" in failures[0]

    def test_exactly_at_the_floor_passes(self) -> None:
        """A gate that fails on equality drifts downward as numbers are rounded."""
        thresholds = Thresholds(recall_at_k=0.6, mrr=0.6, ndcg_at_k=0.6)

        assert thresholds.failures(self.report(0.6, 0.6, 0.6)) == ()


class StubRetriever:
    """Returns a fixed ranking, ignoring the query."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    async def retrieve(self, request: RetrievalRequest) -> list[CandidateChunk]:
        return [
            CandidateChunk(
                chunk_id=index,
                document_id=index,
                content=key,
                score=1.0 - index / 100,
                res_model="res.partner",
                res_id=index,
                metadata={"golden_key": key},
            )
            for index, key in enumerate(self._keys, start=1)
        ]


class TestRetrievalEvaluator:
    async def test_it_scores_every_question(self) -> None:
        questions = [
            GoldenQuestion(id="one", question="?", relevant=("a",)),
            GoldenQuestion(id="two", question="?", relevant=("z",)),
        ]

        report = await RetrievalEvaluator(StubRetriever(["a", "b"]), k=2).run(questions)

        assert report.questions == 2
        assert report.recall == 0.5
        assert report.perfect == 1

    async def test_several_chunks_of_one_document_count_once(self) -> None:
        """Otherwise a long document scores as though it were the whole corpus."""
        retriever = StubRetriever(["a", "a", "a", "b"])
        questions = [GoldenQuestion(id="one", question="?", relevant=("a", "b"))]

        report = await RetrievalEvaluator(retriever, k=2).run(questions)

        assert report.recall == 1.0

    async def test_the_worst_questions_come_first(self) -> None:
        questions = [
            GoldenQuestion(id="found", question="?", relevant=("a",)),
            GoldenQuestion(id="missing", question="?", relevant=("z",)),
        ]

        report = await RetrievalEvaluator(StubRetriever(["a"]), k=1).run(questions)

        assert report.worst[0].question.id == "missing"


class TestAnswerAuditor:
    def context(self, text: str = "Order S00001 totals 12,480.00 EUR.") -> PromptContext:
        return PromptContext(
            text=text,
            citations=(
                Citation(
                    res_model="sale.order",
                    res_id=1,
                    record_name="S00001",
                    snippet=text,
                    score=0.9,
                    sequence=1,
                ),
            ),
        )

    def test_a_grounded_cited_answer_passes(self) -> None:
        answer = Answer(text="It totals 12,480.00 EUR. [1]", citations=self.context().citations)

        check = AnswerAuditor().audit("how much?", answer, self.context())

        assert check.passed
        assert check.cited

    def test_a_marker_naming_a_block_that_was_not_there_fails(self) -> None:
        answer = Answer(text="It totals 12,480.00 EUR. [4]", citations=self.context().citations)

        check = AnswerAuditor().audit("how much?", answer, self.context())

        assert not check.markers_resolved
        assert not check.passed

    def test_a_grounded_answer_with_no_citation_fails(self) -> None:
        answer = Answer(text="It totals 12,480.00 EUR.")

        check = AnswerAuditor().audit("how much?", answer, self.context())

        assert not check.cited
        assert not check.passed

    def test_a_figure_the_context_does_not_contain_is_reported(self) -> None:
        answer = Answer(text="It totals 99,999.00 EUR. [1]", citations=self.context().citations)

        check = AnswerAuditor().audit("how much?", answer, self.context())

        assert check.unsupported_figures == ("99,999.00",)
        assert not check.passed

    def test_formatting_differences_are_not_treated_as_invention(self) -> None:
        """Formatting is not invention.

        A model writes 12,480.00 where the record says 12480.0. A check that
        fails on that is a check somebody switches off.
        """
        answer = Answer(text="It totals 12,480.00 EUR. [1]", citations=self.context().citations)

        check = AnswerAuditor().audit(
            "how much?", answer, self.context("Order S00001 totals 12480.0 EUR.")
        )

        assert check.unsupported_figures == ()

    def test_an_ungrounded_answer_passes_trivially(self) -> None:
        """Nothing to be unfaithful to.

        There was no context. Whether it should have refused is the
        orchestrator's business, and is tested there.
        """
        check = AnswerAuditor().audit(
            "how much?", Answer(text="I don't have information on that."), PromptContext(text="")
        )

        assert check.grounded is False
        assert check.passed

    def test_small_numbers_in_prose_are_not_checked(self) -> None:
        """A small number in prose is not a claim about a figure."""
        answer = Answer(text="The first 3 are listed. [1]", citations=self.context().citations)

        check = AnswerAuditor().audit("which?", answer, self.context())

        assert check.unsupported_figures == ()


class TestAnswerCheckReporting:
    def test_a_failing_check_is_not_silently_passed(self) -> None:
        check = AnswerCheck(question="?", grounded=True, cited=False, markers_resolved=True)

        assert not check.passed


class TestTheHarness:
    """`make eval` itself: it has to run offline and it has to be a gate."""

    def test_the_shipped_golden_set_and_corpus_agree(self) -> None:
        """Every labelled document has to exist.

        Otherwise the metric is measuring a typo rather than retrieval.
        """
        from atlas.interfaces.evaluate import EVALUATION_DIR, load_corpus, load_golden

        corpus = {fixture.key for fixture in load_corpus(EVALUATION_DIR / "corpus.yaml")}
        questions = load_golden(EVALUATION_DIR / "golden.yaml")

        unknown = {key for question in questions for key in question.relevant if key not in corpus}
        assert not unknown, f"golden.yaml labels documents that are not in corpus.yaml: {unknown}"

    def test_every_golden_question_records_its_reasoning(self) -> None:
        """Labels need their reasoning recorded.

        When a metric drops, the first question is whether retrieval got worse
        or the label was wrong. A set with no reasoning cannot answer that.
        """
        from atlas.interfaces.evaluate import EVALUATION_DIR, load_golden

        for question in load_golden(EVALUATION_DIR / "golden.yaml"):
            assert question.note, f"{question.id} has no note explaining its labels"

    def test_the_offline_run_clears_its_own_floors(self) -> None:
        """The gate has to pass on the commit that sets it.

        Otherwise it is not a gate, it is a broken build somebody will disable.
        """
        from atlas.interfaces.evaluate import main

        assert main([]) == 0

    def test_it_reports_without_gating_when_asked(self) -> None:
        from atlas.interfaces.evaluate import main

        assert main(["--no-gate", "--k", "1"]) == 0

    def test_an_unreachable_floor_fails_the_run(self) -> None:
        """The gate actually gates.

        Asserted by making the floor unreachable rather than by trusting the
        comparison.
        """
        from atlas.domain.evaluation import Thresholds as _Thresholds
        from atlas.interfaces import evaluate

        original = evaluate.OFFLINE_THRESHOLDS
        evaluate.OFFLINE_THRESHOLDS = _Thresholds(recall_at_k=1.0, mrr=1.0, ndcg_at_k=1.0)
        try:
            assert evaluate.main([]) == 1
        finally:
            evaluate.OFFLINE_THRESHOLDS = original
