"""The chat completion port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from atlas.domain.chat import ChatChunk, ChatRequest, ChatResponse


@runtime_checkable
class ChatProvider(Protocol):
    """A model that completes conversations and may call tools.

    Implementations are interchangeable by construction: every adapter passes the
    same contract test suite, which is what lets the application layer be tested
    against a fake and deployed against a vendor.

    Implementations must:

    - Raise only :class:`~atlas.domain.errors.AtlasError` subclasses. Vendor SDK
      exceptions are translated at the boundary; letting one escape means a use
      case has to know which SDK produced it.
    - Return :attr:`~atlas.domain.chat.StopReason.REFUSAL` rather than raising
      when a safety classifier declines. A refusal is a successful call with a
      declined outcome, and callers must be able to tell the two apart.
    - Preserve tool-call ordering.
    - Populate :class:`~atlas.domain.usage.TokenUsage`, including cache fields
      when the vendor reports them. Zero is a legitimate value; a missing figure
      is not silently invented.
    """

    @property
    def name(self) -> str:
        """Stable identifier for logs and metrics, for example ``anthropic``."""
        ...

    @property
    def model(self) -> str:
        """The model this instance is configured to call."""
        ...

    @property
    def supports_tools(self) -> bool:
        """Whether tool definitions are honoured.

        A capability flag rather than a silent no-op: the orchestrator in M10
        routes structured questions elsewhere when tools are unavailable instead
        of sending a request whose tools would be ignored.
        """
        ...

    async def complete(self, request: ChatRequest) -> ChatResponse:
        """Generate a complete response.

        Raises:
            ProviderTimeoutError: The provider did not answer within its budget.
            RateLimitedError: The provider applied rate limiting.
            ProviderError: Any other provider-side failure.
        """
        ...

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Generate a response incrementally.

        The final chunk carries a stop reason and usage; earlier chunks carry text
        deltas. Implementations that cannot stream may yield a single terminal
        chunk, so callers never need to branch on the capability.
        """
        ...
