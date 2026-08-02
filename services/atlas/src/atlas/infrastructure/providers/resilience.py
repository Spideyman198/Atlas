"""Cross-cutting provider concerns, applied by decoration.

Retry, timeout and usage accounting are implemented once and wrapped around any
:class:`~atlas.domain.ports.chat.ChatProvider`, rather than being repeated in each
vendor adapter. That is what keeps "add a provider" to one small class: an adapter
translates payloads and nothing else.

The decorators are themselves ``ChatProvider`` implementations, so they compose
and are interchangeable with what they wrap.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from atlas.domain.chat import ChatChunk, ChatRequest, ChatResponse
from atlas.domain.errors import ProviderError, ProviderTimeoutError, RateLimitedError
from atlas.domain.ports.chat import ChatProvider
from atlas.infrastructure.providers.pricing import estimate_cost

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]

#: Cap on any single backoff wait. Without it, a provider advertising a long
#: `retry-after` can stall a request past the caller's own deadline.
_MAX_BACKOFF_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """When and how hard to retry.

    Attributes:
        max_attempts: Total attempts including the first. ``1`` disables retrying.
        initial_backoff_seconds: Wait before the second attempt.
        multiplier: Growth factor applied to each subsequent wait.
        jitter: Fraction of the computed wait to randomise, in ``[0, 1]``. Without
            jitter, every client that hit the same rate limit retries in lockstep
            and reproduces the burst that caused it.
    """

    max_attempts: int = 3
    initial_backoff_seconds: float = 0.5
    multiplier: float = 2.0
    jitter: float = 0.25

    def backoff_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Seconds to wait before ``attempt`` (1-based, so the first wait is 1).

        A provider-supplied ``retry_after`` wins over the computed value: the
        provider knows when its window resets and we do not.
        """
        base = (
            retry_after
            if retry_after is not None
            else self.initial_backoff_seconds * (self.multiplier ** (attempt - 1))
        )
        spread = base * self.jitter
        return min(max(base + random.uniform(-spread, spread), 0.0), _MAX_BACKOFF_SECONDS)  # noqa: S311


class RetryingChatProvider:
    """Retries transient provider failures.

    Retries rate limiting and timeouts, and any :class:`ProviderError` explicitly
    marked retryable. A provider error that is *not* marked retryable — a bad
    request, an invalid key — is re-raised immediately: retrying a request the
    provider has already rejected on its merits only wastes the caller's budget.
    """

    def __init__(
        self,
        inner: ChatProvider,
        policy: RetryPolicy | None = None,
        *,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()
        # Injected so tests exercise the backoff schedule without real waiting.
        self._sleep: Sleeper = sleeper or asyncio.sleep

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def supports_tools(self) -> bool:
        return self._inner.supports_tools

    async def complete(self, request: ChatRequest) -> ChatResponse:
        last_error: Exception | None = None

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return await self._inner.complete(request)
            except RateLimitedError as exc:
                last_error = exc
                retry_after = exc.retry_after_seconds
            except ProviderTimeoutError as exc:
                last_error = exc
                retry_after = None
            except ProviderError as exc:
                if not _is_retryable(exc):
                    raise
                last_error = exc
                retry_after = None

            if attempt == self._policy.max_attempts:
                break

            delay = self._policy.backoff_for(attempt, retry_after=retry_after)
            logger.warning(
                "provider call failed, retrying",
                extra={
                    "provider": self._inner.name,
                    "attempt": attempt,
                    "max_attempts": self._policy.max_attempts,
                    "delay_seconds": round(delay, 3),
                    "error": type(last_error).__name__,
                },
            )
            await self._sleep(delay)

        assert last_error is not None  # noqa: S101 - the loop cannot exit otherwise
        raise last_error

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Delegate streaming without retrying.

        A stream that fails partway has already delivered tokens to the caller.
        Restarting it would duplicate them, so recovery is the caller's decision.
        """
        return self._inner.stream(request)


class AccountingChatProvider:
    """Records latency, token usage and estimated cost for every call.

    Wraps rather than modifies the adapters, so accounting is uniform across
    vendors and cannot be forgotten in a new one. The `trace_id` on each log
    record ties the spend back to a single request.
    """

    def __init__(self, inner: ChatProvider) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def supports_tools(self) -> bool:
        return self._inner.supports_tools

    async def complete(self, request: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        response = await self._inner.complete(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        logger.info(
            "provider call completed",
            extra={
                "provider": self._inner.name,
                "model": response.model,
                "stop_reason": response.stop_reason.value,
                "latency_ms": elapsed_ms,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": response.usage.cache_read_input_tokens,
                "cost_usd": str(estimate_cost(response.model, response.usage)),
            },
        )

        # The adapter does not time itself; latency is measured here so every
        # provider reports it the same way.
        return (
            response
            if response.latency_ms
            else ChatResponse(
                content=response.content,
                stop_reason=response.stop_reason,
                model=response.model,
                usage=response.usage,
                tool_calls=response.tool_calls,
                latency_ms=elapsed_ms,
            )
        )

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        return self._inner.stream(request)


def _is_retryable(error: ProviderError) -> bool:
    """Whether a generic provider error is worth another attempt."""
    retryable = error.context.get("retryable")
    return bool(retryable)
