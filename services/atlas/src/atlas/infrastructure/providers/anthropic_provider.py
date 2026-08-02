"""Anthropic chat adapter.

Translation only. Retry, cost accounting and telemetry are decorators applied by
the composition root, so this module is a payload translator and an error
translator and nothing else.

Three vendor facts shape it:

- **Sampling parameters are rejected.** ``temperature``, ``top_p`` and ``top_k``
  return a 400 on current models. The port does not model them, so there is
  nothing to drop here — but do not add them back.
- **Reasoning depth is ``output_config.effort``**, not a token budget.
  ``thinking.budget_tokens`` was removed and returns a 400.
- **A refusal is a successful call.** Safety classifiers return HTTP 200 with
  ``stop_reason: "refusal"`` and empty content. It maps to a stop reason, never
  an exception.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Final

import anthropic

from atlas.domain.chat import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    Effort,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
)
from atlas.domain.errors import ProviderError, ProviderTimeoutError, RateLimitedError
from atlas.domain.usage import TokenUsage

logger = logging.getLogger(__name__)

DEFAULT_MODEL: Final = "claude-opus-5"

#: The port's three levels onto Anthropic's five. `xhigh` and `max` are reachable
#: by configuring the model rather than per request — the port deliberately does
#: not expose a knob whose top settings most callers should not be reaching for.
_EFFORT: Final[dict[Effort, str]] = {
    Effort.LOW: "low",
    Effort.MEDIUM: "medium",
    Effort.HIGH: "high",
}

#: At or above this status the fault is the provider's, so another attempt may
#: succeed. Below it the request was rejected on its merits and will be again.
_SERVER_ERROR: Final = 500

_STOP_REASONS: Final[dict[str, StopReason]] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "tool_use": StopReason.TOOL_USE,
    "refusal": StopReason.REFUSAL,
}


class AnthropicChatProvider:
    """A :class:`~atlas.domain.ports.chat.ChatProvider` backed by the Anthropic SDK.

    Args:
        client: An ``AsyncAnthropic`` instance. Injected rather than constructed
            so translation is testable without a network or an API key.
        model: Model identifier, for example ``claude-opus-5``.
        adaptive_thinking: Send ``thinking={"type": "adaptive"}``. Thinking is on
            by default on current models; setting it explicitly makes the intent
            visible in the request. Note that ``max_output_tokens`` caps thinking
            *and* the answer together, so a tight budget truncates mid-response.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str = DEFAULT_MODEL,
        adaptive_thinking: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._adaptive_thinking = adaptive_thinking

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tools(self) -> bool:
        return True

    async def complete(self, request: ChatRequest) -> ChatResponse:
        payload = self._payload(request)
        try:
            message = await self._client.messages.create(**payload)
        except Exception as exc:
            raise _translate(exc) from exc

        return self._to_response(message)

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        payload = self._payload(request)
        try:
            async with self._client.messages.stream(**payload) as stream:
                async for text in stream.text_stream:
                    yield ChatChunk(text_delta=text)
                final = await stream.get_final_message()
        except Exception as exc:
            raise _translate(exc) from exc

        response = self._to_response(final)
        yield ChatChunk(
            stop_reason=response.stop_reason,
            usage=response.usage,
            tool_calls=response.tool_calls,
        )

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        system, messages = _split_system(request)

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
            "output_config": {"effort": _EFFORT[request.effort]},
        }
        if system:
            payload["system"] = system
        if self._adaptive_thinking:
            payload["thinking"] = {"type": "adaptive"}
        if request.tools:
            payload["tools"] = [_tool(tool) for tool in request.tools]
        return payload

    def _to_response(self, message: Any) -> ChatResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in message.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )
            # `thinking` blocks are intentionally dropped: the raw chain of
            # thought is never returned, and a summary is not part of the answer.

        return ChatResponse(
            content="".join(text_parts),
            stop_reason=_stop_reason(getattr(message, "stop_reason", None)),
            model=getattr(message, "model", self._model),
            usage=_usage(getattr(message, "usage", None)),
            tool_calls=tuple(tool_calls),
        )


def _split_system(request: ChatRequest) -> tuple[str, list[dict[str, Any]]]:
    """Separate system text from the conversation.

    Anthropic takes the system prompt as its own parameter rather than a message.
    Any ``SYSTEM``-role entry in ``messages`` is folded into it, in order, so a
    caller that expresses the prompt either way gets the same request.
    """
    system_parts = [request.system] if request.system else []
    messages: list[dict[str, Any]] = []

    for message in request.messages:
        if message.role is Role.SYSTEM:
            if message.content:
                system_parts.append(message.content)
            continue
        messages.append(_message(message))

    return "\n\n".join(system_parts), messages


def _message(message: Message) -> dict[str, Any]:
    """Render one domain message as an Anthropic message."""
    if message.role is Role.TOOL or message.tool_results:
        # Tool results are user-role content blocks, not a distinct role.
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in message.tool_results
            ],
        }

    if message.tool_calls:
        content: list[dict[str, Any]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        content.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": dict(call.arguments),
            }
            for call in message.tool_calls
        )
        return {"role": "assistant", "content": content}

    return {"role": message.role.value, "content": message.content}


def _tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.parameters),
    }


def _stop_reason(raw: str | None) -> StopReason:
    """Map an Anthropic stop reason onto the port's vocabulary.

    ``pause_turn`` means a server-side tool loop paused. Atlas declares no server
    tools, so seeing it means an assumption changed — it is logged and treated as
    a completed turn rather than silently discarded.
    """
    if raw is None:
        return StopReason.END_TURN
    mapped = _STOP_REASONS.get(raw)
    if mapped is None:
        logger.warning("unmapped anthropic stop reason", extra={"stop_reason": raw})
        return StopReason.END_TURN
    return mapped


def _usage(raw: Any) -> TokenUsage:
    if raw is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_input_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
    )


def _translate(exc: Exception) -> Exception:
    """Map an SDK exception onto the error taxonomy.

    Ordering matters: ``APITimeoutError`` subclasses ``APIConnectionError`` and
    ``RateLimitError`` subclasses ``APIStatusError``, so the specific cases are
    tested first.
    """
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError(str(exc), provider="anthropic")

    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitedError(
            str(exc),
            provider="anthropic",
            retry_after_seconds=_retry_after(exc),
        )

    if isinstance(exc, anthropic.APIConnectionError):
        # A connection failure never reached the provider, so it is always safe
        # to retry — the request cannot have been partially applied.
        return ProviderError(str(exc), provider="anthropic", context={"retryable": True})

    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        return ProviderError(
            str(exc),
            provider="anthropic",
            context={"status_code": status, "retryable": status >= _SERVER_ERROR},
        )

    return ProviderError(str(exc), provider="anthropic")


def _retry_after(exc: anthropic.RateLimitError) -> float | None:
    """Read the provider's own retry hint, when it supplies one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


__all__ = ["DEFAULT_MODEL", "AnthropicChatProvider"]
