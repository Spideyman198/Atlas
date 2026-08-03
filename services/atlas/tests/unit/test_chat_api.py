"""The chat endpoint, over a real HTTP client.

The interesting cases are all about a stream that has already started. Once the
first byte is out, the status line is gone and a failure has nowhere to go
except into the stream itself — so a client that treats 200 as "it worked" is
wrong, and these tests pin the shape that lets it be right.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atlas.config.container import Container
from atlas.domain.authorization import UserContext
from atlas.domain.errors import AuthorizationError, DependencyUnavailableError
from atlas.domain.orchestration import Answer, AnswerEvent, AnswerRequest, Intent
from atlas.domain.retrieval import Citation
from atlas.interfaces.http.chat import router
from atlas.interfaces.http.errors import register_exception_handlers
from atlas.interfaces.http.middleware import TraceIdMiddleware

pytestmark = pytest.mark.unit


class StubAnswers:
    """An orchestrator that replays a script, and records what it was asked."""

    def __init__(
        self,
        events: list[AnswerEvent] | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self._events = events or [
            AnswerEvent.delta("It is "),
            AnswerEvent.delta("confirmed."),
            AnswerEvent.done(Answer(text="It is confirmed.")),
        ]
        self._failure = failure
        self.calls: list[tuple[UserContext, AnswerRequest]] = []

    async def stream(
        self, context: UserContext, request: AnswerRequest
    ) -> AsyncIterator[AnswerEvent]:
        self.calls.append((context, request))
        for event in self._events:
            yield event
        if self._failure is not None:
            raise self._failure


def client(answers: StubAnswers) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(TraceIdMiddleware)
    app.include_router(router)
    app.state.container = cast(Container, type("C", (), {"answers": answers})())
    return TestClient(app)


def events(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body into ``{event, data}`` dictionaries."""
    parsed = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        kind = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        parsed.append({"event": kind, "data": json.loads(data)})
    return parsed


def ask(answers: StubAnswers, **overrides: Any) -> list[dict[str, Any]]:
    body = {"question": "what is the status of S00001?", "context_token": "alice-token"}
    body.update(overrides)
    response = client(answers).post("/v1/chat", json=body)
    assert response.status_code == 200, response.text
    return events(response.text)


class TestStreaming:
    def test_the_answer_arrives_as_events(self) -> None:
        parsed = ask(StubAnswers())

        assert [event["event"] for event in parsed] == ["delta", "delta", "done"]
        assert "".join(e["data"]["text"] for e in parsed if e["event"] == "delta") == (
            "It is confirmed."
        )

    def test_the_content_type_is_a_stream(self) -> None:
        response = client(StubAnswers()).post(
            "/v1/chat", json={"question": "hello?", "context_token": "t"}
        )

        assert response.headers["content-type"].startswith("text/event-stream")

    def test_buffering_is_disabled(self) -> None:
        """Buffering has to be off.

        nginx buffers text/event-stream by default, which turns a stream into
        one long pause followed by everything at once.
        """
        response = client(StubAnswers()).post(
            "/v1/chat", json={"question": "hello?", "context_token": "t"}
        )

        assert response.headers["x-accel-buffering"] == "no"

    def test_the_terminal_event_carries_the_whole_answer(self) -> None:
        answer = Answer(
            text="It is confirmed. [1]",
            citations=(
                Citation(
                    res_model="sale.order",
                    res_id=42,
                    record_name="S00001",
                    snippet="Order S00001 is confirmed.",
                    score=0.91,
                    sequence=1,
                ),
            ),
            intent=Intent.HYBRID,
            tools_called=("find_records",),
            prompt_version="system@abc123",
        )
        parsed = ask(StubAnswers([AnswerEvent.done(answer)]))

        done = parsed[-1]["data"]
        assert done["text"] == "It is confirmed. [1]"
        assert done["tools_called"] == ["find_records"]
        assert done["prompt_version"] == "system@abc123"
        assert done["citations"][0]["record_name"] == "S00001"
        assert done["citations"][0]["res_id"] == 42

    def test_a_refusal_is_flagged_rather_than_hidden(self) -> None:
        """A refusal has to be distinguishable.

        A client that renders one as a normal answer shows the user an assistant
        that appears to have nothing to say.
        """
        parsed = ask(StubAnswers([AnswerEvent.done(Answer(text="I don't know.", refused=True))]))

        assert parsed[-1]["data"]["refused"] is True

    def test_a_newline_in_the_answer_does_not_end_the_event(self) -> None:
        """A bare newline in an SSE payload terminates the event early."""
        parsed = ask(StubAnswers([AnswerEvent.delta("line one\n\nline two")]))

        assert parsed[0]["data"]["text"] == "line one\n\nline two"


