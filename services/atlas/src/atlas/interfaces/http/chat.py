"""The chat API.

One endpoint, streamed. The addon calls it with the caller's context token and
renders what comes back as it arrives.

Streaming is not decoration here. A grounded answer means a document search, an
authorization round-trip to Odoo, and often two model calls with a tool
execution between them. That is measured in seconds, and a request that shows
nothing until it is finished is one people assume has hung.

Errors after the first byte cannot become an HTTP status — the status line is
long gone. They arrive as an ``error`` event instead, which is why the client
must read events rather than assume a 200 means an answer.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from atlas.config.container import Container
from atlas.domain.authorization import UserContext
from atlas.domain.errors import AtlasError, ConfigurationError
from atlas.domain.orchestration import AnswerEvent, AnswerRequest, EventKind, Intent, Turn
from atlas.domain.usage import TokenUsage
from atlas.infrastructure.providers.pricing import estimate_cost
from atlas.interfaces.http.middleware import TRACE_ID_SCOPE_KEY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chat", tags=["chat"])

#: Sent before anything else so a proxy that buffers on content-type has
#: something to flush, and so a client can distinguish "connected" from "still
#: waiting for the model".
_OPEN_EVENT = ": open\n\n"


class TurnModel(BaseModel):
    """One earlier exchange."""

    model_config = {"extra": "forbid"}

    question: str
    answer: str


class AskRequest(BaseModel):
    """A question, asked as the holder of a context token."""

    model_config = {"extra": "forbid"}

    question: str = Field(min_length=1, max_length=4000)
    context_token: str = Field(
        min_length=1,
        description="Minted by Odoo for the acting user. Opaque here (ADR-0006).",
    )
    history: list[TurnModel] = Field(
        default_factory=list,
        description="Earlier turns, oldest first. Summarised when they stop fitting.",
    )
    conversation_id: int | None = Field(
        default=None, description="The atlas.conversation this belongs to."
    )
    intent: Intent | None = Field(
        default=None,
        description="Force a route. Omit to let the router decide, which is the norm.",
    )


@router.post("", summary="Ask a question")
async def ask(request: Request, body: AskRequest) -> StreamingResponse:
    """Answer as the holder of the context token, streaming the result.

    Always returns 200 once the stream opens. A failure that happens after that
    is delivered as an ``error`` event, because there is no status code left to
    put it in.
    """
    container: Container = request.app.state.container

    # Keyed on the context token, which names the user. Checked before anything
    # is fetched: the point is to spend nothing on a request that will not be
    # served, and a limit applied after retrieval would have already cost an
    # authorization round-trip and a search.
    decision = container.limiter.check(body.context_token, now=time.monotonic())
    if not decision.allowed:
        logger.info("rate limited", extra={"retry_after": round(decision.retry_after, 1)})
        return _refused(
            "You are asking faster than Atlas can answer. "
            f"Try again in {max(int(decision.retry_after), 1)} seconds.",
            retry_after=decision.retry_after,
        )

    context = UserContext(
        token=body.context_token,
        # From the ASGI scope, which is where the middleware puts it. Reading
        # `request.state` instead silently yields None, and an answer with no
        # trace id cannot be lined up against Odoo's access log — the one thing
        # the id exists for.
        trace_id=request.scope.get(TRACE_ID_SCOPE_KEY),
    )
    answer_request = AnswerRequest(
        question=body.question,
        history=tuple(Turn(question=t.question, answer=t.answer) for t in body.history),
        conversation_id=body.conversation_id,
        intent=body.intent,
    )

    return StreamingResponse(
        _events(container, context, answer_request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers text/event-stream by default, which turns a stream
            # into one long pause followed by everything at once.
            "X-Accel-Buffering": "no",
        },
    )


async def _events(
    container: Container, context: UserContext, request: AnswerRequest
) -> AsyncIterator[str]:
    """Render the orchestrator's events as server-sent events."""
    yield _OPEN_EVENT
    try:
        async for event in container.answers.stream(context, request):
            yield _encode(event)
    except AtlasError as exc:
        logger.warning(
            "answer failed mid-stream",
            extra={"trace_id": context.trace_id, "error": exc.code},
        )
        yield _encode(AnswerEvent.error(exc.message))
    except Exception:
        # Deliberately broad, and deliberately vague to the client: an unhandled
        # failure here would otherwise close the connection with no explanation
        # at all, and the client cannot tell that from a network drop.
        logger.exception("unhandled failure while answering", extra={"trace_id": context.trace_id})
        yield _encode(AnswerEvent.error("The answer could not be completed."))


def _refused(message: str, *, retry_after: float) -> StreamingResponse:
    """A refusal the client parses exactly like any other stream.

    Still 200 with an ``error`` event rather than a 429. The panel reads events;
    giving it a second failure shape to handle would mean two code paths where
    one will do. ``Retry-After`` is set anyway, so a proxy or a script sees the
    conventional signal.
    """
    return StreamingResponse(
        iter([_encode(AnswerEvent.error(message))]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Retry-After": str(max(int(retry_after), 1)),
        },
    )


def _cost(model: str, usage: TokenUsage) -> float:
    """What this answer cost, in US dollars.

    An estimate. Providers round and occasionally reprice, and a model nobody
    has priced yet reports zero rather than failing the request — a missing cost
    figure is a reporting gap, not a reason to withhold an answer somebody is
    already reading.
    """
    if not model:
        return 0.0
    try:
        return float(estimate_cost(model, usage))
    except ConfigurationError:
        logger.warning("no price configured for %s; reporting zero cost", model)
        return 0.0


def _encode(event: AnswerEvent) -> str:
    """One event, in the SSE wire format.

    Newlines inside a payload would end the event early, so everything travels
    as JSON on a single ``data:`` line.
    """
    payload: dict[str, object] = {}
    if event.kind is EventKind.DELTA:
        payload = {"text": event.text}
    elif event.kind is EventKind.ERROR:
        payload = {"message": event.text}
    elif event.kind is EventKind.DONE and event.answer is not None:
        answer = event.answer
        payload = {
            "text": answer.text,
            "refused": answer.refused,
            "intent": answer.intent.value,
            "tools_called": list(answer.tools_called),
            "prompt_version": answer.prompt_version,
            "trace_id": answer.trace_id,
            "model": answer.model,
            "cost_usd": _cost(answer.model, answer.usage),
            "usage": {
                "input_tokens": answer.usage.input_tokens,
                "output_tokens": answer.usage.output_tokens,
                "total_tokens": answer.usage.total_tokens,
            },
            "citations": [
                {
                    "sequence": citation.sequence,
                    "res_model": citation.res_model,
                    "res_id": citation.res_id,
                    "record_name": citation.record_name,
                    "snippet": citation.snippet,
                }
                for citation in answer.citations
            ],
        }
    return f"event: {event.kind.value}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
