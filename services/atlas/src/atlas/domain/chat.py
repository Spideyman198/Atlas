"""Chat value objects.

The vocabulary the application layer speaks. Adapters translate vendor payloads
into these types and back; nothing above ``infrastructure`` sees an SDK object.

Two deliberate omissions, both driven by where the vendors actually are:

**No sampling parameters.** ``temperature``, ``top_p`` and ``top_k`` are rejected
with a 400 by current Anthropic models. Modelling them would mean a field that
silently fails on one vendor and works on another — the leaky abstraction the
port exists to prevent. Determinism and variation are prompt concerns; where a
knob is genuinely needed, :class:`Effort` covers the case that generalises.

**No assistant prefill.** Also rejected by current models. Response shaping is
done with tool schemas or explicit instructions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from atlas.domain.usage import TokenUsage


class Role(StrEnum):
    """Who produced a message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    """Why the provider stopped generating.

    ``REFUSAL`` is not an error: current models return HTTP 200 with this stop
    reason when a safety classifier declines the request. Callers must check the
    stop reason before reading content, which is why it is part of the port
    rather than an exception.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    TOOL_USE = "tool_use"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"


class Effort(StrEnum):
    """How much reasoning to spend on a request.

    Generalises Anthropic's ``output_config.effort`` and OpenAI's reasoning
    effort. Adapters map these onto whatever their vendor supports and ignore the
    hint when it supports none.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool the model may call.

    Attributes:
        name: Stable identifier the model emits when calling the tool.
        description: What the tool does *and when to call it*. The trigger
            condition matters as much as the capability — current models reach
            for tools conservatively, and a description that only states what a
            tool does measurably under-triggers it.
        parameters: JSON Schema for the arguments. Must be an object schema with
            ``additionalProperties: false`` for strict validation.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to invoke a tool."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of executing a tool call.

    A failed tool returns a result with ``is_error`` set rather than being
    dropped: omitting it leaves a dangling call the provider will reject.
    """

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in a conversation."""

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A completion request, expressed in domain terms.

    Attributes:
        messages: Conversation turns in order.
        system: System prompt. Kept separate from ``messages`` because vendors
            disagree on where it belongs, and the adapter is the right place to
            resolve that.
        tools: Tools available for this request.
        max_output_tokens: Hard ceiling on generated tokens. On models where
            reasoning is billed as output, this caps reasoning *and* answer
            together — size it with headroom.
        effort: Reasoning-depth hint.
        stream_hint: Whether the caller intends to stream. Adapters use it to
            choose a transport; it does not change the response type.
    """

    messages: tuple[Message, ...]
    system: str | None = None
    tools: tuple[ToolDefinition, ...] = ()
    max_output_tokens: int = 4096
    effort: Effort = Effort.MEDIUM
    stream_hint: bool = False

    def with_messages(self, messages: Sequence[Message]) -> ChatRequest:
        """Return a copy carrying a different conversation.

        Used by the tool loop, which re-sends a growing message list against an
        otherwise identical request.
        """
        return replace(self, messages=tuple(messages))


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """A completed generation.

    Cost is deliberately absent: pricing is vendor knowledge and lives in
    ``infrastructure.providers.pricing``, computed from ``model`` and ``usage``.
    """

    content: str
    stop_reason: StopReason
    model: str
    usage: TokenUsage
    tool_calls: tuple[ToolCall, ...] = ()
    latency_ms: int = 0

    @property
    def is_refusal(self) -> bool:
        """True when a safety classifier declined the request."""
        return self.stop_reason is StopReason.REFUSAL

    @property
    def requires_tool_execution(self) -> bool:
        """True when the caller must execute tools and continue the loop."""
        return self.stop_reason is StopReason.TOOL_USE and bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """One increment of a streamed response.

    Exactly one of ``text_delta`` or ``usage`` is meaningful on a given chunk:
    text arrives during generation, usage only on the final chunk.
    """

    text_delta: str = ""
    stop_reason: StopReason | None = None
    usage: TokenUsage | None = None
    tool_calls: tuple[ToolCall, ...] = field(default=())

    @property
    def is_final(self) -> bool:
        """True for the terminal chunk of a stream."""
        return self.stop_reason is not None
