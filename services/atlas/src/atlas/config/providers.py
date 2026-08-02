"""Provider construction.

The only module that names a concrete adapter. Everything else takes a
:class:`~atlas.domain.ports.chat.ChatProvider` or
:class:`~atlas.domain.ports.embedding.EmbeddingProvider` and cannot tell which
vendor it received — which is what makes "switch provider by environment
variable" a configuration change rather than a code change.

Every chat provider is wrapped in the same decorator stack, in the same order:

    Accounting( Retrying( Adapter ) )

Retry sits closest to the vendor so each attempt is timed and costed
individually. Accounting on the outside reports one figure per logical call, not
one per attempt.
"""

from __future__ import annotations

import logging

import anthropic
import openai
import voyageai

from atlas.config.settings import ChatSettings, EmbeddingSettings, Settings
from atlas.domain.errors import ConfigurationError
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.infrastructure.providers.anthropic_provider import AnthropicChatProvider
from atlas.infrastructure.providers.fakes import FakeChatProvider, HashEmbeddingProvider
from atlas.infrastructure.providers.openai_provider import (
    OpenAIChatProvider,
    OpenAIEmbeddingProvider,
)
from atlas.infrastructure.providers.pricing import known_models
from atlas.infrastructure.providers.resilience import (
    AccountingChatProvider,
    RetryingChatProvider,
    RetryPolicy,
)
from atlas.infrastructure.providers.voyage_provider import VoyageEmbeddingProvider

logger = logging.getLogger(__name__)


def build_chat_provider(settings: ChatSettings) -> ChatProvider:
    """Construct the configured chat provider with its decorator stack."""
    adapter = _chat_adapter(settings)
    _require_pricing(adapter.model)

    return AccountingChatProvider(
        RetryingChatProvider(adapter, RetryPolicy(max_attempts=settings.max_retries))
    )


def build_embedding_provider(settings: EmbeddingSettings) -> EmbeddingProvider:
    """Construct the configured embedding provider."""
    provider = _embedding_adapter(settings)

    if provider.dimensions != settings.dimensions:
        msg = (
            f"embedding provider reports {provider.dimensions} dimensions but "
            f"{settings.dimensions} is configured"
        )
        raise ConfigurationError(msg, context={"model": provider.model_id})

    _require_pricing(provider.model_id)
    return provider


def build_providers(settings: Settings) -> tuple[ChatProvider, EmbeddingProvider]:
    """Construct both providers, failing fast on a misconfiguration."""
    return build_chat_provider(settings.chat), build_embedding_provider(settings.embedding)


def _chat_adapter(settings: ChatSettings) -> ChatProvider:
    if settings.vendor == "fake":
        return FakeChatProvider(model=settings.model)

    api_key = _api_key(settings.api_key, settings.vendor)

    if settings.vendor == "anthropic":
        return AnthropicChatProvider(
            anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                # Atlas owns retrying; leaving the SDK's own retries on would
                # multiply the two policies together.
                max_retries=0,
            ),
            model=settings.model,
        )

    return OpenAIChatProvider(
        openai.AsyncOpenAI(
            api_key=api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=0,
        ),
        model=settings.model,
    )


def _embedding_adapter(settings: EmbeddingSettings) -> EmbeddingProvider:
    if settings.vendor == "hash":
        return HashEmbeddingProvider(
            dimensions=settings.dimensions,
            model_id=settings.model,
            max_batch_size=settings.max_batch_size,
        )

    api_key = _api_key(settings.api_key, settings.vendor)

    if settings.vendor == "voyage":
        return VoyageEmbeddingProvider(
            voyageai.AsyncClient(api_key=api_key),
            model=settings.model,
            dimensions=settings.dimensions,
            max_batch_size=settings.max_batch_size,
        )

    return OpenAIEmbeddingProvider(
        openai.AsyncOpenAI(api_key=api_key, timeout=settings.timeout_seconds, max_retries=0),
        model=settings.model,
        dimensions=settings.dimensions,
        max_batch_size=settings.max_batch_size,
    )


def _api_key(key: object, vendor: str) -> str:
    """Read a configured key, failing at startup rather than on first request."""
    secret = getattr(key, "get_secret_value", None)
    value = secret() if callable(secret) else None
    if not value:
        msg = f"no API key configured for the {vendor} provider"
        raise ConfigurationError(msg, context={"vendor": vendor})
    return str(value)


def _require_pricing(model: str) -> None:
    """Refuse to start with a model whose cost cannot be reported.

    Cost reporting is a product requirement, not a nice-to-have, so an unpriced
    model is a configuration error caught at boot rather than a silent zero on a
    dashboard three weeks later.
    """
    if model not in known_models():
        msg = f"no pricing configured for model {model!r}"
        raise ConfigurationError(msg, context={"model": model})


__all__ = ["build_chat_provider", "build_embedding_provider", "build_providers"]
