"""The contract every ChatProvider adapter must satisfy.

Subclass :class:`ChatProviderContract`, supply a ``provider`` fixture, and the
whole suite runs against that adapter. This is how the port's substitutability is
enforced rather than assumed: when the Anthropic, OpenAI and Voyage adapters land
in M3b, each registers here and any behavioural divergence fails the build.

The base class is deliberately not named ``Test*`` so pytest does not collect it
without a provider.
"""

from __future__ import annotations

from typing import cast

import anthropic
import openai
import pytest
from tests.support.stubs import StubAnthropicClient, StubOpenAIClient

from atlas.domain.chat import (
    ChatRequest,
    Effort,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
)
from atlas.domain.ports.chat import ChatProvider
from atlas.infrastructure.providers.anthropic_provider import AnthropicChatProvider
from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response
from atlas.infrastructure.providers.openai_provider import OpenAIChatProvider
from atlas.infrastructure.providers.resilience import (
    AccountingChatProvider,
    RetryingChatProvider,
)

pytestmark = pytest.mark.contract

_WEATHER_TOOL = ToolDefinition(
    name="get_weather",
    description="Return the current weather. Call this when asked about conditions.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
)


def _request(text: str = "Hello", **kwargs: object) -> ChatRequest:
    return ChatRequest(messages=(Message(role=Role.USER, content=text),), **kwargs)  # type: ignore[arg-type]


class ChatProviderContract:
    """Behaviour required of every chat adapter."""

    @pytest.fixture
    def provider(self) -> ChatProvider:
        raise NotImplementedError

    def test_it_satisfies_the_protocol(self, provider: ChatProvider) -> None:
        assert isinstance(provider, ChatProvider)

    def test_it_reports_identity(self, provider: ChatProvider) -> None:
        assert provider.name
        assert provider.model
        assert isinstance(provider.supports_tools, bool)

    async def test_complete_returns_content_and_a_stop_reason(self, provider: ChatProvider) -> None:
        response = await provider.complete(_request())

        assert isinstance(response.stop_reason, StopReason)
        assert response.model

    async def test_complete_reports_token_usage(self, provider: ChatProvider) -> None:
        """Usage drives cost reporting, so it is part of the contract."""
        response = await provider.complete(_request())

        assert response.usage.input_tokens >= 0
        assert response.usage.output_tokens >= 0
        assert response.usage.total_tokens == response.usage.total_prompt_tokens + (
            response.usage.output_tokens
        )

    async def test_effort_is_accepted_at_every_level(self, provider: ChatProvider) -> None:
        """A provider that ignores the hint must still accept it."""
        for effort in Effort:
            response = await provider.complete(_request(effort=effort))
            assert isinstance(response.stop_reason, StopReason)

    async def test_stream_yields_a_terminal_chunk(self, provider: ChatProvider) -> None:
        chunks = [chunk async for chunk in provider.stream(_request())]

        assert chunks, "a stream must yield at least the terminal chunk"
        assert chunks[-1].is_final
        assert not any(chunk.is_final for chunk in chunks[:-1]), (
            "only the last chunk may carry a stop reason"
        )

    async def test_stream_text_reassembles(self, provider: ChatProvider) -> None:
        chunks = [chunk async for chunk in provider.stream(_request())]

        assembled = "".join(chunk.text_delta for chunk in chunks)
        assert isinstance(assembled, str)


class TestFakeChatProvider(ChatProviderContract):
    """The fake must honour the same contract as a vendor adapter."""

    @pytest.fixture
    def provider(self) -> ChatProvider:
        return FakeChatProvider()

    async def test_it_records_what_the_caller_sent(self) -> None:
        provider = FakeChatProvider()

        await provider.complete(_request("first"))
        await provider.complete(_request("second"))

        assert provider.call_count == 2
        assert provider.requests[0].messages[0].content == "first"
        assert provider.requests[1].messages[0].content == "second"

    async def test_queued_responses_are_returned_in_order(self) -> None:
        provider = FakeChatProvider([fake_response("one"), fake_response("two")])

        assert (await provider.complete(_request())).content == "one"
        assert (await provider.complete(_request())).content == "two"

    async def test_the_last_response_repeats_once_the_queue_drains(self) -> None:
        provider = FakeChatProvider([fake_response("only")])

        await provider.complete(_request())

        assert (await provider.complete(_request())).content == "only"

    async def test_a_queued_exception_is_raised(self) -> None:
        provider = FakeChatProvider([RuntimeError("boom")])

        with pytest.raises(RuntimeError, match="boom"):
            await provider.complete(_request())

    async def test_tool_calls_round_trip(self) -> None:
        call = ToolCall(id="call_1", name="get_weather", arguments={"city": "Paris"})
        provider = FakeChatProvider(
            [fake_response("", stop_reason=StopReason.TOOL_USE, tool_calls=(call,))]
        )

        response = await provider.complete(_request(tools=(_WEATHER_TOOL,)))

        assert response.requires_tool_execution
        assert response.tool_calls == (call,)
        assert response.tool_calls[0].arguments == {"city": "Paris"}

    async def test_a_refusal_is_a_response_not_an_exception(self) -> None:
        """Safety declines arrive as HTTP 200 with a refusal stop reason."""
        provider = FakeChatProvider([fake_response("", stop_reason=StopReason.REFUSAL)])

        response = await provider.complete(_request())

        assert response.is_refusal
        assert not response.requires_tool_execution


class TestAnthropicChatProvider(ChatProviderContract):
    """The Anthropic adapter, driven by a stub SDK client.

    Registering it here is what makes substitutability a build-time fact rather
    than a claim: any behaviour that diverges from the fake fails this suite.
    """

    @pytest.fixture
    def provider(self) -> ChatProvider:
        return AnthropicChatProvider(
            cast("anthropic.AsyncAnthropic", StubAnthropicClient()),
            model="claude-opus-5",
        )


class TestOpenAIChatProvider(ChatProviderContract):
    """The OpenAI adapter, driven by a stub SDK client."""

    @pytest.fixture
    def provider(self) -> ChatProvider:
        return OpenAIChatProvider(cast("openai.AsyncOpenAI", StubOpenAIClient()), model="gpt-4o")


class TestDecoratedProvider(ChatProviderContract):
    """The stack the composition root actually builds.

    Decorators are themselves providers, so the wrapped form must satisfy the
    same contract as the adapter inside it.
    """

    @pytest.fixture
    def provider(self) -> ChatProvider:
        adapter = AnthropicChatProvider(
            cast("anthropic.AsyncAnthropic", StubAnthropicClient()),
            model="claude-opus-5",
        )
        return AccountingChatProvider(RetryingChatProvider(adapter))
