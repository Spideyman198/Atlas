"""Tests for the provider decorator stack."""

from __future__ import annotations

import pytest

from atlas.domain.chat import ChatRequest, Message, Role, StopReason
from atlas.domain.errors import ProviderError, ProviderTimeoutError, RateLimitedError
from atlas.domain.usage import TokenUsage
from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response
from atlas.infrastructure.providers.resilience import (
    AccountingChatProvider,
    RetryingChatProvider,
    RetryPolicy,
)

pytestmark = pytest.mark.unit

_REQUEST = ChatRequest(messages=(Message(role=Role.USER, content="hi"),))


class RecordingSleeper:
    """Captures the backoff schedule instead of waiting it out."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _retrying(
    *outcomes: object, policy: RetryPolicy | None = None
) -> tuple[RetryingChatProvider, FakeChatProvider, RecordingSleeper]:
    inner = FakeChatProvider(list(outcomes))  # type: ignore[arg-type]
    sleeper = RecordingSleeper()
    provider = RetryingChatProvider(inner, policy or RetryPolicy(), sleeper=sleeper)
    return provider, inner, sleeper


async def test_a_successful_call_is_not_retried() -> None:
    provider, inner, sleeper = _retrying(fake_response("ok"))

    response = await provider.complete(_REQUEST)

    assert response.content == "ok"
    assert inner.call_count == 1
    assert sleeper.delays == []


async def test_rate_limiting_is_retried_then_succeeds() -> None:
    provider, inner, sleeper = _retrying(
        RateLimitedError("slow down", provider="fake"), fake_response("recovered")
    )

    response = await provider.complete(_REQUEST)

    assert response.content == "recovered"
    assert inner.call_count == 2
    assert len(sleeper.delays) == 1


async def test_timeouts_are_retried() -> None:
    provider, inner, _ = _retrying(ProviderTimeoutError("timed out"), fake_response("recovered"))

    assert (await provider.complete(_REQUEST)).content == "recovered"
    assert inner.call_count == 2


async def test_the_last_error_surfaces_once_attempts_are_exhausted() -> None:
    policy = RetryPolicy(max_attempts=3, initial_backoff_seconds=0.01)
    provider, inner, sleeper = _retrying(
        RateLimitedError("first"),
        RateLimitedError("second"),
        RateLimitedError("third"),
        policy=policy,
    )

    with pytest.raises(RateLimitedError, match="third"):
        await provider.complete(_REQUEST)

    assert inner.call_count == 3
    assert len(sleeper.delays) == 2, "no sleep after the final attempt"


async def test_a_non_retryable_provider_error_fails_immediately() -> None:
    """Retrying a request the provider rejected on its merits only wastes budget."""
    provider, inner, sleeper = _retrying(ProviderError("bad request", provider="fake"))

    with pytest.raises(ProviderError, match="bad request"):
        await provider.complete(_REQUEST)

    assert inner.call_count == 1
    assert sleeper.delays == []


async def test_an_error_marked_retryable_is_retried() -> None:
    provider, inner, _ = _retrying(
        ProviderError("overloaded", context={"retryable": True}),
        fake_response("recovered"),
    )

    assert (await provider.complete(_REQUEST)).content == "recovered"
    assert inner.call_count == 2


async def test_max_attempts_of_one_disables_retrying() -> None:
    provider, inner, _ = _retrying(RateLimitedError("nope"), policy=RetryPolicy(max_attempts=1))

    with pytest.raises(RateLimitedError):
        await provider.complete(_REQUEST)

    assert inner.call_count == 1


def test_backoff_grows_with_each_attempt() -> None:
    policy = RetryPolicy(initial_backoff_seconds=1.0, multiplier=2.0, jitter=0.0)

    assert policy.backoff_for(1) == pytest.approx(1.0)
    assert policy.backoff_for(2) == pytest.approx(2.0)
    assert policy.backoff_for(3) == pytest.approx(4.0)


def test_a_provider_supplied_retry_after_wins_over_computed_backoff() -> None:
    """The provider knows when its window resets; we do not."""
    policy = RetryPolicy(initial_backoff_seconds=1.0, jitter=0.0)

    assert policy.backoff_for(1, retry_after=7.5) == pytest.approx(7.5)


def test_backoff_is_capped() -> None:
    """An unbounded retry-after would stall a request past the caller's deadline."""
    policy = RetryPolicy(jitter=0.0)

    assert policy.backoff_for(1, retry_after=9999.0) <= 30.0


def test_jitter_spreads_retries() -> None:
    policy = RetryPolicy(initial_backoff_seconds=10.0, jitter=0.5)

    delays = {policy.backoff_for(1) for _ in range(50)}

    assert len(delays) > 1, "identical delays would reproduce the burst"
    assert all(5.0 <= d <= 15.0 for d in delays)


async def test_accounting_records_latency_when_the_adapter_does_not() -> None:
    provider = AccountingChatProvider(FakeChatProvider([fake_response("ok")]))

    response = await provider.complete(_REQUEST)

    assert response.latency_ms >= 0
    assert response.content == "ok"


async def test_accounting_preserves_latency_the_adapter_measured() -> None:
    measured = fake_response("ok")
    measured = type(measured)(
        content=measured.content,
        stop_reason=measured.stop_reason,
        model=measured.model,
        usage=measured.usage,
        latency_ms=1234,
    )
    provider = AccountingChatProvider(FakeChatProvider([measured]))

    assert (await provider.complete(_REQUEST)).latency_ms == 1234


async def test_decorators_compose_and_preserve_identity() -> None:
    inner = FakeChatProvider([RateLimitedError("once"), fake_response("done")])
    stack = AccountingChatProvider(
        RetryingChatProvider(inner, RetryPolicy(initial_backoff_seconds=0.0), sleeper=_no_sleep)
    )

    response = await stack.complete(_REQUEST)

    assert response.content == "done"
    assert stack.name == inner.name
    assert stack.model == inner.model
    assert stack.supports_tools == inner.supports_tools


async def test_streaming_passes_through_the_stack() -> None:
    inner = FakeChatProvider([fake_response("hello world")])
    stack = AccountingChatProvider(RetryingChatProvider(inner, sleeper=_no_sleep))

    chunks = [chunk async for chunk in stack.stream(_REQUEST)]

    assert "".join(c.text_delta for c in chunks).strip() == "hello world"
    assert chunks[-1].stop_reason is StopReason.END_TURN
    assert chunks[-1].usage == TokenUsage(input_tokens=10, output_tokens=5)


async def _no_sleep(seconds: float) -> None:
    return None
