"""Translation tests for the OpenAI adapters."""

from __future__ import annotations

from typing import Any, cast

import openai
import pytest
from tests.support.stubs import (
    OpenAIChoice,
    OpenAICompletion,
    OpenAIEmbeddingItem,
    OpenAIEmbeddingResponse,
    OpenAIFunction,
    OpenAIMessage,
    OpenAIPromptDetails,
    OpenAIToolCall,
    OpenAIUsage,
    StubOpenAIClient,
)

from atlas.domain.chat import (
    ChatRequest,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from atlas.domain.errors import ProviderError, ValidationError
from atlas.infrastructure.providers.openai_provider import (
    OpenAIChatProvider,
    OpenAIEmbeddingProvider,
)

pytestmark = pytest.mark.unit

_TOOL = ToolDefinition(
    name="stock_levels",
    description="Return stock. Call this when asked what is in inventory.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)


def _chat(client: StubOpenAIClient, **kwargs: Any) -> OpenAIChatProvider:
    return OpenAIChatProvider(cast("openai.AsyncOpenAI", client), **kwargs)


def _embedder(client: StubOpenAIClient, **kwargs: Any) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(cast("openai.AsyncOpenAI", client), **kwargs)


def _request(**kwargs: Any) -> ChatRequest:
    kwargs.setdefault("messages", (Message(role=Role.USER, content="hi"),))
    return ChatRequest(**kwargs)


# --- chat request translation ---------------------------------------------


async def test_the_system_prompt_is_the_first_message() -> None:
    """The opposite of Anthropic, which takes it as a parameter."""
    client = StubOpenAIClient()

    await _chat(client).complete(_request(system="You are Atlas."))

    messages = client.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "You are Atlas."}


async def test_tools_are_wrapped_in_a_function_envelope() -> None:
    client = StubOpenAIClient()

    await _chat(client).complete(_request(tools=(_TOOL,)))

    tool = client.calls[0]["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "stock_levels"
    assert tool["function"]["parameters"]["additionalProperties"] is False


async def test_each_tool_result_becomes_its_own_tool_message() -> None:
    client = StubOpenAIClient()

    await _chat(client).complete(
        _request(
            messages=(
                Message(role=Role.USER, content="hi"),
                Message(
                    role=Role.TOOL,
                    tool_results=(
                        ToolResult(call_id="c1", content="a"),
                        ToolResult(call_id="c2", content="b"),
                    ),
                ),
            )
        )
    )

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "tool", "tool"]
    assert messages[1]["tool_call_id"] == "c1"


async def test_assistant_tool_call_arguments_are_serialised_to_json() -> None:
    client = StubOpenAIClient()

    await _chat(client).complete(
        _request(
            messages=(
                Message(role=Role.USER, content="hi"),
                Message(
                    role=Role.ASSISTANT,
                    tool_calls=(ToolCall(id="c1", name="stock_levels", arguments={"sku": "A"}),),
                ),
            )
        )
    )

    call = client.calls[0]["messages"][1]["tool_calls"][0]
    assert call["function"]["arguments"] == '{"sku": "A"}'


# --- chat response translation --------------------------------------------


async def test_tool_call_arguments_are_parsed_from_json() -> None:
    """OpenAI returns a JSON string where Anthropic returns an object."""
    client = StubOpenAIClient(
        OpenAICompletion(
            choices=[
                OpenAIChoice(
                    message=OpenAIMessage(
                        tool_calls=[
                            OpenAIToolCall(
                                id="c1",
                                function=OpenAIFunction(
                                    name="stock_levels", arguments='{"sku": "A"}'
                                ),
                            )
                        ]
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
    )

    response = await _chat(client).complete(_request())

    assert response.requires_tool_execution
    assert response.tool_calls[0].arguments == {"sku": "A"}


async def test_unparseable_tool_arguments_degrade_to_an_empty_mapping() -> None:
    """The caller's validation rejects bad arguments better than a parse error."""
    client = StubOpenAIClient(
        OpenAICompletion(
            choices=[
                OpenAIChoice(
                    message=OpenAIMessage(
                        tool_calls=[
                            OpenAIToolCall(
                                id="c1",
                                function=OpenAIFunction(name="stock_levels", arguments="{not json"),
                            )
                        ]
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
    )

    response = await _chat(client).complete(_request())

    assert response.tool_calls[0].arguments == {}


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", StopReason.END_TURN),
        ("length", StopReason.MAX_TOKENS),
        ("tool_calls", StopReason.TOOL_USE),
        ("content_filter", StopReason.REFUSAL),
        ("something_new", StopReason.END_TURN),
    ],
)
async def test_finish_reasons_map_onto_the_ports_vocabulary(
    finish_reason: str, expected: StopReason
) -> None:
    client = StubOpenAIClient(
        OpenAICompletion(
            choices=[OpenAIChoice(message=OpenAIMessage(content="x"), finish_reason=finish_reason)]
        )
    )

    response = await _chat(client).complete(_request())

    assert response.stop_reason is expected


async def test_cached_tokens_are_subtracted_from_prompt_tokens() -> None:
    """OpenAI's `prompt_tokens` includes cached ones; ours must be disjoint."""
    client = StubOpenAIClient(
        OpenAICompletion(
            choices=[OpenAIChoice(message=OpenAIMessage(content="x"))],
            usage=OpenAIUsage(
                prompt_tokens=1000,
                completion_tokens=20,
                prompt_tokens_details=OpenAIPromptDetails(cached_tokens=900),
            ),
        )
    )

    usage = (await _chat(client).complete(_request())).usage

    assert usage.input_tokens == 100
    assert usage.cache_read_input_tokens == 900
    assert usage.total_prompt_tokens == 1000


async def test_streaming_requests_usage_explicitly() -> None:
    """Streamed responses omit usage unless asked, and accounting needs it."""
    client = StubOpenAIClient()

    chunks = [chunk async for chunk in _chat(client).stream(_request())]

    assert client.calls[0]["stream_options"] == {"include_usage": True}
    assert chunks[-1].is_final
    assert chunks[-1].usage is not None


def _tool_call_completion() -> OpenAICompletion:
    return OpenAICompletion(
        choices=[
            OpenAIChoice(
                message=OpenAIMessage(
                    tool_calls=[
                        OpenAIToolCall(
                            id="call_1",
                            function=OpenAIFunction(
                                name="aggregate", arguments='{"model": "sale.order"}'
                            ),
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        ]
    )


async def test_streaming_reassembles_a_fragmented_tool_call() -> None:
    """The tool loop reads calls off the stream; dropping them empties the answer."""
    client = StubOpenAIClient(completion=_tool_call_completion())

    chunks = [chunk async for chunk in _chat(client).stream(_request())]

    final = chunks[-1]
    assert final.stop_reason is StopReason.TOOL_USE
    assert final.tool_calls == (
        ToolCall(id="call_1", name="aggregate", arguments={"model": "sale.order"}),
    )


async def test_streaming_reassembles_a_tool_call_that_carries_no_index() -> None:
    """Google's compatibility endpoint sends each call whole and omits `index`."""
    client = StubOpenAIClient(completion=_tool_call_completion(), fragment_tool_calls=False)

    chunks = [chunk async for chunk in _chat(client).stream(_request())]

    assert chunks[-1].tool_calls == (
        ToolCall(id="call_1", name="aggregate", arguments={"model": "sale.order"}),
    )


async def test_streaming_keeps_two_tool_calls_in_the_order_the_model_asked() -> None:
    completion = OpenAICompletion(
        choices=[
            OpenAIChoice(
                message=OpenAIMessage(
                    tool_calls=[
                        OpenAIToolCall(
                            id="call_1",
                            function=OpenAIFunction(name="first", arguments='{"a": 1}'),
                        ),
                        OpenAIToolCall(
                            id="call_2",
                            function=OpenAIFunction(name="second", arguments='{"b": 2}'),
                        ),
                    ]
                ),
                finish_reason="tool_calls",
            )
        ]
    )
    client = StubOpenAIClient(completion=completion)

    chunks = [chunk async for chunk in _chat(client).stream(_request())]

    assert [call.name for call in chunks[-1].tool_calls] == ["first", "second"]
    assert [call.arguments for call in chunks[-1].tool_calls] == [{"a": 1}, {"b": 2}]


# --- embeddings ------------------------------------------------------------


async def test_dimensions_are_sent_explicitly() -> None:
    """The value is baked into a column type, so it must be the one we intend."""
    client = StubOpenAIClient(
        embeddings=OpenAIEmbeddingResponse(data=[OpenAIEmbeddingItem(embedding=[0.1] * 64)])
    )

    await _embedder(client, dimensions=64).embed(["text"])

    assert client.embeddings.calls[0]["dimensions"] == 64


async def test_out_of_order_results_are_restored_to_input_order() -> None:
    client = StubOpenAIClient(
        embeddings=OpenAIEmbeddingResponse(
            data=[
                OpenAIEmbeddingItem(embedding=[0.2] * 4, index=1),
                OpenAIEmbeddingItem(embedding=[0.1] * 4, index=0),
            ]
        )
    )

    result = await _embedder(client, dimensions=4).embed(["first", "second"])

    assert result.vectors[0][0] == pytest.approx(0.1)
    assert result.vectors[1][0] == pytest.approx(0.2)


async def test_results_without_an_index_keep_the_response_order() -> None:
    """Google's compatibility endpoint returns a null index; sorting on it raised."""
    client = StubOpenAIClient(
        embeddings=OpenAIEmbeddingResponse(
            data=[
                OpenAIEmbeddingItem(embedding=[0.1] * 4, index=None),
                OpenAIEmbeddingItem(embedding=[0.2] * 4, index=None),
            ]
        )
    )

    result = await _embedder(client, dimensions=4).embed(["first", "second"])

    assert result.vectors[0][0] == pytest.approx(0.1)
    assert result.vectors[1][0] == pytest.approx(0.2)


async def test_an_unexpected_vector_width_is_an_error_not_a_corrupt_write() -> None:
    client = StubOpenAIClient(
        embeddings=OpenAIEmbeddingResponse(data=[OpenAIEmbeddingItem(embedding=[0.1] * 128)])
    )

    with pytest.raises(ProviderError, match="expected 64"):
        await _embedder(client, dimensions=64).embed(["text"])


async def test_batch_limits_are_enforced() -> None:
    client = StubOpenAIClient()
    embedder = _embedder(client, dimensions=1536, max_batch_size=2)

    with pytest.raises(ValidationError):
        await embedder.embed([])
    with pytest.raises(ValidationError):
        await embedder.embed(["a", "b", "c"])
