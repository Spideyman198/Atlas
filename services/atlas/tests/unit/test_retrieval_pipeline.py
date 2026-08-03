"""Tests for the retrieval pipeline and context assembly.

The pipeline's contract is an ordering — retrieve, authorize, assemble — and
most of what is worth asserting is that the middle step cannot be skipped,
softened, or survived when Odoo is unhappy.
"""

from __future__ import annotations

import pytest

from atlas.application.authorization import AuthorizationFilter
from atlas.application.retrieval import ContextAssembler, RetrievalPipeline
from atlas.domain.authorization import UserContext
from atlas.domain.corpus import AuthorizedChunk, CandidateChunk
from atlas.domain.errors import AuthorizationError
from atlas.domain.retrieval import RetrievalRequest, estimate_tokens
from atlas.infrastructure.odoo.fakes import FakeOdooGateway

pytestmark = pytest.mark.unit

ALICE = UserContext(token="alice-token", trace_id="trace-1")


def candidate(chunk_id: int, res_id: int, content: str = "some content") -> CandidateChunk:
    return CandidateChunk(
        chunk_id=chunk_id,
        document_id=chunk_id * 10,
        content=content,
        score=1.0 / chunk_id,
        res_model="sale.order",
        res_id=res_id,
        metadata={"title": f"S{res_id:05d}"},
    )


def authorized(chunk_id: int, res_id: int, content: str = "some content") -> AuthorizedChunk:
    return AuthorizedChunk(
        chunk_id=chunk_id,
        document_id=chunk_id * 10,
        content=content,
        score=1.0 / chunk_id,
        res_model="sale.order",
        res_id=res_id,
        metadata={"title": f"S{res_id:05d}"},
    )


class StubRetriever:
    def __init__(self, candidates: list[CandidateChunk]) -> None:
        self._candidates = candidates
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> list[CandidateChunk]:
        self.requests.append(request)
        return list(self._candidates)


def build_pipeline(
    candidates: list[CandidateChunk],
    readable: dict[str, list[int]] | None = None,
    **gateway_kwargs: object,
) -> tuple[RetrievalPipeline, StubRetriever]:
    retriever = StubRetriever(candidates)
    gateway = FakeOdooGateway(
        readable={ALICE.token: readable if readable is not None else {"sale.order": [1, 2, 3]}},
        **gateway_kwargs,  # type: ignore[arg-type]
    )
    pipeline = RetrievalPipeline(
        retriever=retriever,
        authorization=AuthorizationFilter(gateway),
        assembler=ContextAssembler(),
    )
    return pipeline, retriever


# --- the ordering -----------------------------------------------------------


async def test_only_authorized_chunks_reach_the_context() -> None:
    pipeline, _retriever = build_pipeline(
        [candidate(1, 1), candidate(2, 9), candidate(3, 2)],
        readable={"sale.order": [1, 2]},
    )

    result = await pipeline.run(ALICE, RetrievalRequest(query="orders"))

    assert result.candidates == 3
    assert result.authorized == 2
    assert result.denied == 1
    assert "S00009" not in result.context.text


async def test_an_unreachable_odoo_produces_no_context_at_all() -> None:
    """Fail closed. Not "degrade to unfiltered", not "return what we had"."""
    pipeline, _retriever = build_pipeline([candidate(1, 1)], unavailable=True)

    with pytest.raises(AuthorizationError):
        await pipeline.run(ALICE, RetrievalRequest(query="orders"))


async def test_a_refused_context_produces_no_context_at_all() -> None:
    pipeline, _retriever = build_pipeline([candidate(1, 1)])

    with pytest.raises(AuthorizationError):
        await pipeline.run(UserContext(token="not-a-token"), RetrievalRequest(query="orders"))


async def test_nothing_authorized_is_an_empty_context_not_an_error() -> None:
    """Refusal is a correct answer, and the pipeline has to be able to say it."""
    pipeline, _retriever = build_pipeline([candidate(1, 9)], readable={"sale.order": []})

    result = await pipeline.run(ALICE, RetrievalRequest(query="orders"))

    assert result.authorized == 0
    assert result.context.is_empty
    assert result.context.citations == ()


async def test_the_over_fetch_is_trimmed_after_authorization_not_before() -> None:
    """The extra candidates exist for the filter's benefit, not the prompt's."""
    pipeline, retriever = build_pipeline(
        [candidate(index, index) for index in range(1, 13)],
        readable={"sale.order": list(range(1, 13))},
    )

    result = await pipeline.run(ALICE, RetrievalRequest(query="orders", limit=3, over_fetch=4))

    assert retriever.requests[0].candidate_limit == 12
    assert result.authorized == 12
    assert result.context.chunks_used == 3


