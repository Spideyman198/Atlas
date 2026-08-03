"""What the router decides, and what it deliberately does not decide.

The rules only fire on questions a rule can actually recognise. Everything else
is hybrid, which costs more and cannot be wrong for it — so most of this file is
about the boundary between "confident" and "not confident", not about coverage
of every phrasing.
"""

from __future__ import annotations

import pytest

from atlas.application.router import IntentRouter
from atlas.domain.orchestration import Intent

pytestmark = pytest.mark.unit


@pytest.fixture
def router() -> IntentRouter:
    return IntentRouter()


class TestLiveDataQuestions:
    @pytest.mark.parametrize(
        "question",
        [
            "how many orders did we ship last month?",
            "what is the total revenue this quarter?",
            "which invoices are overdue?",
            "how much of the large cabinet is in stock?",
            "who owes us the most money?",
            "what is the status of S00042?",
            "show me the top customers by revenue",
        ],
    )
    def test_they_route_to_tools(self, router: IntentRouter, question: str) -> None:
        assert router.route(question).intent is Intent.STRUCTURED

    def test_a_record_reference_is_enough_on_its_own(self, router: IntentRouter) -> None:
        """Whatever else the sentence says, it is about that record."""
        assert router.route("anything on INV/2026/0001?").intent is Intent.STRUCTURED


class TestDocumentQuestions:
    @pytest.mark.parametrize(
        "question",
        [
            "what does our refund policy say?",
            "explain the warranty terms",
            "where is the installation manual?",
            "what were the notes on that discussion?",
        ],
    )
    def test_they_route_to_search(self, router: IntentRouter, question: str) -> None:
        assert router.route(question).intent is Intent.SEMANTIC


class TestBoth:
    def test_a_question_touching_both_gets_both(self, router: IntentRouter) -> None:
        routing = router.route("what does the contract say about the overdue invoices?")

        assert routing.intent is Intent.HYBRID

    @pytest.mark.parametrize(
        "question",
        [
            "how are things going?",
            "tell me about Acme",
            "what should I look at today?",
            "anything I should know?",
        ],
    )
    def test_an_unrecognised_question_gets_both(self, router: IntentRouter, question: str) -> None:
        """The design: no rule fires on a question a rule cannot recognise."""
        routing = router.route(question)

        assert routing.intent is Intent.HYBRID
        assert routing.intent.uses_tools
        assert routing.intent.uses_retrieval


class TestRefusal:
    @pytest.mark.parametrize("question", ["", "   ", "?", "ok"])
    def test_there_is_nothing_to_answer(self, router: IntentRouter, question: str) -> None:
        routing = router.route(question)

        assert routing.intent is Intent.REFUSE
        assert routing.refusal == "empty"

    @pytest.mark.parametrize(
        "question",
        [
            "create a new customer called Acme",
            "delete order S00042",
            "change the price of the large cabinet to 90",
            "send the invoice to the customer",
            "confirm quotation S00013",
        ],
    )
    def test_asking_for_a_change_is_refused_immediately(
        self, router: IntentRouter, question: str
    ) -> None:
        """Better than letting the model discover there is no such tool."""
        routing = router.route(question)

        assert routing.intent is Intent.REFUSE
        assert routing.refusal == "write"

    @pytest.mark.parametrize(
        "question",
        [
            "which orders were cancelled last week?",
            "how many invoices were sent yesterday?",
            "who created this quotation?",
            "what changed on order S00042?",
        ],
    )
    def test_asking_about_a_change_is_not_asking_for_one(
        self, router: IntentRouter, question: str
    ) -> None:
        """The words overlap; the intent does not."""
        assert router.route(question).intent is not Intent.REFUSE


class TestTheDecisionIsExplained:
    def test_every_route_carries_a_reason(self, router: IntentRouter) -> None:
        """When an answer is wrong, the first question is what was fetched."""
        for question in ("how many orders?", "refund policy?", "hello there", ""):
            assert router.route(question).reason


class TestIntentFlags:
    def test_structured_uses_tools_only(self) -> None:
        assert Intent.STRUCTURED.uses_tools
        assert not Intent.STRUCTURED.uses_retrieval

    def test_semantic_uses_retrieval_only(self) -> None:
        assert Intent.SEMANTIC.uses_retrieval
        assert not Intent.SEMANTIC.uses_tools

    def test_refuse_uses_neither(self) -> None:
        assert not Intent.REFUSE.uses_tools
        assert not Intent.REFUSE.uses_retrieval
