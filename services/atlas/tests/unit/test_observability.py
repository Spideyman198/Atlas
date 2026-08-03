"""Metrics, traces and cost.

The rule under test throughout: **measurement never breaks the thing being
measured.** A metrics backend that rejects a label, a collector that is not
running, a model nobody has priced — none of them may turn into a failed answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from atlas.domain.observability import NullRecorder, Recorder
from atlas.domain.usage import TokenUsage
from atlas.infrastructure.observability import configure_tracing, metrics, span
from atlas.infrastructure.observability.recorder import PrometheusRecorder
from atlas.interfaces.http.app import create_app

pytestmark = pytest.mark.unit


class TestTheRecorderPort:
    def test_the_null_recorder_satisfies_it(self) -> None:
        assert isinstance(NullRecorder(), Recorder)

    def test_the_prometheus_recorder_satisfies_it(self) -> None:
        assert isinstance(PrometheusRecorder(), Recorder)

    def test_the_null_recorder_swallows_everything(self) -> None:
        """The default, so no use case has to branch on whether anyone watches."""
        recorder = NullRecorder()

        recorder.answer_finished(outcome="answered", intent="hybrid", seconds=1.0)
        recorder.retrieval_finished(candidates=10, authorized=4, used=3, seconds=0.1)
        recorder.tool_finished(tool="find_records", outcome="ok", seconds=0.2)
        recorder.provider_finished(provider="fake", model="m", outcome="ok")


class TestPrometheusRecorder:
    def value(self, name: str, **labels: str) -> float:
        return metrics.REGISTRY.get_sample_value(name, labels) or 0.0

    def test_an_answer_is_counted(self) -> None:
        before = self.value("atlas_answers_total", outcome="answered", intent="hybrid")

        PrometheusRecorder().answer_finished(outcome="answered", intent="hybrid", seconds=0.5)

        after = self.value("atlas_answers_total", outcome="answered", intent="hybrid")
        assert after == before + 1

    def test_the_denial_rate_is_derivable(self) -> None:
        """The number that says whether the over-fetch factor is set right."""
        before = self.value("atlas_chunks_total", stage="denied")

        PrometheusRecorder().retrieval_finished(candidates=32, authorized=8, used=6, seconds=0.05)

        assert self.value("atlas_chunks_total", stage="denied") == before + 24

    def test_tokens_are_counted_by_kind(self) -> None:
        recorder = PrometheusRecorder()
        before = self.value("atlas_tokens_total", provider="fake", model="fake-model", kind="input")

        recorder.provider_finished(
            provider="fake", model="fake-model", outcome="ok", input_tokens=120, output_tokens=8
        )

        after = self.value("atlas_tokens_total", provider="fake", model="fake-model", kind="input")
        assert after == before + 120

    def test_a_broken_metric_never_reaches_the_caller(self) -> None:
        """A metrics backend is not worth a failed answer."""
        recorder = PrometheusRecorder()

        # A label value of the wrong type is a programming mistake, and it must
        # surface as a missing series rather than as a 500 on somebody's
        # question.
        recorder.answer_finished(outcome=None, intent="hybrid", seconds="not a number")  # type: ignore[arg-type]


class TestTheMetricsEndpoint:
    def test_it_serves_the_exposition_format(self) -> None:
        response = TestClient(create_app()).get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "atlas_answers_total" in response.text

    def test_it_is_not_in_the_public_schema(self) -> None:
        """It is scraped, not called.

        Listing it as an API invites somebody to build on the format.
        """
        schema = TestClient(create_app()).get("/openapi.json").json()

        assert "/metrics" not in schema["paths"]

    def test_no_series_is_labelled_by_anything_unbounded(self) -> None:
        """Labels stay low-cardinality.

        Scraping must not be a way to learn what anybody asked, and a label with
        unbounded values is how a metrics backend falls over.

        Asserted over label *names*, not over the exposition text: the help
        strings legitimately contain words like "user" while the series
        themselves carry nothing of the sort.
        """
        allowed = {"outcome", "intent", "stage", "tool", "provider", "model", "kind", "source"}

        used = {
            name
            for family in metrics.REGISTRY.collect()
            for sample in family.samples
            for name in sample.labels
            # Histograms label their buckets with `le`, which is the format, not
            # a dimension somebody chose.
            if name != "le"
        }

        assert used <= allowed, f"unbounded metric labels: {sorted(used - allowed)}"


class TestTracing:
    def test_it_stays_off_without_an_endpoint(self) -> None:
        """An engine that cannot start without a collector would be a poor trade."""
        assert configure_tracing(endpoint=None) is False

    def test_a_span_works_whether_or_not_tracing_is_on(self) -> None:
        with span("test", trace_id="abc123", attributes={"atlas.intent": "hybrid"}) as current:
            assert current is not None

    def test_a_span_does_not_swallow_the_failure_it_records(self) -> None:
        """The failure is recorded and re-raised.

        A trace saying the request succeeded while the caller saw it fail is
        worse than no trace.
        """
        with pytest.raises(ValueError, match="boom"), span("test"):
            raise ValueError("boom")

    def test_an_attribute_with_no_value_is_left_off(self) -> None:
        with span("test", attributes={"present": "yes", "absent": None}) as current:
            assert current is not None


class TestCostReporting:
    def cost(self, model: str, usage: TokenUsage) -> float:
        from atlas.interfaces.http.chat import _cost

        return _cost(model, usage)

    def test_a_priced_model_reports_a_figure(self) -> None:
        from atlas.infrastructure.providers.pricing import known_models

        model = sorted(known_models())[0]

        assert self.cost(model, TokenUsage(input_tokens=1_000, output_tokens=1_000)) > 0

    def test_an_unpriced_model_reports_zero_rather_than_failing(self) -> None:
        """A missing price is a reporting gap.

        Not a reason to withhold an answer somebody is already reading.
        """
        assert self.cost("nobody-priced-this", TokenUsage(input_tokens=100)) == 0.0

    def test_no_model_reports_zero(self) -> None:
        assert self.cost("", TokenUsage(input_tokens=100)) == 0.0


class TestAnswersCarryWhatCostingNeeds:
    async def test_an_answer_names_the_model_that_produced_it(self) -> None:
        """Cost is computed where the price table lives, from this field."""
        from tests.unit.test_synthesis import CONTEXT, chunk, service

        from atlas.domain.orchestration import AnswerRequest

        answer = await service(chunks=(chunk("Order S00001 is draft."),)).answer(
            CONTEXT, AnswerRequest(question="what does the policy say?")
        )

        assert answer.model == "fake-model"

    async def test_usage_is_accumulated_across_the_tool_loop(self) -> None:
        """One answer can be several provider calls, and the cost is all of them."""
        from tests.unit.test_synthesis import CONTEXT, service

        from atlas.domain.chat import StopReason, ToolCall
        from atlas.domain.orchestration import AnswerRequest
        from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response

        chat = FakeChatProvider(
            [
                fake_response(
                    "",
                    stop_reason=StopReason.TOOL_USE,
                    tool_calls=(ToolCall(id="c1", name="find_records", arguments={}),),
                ),
                fake_response("Confirmed."),
            ]
        )

        answer = await service(chat=chat, tools={"find_records": lambda _: {}}).answer(
            CONTEXT, AnswerRequest(question="how many orders are there?")
        )

        # Two calls at 10 input tokens each.
        assert answer.usage.input_tokens == 20


class TestRecordedDuringAnAnswer:
    async def test_a_refusal_is_recorded_as_one(self) -> None:
        from tests.unit.test_synthesis import CONTEXT, service

        from atlas.domain.orchestration import AnswerRequest

        recorded: list[dict[str, Any]] = []

        class Spy(NullRecorder):
            def answer_finished(self, *, outcome: str, intent: str, seconds: float) -> None:
                recorded.append({"outcome": outcome, "intent": intent})

        answers = service(chunks=(), tools={})
        # Reaching into the instance: asserting on the wiring is the point.
        answers._recorder = Spy()

        await answers.answer(CONTEXT, AnswerRequest(question="delete order S00042"))

        assert recorded == [{"outcome": "refused", "intent": "refuse"}]
