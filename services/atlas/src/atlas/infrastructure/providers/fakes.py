"""Deterministic providers for tests.

These are the reason the suite runs with no network, no API key and no cost, and
the reason the M12 evaluation harness is reproducible. They are shipped in
``infrastructure`` rather than under ``tests`` so the contract suite can exercise
them through exactly the same import path as a real adapter.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence

from atlas.domain.chat import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    StopReason,
    ToolCall,
)
from atlas.domain.embedding import EmbeddingPurpose, EmbeddingResult, Vector
from atlas.domain.errors import ValidationError
from atlas.domain.usage import TokenUsage

#: Word characters and digits. Punctuation separates, so `INV/2026/0104` becomes
#: three tokens rather than one nothing else will ever match.
_TOKENS = re.compile(r"[a-z0-9]+")

_DEFAULT_REPLY = "This is a fake response."


class FakeChatProvider:
    """A scripted :class:`~atlas.domain.ports.chat.ChatProvider`.

    Returns queued responses in order, then repeats the last one. Queue an
    exception instance to make the next call raise it — that is how retry and
    failure paths are tested without touching a network.

    Every request is recorded on :attr:`requests`, so a test can assert what the
    application layer actually sent rather than only what came back.
    """

    def __init__(
        self,
        responses: Iterable[ChatResponse | Exception] | None = None,
        *,
        name: str = "fake",
        model: str = "fake-model",
        supports_tools: bool = True,
    ) -> None:
        self._queue: deque[ChatResponse | Exception] = deque(responses or ())
        self._last: ChatResponse | Exception | None = None
        self._name = name
        self._model = model
        self._supports_tools = supports_tools
        self.requests: list[ChatRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tools(self) -> bool:
        return self._supports_tools

    @property
    def call_count(self) -> int:
        """How many times :meth:`complete` or :meth:`stream` has been invoked."""
        return len(self.requests)

    def queue(self, *outcomes: ChatResponse | Exception) -> None:
        """Append outcomes to the script."""
        self._queue.extend(outcomes)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self._next()

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Replay a scripted response as a stream.

        The text is split into word-sized deltas so a consumer that reassembles
        chunks is genuinely exercised, followed by one terminal chunk carrying the
        stop reason and usage.
        """
        self.requests.append(request)
        response = self._next()

        for word in response.content.split(" "):
            if word:
                yield ChatChunk(text_delta=word + " ")

        yield ChatChunk(
            stop_reason=response.stop_reason,
            usage=response.usage,
            tool_calls=response.tool_calls,
        )

    def _next(self) -> ChatResponse:
        outcome = self._queue.popleft() if self._queue else self._last
        if outcome is None:
            outcome = ChatResponse(
                content=_DEFAULT_REPLY,
                stop_reason=StopReason.END_TURN,
                model=self._model,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
        self._last = outcome

        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_response(
    content: str = _DEFAULT_REPLY,
    *,
    stop_reason: StopReason = StopReason.END_TURN,
    tool_calls: tuple[ToolCall, ...] = (),
    model: str = "fake-model",
    usage: TokenUsage | None = None,
) -> ChatResponse:
    """Build a :class:`ChatResponse` without restating every field in each test."""
    return ChatResponse(
        content=content,
        stop_reason=stop_reason,
        model=model,
        usage=usage or TokenUsage(input_tokens=10, output_tokens=5),
        tool_calls=tool_calls,
    )


class HashEmbeddingProvider:
    """A deterministic :class:`~atlas.domain.ports.embedding.EmbeddingProvider`.

    Derives vectors from a BLAKE2b digest of the text, so the same input always
    produces the same vector and different inputs produce different ones. Vectors
    are L2-normalised, which makes cosine distance behave the way the real
    providers' output does — enough for retrieval plumbing tests, obviously not
    for semantic quality.
    """

    def __init__(
        self,
        *,
        dimensions: int = 1536,
        model_id: str = "hash-embedding-v1",
        max_batch_size: int = 96,
    ) -> None:
        if dimensions <= 0:
            msg = "dimensions must be positive"
            raise ValidationError(msg)
        self._dimensions = dimensions
        self._model_id = model_id
        self._max_batch_size = max_batch_size
        self.call_count = 0

    @property
    def name(self) -> str:
        return "hash"

    @property
    def model_id(self) -> str:
        return self._model_id

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
        if not texts:
            msg = "texts must not be empty"
            raise ValidationError(msg)
        if len(texts) > self._max_batch_size:
            msg = f"batch of {len(texts)} exceeds max_batch_size {self._max_batch_size}"
            raise ValidationError(msg, context={"batch_size": len(texts)})

        self.call_count += 1
        vectors = tuple(self._vector(text, purpose) for text in texts)
        return EmbeddingResult(
            vectors=vectors,
            model=self._model_id,
            usage=TokenUsage(input_tokens=sum(len(t.split()) for t in texts)),
        )

    def _vector(self, text: str, purpose: EmbeddingPurpose) -> Vector:
        """Expand a digest into a normalised vector of the configured length."""
        seed = f"{purpose.value}:{text}".encode()
        raw = bytearray()
        counter = 0
        needed = self._dimensions * 4
        while len(raw) < needed:
            digest = hashlib.blake2b(seed + counter.to_bytes(4, "big"), digest_size=64)
            raw.extend(digest.digest())
            counter += 1

        values = [
            # Map each 32-bit word onto [-1, 1) so vectors spread across the space
            # rather than clustering in the positive orthant.
            struct.unpack_from(">I", raw, offset=i * 4)[0] / 2**31 - 1.0
            for i in range(self._dimensions)
        ]

        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:  # pragma: no cover - impossible for a non-empty digest
            return tuple(values)
        return tuple(v / norm for v in values)


class TokenEmbeddingProvider:
    """A deterministic embedder where similar text produces similar vectors.

    :class:`HashEmbeddingProvider` digests the whole string, so two documents
    differing by one word are as far apart as two unrelated ones. That is fine
    for plumbing tests and useless for evaluation: the dense half of retrieval
    scores noise, and a metric over noise measures nothing.

    This hashes *tokens* into buckets instead — feature hashing, the oldest
    trick there is. Texts sharing words land near each other, so dense search
    genuinely contributes and fusion is exercised on something real.

    What it is not is semantic. "Owes" and "outstanding" share no characters and
    so share no bucket, and questions in the golden set that turn on that will
    score badly here. That is the honest limit of an offline gate, and it is why
    the same harness can run against a real provider when one is configured.
    """

    def __init__(
        self,
        *,
        dimensions: int = 256,
        model_id: str = "token-embedding-v1",
        max_batch_size: int = 96,
    ) -> None:
        if dimensions <= 0:
            msg = "dimensions must be positive"
            raise ValidationError(msg)
        self._dimensions = dimensions
        self._model_id = model_id
        self._max_batch_size = max_batch_size
        self.call_count = 0

    @property
    def name(self) -> str:
        return "token"

    @property
    def model_id(self) -> str:
        return self._model_id

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
        if not texts:
            msg = "texts must not be empty"
            raise ValidationError(msg)
        if len(texts) > self._max_batch_size:
            msg = f"batch of {len(texts)} exceeds max_batch_size {self._max_batch_size}"
            raise ValidationError(msg, context={"batch_size": len(texts)})

        self.call_count += 1
        return EmbeddingResult(
            vectors=tuple(self._vector(text) for text in texts),
            model=self._model_id,
            usage=TokenUsage(input_tokens=sum(len(t.split()) for t in texts)),
        )

    def _vector(self, text: str) -> Vector:
        """Sum one bucket per token, then normalise.

        The purpose is deliberately ignored. A query and a document containing
        the same word must land in the same bucket, and salting by purpose — as
        the digest embedder does — would put them in different ones.
        """
        values = [0.0] * self._dimensions
        for token in _TOKENS.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimensions
            # A sign bit taken from the digest, so unrelated tokens colliding in
            # one bucket cancel as often as they reinforce.
            sign = 1.0 if digest[4] & 1 else -1.0
            values[bucket] += sign

        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:
            return tuple(values)
        return tuple(v / norm for v in values)
