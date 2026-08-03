"""Capping how often one person can ask.

The thing being protected is not the engine. It is the bill, and Odoo's worker
pool: one runaway client can spend a month's provider budget in an afternoon,
and every answer costs Odoo an authorization round-trip and up to five tool
reads out of a synchronous worker pool the ERP needs for its own users.

**Per user, not per IP.** Everyone in an Odoo deployment arrives from the same
handful of addresses — often exactly one, if there is a reverse proxy — so an
IP limit would either be too loose to matter or would let one person exhaust the
allowance for the whole company. The context token names the user, which is what
the limit is keyed on.

A token bucket rather than a fixed window. A fixed window lets somebody spend
the whole minute's allowance in its last second and the next minute's in its
first, which is the burst it exists to prevent; and it stalls a user who asked
one question too many at 12:00:59 until 12:01:00 even though they have been idle
for an hour. A bucket refills continuously, so normal use never notices it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: Questions per minute, sustained. Chosen from what a person does rather than
#: what the engine can take: reading an answer and thinking of the next question
#: takes longer than four seconds, so this is invisible to anyone using the
#: product and immediate for anything in a loop.
DEFAULT_PER_MINUTE: Final = 15

#: How many may be asked back to back before the sustained rate applies. Covers
#: someone pasting three questions in a row, which is normal, without covering a
#: script, which is not.
DEFAULT_BURST: Final = 5


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether one request may proceed.

    Attributes:
        allowed: Whether to serve it.
        retry_after: Seconds until the next token, when refused. Sent to the
            client so it can wait the right amount rather than hammering.
        remaining: Whole tokens left, for the response headers.
    """

    allowed: bool
    retry_after: float = 0.0
    remaining: int = 0


@dataclass
class _Bucket:
    """One user's allowance."""

    tokens: float
    updated: float


@dataclass
class TokenBucketLimiter:
    """An in-process token bucket, keyed by whatever identifies a caller.

    In process, and therefore per replica: two engine replicas each allow the
    configured rate, so the effective limit is double. Stated rather than hidden
    — the alternative is a round-trip to Redis on every question to enforce a
    number chosen with a factor-of-two margin anyway. A shared limiter belongs
    with the horizontal-scaling work, not before it.

    Not thread-safe by design: the engine runs one asyncio loop per process, and
    a lock here would serialise every request behind a dictionary update.
    """

    per_minute: int = DEFAULT_PER_MINUTE
    burst: int = DEFAULT_BURST
    _buckets: dict[str, _Bucket] = field(default_factory=dict, repr=False)

    def check(self, key: str, *, now: float) -> Decision:
        """Take a token for ``key``, or refuse.

        Args:
            key: Identifies the caller. A context token, not an IP.
            now: Monotonic seconds. Passed in rather than read here so a test
                can advance time without sleeping through it.
        """
        rate = self.per_minute / 60.0
        bucket = self._buckets.get(key)

        if bucket is None:
            bucket = _Bucket(tokens=float(self.burst), updated=now)
            self._buckets[key] = bucket
        else:
            elapsed = max(now - bucket.updated, 0.0)
            bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * rate)
            bucket.updated = now

        if bucket.tokens < 1.0:
            missing = 1.0 - bucket.tokens
            return Decision(allowed=False, retry_after=missing / rate, remaining=0)

        bucket.tokens -= 1.0
        return Decision(allowed=True, remaining=int(bucket.tokens))

    def forget(self, before: float) -> int:
        """Drop buckets untouched since ``before``, returning how many went.

        Without this the dictionary grows one entry per user who ever asked
        anything and never shrinks. Called on a schedule rather than on every
        request: sweeping the whole map to serve one question would make the
        limiter cost more than the thing it protects.
        """
        stale = [key for key, bucket in self._buckets.items() if bucket.updated < before]
        for key in stale:
            del self._buckets[key]
        return len(stale)

    @property
    def tracked(self) -> int:
        """How many callers are currently held in memory."""
        return len(self._buckets)
