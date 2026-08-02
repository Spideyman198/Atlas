"""Token accounting.

One shape for every provider. Adapters normalise vendor usage payloads into
:class:`TokenUsage` so that cost, telemetry and the per-conversation reporting in
M12 never branch on which vendor served a request.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens consumed by a single provider call.

    Cache fields are separate because they are priced differently: a cache read
    costs a fraction of an uncached input token and a cache write costs a premium.
    Folding them into ``input_tokens`` would make cost estimates wrong in both
    directions and hide whether caching is working at all.

    Attributes:
        input_tokens: Uncached prompt tokens, billed at the full input rate.
        output_tokens: Generated tokens.
        cache_read_input_tokens: Prompt tokens served from a provider-side cache.
        cache_write_input_tokens: Prompt tokens written to a provider-side cache.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0

    @property
    def total_prompt_tokens(self) -> int:
        """Every prompt token, however it was billed."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_write_input_tokens

    @property
    def total_tokens(self) -> int:
        """Prompt plus generated tokens."""
        return self.total_prompt_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Accumulate usage across the calls that make up one answer."""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=(self.cache_read_input_tokens + other.cache_read_input_tokens),
            cache_write_input_tokens=(
                self.cache_write_input_tokens + other.cache_write_input_tokens
            ),
        )
