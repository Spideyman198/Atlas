"""Tests for the engine's ingestion API and command line.

Both queue work rather than doing it. A full sync of a real ERP runs for
minutes, and an endpoint that waited for one would hold an Odoo cron thread open
for all of them — the coupling ADR-0002 exists to avoid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atlas.config.container import Container
from atlas.domain.ingestion import JobKind
from atlas.domain.sources import source_keys
from atlas.interfaces.cli import _targets, build_parser
from atlas.interfaces.http.errors import register_exception_handlers
from atlas.interfaces.http.ingest import router

pytestmark = pytest.mark.unit


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(
        self,
        source_key: str,
        kind: JobKind,
        *,
        payload: Mapping[str, Any] | None = None,
        run_after: datetime | None = None,
    ) -> int:
        self.enqueued.append(
            {"source_key": source_key, "kind": kind, "payload": dict(payload or {})}
        )
        return len(self.enqueued)


class StubReader:
    def __init__(
        self,
        available: Mapping[str, bool] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self._available = dict(available or {})
        self._failure = failure

    async def available_sources(self) -> Mapping[str, bool]:
        if self._failure is not None:
            raise self._failure
        return self._available


@pytest.fixture
def client() -> Any:
    queue = RecordingQueue()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)

    class _Container:
        job_queue = queue
        source_reader = StubReader({"odoo.res.partner": True, "odoo.crm.lead": False})

    app.state.container = cast(Container, _Container())
    with TestClient(app) as test_client:
        test_client.queue = queue  # type: ignore[attr-defined]
        yield test_client


def test_queueing_a_sync_returns_accepted_not_done(client: Any) -> None:
    response = client.post("/v1/ingest/sync", json={"sources": ["odoo.res.partner"]})

    assert response.status_code == 202
    assert response.json()["queued"] == {"odoo.res.partner": 1}


def test_no_sources_means_every_source(client: Any) -> None:
    response = client.post("/v1/ingest/sync", json={})

    assert response.status_code == 202
    assert set(response.json()["queued"]) == set(source_keys())


def test_the_kind_reaches_the_queue(client: Any) -> None:
    client.post("/v1/ingest/sync", json={"sources": ["odoo.res.partner"], "kind": "reindex"})

    assert client.queue.enqueued[0]["kind"] is JobKind.REINDEX


def test_an_unknown_source_is_refused(client: Any) -> None:
    response = client.post("/v1/ingest/sync", json={"sources": ["odoo.nope"]})

    assert response.status_code == 422
    assert "odoo.nope" in response.json()["detail"]


def test_record_ids_reach_the_payload(client: Any) -> None:
    client.post(
        "/v1/ingest/sync",
        json={"sources": ["odoo.res.partner"], "record_ids": [4, 5]},
    )

    assert client.queue.enqueued[0]["payload"] == {"ids": [4, 5]}


def test_deleted_ids_reach_the_payload(client: Any) -> None:
    client.post(
        "/v1/ingest/sync",
        json={"sources": ["odoo.res.partner"], "deleted_ids": [9]},
    )

    assert client.queue.enqueued[0]["payload"] == {"deleted": [9]}


def test_record_ids_need_exactly_one_source(client: Any) -> None:
    """Ids belong to a model; applying them to several sources means nothing."""
    response = client.post("/v1/ingest/sync", json={"record_ids": [1]})

    assert response.status_code == 422


def test_an_unexpected_field_is_refused(client: Any) -> None:
    response = client.post("/v1/ingest/sync", json={"sources": [], "srcs": ["typo"]})

    assert response.status_code == 422


def test_listing_sources_reports_what_odoo_can_serve(client: Any) -> None:
    response = client.get("/v1/ingest/sources")

    assert response.status_code == 200
    entries = {entry["key"]: entry for entry in response.json()["sources"]}
    assert entries["odoo.res.partner"]["available"] is True
    assert entries["odoo.crm.lead"]["available"] is False
    assert entries["odoo.crm.lead"]["requires_module"] == "crm"


def test_listing_sources_survives_an_unreachable_odoo() -> None:
    """A listing that 500s because Odoo blinked is a listing nobody can use."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)

    class _Container:
        job_queue = RecordingQueue()
        source_reader = StubReader(failure=RuntimeError("odoo is down"))

    app.state.container = cast(Container, _Container())
    with TestClient(app) as test_client:
        response = test_client.get("/v1/ingest/sources")

    assert response.status_code == 200
    assert all(entry["available"] is None for entry in response.json()["sources"])


# --- the command line -------------------------------------------------------


def test_the_parser_accepts_the_documented_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["sources"]).command == "sources"
    assert parser.parse_args(["worker", "--once"]).once is True
    assert parser.parse_args(["sync", "--full"]).full is True
    assert parser.parse_args(["reindex", "--source", "odoo.res.partner"]).sources == [
        "odoo.res.partner"
    ]


def test_no_source_argument_means_every_source() -> None:
    assert _targets(None) == list(source_keys())


def test_named_sources_are_kept_in_the_order_given() -> None:
    requested: Sequence[str] = ["odoo.sale.order", "odoo.res.partner"]

    assert _targets(requested) == list(requested)


def test_an_unknown_source_exits_with_the_ones_that_exist() -> None:
    with pytest.raises(SystemExit, match=r"odoo\.res\.partner"):
        _targets(["odoo.not.a.thing"])