async def test_the_trace_id_follows_the_request_through() -> None:
    pipeline, _retriever = build_pipeline([candidate(1, 1)])

    result = await pipeline.run(ALICE, RetrievalRequest(query="orders"))

    assert result.trace_id == "trace-1"


# --- assembly ---------------------------------------------------------------


def test_blocks_are_numbered_so_an_answer_can_refer_to_them() -> None:
    context = ContextAssembler().assemble(
        [authorized(1, 1, "First order"), authorized(2, 2, "Second order")], budget=1000
    )

    assert context.text.startswith("[1] S00001")
    assert "[2] S00002" in context.text


def test_the_budget_is_respected_and_the_overflow_reported() -> None:
    long_text = "word " * 500
    chunks = [authorized(index, index, long_text) for index in range(1, 6)]
    budget = estimate_tokens(long_text) * 2

    context = ContextAssembler().assemble(chunks, budget=budget)

    assert context.chunks_used < 5
    assert context.chunks_dropped == 5 - context.chunks_used
    assert context.estimated_tokens <= budget


def test_a_chunk_that_does_not_fit_is_skipped_not_truncated() -> None:
    """Half a sales order is how a model states half a fact with confidence."""
    chunks = [authorized(1, 1, "short"), authorized(2, 2, "word " * 5000)]

    context = ContextAssembler().assemble(chunks, budget=estimate_tokens("short") + 20)

    assert "short" in context.text
    assert "word word" not in context.text


def test_a_later_chunk_can_still_fit_after_one_is_skipped() -> None:
    """Greedy by rank, not stop-at-first-miss: the budget is there to be used."""
    chunks = [
        authorized(1, 1, "small one"),
        authorized(2, 2, "word " * 5000),
        authorized(3, 3, "small two"),
    ]

    context = ContextAssembler().assemble(chunks, budget=200)

    assert context.chunks_used == 2
    assert "small two" in context.text


def test_citations_come_from_what_actually_entered_the_prompt() -> None:
    """A citation cannot be hallucinated because no model produces one."""
    context = ContextAssembler().assemble([authorized(1, 7, "Deco Addict order")], budget=1000)

    assert len(context.citations) == 1
    citation = context.citations[0]
    assert (citation.res_model, citation.res_id) == ("sale.order", 7)
    assert citation.record_name == "S00007"
    assert "Deco Addict" in citation.snippet
    assert citation.sequence == 1


def test_several_chunks_of_one_record_produce_one_citation() -> None:
    """Three chunks of the same order are one thing to go and look at."""
    context = ContextAssembler().assemble(
        [authorized(1, 7, "header"), authorized(2, 7, "lines"), authorized(3, 8, "other")],
        budget=1000,
    )

    assert [(c.res_id, c.sequence) for c in context.citations] == [(7, 1), (8, 2)]


def test_a_chunk_with_no_record_behind_it_is_used_but_not_cited() -> None:
    orphan = AuthorizedChunk(chunk_id=1, document_id=1, content="policy text", score=1.0)

    context = ContextAssembler().assemble([orphan], budget=1000)

    assert "policy text" in context.text
    assert context.citations == ()


def test_a_label_falls_back_when_a_title_was_never_recorded() -> None:
    bare = AuthorizedChunk(
        chunk_id=1, document_id=1, content="x", score=1.0, res_model="sale.order", res_id=4
    )
    referenced = AuthorizedChunk(
        chunk_id=2,
        document_id=2,
        content="x",
        score=1.0,
        res_model="sale.order",
        res_id=5,
        external_ref="S00005",
    )

    context = ContextAssembler().assemble([bare, referenced], budget=1000)

    assert "sale.order #4" in context.text
    assert "S00005" in context.text


def test_an_empty_chunk_list_assembles_to_an_empty_context() -> None:
    context = ContextAssembler().assemble([], budget=1000)

    assert context.is_empty
    assert context.chunks_used == 0
    assert context.citations == ()


def test_snippets_are_one_line_and_bounded() -> None:
    context = ContextAssembler(snippet_chars=40).assemble(
        [authorized(1, 1, "line one\nline two\n" + "x " * 200)], budget=5000
    )

    snippet = context.citations[0].snippet
    assert "\n" not in snippet
    assert len(snippet) <= 40
