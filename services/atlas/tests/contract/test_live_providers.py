"""The contract suites, run against real vendor APIs.

Marked ``live`` and skipped unless the matching key is present, so this never
runs on a pull request: it needs credentials and it costs money. It exists to
catch vendor drift — an SDK change or an API change that the stubs, being frozen
copies of today's shapes, cannot detect.

Run it deliberately::

    ANTHROPIC_API_KEY=... pytest -m live

M14 schedules it nightly with repository secrets.
"""

from __future__ import annotations

import os

import pytest

from atlas.domain.chat import ChatRequest, Message, Role, StopReason
from atlas.domain.embedding import EmbeddingPurpose

pytestmark = pytest.mark.live

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
_VOYAGE_KEY = os.environ.get("VOYAGE_API_KEY")

_PROMPT = ChatRequest(
    messages=(Message(role=Role.USER, content="Reply with the single word: ok"),),
    max_output_tokens=2048,
)


@pytest.mark.skipif(not _ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY is not set")
async def test_anthropic_completes_and_reports_usage() -> None:
    import anthropic

    from atlas.infrastructure.providers.anthropic_provider import AnthropicChatProvider

    provider = AnthropicChatProvider(
        anthropic.AsyncAnthropic(api_key=_ANTHROPIC_KEY, max_retries=0),
        model="claude-opus-5",
    )

    response = await provider.complete(_PROMPT)

    assert response.stop_reason in {StopReason.END_TURN, StopReason.MAX_TOKENS}
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    assert response.model


@pytest.mark.skipif(not _ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY is not set")
async def test_anthropic_streams() -> None:
    import anthropic

    from atlas.infrastructure.providers.anthropic_provider import AnthropicChatProvider

    provider = AnthropicChatProvider(
        anthropic.AsyncAnthropic(api_key=_ANTHROPIC_KEY, max_retries=0),
        model="claude-opus-5",
    )

    chunks = [chunk async for chunk in provider.stream(_PROMPT)]

    assert chunks[-1].is_final
    assert chunks[-1].usage is not None


@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY is not set")
async def test_openai_embeds_at_the_configured_width() -> None:
    import openai

    from atlas.infrastructure.providers.openai_provider import OpenAIEmbeddingProvider

    provider = OpenAIEmbeddingProvider(
        openai.AsyncOpenAI(api_key=_OPENAI_KEY, max_retries=0),
        model="text-embedding-3-small",
        dimensions=1536,
    )

    result = await provider.embed(["Odoo Atlas", "retrieval augmented generation"])

    assert len(result) == 2
    assert len(result.vectors[0]) == 1536
    assert result.vectors[0] != result.vectors[1]


@pytest.mark.skipif(not _VOYAGE_KEY, reason="VOYAGE_API_KEY is not set")
async def test_voyage_distinguishes_documents_from_queries() -> None:
    import voyageai

    from atlas.infrastructure.providers.voyage_provider import VoyageEmbeddingProvider

    provider = VoyageEmbeddingProvider(
        voyageai.AsyncClient(api_key=_VOYAGE_KEY),
        model="voyage-3",
        dimensions=1024,
    )

    document = await provider.embed(["shared text"], EmbeddingPurpose.DOCUMENT)
    query = await provider.embed(["shared text"], EmbeddingPurpose.QUERY)

    assert len(document.vectors[0]) == 1024
    assert document.vectors[0] != query.vectors[0]
