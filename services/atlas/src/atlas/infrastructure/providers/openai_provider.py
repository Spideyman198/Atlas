"""OpenAI chat and embedding adapters.

Uses the Chat Completions API rather than the Responses API: it is the stable,
widely deployed surface, and Azure OpenAI exposes it through the same client with
a base-URL override — which is the compatibility ADR-0005 is buying.

Tool arguments arrive as a JSON *string* here, unlike Anthropic which returns a
parsed object. Normalising that is exactly the kind of divergence the port exists
to absorb.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

import openai

from atlas.domain.chat import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
)
from atlas.domain.embedding import EmbeddingPurpose, EmbeddingResult, Vector
from atlas.domain.errors import (
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    ValidationError,
)
from atlas.domain.usage import TokenUsage

logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL: Final = "gpt-4o"
DEFAULT_EMBEDDING_MODEL: Final = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS: Final = 1536

#: At or above this status the fault is the provider's, so another attempt may
#: succeed. Below it the request was rejected on its merits and will be again.
_SERVER_ERROR: Final = 500

_FINISH_REASONS: Final[dict[str, StopReason]] = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "content_filter": StopReason.REFUSAL,
}


class OpenAIChatProvider:
    """A :class:`~atlas.domain.ports.chat.ChatProvider` backed by the OpenAI SDK.

    Args:
        client: An ``AsyncOpenAI`` instance. For Azure, construct it with the
            deployment's base URL; nothing else in this class changes.
        model: Model identifier.
    """

    def __init__(self, client: openai.AsyncOpenAI, *, model: str = DEFAULT_CHAT_MODEL) -> None:
        self._client = client
        self._model = model

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tools(self) -> bool:
        return True

    async def complete(self, request: ChatRequest) -> ChatResponse:
        try:
            completion = await self._client.chat.completions.create(**self._payload(request))
        except Exception as exc:
            raise _translate(exc) from exc

        return self._to_response(completion)

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        payload = self._payload(request)
        payload["stream"] = True
        # Usage is omitted from streamed responses unless explicitly requested,
        # and the accounting decorator needs it on every call.
        payload["stream_options"] = {"include_usage": True}

        finish_reason: str | None = None
        usage = TokenUsage()
        calls = _StreamedToolCalls()

        try:
            stream = await self._client.chat.completions.create(**payload)
            async for event in stream:
                if getattr(event, "usage", None):
                    usage = _usage(event.usage)
                for choice in getattr(event, "choices", []) or []:
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta else None
                    if text:
                        yield ChatChunk(text_delta=text)
                    if delta is not None:
                        calls.collect(getattr(delta, "tool_calls", None) or [])
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
        except Exception as exc:
            raise _translate(exc) from exc

        # Mirrors the Anthropic adapter: the terminal chunk carries the stop
        # reason, the usage and the tool calls together. Streaming previously
        # dropped the calls entirely, so any question the model wanted to answer
        # with a tool ended the loop in `synthesis` on its first round and
        # returned an empty answer.
        yield ChatChunk(
            stop_reason=_stop_reason(finish_reason),
            usage=usage,
            tool_calls=calls.finish(),
        )

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": request.max_output_tokens,
            "messages": _messages(request),
        }
        if request.tools:
            payload["tools"] = [_tool(tool) for tool in request.tools]
        return payload

    def _to_response(self, completion: Any) -> ChatResponse:
        choice = completion.choices[0]
        message = choice.message

        tool_calls = tuple(
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=_arguments(call.function.arguments),
            )
            for call in (getattr(message, "tool_calls", None) or [])
        )

        return ChatResponse(
            content=getattr(message, "content", None) or "",
            stop_reason=_stop_reason(getattr(choice, "finish_reason", None)),
            model=getattr(completion, "model", self._model),
            usage=_usage(getattr(completion, "usage", None)),
            tool_calls=tool_calls,
        )


class OpenAIEmbeddingProvider:
    """An :class:`~atlas.domain.ports.embedding.EmbeddingProvider` over OpenAI.

    OpenAI draws no distinction between embedding a document and embedding a
    query, so ``purpose`` is accepted and ignored — the port keeps the parameter
    because Voyage does distinguish them.

    ``dimensions`` is sent explicitly rather than relied on as a default: the
    value is baked into a PostgreSQL column type, so it must be the value we
    think it is.
    """

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
        max_batch_size: int = 96,
    ) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._max_batch_size = max_batch_size

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    async def embed(
        self,
        texts: Sequence[str],
        purpose: EmbeddingPurpose = EmbeddingPurpose.DOCUMENT,
    ) -> EmbeddingResult:
        # `purpose` is unused: OpenAI embeds documents and queries identically.
        # The parameter stays because the port requires it and Voyage honours it.
        _check_batch(texts, self._max_batch_size)

        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=list(texts),
                dimensions=self._dimensions,
            )
        except Exception as exc:
            raise _translate(exc) from exc

        return _embedding_result(response, self._model, self._dimensions)


def _messages(request: ChatRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})

    for message in request.messages:
        if message.role is Role.TOOL or message.tool_results:
            # One `tool` message per result, each keyed to its call.
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
                for result in message.tool_results
            )
            continue

        if message.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(dict(call.arguments)),
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            continue

        messages.append({"role": message.role.value, "content": message.content})

    return messages


def _tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }


class _StreamedToolCalls:
    """Reassembles tool calls that arrive in pieces across streamed deltas.

    OpenAI splits one call over many chunks — the id and name in the first, the
    argument JSON a few characters at a time after it — and identifies the parts
    by ``index``. Google's compatibility endpoint sends each call whole and omits
    ``index`` altogether, so position within the delta is the only key available.
    Both are handled: a fragment with an index joins the slot of that index, one
    without starts a slot of its own, and slots are emitted in first-seen order.
    """

    def __init__(self) -> None:
        self._slots: dict[int, dict[str, str]] = {}
        self._order: list[int] = []
        self._anonymous = 0

    def collect(self, fragments: Sequence[Any]) -> None:
        for fragment in fragments:
            slot = self._slot_for(fragment)
            if getattr(fragment, "id", None):
                slot["id"] = fragment.id
            function = getattr(fragment, "function", None)
            if function is None:
                continue
            if getattr(function, "name", None):
                slot["name"] = function.name
            # Concatenated, never replaced: this is the field OpenAI fragments.
            if getattr(function, "arguments", None):
                slot["arguments"] += function.arguments

    def _slot_for(self, fragment: Any) -> dict[str, str]:
        key = getattr(fragment, "index", None)
        if key is None:
            # Negative keys cannot collide with a real index, and `_order` keeps
            # the emission order right regardless of their sign.
            self._anonymous += 1
            key = -self._anonymous
        if key not in self._slots:
            self._slots[key] = {"id": "", "name": "", "arguments": ""}
            self._order.append(key)
        return self._slots[key]

    def finish(self) -> tuple[ToolCall, ...]:
        return tuple(
            ToolCall(
                id=self._slots[key]["id"],
                name=self._slots[key]["name"],
                arguments=_arguments(self._slots[key]["arguments"]),
            )
            for key in self._order
            # A slot with no name is a fragment the provider never completed;
            # forwarding it would call a tool that does not exist.
            if self._slots[key]["name"]
        )


def _arguments(raw: str | None) -> dict[str, Any]:
    """Parse the JSON string OpenAI returns for tool arguments.

    A malformed payload becomes an empty argument set rather than an exception:
    the model called the tool, and the caller's own validation is better placed
    to reject the arguments than a parse error is.
    """
    if not raw:
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("openai returned unparseable tool arguments")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stop_reason(raw: str | None) -> StopReason:
    if raw is None:
        return StopReason.END_TURN
    mapped = _FINISH_REASONS.get(raw)
    if mapped is None:
        logger.warning("unmapped openai finish reason", extra={"finish_reason": raw})
        return StopReason.END_TURN
    return mapped


def _usage(raw: Any) -> TokenUsage:
    if raw is None:
        return TokenUsage()

    cached = 0
    details = getattr(raw, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0

    prompt = getattr(raw, "prompt_tokens", 0) or 0
    return TokenUsage(
        # `prompt_tokens` includes cached tokens, so subtract to keep the two
        # fields disjoint and the cost calculation correct.
        input_tokens=max(prompt - cached, 0),
        output_tokens=getattr(raw, "completion_tokens", 0) or 0,
        cache_read_input_tokens=cached,
    )


def _check_batch(texts: Sequence[str], max_batch_size: int) -> None:
    if not texts:
        msg = "texts must not be empty"
        raise ValidationError(msg)
    if len(texts) > max_batch_size:
        msg = f"batch of {len(texts)} exceeds max_batch_size {max_batch_size}"
        raise ValidationError(msg, context={"batch_size": len(texts)})


def _embedding_result(response: Any, model: str, dimensions: int) -> EmbeddingResult:
    """Build a result, restoring input order and verifying the vector width."""
    # OpenAI documents `index` on every item and does not promise to return them
    # in order, so the input order is restored from it. Google's compatibility
    # endpoint leaves the field null, which made this sort raise "'<' not
    # supported between instances of 'int' and 'NoneType'" partway through a
    # sync. When any index is missing there is nothing to sort by, and every
    # such endpoint returns items in input order, so the response order stands.
    items = list(response.data)
    if all(getattr(item, "index", None) is not None for item in items):
        items.sort(key=lambda item: item.index)
    vectors: list[Vector] = []

    for item in items:
        vector = tuple(float(value) for value in item.embedding)
        if len(vector) != dimensions:
            msg = f"provider returned {len(vector)}-d vector, expected {dimensions}"
            raise ProviderError(msg, provider="openai", context={"model": model})
        vectors.append(vector)

    usage = getattr(response, "usage", None)
    return EmbeddingResult(
        vectors=tuple(vectors),
        model=getattr(response, "model", model),
        usage=TokenUsage(input_tokens=getattr(usage, "prompt_tokens", 0) or 0),
    )


def _translate(exc: Exception) -> Exception:
    """Map an SDK exception onto the error taxonomy.

    Same ordering constraint as the Anthropic adapter: the SDK's exception
    hierarchy puts the specific cases underneath the general ones.
    """
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeoutError(str(exc), provider="openai")

    if isinstance(exc, openai.RateLimitError):
        return RateLimitedError(str(exc), provider="openai", retry_after_seconds=_retry_after(exc))

    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(str(exc), provider="openai", context={"retryable": True})

    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        return ProviderError(
            str(exc),
            provider="openai",
            context={"status_code": status, "retryable": status >= _SERVER_ERROR},
        )

    if isinstance(exc, ProviderError):
        return exc

    return ProviderError(str(exc), provider="openai")


def _retry_after(exc: openai.RateLimitError) -> float | None:
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


__all__ = [
    "DEFAULT_CHAT_MODEL",
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_EMBEDDING_MODEL",
    "OpenAIChatProvider",
    "OpenAIEmbeddingProvider",
]
