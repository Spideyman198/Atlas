"""Tests for the HTTP adapter onto Odoo's Atlas endpoints.

Driven through ``httpx.MockTransport`` so the real request is built — headers,
JSON body, path — and only the wire is faked. A test that constructed its own
client would prove the test's headers were right, not the adapter's.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from atlas.domain.authorization import UserContext
from atlas.domain.errors import (
    AuthorizationError,
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from atlas.infrastructure.odoo.http_gateway import DATABASE_HEADER, OdooHttpGateway

pytestmark = pytest.mark.unit

SERVICE_TOKEN = "service-token"
CONTEXT = UserContext(token="ctx-token", trace_id="trace-1")


def build_gateway(
    handler: Any,
    **kwargs: Any,
) -> OdooHttpGateway:
    return OdooHttpGateway(
        base_url="http://odoo:8069",
        database="odoo",
        service_token=SERVICE_TOKEN,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def responder(payload: dict[str, Any], status_code: int = 200) -> Any:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handle


async def test_authorize_sends_the_service_token_and_the_database() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["database"] = request.headers.get(DATABASE_HEADER)
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"granted": {"sale.order": [1]}})

    async with build_gateway(handle) as gateway:
        await gateway.authorize(CONTEXT, {"sale.order": [1, 2]})

    assert seen["auth"] == f"Bearer {SERVICE_TOKEN}"
    assert seen["database"] == "odoo"
    assert seen["path"] == "/atlas/api/authorize"
    assert seen["body"] == {
        "context_token": "ctx-token",
        "trace_id": "trace-1",
        "records": {"sale.order": [1, 2]},
    }


async def test_authorize_returns_an_entry_for_every_model_asked_about() -> None:
    # Odoo answering about only one model must not read as "the other was
    # granted"; a missing entry has to become an explicit empty set.
    async with build_gateway(responder({"granted": {"sale.order": [1]}})) as gateway:
        granted = await gateway.authorize(CONTEXT, {"sale.order": [1, 2], "res.partner": [9]})

    assert granted == {"sale.order": frozenset({1}), "res.partner": frozenset()}


async def test_authorize_ignores_anything_that_is_not_a_record_id() -> None:
    payload = {"granted": {"sale.order": [1, "2", None, True, 3]}}

    async with build_gateway(responder(payload)) as gateway:
        granted = await gateway.authorize(CONTEXT, {"sale.order": [1, 2, 3]})

    assert granted == {"sale.order": frozenset({1, 3})}


async def test_authorize_makes_one_call_for_every_model() -> None:
    calls: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"granted": {}})

    async with build_gateway(handle) as gateway:
        await gateway.authorize(CONTEXT, {"sale.order": [1], "res.partner": [2]})

    assert len(calls) == 1
    assert set(calls[0]["records"]) == {"sale.order", "res.partner"}


async def test_authorize_skips_the_round_trip_when_there_is_nothing_to_ask() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    async with build_gateway(handle) as gateway:
        assert await gateway.authorize(CONTEXT, {"sale.order": []}) == {}


async def test_an_over_large_batch_is_refused_before_the_round_trip() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    async with build_gateway(handle, max_ids_per_call=2) as gateway:
        with pytest.raises(ValidationError):
            await gateway.authorize(CONTEXT, {"sale.order": [1, 2, 3]})


@pytest.mark.parametrize("status_code", [401, 403])
async def test_refused_credentials_become_an_authorization_error(status_code: int) -> None:
    async with build_gateway(responder({"error": "nope"}, status_code)) as gateway:
        with pytest.raises(AuthorizationError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_a_missing_endpoint_becomes_not_found() -> None:
    async with build_gateway(responder({}, 404)) as gateway:
        with pytest.raises(NotFoundError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_a_rejected_request_becomes_a_validation_error() -> None:
    async with build_gateway(responder({}, 400)) as gateway:
        with pytest.raises(ValidationError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_an_odoo_failure_becomes_a_dependency_error() -> None:
    async with build_gateway(responder({}, 500)) as gateway:
        with pytest.raises(DependencyUnavailableError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_an_unreachable_odoo_becomes_a_dependency_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with build_gateway(handle) as gateway:
        with pytest.raises(DependencyUnavailableError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_a_timeout_becomes_a_dependency_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async with build_gateway(handle) as gateway:
        with pytest.raises(DependencyUnavailableError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_a_nonsense_body_becomes_a_dependency_error() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    async with build_gateway(handle) as gateway:
        with pytest.raises(DependencyUnavailableError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_a_body_without_the_expected_key_becomes_a_dependency_error() -> None:
    # Never silently "nothing was granted": that is indistinguishable from a
    # denial, and only one of the two is safe.
    async with build_gateway(responder({"unexpected": True})) as gateway:
        with pytest.raises(DependencyUnavailableError):
            await gateway.authorize(CONTEXT, {"sale.order": [1]})


async def test_read_records_asks_for_the_fields_it_was_given() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"records": [{"id": 1, "name": "SO001"}]})

    async with build_gateway(handle) as gateway:
        rows = await gateway.read_records(CONTEXT, "sale.order", [1, 2], ["name"])

    assert seen["model"] == "sale.order"
    assert seen["ids"] == [1, 2]
    assert seen["fields"] == ["name"]
    assert rows == [{"id": 1, "name": "SO001"}]


async def test_read_records_skips_the_round_trip_for_no_ids() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    async with build_gateway(handle) as gateway:
        assert await gateway.read_records(CONTEXT, "sale.order", [], ["name"]) == []


async def test_execute_tool_posts_the_name_and_arguments() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"result": {"rows": []}})

    async with build_gateway(handle) as gateway:
        result = await gateway.execute_tool(CONTEXT, "find_records", {"model": "sale.order"})

    assert seen["path"] == "/atlas/api/tool/execute"
    assert seen["tool"] == "find_records"
    assert seen["arguments"] == {"model": "sale.order"}
    assert result == {"rows": []}


async def test_status_reports_what_odoo_answered() -> None:
    payload = {"addon": "odoo_atlas", "version": "19.0", "database": "odoo"}

    async with build_gateway(responder(payload)) as gateway:
        assert await gateway.status() == payload


async def test_status_fails_when_the_addon_is_not_installed() -> None:
    async with build_gateway(responder({}, 404)) as gateway:
        with pytest.raises(NotFoundError):
            await gateway.status()
