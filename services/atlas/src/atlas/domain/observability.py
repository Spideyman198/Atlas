"""What the application layer is allowed to know about being watched.

A port, for the same reason every other adapter is behind one: the orchestrator
should record that an answer was refused without importing a Prometheus
counter, and a test should be able to assert that it did without scraping an
exposition endpoint.

The methods are named after events rather than instruments — ``answer_finished``
rather than ``increment_counter`` — so the adapter decides whether that becomes
a counter, a histogram, a span attribute or all three. Naming them after
instruments would put that decision in the use case, which is where it would
then have to be changed to add a second backend.

:class:`NullRecorder` is the default everywhere. Observability that has to be
wired up before the code runs is observability that breaks tests, and a use case
should not branch on whether anyone is watching.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Recorder(Protocol):
    """Records what happened, for whoever is watching.

    Implementations must not raise. A metrics backend that is unreachable, or a
    label that turns out to be malformed, must never be the reason an answer
    fails — the request is the product, the measurement is not.
    """

    def answer_finished(self, *, outcome: str, intent: str, seconds: float) -> None:
        """One answer completed. ``outcome`` is ``answered``, ``refused`` or ``failed``."""
        ...

    def retrieval_finished(
        self, *, candidates: int, authorized: int, used: int, seconds: float
    ) -> None:
        """One retrieval completed, with what survived each stage.

        The gap between ``candidates`` and ``authorized`` is the denial rate,
        which is what says whether the over-fetch factor is set right.
        """
        ...

    def tool_finished(self, *, tool: str, outcome: str, seconds: float) -> None:
        """One tool call completed. ``outcome`` is ``ok`` or ``rejected``."""
        ...

    def provider_finished(
        self,
        *,
        provider: str,
        model: str,
        outcome: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """One model provider call completed."""
        ...


class NullRecorder:
    """Records nothing, and is the default.

    Not an oversight: most of the suite has no interest in metrics, and a use
    case that had to check whether a recorder existed before using one would
    carry that branch on every path.
    """

    def answer_finished(self, *, outcome: str, intent: str, seconds: float) -> None:
        """Ignore the event."""

    def retrieval_finished(
        self, *, candidates: int, authorized: int, used: int, seconds: float
    ) -> None:
        """Ignore the event."""

    def tool_finished(self, *, tool: str, outcome: str, seconds: float) -> None:
        """Ignore the event."""

    def provider_finished(
        self,
        *,
        provider: str,
        model: str,
        outcome: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Ignore the event."""
