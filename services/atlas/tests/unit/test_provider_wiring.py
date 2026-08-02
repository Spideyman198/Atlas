"""Tests for the provider composition root.

Misconfiguration must fail at startup. Every case here is one that would
otherwise surface as a confusing failure on the first user request, or — worse —
as a silently wrong number on a cost report.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from atlas.config.providers import build_chat_provider, build_embedding_provider, build_providers
from atlas.config.settings import ChatSettings, DatabaseSettings, EmbeddingSettings, Settings
from atlas.domain.errors import ConfigurationError
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.embedding import EmbeddingProvider

pytestmark = pytest.mark.unit


def test_the_offline_vendors_need_no_api_key() -> None:
    """So an air-gapped demo can run the whole stack without an account."""
    chat = build_chat_provider(ChatSettings(vendor="fake", model="fake-model"))
    embedding = build_embedding_provider(
        EmbeddingSettings(vendor="hash", model="hash-embedding-v1", dimensions=64)
    )

    assert isinstance(chat, ChatProvider)
    assert isinstance(embedding, EmbeddingProvider)


def test_a_real_vendor_without_a_key_fails_at_startup() -> None:
    with pytest.raises(ConfigurationError, match="no API key"):
        build_chat_provider(ChatSettings(vendor="anthropic", model="claude-opus-5"))


def test_an_empty_key_is_treated_as_missing() -> None:
    with pytest.raises(ConfigurationError, match="no API key"):
        build_chat_provider(
            ChatSettings(vendor="anthropic", model="claude-opus-5", api_key=SecretStr(""))
        )


def test_an_unpriced_chat_model_is_refused() -> None:
    """Cost reporting is a requirement, so an unpriced model cannot start."""
    with pytest.raises(ConfigurationError, match="no pricing configured"):
        build_chat_provider(ChatSettings(vendor="fake", model="some-unlisted-model"))


def test_a_dimension_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured width is baked into a pgvector column, so disagreement is fatal.

    Substitutes an adapter that ignores the configured width — the failure mode
    this guard exists to catch, and one no correct adapter would reproduce.
    """
    from atlas.config import providers
    from atlas.infrastructure.providers.fakes import HashEmbeddingProvider

    monkeypatch.setattr(
        providers,
        "_embedding_adapter",
        lambda _settings: HashEmbeddingProvider(dimensions=128, model_id="hash-embedding-v1"),
    )

    with pytest.raises(ConfigurationError, match="128 dimensions but 64 is configured"):
        build_embedding_provider(
            EmbeddingSettings(vendor="hash", model="hash-embedding-v1", dimensions=64)
        )


def test_the_configured_dimension_reaches_the_provider() -> None:
    provider = build_embedding_provider(
        EmbeddingSettings(vendor="hash", model="hash-embedding-v1", dimensions=256)
    )

    assert provider.dimensions == 256


def test_the_chat_provider_is_wrapped_in_the_decorator_stack() -> None:
    """Accounting outside, retry inside — one cost figure per logical call."""
    provider = build_chat_provider(ChatSettings(vendor="fake", model="fake-model", max_retries=5))

    assert type(provider).__name__ == "AccountingChatProvider"


def test_build_providers_returns_both() -> None:
    settings = Settings(
        database=DatabaseSettings(url="postgresql://atlas:atlas@localhost:5432/atlas"),
        chat=ChatSettings(vendor="fake", model="fake-model"),
        embedding=EmbeddingSettings(vendor="hash", model="hash-embedding-v1", dimensions=64),
    )

    chat, embedding = build_providers(settings)

    assert isinstance(chat, ChatProvider)
    assert isinstance(embedding, EmbeddingProvider)


def test_defaults_describe_the_documented_deployment() -> None:
    """Claude for chat, OpenAI for embeddings — ADR-0005's default pair."""
    settings = ChatSettings()
    embedding = EmbeddingSettings()

    assert settings.vendor == "anthropic"
    assert settings.model == "claude-opus-5"
    assert embedding.vendor == "openai"
    assert embedding.model == "text-embedding-3-small"
    assert embedding.dimensions == 1536
