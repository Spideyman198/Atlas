"""The contract every EmbeddingProvider adapter must satisfy."""

from __future__ import annotations

from typing import cast

import openai
import pytest
import voyageai
from tests.support.stubs import StubOpenAIClient, StubVoyageClient

from atlas.domain.embedding import EmbeddingPurpose
from atlas.domain.errors import ValidationError
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.infrastructure.providers.fakes import HashEmbeddingProvider
from atlas.infrastructure.providers.openai_provider import OpenAIEmbeddingProvider
from atlas.infrastructure.providers.voyage_provider import VoyageEmbeddingProvider

pytestmark = pytest.mark.contract


class EmbeddingProviderContract:
    """Behaviour required of every embedding adapter."""

    @pytest.fixture
    def provider(self) -> EmbeddingProvider:
        raise NotImplementedError

    def test_it_satisfies_the_protocol(self, provider: EmbeddingProvider) -> None:
        assert isinstance(provider, EmbeddingProvider)

    def test_it_declares_its_model_and_dimensions(self, provider: EmbeddingProvider) -> None:
        """Both are persisted per document so a model change is detectable."""
        assert provider.model_id
        assert provider.dimensions > 0
        assert provider.max_batch_size > 0

    async def test_vectors_have_the_declared_dimensions(self, provider: EmbeddingProvider) -> None:
        result = await provider.embed(["hello"])

        assert len(result.vectors) == 1
        assert len(result.vectors[0]) == provider.dimensions

    async def test_results_align_positionally_with_the_input(
        self, provider: EmbeddingProvider
    ) -> None:
        """Retrieval attributes vectors to texts by position, so order is load-bearing."""
        texts = ["alpha", "beta", "gamma"]

        result = await provider.embed(texts)

        assert len(result) == len(texts)
        single = await provider.embed(["beta"])
        assert result.vectors[1] == single.vectors[0]

    async def test_the_same_text_embeds_identically(self, provider: EmbeddingProvider) -> None:
        first = await provider.embed(["stable"])
        second = await provider.embed(["stable"])

        assert first.vectors[0] == second.vectors[0]

    async def test_different_texts_embed_differently(self, provider: EmbeddingProvider) -> None:
        result = await provider.embed(["alpha", "beta"])

        assert result.vectors[0] != result.vectors[1]

    async def test_an_empty_batch_is_rejected(self, provider: EmbeddingProvider) -> None:
        with pytest.raises(ValidationError):
            await provider.embed([])

    async def test_an_oversized_batch_is_rejected(self, provider: EmbeddingProvider) -> None:
        """Rejected rather than truncated — a silent drop loses documents."""
        oversized = ["x"] * (provider.max_batch_size + 1)

        with pytest.raises(ValidationError):
            await provider.embed(oversized)

    async def test_it_reports_usage(self, provider: EmbeddingProvider) -> None:
        result = await provider.embed(["some text to embed"])

        assert result.usage.input_tokens >= 0
        assert result.model == provider.model_id


class TestHashEmbeddingProvider(EmbeddingProviderContract):
    """The offline fake must honour the same contract as a vendor adapter."""

    @pytest.fixture
    def provider(self) -> EmbeddingProvider:
        return HashEmbeddingProvider(dimensions=64, max_batch_size=8)

    async def test_vectors_are_l2_normalised(self) -> None:
        """Cosine distance behaves the way it will against real providers."""
        provider = HashEmbeddingProvider(dimensions=64)

        result = await provider.embed(["normalise me"])

        magnitude = sum(value * value for value in result.vectors[0]) ** 0.5
        assert magnitude == pytest.approx(1.0, abs=1e-9)

    async def test_purpose_changes_the_vector(self) -> None:
        """Query and document embeddings differ, as they do for real providers."""
        provider = HashEmbeddingProvider(dimensions=64)

        document = await provider.embed(["shared text"], EmbeddingPurpose.DOCUMENT)
        query = await provider.embed(["shared text"], EmbeddingPurpose.QUERY)

        assert document.vectors[0] != query.vectors[0]

    def test_a_non_positive_dimension_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HashEmbeddingProvider(dimensions=0)


class TestOpenAIEmbeddingProvider(EmbeddingProviderContract):
    """The OpenAI adapter, driven by a stub SDK client."""

    @pytest.fixture
    def provider(self) -> EmbeddingProvider:
        return OpenAIEmbeddingProvider(
            cast("openai.AsyncOpenAI", StubOpenAIClient()),
            model="text-embedding-3-small",
            dimensions=64,
            max_batch_size=8,
        )


class TestVoyageEmbeddingProvider(EmbeddingProviderContract):
    """The Voyage adapter, driven by a stub SDK client."""

    @pytest.fixture
    def provider(self) -> EmbeddingProvider:
        return VoyageEmbeddingProvider(
            cast("voyageai.AsyncClient", StubVoyageClient(dimensions=64)),
            model="voyage-3",
            dimensions=64,
            max_batch_size=8,
        )

    async def test_the_purpose_is_sent_as_an_input_type(self) -> None:
        """The purpose must reach the vendor.

        Voyage embeds documents and queries differently, and using the wrong side
        measurably degrades recall.
        """
        client = StubVoyageClient(dimensions=64)
        provider = VoyageEmbeddingProvider(
            cast("voyageai.AsyncClient", client), dimensions=64, model="voyage-3"
        )

        await provider.embed(["text"], EmbeddingPurpose.QUERY)

        assert client.calls[0]["input_type"] == "query"
