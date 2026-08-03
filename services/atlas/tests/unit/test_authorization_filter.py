"""Tests for stage 2 of retrieval.

The point of these is the negative direction. A filter that lets the right
chunks through is table stakes; what matters is that there is no input, and no
failure of Odoo, that lets a wrong one through.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlas.application.authorization import AuthorizationFilter
from atlas.domain.authorization import UserContext
from atlas.domain.corpus import CandidateChunk
from atlas.domain.errors import (
    AuthorizationError,
    DependencyUnavailableError,
    StorageError,
)
from atlas.infrastructure.odoo.fakes import FakeOdooGateway

pytestmark = pytest.mark.unit

ALICE = UserContext(token="alice-token", trace_id="trace-1")
BOB = UserContext(token="bob-token")


def candidate(
    chunk_id: int, model: str | None = "sale.order", res_id: int | None = 1
) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=chunk_id,
        document_id=chunk_id * 10,
        content=f"chunk {chunk_id}",
        score=1.0 / chunk_id,
        res_model=model,
        res_id=res_id,
    )


def gateway(**kwargs: Any) -> FakeOdooGateway:
    return FakeOdooGateway(
        readable={
            ALICE.token: {"sale.order": [1, 2], "res.partner": [7]},
            BOB.token: {"sale.order": [3]},
        },
        **kwargs,
    )


async def test_only_the_records_odoo_grants_survive() -> None:
    subject = AuthorizationFilter(gateway())
    candidates = [candidate(1, res_id=1), candidate(2, res_id=3), candidate(3, res_id=2)]

    authorized = await subject.filter(ALICE, candidates)

    assert [chunk.chunk_id for chunk in authorized] == [1, 3]


async def test_two_users_see_different_things_from_the_same_candidates() -> None:
    subject = AuthorizationFilter(gateway())
    candidates = [candidate(1, res_id=1), candidate(2, res_id=3)]

    assert [c.chunk_id for c in await subject.filter(ALICE, candidates)] == [1]
    assert [c.chunk_id for c in await subject.filter(BOB, candidates)] == [2]


async def test_ranking_survives_the_filter() -> None:
    subject = AuthorizationFilter(gateway())
    candidates = [candidate(2, res_id=2), candidate(1, res_id=1)]

    authorized = await subject.filter(ALICE, candidates)

    assert [chunk.chunk_id for chunk in authorized] == [2, 1]


async def test_the_authorized_chunk_carries_the_candidate_through() -> None:
    subject = AuthorizationFilter(gateway())

    authorized = await subject.filter(ALICE, [candidate(1, res_id=1)])

    chunk = authorized[0]
    assert (chunk.chunk_id, chunk.document_id, chunk.content) == (1, 10, "chunk 1")
    assert (chunk.res_model, chunk.res_id) == ("sale.order", 1)
    assert chunk.score == pytest.approx(1.0)


async def test_candidates_are_batched_into_one_call_per_model() -> None:
    fake = gateway()
    subject = AuthorizationFilter(fake)
    candidates = [
        candidate(1, "sale.order", 1),
        candidate(2, "sale.order", 2),
        candidate(3, "res.partner", 7),
    ]

    await subject.filter(ALICE, candidates)

    assert fake.authorize_calls == [{"sale.order": [1, 2], "res.partner": [7]}]


async def test_nothing_in_means_nothing_out_and_no_call() -> None:
    fake = gateway()

    assert await AuthorizationFilter(fake).filter(ALICE, []) == []
    assert fake.authorize_calls == []


async def test_chunks_with_no_odoo_record_are_dropped() -> None:
    # Uploads and manuals are authorized by visibility tier and owning group
    # instead (ADR-0006), and that check does not exist yet. Until it does,
    # dropping them is the only answer that cannot leak.
    fake = gateway()
    subject = AuthorizationFilter(fake)

    authorized = await subject.filter(ALICE, [candidate(1, model=None, res_id=None)])

    assert authorized == []
    assert fake.authorize_calls == []


async def test_an_unreachable_odoo_authorizes_nothing() -> None:
    subject = AuthorizationFilter(gateway(unavailable=True))

    with pytest.raises(AuthorizationError):
        await subject.filter(ALICE, [candidate(1, res_id=1)])


async def test_a_refused_context_authorizes_nothing() -> None:
    subject = AuthorizationFilter(gateway())

    with pytest.raises(AuthorizationError):
        await subject.filter(UserContext(token="not-a-token"), [candidate(1, res_id=1)])


@pytest.mark.parametrize(
    "failure",
    [
        DependencyUnavailableError("odoo is down"),
        StorageError("something unrelated broke"),
        RuntimeError("a bug nobody predicted"),
    ],
)
async def test_every_failure_fails_closed(failure: Exception) -> None:
    """Any way the gateway can fail must end in a refusal, not a bypass.

    ``RuntimeError`` is in this list deliberately. The filter catches broadly on
    purpose: a future exception type escaping uncaught would be a leak, and this
    is the test that says so.
    """
    subject = AuthorizationFilter(gateway(failure=failure))

    with pytest.raises(AuthorizationError):
        await subject.filter(ALICE, [candidate(1, res_id=1)])


async def test_ids_nobody_retrieved_cannot_be_injected_by_a_grant() -> None:
    """A buggy or compromised Odoo cannot add chunks that were never retrieved.

    The filter keeps candidates and never ids, so an id with no candidate behind
    it has no chunk to promote — granting the world adds nothing to the prompt.
    """
    over_generous = FakeOdooGateway(readable={ALICE.token: {"sale.order": list(range(1, 1000))}})
    subject = AuthorizationFilter(over_generous)

    authorized = await subject.filter(ALICE, [candidate(1, res_id=1)])

    assert [chunk.chunk_id for chunk in authorized] == [1]
