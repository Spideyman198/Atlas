"""Translation tests for the Anthropic adapter.

Both directions matter. A response-only test would miss the half of an adapter
that builds the request, which is where the vendor's hard constraints live.
"""

from __future__ import annotations

from typing import Any, cast

import anthropic
import httpx
import pytest
from tests.support.stubs import (
    AnthropicBlock,
    AnthropicMessage,
    AnthropicUsage,
    StubAnthropicClient,
)

from atlas.domain.chat import (
    ChatRequest,
    Effort,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from atlas.domain.errors import ProviderError, ProviderTimeoutError, RateLimitedError
from atlas.infrastructure.providers.anthropic_provider import AnthropicChatProvider

pytestmark = pytest.mark.unit

_TOOL = ToolDefinition(
    name="overdue_invoices",
    description="List overdue invoices. Call this when asked about outstanding balances.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)


def _provider(client: StubAnthropicClient, **kwargs: Any) -> AnthropicChatProvider:
    return AnthropicChatProvider(cast("anthropic.AsyncAnthropic", client), **kwargs)


def _request(**kwargs: Any) -> ChatRequest:
    kwargs.setdefault("messages", (Message(role=Role.USER, content="hi"),))
    return ChatRequest(**kwargs)


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError("boom", response=response, body=None)


# --- request translation ---------------------------------------------------


async def test_the_system_prompt_is_a_parameter_not_a_message() -> None:
    client = StubAnthropicClient()

    await _provider(client).complete(_request(system="You are Atlas."))

    payload = client.calls[0]
    assert payload["system"] == "You are Atlas."
    assert all(m["role"] != "system" for m in payload["messages"])


async def test_system_role_messages_are_folded_into_the_system_parameter() -> None:
    """A caller may express the prompt either way and get the same request."""
    client = StubAnthropicClient()

    await _provider(client).complete(
        _request(
            system="First.",
            messages=(
                Message(role=Role.SYSTEM, content="Second."),
                Message(role=Role.USER, content="hi"),
            ),
        )
    )

    payload = client.calls[0]
    assert payload["system"] == "First.\n\nSecond."
    assert len(payload["messages"]) == 1


async def test_no_sampling_parameters_are_sent() -> None:
    """Current models reject temperature, top_p and top_k with a 400."""
    client = StubAnthropicClient()

    await _provider(client).complete(_request())

    payload = client.calls[0]
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "top_k" not in payload


async def test_no_thinking_token_budget_is_sent() -> None:
    """`budget_tokens` was removed and now returns a 400."""
    client = StubAnthropicClient()

    await _provider(client).complete(_request())

    assert payload_thinking(client) == {"type": "adaptive"}
    assert "budget_tokens" not in payload_thinking(client)


def payload_thinking(client: StubAnthropicClient) -> dict[str, Any]:
    thinking: dict[str, Any] = client.calls[0]["thinking"]
    return thinking


async def test_adaptive_thinking_can_be_turned_off() -> None:
    client = StubAnthropicClient()

    await _provider(client, adaptive_thinking=False).complete(_request())

    assert "thinking" not in client.calls[0]


@pytest.mark.parametrize(
    ("effort", "expected"),
    [(Effort.LOW, "low"), (Effort.MEDIUM, "medium"), (Effort.HIGH, "high")],
)
async def test_effort_maps_onto_output_config(effort: Effort, expected: str) -> None:
    client = StubAnthropicClient()

    await _provider(client).complete(_request(effort=effort))

    assert client.calls[0]["output_config"] == {"effort": expected}


async def test_tools_are_sent_with_an_input_schema() -> None:
    client = StubAnthropicClient()

    await _provider(client).complete(_request(tools=(_TOOL,)))

    tool = client.calls[0]["tools"][0]
    assert tool["name"] == "overdue_invoices"
    assert tool["input_schema"]["additionalProperties"] is False


async def test_tool_results_become_user_content_blocks() -> None:
    """Anthropic has no tool role — results are blocks inside a user message."""
    client = StubAnthropicClient()

    await _provider(client).complete(
        _request(
            messages=(
                Message(role=Role.USER, content="hi"),
                Message(
                    role=Role.TOOL,
                    tool_results=(ToolResult(call_id="c1", content="42", is_error=False),),
                ),
            )
        )
    )

    result_message = client.calls[0]["messages"][1]
    assert result_message["role"] == "user"
    assert result_message["content"][0]["type"] == "tool_result"
    assert result_message["content"][0]["tool_use_id"] == "c1"


async def test_assistant_tool_calls_become_tool_use_blocks() -> None:
    client = StubAnthropicClient()

    await _provider(client).complete(
        _request(
            messages=(
                Message(role=Role.USER, content="hi"),
                Message(
                    role=Role.ASSISTANT,
                    content="Checking.",
                    tool_calls=(ToolCall(id="c1", name="overdue_invoices", arguments={}),),
                ),
            )
        )
    )

    blocks = client.calls[0]["messages"][1]["content"]
    assert [b["type"] for b in blocks] == ["text", "tool_use"]
    assert blocks[1]["id"] == "c1"


# --- response translation --------------------------------------------------


async def test_text_blocks_are_concatenated() -> None:
    client = StubAnthropicClient(
        AnthropicMessage(
            content=[
                AnthropicBlock(type="text", text="Part one. "),
                AnthropicBlock(type="text", text="Part two."),
            ]
        )
    )

    response = await _provider(client).complete(_request())

    assert response.content == "Part one. Part two."


async def test_thinking_blocks_are_not_part_of_the_answer() -> None:
    client = StubAnthropicClient(
        AnthropicMessage(
            content=[
                AnthropicBlock(type="thinking", text="internal reasoning"),
                AnthropicBlock(type="text", text="The answer."),
            ]
        )
    )

    response = await _provider(client).complete(_request())

    assert response.content == "The answer."
    assert "internal" not in response.content


async def test_tool_use_blocks_become_tool_calls() -> None:
    client = StubAnthropicClient(
        AnthropicMessage(
            content=[
                AnthropicBlock(
                    type="tool_use", id="c1", name="overdue_invoices", input={"days": 30}
                )
            ],
            stop_reason="tool_use",
        )
    )

    response = await _provider(client).complete(_request())

    assert response.requires_tool_execution
    assert response.tool_calls[0].arguments == {"days": 30}


async def test_a_refusal_maps_to_a_stop_reason_not_an_exception() -> None:
    client = StubAnthropicClient(AnthropicMessage(content=[], stop_reason="refusal"))

    response = await _provider(client).complete(_request())

    assert response.is_refusal
    assert response.content == ""


async def test_an_unknown_stop_reason_degrades_to_end_turn() -> None:
    """`pause_turn` means an assumption changed; it is logged, not dropped."""
    client = StubAnthropicClient(
        AnthropicMessage(content=[AnthropicBlock(type="text", text="x")], stop_reason="pause_turn")
    )

    response = await _provider(client).complete(_request())

    assert response.stop_reason is StopReason.END_TURN


async def test_cache_tokens_are_reported_separately() -> None:
    """They are priced differently, so folding them into input would skew cost."""
    client = StubAnthropicClient(
        AnthropicMessage(
            content=[AnthropicBlock(type="text", text="x")],
            usage=AnthropicUsage(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=50,
            ),
        )
    )

    usage = (await _provider(client).complete(_request())).usage

    assert usage.input_tokens == 100
    assert usage.cache_read_input_tokens == 900
    assert usage.cache_write_input_tokens == 50
    assert usage.total_prompt_tokens == 1050


# --- streaming -------------------------------------------------------------


async def test_streaming_yields_text_then_a_terminal_chunk() -> None:
    client = StubAnthropicClient(
        AnthropicMessage(content=[AnthropicBlock(type="text", text="hello world")])
    )

    chunks = [chunk async for chunk in _provider(client).stream(_request())]

    assert "".join(c.text_delta for c in chunks).strip() == "hello world"
    assert chunks[-1].is_final
    assert chunks[-1].usage is not None


# --- error translation -----------------------------------------------------


async def test_a_timeout_becomes_a_provider_timeout() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = StubAnthropicClient(anthropic.APITimeoutError(request=request))

    with pytest.raises(ProviderTimeoutError):
        await _provider(client).complete(_request())


async def test_a_connection_error_is_marked_retryable() -> None:
    """It never reached the provider, so the request cannot be half-applied."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = StubAnthropicClient(anthropic.APIConnectionError(request=request))

    with pytest.raises(ProviderError) as caught:
        await _provider(client).complete(_request())

    assert caught.value.context["retryable"] is True


async def test_a_server_error_is_retryable_and_a_client_error_is_not() -> None:
    server = StubAnthropicClient(_status_error(503))
    with pytest.raises(ProviderError) as server_error:
        await _provider(server).complete(_request())
    assert server_error.value.context["retryable"] is True

    client_side = StubAnthropicClient(_status_error(400))
    with pytest.raises(ProviderError) as client_error:
        await _provider(client_side).complete(_request())
    assert client_error.value.context["retryable"] is False


async def test_rate_limiting_carries_the_providers_retry_hint() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request, headers={"retry-after": "12"})
    client = StubAnthropicClient(
        anthropic.RateLimitError("slow down", response=response, body=None)
    )

    with pytest.raises(RateLimitedError) as caught:
        await _provider(client).complete(_request())

    assert caught.value.retry_after_seconds == 12.0
