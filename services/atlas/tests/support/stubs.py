"""Stub SDK clients shaped like the real vendor responses.

Adapters read attributes off SDK objects, so a stub only has to expose the same
attribute names. Every stub records the payload it was called with, which is how
request translation is asserted — the half of an adapter that a response-shaped
assertion would otherwise miss.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


@dataclass
class AnthropicBlock:
    """A content block: ``text``, ``tool_use`` or ``thinking``."""

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnthropicUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class AnthropicMessage:
    content: list[AnthropicBlock]
    stop_reason: str = "end_turn"
    model: str = "claude-opus-5"
    usage: AnthropicUsage = field(default_factory=AnthropicUsage)


class _AnthropicStream:
    def __init__(self, message: AnthropicMessage) -> None:
        self._message = message

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    @property
    async def text_stream(self) -> AsyncIterator[str]:  # pragma: no cover - see __aiter__
        raise NotImplementedError

    async def get_final_message(self) -> AnthropicMessage:
        return self._message


class _AnthropicStreamWithText(_AnthropicStream):
    """Splits the scripted text into deltas, as the real stream does."""

    @property
    def text_stream(self) -> AsyncIterator[str]:  # type: ignore[override]
        async def generate() -> AsyncIterator[str]:
            for block in self._message.content:
                if block.type == "text":
                    for word in block.text.split(" "):
                        if word:
                            yield word + " "

        return generate()


class _AnthropicMessages:
    def __init__(self, outcome: AnthropicMessage | Exception) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> AnthropicMessage:
        self.calls.append(payload)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    def stream(self, **payload: Any) -> _AnthropicStreamWithText:
        self.calls.append(payload)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _AnthropicStreamWithText(self._outcome)


class StubAnthropicClient:
    """Stands in for ``anthropic.AsyncAnthropic``."""

    def __init__(self, outcome: AnthropicMessage | Exception | None = None) -> None:
        resolved = (
            outcome
            if outcome is not None
            else AnthropicMessage(
                content=[AnthropicBlock(type="text", text="Hello from Anthropic.")]
            )
        )
        self.messages = _AnthropicMessages(resolved)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------


@dataclass
class OpenAIFunction:
    name: str
    arguments: str


@dataclass
class OpenAIToolCall:
    id: str
    function: OpenAIFunction
    type: str = "function"


@dataclass
class OpenAIMessage:
    content: str | None = None
    tool_calls: list[OpenAIToolCall] = field(default_factory=list)


@dataclass
class OpenAIChoice:
    message: OpenAIMessage
    finish_reason: str = "stop"


@dataclass
class OpenAIPromptDetails:
    cached_tokens: int = 0


@dataclass
class OpenAIUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    prompt_tokens_details: OpenAIPromptDetails = field(default_factory=OpenAIPromptDetails)


@dataclass
class OpenAICompletion:
    choices: list[OpenAIChoice]
    model: str = "gpt-4o"
    usage: OpenAIUsage = field(default_factory=OpenAIUsage)


@dataclass
class OpenAIEmbeddingItem:
    embedding: list[float]
    index: int = 0


@dataclass
class OpenAIEmbeddingResponse:
    data: list[OpenAIEmbeddingItem]
    model: str = "text-embedding-3-small"
    usage: OpenAIUsage = field(default_factory=OpenAIUsage)


class _OpenAICompletions:
    def __init__(self, outcome: OpenAICompletion | Exception) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> Any:
        self.calls.append(payload)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if payload.get("stream"):
            return _openai_stream(self._outcome)
        return self._outcome


def _openai_stream(completion: OpenAICompletion) -> AsyncIterator[Any]:
    """Replay a completion as streamed events, usage last."""

    @dataclass
    class _Delta:
        content: str | None = None

    @dataclass
    class _Choice:
        delta: _Delta
        finish_reason: str | None = None

    @dataclass
    class _Event:
        choices: list[_Choice] = field(default_factory=list)
        usage: OpenAIUsage | None = None

    async def generate() -> AsyncIterator[Any]:
        text = completion.choices[0].message.content or ""
        for word in text.split(" "):
            if word:
                yield _Event(choices=[_Choice(delta=_Delta(content=word + " "))])
        yield _Event(
            choices=[_Choice(delta=_Delta(), finish_reason=completion.choices[0].finish_reason)],
            usage=completion.usage,
        )

    return generate()


class _OpenAIChat:
    def __init__(self, completions: _OpenAICompletions) -> None:
        self.completions = completions


def deterministic_vector(text: str, dimensions: int) -> list[float]:
    """A stable pseudo-vector derived from the text.

    The embedding contract asserts that identical text embeds identically and
    different text does not, so a stub that returns a constant would pass the
    shape checks while proving nothing.
    """
    digest = hashlib.blake2b(text.encode(), digest_size=8).digest()
    seed = int.from_bytes(digest, "big")
    return [((seed >> (i % 32)) % 1000) / 1000.0 + i * 1e-6 for i in range(dimensions)]


class _OpenAIEmbeddings:
    def __init__(self, outcome: OpenAIEmbeddingResponse | Exception | None) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> OpenAIEmbeddingResponse:
        self.calls.append(payload)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if self._outcome is not None:
            return self._outcome

        texts: list[str] = list(payload["input"])
        dimensions = int(payload["dimensions"])
        return OpenAIEmbeddingResponse(
            data=[
                OpenAIEmbeddingItem(embedding=deterministic_vector(text, dimensions), index=index)
                for index, text in enumerate(texts)
            ]
        )


class StubOpenAIClient:
    """Stands in for ``openai.AsyncOpenAI``."""

    def __init__(
        self,
        completion: OpenAICompletion | Exception | None = None,
        embeddings: OpenAIEmbeddingResponse | Exception | None = None,
    ) -> None:
        resolved_completion = (
            completion
            if completion is not None
            else OpenAICompletion(
                choices=[OpenAIChoice(message=OpenAIMessage(content="Hello from OpenAI."))]
            )
        )
        self.chat = _OpenAIChat(_OpenAICompletions(resolved_completion))
        # `None` means generate a vector per input rather than replay a fixture.
        self.embeddings = _OpenAIEmbeddings(embeddings)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


# --------------------------------------------------------------------------
# Voyage
# --------------------------------------------------------------------------


@dataclass
class VoyageResponse:
    embeddings: list[list[float]]
    total_tokens: int = 12


class StubVoyageClient:
    """Stands in for ``voyageai.AsyncClient``.

    With no scripted outcome it generates one vector per input text, keyed by
    text *and* ``input_type`` so the document/query distinction is observable.
    """

    def __init__(
        self,
        outcome: VoyageResponse | Exception | None = None,
        *,
        dimensions: int = 1024,
    ) -> None:
        self._outcome = outcome
        self._dimensions = dimensions
        self.calls: list[dict[str, Any]] = []

    async def embed(self, texts: list[str], **payload: Any) -> VoyageResponse:
        self.calls.append({"texts": texts, **payload})
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if self._outcome is not None:
            return self._outcome

        input_type = payload.get("input_type", "document")
        return VoyageResponse(
            embeddings=[
                deterministic_vector(f"{input_type}:{text}", self._dimensions) for text in texts
            ]
        )