class TestFailures:
    def test_a_failure_after_the_first_byte_arrives_as_an_event(self) -> None:
        """There is no status code left to put it in."""
        parsed = ask(
            StubAnswers(
                [AnswerEvent.delta("Looking...")],
                failure=AuthorizationError("Odoo declined the context"),
            )
        )

        assert parsed[-1]["event"] == "error"
        assert "declined" in parsed[-1]["data"]["message"]

    def test_an_unexpected_failure_still_produces_an_event(self) -> None:
        """Something has to come back.

        A closed connection with no explanation is indistinguishable from a
        dropped network.
        """
        parsed = ask(StubAnswers([], failure=RuntimeError("something unforeseen")))

        assert parsed[-1]["event"] == "error"

    def test_an_unexpected_failure_does_not_leak_its_detail(self) -> None:
        parsed = ask(StubAnswers([], failure=RuntimeError("psycopg: password authentication")))

        assert "password" not in json.dumps(parsed)

    def test_a_dependency_failure_is_reported_in_words(self) -> None:
        parsed = ask(StubAnswers([], failure=DependencyUnavailableError("Odoo is unreachable")))

        assert parsed[-1]["event"] == "error"
        assert "unreachable" in parsed[-1]["data"]["message"]


class TestRequestValidation:
    def test_the_question_and_token_are_both_required(self) -> None:
        for body in ({"question": "hello?"}, {"context_token": "t"}):
            response = client(StubAnswers()).post("/v1/chat", json=body)

            assert response.status_code == 422

    def test_an_empty_question_is_refused_by_the_schema(self) -> None:
        response = client(StubAnswers()).post(
            "/v1/chat", json={"question": "", "context_token": "t"}
        )

        assert response.status_code == 422

    def test_an_unknown_field_is_refused(self) -> None:
        """A typo in a field name should not silently do nothing."""
        response = client(StubAnswers()).post(
            "/v1/chat", json={"question": "hi?", "context_token": "t", "intnet": "structured"}
        )

        assert response.status_code == 422


class TestWhatReachesTheOrchestrator:
    def test_the_token_is_passed_through_untouched(self) -> None:
        answers = StubAnswers()

        ask(answers, context_token="opaque-token-value")

        assert answers.calls[0][0].token == "opaque-token-value"

    def test_history_arrives_oldest_first(self) -> None:
        answers = StubAnswers()

        ask(
            answers,
            history=[
                {"question": "which orders are open?", "answer": "S00001 and S00002."},
                {"question": "and the second?", "answer": "S00002 is confirmed."},
            ],
        )

        history = answers.calls[0][1].history
        assert [turn.question for turn in history] == [
            "which orders are open?",
            "and the second?",
        ]

    def test_a_forced_intent_is_passed_on(self) -> None:
        answers = StubAnswers()

        ask(answers, intent="semantic")

        assert answers.calls[0][1].intent is Intent.SEMANTIC

    def test_no_intent_leaves_the_routing_to_the_router(self) -> None:
        answers = StubAnswers()

        ask(answers)

        assert answers.calls[0][1].intent is None

    def test_the_answer_is_traceable_to_odoos_access_log(self) -> None:
        """The id the middleware bound has to reach the orchestrator.

        Read from the wrong place it is silently None, and the answer cannot be
        lined up against what Odoo recorded — the one thing the id is for.
        """
        answers = StubAnswers()

        response = client(answers).post(
            "/v1/chat",
            json={"question": "hello?", "context_token": "t"},
            headers={"X-Request-ID": "abc123def456"},
        )

        assert response.status_code == 200
        assert answers.calls[0][0].trace_id == "abc123def456"

    def test_the_conversation_id_is_carried(self) -> None:
        answers = StubAnswers()

        ask(answers, conversation_id=17)

        assert answers.calls[0][1].conversation_id == 17
