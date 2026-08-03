"""Turning a question into a grounded answer.

    route → gather → prompt → generate (with tools) → resolve citations

Three properties this module is responsible for, in the order they matter.

**Nothing to go on means refusing, not generating.** If no context was retrieved
and no tool is available, the model is not asked. There is no prompt wording that
makes a generation from nothing anything other than a guess, and the one thing
worse than "I don't have information on that" is a fluent paragraph of invented
figures. This is the M10 acceptance criterion and it is enforced here rather than
hoped for in a template.

**Citations are resolved, not accepted.** The model writes ``[2]``; this module
decides whether block 2 existed. A marker pointing at nothing is removed from the
answer, because a citation a reader cannot follow is worse than no citation.

**The tool loop is bounded.** A model that keeps calling tools forever is a model
that never answers, and the bound turns that into a slightly worse answer instead
of a request that never returns.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Final

from atlas.application.memory import ConversationMemory
from atlas.application.retrieval import RetrievalPipeline
from atlas.application.router import IntentRouter
from atlas.application.tools import ToolBox
from atlas.domain.authorization import UserContext
from atlas.domain.chat import (
    ChatRequest,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from atlas.domain.observability import NullRecorder, Recorder
from atlas.domain.orchestration import (
    Answer,
    AnswerEvent,
    AnswerRequest,
    Intent,
    Routing,
    Turn,
)
from atlas.domain.output_guard import inspect as inspect_output
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.prompts import PromptLibrary, RenderedPrompt
from atlas.domain.redaction import redact
from atlas.domain.retrieval import Citation, PromptContext, RetrievalRequest
from atlas.domain.usage import TokenUsage

logger = logging.getLogger(__name__)

#: How many times the model may call tools before it has to answer with what it
#: has. Five covers "look it up, notice it was the wrong customer, look again,
#: check the invoices" with room to spare.
MAX_TOOL_ROUNDS: Final = 5

#: A citation marker in generated text: [1], [12].
_MARKER = re.compile(r"\[(\d{1,3})\]")


@dataclass(frozen=True, slots=True)
class AnswerBudget:
    """The three numbers that decide how much one answer may spend.

    Grouped because they only make sense together: raising the retrieval limit
    without the token budget just fetches chunks that get dropped again.
    """

    retrieval_limit: int = 8
    token_budget: int = 4000
    max_output_tokens: int = 4096


class AnswerService:
    """Answers one question, as one person, from what that person may see."""

    # An orchestrator's collaborator count is the job, not an accident: it is
    # the one place routing, retrieval, tools, memory and the model meet.
    def __init__(  # noqa: PLR0913
        self,
        *,
        chat: ChatProvider,
        prompts: PromptLibrary,
        retrieval: RetrievalPipeline,
        tools: ToolBox,
        memory: ConversationMemory,
        router: IntentRouter | None = None,
        budget: AnswerBudget | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self._chat = chat
        self._prompts = prompts
        self._retrieval = retrieval
        self._tools = tools
        self._memory = memory
        self._router = router or IntentRouter()
        self._budget = budget or AnswerBudget()
        # A no-op unless the composition root wires the real one. The
        # application layer records through a port; what a Prometheus counter
        # is stays in infrastructure.
        self._recorder = recorder or NullRecorder()

    async def answer(self, context: UserContext, request: AnswerRequest) -> Answer:
        """Answer in one piece.

        Raises:
            AuthorizationError: Odoo declined the context, or could not be
                asked. Retrieval fails closed and this does not soften it.
        """
        final: Answer | None = None
        async for event in self.stream(context, request):
            if event.answer is not None:
                final = event.answer
        # `stream` always ends with a done event, so this cannot be None. The
        # assert is for mypy and for anyone who later makes that untrue.
        assert final is not None  # noqa: S101
        return final

    async def stream(
        self, context: UserContext, request: AnswerRequest
    ) -> AsyncIterator[AnswerEvent]:
        """Answer incrementally, ending with a terminal event carrying the whole.

        Both entry points share this one path. An answer assembled differently
        from the way it is streamed is an answer that behaves differently in the
        UI than in the tests.
        """
        started = time.perf_counter()
        routing = self._route(request)

        if routing.intent is Intent.REFUSE:
            answer = self._refuse(routing, context, kind=routing.refusal or "unknown")
            self._recorder.answer_finished(
                outcome="refused", intent=routing.intent.value, seconds=_since(started)
            )
            yield AnswerEvent.delta(answer.text)
            yield AnswerEvent.done(answer)
            return

        prompt_context = await self._gather(context, request, routing)
        catalog = await self._catalog(context, routing)

        if prompt_context.is_empty and not catalog:
            # The acceptance criterion. Nothing was retrieved and there is no
            # tool to ask, so there is nothing to be grounded in — the model is
            # not called at all.
            logger.info(
                "refusing: nothing to ground an answer on",
                extra={"trace_id": context.trace_id, "intent": routing.intent.value},
            )
            answer = self._refuse(routing, context, kind="unknown")
            self._recorder.answer_finished(
                outcome="refused", intent=routing.intent.value, seconds=_since(started)
            )
            yield AnswerEvent.delta(answer.text)
            yield AnswerEvent.done(answer)
            return

        system = self._prompts.render("system")
        chat_request = await self._build_request(request, prompt_context, catalog, system)

        text_parts: list[str] = []
        tools_called: list[str] = []
        usage = None
        rounds = 0

        while True:
            turn_text: list[str] = []
            calls: list[ToolCall] = []
            stop: StopReason | None = None

            async for chunk in self._chat.stream(chat_request):
                if chunk.text_delta:
                    turn_text.append(chunk.text_delta)
                    yield AnswerEvent.delta(chunk.text_delta)
                if chunk.tool_calls:
                    calls.extend(chunk.tool_calls)
                if chunk.usage is not None:
                    usage = chunk.usage if usage is None else usage + chunk.usage
                if chunk.stop_reason is not None:
                    stop = chunk.stop_reason

            text_parts.extend(turn_text)

            if stop is not StopReason.TOOL_USE or not calls:
                break

            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                logger.warning(
                    "tool loop hit its ceiling; answering with what is in hand",
                    extra={"trace_id": context.trace_id, "rounds": rounds},
                )
                break

            results = await self._execute(context, calls)
            tools_called.extend(call.name for call in calls)

            chat_request = chat_request.with_messages(
                [
                    *chat_request.messages,
                    Message(
                        role=Role.ASSISTANT,
                        content="".join(turn_text),
                        tool_calls=tuple(calls),
                    ),
                    Message(role=Role.TOOL, tool_results=tuple(results)),
                ]
            )

        answer = self._finish(
            context=context,
            routing=routing,
            system=system,
            prompt_context=prompt_context,
            text="".join(text_parts),
            tools_called=tools_called,
            usage=usage,
            started=started,
        )
        yield AnswerEvent.done(answer)

    def _finish(  # noqa: PLR0913 - one call site, and every part is needed
        self,
        *,
        context: UserContext,
        routing: Routing,
        system: RenderedPrompt,
        prompt_context: PromptContext,
        text: str,
        tools_called: Sequence[str],
        usage: TokenUsage | None,
        started: float,
    ) -> Answer:
        """Resolve citations, check the output, and assemble the answer."""
        resolved, citations = _resolve_citations(text, prompt_context.citations)

        # Last gate before a person reads this. The redaction upstream covers
        # what went in; this covers a model that reconstructed something, and a
        # model that was talked into quoting its own instructions.
        cleaned = redact(resolved)
        if cleaned.redacted:
            logger.warning(
                "redacted a generated answer",
                extra={"trace_id": context.trace_id, "redactions": cleaned.counts},
            )
        resolved = cleaned.text

        if not inspect_output(resolved, instructions=system.text).safe:
            logger.warning(
                "answer reproduced the system prompt; replacing it",
                extra={"trace_id": context.trace_id},
            )
            self._recorder.answer_finished(
                outcome="blocked", intent=routing.intent.value, seconds=_since(started)
            )
            return Answer(
                text=self._prompts.render("refusal", kind="unknown").text,
                refused=True,
                intent=routing.intent,
                usage=usage or TokenUsage(),
                model=self._chat.model,
                prompt_version=system.identity,
                trace_id=context.trace_id,
            )

        answer = Answer(
            text=resolved,
            citations=citations,
            intent=routing.intent,
            tools_called=tuple(tools_called),
            usage=usage or TokenUsage(),
            model=self._chat.model,
            prompt_version=system.identity,
            trace_id=context.trace_id,
        )
        self._recorder.provider_finished(
            provider=self._chat.name,
            model=self._chat.model,
            outcome="ok",
            input_tokens=answer.usage.input_tokens,
            output_tokens=answer.usage.output_tokens,
        )
        self._recorder.answer_finished(
            outcome="answered", intent=routing.intent.value, seconds=_since(started)
        )
        logger.info(
            "answered",
            extra={
                "trace_id": context.trace_id,
                "intent": routing.intent.value,
                "chunks_used": prompt_context.chunks_used,
                "tool_calls": len(tools_called),
                "citations": len(citations),
                "prompt": system.identity,
            },
        )
        return answer

    async def _build_request(
        self,
        request: AnswerRequest,
        prompt_context: PromptContext,
        catalog: Sequence[ToolDefinition],
        system: RenderedPrompt,
    ) -> ChatRequest:
        """Assemble the first chat request: history, context, question, tools.

        History is compressed here rather than by the caller because the
        summariser is a model call, and doing it before the refusal checks would
        spend one on a question that is about to be declined.
        """
        summary, recent = await self._memory.compress(request.history)
        messages = _conversation(recent, question=request.question)
        # The last turn is the question, rendered with whatever context and
        # summary came back. Everything before it is the conversation as it was.
        messages[-1] = Message(
            role=Role.USER,
            content=self._prompts.render(
                "answer",
                question=request.question,
                context=prompt_context.text,
                summary=summary,
            ).text,
        )
        return ChatRequest(
            messages=tuple(messages),
            system=system.text,
            tools=tuple(catalog),
            max_output_tokens=self._budget.max_output_tokens,
            stream_hint=True,
        )

    async def _execute(self, context: UserContext, calls: Sequence[ToolCall]) -> list[ToolResult]:
        """Run the tools the model asked for, in the order it asked.

        Sequential rather than concurrent. The calls in one round are usually a
        lookup followed by a narrowing of it, and Odoo is a synchronous worker
        pool — firing four at once takes four workers away from the ERP to save
        a fraction of one model call.
        """
        results: list[ToolResult] = []
        for call in calls:
            started = time.perf_counter()
            result = await self._tools.execute(context, call)
            self._recorder.tool_finished(
                tool=call.name,
                outcome="rejected" if result.is_error else "ok",
                seconds=_since(started),
            )
            results.append(result)
        return results

    def _route(self, request: AnswerRequest) -> Routing:
        if request.intent is not None:
            return Routing(request.intent, "requested by the caller")
        return self._router.route(request.question)

    async def _gather(
        self, context: UserContext, request: AnswerRequest, routing: Routing
    ) -> PromptContext:
        """Retrieve documents, when the route calls for them.

        Raises:
            AuthorizationError: Propagated. An unreachable authorization gateway
                must never degrade into an ungrounded answer.
        """
        if not routing.intent.uses_retrieval:
            return PromptContext(text="")

        result = await self._retrieval.run(
            context,
            RetrievalRequest(
                query=request.question,
                limit=self._budget.retrieval_limit,
                token_budget=self._budget.token_budget,
            ),
        )
        return result.context

    async def _catalog(self, context: UserContext, routing: Routing) -> Sequence[ToolDefinition]:
        """The tools to offer, if any.

        A provider that ignores tool definitions is told about none: sending
        them anyway produces a model that describes the tool it would have
        called.
        """
        if not routing.intent.uses_tools or not self._chat.supports_tools:
            return ()
        return await self._tools.catalog(context)

    def _refuse(self, routing: Routing, context: UserContext, *, kind: str) -> Answer:
        refusal = self._prompts.render("refusal", kind=kind)
        return Answer(
            text=refusal.text,
            refused=True,
            intent=routing.intent,
            prompt_version=refusal.identity,
            trace_id=context.trace_id,
        )


def _since(started: float) -> float:
    """Seconds elapsed, from a monotonic clock rather than the wall."""
    return time.perf_counter() - started


def _conversation(history: Sequence[Turn], *, question: str) -> list[Message]:
    """Recent turns as messages, with a placeholder for the current question.

    The caller replaces the last entry with the rendered prompt. Building the
    list here keeps the ordering — user, assistant, user — in one place.
    """
    messages: list[Message] = []
    for turn in history:
        messages.append(Message(role=Role.USER, content=turn.question))
        messages.append(Message(role=Role.ASSISTANT, content=turn.answer))
    messages.append(Message(role=Role.USER, content=question))
    return messages


def _resolve_citations(
    text: str, available: Sequence[Citation]
) -> tuple[str, tuple[Citation, ...]]:
    """Match the markers the model wrote against the blocks it was given.

    A marker naming a block that was never in the prompt is removed from the
    answer. Leaving it would show the reader a reference they cannot follow, and
    a citation that cannot be followed is indistinguishable from a fabricated
    one.

    Returns:
        The answer text, and the cited blocks ordered by block number — the same
        order as the markers a reader sees in the text, so the list under an
        answer can be scanned by number rather than read through.
    """
    by_sequence = {citation.sequence: citation for citation in available}
    used: dict[int, Citation] = {}
    invented: list[int] = []

    def keep(match: re.Match[str]) -> str:
        number = int(match.group(1))
        citation = by_sequence.get(number)
        if citation is None:
            invented.append(number)
            return ""
        used.setdefault(number, citation)
        return match.group(0)

    cleaned = _MARKER.sub(keep, text)
    if invented:
        logger.warning(
            "removed citation markers pointing at blocks that were not in the prompt",
            extra={"markers": sorted(set(invented)), "blocks": len(by_sequence)},
        )
        # Only where a marker was actually dropped: an answer should not be
        # reflowed just because it was checked.
        cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)

    return cleaned.strip(), tuple(used[key] for key in sorted(used))
